"""Read-only offline classroom views and scoped cloud-client folder exports."""
import hashlib
import html
import json
import os
from pathlib import Path
import shutil
import secrets
import time
import tempfile
from urllib.parse import quote

from collector import resource_key
from google_api import write_private
from file_ops import file_hash, atomic_copy, safe_filename

PROVIDERS = ("Google Drive", "OneDrive", "Dropbox")


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def project_library(manager, project_id, include_text=True):
    with manager.lock:
        project = dict(manager.project(project_id))
    folder = manager.store.root / "projects" / project_id
    catalog = read_json(folder / "catalog.json", {})
    task = None
    if project.get("current_task"):
        try:
            task = manager.collector.status(project["current_task"])
        except (ValueError, OSError):
            pass
    candidates = {}
    for key, item in catalog.items():
        path = Path(item.get("library_path", ""))
        exists = path.is_file() and path.resolve().is_relative_to((folder / "资料库").resolve())
        candidates[key] = {"key": key, "name": safe_filename(item.get("filename", path.name)), "source_url": item["source_url"],
                           "bytes": item.get("bytes", 0), "status": "downloaded" if exists else "missing",
                           "path": str(path.resolve()) if exists else None, "text_extracted": bool(item.get("chars", item.get("material_id"))),
                           "freshness_warning": item.get("freshness_warning", ""), "warnings": item.get("warnings", []), "error": item.get("extraction_error", "")}
    errors = {}
    if task:
        for error in task.get("errors", []):
            if error.get("source_url") and error.get("stage") == "download":
                errors[resource_key(error["source_url"])] = error.get("error", "")
        for key, item in candidates.items():
            if key in errors:
                item["update_error"] = errors[key]
        for item in task.get("discovered_files", []):
            key = resource_key(item["url"])
            if key not in candidates:
                candidates[key] = {"key": key, "name": safe_filename(item.get("title") or "附件"), "source_url": item["url"],
                                   "bytes": 0, "status": "error" if key in errors else "pending", "path": None,
                                   "text_extracted": False, "error": errors.get(key, "")}
        for item in task.get("skipped_downloads", []):
            key = resource_key(item["url"])
            if key in candidates:
                if candidates[key]["path"]:
                    candidates[key]["update_error"] = item.get("reason", "本次更新跳过，保留旧副本")
                else:
                    candidates[key].update(status="deferred", error=item.get("reason", "本次跳过"))
    pages = []
    seen = set()
    ids = [p["material_id"] for p in (task or {}).get("pages", [])]
    for mid in (ids + project.get("material_ids", [])) if include_text else []:
        if mid in seen:
            continue
        seen.add(mid)
        try:
            material = manager.store.read(mid, 0, 30000)
        except ValueError:
            continue
        if material.get("coverage") == "browser_attachment_text":
            continue
        pages.append({"id": mid, "title": material["title"], "text": material["text"], "source_url": material["source_url"],
                      "coverage": material["coverage"], "warnings": material.get("warnings", []), "preview_truncated": material.get("next_offset") is not None})
    with manager.lock:
        children = [dict(p) for p in manager.settings["projects"] if p.get("parent_id") == project_id]
    return {"project": project, "files": list(candidates.values()), "pages": pages,
            "children": children,
            "task": {k: task.get(k) for k in ("status", "files_discovered", "unvisited_pages", "undownloaded_due_to_limit", "errors")} if task else None}


def local_file(manager, project_id, key):
    manager.project(project_id)
    catalog = read_json(manager.store.root / "projects" / project_id / "catalog.json", {})
    file = catalog.get(key)
    if not file or not file.get("library_path"):
        raise ValueError("这个附件尚未下载到本机；请查看它的下载状态")
    path = Path(file["library_path"]).resolve()
    allowed = (manager.store.root / "projects" / project_id / "资料库").resolve()
    if not path.is_relative_to(allowed) or not path.is_file():
        raise ValueError("拒绝打开资料库目录以外的路径")
    return path


