"""Deterministic course traversal and attachment downloads, without model calls."""
import json
import re
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit, parse_qs, urlencode, urlunsplit

from browser_rpc import save_download
from google_api import write_private
from store import utc_now


def link_kind(url):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
        return None
    if parsed.hostname == "docs.google.com" and re.search(r"/(document|spreadsheets|presentation)(?:/u/\d+)?/d/[\w-]+", parsed.path):
        return "file"
    if parsed.hostname == "drive.google.com":
        if re.search(r"/folders/[\w-]+", parsed.path):
            return "folder"
        if re.search(r"/file(?:/u/\d+)?/d/[\w-]+", parsed.path) or parse_qs(parsed.query).get("id"):
            return "file"
    if parsed.hostname == "classroom.google.com" and re.search(r"/[cw]/[\w-]+", parsed.path):
        return "course"
    return None


def resource_key(url):
    """Deduplicate Drive IDs across /open?id=, /file/d/, and native Docs links."""
    parsed = urlsplit(url)
    match = re.search(r"/(?:d|folders)/([\w-]+)", parsed.path)
    file_id = match.group(1) if match else parse_qs(parsed.query).get("id", [None])[0]
    if parsed.hostname in {"drive.google.com", "docs.google.com"} and file_id:
        return "google-file:" + file_id
    # Account selector/resource key does not make a second copy of a course page.
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def course_id(url):
    if urlsplit(url).hostname != "classroom.google.com":
        return None
    match = re.search(r"/[cw]/([\w-]+)", urlsplit(url).path)
    return match.group(1) if match else None


def assignment_id(url):
    if not course_id(url):
        return None
    match = re.search(r"/[am]/([\w-]+)", urlsplit(url).path)
    return match.group(1) if match else None


def project_key(url):
    course = course_id(url)
    return ("classroom:" + course + ":" + (assignment_id(url) or "course")) if course else resource_key(url)


def inherit_account(url, parent):
    """Carry the observed account to linked pages; never try alternative accounts."""
    target, source = urlsplit(url), urlsplit(parent)
    if target.hostname not in {"classroom.google.com", "drive.google.com", "docs.google.com"}:
        return url
    query = parse_qs(target.query, keep_blank_values=True)
    if "authuser" in query:
        return url
    account = re.search(r"/u/(\d+)(?:/|$)", target.path)
    if account:
        value = account.group(1)
    else:
        value = parse_qs(source.query).get("authuser", [None])[0]
        inherited = re.search(r"/u/(\d+)(?:/|$)", source.path)
        if value is None and inherited:
            value = inherited.group(1)
    if value is None:
        return url
    query["authuser"] = [value]
    return urlunsplit((target.scheme, target.netloc, target.path, urlencode(query, doseq=True), target.fragment))


