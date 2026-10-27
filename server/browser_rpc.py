"""Authenticated loopback command queue for the user's connected Chrome tab.

No arbitrary JavaScript, cookie extraction, arbitrary path reads or shell commands.
The browser receives only the fixed operations implemented in extension/agent.js.
"""
import base64
import hashlib
import json
import queue
import re
import secrets
import os
import stat
import tempfile
import shutil
import threading
import time
from pathlib import Path
from urllib.error import URLError, HTTPError
from urllib.request import Request, build_opener, ProxyHandler

from google_api import extract_text
from file_ops import mark_internet_file, safe_filename, file_hash
from store import utc_now, validate_item

OPERATIONS = {"tabs", "open", "read", "click", "scroll", "download", "collect", "download_url", "close"}
MAX_RESULT = 30 * 1024 * 1024
MAX_DOWNLOAD = 20 * 1024 * 1024


def native_download_path(result, expected_token):
    """Import only this pending job's browser-created file, never an arbitrary local path."""
    reference = result.get("native_download")
    if not isinstance(reference, dict) or not isinstance(expected_token, str) or not re.fullmatch(r"[a-f0-9]{32}", expected_token):
        raise ValueError("本机下载任务标识无效")
    if reference.get("token") != expected_token:
        raise ValueError("本机下载结果不属于当前任务")
    raw_path = reference.get("path")
    if not isinstance(raw_path, str) or len(raw_path) > 4000:
        raise ValueError("本机下载路径无效")
    validate_item({"title": "浏览器附件", "source_url": result.get("source_url", ""), "text": "",
                   "coverage": "browser_attachment_text", "captured_at": utc_now(), "links": [], "warnings": []})
    if raw_path.startswith(("\\\\", "//")):
        raise ValueError("自动归档只接受本机文件，不读取网络共享路径")
    path = Path(raw_path)
    if not path.is_absolute() or path.is_symlink() or path.parent.is_symlink() or path.parent.parent.is_symlink():
        raise ValueError("本机下载文件必须位于当前任务专用目录")
    try:
        path = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ValueError("Chrome 报告下载完成，但本机文件尚不可读") from None
    if path.parent.name != expected_token or path.parent.parent.name != "CourseMaterialAssistant":
        raise ValueError("拒绝读取任务专用目录以外的文件")
    return path


def read_native_download(result, expected_token):
    path = native_download_path(result, expected_token)
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_DOWNLOAD:
                raise ValueError("下载文件为空、不是普通文件或超过 20 MiB")
            content = handle.read(MAX_DOWNLOAD + 1)
    except OSError:
        raise ValueError("Chrome 下载文件暂时无法读取，未导入；请检查文件占用后重新同步") from None
    if len(content) > MAX_DOWNLOAD:
        raise ValueError("下载文件超过 20 MiB")
    prefix = content[:4096].lstrip().lower()
    if str(result.get("mime", "")).split(";")[0] in {"text/html", "application/xhtml+xml"} or prefix.startswith((b"<!doctype html", b"<html")):
        raise ValueError("Chrome 下载得到的是网页，尚未取得附件文件")
    output = {key: value for key, value in result.items() if key != "native_download"}
    output.update(data_base64=base64.b64encode(content).decode("ascii"), download_mode="chrome_downloads")
    return output


def import_native_download(store, result, expected_token, threshold_bytes, approved_bytes=0):
    with store.material_access():
        return _import_native_download(store, result, expected_token, threshold_bytes, approved_bytes)