def write_offline_classroom(manager, destination=None):
    destination = Path(destination or manager.store.root / "本地课堂.html")
    projects = manager.snapshot()["projects"]
    escape = html.escape
    def section(project):
        data = project_library(manager, project["id"])
        text = '<section id="p-' + project["id"] + '"><h2>' + escape(project["name"]) + '</h2>'
        text += '<a href="' + escape(project["url"], quote=True) + '">查看 Classroom 原页面</a>'
        for page in data["pages"]:
            text += '<article><h3>' + escape(page["title"]) + '</h3><pre>' + escape(page["text"]) + '</pre>'
            if page.get("preview_truncated"):
                text += '<p>此处为前 30000 字预览，完整正文请查看导出资料包。</p>'
            text += '</article>'
        text += '<h3>课件与附件</h3><ul>'
        for file in data["files"]:
            label = escape(file["name"])
            if file.get("path"):
                from file_ops import SAFE_OPEN_EXTENSIONS
                if Path(file["path"]).suffix.lower() in SAFE_OPEN_EXTENSIONS:
                    label = '<a href="' + escape(Path(file["path"]).as_uri(), quote=True) + '">' + label + '</a>'
                else:
                    label += ' · 此类型请在图形助手中确认打开'
                if file.get("update_error"):
                    label += ' · 本次更新失败，保留旧副本：' + escape(file["update_error"])
            else:
                label += ' · ' + escape(file.get("error") or "尚未下载")
            text += '<li>' + label + '</li>'
        return text + '</ul></section>'
    content, navigation = [], []
    for course in [p for p in projects if not p.get("parent_id")]:
        children = [p for p in projects if p.get("parent_id") == course["id"]]
        navigation.append('<details open><summary><a href="#p-' + course["id"] + '">' + escape(course["name"]) + '</a></summary>')
        content.append(section(course))
        for topic in dict.fromkeys(p.get("topic") or "未分组" for p in children):
            navigation.append('<details open><summary>' + escape(topic) + '</summary><ul>')
            for child in [p for p in children if (p.get("topic") or "未分组") == topic]:
                navigation.append('<li><a href="#p-' + child["id"] + '">' + escape(child["name"]) + '</a></li>')
                content.append(section(child))
            navigation.append('</ul></details>')
        navigation.append('</details>')
    document = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>本地课堂</title><style>body{margin:0;background:#f6f8f3;color:#243b30;font:16px system-ui}header{padding:24px;background:#e5eee1}nav{position:fixed;top:110px;bottom:0;width:270px;padding:18px;overflow:auto}main{margin-left:310px;padding:20px}section{background:white;padding:24px;margin-bottom:20px;border-radius:12px;scroll-margin-top:20px}a{color:#0866bd}summary{cursor:pointer;padding:8px}li{padding:7px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit}article{border-bottom:1px solid #ddd} @media(max-width:700px){nav{position:static;width:auto}main{margin:0}}</style><header><h1>本地课堂</h1>已采集内容的离线副本 · 未下载附件会明确显示 · 课程页面未必全部覆盖</header><nav>' + ''.join(navigation) + '</nav><main>' + ''.join(content) + '</main></html>'
    descriptor, temporary = tempfile.mkstemp(prefix=".classroom-", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(document)
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return destination


def sync_to_folder(manager, selected_ids, provider, verify=False):
    with manager.cloud_transfer_lock:
        try:
            report = _sync_to_folder(manager, selected_ids, provider, verify)
        except (ValueError, OSError) as error:
            manager.record_cloud_report(provider, selected_ids, error=str(error))
            raise
        manager.record_cloud_report(provider, selected_ids, report=report)
        return report


def _cloud_files(manager, project):
    source = Path(project["latest_bundle"]).resolve()
    allowed = (manager.store.root / "projects" / project["id"] / "runs").resolve()
    if not source.is_relative_to(allowed):
        raise ValueError("资料包目录无效")
    manifest = read_json(source / "manifest.json", {})
    names = ["manifest.json", "ANALYSIS-PROMPT.txt", "START-HERE.txt"]
    names += [p["path"] for p in manifest.get("ai_parts", [])]
    names += [p["text_path"] for p in manifest.get("sources", [])]
    names += [p["path"] for p in manifest.get("files", [])]
    files = []
    for name in dict.fromkeys(names):
        path = (source / name).resolve()
        if Path(name).is_absolute() or ".." in Path(name).parts or not path.is_relative_to(source) or not path.is_file():
            raise ValueError("资料包包含无效路径或缺失文件")
        files.append((name, path))
    return files, manifest


def _stats(path):
    info = path.stat()
    return [info.st_size, info.st_mtime_ns]


def _cached_hash(path, cache):
    stamp = _stats(path)
    key = str(path)
    previous = cache.get(key, {})
    if previous.get("stat") != stamp:
        previous = {"stat": stamp, "sha256": file_hash(path)}
        cache[key] = previous
    return previous["sha256"]


def _valid_cloud_snapshot(target, digest, hashes, ledger, verify):
    complete = target / "完成标记.json"
    if complete.is_symlink() or not complete.is_file():
        return False
    previous = read_json(complete, {})
    if previous.get("format") != "course-material-sync-complete" or previous.get("fingerprint") != digest or set(previous.get("hashes", {})) != set(hashes):
        return False
    if any(previous["hashes"][name] != value for name, value in hashes.items() if name != "manifest.json"):
        return False
    known_ledger = set(ledger) == set(hashes)
    stamps = {}
    for name, expected in previous["hashes"].items():
        path = target / name
        if path.is_symlink() or not path.resolve().is_relative_to(target.resolve()) or not path.is_file():
            return False
        stamp = _stats(path)
        # A changed cloud file is preserved as a conflict; never overwrite user annotations.
        changed = bool(ledger.get(name) and ledger[name] != stamp)
        if verify or not known_ledger or changed:
            if file_hash(path) != expected:
                return False
        stamps[name] = stamp
    ledger.clear()
    ledger.update(stamps)
    return True


def _sync_to_folder(manager, selected_ids, provider, verify=False):
    if provider not in PROVIDERS:
        raise ValueError("不支持的网盘类型")
    with manager.lock:
        configuration = dict(manager.settings.get("cloud_folders", {}).get(provider, {}))
        selected = {manager.project(pid)["id"] for pid in selected_ids}
        projects = [dict(p) for p in manager.settings["projects"] if p["id"] in selected or p.get("parent_id") in selected]
    if not configuration.get("enabled") or not configuration.get("path"):
        raise ValueError("请先在设置中启用该网盘并选择官方客户端的同步目录")
    if not projects:
        raise ValueError("请先勾选要同步的课程或课题")
    base = Path(configuration["path"]).expanduser().resolve()
    if not base.is_dir() or base.is_relative_to(manager.store.root.resolve()) or manager.store.root.resolve().is_relative_to(base):
        raise ValueError("请选择独立的网盘同步目录，不能选择资料库或整个用户目录")
    if all(not project.get("latest_bundle") for project in projects):
        raise ValueError("所选项目均尚未完成首次同步")
    destination = base / "课题资料助手"
    destination.mkdir(exist_ok=True)
    if destination.is_symlink() or not destination.resolve().is_relative_to(base):
        raise ValueError("同步目标不能指向目录外部")
    state_path = manager.store.root / "cloud-copy-ledger.json"
    state = read_json(state_path, {"local_hashes": {}, "copies": {}})
    if not isinstance(state, dict) or not isinstance(state.get("local_hashes"), dict) or not isinstance(state.get("copies"), dict):
        state = {"local_hashes": {}, "copies": {}}
    cache, copies = state["local_hashes"], state["copies"]
    index_path = destination / "项目索引.json"
    if index_path.is_symlink():
        raise ValueError("同步索引不能是链接")
    index = read_json(index_path, {})
    if not isinstance(index, dict):
        raise ValueError("网盘项目索引格式无效")
    # Remove only stale files with our exact mkstemp name inside our owned snapshot tree.
    import re
    for temporary in destination.rglob(".copy-*"):
        if re.fullmatch(r"\.copy-[a-z0-9_]{8}", temporary.name) and not temporary.is_symlink() and temporary.resolve().is_relative_to(destination.resolve()):
            try:
                if time.time() - temporary.stat().st_mtime > 86400:
                    temporary.unlink()
            except OSError:
                pass
    included, skipped, failed, conflicts = [], [], [], []
    written = unchanged = 0
    for project in projects:
        if not project.get("latest_bundle"):
            skipped.append({"id": project["id"], "name": project["name"], "reason": "尚未完成首次同步"})
            continue
        try:
            files, manifest = _cloud_files(manager, project)
            hashes = {name: _cached_hash(path, cache) for name, path in files}
            semantic = {"files": {name: digest for name, digest in hashes.items() if name != "manifest.json"},
                        "status": manifest.get("status"), "errors": manifest.get("errors"),
                        "unvisited_pages": manifest.get("unvisited_pages"), "undownloaded_due_to_limit": manifest.get("undownloaded_due_to_limit")}
            digest = hashlib.sha256(json.dumps(semantic, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]
            parent = destination / project["id"]
            parent.mkdir(exist_ok=True)
            if parent.is_symlink() or not parent.resolve().is_relative_to(destination.resolve()):
                raise ValueError("同步目标目录无效")
            latest = parent / "最新资料.json"
            if latest.is_symlink():
                raise ValueError("同步索引不能是链接")
            # Reuse the last fork if an older snapshot contains user edits.
            prior = index.get(project["id"], {}).get("folder", "")
            candidate = prior.split("/")[-1] if prior.startswith(project["id"] + "/") else digest
            if not re_cloud_name(candidate, digest):
                candidate = digest
            target = parent / candidate
            key = str(target)
            ledger = copies.get(key, {})
            valid = False
            if target.exists() and not target.is_symlink():
                try:
                    valid = _valid_cloud_snapshot(target, digest, hashes, ledger, verify)
                except (ValueError, OSError):
                    valid = False
            if valid:
                unchanged += 1
            else:
                if target.exists() or target.is_symlink():
                    conflicts.append({"id": project["id"], "name": project["name"], "reason": "保留已有修改或不完整副本，写入新的完整副本"})
                    target = parent / (digest + "-" + secrets.token_hex(4))
                    key, ledger = str(target), {}
                target.mkdir(exist_ok=False)
                for name, source in files:
                    output = target / name
                    output.parent.mkdir(parents=True, exist_ok=True)
                    if output.is_symlink() or not output.parent.resolve().is_relative_to(target.resolve()):
                        raise ValueError("同步文件路径无效")
                    atomic_copy(source, output, hashes[name])
                    ledger[name] = _stats(output)
                write_private(target / "完成标记.json", {"format": "course-material-sync-complete", "fingerprint": digest, "hashes": hashes})
                written += 1
            copies[key] = ledger
            public = {"name": project["name"], "parent_id": project.get("parent_id"), "topic": project.get("topic"),
                      "folder": project["id"] + "/" + target.name, "status": "complete"}
            write_private(latest, public)
            index[project["id"]] = public
            included.append(project["id"])
        except (ValueError, OSError, KeyError, TypeError) as error:
            failed.append({"id": project["id"], "name": project["name"], "reason": str(error)[:300]})
    # Both successful and failed projects are accounted for; one project cannot starve others.
    write_private(state_path, state)
    write_private(index_path, index)
    return {"provider": provider, "projects": len(included), "included": included, "written": written, "unchanged": unchanged,
            "skipped": skipped, "failed": failed, "conflicts": conflicts, "path": str(destination),
            "status": "partial" if failed or skipped else "written_to_sync_folder",
            "note": "内容未变不重复复制；日常仅核对本机台账与文件元数据，不回读网盘大文件。手动校验会读取原件。实际上传由网盘客户端完成。"}


def re_cloud_name(candidate, digest):
    import re
    return bool(re.fullmatch(re.escape(digest) + r"(?:-[a-f0-9]{8})?", candidate))
