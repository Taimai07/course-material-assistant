import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
import os
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
from browser_rpc import BrowserBroker, save_download, import_native_download
from file_ops import safe_filename, atomic_copy, file_hash
from bundle import export_task
from library import project_library, local_file, write_offline_classroom, sync_to_folder
from projects import ProjectManager
from collector import CourseCollector, resource_key
import time
from store import Store, utc_now


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "data")
        self.broker = BrowserBroker(store=self.store)
        class Collector:
            def status(self, task_id):
                raise ValueError("no task")
        self.manager = ProjectManager(self.store, self.broker, Collector())
        self.course = self.manager.add("课程 <script>alert(1)</script>", "https://classroom.google.com/c/course1")
        self.child = self.manager.add("课题一", "https://classroom.google.com/c/course1/a/work1/details", parent_id=self.course["id"])
        self.manager.project(self.child["id"])["topic"] = "第一单元"
        self.saved = save_download(self.store, {"filename": "课件.txt", "source_url": "https://drive.google.com/file/d/file1/view",
                                                  "mime": "text/plain", "data_base64": base64.b64encode("课件完整正文".encode()).decode()})
        self.manager.record_download(self.child["id"], self.saved)

    def ready(self, task_id="a" * 24):
        folder = self.store.root / "projects" / self.child["id"] / "runs" / task_id
        task = {"task_id": task_id, "status": "completed", "pages": [], "downloads": [self.saved], "errors": []}
        result = export_task(self.store, task, folder)
        self.manager.project(self.child["id"]).update(latest_bundle=result["directory"], last_synced=123, status="ready")

    def test_project_file_navigation_and_html_escape(self):
        view = project_library(self.manager, self.child["id"])
        self.assertEqual(view["files"][0]["status"], "downloaded")
        self.assertTrue(local_file(self.manager, self.child["id"], view["files"][0]["key"]).is_file())
        page = write_offline_classroom(self.manager).read_text(encoding="utf-8")
        self.assertIn("第一单元", page)
        self.assertIn("课题一", page)
        self.assertIn("file:///", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertNotIn("<script>", page)

    def test_pending_downloads_are_distinct_from_downloaded_without_text(self):
        self.manager.project(self.child["id"])["current_task"] = "a" * 24
        self.manager.collector.status = lambda _: {"pages": [], "discovered_files": [
            {"title": "课件.txt", "url": self.saved["source_url"]},
            {"title": "待下载.pdf", "url": "https://drive.google.com/file/d/file2/view"}],
            "errors": [{"stage": "download", "source_url": "https://drive.google.com/file/d/file2/view", "error": "HTTP 403"}]}
        view = project_library(self.manager, self.child["id"])
        self.assertEqual({f["status"] for f in view["files"]}, {"downloaded", "error"})
        self.assertIn("HTTP 403", view["files"][1]["error"])

    def test_preferences_persist_and_do_not_enable_cloud_by_default(self):
        self.assertEqual(self.manager.settings["cloud_folders"], {})
        with self.assertRaises(ValueError):
            sync_to_folder(self.manager, [self.child["id"]], "Google Drive")
        self.manager.configure(128, 5000, {}, False)
        restored = ProjectManager(self.store, self.broker, self.manager.collector)
        self.assertEqual(restored.settings["large_file_mib"], 128)
        self.assertEqual(restored.settings["scan_max_files"], 5000)
        with self.assertRaises(ValueError):
            self.manager.configure(0, 100, {})
        with self.assertRaises(ValueError):
            self.manager.configure(10, 100, {"Google Drive": {"enabled": True, "path": str(self.store.root)}})

    def test_cloud_folder_receives_readable_files_and_never_credentials(self):
        self.ready()
        cloud = Path(self.temp.name) / "cloud"
        cloud.mkdir()
        (self.store.root / "pairing.json").write_text('{"token":"must-not-copy"}', encoding="utf-8")
        self.manager.configure(50, 1000, {"Google Drive": {"enabled": True, "path": str(cloud)}})
        result = sync_to_folder(self.manager, [self.child["id"]], "Google Drive")
        self.assertEqual(result["status"], "written_to_sync_folder")
        names = [p.name for p in cloud.rglob("*") if p.is_file()]
        self.assertIn("manifest.json", names)
        self.assertIn("part-0001.md", names)
        self.assertNotIn("pairing.json", names)
        self.assertTrue(any(p.read_text(encoding="utf-8").find("课件完整正文") >= 0 for p in cloud.rglob("part-*.md")))
        again = sync_to_folder(self.manager, [self.child["id"]], "Google Drive")
        self.assertEqual(again["projects"], 1)
        self.assertEqual(len(list(cloud.rglob("manifest.json"))), 1)

    def test_large_file_streams_after_this_jobs_approval(self):
        self.broker.connect("large_file_session_abcdefghijkl")
        self.broker.confirm_download = lambda info: True
        self.manager.settings["large_file_mib"] = 1
        length = 21 * 1024 * 1024
        with ThreadPoolExecutor() as executor:
            future = executor.submit(self.broker.call, "download_url", {"url": self.saved["source_url"]}, 10)
            job = self.broker.next("large_file_session_abcdefghijkl", timeout=1)["job"]
            token = job["arguments"]["download_token"]
            path = Path(self.temp.name) / "Downloads" / "CourseMaterialAssistant" / token / "large.pdf"
            path.parent.mkdir(parents=True)
            with path.open("wb") as output:
                output.write(b"%PDF synthetic large file\n")
                output.truncate(length)
            native = {"filename": "large.pdf", "mime": "application/pdf", "source_url": self.saved["source_url"],
                      "native_download": {"id": 1, "path": str(path), "token": token}}
            self.assertTrue(self.broker.decide_download(token, "large.pdf", length)["allowed"])
            self.broker.finish("large_file_session_abcdefghijkl", job["id"], native)
            saved = save_download(self.store, future.result())
        self.assertEqual(saved["bytes"], length)
        self.assertEqual(Path(saved["download_path"]).stat().st_size, length)
        self.assertFalse(saved["text_extracted"])
        self.assertNotIn("data_base64", saved)

    def test_large_file_without_approval_is_not_imported(self):
        token = "a" * 32
        path = Path(self.temp.name) / "CourseMaterialAssistant" / token / "large.pdf"
        path.parent.mkdir(parents=True)
        with path.open("wb") as output:
            output.truncate(2 * 1024 * 1024)
        native = {"filename": "large.pdf", "mime": "application/pdf", "source_url": self.saved["source_url"],
                  "native_download": {"id": 1, "path": str(path), "token": token}}
        with self.assertRaisesRegex(ValueError, "确认"):
            import_native_download(self.store, native, token, 1024 * 1024)
        self.assertEqual(list((self.store.root / "attachments").glob(".incoming-*")), [])

    def cloud(self):
        cloud = Path(self.temp.name) / "cloud"
        cloud.mkdir(exist_ok=True)
        self.manager.configure(1, 1000, {"Google Drive": {"enabled": True, "path": str(cloud)}})
        return cloud

    def test_changed_run_id_does_not_duplicate_cloud_contents(self):
        self.ready()
        cloud = self.cloud()
        first = sync_to_folder(self.manager, [self.course["id"]], "Google Drive")
        self.assertEqual(len(first["skipped"]), 1)
        self.ready("b" * 24)
        second = sync_to_folder(self.manager, [self.course["id"]], "Google Drive")
        self.assertEqual(second["written"], 0)
        self.assertEqual(second["unchanged"], 1)
        self.assertEqual(len(list(cloud.rglob("manifest.json"))), 1)
        marker = next(cloud.rglob("完成标记.json"))
        bad = json.loads(marker.read_text(encoding="utf-8"))
        bad["hashes"] = {}
        marker.write_text(json.dumps(bad), encoding="utf-8")
        recovered = sync_to_folder(self.manager, [self.child["id"]], "Google Drive")
        self.assertEqual(recovered["written"], 1)
        self.assertEqual(len(recovered["conflicts"]), 1)
        self.assertEqual(json.loads(marker.read_text(encoding="utf-8"))["hashes"], {})

    def test_partial_export_and_failed_replacement_preserves_existing_archive(self):
        self.ready()
        target = Path(self.temp.name) / "combined.zip"
        result = self.manager.export_selected([self.course["id"]], target)
        self.assertEqual(result["projects"], 1)
        self.assertEqual(result["skipped"][0]["id"], self.course["id"])
        original = target.read_bytes()
        bundle = Path(self.manager.project(self.child["id"])["latest_bundle"])
        (bundle / "START-HERE.txt").unlink()
        with self.assertRaises(ValueError):
            self.manager.export_selected([self.course["id"]], target)
        self.assertEqual(target.read_bytes(), original)
        self.assertFalse(list(target.parent.glob(".course-export-*")))

    def test_atomic_copy_repairs_corruption_without_replacing_unchanged_open_file(self):
        original = Path(self.saved["download_path"])
        target = Path(self.temp.name) / "copy.txt"
        atomic_copy(original, target, self.saved["sha256"])
        target.write_bytes(b"corrupted")
        atomic_copy(original, target, self.saved["sha256"])
        self.assertEqual(file_hash(target), self.saved["sha256"])
        with patch("file_ops.os.replace", side_effect=PermissionError("synthetic file lock")):
            atomic_copy(original, target, self.saved["sha256"])

    def test_native_reuse_keeps_validators_and_does_not_replace_open_original(self):
        token = "b" * 32
        source = Path(self.temp.name) / "CourseMaterialAssistant" / token / "paper.txt"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"paper")
        native = {"filename": "CON.txt", "mime": "text/plain", "source_url": self.saved["source_url"],
                  "etag": '"v1"', "last_modified": "Mon, 26 Oct 2026 12:00:00 GMT",
                  "native_download": {"id": 2, "path": str(source), "token": token}}
        first = import_native_download(self.store, native, token, 1024)
        with patch("browser_rpc.os.replace", side_effect=PermissionError("synthetic file lock")):
            second = import_native_download(self.store, native, token, 1024)
        self.assertEqual(first["download_path"], second["download_path"])
        self.assertEqual(second["etag"], '"v1"')
        self.assertEqual(second["filename"], "_CON.txt")
        self.assertTrue(second["text_extracted"])

    def test_same_size_and_google_warning_choices_are_remembered(self):
        session = "remember_choice_session_abcdefghijkl"
        self.broker.connect(session)
        prompts = []
        self.broker.confirm_download = lambda info: prompts.append(info) or True
        self.manager.settings["large_file_mib"] = 1
        for _ in range(2):
            with ThreadPoolExecutor() as executor:
                future = executor.submit(self.broker.call, "download_url", {"url": self.saved["source_url"]}, 5)
                job = self.broker.next(session, timeout=1)["job"]
                token = job["arguments"]["download_token"]
                self.broker.progress_download(token, 100)
                self.assertEqual(self.broker.status()["active"]["bytes"], 100)
                self.assertTrue(self.broker.decide_download(token, "file.pdf", 2*1024*1024)["allowed"])
                if len(prompts) == 1:
                    # Busy commands fail promptly; they do not sit behind the confirmation.
                    with self.assertRaisesRegex(ValueError, "正在下载"):
                        self.broker.call("tabs", {}, 1)
                self.broker.finish(session, job["id"], {"skipped": True})
                future.result()
        self.assertEqual(len(prompts), 1)
        with ThreadPoolExecutor() as executor:
            future = executor.submit(self.broker.call, "download_url", {"url": self.saved["source_url"]}, 5)
            job = self.broker.next(session, timeout=1)["job"]
            token = job["arguments"]["download_token"]
            for _ in range(2):
                self.assertTrue(self.broker.decide_download(token, "file.pdf", None, "google_scan_warning")["allowed"])
            self.broker.finish(session, job["id"], {"skipped": True})
            future.result()
        self.assertEqual(len(prompts), 2)

    def test_failed_update_keeps_old_download_visible_with_error(self):
        self.manager.project(self.child["id"])["current_task"] = "a" * 24
        self.manager.collector.status = lambda _: {"pages": [], "errors": [{"stage": "download", "source_url": self.saved["source_url"], "error": "synthetic unavailable"}]}
        file = project_library(self.manager, self.child["id"])["files"][0]
        self.assertEqual(file["status"], "downloaded")
        self.assertTrue(Path(file["path"]).exists())
        self.assertEqual(file["update_error"], "synthetic unavailable")

    def test_reserved_names_and_bidi_controls_are_sanitized(self):
        self.assertEqual(safe_filename("NUL.pdf"), "_NUL.pdf")
        self.assertEqual(safe_filename("slides\u202efdp.exe"), "slides_fdp.exe")
        self.assertNotIn("/", safe_filename("../../file.pdf"))

    @unittest.skipUnless(os.name == "nt", "Windows ADS verification")
    def test_windows_origin_mark_exists_after_download_and_library_copy(self):
        self.assertIn("ZoneId=3", Path(self.saved["download_path"] + ":Zone.Identifier").read_text())
        path = project_library(self.manager, self.child["id"])["files"][0]["path"]
        self.assertIn("ZoneId=3", Path(path + ":Zone.Identifier").read_text())

    def test_one_broken_project_does_not_block_later_cloud_projects_or_index(self):
        self.ready()
        cloud = self.cloud()
        broken = self.manager.add("损坏子项目", "https://classroom.google.com/c/course1/a/work2/details", parent_id=self.course["id"])
        self.manager.project(broken["id"])["latest_bundle"] = str(self.store.root / "projects" / broken["id"] / "runs" / ("c" * 24))
        projects = self.manager.settings["projects"]
        projects.insert(1, projects.pop())
        result = sync_to_folder(self.manager, [self.course["id"]], "Google Drive")
        self.assertEqual(result["included"], [self.child["id"]])
        self.assertEqual(result["failed"][0]["id"], broken["id"])
        index = json.loads((cloud / "课题资料助手" / "项目索引.json").read_text(encoding="utf-8"))
        self.assertIn(self.child["id"], index)
        self.assertIn("Google Drive", self.manager.project(broken["id"])["cloud_errors"])

    def test_repeated_sync_does_not_read_cloud_originals_or_erase_other_provider_errors(self):
        self.ready()
        cloud = self.cloud()
        first = sync_to_folder(self.manager, [self.child["id"]], "Google Drive")
        self.manager.record_cloud_report("OneDrive", [self.child["id"]], error="synthetic offline")
        original_hash = file_hash
        def no_remote_read(path):
            self.assertFalse(Path(path).resolve().is_relative_to(cloud.resolve()))
            return original_hash(path)
        with patch("library.file_hash", side_effect=no_remote_read):
            result = sync_to_folder(self.manager, [self.child["id"]], "Google Drive")
        self.assertEqual(result["unchanged"], 1)
        self.assertEqual(self.manager.project(self.child["id"])["cloud_errors"]["OneDrive"], "synthetic offline")
        self.assertEqual(len(self.manager.client_state["cloud_folder_errors"]), 1)
        # Explicit verification reads originals; a user's change is retained in a conflict copy.
        remote = next(cloud.rglob("*-课件.txt"))
        remote.write_bytes(b"user edited")
        result = sync_to_folder(self.manager, [self.child["id"]], "Google Drive", verify=True)
        self.assertEqual(len(result["conflicts"]), 1)
        self.assertEqual(remote.read_bytes(), b"user edited")

    def test_confirmation_timeout_does_not_remember_a_decline(self):
        session = "timeout_choice_session_abcdefghijkl"
        self.broker.connect(session)
        self.broker.confirm_download = lambda info: {"allowed": False, "remember": False}
        self.manager.settings["large_file_mib"] = 1
        with ThreadPoolExecutor() as executor:
            future = executor.submit(self.broker.call, "download_url", {"url": self.saved["source_url"]}, 5)
            job = self.broker.next(session, timeout=1)["job"]
            self.assertFalse(self.broker.decide_download(job["arguments"]["download_token"], "file.pdf", 2*1024*1024)["allowed"])
            self.broker.finish(session, job["id"], {"skipped": True})
            future.result()
        self.assertIsNone(self.manager.lookup_download_choice(self.saved["source_url"], 2*1024*1024))

    def test_unvalidated_large_file_uses_visible_cooldown_and_manual_update_bypasses_it(self):
        source = self.store.root / "attachments" / "synthetic-large.pdf"
        with source.open("wb") as output:
            output.write(b"%PDF synthetic")
            output.truncate(21*1024*1024)
        cached = dict(self.saved, download_path=str(source), bytes=source.stat().st_size, sha256=file_hash(source), checked_at=time.time())
        cached.pop("etag", None)
        cached.pop("last_modified", None)
        url = self.saved["source_url"]
        class Broker:
            requests = 0
            def status(self):
                return {"connected": True}
            def call(self, operation, arguments):
                if operation == "collect":
                    return {"capture": {"title": "课程", "source_url": "https://classroom.google.com/c/course1", "text": "正文", "coverage": "rendered_collection", "captured_at": utc_now(), "warnings": [], "links": [{"title": "课件", "url": url}]}}
                if operation == "download_url":
                    self.requests += 1
                    return {"not_modified": True}
                raise AssertionError(operation)
        broker = Broker()
        collector = CourseCollector(self.store, broker)
        collector.cache[resource_key(url)] = cached
        def run(**settings):
            task_id = collector.start(1, **settings)["task_id"]
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                task = collector.status(task_id)
                if task["status"] != "running":
                    return task
                time.sleep(.01)
            self.fail("collector did not finish")
        task = run()
        self.assertEqual(broker.requests, 0)
        self.assertIn("freshness_warning", task["downloads"][0])
        self.assertTrue(task["freshness_warnings"])
        forced = run(force_download=True)
        self.assertEqual(broker.requests, 1)
        self.assertEqual(task["downloads"][0]["freshness_warning"], forced["downloads"][0]["freshness_warning"])
        run(recheck_seconds=0)
        self.assertEqual(broker.requests, 2)

    def test_cloud_client_timestamp_normalization_does_not_fork_snapshot(self):
        self.ready()
        cloud = self.cloud()
        sync_to_folder(self.manager, [self.child["id"]], "Google Drive")
        original = next(cloud.rglob("*-课件.txt"))
        stamp = original.stat()
        os.utime(original, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 10000000))
        result = sync_to_folder(self.manager, [self.child["id"]], "Google Drive")
        self.assertEqual(result["unchanged"], 1)
        self.assertEqual(result["conflicts"], [])
        self.assertEqual(len(list(cloud.rglob("manifest.json"))), 1)
