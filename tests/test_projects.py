import base64
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
import zipfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
from browser_rpc import save_download
from projects import ProjectManager
from store import Store, utc_now


class ProjectsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        class Broker:
            connected = True
            def status(self):
                return {"connected": self.connected}
            def call(self, operation, args, timeout=80):
                return {"tab_id": 1} if operation == "open" else {"closed": True}
        self.broker = Broker()
        self.file = save_download(self.store, {"filename": "reading.txt", "mime": "text/plain", "source_url": "https://drive.google.com/file/d/file1/view", "data_base64": base64.b64encode("生态文献全文".encode()).decode()})
        task = {"task_id": "a" * 24, "status": "completed", "pages": [], "downloads": [self.file], "errors": []}
        class Collector:
            def start(self, tab, **settings):
                return {"task_id": task["task_id"]}
            def status(self, task_id):
                return task
        self.manager = ProjectManager(self.store, self.broker, Collector(), clock=lambda: 1000)

    def wait(self):
        deadline = time.monotonic() + 3
        while self.manager.active and time.monotonic() < deadline:
            time.sleep(.02)

    def test_background_sync_classifies_and_scopes_materials(self):
        project = self.manager.add("生态课题", "https://classroom.google.com/c/course1")
        self.manager.tick()
        self.wait()
        state = self.manager.snapshot()["projects"][0]
        self.assertEqual(state["status"], "ready")
        self.assertEqual(state["changed"], 1)
        self.assertTrue((Path(state["latest_bundle"]) / "ai-materials.zip").exists())
        self.assertEqual(len(list((self.store.root / "projects" / project["id"] / "资料库" / "文档").glob("*.txt"))), 1)
        self.assertEqual(len(self.manager.search("生态")["items"]), 1)
        self.assertIn("全文", self.manager.dispatch("read", {"material_id": self.file["material_id"]})["text"])
        with self.assertRaises(ValueError):
            self.manager.dispatch("read", {"material_id": "unselected"})

    def test_offline_pause_and_duplicate_guards(self):
        self.manager.add("生态课题", "https://classroom.google.com/c/course1")
        self.manager.set_automatic(False)
        self.manager.tick()
        self.assertIsNone(self.manager.active)
        self.broker.connected = False
        with self.assertRaises(ValueError):
            self.manager.request_sync(self.manager.settings["projects"][0]["id"])
        with self.assertRaises(ValueError):
            self.manager.add("重复课程", "https://classroom.google.com/c/course1?authuser=1")
        with self.assertRaises(ValueError):
            self.manager.add("非课程", "https://evil.example/")

    def test_chat_can_add_an_explicit_course_and_pause(self):
        project = self.manager.dispatch("add", {"name": "会话中指定的课题", "url": "https://classroom.google.com/c/course2"})
        self.assertEqual(self.manager.project(project["id"])["name"], "会话中指定的课题")
        self.assertFalse(self.manager.dispatch("automatic", {"enabled": False})["automatic"])
        with self.assertRaises(ValueError):
            self.manager.dispatch("add", {"name": "外部", "url": "file:///etc/passwd"})

    def test_auto_discovery_hierarchy_is_idempotent_and_migrates_existing_course(self):
        parent = self.manager.add("设计演习", "https://classroom.google.com/c/course1")
        self.manager.project(parent["id"]).pop("parent_id")
        self.manager._save()
        self.manager = ProjectManager(self.store, self.broker, self.manager.collector, clock=lambda: 1000)
        calls = []
        def browser(operation, args, timeout=80):
            calls.append(operation)
            if operation == "tabs":
                return {"tabs": [{"tab_id": 8, "url": "https://classroom.google.com/w/course1/t/all", "title": "设计演习"}]}
            if operation == "open":
                return {"tab_id": 9}
            if operation == "collect":
                return {"capture": {"links": [
                    {"title": "河流课题", "url": "https://classroom.google.com/c/course1/a/work1/details"},
                    {"title": "同一课题入口", "url": "https://classroom.google.com/c/course1/a/work1/submissions/details"},
                    {"title": "其他课程课题", "url": "https://classroom.google.com/c/course2/a/work2/details"}]}}
            return {"closed": True}
        self.broker.call = browser
        with patch.object(self.manager, "request_sync") as sync:
            self.manager.tick()
            sync.assert_called_once()
        self.manager.import_discovered()
        state = self.manager.snapshot()["projects"]
        self.assertEqual(len(state), 2)
        self.assertEqual(state[1]["parent_id"], parent["id"])
        self.assertEqual(state[1]["kind"], "assignment")
        self.assertEqual(state[0]["name"], "设计演习")
        self.assertEqual(calls.count("close"), 2)

    def test_multi_export_parent_expands_children_and_rejects_external_paths(self):
        parent = self.manager.add("课程", "https://classroom.google.com/c/course1")
        child = self.manager.add("课题一", "https://classroom.google.com/c/course1/a/work1/details", parent_id=parent["id"])
        other = self.manager.add("不选这门", "https://classroom.google.com/c/course2")
        for p in (parent, child):
            self.manager.request_sync(p["id"])
            self.wait()
        target = self.store.root / "export.zip"
        result = self.manager.export_selected([parent["id"], child["id"]], target)
        self.assertEqual(result["projects"], 2)
        with zipfile.ZipFile(target) as archive:
            index = json.loads(archive.read("项目清单.json"))
            self.assertEqual({p["id"] for p in index}, {parent["id"], child["id"]})
            self.assertNotIn("pairing.json", str(archive.namelist()))
            self.assertTrue(any("for-ai/" in name for name in archive.namelist()))
        self.assertEqual(self.manager.export_selected([child["id"]], target)["projects"], 1)
        with self.assertRaisesRegex(ValueError, "尚未完成"):
            self.manager.export_selected([other["id"]], target)
        self.manager.project(child["id"])["latest_bundle"] = str(self.store.root)
        with self.assertRaisesRegex(ValueError, "目录无效"):
            self.manager.export_selected([child["id"]], target)

    def test_upgrade_keeps_pairing_files_and_preferences(self):
        project = self.manager.add("保留名称", "https://classroom.google.com/c/course1")
        self.manager.set_automatic(False)
        self.manager.settings["relay_url"] = "https://example.chatgpt.site"
        self.manager._save()
        (self.store.root / "pairing.json").write_text('{"token":"synthetic-only"}', encoding="utf-8")
        before = {p.relative_to(self.store.root): p.read_bytes() for p in self.store.root.rglob("*") if p.is_file()}
        updated = ProjectManager(self.store, self.broker, self.manager.collector)
        self.assertEqual(updated.project(project["id"])["name"], "保留名称")
        self.assertFalse(updated.settings["automatic"])
        self.assertEqual(updated.settings["relay_url"], "https://example.chatgpt.site")
        for name, content in before.items():
            self.assertEqual((self.store.root / name).read_bytes(), content)

    def test_unchanged_scan_reuses_local_snapshot_and_failed_update_keeps_original_in_bundle(self):
        self.file["freshness_warning"] = "来源无版本标记，按设置定期复查"
        self.file["checked_at"] = 100
        project = self.manager.add("课程", "https://classroom.google.com/c/course1")
        self.manager.request_sync(project["id"])
        self.wait()
        first = self.manager.project(project["id"])["latest_bundle"]
        self.file["checked_at"] = 200
        same = {"task_id": "b" * 24, "status": "completed", "pages": [], "downloads": [self.file], "errors": []}
        self.manager.collector.start = lambda *args, **kwargs: {"task_id": same["task_id"]}
        self.manager.collector.status = lambda _: same
        self.manager.request_sync(project["id"])
        self.wait()
        self.assertEqual(self.manager.project(project["id"])["status"], "ready")
        self.assertEqual(self.manager.project(project["id"])["latest_bundle"], first)
        self.assertEqual(len(list((self.store.root / "projects" / project["id"] / "runs").iterdir())), 1)
        failed = {"task_id": "c" * 24, "status": "completed_with_gaps", "pages": [], "downloads": [],
                  "errors": [{"stage": "download", "source_url": self.file["source_url"], "error": "synthetic unavailable"}]}
        self.manager.collector.start = lambda *args, **kwargs: {"task_id": failed["task_id"]}
        self.manager.collector.status = lambda _: failed
        self.manager.request_sync(project["id"])
        self.wait()
        manifest = json.loads((Path(self.manager.project(project["id"])["latest_bundle"]) / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["files"]), 1)
        self.assertEqual(manifest["errors"][0]["error"], "synthetic unavailable")


if __name__ == "__main__":
    unittest.main(verbosity=2)