class CourseCollector:
    def __init__(self, store, broker):
        self.store, self.broker = store, broker
        self.lock = threading.RLock()
        self.tasks = {}
        self.download_callbacks = {}
        self.folder = store.root / "collections"
        if store.storage_available:
            self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.cache_path = store.root / "download-cache.json"
        self.cache = json.loads(self.cache_path.read_text(encoding="utf-8")) if self.cache_path.exists() else {}

    def _save(self, task):
        with self.lock:
            write_private(self.folder / (task["task_id"] + ".json"), task)

    def start(self, tab_id, max_pages=10, max_files=30, max_scrolls=20, download_callback=None, force_download=False, recheck_seconds=86400):
        self.store.require_available()
        if type(force_download) is not bool or type(recheck_seconds) is not int or not 0 <= recheck_seconds <= 604800:
            raise ValueError("复查参数无效")
        if download_callback is not None and not callable(download_callback):
            raise ValueError("下载回调无效")
        for name, value, upper in (("tab_id", tab_id, 2**31 - 1), ("max_pages", max_pages, 1000), ("max_files", max_files, 10000), ("max_scrolls", max_scrolls, 200)):
            if type(value) is not int or value < (0 if name in {"tab_id", "max_files"} else 1) or value > upper:
                raise ValueError("无效的批处理参数：" + name)
        if not self.broker.status()["connected"]:
            raise ValueError("请先在 Chrome 中连接浏览器")
        with self.lock:
            if any(t["status"] == "running" for t in self.tasks.values()):
                raise ValueError("已有采集脚本在运行；先查看该任务，避免同时浏览相同课程")
            task_id = secrets.token_hex(12)
            task = {"task_id": task_id, "status": "running", "started_at": utc_now(),
                    "settings": {"tab_id": tab_id, "max_pages": max_pages, "max_files": max_files, "max_scrolls": max_scrolls, "force_download": force_download, "recheck_seconds": recheck_seconds},
                    "pages": [], "downloads": [], "errors": [], "skipped_links": 0,
                    "coverage_complete": False, "note": "脚本遍历渲染页面及其中发现的文件/文件夹，不代表已覆盖整门课程。"}
            self.tasks[task_id] = task
            self.download_callbacks[task_id] = download_callback
            self._save(task)
        threading.Thread(target=self._run, args=(task_id,), daemon=True).start()
        return {"task_id": task_id, "status": "running", "next_step": "脚本会自动遍历、去重、下载和建索引；用 course_collection_status 查看结果，无需逐页调用模型。"}

    def status(self, task_id):
        if not isinstance(task_id, str) or not re.fullmatch(r"[a-f0-9]{24}", task_id):
            raise ValueError("无效的任务编号")
        path = self.folder / (task_id + ".json")
        if not path.exists():
            raise ValueError("没有找到此采集任务")
        with self.lock:
            task = json.loads(path.read_text(encoding="utf-8"))
        if task["status"] == "running" and task_id not in self.tasks:
            task.update(status="interrupted", note="服务重启前此任务尚未完成；不会自动恢复，请核对已下载结果后重新启动。")
        return task

    def _run(self, task_id):
        task = self.tasks[task_id]
        settings = task["settings"]
        created_tabs = []
        pages = [(settings["tab_id"], None)]
        seen_pages, seen_files = set(), set()
        files = []
        root_course = None
        root_assignment = None
        try:
            while pages and len(task["pages"]) < settings["max_pages"]:
                tab_id, url = pages.pop(0)
                try:
                    if tab_id is None:
                        opened = self.broker.call("open", {"url": url})
                        tab_id = opened["tab_id"]
                        created_tabs.append(tab_id)
                    result = self.broker.call("collect", {"tab_id": tab_id, "max_scrolls": settings["max_scrolls"]})
                    capture = result["capture"]
                    if root_course is None and not task["pages"]:
                        root_course = course_id(capture["source_url"])
                        root_assignment = assignment_id(capture["source_url"])
                    seen_pages.add(resource_key(capture["source_url"]))
                    if link_kind(capture["source_url"]) == "file":
                        key = resource_key(capture["source_url"])
                        if key not in seen_files:
                            seen_files.add(key)
                            files.append({"url": capture["source_url"], "title": capture["title"], "source_url": capture["source_url"]})
                    material_id = self.store.put_many([capture])[0]
                    task["pages"].append({"title": capture["title"], "source_url": capture["source_url"],
                                          "material_id": material_id, "scans": result.get("scans"), "warnings": capture.get("warnings", [])})
                    for link in capture.get("links", []):
                        target = inherit_account(link["url"], capture["source_url"])
                        key, kind = resource_key(target), link_kind(target)
                        if kind == "file":
                            if key not in seen_files:
                                seen_files.add(key)
                                files.append({"url": target, "title": link["title"], "source_url": capture["source_url"]})
                        elif kind == "folder" or kind == "course" and root_course and course_id(target) == root_course and (not root_assignment or assignment_id(target) == root_assignment):
                            if key not in seen_pages:
                                seen_pages.add(key)
                                pages.append((None, target))
                        else:
                            task["skipped_links"] += 1
                    task["files_discovered"] = len(files)
                    task["discovered_files"] = files
                    self._save(task)
                except Exception as error:
                    task["errors"].append({"stage": "page", "source_url": url, "error": str(error)[:700]})
                    self._save(task)
                    if not self.broker.status()["connected"]:
                        raise ValueError("浏览器连接已断开，脚本停止") from None
                finally:
                    if tab_id in created_tabs:
                        try:
                            self.broker.call("close", {"tab_id": tab_id}, timeout=10)
                        except ValueError:
                            pass
                        created_tabs.remove(tab_id)
            task["unvisited_pages"] = len(pages)
            if pages:
                task["errors"].append({"stage": "limit", "error": "已达到页面数量上限，还有已发现的页面尚未遍历"})
            for file in files[:settings["max_files"]]:
                try:
                    cached = self.cache.get(resource_key(file["url"]))
                    usable = cached and Path(cached.get("download_path", "")).is_file() and Path(cached["download_path"]).resolve().is_relative_to((self.store.root / "attachments").resolve())
                    request = dict(file)
                    if usable:
                        request["cache"] = {k: cached[k] for k in ("etag", "last_modified", "validator_scope") if cached.get(k)}
                    deferred = bool(usable and cached.get("bytes", 0) > 20*1024*1024 and not cached.get("etag") and not cached.get("last_modified") and not settings["force_download"] and time.time() - cached.get("checked_at", 0) < settings["recheck_seconds"])
                    result = {"not_modified": True} if deferred else self.broker.call("download_url", request)
                    if result.get("skipped"):
                        task.setdefault("skipped_downloads", []).append(dict(file, reason=result.get("reason", "本次跳过")))
                        self._save(task)
                        continue
                    if result.get("not_modified"):
                        if not usable:
                            raise ValueError("附件缓存不可用，请重新同步")
                        saved = dict(cached, reused=True)
                    else:
                        saved = save_download(self.store, result)
                    # Stable across validation/deferred rounds: this is a source capability,
                    # not a content change. Otherwise large snapshots alternate every cycle.
                    if saved.get("bytes", 0) > 20*1024*1024 and not saved.get("etag") and not saved.get("last_modified"):
                        saved["freshness_warning"] = "来源未提供版本标记；按设置定期完整复查，期间暂用已保存副本，点击立即更新可马上复查。"
                        task.setdefault("freshness_warnings", []).append({"source_url": file["url"], "warning": saved["freshness_warning"]})
                    else:
                        saved.pop("freshness_warning", None)
                    if not deferred:
                        saved["checked_at"] = time.time()
                    self.cache[resource_key(file["url"])] = saved
                    write_private(self.cache_path, self.cache)
                    task["downloads"].append(saved)
                    callback = self.download_callbacks.get(task_id)
                    if callback:
                        callback(saved)
                    if not saved.get("material_id"):
                        task.setdefault("extraction_warnings", []).append({"source_url": file["url"], "warning": saved.get("extraction_error", "原文件已下载，未提取正文")})
                except Exception as error:
                    task["errors"].append({"stage": "download", "source_url": file["url"], "error": str(error)[:700]})
                    if not self.broker.status()["connected"]:
                        raise ValueError("浏览器连接已断开，脚本停止") from None
                self._save(task)
            task["undownloaded_due_to_limit"] = max(0, len(files) - settings["max_files"])
            task["status"] = "completed_with_gaps" if task["errors"] or task["undownloaded_due_to_limit"] or task.get("skipped_downloads") else "completed"
        except Exception as error:
            task["status"] = "interrupted"
            task["errors"].append({"stage": "connection", "error": str(error)[:700]})
        finally:
            task["finished_at"] = utc_now()
            self._save(task)
            self.download_callbacks.pop(task_id, None)
