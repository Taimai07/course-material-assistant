import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "server"))
from automation import analyze_bundle, selected_parts
from bundle import export_task
from store import Store, utc_now
from workflow import run_workflow


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "private-data")
        self.text = "河流生态研究：引用原始文献。\n" * 5000
        self.material_id = self.store.put_many([{"title": "课题要求", "source_url": "https://classroom.google.com/c/course1",
            "text": self.text, "coverage": "rendered_collection", "captured_at": utc_now(), "links": [], "warnings": ["尚有未展开内容"]}])[0]
        self.task = {"task_id": "a" * 24, "status": "completed_with_gaps", "pages": [{"material_id": self.material_id}], "downloads": [],
                     "errors": [{"stage": "extraction", "error": str(self.store.root) + "/missing.pdf"}], "settings": {"tab_id": 17}}
        self.destination = Path(self.temp.name) / "bundle"

    def test_manual_bundle_is_lossless_bounded_and_portable(self):
        result = export_task(self.store, self.task, self.destination, part_chars=8000)
        manifest = json.loads((self.destination / "manifest.json").read_text(encoding="utf-8"))
        pieces = []
        self.assertGreater(result["parts"], 1)
        for part in manifest["ai_parts"]:
            text = (self.destination / part["path"]).read_text(encoding="utf-8")
            self.assertLessEqual(len(text), 8000)
            self.assertIn("https://classroom.google.com/c/course1", text)
            pieces.append(re.split(r"正文片段：\d+/\d+\n\n", text, maxsplit=1)[1])
        self.assertEqual("".join(pieces), self.text)
        self.assertNotIn(str(self.store.root), json.dumps(manifest))
        with zipfile.ZipFile(result["ai_archive"]) as archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual(set(archive.namelist()), {"ANALYSIS-PROMPT.txt", "START-HERE.txt", "manifest.json"} | {p["path"] for p in manifest["ai_parts"]})
        with self.assertRaises(ValueError):
            export_task(self.store, self.task, self.destination)

    def test_collection_and_bundle_work_without_an_ai_command(self):
        class FakeClient:
            def post(_, path, args):
                return {"task_id": "a" * 24} if path.endswith("start") else self.task
        with patch("subprocess.run", side_effect=AssertionError("the collector must not invoke AI")):
            result = run_workflow(FakeClient(), self.store, 17, output=self.destination, poll=lambda _: None)
        self.assertTrue(Path(result["ai_archive"]).is_file())

    def test_optional_ai_has_budget_guard_and_manual_fallback(self):
        export_task(self.store, self.task, self.destination)
        with self.assertRaisesRegex(ValueError, "上限"):
            selected_parts(self.destination, max_chars=10)
        with self.assertRaisesRegex(ValueError, "未安装"):
            analyze_bundle(self.destination, max_chars=500000, find=lambda _: None)
        self.assertTrue((self.destination / "ai-materials.zip").exists())

    def test_optional_adapter_uses_readonly_cli_and_selected_data(self):
        export_task(self.store, self.task, self.destination)
        calls = []
        class Result:
            returncode = 0
            stdout = "--sandbox --skip-git-repo-check --output-last-message --ignore-user-config"
        def runner(command, **kwargs):
            calls.append(command)
            if command[-1] != "--help":
                self.assertIn("read-only", command)
                self.assertIn("--ignore-user-config", command)
                self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)
                workspace = Path(command[command.index("--cd") + 1])
                self.assertTrue((workspace / "for-ai").is_dir())
                self.assertFalse((workspace / "pairing.json").exists())
                Path(command[command.index("--output-last-message") + 1]).write_text("课题分析报告", encoding="utf-8")
            return Result()
        report = analyze_bundle(self.destination, max_chars=500000, runner=runner, find=lambda _: "/test/codex")
        self.assertIn("课题分析报告", report.read_text(encoding="utf-8"))
        self.assertEqual(len(calls), 2)
        with self.assertRaisesRegex(ValueError, "已有分析报告"):
            analyze_bundle(self.destination, max_chars=500000, runner=runner, find=lambda _: "/test/codex")

    def test_ai_adapter_rejects_external_bundle_paths(self):
        export_task(self.store, self.task, self.destination)
        path = self.destination / "manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["ai_parts"][0]["path"] = "../../pairing.json"
        path.write_text(json.dumps(manifest))
        with self.assertRaises(ValueError):
            selected_parts(self.destination)


if __name__ == "__main__":
    unittest.main(verbosity=2)
