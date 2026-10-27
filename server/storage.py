"""Selectable material storage. Credentials and the extension remain in the control directory."""
from contextlib import contextmanager, closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
import re
import uuid

from file_ops import atomic_copy, file_hash
from google_api import write_private

MATERIAL_DIRS = ("attachments", "projects", "collections", "exports")
MATERIAL_FILES = ("materials.sqlite3", "download-cache.json", "cloud-copy-ledger.json", "本地课堂.html")
PATH_FIELDS = {"download_path", "library_path", "latest_bundle", "directory", "ai_archive"}


def location(control):
    path = Path(control) / "storage-location.json"
    if not path.exists():
        return Path(control).resolve()
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema") != 1 or not isinstance(value.get("path"), str) or not Path(value["path"]).is_absolute():
        raise ValueError("资料保存位置配置无效，请在设置中重新选择")
    return Path(value["path"]).resolve()


@contextmanager
def switch_lock(control, wait=0):
    """OS-owned lock releases on process death; the file itself is harmless."""
    path = Path(control) / "storage-switch.lock"
    with path.open("a+b") as handle:
        handle.seek(0, 2)
        if not handle.tell():
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        deadline = time.monotonic() + wait
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise ValueError("资料库正在切换或被其他进程使用，请稍后再试") from None
                time.sleep(.03)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def recover(control, wait=30):
    journal = Path(control) / "storage-switch.json"
    if not journal.exists():
        return
    with switch_lock(control, wait=wait):
        if journal.exists():
            data = json.loads(journal.read_text(encoding="utf-8"))
            # The single atomic location file is the commit point. Originals are kept until then.
            committed = location(control) == Path(data["target"]).resolve()
            projects = data["new_projects"] if committed else data["old_projects"]
            if projects is not None:
                write_private(Path(control) / "projects.json", projects)
            if not committed and data.get("id"):
                for name in ("target", "staging"):
                    path = Path(data.get(name, "/missing"))
                    marker = path / ".course-material-storage.json"
                    if path.is_absolute() and not path.is_symlink() and marker.is_file() and json.loads(marker.read_text()).get("id") == data["id"]:
                        shutil.rmtree(path)
            journal.unlink()


def rewrite_paths(value, source, destination, field=None):
    if isinstance(value, dict):
        return {k: rewrite_paths(v, source, destination, k) for k, v in value.items()}
    if isinstance(value, list):
        return [rewrite_paths(v, source, destination, field) for v in value]
    if field in PATH_FIELDS and isinstance(value, str):
        path = Path(value)
        if path.is_absolute() and path.is_relative_to(source):
            return str(destination / path.relative_to(source))
    return value


def validate_destination(store, target):
    target = Path(target).expanduser().resolve()
    source, control = store.root.resolve(), store.control_root.resolve()
    if target == source:
        return target
    if not target.is_absolute() or target.parent == target or target == Path.home().resolve():
        raise ValueError("请选择专门存放课题资料的文件夹，不要选择磁盘根目录或整个用户目录")
    if target.is_relative_to(source) or source.is_relative_to(target) or target.is_relative_to(control) or control.is_relative_to(target):
        raise ValueError("新位置不能与当前资料目录或程序配置目录互相包含")
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise ValueError("新位置需要是空文件夹，以免覆盖已有资料")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, probe = tempfile.mkstemp(prefix=".course-write-check-", dir=target.parent)
    os.close(descriptor)
    Path(probe).unlink()
    return target


def managed_json(relative):
    """Only application metadata is rewritten. Course JSON attachments are opaque originals."""
    parts = relative.parts
    if len(parts) == 1 and parts[0] in {"download-cache.json", "cloud-copy-ledger.json"}:
        return True
    if len(parts) == 2 and parts[0] == "collections" and re.fullmatch(r"[a-f0-9]{24}\.json", parts[1]):
        return True
    return (len(parts) == 3 and parts[0] == "projects" and re.fullmatch(r"[a-f0-9]{24}", parts[1]) and parts[2] == "catalog.json") or (len(parts) == 5 and parts[0] == "projects" and re.fullmatch(r"[a-f0-9]{24}", parts[1]) and parts[2] == "runs" and re.fullmatch(r"[a-f0-9]{24}", parts[3]) and parts[4] == "manifest.json")


def storage_available(control, root):
    pointer = Path(control) / "storage-location.json"
    if not pointer.exists():
        return root == Path(control).resolve()
    try:
        info = json.loads(pointer.read_text(encoding="utf-8"))
        marker = json.loads((root / ".course-material-storage.json").read_text(encoding="utf-8"))
        return marker.get("schema") == 1 and (not info.get("id") or info["id"] == marker.get("id")) and (root / "materials.sqlite3").is_file()
    except (OSError, ValueError):
        return False


