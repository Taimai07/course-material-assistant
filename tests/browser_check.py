"""Chromium extractor and static UI check. Chrome APIs mocked; no account access."""
import json
from pathlib import Path
import tempfile
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main():
    fixture = """<!doctype html><html><head><meta charset="utf-8"><title>河流生态 · 大课题</title></head><body>
    <nav>导航文字</nav><main><h1>课题要求</h1><p id="excerpt">提交一份河流生态文献综述，引用至少十篇论文。</p>
    <a href="https://drive.google.com/open?id=source1&amp;authuser=2">参考资料</a>
    <a href="https://drive.google.com/open?id=source1">重复附件</a>
    <a href="https://attacker.example/?secret=private">不收集外站</a>
    <a href="javascript:alert(1)">忽略脚本</a></main></body></html>"""
    with tempfile.TemporaryDirectory() as profile, sync_playwright() as p:
        context = p.chromium.launch_persistent_context(profile, executable_path="/usr/bin/chromium", headless=True,
            args=["--no-sandbox"])
        try:
            context.route("https://classroom.google.com/**", lambda route: route.fulfill(status=200, content_type="text/html", body=fixture))
            context.route("https://docs.google.com/**", lambda route: route.fulfill(status=200, content_type="text/html", body=fixture))
            page = context.new_page()
            page.goto("https://classroom.google.com/c/course1?authuser=2&cjc=private-join-code")
            capture = (ROOT / "extension/capture.js").read_text(encoding="utf-8")
            page.evaluate(capture)
            snapshot = page.evaluate("capturePage(false)")
            assert "至少十篇" in snapshot["text"]
            assert "导航文字" not in snapshot["text"]
            assert snapshot["source_url"] == "https://classroom.google.com/c/course1"
            assert snapshot["links"] == [{"title": "重复附件", "url": "https://drive.google.com/file/d/source1/view"}]
            assert snapshot["coverage"] == "rendered_partial"
            page.evaluate("""() => {const range=document.createRange();range.selectNodeContents(document.getElementById('excerpt'));getSelection().removeAllRanges();getSelection().addRange(range)}""")
            excerpt = page.evaluate("capturePage(true)")
            assert excerpt["coverage"] == "selected_excerpt"
            assert excerpt["text"].startswith("提交一份")
            page.goto("https://docs.google.com/document/d/abc/edit")
            page.evaluate(capture)
            docs = page.evaluate("capturePage(false)")
            assert docs["coverage"] == "document_ui_only"
            assert any("画布" in warning for warning in docs["warnings"])
            # This environment's administrator blocks unpacked extensions. Do not
            # bypass it: test the static UI with a narrow Chrome API test double.
            popup = context.new_page()
            # Serve only the four authored UI assets as an in-memory test site.
            # No file:// access, network request, or browser policy changes.
            assets = {name: (ROOT / "extension" / name).read_text(encoding="utf-8") for name in ("popup.html", "popup.css", "popup.js", "capture.js")}
            def ui_route(route):
                name = route.request.url.rsplit("/", 1)[-1]
                if name not in assets:
                    route.fulfill(status=404, body="not found")
                    return
                mime = "text/html" if name.endswith(".html") else "text/css" if name.endswith(".css") else "text/javascript"
                route.fulfill(status=200, content_type=mime + "; charset=utf-8", body=assets[name])
            context.route("http://127.0.0.1:47822/**", ui_route)
            seed = json.dumps([snapshot, excerpt], ensure_ascii=False)
            popup.add_init_script("""(() => {
                const state = {items: SEED};
                const extensionId='jckpicphffjcopnehjiolplnhbmikgla';
                window.createdHelperTabs=[];window.pairRequests=0;
                const originalFetch=window.fetch.bind(window);
                window.fetch=async(url,options)=>{
                  if(url==='http://127.0.0.1:47821/client/pair'){
                    if(options?.headers?.['X-Course-Extension']!==extensionId)throw new Error('Missing extension identity');
                    window.pairRequests++;
                    return new Response(JSON.stringify({token:'t'.repeat(48)}),{status:200,headers:{'Content-Type':'application/json'}});
                  }
                  return originalFetch(url,options);
                };
                window.chrome = {
                  runtime:{id:extensionId,getURL:path=>'chrome-extension://'+extensionId+'/'+path},
                  permissions:{request:async()=>true},
                  tabs:{query:async()=>[],create:async({url})=>{window.createdHelperTabs.push(url)},update:async()=>{}},
                  storage: {local: {
                    get: async key => ({[key]: state[key]}),
                    set: async data => Object.assign(state,data),
                    remove: async key => {delete state[key]}, setAccessLevel: async () => {}
                  }},
                  downloads: {download: async ({url,filename}) => {
                    const a=document.createElement('a');a.href=url;a.download=filename;
                    document.body.append(a);a.click();a.remove();return 1;
                  }}
                };
            })();""".replace("SEED", seed))
            errors = []
            popup.on("pageerror", lambda error: errors.append(str(error)))
            popup.goto("http://127.0.0.1:47822/popup.html")
            popup.wait_for_function("document.getElementById('count').textContent === '2 条'")
            popup.locator("#manual > summary").click()
            with popup.expect_download() as pending:
                popup.locator("#markdown").click()
            download = pending.value
            exported = Path(download.path()).read_text(encoding="utf-8")
            assert "课题要求" in exported and "rendered_partial" in exported
            with popup.expect_download() as pending:
                popup.locator("#json").click()
            payload = json.loads(Path(pending.value.path()).read_text(encoding="utf-8"))
            assert len(payload["items"]) == 2
            popup.locator("#manual > summary").click()
            popup.locator("#connect").click()
            popup.wait_for_function("window.createdHelperTabs.length === 1")
            assert popup.evaluate("window.pairRequests") == 1
            assert popup.evaluate("window.createdHelperTabs[0]") == "chrome-extension://jckpicphffjcopnehjiolplnhbmikgla/connect.html"
            popup.locator("#connect").click()
            popup.wait_for_function("window.createdHelperTabs.length === 2")
            assert popup.evaluate("window.pairRequests") == 1  # Reuse the saved pairing token.
            popup.set_viewport_size({"width": 380, "height": 700})
            popup.screenshot(path=str(ROOT / "tests/preview.png"), full_page=True)
            assert errors == [], errors
            print("Chromium: static popup UI (mock Chrome API), Markdown/JSON exports, CJK extractor, selected excerpt, source normalization, link filtering and canvas warning OK")
            print("Unpacked extension installation is blocked by this environment's administrator. Actual extension APIs, activeTab gesture, and real school account are not verified.")
        finally:
            context.close()


if __name__ == "__main__":
    main()
