import base64
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from http.server import ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "server"))
from app import make_handler, call_tool
from browser_rpc import BrowserBroker, BrowserClient, save_download, read_native_download
from collector import CourseCollector, resource_key, inherit_account
from store import Store, utc_now
from google_api import GoogleClient, write_private
from research import export_task


def capture(url, links):
    return {"title": "河流生态", "source_url": url, "text": "文献综述要求", "coverage": "rendered_collection", "captured_at": utc_now(), "links": [{"title": "参考资料", "url": u} for u in links], "warnings": ["渲染资料可能缺失"]}


def downloaded(source="https://drive.google.com/file/d/file1/view"):
    return {"filename": "文献.txt", "source_url": source, "mime": "text/plain", "data_base64": base64.b64encode("河流生态研究全文".encode()).decode()}


class BrowserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)

    def test_broker_roundtrip_session_scope_and_expiry(self):
        broker = BrowserBroker()
        with self.assertRaises(ValueError):
            broker.call("tabs", {})
        session = "session1_abcdefghijklmnop"
        broker.connect(session)
        with self.assertRaises(ValueError):
            broker.connect("session2_abcdefghijklmnop")
        with ThreadPoolExecutor() as executor:
            future = executor.submit(broker.call, "tabs", {}, 2)
            job = broker.next(session, timeout=1)["job"]
            broker.finish(session, job["id"], {"tabs": [{"tab_id": 7}]})
            self.assertEqual(future.result()["tabs"][0]["tab_id"], 7)
        with self.assertRaises(ValueError):
            broker.call("tabs", {}, timeout=.01)
        self.assertIsNone(broker.next(session, timeout=.01)["job"])
        with self.assertRaises(ValueError):
            broker.finish(session, job["id"], {"tabs": []})

    def test_disconnect_cancels_pending_command(self):
        broker = BrowserBroker()
        session = "session1_abcdefghijklmnop"
        broker.connect(session)
        with ThreadPoolExecutor() as executor:
            future = executor.submit(broker.call, "read", {"tab_id": 1}, 2)
            broker.next(session, timeout=1)
            broker.disconnect(session)
            with self.assertRaisesRegex(ValueError, "断开"):
                future.result()

    def test_http_command_and_attachment_saved_without_manual_upload(self):
        token = "synthetic-local-pairing-only"
        write_private(self.store.root / "pairing.json", {"token": token})
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.store, token))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client = BrowserClient(self.store.root, server.server_port)
        session = "session1_abcdefghijklmnop"
        client.post("/browser/connect", {"session_id": session})
        with ThreadPoolExecutor() as executor:
            future = executor.submit(client.call, "download", {"tab_id": 7})
            job = client.post("/browser/next", {"session_id": session})["job"]
            self.assertEqual(job["operation"], "download")
            client.post("/browser/result", {"session_id": session, "job_id": job["id"], "result": downloaded()})
            result = save_download(self.store, future.result())
        self.assertTrue(Path(result["download_path"]).is_file())
        self.assertEqual(self.store.read(result["material_id"])["text"], "河流生态研究全文")

    def test_interrupted_cached_file_is_repaired_atomically(self):
        result = downloaded()
        saved = save_download(self.store, result)
        path = Path(saved["download_path"])
        original = path.read_bytes()
        path.write_bytes(original[:3])
        save_download(self.store, result)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(path.parent.glob("*.part")), [])

    def test_account_is_carried_to_folders_assignments_and_attachments(self):
        self.assertIn("authuser=2", inherit_account("https://drive.google.com/drive/folders/folder1?resourcekey=keep", "https://classroom.google.com/u/2/c/course1"))
        self.assertIn("resourcekey=keep", inherit_account("https://drive.google.com/drive/folders/folder1?resourcekey=keep", "https://classroom.google.com/u/2/c/course1"))
        explicit = "https://drive.google.com/file/d/file1/view?usp=classroom_web&authuser=0"
        self.assertEqual(inherit_account(explicit, "https://classroom.google.com/u/2/c/course1"), explicit)
        self.assertIn("authuser=3", inherit_account("https://drive.google.com/file/u/3/d/file1/view", "https://classroom.google.com/u/2/c/course1"))
        self.assertIn("authuser=0", inherit_account("https://classroom.google.com/w/course1/t/all", "https://classroom.google.com/u/0/c/course1"))

    def test_native_download_bytes_are_imported_before_acknowledgement(self):
        broker = BrowserBroker()
        session = "native_session_abcdefghijklmnop"
        broker.connect(session)
        original = "浏览器正常下载的研究正文".encode()
        with ThreadPoolExecutor() as executor:
            future = executor.submit(broker.call, "download_url", {"url": "https://drive.google.com/file/d/file1/view?authuser=0", "download_token": "caller-cannot-choose"}, 3)
            job = broker.next(session, timeout=1)["job"]
            token = job["arguments"]["download_token"]
            self.assertRegex(token, r"^[a-f0-9]{32}$")
            path = self.store.root / "custom Chrome downloads" / "CourseMaterialAssistant" / token / "paper.txt"
            path.parent.mkdir(parents=True)
            path.write_bytes(original)
            result = {"filename": "paper.txt", "mime": "text/plain", "source_url": job["arguments"]["url"],
                      "native_download": {"id": 9, "path": str(path), "token": token}}
            self.assertTrue(broker.finish(session, job["id"], result)["accepted"])
            path.unlink()  # Extension removes only its own temporary file after the acknowledgement.
            received = future.result()
        self.assertNotIn("native_download", received)
        self.assertEqual(base64.b64decode(received["data_base64"]), original)
        saved = save_download(self.store, received)
        self.assertEqual(self.store.read(saved["material_id"])["text"], original.decode())

    def test_native_import_rejects_external_files_wrong_job_and_html(self):
        token = "a" * 32
        outside = self.store.root / "private-settings.json"
        outside.write_text("must not be read", encoding="utf-8")
        result = {"filename": "paper.pdf", "mime": "application/pdf", "source_url": "https://drive.google.com/file/d/file1/view",
                  "native_download": {"id": 9, "path": str(outside), "token": token}}
        with self.assertRaisesRegex(ValueError, "专用目录"):
            read_native_download(result, token)
        with self.assertRaisesRegex(ValueError, "不属于"):
            read_native_download(result, "b" * 32)
        path = self.store.root / "CourseMaterialAssistant" / token / "paper.pdf"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"<!doctype html><html>permission page</html>")
        result["native_download"]["path"] = str(path)
        with self.assertRaisesRegex(ValueError, "网页"):
            read_native_download(result, token)
        path.write_bytes(b"")
        with self.assertRaisesRegex(ValueError, "为空"):
            read_native_download(result, token)
        with path.open("wb") as output:
            output.truncate(20 * 1024 * 1024 + 1)
        with self.assertRaisesRegex(ValueError, "20 MiB"):
            read_native_download(result, token)

    def test_download_rejects_invalid_data_and_preserves_unparsed_binary(self):
        result = downloaded()
        result["data_base64"] = "invalid!"
        with self.assertRaises(ValueError):
            save_download(self.store, result)
        result = downloaded()
        result.update(filename="../../image.bin", mime="application/octet-stream")
        saved = save_download(self.store, result)
        self.assertTrue(saved["downloaded"])
        self.assertFalse(saved["text_extracted"])
        self.assertTrue(Path(saved["download_path"]).is_relative_to(self.store.root / "attachments"))

    def test_script_exports_original_and_full_text_with_sources(self):
        saved = save_download(self.store, downloaded())
        task = {"pages": [], "downloads": [saved], "status": "completed"}
        destination = self.store.root / "export"
        export_task(self.store, task, destination)
        text = next(destination.glob("*.md")).read_text(encoding="utf-8")
        self.assertIn("河流生态研究全文", text)
        self.assertIn("https://drive.google.com/file/d/file1/view", text)
        self.assertTrue((destination / (saved["sha256"][:10] + "-文献.txt")).is_file())
        self.assertEqual(json.loads((destination / "manifest.json").read_text(encoding="utf-8"))["status"], "completed")

    def test_model_status_is_compact_and_detail_is_paginated(self):
        task = {"task_id": "a" * 24, "status": "completed", "coverage_complete": False, "note": "渲染范围",
                "pages": [{"title": "课题", "material_id": "page1"}],
                "downloads": [{"filename": "paper.pdf", "material_id": "file1", "sha256": "omit-this-hash"}], "errors": []}
        with patch("app.BrowserClient") as client:
            client.return_value.post.return_value = task
            summary = call_tool(self.store, GoogleClient(self.store.root), "course_collection_status", {"task_id": "a" * 24})
            self.assertNotIn("items", summary)
            self.assertEqual(summary["root_material_id"], "page1")
            self.assertEqual(summary["files_downloaded"], 1)
            detail = call_tool(self.store, GoogleClient(self.store.root), "course_collection_status", {"task_id": "a" * 24, "mode": "results", "limit": 1})
            self.assertEqual(detail["next_offset"], 1)
            self.assertEqual(detail["items"][0]["kind"], "page")

    def test_assignment_collection_does_not_follow_sibling_or_course_home(self):
        root_url = "https://classroom.google.com/c/course1/a/work1/details"
        class Broker:
            def status(self):
                return {"connected": True}
            def call(self, operation, args, timeout=80):
                if operation == "collect":
                    return {"capture": capture(root_url, ["https://classroom.google.com/c/course1/a/work2/details", "https://classroom.google.com/c/course1"])}
                raise AssertionError("Sibling navigation must not occur: " + operation)
        collector = CourseCollector(self.store, Broker())
        started = collector.start(1)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            task = collector.status(started["task_id"])
            if task["status"] != "running":
                break
            time.sleep(.01)
        self.assertEqual(task["status"], "completed")
        self.assertEqual([p["source_url"] for p in task["pages"]], [root_url])

    def test_script_traverses_folder_deduplicates_and_downloads_without_model(self):
        class FixtureBroker:
            def __init__(self):
                self.next_tab = 10
                self.urls = {1: "https://classroom.google.com/c/course1"}
                self.downloads = []
            def status(self):
                return {"connected": True}
            def call(self, operation, args, timeout=80):
                if operation == "open":
                    self.next_tab += 1
                    self.urls[self.next_tab] = args["url"]
                    return {"tab_id": self.next_tab}
                if operation == "close":
                    return {"closed": True}
                if operation == "download_url":
                    self.downloads.append(args["url"])
                    return downloaded(args["url"])
                url = self.urls[args["tab_id"]]
                links = ["https://drive.google.com/file/d/file1/view", "https://docs.google.com/document/d/file1/edit", "https://drive.google.com/drive/folders/folder1", "https://classroom.google.com/c/othercourse"] if url.endswith("course1") else ["https://drive.google.com/open?id=file2", "https://drive.google.com/open?id=file1"]
                return {"capture": capture(url, links), "scans": 4}
        broker = FixtureBroker()
        collector = CourseCollector(self.store, broker)
        started = collector.start(1, max_pages=5, max_files=10, max_scrolls=5)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            task = collector.status(started["task_id"])
            if task["status"] != "running":
                break
            time.sleep(.01)
        self.assertEqual(task["status"], "completed")
        self.assertEqual(len(task["pages"]), 2)
        self.assertEqual(len(task["downloads"]), 2)
        self.assertFalse(task["coverage_complete"])
        self.assertEqual(resource_key("https://drive.google.com/open?id=file1&authuser=2"), resource_key("https://docs.google.com/document/d/file1/edit"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
