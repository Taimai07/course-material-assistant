"""Local material store shared by the Chrome receiver and stdio MCP process."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from urllib.parse import urlsplit

GOOGLE_HOSTS = {"classroom.google.com", "drive.google.com", "docs.google.com"}


def state_dir():
    root = Path(os.environ.get("COURSE_BRIDGE_DATA", Path.home() / ".course-material-bridge")).expanduser()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def validate_item(item):
    if not isinstance(item, dict):
        raise ValueError("资料必须是对象")
    url = item.get("source_url", "")
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in GOOGLE_HOSTS or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError("只接受 Google Classroom、Drive 与 Docs 的 HTTPS 来源")
    if len(url) > 4000:
        raise ValueError("来源地址过长")
    for field, limit in (("title", 500), ("text", 500000), ("coverage", 80), ("captured_at", 80)):
        if not isinstance(item.get(field), str) or len(item[field]) > limit:
            raise ValueError(f"资料字段无效：{field}")
    links = item.get("links", [])
    warnings = item.get("warnings", [])
    if not isinstance(links, list) or len(links) > 1000 or not isinstance(warnings, list) or len(warnings) > 20:
        raise ValueError("链接或警告格式无效")
    for link in links:
        if not isinstance(link, dict) or not isinstance(link.get("title"), str) or len(link["title"]) > 500:
            raise ValueError("链接格式无效")
        target = urlsplit(link.get("url", ""))
        if target.scheme != "https" or not target.hostname or target.username or target.password or len(link["url"]) > 4000:
            raise ValueError("链接必须为 HTTPS")
    if any(not isinstance(w, str) or len(w) > 2000 for w in warnings):
        raise ValueError("警告格式无效")
    return item


class Store:
    def __init__(self, root=None):
        self.control_root = (Path(root) if root is not None else state_dir()).resolve()
        self.control_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        from storage import recover, location
        recover(self.control_root)
        self.root = location(self.control_root)
        self.activity_lock = threading.RLock()
        self.access_local = threading.local()
        from storage import storage_available
        self.storage_available = storage_available(self.control_root, self.root)
        self.path = self.root / "materials.sqlite3"
        if not self.storage_available:
            return
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS materials (
                id TEXT PRIMARY KEY, source_url TEXT NOT NULL, title TEXT NOT NULL,
                text TEXT NOT NULL, coverage TEXT NOT NULL, captured_at TEXT NOT NULL,
                links TEXT NOT NULL, warnings TEXT NOT NULL, saved_at TEXT NOT NULL)""")
        self.path.chmod(0o600)

    def require_available(self):
        from storage import location, storage_available
        root = location(self.control_root)
        self.storage_available = storage_available(self.control_root, root)
        if not self.storage_available:
            raise ValueError("资料位置暂不可用：" + str(root) + "；请接回磁盘后在设置中重新检查，不会创建空资料库")
        self.root, self.path = root, root / "materials.sqlite3"

    @contextmanager
    def material_access(self):
        # Every process uses the same OS lock. Nested store calls reuse the thread's guard.
        with self.activity_lock:
            depth = getattr(self.access_local, "depth", 0)
            if depth:
                self.access_local.depth += 1
                try:
                    yield
                finally:
                    self.access_local.depth -= 1
                return
            from storage import switch_lock
            with switch_lock(self.control_root, wait=2):
                self.require_available()
                self.access_local.depth = 1
                try:
                    yield
                finally:
                    self.access_local.depth = 0

    @contextmanager
    def connect(self):
        with self.material_access():
            db = sqlite3.connect(self.path, timeout=15)
            db.row_factory = sqlite3.Row
            try:
                with db:
                    yield db
            finally:
                db.close()

    def put_many(self, items):
        if not isinstance(items, list) or not 1 <= len(items) <= 100:
            raise ValueError("每批需要 1–100 条资料")
        # Validate the entire batch before writing any item.
        for item in items:
            validate_item(item)
        ids = []
        with self.connect() as db:
            for item in items:
                identity = item["source_url"] + "\n" + item["coverage"]
                if item["coverage"] == "selected_excerpt":
                    identity += "\n" + item["text"]
                material_id = hashlib.sha256(identity.encode()).hexdigest()[:24]
                db.execute("INSERT OR REPLACE INTO materials VALUES (?,?,?,?,?,?,?,?,?)", (
                    material_id, item["source_url"], item["title"], item["text"], item["coverage"],
                    item["captured_at"], json.dumps(item.get("links", []), ensure_ascii=False),
                    json.dumps(item.get("warnings", []), ensure_ascii=False), utc_now()))
                ids.append(material_id)
        return ids

    def status(self):
        with self.connect() as db:
            return dict(db.execute("SELECT count(*) AS saved_materials, max(saved_at) AS latest_saved_at FROM materials").fetchone())

    def search(self, query="", offset=0, limit=20):
        if not isinstance(query, str) or len(query) > 500:
            raise ValueError("关键词限 500 字符")
        offset, limit = max(0, int(offset)), max(1, min(50, int(limit)))
        # instr treats percent, underscore, quote, and CJK text literally.
        with self.connect() as db:
            rows = db.execute("SELECT id,title,source_url,coverage,captured_at,substr(text,1,350) AS excerpt FROM materials WHERE instr(lower(title || char(10) || text),lower(?)) > 0 ORDER BY saved_at DESC,id LIMIT ? OFFSET ?", (query, limit + 1, offset)).fetchall()
        return {"items": [dict(row) for row in rows[:limit]], "next_offset": offset + limit if len(rows) > limit else None}

    def read(self, material_id, offset=0, limit=12000):
        offset, limit = max(0, int(offset)), max(1, min(30000, int(limit)))
        with self.connect() as db:
            row = db.execute("SELECT * FROM materials WHERE id=?", (material_id,)).fetchone()
        if row is None:
            raise ValueError("没有找到这条资料")
        result = dict(row)
        text = result["text"]
        result.update(text=text[offset:offset + limit], total_chars=len(text), offset=offset,
                      next_offset=offset + limit if offset + limit < len(text) else None,
                      links=json.loads(result["links"]), warnings=json.loads(result["warnings"]))
        return result
