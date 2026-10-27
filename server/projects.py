"""Persistent project automation shared by the desktop GUI and chat adapters."""
import copy
import os
import hashlib
import json
from pathlib import Path
import re
import secrets
import shutil
import tempfile
import zipfile
import threading
import time
from urllib.parse import urlsplit

from bundle import export_task
from file_ops import atomic_copy, file_hash, safe_filename
from collector import resource_key, course_id, assignment_id, project_key, link_kind, inherit_account
from google_api import write_private

CATEGORIES = {".pdf": "PDF资料", ".docx": "文档", ".doc": "文档", ".txt": "文档", ".md": "文档",
              ".pptx": "演示文稿", ".ppt": "演示文稿", ".xlsx": "数据表", ".xls": "数据表", ".csv": "数据表",
              ".png": "图片", ".jpg": "图片", ".jpeg": "图片", ".webp": "图片", ".zip": "压缩包"}


class ProjectManager:
    def __init__(self, store, broker, collector, clock=time.time):
        self.store, self.broker, self.collector, self.clock = store, broker, collector, clock
        self.lock = threading.RLock()
        self.path = store.control_root / "projects.json"
        self.cloud_transfer_lock = threading.RLock()
        self.settings = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {"automatic": True, "relay_url": "", "projects": []}
        self.maintenance = False
        self.active = None
        self.stopping = threading.Event()
        self.pair_until = self.clock() + 300
        self.client_state = {"cloud": "not_configured"}
        self.next_discovery = 0
        self.normalize()

    def normalize(self):
        """Apply defaults after both initial load and a storage-settings reload."""
        self.settings.setdefault("automatic", True)
        self.settings.setdefault("relay_url", "")
        self.settings.setdefault("projects", [])
        self.settings.setdefault("auto_discover", True)
        self.settings.setdefault("large_file_mib", 50)
        self.settings.setdefault("large_file_choices", {})
        self.settings.setdefault("google_warning_choices", {})
        self.settings.setdefault("unvalidated_recheck_hours", 24)
        self.broker.lookup_download_choice = self.lookup_download_choice
        self.broker.save_download_choice = self.save_download_choice
        self.broker.lookup_google_warning_choice = lambda url: self.settings["google_warning_choices"].get(resource_key(url)) if url else None
        self.broker.save_google_warning_choice = self.save_google_warning_choice
        self.settings.setdefault("scan_max_files", 1000)
        self.settings.setdefault("cloud_folders", {})
        self.settings.setdefault("cloud_automatic", False)
        self.broker.download_settings = lambda: {"threshold_bytes": self.settings["large_file_mib"] * 1024 * 1024}
        for project in self.settings["projects"]:
            project.setdefault("parent_id", None)
            project.setdefault("kind", "assignment" if assignment_id(project["url"]) else "course" if course_id(project["url"]) else "folder")
            project.setdefault("topic", "")

    def lookup_download_choice(self, source_url, size):
        if not source_url or size is None:
            return None
        key = resource_key(source_url) + "@" + str(size)
        with self.lock:
            return self.settings["large_file_choices"].get(key)

    def save_download_choice(self, source_url, size, allowed):
        if not source_url or size is None:
            return
        with self.lock:
            self.settings["large_file_choices"][resource_key(source_url) + "@" + str(size)] = bool(allowed)
            self._save()

    def save_google_warning_choice(self, source_url, allowed):
        if source_url:
            with self.lock:
                self.settings["google_warning_choices"][resource_key(source_url)] = bool(allowed)
                self._save()

    def configure(self, large_file_mib, scan_max_files, cloud_folders, cloud_automatic=False, unvalidated_recheck_hours=None):
        if unvalidated_recheck_hours is not None and (type(unvalidated_recheck_hours) is not int or not 0 <= unvalidated_recheck_hours <= 168):
            raise ValueError("无更新标记的大文件复查间隔应为 0–168 小时；0 表示每轮检查")
        if type(large_file_mib) is not int or not 1 <= large_file_mib <= 65536:
            raise ValueError("大文件询问阈值应为 1–65536 MiB")
        if type(scan_max_files) is not int or not 1 <= scan_max_files <= 10000:
            raise ValueError("每轮附件数量应为 1–10000")
        from library import PROVIDERS
        if not isinstance(cloud_folders, dict) or any(name not in PROVIDERS for name in cloud_folders):
            raise ValueError("网盘设置无效")
        cleaned = {}
        for name, config in cloud_folders.items():
            path = str(config.get("path", "")).strip()
            enabled = config.get("enabled") is True
            if enabled:
                root = Path(path).expanduser().resolve() if path else None
                if not root or not root.is_dir() or root.parent == root or root == Path.home().resolve() or root.is_relative_to(self.store.root) or self.store.root.is_relative_to(root):
                    raise ValueError(name + "：请选择独立的官方客户端同步文件夹")
                path = str(root)
            cleaned[name] = {"path": path, "enabled": enabled}
        enabled_roots = [Path(config["path"]) for config in cleaned.values() if config["enabled"]]
        for i, root in enumerate(enabled_roots):
            if any(root.is_relative_to(other) or other.is_relative_to(root) for other in enabled_roots[:i]):
                raise ValueError("不同网盘的同步目录不能相同或互相嵌套")
        with self.lock:
            self.settings.update(large_file_mib=large_file_mib, scan_max_files=scan_max_files,
                                 cloud_folders=cleaned, cloud_automatic=bool(cloud_automatic))
            if unvalidated_recheck_hours is not None:
                self.settings["unvalidated_recheck_hours"] = unvalidated_recheck_hours
            self._save()

    def library(self, project_id, include_text=True):
        from library import project_library
        return project_library(self, project_id, include_text=include_text)

    def record_download(self, project_id, file):
        folder = self.store.root / "projects" / project_id
        source = Path(file["download_path"]).resolve()
        if not source.is_relative_to((self.store.root / "attachments").resolve()) or not source.is_file():
            raise ValueError("附件不属于当前资料库")
        category = CATEGORIES.get(source.suffix.lower(), "其他资料")
        target = folder / "资料库" / category / (file["sha256"][:10] + "-" + source.name)
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_copy(source, target, file["sha256"])
        with self.lock:
            path = folder / "catalog.json"
            catalog = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            catalog[resource_key(file["source_url"])] = {k: v for k, v in file.items() if k != "next_step"}
            catalog[resource_key(file["source_url"])].update(category=category, library_path=str(target))
            write_private(path, catalog)
            current = self.project(project_id)
            current["files"] = len(catalog)
            if file.get("material_id") and file["material_id"] not in current["material_ids"]:
                current["material_ids"].append(file["material_id"])
            self._save()

    def _save(self):
        with self.lock:
            write_private(self.path, self.settings)

    def snapshot(self):
        with self.lock:
            data = copy.deepcopy(self.settings)
            data.update(browser=self.broker.status(), active=self.active, client=copy.deepcopy(self.client_state))
            return data

    def add(self, name, url, interval_minutes=15, parent_id=None):
        u = urlsplit(url)
        if u.scheme != "https" or u.hostname not in {"classroom.google.com", "drive.google.com", "docs.google.com"} or u.username or u.password or u.port not in (None, 443):
            raise ValueError("请选择 Google Classroom、Drive 或 Docs 的 HTTPS 页面")
        if type(interval_minutes) is not int or not 5 <= interval_minutes <= 1440:
            raise ValueError("自动更新间隔需要在 5 分钟到 1 天之间")
        if not isinstance(name, str) or not name.strip() or len(name) > 100 or len(url) > 4000:
            raise ValueError("请填写课题名称和有效链接")
        with self.lock:
            if self.maintenance:
                raise ValueError("正在更改保存位置或准备更新，请稍后添加课题")
            key = project_key(url)
            if any(project_key(p["url"]) == key for p in self.settings["projects"]):
                raise ValueError("这个课程或文件夹已加入自动同步")
            if parent_id:
                parent = self.project(parent_id)
                if parent.get("parent_id") or course_id(parent["url"]) != course_id(url) or not assignment_id(url):
                    raise ValueError("课题必须属于对应的课程")
            project = {"id": secrets.token_hex(12), "name": name.strip(), "url": url, "enabled": True,
                       "parent_id": parent_id, "kind": "assignment" if assignment_id(url) else "course" if course_id(url) else "folder",
                       "interval_minutes": interval_minutes, "next_scan": 0, "status": "waiting", "files": 0,
                       "last_synced": None, "last_error": "", "material_ids": [], "latest_bundle": None}
            self.settings["projects"].append(project)
            self._save()
            return copy.deepcopy(project)

    def project(self, project_id):
        for project in self.settings["projects"]:
            if project["id"] == project_id:
                return project
        raise ValueError("没有找到这个课题")

    def record_cloud_report(self, provider, selected_ids, report=None, error=None):
        with self.lock:
            selected = set(selected_ids)
            affected = [p for p in self.settings["projects"] if p["id"] in selected or p.get("parent_id") in selected]
            errors = self.client_state.setdefault("cloud_folder_errors", {})
            failures = {p["id"]: p["reason"] for p in (report or {}).get("failed", []) + (report or {}).get("skipped", [])}
            included = set((report or {}).get("included", []))
            for project in affected:
                key = provider + ":" + project["id"]
                messages = project.setdefault("cloud_errors", {})
                message = error or failures.get(project["id"])
                if message:
                    errors[key] = messages[provider] = str(message)[:300]
                elif project["id"] in included:
                    errors.pop(key, None)
                    messages.pop(provider, None)
            self._save()

    def set_automatic(self, enabled):
        if type(enabled) is not bool:
            raise ValueError("自动同步开关无效")
        with self.lock:
            self.settings["automatic"] = enabled
            self._save()
        return {"automatic": enabled}

    def request_sync(self, project_id, force=False):
        with self.lock:
            if self.maintenance:
                raise ValueError("正在更改保存位置或准备软件更新，请稍后再同步")
            self.store.require_available()
            project = self.project(project_id)
            if self.active or self.client_state.get("discovering"):
                raise ValueError("已有同步在进行，请稍后查看结果")
            if not self.broker.status()["connected"]:
                raise ValueError("浏览器尚未连接，请先在 Chrome 启用课题资料助手")
            self.active = project_id
            project.update(status="running", last_error="")
            self._save()
            threading.Thread(target=self._sync, args=(project_id, force), daemon=True).start()
        return {"project_id": project_id, "status": "running"}

    def tick(self):
        with self.lock:
            if self.maintenance:
                return
            try:
                self.store.require_available()
                self.client_state.pop("storage_error", None)
            except ValueError as error:
                self.client_state["storage_error"] = str(error)
                return
        if not self.active and self.settings["automatic"] and self.settings["auto_discover"] and self.broker.status()["connected"] and self.clock() >= self.next_discovery:
            self.next_discovery = self.clock() + 900
            with self.lock:
                if self.maintenance or self.active:
                    return
                self.client_state["discovering"] = True
            try:
                self.import_discovered()
                with self.lock:
                    self.client_state.pop("discovery_error", None)
                if not self.settings["projects"]:
                    self.next_discovery = self.clock() + 30
            except Exception as error:
                with self.lock:
                    self.client_state["discovery_error"] = str(error)[:300]
                self.next_discovery = self.clock() + 60
            finally:
                with self.lock:
                    self.client_state["discovering"] = False
        with self.lock:
            if self.maintenance or self.active or not self.settings["automatic"] or not self.broker.status()["connected"]:
                return
            candidates = [p for p in self.settings["projects"] if p["enabled"] and p.get("next_scan", 0) <= self.clock()]
            if not candidates:
                return
            project_id = min(candidates, key=lambda p: p.get("next_scan", 0))["id"]
        try:
            self.request_sync(project_id)
        except ValueError:
            pass

    def run(self):
        while not self.stopping.wait(2):
            self.tick()

    def _sync(self, project_id, force=False):
        opened = None
        try:
            with self.lock:
                project = copy.deepcopy(self.project(project_id))
            opened = self.broker.call("open", {"url": project["url"]})["tab_id"]
            previous_path = self.store.root / "projects" / project_id / "catalog.json"
            previous_catalog = json.loads(previous_path.read_text(encoding="utf-8")) if previous_path.exists() else {}
            task_id = self.collector.start(opened, max_files=self.settings["scan_max_files"],
                                           force_download=force, recheck_seconds=self.settings["unvalidated_recheck_hours"] * 3600,
                                           download_callback=lambda file: self.record_download(project_id, file))["task_id"]
            with self.lock:
                self.project(project_id)["current_task"] = task_id
            while not self.stopping.wait(.5):
                task = self.collector.status(task_id)
                if task["status"] != "running":
                    break
            else:
                return
            folder = self.store.root / "projects" / project_id
            destination = folder / "runs" / task_id
            catalog_path = folder / "catalog.json"
            catalog = json.loads(catalog_path.read_text(encoding="utf-8")) if catalog_path.exists() else {}
            changed = 0
            for file in task["downloads"]:
                key = resource_key(file["source_url"])
                previous = previous_catalog.get(key)
                if not previous or previous["sha256"] != file["sha256"]:
                    changed += 1
                self.record_download(project_id, file)
                source = Path(file["download_path"])
                category = CATEGORIES.get(source.suffix.lower(), "其他资料")
                target = folder / "资料库" / category / (file["sha256"][:10] + "-" + source.name)
                catalog[key] = {k: v for k, v in file.items() if k != "next_step"}
                catalog[key].update(category=category, library_path=str(target))
            with self.lock:
                write_private(catalog_path, catalog)
            bundle_task = copy.deepcopy(task)
            bundle_task["downloads"] = list(catalog.values())
            known_pages = {p["material_id"] for p in bundle_task["pages"]}
            for mid in project.get("material_ids", []):
                if mid in known_pages:
                    continue
                material = self.store.read(mid, 0, 1)
                if material["coverage"] != "browser_attachment_text":
                    bundle_task["pages"].append({"material_id": mid, "title": material["title"], "source_url": material["source_url"]})
            # Material IDs are stable across edits, so fingerprint actual text and metadata.
            semantic = {k: bundle_task.get(k) for k in ("status", "errors", "settings", "note", "unvisited_pages", "undownloaded_due_to_limit", "skipped_downloads")}
            semantic["settings"] = {k: v for k, v in (bundle_task.get("settings") or {}).items() if k not in {"tab_id", "force_download"}}
            semantic["files"] = [{k: f.get(k) for k in ("filename", "source_url", "bytes", "sha256", "text_extracted", "extraction_error", "freshness_warning")} for f in bundle_task["downloads"]]
            source_ids = [p["material_id"] for p in bundle_task["pages"]] + [f["material_id"] for f in bundle_task["downloads"] if f.get("material_id")]
            from bundle import read_all
            semantic["sources"] = []
            for mid in dict.fromkeys(source_ids):
                material, text = read_all(self.store, mid)
                semantic["sources"].append({k: material.get(k) for k in ("id", "title", "source_url", "coverage", "warnings")} | {"text_hash": hashlib.sha256(text.encode()).hexdigest()})
            fingerprint = hashlib.sha256(json.dumps(semantic, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            previous_bundle = Path(project["latest_bundle"]) if project.get("latest_bundle") else None
            reusable = bool(previous_bundle and previous_bundle.is_dir() and project.get("bundle_fingerprint") == fingerprint)
            if reusable:
                reusable = bool(project.get("bundle_hashes"))
                for name, digest in project.get("bundle_hashes", {}).items():
                    path = previous_bundle / name
                    if not path.is_file():
                        reusable = False
                        break
                    info = path.stat()
                    if project.get("bundle_stats", {}).get(name) != [info.st_size, info.st_mtime_ns] and file_hash(path) != digest:
                        reusable = False
                        break
            if reusable:
                bundle = {"directory": str(previous_bundle)}
                bundle_hashes = project["bundle_hashes"]
            else:
                bundle = export_task(self.store, bundle_task, destination)
                bundle_hashes = {str(p.relative_to(destination)): file_hash(p) for p in destination.rglob("*") if p.is_file()}
            bundle_root = Path(bundle["directory"])
            bundle_stats = {name: [p.stat().st_size, p.stat().st_mtime_ns] for name in bundle_hashes for p in [bundle_root / name]}
            material_ids = list(dict.fromkeys([p["material_id"] for p in task["pages"]] + [f["material_id"] for f in task["downloads"] if f.get("material_id")]))
            with self.lock:
                current = self.project(project_id)
                current.update(status="ready" if task["status"] == "completed" else "gaps", files=len(catalog), changed=changed,
                               last_synced=self.clock(), material_ids=list(dict.fromkeys(current.get("material_ids", []) + material_ids)), latest_bundle=bundle["directory"], bundle_fingerprint=fingerprint, bundle_hashes=bundle_hashes, bundle_stats=bundle_stats,
                               last_error=f"本次有 {len(task['errors'])} 项未完成，详情见资料清单" if task["errors"] else "")
            if self.settings.get("cloud_automatic"):
                from library import sync_to_folder
                for provider, config in self.settings["cloud_folders"].items():
                    if config.get("enabled"):
                        try:
                            sync_to_folder(self, [project_id], provider)
                        except (OSError, ValueError):
                            pass  # sync_to_folder records errors per provider and project.
        except Exception as error:
            with self.lock:
                self.project(project_id).update(status="error", last_error=str(error)[:700])
        finally:
            if opened is not None:
                try:
                    self.broker.call("close", {"tab_id": opened}, timeout=10)
                except (ValueError, OSError):
                    pass
            with self.lock:
                current = self.project(project_id)
                current["next_scan"] = self.clock() + current["interval_minutes"] * 60
                self.active = None
                self._save()

    def search(self, query="", project_id=None, offset=0, limit=20):
        if not isinstance(query, str) or len(query) > 500:
            raise ValueError("搜索关键词不能超过 500 字符")
        with self.lock:
            projects = [self.project(project_id)] if project_id else self.settings["projects"]
            if project_id:
                projects += [p for p in self.settings["projects"] if p.get("parent_id") == project_id]
            ids = {mid for project in projects for mid in project.get("material_ids", [])}
        rows = []
        for material_id in sorted(ids):
            material = self.store.read(material_id, 0, 500)
            # Query the database rather than only matching the preview.
            with self.store.connect() as db:
                hit = db.execute("SELECT instr(lower(title || char(10) || text),lower(?)) AS found FROM materials WHERE id=?", (query, material_id)).fetchone()
            if hit and hit["found"]:
                rows.append({key: material[key] for key in ("id", "title", "source_url", "coverage", "text")})
        offset, limit = max(0, int(offset)), min(30, max(1, int(limit)))
        return {"items": rows[offset:offset + limit], "next_offset": offset + limit if offset + limit < len(rows) else None}

    def discover(self):
        tabs = self.broker.call("tabs", {}).get("tabs", [])
        choices = {}
        for tab in tabs[:10]:
            url = tab.get("url", "")
            if link_kind(url):
                choices[resource_key(url)] = {"name": tab.get("title", "课题资料")[:100], "url": url}
            elif urlsplit(url).hostname == "classroom.google.com":
                snapshot = self.broker.call("read", {"tab_id": tab["tab_id"], "limit": 1000})
                for link in snapshot.get("capture", {}).get("links", []):
                    if course_id(link["url"]):
                        target = inherit_account(link["url"], snapshot.get("capture", {}).get("source_url", url))
                        choices[resource_key(target)] = {"name": link.get("title", "课程")[:100], "url": target}
        return {"items": list(choices.values())[:100], "partial": True,
                "note": "来自已打开的 Google 页面及课程首页当前可见的链接；可能没有列全，可使用本人提供的课程链接。"}

    def import_discovered(self):
        """Discover course cards, then read each classwork page with normal browser DOM operations."""
        items = self.discover()["items"]
        courses = {}
        for item in items:
            cid = course_id(item["url"])
            if cid and not assignment_id(item["url"]):
                courses.setdefault(cid, item)
            elif not cid:
                continue  # Only Classroom courses are automatically enrolled.
            else:
                courses.setdefault(cid, {"name": "课程 " + cid, "url": "https://classroom.google.com/c/" + cid})
        for existing in self.snapshot()["projects"]:
            if existing.get("kind") == "course":
                courses[course_id(existing["url"])] = existing
        for cid, item in list(courses.items())[:100]:
            with self.lock:
                parent = next((p for p in self.settings["projects"] if project_key(p["url"]) == "classroom:" + cid + ":course"), None)
                if parent is None:
                    parent = self.add(item["name"] or "课程", item["url"])
            opened = None
            try:
                parsed = urlsplit(item["url"])
                url = inherit_account("https://classroom.google.com/w/" + cid + "/t/all" + ("?" + parsed.query if parsed.query else ""), item["url"])
                opened = self.broker.call("open", {"url": url})["tab_id"]
                result = self.broker.call("collect", {"tab_id": opened, "max_scrolls": 20})
                for link in result.get("capture", {}).get("links", []):
                    if course_id(link["url"]) != cid or not assignment_id(link["url"]):
                        continue
                    with self.lock:
                        known = next((p for p in self.settings["projects"] if project_key(p["url"]) == project_key(link["url"])), None)
                        if known:
                            known["parent_id"] = parent["id"]
                        else:
                            added = self.add((link.get("title") or "课题")[:100], inherit_account(link["url"], url), parent_id=parent["id"])
                            known = self.project(added["id"])
                        if link.get("topic"):
                            known["topic"] = str(link["topic"])[:100]
                self._save()
            finally:
                if opened is not None:
                    self.broker.call("close", {"tab_id": opened}, timeout=10)

    def export_selected(self, selected_ids, destination):
        """Export only selected projects and their children; never archive the data root."""
        with self.lock:
            selected = {self.project(pid)["id"] for pid in selected_ids}
            if not selected:
                raise ValueError("请勾选要导出的课程或课题")
            projects = copy.deepcopy([p for p in self.settings["projects"] if p["id"] in selected or p.get("parent_id") in selected])
        destination = Path(destination)
        index, skipped = [], []
        ready = []
        for project in projects:
            if not project.get("latest_bundle"):
                skipped.append({"id": project["id"], "name": project["name"], "status": "skipped", "reason": "尚未完成首次同步"})
            else:
                ready.append(project)
        if not ready:
            raise ValueError("所选项目均尚未完成首次同步")
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".course-export-", suffix=".zip", dir=destination.parent)
        os.close(descriptor)
        folder_name = lambda p: safe_filename(p["name"])[:65].rstrip(" .") + "-" + p["id"][:8]
        try:
            with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
                for project in ready:
                    source = Path(project["latest_bundle"]).resolve()
                    allowed = (self.store.root / "projects" / project["id"] / "runs").resolve()
                    if not source.is_relative_to(allowed):
                        raise ValueError("资料包目录无效")
                    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
                    folder = folder_name(project)
                    if project.get("parent_id"):
                        folder = folder_name(self.project(project["parent_id"])) + "/" + folder
                    names = ["manifest.json", "START-HERE.txt", "ANALYSIS-PROMPT.txt"]
                    names += [s["text_path"] for s in manifest["sources"]] + [f["path"] for f in manifest["files"]] + [p["path"] for p in manifest["ai_parts"]]
                    for name in dict.fromkeys(names):
                        path = (source / name).resolve()
                        if Path(name).is_absolute() or ".." in Path(name).parts or not path.is_relative_to(source) or not path.is_file():
                            raise ValueError("资料包文件缺失或路径无效")
                        compression = zipfile.ZIP_STORED if path.suffix.lower() in {".pdf", ".jpg", ".png", ".zip", ".mp4", ".docx", ".xlsx", ".pptx"} else zipfile.ZIP_DEFLATED
                        archive.write(path, folder + "/" + name, compress_type=compression)
                    index.append({"id": project["id"], "name": project["name"], "parent_id": project.get("parent_id"), "topic": project.get("topic"), "folder": folder, "last_synced": project["last_synced"], "status": "included"})
                archive.writestr("项目清单.json", json.dumps(index + skipped, ensure_ascii=False, indent=2))
                guide = "每个文件夹是一份课程或课题资料，项目清单记录层级与跳过项。向 AI 提供 ANALYSIS-PROMPT.txt 和 for-ai 文字，出处和缺失见 manifest.json。\n"
                if skipped:
                    guide += "本次未包含：\n" + "\n".join(p["name"] + "：" + p["reason"] for p in skipped)
                archive.writestr("先读我.txt", guide)
            os.replace(temporary, destination)
            from file_ops import mark_internet_file
            mark_internet_file(destination)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return {"projects": len(ready), "path": str(destination), "skipped": skipped, "partial": bool(skipped)}

    def dispatch(self, operation, args):
        if not isinstance(args, dict):
            raise ValueError("无效的课题操作参数")
        if operation == "status":
            data = self.snapshot()
            return {"browser_connected": data["browser"]["connected"], "automatic": data["automatic"], "active": data["active"],
                    "projects": [{k: v for k, v in p.items() if k not in {"material_ids", "latest_bundle"}} for p in data["projects"]]}
        if operation == "sync":
            return self.request_sync(args["project_id"])
        if operation == "discover":
            return self.discover()
        if operation == "add":
            return self.add(args["name"], args["url"])
        if operation == "automatic":
            return self.set_automatic(args["enabled"])
        if operation == "search":
            return self.search(**args)
        if operation == "read":
            material_id = args.get("material_id")
            with self.lock:
                allowed = {mid for p in self.settings["projects"] for mid in p.get("material_ids", [])}
            if material_id not in allowed:
                raise ValueError("资料不在已选择的课题范围中")
            return self.store.read(material_id, args.get("offset", 0), min(30000, args.get("limit", 12000)))
        raise ValueError("不支持的聊天操作")
