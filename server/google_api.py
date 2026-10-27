"""Optional Google read-only API integration. Never reads browser credentials."""
import base64
import hashlib
import io
import json
import os
import secrets
import threading
import time
import tempfile
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener
from xml.etree import ElementTree

from store import utc_now

SCOPES = [
    "https://www.googleapis.com/auth/classroom.courses.readonly",
    "https://www.googleapis.com/auth/classroom.announcements.readonly",
    "https://www.googleapis.com/auth/classroom.coursework.me.readonly",
    "https://www.googleapis.com/auth/classroom.courseworkmaterials.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]
MAX_BYTES = 20 * 1024 * 1024


def write_private(path, value):
    fd, name = tempfile.mkstemp(prefix=".private-", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output)
        # Windows readers / antivirus can briefly deny atomic replacement. Keep the old file valid.
        for attempt in range(20):
            try:
                temp.replace(path)
                break
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(min(.01 * (attempt + 1), .1))
        path.chmod(0o600)
    finally:
        temp.unlink(missing_ok=True)


class SameOriginRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = urlsplit(req.full_url), urlsplit(newurl)
        if (old.scheme, old.hostname, old.port) != (new.scheme, new.hostname, new.port):
            raise ValueError("Google API 返回跨域重定向；为避免转发授权凭证，请手动下载此文件后导入")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def request_bytes(url, token=None, data=None, headers=None, max_bytes=MAX_BYTES):
    # API calls and OAuth only; user-supplied URLs are never fetched.
    if urlsplit(url).hostname not in {"oauth2.googleapis.com", "classroom.googleapis.com", "www.googleapis.com"}:
        raise ValueError("不支持的 API 主机")
    combined = dict(headers or {})
    if token:
        combined["Authorization"] = "Bearer " + token
    try:
        with build_opener(SameOriginRedirect()).open(Request(url, data=data, headers=combined), timeout=30) as response:
            body = response.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise ValueError("文件超过 20 MiB，请手动下载后分段导入")
            return body
    except HTTPError as error:
        # Response bodies may contain personal data; expose only bounded structured reason.
        reason = ""
        try:
            payload = json.loads(error.read(32000))
            raw = payload.get("error", {})
            if isinstance(raw, dict):
                reason = raw.get("status", "")
                details = raw.get("errors", [])
                if details:
                    reason += " " + details[0].get("reason", "")
            elif isinstance(raw, str):
                reason = raw
        except (ValueError, AttributeError, IndexError):
            pass
        raise ValueError(f"Google API HTTP {error.code} {reason[:160]}；学校可能限制第三方授权或此资源的访问。") from None


def authorize(root, credentials_path):
    configuration = json.loads(Path(credentials_path).read_text(encoding="utf-8"))
    client = configuration.get("installed")
    if not isinstance(client, dict) or not client.get("client_id"):
        raise ValueError("需要 Google Cloud 的桌面应用 OAuth 客户端 JSON（installed），不是服务账号密钥")
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(32)
    outcome = {}
    finished = threading.Event()

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            parsed = urlsplit(self.path)
            params = parse_qs(parsed.query)
            if parsed.path != "/callback" or params.get("state", [None])[0] != state:
                self.send_error(400, "Invalid OAuth callback")
                return
            if "error" in params:
                outcome["error"] = "Google 授权被取消或被学校管理员策略阻止。"
            else:
                outcome["code"] = params.get("code", [None])[0]
            body = "授权流程已返回。请查看本机终端，然后关闭此页。".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            finished.set()

    server = HTTPServer(("127.0.0.1", 0), Callback)
    redirect = f"http://127.0.0.1:{server.server_port}/callback"
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
        "client_id": client["client_id"], "redirect_uri": redirect, "response_type": "code",
        "scope": " ".join(SCOPES), "state": state, "code_challenge": challenge,
        "code_challenge_method": "S256", "access_type": "offline", "prompt": "consent select_account",
    })
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print("请在本机浏览器选择学校账号。若学校阻止授权，停止此流程，使用 Chrome 采集或手动下载导入。", flush=True)
    print("若浏览器没有自动打开，可在本机打开此授权链接：\n" + url, flush=True)
    webbrowser.open(url)
    try:
        if not finished.wait(180):
            raise ValueError("Google 授权等待超时；尚未保存凭证")
        if outcome.get("error") or not outcome.get("code"):
            raise ValueError(outcome.get("error", "Google 没有返回授权码"))
        data = {"code": outcome["code"], "client_id": client["client_id"],
                "redirect_uri": redirect, "grant_type": "authorization_code", "code_verifier": verifier}
        if client.get("client_secret"):
            data["client_secret"] = client["client_secret"]
        token = json.loads(request_bytes("https://oauth2.googleapis.com/token", data=urlencode(data).encode(), headers={"Content-Type": "application/x-www-form-urlencoded"}))
        granted = set(token.get("scope", "").split())
        missing = set(SCOPES) - granted
        if missing:
            raise ValueError("未获得全部所需只读权限；未保存凭证。可改用 Chrome 页面采集。")
        token["expires_at"] = time.time() + int(token.get("expires_in", 3600))
        token["client_id"] = client["client_id"]
        token["client_secret"] = client.get("client_secret", "")
        write_private(root / "google-token.json", token)
        print("只读授权已保存在本机。请用 list_courses 确认所选账号的课程。", flush=True)
    finally:
        server.shutdown()
        server.server_close()