def _import_native_download(store, result, expected_token, threshold_bytes, approved_bytes=0):
    """Stream to the local attachment store. Download size is independent of text extraction."""
    source = native_download_path(result, expected_token)
    filename = safe_filename(result.get("filename", "attachment.bin"))
    attachments = store.root / "attachments"
    attachments.mkdir(exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".incoming-", dir=attachments)
    digest, size = hashlib.sha256(), 0
    try:
        with os.fdopen(descriptor, "wb") as output:
            read_handle = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(read_handle, "rb") as incoming:
                info = os.fstat(incoming.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
                    raise ValueError("下载文件为空或不是普通文件")
                if info.st_size > threshold_bytes and approved_bytes is not None and info.st_size > max(threshold_bytes, approved_bytes):
                    raise ValueError("大文件尚未获得本次下载确认，未导入")
                if shutil.disk_usage(attachments).free < info.st_size + 16 * 1024 * 1024:
                    raise ValueError("本机资料库磁盘空间不足，未导入附件")
                first = incoming.read(1024 * 1024)
                prefix = first[:4096].removeprefix(b"\xef\xbb\xbf").lstrip().lower()
                if str(result.get("mime", "")).split(";")[0] in {"text/html", "application/xhtml+xml"} or prefix.startswith((b"<!doctype html", b"<html")):
                    raise ValueError("Chrome 返回的是网页，尚未取得原附件")
                chunk = first
                while chunk:
                    digest.update(chunk)
                    output.write(chunk)
                    size += len(chunk)
                    if size > max(threshold_bytes, approved_bytes or 0) and approved_bytes is not None:
                        raise ValueError("文件实际大小超过本次确认范围")
                    chunk = incoming.read(1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
        hexdigest = digest.hexdigest()
        folder = attachments / hexdigest[:24]
        folder.mkdir(exist_ok=True, mode=0o700)
        target = folder / filename
        if not target.is_file() or file_hash(target) != hexdigest:
            os.replace(temporary, target)
        target.chmod(0o600)
        mark_internet_file(target)
        saved = {"downloaded": True, "filename": filename, "download_path": str(target.resolve()),
                 "source_url": result["source_url"], "bytes": size, "sha256": hexdigest, "download_mode": "chrome_downloads", "_native_imported": True}
        for key in ("etag", "last_modified", "validator_scope"):
            value = result.get(key)
            if isinstance(value, str) and len(value) < 1000 and "\r" not in value and "\n" not in value:
                saved[key] = value
        if size <= MAX_DOWNLOAD:
            try:
                text, warnings = extract_text(target.read_bytes(), filename, str(result.get("mime", "")))
                if len(text) > 500000:
                    warnings.append("正文超过 500000 字符，已截断；原文件完整保留")
                item = {"title": filename, "source_url": result["source_url"], "text": text[:500000],
                        "coverage": "browser_attachment_text", "captured_at": utc_now(), "links": [], "warnings": warnings}
                saved.update(material_id=store.put_many([item])[0], text_extracted=True, chars=len(item["text"]), warnings=warnings)
            except Exception as error:
                saved.update(text_extracted=False, extraction_error=str(error)[:500])
        else:
            saved.update(text_extracted=False, extraction_error="大文件原件已完整保存；为控制内存，本机未提取正文，可直接打开或同步原文件给 AI")
        return saved
    except OSError:
        raise ValueError("本机无法读取或保存 Chrome 下载文件，请检查磁盘空间和文件占用") from None
    finally:
        Path(temporary).unlink(missing_ok=True)


class BrowserBroker:
    def __init__(self, lease=45, store=None):
        self.store = store
        self.lease = lease
        self.lock = threading.Lock()
        self.session_id = None
        self.last_seen = 0
        self.pending = queue.Queue(maxsize=32)
        self.jobs = {}

    def connect(self, session_id):
        if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,100}", session_id):
            raise ValueError("无效的浏览器会话")
        with self.lock:
            if self.session_id and self.session_id != session_id and time.monotonic() - self.last_seen < self.lease:
                raise ValueError("已有一个浏览器连接页在线，请先关闭它或在原页继续连接")
            if self.session_id != session_id:
                self._cancel_locked("浏览器会话已更换，请重新读取页面")
            self.session_id = session_id
            self.last_seen = time.monotonic()
        return {"connected": True}

    def _check_locked(self, session_id, touch=True):
        if session_id != self.session_id or not self.session_id:
            raise ValueError("浏览器连接已失效，请在扩展连接页重新连接")
        if touch:
            self.last_seen = time.monotonic()

    def _cancel_locked(self, message):
        for job in self.jobs.values():
            job["result"] = {"error": message}
            job["event"].set()
        self.jobs.clear()
        while True:
            try:
                self.pending.get_nowait()
            except queue.Empty:
                break

    def disconnect(self, session_id):
        with self.lock:
            self._check_locked(session_id)
            self._cancel_locked("浏览器已断开连接")
            self.session_id = None
        return {"connected": False}

    def status(self):
        with self.lock:
            age = time.monotonic() - self.last_seen
            active = next((j for j in self.jobs.values() if j["delivered"] and not j["event"].is_set()), None)
            return {"connected": bool(self.session_id and age < self.lease),
                    "pending_commands": len(self.jobs),
                    "active": {"operation": active["operation"], "phase": active.get("phase", "working"), "bytes": active.get("bytes", 0)} if active else None}

    def call(self, operation, arguments, timeout=80):
        if operation not in OPERATIONS or not isinstance(arguments, dict):
            raise ValueError("不支持的浏览器操作")
        if operation in {"download", "download_url"}:
            policy = getattr(self, "download_settings", lambda: {"threshold_bytes": 50 * 1024 * 1024})()
            arguments = dict(arguments, download_token=secrets.token_hex(16), download_threshold_bytes=policy["threshold_bytes"])
            if timeout == 80:
                timeout = 180
        with self.lock:
            if not self.session_id or time.monotonic() - self.last_seen >= self.lease:
                raise ValueError("浏览器尚未连接：请启动本机服务，并在 Chrome 扩展中打开「连接浏览器」页面。无需逐页采集。")
            if any(j["delivered"] and j["operation"] in {"download", "download_url"} and not j["event"].is_set() for j in self.jobs.values()):
                raise ValueError("浏览器正在下载附件或等待大文件确认，请在当前任务完成后重试")
            job_id = secrets.token_urlsafe(24)
            job = {"id": job_id, "operation": operation, "arguments": arguments,
                   "session_id": self.session_id, "event": threading.Event(),
                   "expires": time.monotonic() + min(timeout, 80), "timeout": timeout, "delivered": False}
            try:
                self.pending.put_nowait(job_id)
            except queue.Full:
                raise ValueError("浏览器操作排队已满，请稍后重试") from None
            self.jobs[job_id] = job
        complete = False
        while True:
            if job["event"].wait(.5):
                complete = True
                break
            with self.lock:
                if time.monotonic() >= job["expires"]:
                    break
        with self.lock:
            self.jobs.pop(job_id, None)
        if not complete:
            raise ValueError("浏览器操作超时；先读取当前页面核对状态，再决定是否重试点击或下载")
        result = job["result"]
        if isinstance(result, dict) and result.get("error"):
            raise ValueError(str(result["error"])[:1000])
        return result

    def next(self, session_id, timeout=20):
        with self.lock:
            self._check_locked(session_id)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                self._check_locked(session_id, touch=False)
            try:
                job_id = self.pending.get(timeout=max(.001, min(1, deadline - time.monotonic())))
            except queue.Empty:
                continue
            with self.lock:
                if session_id != self.session_id:
                    if job_id in self.jobs:
                        self.pending.put_nowait(job_id)
                    raise ValueError("浏览器连接已更换")
                self._check_locked(session_id, touch=False)
                job = self.jobs.get(job_id)
                if not job or job["session_id"] != session_id or job["expires"] <= time.monotonic():
                    continue
                job["delivered"] = True
                job["expires"] = time.monotonic() + job["timeout"]
                return {"job": {key: job[key] for key in ("id", "operation", "arguments")}}
        return {"job": None}

    def progress_download(self, token, received):
        if not isinstance(token, str) or type(received) is not int or received < 0:
            raise ValueError("下载进度无效")
        with self.lock:
            job = next((j for j in self.jobs.values() if j["arguments"].get("download_token") == token and j["delivered"] and not j["event"].is_set() and j["expires"] > time.monotonic()), None)
            if job is None:
                raise ValueError("下载任务已结束")
            if received > job.get("bytes", -1):
                job.update(bytes=received, phase="downloading", expires=time.monotonic() + max(180, job["timeout"]))
        return {"accepted": True}

    def decide_download(self, token, name, size, reason="large_file"):
        if reason not in {"large_file", "google_scan_warning"}:
            raise ValueError("下载确认类型无效")
        if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{32}", token) or not isinstance(name, str) or len(name) > 500:
            raise ValueError("下载确认参数无效")
        if size is not None and (type(size) is not int or size < 0 or size > 10**13):
            raise ValueError("文件大小无效")
        with self.lock:
            job = next((j for j in self.jobs.values() if j["arguments"].get("download_token") == token and j["delivered"] and not j["event"].is_set() and j["expires"] > time.monotonic()), None)
            if job is None:
                raise ValueError("下载任务已过期")
            threshold = job["arguments"]["download_threshold_bytes"]
            if reason == "large_file" and size is not None and size <= max(threshold, job.get("approved_bytes") or 0):
                return {"allowed": True}
            if reason == "large_file" and job.get("approved_bytes", 0) is None:
                return {"allowed": True}
            job.update(phase="awaiting_confirmation", expires=time.monotonic() + 330)
        source_url = job["arguments"].get("url", "")
        lookup = getattr(self, "lookup_download_choice", None)
        previous = lookup(source_url, size) if lookup and reason == "large_file" else None
        if reason == "google_scan_warning":
            warning_lookup = getattr(self, "lookup_google_warning_choice", None)
            previous = warning_lookup(source_url) if warning_lookup else None
        callback = getattr(self, "confirm_download", None)
        if previous is None:
            answer = callback({"filename": safe_filename(name), "bytes": size, "threshold_bytes": threshold, "reason": reason, "timeout_seconds": 300, "cancelled": job["event"]}) if callback else False
            allowed = bool(answer.get("allowed")) if isinstance(answer, dict) else bool(answer)
            remember = bool(answer.get("remember")) if isinstance(answer, dict) else bool(callback)
            save = getattr(self, "save_download_choice", None)
            if save and remember and reason == "large_file":
                save(source_url, size, allowed)
            if remember and reason == "google_scan_warning":
                warning_save = getattr(self, "save_google_warning_choice", None)
                if warning_save:
                    warning_save(source_url, allowed)
        else:
            allowed = previous
        with self.lock:
            if job["id"] not in self.jobs or job["event"].is_set():
                raise ValueError("下载确认期间任务已结束")
            job.update(phase="downloading", expires=time.monotonic() + max(180, job["timeout"]))
            if allowed and reason == "large_file":
                job["approved_bytes"] = size  # None explicitly authorizes this single unknown-size file.
        return {"allowed": allowed}

    def finish(self, session_id, job_id, result):
        with self.lock:
            self._check_locked(session_id)
            job = self.jobs.get(job_id)
            if not job or job["session_id"] != session_id or not job["delivered"] or job["event"].is_set() or job["expires"] < time.monotonic() or job.get("importing"):
                raise ValueError("此浏览器结果已过期或任务不存在")
            if not isinstance(result, dict) or "_native_imported" in result:
                raise ValueError("浏览器结果无效或试图伪造入库状态")
            native = "native_download" in result
            if native and job["operation"] not in {"download", "download_url"}:
                raise ValueError("当前任务不是附件下载")
            job["importing"] = native
            if native:
                job.update(phase="importing", expires=time.monotonic() + 1800)
        # File I/O must not hold the broker lock: browser heartbeats continue while importing.
        try:
            if native:
                if self.store is not None:
                    result = import_native_download(self.store, result, job["arguments"].get("download_token"),
                                                    job["arguments"]["download_threshold_bytes"], job.get("approved_bytes", 0))
                else:
                    result = read_native_download(result, job["arguments"].get("download_token"))
        except (ValueError, OSError) as error:
            with self.lock:
                job["result"] = {"error": str(error)[:700]}
                job["event"].set()
            raise
        with self.lock:
            self._check_locked(session_id)
            if job["id"] not in self.jobs or job["event"].is_set():
                raise ValueError("导入期间浏览器任务已结束，已保存的原文件保留")
            job["result"] = result
            job["event"].set()
        return {"accepted": True}


class BrowserClient:
    def __init__(self, root, port=47821):
        self.root = Path(root)
        self.port = port

    def post(self, path, payload, timeout=95):
        token_path = self.root / "pairing.json"
        if not token_path.exists():
            raise ValueError("尚未启动本机资料桥；请运行 start-bridge 或 server/app.py serve")
        token = json.loads(token_path.read_text(encoding="utf-8"))["token"]
        req = Request(f"http://127.0.0.1:{self.port}" + path,
                      data=json.dumps(payload).encode(), headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
        # This fixed loopback connection must not forward its credential to an HTTP proxy.
        try:
            with build_opener(ProxyHandler({})).open(req, timeout=timeout) as response:
                raw = response.read(MAX_RESULT + 1)
                if len(raw) > MAX_RESULT:
                    raise ValueError("浏览器返回结果过大")
                return json.loads(raw)
        except HTTPError as error:
            try:
                payload = json.loads(error.read(4000))
                message = str(payload.get("error", "浏览器连接错误"))[:1000]
            except (ValueError, AttributeError):
                message = f"浏览器连接 HTTP {error.code}"
            raise ValueError(message) from None
        except (URLError, TimeoutError):
            raise ValueError("无法连接本机资料桥；请确认服务正在运行，且连接页保持打开") from None

    def status(self):
        try:
            return self.post("/browser/status", {}, timeout=2)
        except (ValueError, OSError):
            return {"connected": False, "service_reachable": False}

    def call(self, operation, arguments):
        return self.post("/browser/command", {"operation": operation, "arguments": arguments},
                         timeout=86400 if operation in {"download", "download_url"} else 95)


def save_download(store, result):
    with store.material_access():
        return _save_download(store, result)


def _save_download(store, result):
    if result.get("_native_imported"):
        path = Path(result.get("download_path", "")).resolve()
        if not path.is_file() or not path.is_relative_to((store.root / "attachments").resolve()):
            raise ValueError("入库附件路径无效")
        return {k: v for k, v in result.items() if k != "_native_imported"}
    encoded = result.get("data_base64")
    if not isinstance(encoded, str) or len(encoded) > 28 * 1024 * 1024:
        raise ValueError("下载数据无效或超过 20 MiB")
    try:
        content = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        raise ValueError("下载数据编码无效") from None
    if not content or len(content) > 20 * 1024 * 1024:
        raise ValueError("下载文件为空或超过 20 MiB")
    source_url = result.get("source_url", "")
    filename = safe_filename(result.get("filename", "attachment.bin"))
    reference = {"title": filename, "source_url": source_url, "text": "", "coverage": "browser_attachment_text",
                 "captured_at": utc_now(), "links": [], "warnings": []}
    validate_item(reference)
    digest = hashlib.sha256(content).hexdigest()
    folder = store.root / "attachments" / digest[:24]
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = folder / filename
    if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        # A fresh name is never interpreted as a command or extracted onto disk.
        import tempfile
        import os
        handle, pending = tempfile.mkstemp(dir=folder, suffix=".part")
        try:
            with os.fdopen(handle, "wb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(pending, path)
            path.chmod(0o600)
        finally:
            if os.path.exists(pending):
                os.unlink(pending)
    mark_internet_file(path)
    saved = {"downloaded": True, "filename": filename, "download_path": str(path.resolve()),
             "source_url": source_url, "bytes": len(content), "sha256": digest}
    for key in ("etag", "last_modified", "validator_scope"):
        value = result.get(key)
        if isinstance(value, str) and len(value) < 1000 and "\r" not in value and "\n" not in value:
            saved[key] = value
    try:
        text, warnings = extract_text(content, filename, str(result.get("mime", "")))
        if len(text) > 500000:
            warnings.append("正文超过 500000 字符，已截断")
        reference.update(text=text[:500000], warnings=warnings)
        saved.update(material_id=store.put_many([reference])[0], text_extracted=True, chars=len(reference["text"]), warnings=warnings,
                     next_step="用 read_material 分页读取全文")
    except Exception as error:
        saved.update(text_extracted=False, extraction_error=str(error)[:500])
    return saved
