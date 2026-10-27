"""HTTPS-only updater for this application's pinned public release service."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener

from file_ops import file_hash, mark_internet_file

ORIGIN = "https://github.com/Taimai07/course-material-assistant"
FEED_URL = "https://raw.githubusercontent.com/Taimai07/course-material-assistant/main/updates/windows/stable.json"


def version_tuple(text):
    if not isinstance(text, str) or not re.fullmatch(r"\d{1,5}\.\d{1,5}\.\d{1,5}", text):
        raise ValueError("更新服务返回的版本号无效")
    return tuple(int(n) for n in text.split("."))


def trusted_url(url):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.port not in (None, 443) or parsed.username or parsed.password or parsed.fragment:
        return False
    if url == FEED_URL:
        return True
    # GitHub releases redirect bytes to a short-lived signed URL on their asset host.
    # Manifests themselves must name the exact pinned repository URL, checked separately.
    if parsed.hostname == "release-assets.githubusercontent.com":
        return parsed.path.startswith("/github-production-release-asset/")
    return not parsed.query and bool(re.fullmatch(re.escape(ORIGIN) + r"/releases/download/v\d{1,5}\.\d{1,5}\.\d{1,5}/[a-f0-9]{64}\.exe", url))


class UpdateRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not trusted_url(newurl):
            raise ValueError("更新下载跳转到了未知站点，已停止")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def validate_manifest(data):
    if not isinstance(data, dict) or data.get("schema") != 1 or data.get("app_id") != "course-material-assistant" or data.get("platform") != "windows" or data.get("architecture") != "x64":
        raise ValueError("更新清单不是此应用的 Windows 安装包")
    version_tuple(data.get("version"))
    sha = data.get("sha256", "")
    size = data.get("size")
    if not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{64}", sha) or type(size) is not int or size <= 0 or size > 1024*1024*1024:
        raise ValueError("更新安装包的大小或校验信息无效")
    url = data.get("url", "")
    if not isinstance(url, str) or not trusted_url(url) or url != ORIGIN + f"/releases/download/v{data['version']}/{sha}.exe":
        raise ValueError("更新安装包地址不属于官方更新服务")
    if not isinstance(data.get("notes", ""), str) or len(data.get("notes", "")) > 8000:
        raise ValueError("更新说明无效")
    return dict(data)


def check(current_version, opener=None):
    opener = opener or build_opener(UpdateRedirect())
    request = Request(FEED_URL, headers={"Accept": "application/json", "User-Agent": "CourseMaterialAssistant/" + current_version})
    try:
        with opener.open(request, timeout=12) as response:
            if response.geturl() != FEED_URL:
                raise ValueError("更新清单地址无效")
            raw = response.read(65537)
            if len(raw) > 65536:
                raise ValueError("更新清单过大")
            result = validate_manifest(json.loads(raw))
    except HTTPError as error:
        if error.code in {404, 503}:
            raise ValueError("更新服务正在准备安装包，请稍后再试；资料同步不受影响") from None
        raise ValueError(f"检查更新未完成（HTTP {error.code}），稍后可在设置中重试") from None
    except (URLError, TimeoutError):
        raise ValueError("暂时无法连接更新服务，可在设置中重试") from None
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ValueError("更新服务没有返回有效的版本清单") from None
    return result if version_tuple(result["version"]) > version_tuple(current_version) else None


def download(manifest, control_root, progress=None, cancelled=None, opener=None):
    info = validate_manifest(manifest)
    progress = progress or (lambda done, total: None)
    cancelled = cancelled or (lambda: False)
    opener = opener or build_opener(UpdateRedirect())
    folder = Path(control_root) / "updates"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / ("CourseMaterialAssistant-" + info["version"] + "-" + info["sha256"][:12] + ".exe")
    if target.is_file() and target.stat().st_size == info["size"] and file_hash(target) == info["sha256"]:
        mark_internet_file(target)
        return target
    if shutil.disk_usage(folder).free < info["size"] + 32*1024*1024:
        raise ValueError("安装包保存盘空间不足，未开始下载更新")
    descriptor, temporary = tempfile.mkstemp(prefix=".update-", dir=folder)
    digest, received = hashlib.sha256(), 0
    try:
        with os.fdopen(descriptor, "wb") as output, opener.open(Request(info["url"], headers={"User-Agent": "CourseMaterialAssistant-Updater"}), timeout=30) as response:
            if not trusted_url(response.geturl()):
                raise ValueError("安装包下载地址无效")
            announced = response.headers.get("Content-Length")
            if announced is not None and announced != str(info["size"]):
                raise ValueError("安装包大小与版本清单不一致")
            if "text/html" in response.headers.get("Content-Type", "").lower():
                raise ValueError("服务器返回了网页，未打开安装程序")
            while True:
                if cancelled():
                    raise ValueError("已取消下载更新")
                chunk = response.read(1024*1024)
                if not chunk:
                    break
                received += len(chunk)
                if received > info["size"]:
                    raise ValueError("安装包超过版本清单声明的大小")
                digest.update(chunk)
                output.write(chunk)
                progress(received, info["size"])
            output.flush()
            os.fsync(output.fileno())
        if received != info["size"] or digest.hexdigest() != info["sha256"]:
            raise ValueError("安装包校验失败，未打开安装程序，请重试")
        with Path(temporary).open("rb") as handle:
            if handle.read(2) != b"MZ":
                raise ValueError("下载内容不是 Windows 安装程序")
        os.replace(temporary, target)
        mark_internet_file(target)
        return target
    except (HTTPError, URLError, TimeoutError, OSError) as error:
        raise ValueError("下载安装包失败，请检查网络和磁盘空间后重试") from None
    finally:
        Path(temporary).unlink(missing_ok=True)


def _windows_shell_open(path):
    if os.name != "nt":
        raise ValueError("自动打开安装器目前仅支持 Windows")
    os.startfile(str(path), "open")


def launch(path, manifest, control_root):
    info = validate_manifest(manifest)
    path = Path(path).resolve()
    if not path.is_relative_to((Path(control_root) / "updates").resolve()) or path.suffix != ".exe" or not path.is_file() or path.stat().st_size != info["size"] or file_hash(path) != info["sha256"]:
        raise ValueError("安装包在启动前校验失败，请重新下载")
    # Shell open preserves normal Windows reputation/UAC handling. Never run a shell command.
    _windows_shell_open(path)