def _migrate(store, target, progress=None, remove_old=True):
    """Caller pauses collectors/HTTP writes and waits for active material operations first."""
    progress = progress or (lambda message: None)
    store.require_available()
    target = validate_destination(store, target)
    source, control = store.root.resolve(), store.control_root.resolve()
    if source == target:
        with store.material_access():
            marker = source / ".course-material-storage.json"
            identity = json.loads(marker.read_text()).get("id") if marker.exists() else uuid.uuid4().hex
            write_private(marker, {"schema": 1, "id": identity})
            write_private(control / "storage-location.json", {"schema": 1, "path": str(target), "id": identity})
        return {"path": str(target), "moved": False, "leftovers": []}
    with store.material_access():
        owned = [source / name for name in MATERIAL_DIRS + MATERIAL_FILES if (source / name).exists()]
        files = []
        for entry in owned:
            nodes = [entry] + (list(entry.rglob("*")) if entry.is_dir() else [])
            for path in nodes:
                if path.is_symlink() or getattr(path, "is_junction", lambda: False)() or not path.resolve().is_relative_to(source):
                    raise ValueError("资料中存在目录链接，请先移除链接再迁移，以免复制到目录外的文件")
                if path.is_file() and path.name != "materials.sqlite3":
                    files.append(path)
        original_stats = {path: (path.stat().st_size, path.stat().st_mtime_ns) for path in files}
        required = sum(path.stat().st_size for path in files) + store.path.stat().st_size
        if shutil.disk_usage(target.parent).free < required + 32*1024*1024:
            raise ValueError("新位置空间不足，尚未更改保存位置")
        project_path = control / "projects.json"
        original_projects = json.loads(project_path.read_text(encoding="utf-8")) if project_path.exists() else None
        new_projects = rewrite_paths(original_projects, source, target)
        staging = Path(tempfile.mkdtemp(prefix=".course-move-", dir=target.parent))
        committed = False
        identity = uuid.uuid4().hex
        created_target = False
        try:
            progress("正在复制并校验原始资料…")
            for index, path in enumerate(files):
                output = staging / path.relative_to(source)
                output.parent.mkdir(parents=True, exist_ok=True)
                if managed_json(path.relative_to(source)):
                    value = json.loads(path.read_text(encoding="utf-8"))
                    if path.name == "cloud-copy-ledger.json":
                        value["local_hashes"] = {}  # Keys point to the old local bundle paths.
                    write_private(output, rewrite_paths(value, source, target))
                elif path.name != "本地课堂.html":
                    atomic_copy(path, output, file_hash(path))
                if index % 20 == 0:
                    progress(f"正在迁移资料：{index + 1}/{len(files)}")
            # SQLite backup yields a consistent database rather than a possibly incomplete file copy.
            with closing(sqlite3.connect(store.path)) as old_db, closing(sqlite3.connect(staging / "materials.sqlite3")) as new_db:
                old_db.backup(new_db)
                if new_db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("资料数据库校验失败，仍使用原保存位置")
            for name in MATERIAL_DIRS:
                if (source / name).is_dir():
                    (staging / name).mkdir(exist_ok=True)
            write_private(staging / ".course-material-storage.json", {"schema": 1, "id": identity})
            # JSON manifests were rewritten, so refresh bundle integrity metadata at staged paths.
            for project in (new_projects or {}).get("projects", []):
                bundle = Path(project.get("latest_bundle") or target)
                if not project.get("latest_bundle") or not bundle.resolve().is_relative_to(target):
                    continue
                for name in list(project.get("bundle_hashes", {})):
                    final_path = (bundle / name).resolve()
                    if not final_path.is_relative_to(bundle.resolve()):
                        raise ValueError("资料包校验路径无效，仍使用原保存位置")
                    path = staging / final_path.relative_to(target)
                    if not path.is_file():
                        raise ValueError("资料包校验路径无效，仍使用原保存位置")
                    project["bundle_hashes"][name] = file_hash(path)
                    info = path.stat()
                    project.setdefault("bundle_stats", {})[name] = [info.st_size, info.st_mtime_ns]
            if any(not path.is_file() or (path.stat().st_size, path.stat().st_mtime_ns) != stamp for path, stamp in original_stats.items()):
                raise ValueError("迁移期间原资料被其他程序修改，请关闭正在编辑的文件后重试")
            write_private(control / "storage-switch.json", {"source": str(source), "target": str(target), "staging": str(staging), "id": identity, "old_projects": original_projects, "new_projects": new_projects})
            if target.exists():
                target.rmdir()
            for attempt in range(40):
                try:
                    os.replace(staging, target)
                    break
                except PermissionError:
                    if attempt == 39:
                        target.mkdir(exist_ok=True)
                        raise
                    time.sleep(.1)
            created_target = True
            write_private(control / "storage-location.json", {"schema": 1, "path": str(target), "id": identity})
            committed = True
            if new_projects is not None:
                write_private(project_path, new_projects)
            store.root, store.path = target, target / "materials.sqlite3"
            leftovers = []
            if remove_old:
                progress("新位置已校验，正在清理旧位置的资料副本…")
                for entry in owned:
                    try:
                        if any(not path.is_file() or (path.stat().st_size, path.stat().st_mtime_ns) != stamp for path, stamp in original_stats.items() if path == entry or entry in path.parents):
                            leftovers.append(str(entry))
                            continue
                        if entry.is_dir():
                            shutil.rmtree(entry)
                        else:
                            entry.unlink(missing_ok=True)
                    except OSError:
                        leftovers.append(str(entry))
            (control / "storage-switch.json").unlink(missing_ok=True)
            return {"path": str(target), "moved": True, "leftovers": leftovers}
        except Exception:
            if committed:
                # Complete a committed switch even if cleanup or a settings write was interrupted.
                store.root, store.path = target, target / "materials.sqlite3"
                if new_projects is not None:
                    write_private(project_path, new_projects)
            elif created_target:
                marker = target / ".course-material-storage.json"
                if marker.is_file() and json.loads(marker.read_text()).get("id") == identity:
                    shutil.rmtree(target)
            raise
        finally:
            if staging.exists():
                shutil.rmtree(staging)


def migrate(store, target, progress=None, remove_old=True):
    # Acquire before resolving the source: another process may have switched it.
    with store.material_access():
        return _migrate(store, target, progress=progress, remove_old=remove_old)