class GoogleClient:
    def __init__(self, root):
        self.token_path = root / "google-token.json"

    def authorized(self):
        return self.token_path.exists()

    def token(self):
        if not self.authorized():
            raise ValueError("尚未进行 Google 只读授权。可用 Chrome 采集，或在本机运行 authorize 命令；学校可能禁止第三方授权。")
        saved = json.loads(self.token_path.read_text(encoding="utf-8"))
        if saved.get("expires_at", 0) <= time.time() + 60:
            if not saved.get("refresh_token"):
                raise ValueError("授权已过期，请在本机重新授权")
            form = {"client_id": saved["client_id"], "client_secret": saved.get("client_secret", ""),
                    "refresh_token": saved["refresh_token"], "grant_type": "refresh_token"}
            fresh = json.loads(request_bytes("https://oauth2.googleapis.com/token", data=urlencode(form).encode(), headers={"Content-Type": "application/x-www-form-urlencoded"}))
            saved.update(fresh)
            saved["expires_at"] = time.time() + int(fresh.get("expires_in", 3600))
            write_private(self.token_path, saved)
        return saved["access_token"]

    def get(self, base, path, params=None):
        return json.loads(request_bytes(base + path + ("?" + urlencode(params) if params else ""), token=self.token()))

    @staticmethod
    def resource_id(value):
        if not isinstance(value, str) or not value or len(value) > 250 or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in value):
            raise ValueError("无效的 Google 资源 ID；请传 ID 而不是网址")
        return value

    def list_courses(self, page_token=""):
        params = {"pageSize": 100}
        if page_token:
            params["pageToken"] = page_token
        return self.get("https://classroom.googleapis.com", "/v1/courses", params)

    def sync_course(self, course_id, store):
        course_id = self.resource_id(course_id)
        course = self.get("https://classroom.googleapis.com", f"/v1/courses/{course_id}")
        items = [{"title": course.get("name", course_id), "source_url": course.get("alternateLink", "https://classroom.google.com/"),
                  "text": "\n".join(str(course.get(k, "")) for k in ("name", "section", "descriptionHeading", "description", "room")),
                  "coverage": "api_course_metadata", "captured_at": utc_now(), "links": [], "warnings": []}]
        errors = []
        # Fetch every page; each collection has its own continuation token.
        for endpoint, key in (("announcements", "announcements"), ("courseWork", "courseWork"), ("courseWorkMaterials", "courseWorkMaterial")):
            token = ""
            seen = set()
            try:
                for _ in range(100):
                    params = {"pageSize": 100}
                    if token:
                        params["pageToken"] = token
                    payload = self.get("https://classroom.googleapis.com", f"/v1/courses/{course_id}/{endpoint}", params)
                    for record in payload.get(key, []):
                        links = []
                        for material in record.get("materials", []):
                            attachment = material.get("driveFile", {}).get("driveFile", {}) or material.get("link", {}) or material.get("youtubeVideo", {}) or material.get("form", {})
                            target = attachment.get("alternateLink") or attachment.get("url") or attachment.get("formUrl")
                            if target and urlsplit(target).scheme == "https":
                                links.append({"title": str(attachment.get("title", "附件"))[:500], "url": target})
                        title = record.get("title") or record.get("text", "课程公告").split("\n")[0]
                        body = json.dumps(record, ensure_ascii=False, indent=2)
                        warnings = ["包含本条公告/作业/课程资料的 API 文本；附件全文需另外读取。"]
                        if len(body) > 500000:
                            warnings.append("本条 API 文本超过 500000 字符，已截断")
                        items.append({"title": str(title)[:500], "source_url": record.get("alternateLink") or f"https://classroom.google.com/c/{course_id}?item={endpoint}-{record['id']}",
                                      "text": body[:500000], "coverage": "api_course_item",
                                      "captured_at": utc_now(), "links": links,
                                      "warnings": warnings})
                    token = payload.get("nextPageToken", "")
                    if not token:
                        break
                    if token in seen:
                        raise ValueError("Google 返回了重复分页游标")
                    seen.add(token)
                else:
                    errors.append({"collection": endpoint, "error": "超过 100 页，未完成全部同步"})
            except (ValueError, OSError) as error:
                errors.append({"collection": endpoint, "error": str(error)})
        ids = []
        for start in range(0, len(items), 100):
            ids.extend(store.put_many(items[start:start + 100]))
        return {"course": course.get("name"), "saved": len(ids), "partial": bool(errors), "errors": errors,
                "attachment_contents_included": False, "note": "此操作更新本次可见的资料，不删除历史快照；附件请用 read_drive_file 读取。"}

    def search_drive(self, query="", page_token="", folder_id=""):
        if not isinstance(query, str) or len(query) > 500:
            raise ValueError("关键词限 500 字符")
        escape = lambda s: s.replace("\\", "\\\\").replace("'", "\\'")
        conditions = ["trashed = false"]
        if query:
            conditions.append("fullText contains '" + escape(query) + "'")
        if folder_id:
            conditions.append("'" + self.resource_id(folder_id) + "' in parents")
        params = {"q": " and ".join(conditions), "pageSize": 100,
                  "fields": "nextPageToken,incompleteSearch,files(id,name,mimeType,modifiedTime,webViewLink,size)",
                  "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
        if page_token:
            params["pageToken"] = page_token
        return self.get("https://www.googleapis.com", "/drive/v3/files", params)

    def read_file(self, file_id, store):
        file_id = self.resource_id(file_id)
        metadata = self.get("https://www.googleapis.com", f"/drive/v3/files/{file_id}", {
            "fields": "id,name,mimeType,size,webViewLink,capabilities(canDownload)", "supportsAllDrives": "true"})
        if metadata.get("capabilities", {}).get("canDownload") is False:
            raise ValueError("学校或文件所有者不允许下载/导出此文件；保留原链接并在浏览器查看")
        if int(metadata.get("size", 0)) > MAX_BYTES:
            raise ValueError("文件超过 20 MiB，请手动下载后分段导入")
        mime = metadata["mimeType"]
        exports = {"application/vnd.google-apps.document": "text/plain",
                   "application/vnd.google-apps.presentation": "text/plain",
                   "application/vnd.google-apps.spreadsheet": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
        if mime in exports:
            export_mime = exports[mime]
            url = f"https://www.googleapis.com/drive/v3/files/{file_id}/export?" + urlencode({"mimeType": export_mime})
            name = metadata["name"] + (".xlsx" if mime.endswith("spreadsheet") else ".txt")
        elif mime.startswith("application/vnd.google-apps."):
            raise ValueError("此 Google 文件类型暂不支持全文导出，请在浏览器查看")
        else:
            url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media&supportsAllDrives=true"
            name, export_mime = metadata["name"], mime
        content = request_bytes(url, token=self.token())
        text, warnings = extract_text(content, name, export_mime)
        if len(text) > 500000:
            warnings.append("正文超过 500000 字符，已截断")
        item = {"title": metadata["name"][:500], "source_url": metadata.get("webViewLink") or f"https://drive.google.com/file/d/{file_id}/view",
                "text": text[:500000], "coverage": "api_extracted_text", "captured_at": utc_now(), "links": [], "warnings": warnings}
        material_id = store.put_many([item])[0]
        return {"material_id": material_id, "title": item["title"], "source_url": item["source_url"],
                "chars": len(item["text"]), "warnings": warnings, "next_step": "read_material 分页读取全文"}


def extract_text(data, name, mime=""):
    """Extract text only. Does not execute document macros or follow embedded URLs."""
    suffix = Path(name).suffix.lower()
    suffix = {"application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
              "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
              "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx"}.get(mime, suffix)
    if suffix == ".pdf" or mime == "application/pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            raise ValueError("PDF 读取需要先安装 server/requirements.txt 中的 pypdf") from None
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ValueError("此 PDF 已加密，请先在有权限的应用中解锁后导入")
        if len(reader.pages) > 1000:
            raise ValueError("PDF 超过 1000 页，请分段导入")
        pages = [f"[第 {i + 1} 页]\n{page.extract_text() or ''}" for i, page in enumerate(reader.pages)]
        warnings = ["仅提取文字层；未做 OCR，图表、公式、批注和扫描页可能缺失。"]
        return "\n\n".join(pages), warnings
    if suffix in {".docx", ".pptx", ".xlsx"}:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if sum(info.file_size for info in archive.infolist()) > 50 * 1024 * 1024:
                raise ValueError("文档解压后超过 50 MiB，请分段导入")
            if suffix == ".docx":
                tree = ElementTree.fromstring(archive.read("word/document.xml"))
                paragraphs = ["".join(node.itertext()) for node in tree.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p")]
                return "\n".join(paragraphs), ["仅正文文字；页眉页脚、批注、图像、公式与布局可能缺失。"]
            if suffix == ".pptx":
                import re
                names = sorted((n for n in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)), key=lambda n: int(re.search(r"slide(\d+)", n).group(1)))
                pages = []
                for i, part in enumerate(names):
                    tree = ElementTree.fromstring(archive.read(part))
                    text = "\n".join(n.text or "" for n in tree.iter("{http://schemas.openxmlformats.org/drawingml/2006/main}t"))
                    pages.append(f"[幻灯片 {i + 1}]\n{text}")
                return "\n\n".join(pages), ["仅幻灯片文字；未提取图像、讲者备注、图表或版式。"]
            ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
            shared = []
            if "xl/sharedStrings.xml" in archive.namelist():
                root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
                shared = ["".join(n.itertext()) for n in root.findall("m:si", ns)]
            pages = []
            workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
            rels = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
            targets = {n.attrib["Id"]: n.attrib["Target"] for n in rels}
            for sheet in workbook.findall("m:sheets/m:sheet", ns):
                rid = sheet.attrib["{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"]
                target = targets[rid]
                part = target.lstrip("/") if target.startswith("/") else "xl/" + target
                if not part.startswith("xl/worksheets/") or ".." in part:
                    raise ValueError("不支持的工作表关系路径")
                tree = ElementTree.fromstring(archive.read(part))
                rows = []
                for row in tree.findall("m:sheetData/m:row", ns):
                    values = []
                    for cell in row.findall("m:c", ns):
                        val = cell.findtext("m:v", "", ns)
                        if cell.get("t") == "s":
                            val = shared[int(val)] if val else ""
                        elif cell.get("t") == "inlineStr":
                            val = "".join(cell.find("m:is", ns).itertext())
                        values.append(f"{cell.get('r', '?')}={val}")
                    rows.append("\t".join(values))
                pages.append(f"[工作表 {sheet.attrib.get('name', '')}]\n" + "\n".join(rows))
            return "\n\n".join(pages), ["提取所有工作表单元格的缓存值；不重新计算公式，不还原日期格式、图表或图片。"]
    if mime.startswith("text/") or mime in {"application/json", "application/xml"} or suffix in {".txt", ".md", ".csv", ".tsv", ".json", ".xml"}:
        try:
            return data.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n"), []
        except UnicodeDecodeError:
            raise ValueError("此文本文件不是 UTF-8，请另存为 UTF-8 后导入") from None
    raise ValueError("暂不支持此文件的正文提取；支持 Google Docs/Slides/Sheets、PDF、DOCX、PPTX、XLSX 与 UTF-8 文本")
