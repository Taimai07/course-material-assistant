import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
from app import make_handler, call_tool, import_file
from google_api import GoogleClient, SameOriginRedirect, extract_text
from store import Store, utc_now
from http.server import ThreadingHTTPServer


def item(text="研究要求：城市河流与生态", coverage="rendered_partial", url="https://classroom.google.com/c/abc"):
    return {"title": "大课题", "source_url": url, "text": text, "coverage": coverage,
            "captured_at": utc_now(), "links": [], "warnings": ["部分采集"]}


def zip_data(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    return output.getvalue()


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)

    def test_store_updates_snapshots_and_preserves_distinct_excerpts(self):
        a = self.store.put_many([item("旧正文")])[0]
        b = self.store.put_many([item("新正文")])[0]
        self.assertEqual(a, b)
        self.store.put_many([item("摘录一", "selected_excerpt"), item("摘录二", "selected_excerpt")])
        self.assertEqual(self.store.status()["saved_materials"], 3)
        self.assertEqual(self.store.read(a)["text"], "新正文")

    def test_batch_validation_is_atomic(self):
        with self.assertRaises(ValueError):
            self.store.put_many([item(), item(url="https://attacker.example/material")])
        self.assertEqual(self.store.status()["saved_materials"], 0)

    def test_search_literals_cjk_and_read_pagination(self):
        material_id = self.store.put_many([item("生态%_" + "长" * 24001)])[0]
        self.assertEqual(len(self.store.search("生态%_")["items"]), 1)
        self.assertEqual(self.store.search("not-in-source")["items"], [])
        offset, chunks = 0, []
        while offset is not None:
            result = self.store.read(material_id, offset, 12000)
            chunks.append(result["text"])
            offset = result["next_offset"]
        self.assertEqual("".join(chunks), "生态%_" + "长" * 24001)
        self.assertEqual(result["source_url"], item()["source_url"])
        self.assertEqual(result["coverage"], "rendered_partial")

    def test_import_bundle_and_utf8_attachment(self):
        path = Path(self.temp.name) / "bundle.json"
        path.write_text(json.dumps({"items": [item()]}))
        self.assertEqual(import_file(self.store, path)["saved"], 1)
        path = Path(self.temp.name) / "文献.txt"
        path.write_text("全文\n第二段", encoding="utf-8")
        result = import_file(self.store, path, "https://drive.google.com/file/d/abc/view")
        self.assertEqual(self.store.read(result["ids"][0])["text"], "全文\n第二段")

    def test_ooxml_extractors(self):
        docx = zip_data({"word/document.xml": '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>生态研究</w:t></w:r></w:p><w:p><w:r><w:t>文献综述</w:t></w:r></w:p></w:body></w:document>'})
        self.assertEqual(extract_text(docx, "x.docx")[0], "生态研究\n文献综述")
        slide = lambda text: '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><a:t>' + text + '</a:t></p:sld>'
        pptx = zip_data({"ppt/slides/slide10.xml": slide("十"), "ppt/slides/slide2.xml": slide("二")})
        text, warnings = extract_text(pptx, "x.pptx")
        self.assertLess(text.index("二"), text.index("十"))
        self.assertTrue(warnings)
        ns = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
        xlsx = zip_data({
            "xl/workbook.xml": f'<workbook xmlns="{ns}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="研究数据" r:id="r1"/><sheet name="对照组" r:id="r2"/></sheets></workbook>',
            "xl/_rels/workbook.xml.rels": '<Relationships><Relationship Id="r1" Target="worksheets/sheet1.xml"/><Relationship Id="r2" Target="/xl/worksheets/sheet2.xml"/></Relationships>',
            "xl/sharedStrings.xml": f'<sst xmlns="{ns}"><si><t>河流</t></si></sst>',
            "xl/worksheets/sheet1.xml": f'<worksheet xmlns="{ns}"><sheetData><row><c r="A1" t="s"><v>0</v></c><c r="C1"><v>42</v></c></row></sheetData></worksheet>',
            "xl/worksheets/sheet2.xml": f'<worksheet xmlns="{ns}"><sheetData><row><c r="B2" t="inlineStr"><is><t>湖泊</t></is></c></row></sheetData></worksheet>',
        })
        text, _ = extract_text(xlsx, "x.xlsx")
        for expected in ("研究数据", "对照组", "A1=河流", "C1=42", "B2=湖泊"):
            self.assertIn(expected, text)

    def test_pdf_warns_about_missing_ocr(self):
        try:
            from pypdf import PdfWriter
        except ImportError:
            self.skipTest("pypdf optional dependency not installed")
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        output = io.BytesIO()
        writer.write(output)
        text, warnings = extract_text(output.getvalue(), "scan.pdf")
        self.assertIn("第 1 页", text)
        self.assertTrue(any("OCR" in warning for warning in warnings))

    def test_google_absent_and_invalid_arguments(self):
        google = GoogleClient(self.store.root)
        with self.assertRaisesRegex(ValueError, "尚未"):
            google.list_courses()
        for name, args in (("read_material", {}), ("search_materials", {"offset": -1}), ("search_drive", {"delete": True})):
            with self.assertRaises(ValueError):
                call_tool(self.store, google, name, args)

    def test_google_sync_all_pages_and_partial_errors(self):
        class FixtureGoogle(GoogleClient):
            def get(self, base, path, params=None):
                if path.endswith("/course1"):
                    return {"id": "course1", "name": "河流生态", "alternateLink": "https://classroom.google.com/c/course1"}
                if path.endswith("/announcements"):
                    suffix = "2" if params.get("pageToken") else "1"
                    return {"announcements": [{"id": suffix, "text": "公告" + suffix, "alternateLink": "https://classroom.google.com/c/course1/a/" + suffix}], **({"nextPageToken": "page2"} if suffix == "1" else {})}
                if path.endswith("/courseWork"):
                    return {"courseWork": [{"id": "work1", "title": "综述", "description": "完成文献综述", "alternateLink": "https://classroom.google.com/c/course1/a/work1", "materials": [{"driveFile": {"driveFile": {"id": "file1", "title": "参考文献", "alternateLink": "https://drive.google.com/file/d/file1/view"}}}]}]}
                raise ValueError("Google API HTTP 403 PERMISSION_DENIED")
        result = FixtureGoogle(self.store.root).sync_course("course1", self.store)
        self.assertEqual(result["saved"], 4)
        self.assertTrue(result["partial"])
        self.assertEqual(result["errors"][0]["collection"], "courseWorkMaterials")
        saved = self.store.search("文献综述")["items"][0]
        self.assertEqual(self.store.read(saved["id"])["links"][0]["title"], "参考文献")

    def test_no_cross_origin_credential_redirect(self):
        with self.assertRaises(ValueError):
            SameOriginRedirect().redirect_request(Request("https://www.googleapis.com/drive/v3/files/id", headers={"Authorization": "Bearer secret"}), None, 302, "", {}, "https://attacker.example/file")

    def test_receiver_pairing_origin_host_and_atomic_import(self):
        token = "local-test-credential-only"
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.store, token))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_port}/ingest"

        def post(headers=None, body=None):
            request = Request(url, data=json.dumps(body or {"items": [item()]}).encode(), headers={"Content-Type": "application/json", **(headers or {})})
            return urlopen(request, timeout=5)

        for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": "Bearer " + token, "Origin": "https://attacker.example"}, {"Authorization": "Bearer " + token, "Host": "attacker.example"}):
            with self.assertRaises(HTTPError) as caught:
                post(headers)
            self.assertEqual(caught.exception.code, 403)
        headers = {"Authorization": "Bearer " + token, "Origin": "chrome-extension://jckpicphffjcopnehjiolplnhbmikgla"}
        with post(headers) as response:
            self.assertEqual(json.load(response)["saved"], 1)
            self.assertEqual(response.headers["Access-Control-Allow-Origin"], headers["Origin"])
        with self.assertRaises(HTTPError) as caught:
            post(headers, {"items": [item(url="https://evil.example/")]})
        self.assertEqual(caught.exception.code, 400)

        pair_url = url.removesuffix("/ingest") + "/client/pair"
        # Privileged extension GETs can omit Origin. Require the pinned extension header.
        request = Request(pair_url, headers={"X-Course-Extension": "jckpicphffjcopnehjiolplnhbmikgla"})
        with urlopen(request, timeout=5) as response:
            self.assertEqual(json.load(response)["token"], token)
        for pair_headers in ({}, {"Origin": "https://attacker.example", "X-Course-Extension": "jckpicphffjcopnehjiolplnhbmikgla"}):
            with self.assertRaises(HTTPError) as caught:
                urlopen(Request(pair_url, headers=pair_headers), timeout=5)
            self.assertEqual(caught.exception.code, 403)

    def test_stdio_real_process(self):
        material_id = self.store.put_many([item()])[0]
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "read_material", "arguments": {"material_id": material_id}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "list_courses", "arguments": {}}},
        ]
        result = subprocess.run([sys.executable, str(ROOT / "server/app.py"), "mcp"],
                                input="\n".join(json.dumps(m) for m in messages) + "\n", text=True,
                                capture_output=True, env={**os.environ, "COURSE_BRIDGE_DATA": self.temp.name}, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(replies), 4)
        self.assertEqual(len(replies[1]["result"]["tools"]), 15)
        self.assertIn("城市河流", replies[2]["result"]["content"][0]["text"])
        self.assertTrue(replies[3]["result"]["isError"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
