"""Script/DOM check in Chromium using synthetic content and mocked Chrome APIs."""
import base64
from pathlib import Path
import tempfile
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main():
    fixture = '''<!doctype html><meta charset="utf-8"><title>研究课程</title><main style="height:250px;overflow:auto">
    <h1>河流生态研究</h1><button aria-expanded="false" id="more" onclick="this.setAttribute('aria-expanded','true');document.getElementById('details').hidden=false">展开</button>
    <p id="details" hidden>要求引用十篇文献</p><a id="file" href="https://drive.google.com/file/d/file1/view">课件.pdf</a>
    <button id="danger">Submit</button><a href="https://outside.example/">外部链接</a>
    <div style="height:500px">参考资料列表</div><p>页面末尾</p></main>'''
    with tempfile.TemporaryDirectory() as profile, sync_playwright() as p:
        context = p.chromium.launch_persistent_context(profile, executable_path="/usr/bin/chromium", headless=True, args=["--no-sandbox"])
        try:
            context.route("https://classroom.google.com/**", lambda route: route.fulfill(content_type="text/html; charset=utf-8", body=fixture))
            context.route("https://drive.google.com/**", lambda route: route.fulfill(content_type="text/html; charset=utf-8", body='''<!doctype html><meta charset="utf-8"><main><div role="row" data-id="folder123" data-mime-type="application/vnd.google-apps.folder" aria-label="文献 文件夹">文献</div><div role="row" data-id="file12345" data-mime-type="application/pdf" aria-label="论文.pdf">论文.pdf</div></main>'''))
            page = context.new_page()
            page.goto("https://classroom.google.com/c/course1")
            page.evaluate((ROOT / "extension/agent.js").read_text(encoding="utf-8") + "\nwindow.testAgent=CourseBrowserAgent;")
            view = page.evaluate("inspectCoursePage({})")
            danger = next(c for c in view["controls"] if c["label"] == "Submit")
            assert page.evaluate("id => {try{resolveCourseTarget(id,true);return false}catch{return true}}", danger["target_id"])
            target = next(c for c in view["controls"] if c["label"] == "课件.pdf")
            page.evaluate("document.getElementById('file').textContent='已变化'")
            assert page.evaluate("id => {try{resolveCourseTarget(id,false);return false}catch{return true}}", target["target_id"])
            plan = page.evaluate("planCourseDownload('https://docs.google.com/spreadsheets/u/1/d/sheet1/edit?authuser=1')")
            assert "/spreadsheets/u/1/d/sheet1/export?" in plan["url"]
            assert "format=xlsx" in plan["url"] and "authuser=1" in plan["url"]
            assert page.evaluate("isCoursePage('https://evil.example/')") is False
            assert page.evaluate("isCoursePage('https://drive.google.com@evil.example/')") is False
            assert page.evaluate("isDownloadHost('https://a.googleusercontent.com/f')") is True
            page.evaluate("() => {testAgent.inject=async (_tab,func,args=[])=>func(...args);}")
            result = page.evaluate("testAgent.collect({id:1},5)")
            assert "要求引用十篇文献" in result["capture"]["text"]
            assert result["scans"] >= 2
            assert result["capture"]["coverage"] == "rendered_collection"
            page.evaluate('''() => {
              window.fetch=async url=>({ok:true,status:200,url,
                headers:new Headers({'Content-Type':'text/plain','Content-Disposition':'attachment; filename="paper.txt"'}),
                body:new ReadableStream({start(controller){controller.enqueue(new TextEncoder().encode('研究正文'));controller.close();}})});
            }''')
            file = page.evaluate("testAgent.download({url:'https://drive.google.com/file/d/file1/view',title:'文献'}, {source_url:'https://classroom.google.com/u/1/c/course1'})")
            assert base64.b64decode(file["data_base64"]).decode() == "研究正文"
            assert file["filename"] == "paper.txt"
            page.evaluate("window.fetch=async url=>({ok:true,status:200,url,headers:new Headers({'Content-Type':'text/html'})})")
            assert page.evaluate("async () => {try{await testAgent.download({url:'https://drive.google.com/file/d/file1/view'},{});return false}catch(error){return error.message.includes('尚未下载')}}")
            # Reproduce a 403 on direct fetch, then verify browser-native download fallback.
            assert "authuser=2" in page.evaluate("planCourseDownload('https://drive.google.com/file/u/2/d/file1/view').url")
            assert page.evaluate("withCourseAccount('https://drive.google.com/file/d/file1/view?authuser=0','https://classroom.google.com/u/2/c/course1')").endswith("authuser=0")
            page.evaluate("""() => {
              window.nativeRequests=[];
              window.fetch=async url=>({ok:false,status:403,url,headers:new Headers()});
              window.chrome={downloads:{
                download:async options=>{nativeRequests.push(options);return 17;},
                search:async()=>[{id:17,state:'complete',filename:'/custom/Downloads/CourseMaterialAssistant/'+'a'.repeat(32)+'/paper.pdf',mime:'application/pdf',url:nativeRequests[0].url,finalUrl:'https://drive.usercontent.google.com/download?id=file1&authuser='+new URL(nativeRequests[nativeRequests.length-1].url).searchParams.get('authuser')}],
                cancel:async()=>{},removeFile:async()=>{},erase:async()=>{}
              }};
            }""")
            result = page.evaluate("testAgent.download({url:'https://drive.google.com/file/d/file1/view?usp=classroom_web&authuser=0',title:'paper.pdf'},{download_token:'a'.repeat(32)})")
            assert result["native_download"]["id"] == 17
            assert result["download_mode"] == "chrome_downloads"
            assert "authuser=0" in page.evaluate("nativeRequests[0].url")
            assert page.evaluate("nativeRequests[0].filename").startswith("CourseMaterialAssistant/" + "a" * 32 + "/")
            assert page.evaluate("nativeRequests[0].saveAs") is False
            # Inherit the account before opening the Drive viewer; keep explicit account zero.
            page.evaluate("""() => {
              window.viewerOpened=[];
              chrome.tabs={create:async options=>{viewerOpened.push(options);return {id:81};},remove:async()=>{}};
              testAgent.settle=async()=>({id:81,url:viewerOpened[0].url,title:'paper.pdf'});
            }""")
            result = page.evaluate("testAgent.execute({operation:'download_url',arguments:{url:'https://drive.google.com/file/d/file1/view',source_url:'https://classroom.google.com/u/2/c/course1',download_token:'a'.repeat(32)}})")
            assert "authuser=2" in page.evaluate("viewerOpened[0].url")
            assert result["source_url"].endswith("authuser=2")
            assert page.evaluate("safeCourseFilename('CON.pdf')") == "_CON.pdf"
            assert page.evaluate("safeCourseFilename('slides\\u202efdp.exe')") == "slides_fdp.exe"
            confirmation = '<form id="download-form" action="https://drive.usercontent.google.com/download" method="get"><input type="hidden" name="id" value="file1"><input type="hidden" name="confirm" value="t"><input type="hidden" name="uuid" value="synthetic-token"></form>'
            valid = page.evaluate("async html => courseDownloadConfirmation({url:'https://drive.google.com/uc?id=file1',body:new Response(html).body},{url:'https://drive.google.com/uc?id=file1&authuser=2'})", confirmation)
            assert valid.startswith("https://drive.usercontent.google.com/download?") and "authuser=2" in valid
            assert page.evaluate("async html => courseDownloadConfirmation({url:'https://drive.google.com/uc?id=file1',body:new Response(html).body},{url:'https://drive.google.com/uc?id=file1'})", confirmation.replace("file1", "other-file")) is None
            assert page.evaluate("async html => courseDownloadConfirmation({url:'https://drive.google.com/uc?id=file1',body:new Response(html).body},{url:'https://drive.google.com/uc?id=file1'})", confirmation.replace("drive.usercontent.google.com", "evil.example")) is None
            page.evaluate("""() => {
              window.progress=[];testAgent.reportDownload=async info=>progress.push(info);
              window.fetch=async url=>({ok:true,status:200,url,headers:new Headers({'Content-Type':'application/pdf','Content-Length':String(21*1024*1024),'ETag':'"large-v1"','Content-Disposition':'attachment; filename="original-name.pdf"'}),body:new ReadableStream({start(c){c.close();}})});
            }""")
            result = page.evaluate("testAgent.download({url:'https://drive.google.com/file/d/file1/view?authuser=0',title:'preview title'},{download_token:'a'.repeat(32)})")
            assert result["filename"] == "original-name.pdf"
            assert result["etag"] == '"large-v1"'
            assert page.evaluate("progress.length") > 0
            # Real Google warning form is parsed once; explicit rejection must not start a download.
            count = page.evaluate("nativeRequests.length")
            page.evaluate("html => {window.fetch=async url=>({ok:true,status:200,url,headers:new Headers({'Content-Type':'text/html'}),body:new Response(html).body});testAgent.decideDownload=async info=>({allowed:false});}", confirmation)
            result = page.evaluate("testAgent.download({url:'https://drive.google.com/file/d/file1/view?authuser=0',title:'paper'},{download_token:'a'.repeat(32)})")
            assert result["skipped"] is True and page.evaluate("nativeRequests.length") == count
            # Validators from the actual file must not be sent to or replaced by the HTML page.
            page.evaluate("html => {window.probeCalls=[];testAgent.decideDownload=async()=>{throw new Error('unchanged file must not ask')};window.fetch=async (url,options)=>{probeCalls.push({url,headers:options.headers});if(url.includes('drive.usercontent.google.com'))return {status:304,headers:new Headers()};return {ok:true,status:200,url,headers:new Headers({'Content-Type':'text/html','ETag':'html-only'}),body:new Response(html).body};};}", confirmation)
            result = page.evaluate("testAgent.download({url:'https://drive.google.com/file/d/file1/view?authuser=0',title:'paper'},{download_token:'a'.repeat(32),cache:{etag:'actual-file-v1',validator_scope:'confirmed_file'}})")
            assert result["not_modified"] is True
            assert "If-None-Match" not in page.evaluate("probeCalls[0].headers")
            assert page.evaluate("probeCalls[1].headers['If-None-Match']") == "actual-file-v1"
            page.evaluate("html => {testAgent.decideDownload=async()=>({allowed:true});window.fetch=async (url,options)=>url.includes('drive.usercontent.google.com') ? {ok:true,status:206,url,headers:new Headers({'Content-Type':'application/pdf','Content-Range':'bytes 0-0/22020096','ETag':'actual-file-v2','Content-Disposition':'attachment; filename=actual.pdf'}),body:new Response('x').body} : {ok:true,status:200,url,headers:new Headers({'Content-Type':'text/html','ETag':'html-only'}),body:new Response(html).body};}", confirmation)
            result = page.evaluate("testAgent.download({url:'https://drive.google.com/file/d/file1/view?authuser=0',title:'paper'},{download_token:'a'.repeat(32),cache:{etag:'actual-file-v1',validator_scope:'confirmed_file'}})")
            assert result["etag"] == "actual-file-v2" and result["validator_scope"] == "confirmed_file"
            assert result["filename"] == "actual.pdf"
            # Pause and resume only the current task, then preserve Office MIME extensions.
            page.evaluate("""() => {
              window.pauses=0;window.resumes=0;window.prompts=0;window.polls=0;
              window.fetch=async url=>({ok:false,status:403,url,headers:new Headers()});
              chrome.downloads.pause=async()=>{pauses++;};chrome.downloads.resume=async()=>{resumes++;};
              testAgent.decideDownload=async()=>{prompts++;return {allowed:true};};
              chrome.downloads.search=async()=>[{id:17,state:polls++ ? 'complete' : 'in_progress',bytesReceived:2*1024*1024,totalBytes:2*1024*1024,fileSize:2*1024*1024,filename:'/custom/Downloads/CourseMaterialAssistant/'+'a'.repeat(32)+'/paper',mime:'application/vnd.openxmlformats-officedocument.presentationml.presentation',finalUrl:'https://drive.usercontent.google.com/download?id=file1&authuser=0'}];
            }""")
            result = page.evaluate("testAgent.download({url:'https://drive.google.com/file/d/file1/view?authuser=0',title:'slides'},{download_token:'a'.repeat(32),download_threshold_bytes:1024*1024})")
            assert result["filename"].endswith(".pptx")
            assert page.evaluate("[pauses,resumes,prompts]") == [1,1,1]
            page.evaluate("() => {window.fetch=async url=>({ok:true,status:200,url,headers:new Headers({'Content-Type':'application/pdf','Content-Length':String(21*1024*1024),'ETag':'direct-v1'}),body:new Response('x').body});}")
            count = page.evaluate("nativeRequests.length")
            result = page.evaluate("testAgent.download({url:'https://drive.google.com/file/d/file1/view?authuser=0',title:'paper'},{download_token:'a'.repeat(32),cache:{etag:'direct-v1'}})")
            assert result["not_modified"] is True and page.evaluate("nativeRequests.length") == count
            page.evaluate("html => {window.warningSize='unset';testAgent.decideDownload=async info=>{warningSize=info.bytes;return {allowed:false}};window.fetch=async url=>{if(url.includes('drive.usercontent.google.com'))throw new Error('synthetic Range unsupported');return {ok:true,status:200,url,headers:new Headers({'Content-Type':'text/html','Content-Length':'9999'}),body:new Response(html).body};};}", confirmation)
            result = page.evaluate("testAgent.download({url:'https://drive.google.com/file/d/file1/view?authuser=0',title:'paper'},{download_token:'a'.repeat(32)})")
            assert result["skipped"] and page.evaluate("warningSize") is None
            page.goto("https://drive.google.com/drive/u/1/folders/root1")
            page.evaluate((ROOT / "extension/agent.js").read_text(encoding="utf-8"))
            drive = page.evaluate("inspectCoursePage({})")
            assert any("/drive/folders/folder123?authuser=1" in link["url"] for link in drive["capture"]["links"])
            assert any("/file/d/file12345/view?authuser=1" in link["url"] for link in drive["capture"]["links"])
            print("Chromium synthetic agent checks: DOM Drive rows/file IDs, snapshot targets, write rejection, conditional scrolling, account-aware export URL, 403-to-native-download fallback, task-scoped paths, fetch plumbing and HTML rejection OK")
            print("Chrome APIs/fetch mocked; no real extension, cookies, school account, or Google downloads verified.")
        finally:
            context.close()


if __name__ == "__main__":
    main()
