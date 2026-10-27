// Fixed browser operations exposed to the paired local MCP process.
// No eval, debugger access, arbitrary script input, passwords or cookie APIs.
const COURSE_PAGE_HOSTS = new Set(["classroom.google.com", "drive.google.com", "docs.google.com"]);
function isCoursePage(value) {
  try { const u=new URL(value);return u.protocol === "https:" && COURSE_PAGE_HOSTS.has(u.hostname) && !u.username && !u.password && (!u.port || u.port === "443"); }
  catch {return false;}
}
function isDownloadHost(value) {
  try {const u=new URL(value);return u.protocol === "https:" && !u.username && !u.password && (!u.port || u.port === "443") && (COURSE_PAGE_HOSTS.has(u.hostname) || u.hostname === "drive.usercontent.google.com" || u.hostname.endsWith(".googleusercontent.com"));}
  catch {return false;}
}

function courseAccount(value) {
  try {const u=new URL(value);return u.searchParams.get("authuser") ?? u.pathname.match(/\/u\/(\d+)(?:\/|$)/)?.[1] ?? null;}
  catch {return null;}
}

function withCourseAccount(value,parent) {
  const url=new URL(value);
  const account=courseAccount(value) ?? courseAccount(parent);
  if(account!==null && !url.searchParams.has("authuser"))url.searchParams.set("authuser",account);
  return url.href;
}

// This function is serialized by chrome.scripting into an isolated world.
function inspectCoursePage(args) {
  if (!["classroom.google.com","drive.google.com","docs.google.com"].includes(location.hostname)) throw new Error("当前标签页已经离开授权的 Google 页面。");
  const root=document.querySelector('[role="main"]') || document.querySelector("main") || document.body;
  let state=window.__courseBridgeView;
  if (args.snapshot_id) {
    if (!state || state.id !== args.snapshot_id || state.url !== location.href) throw new Error("页面快照已失效，请不带 snapshot_id 重新读取。");
  } else {
    const viewId=crypto.randomUUID();
    const targets=new Map();
    const controls=[];
    for (const el of root.querySelectorAll('a[href],button,[role="button"],[role="link"],[role="treeitem"],[role="row"]')) {
      if (!el.getClientRects().length || el.getAttribute("aria-hidden") === "true") continue;
      const label=(el.getAttribute("aria-label") || el.innerText || el.getAttribute("title") || "").trim().slice(0,300);
      const nativeHref=typeof el.href === "string" ? el.href : "";
      let href=nativeHref,driveId=null;
      if(!href && location.hostname === "drive.google.com" && el.getAttribute("role") === "row") {
        const id=el.getAttribute("data-id") || el.querySelector("[data-id]")?.getAttribute("data-id");
        if(id && /^[A-Za-z0-9_-]{8,200}$/.test(id)) {
          driveId=id;
          const mime=el.getAttribute("data-mime-type") || el.querySelector("[data-mime-type]")?.getAttribute("data-mime-type") || "";
          const metadata=[el.getAttribute("aria-label") || "",...Array.from(el.querySelectorAll("img[alt],[aria-label]")).map(node=>node.getAttribute("alt") || node.getAttribute("aria-label") || "")].join(" ");
          const folder=mime === "application/vnd.google-apps.folder" || !mime && /\bfolders?\b|文件夹|フォルダ/i.test(metadata);
          const target=new URL(folder ? `https://drive.google.com/drive/folders/${id}` : `https://drive.google.com/file/d/${id}/view`);
          const account=new URL(location.href).searchParams.get("authuser") || location.pathname.match(/\/u\/(\d+)/)?.[1];
          if(account)target.searchParams.set("authuser",account);
          href=target.href;
        }
      }
      if (!label && !href) continue;
      const targetId=`${viewId}:${controls.length}`;
      targets.set(targetId,{el,href,label,nativeHref,driveId});
      controls.push({target_id:targetId,role:el.getAttribute("role") || el.tagName.toLowerCase(),label,href:href.startsWith("https:") ? href : null,expanded:el.getAttribute("aria-expanded")});
      if (controls.length >= 5000) break;
    }
    const rawText=(root.innerText || "").trim();
    const canvas=location.hostname === "docs.google.com";
    const warnings=["当前快照仅含已渲染内容。未展开条目、分页和虚拟滚动可能缺失；请展开、滚动并重新读取。"];
    if (canvas) warnings.push("Google 文档可能用画布显示；请 browser_download 导出附件全文，不能将界面文字当作正文。");
    if (rawText.length > 500000) warnings.push("当前文字超过 500000 字符，已截断。");
    const url=new URL(location.href);url.searchParams.delete("cjc");url.hash="";
    const links=controls.filter(x => x.href).slice(0,1000).map(x => {
      let title=x.label || "附件";
      if(location.hostname === "classroom.google.com" && /\/[am]\/[\w-]+/.test(new URL(x.href).pathname)){
        let ancestor=targets.get(x.target_id).el.parentElement;
        for(let depth=0;ancestor && ancestor!==root && depth<6;depth++,ancestor=ancestor.parentElement){
          const heading=ancestor.querySelector('h2,h3,h4,[role="heading"]');
          const text=heading?.innerText?.trim();
          if(text){title=text.slice(0,300);break;}
        }
      }
      let topic="";
      if(location.hostname === "classroom.google.com" && /\/[am]\/[\w-]+/.test(new URL(x.href).pathname)){
        const anchor=targets.get(x.target_id).el;
        const container=anchor.closest("[data-topic-id]");
        topic=container?.querySelector('h2,h3,[role="heading"]')?.innerText?.trim() || "";
        if(!topic){
          for(const heading of root.querySelectorAll('h2')){
            if(heading.closest('article,[data-stream-item-id],[aria-expanded]'))continue;
            if(heading.compareDocumentPosition(anchor)&Node.DOCUMENT_POSITION_FOLLOWING)topic=heading.innerText.trim();
          }
        }
      }
      return {title,url:x.href,...(topic ? {topic:topic.slice(0,100)} : {})};
    });
    const unique=[...new Map(links.map(x => [x.url,x])).values()];
    state={id:viewId,url:location.href,targets,controls,capture:{title:document.title.slice(0,500),source_url:url.href,text:rawText.slice(0,500000),
      coverage:canvas ? "document_ui_only" : "rendered_partial",captured_at:new Date().toISOString(),links:unique,warnings}};
    window.__courseBridgeView=state;
  }
  const offset=args.text_offset || 0,limit=Math.min(args.limit || 12000,30000),controlOffset=args.controls_offset || 0;
  return {snapshot_id:state.id,title:state.capture.title,source_url:state.capture.source_url,coverage:state.capture.coverage,warnings:state.capture.warnings,
    text:state.capture.text.slice(offset,offset+limit),total_chars:state.capture.text.length,
    next_text_offset:offset+limit < state.capture.text.length ? offset+limit : null,
    controls:state.controls.slice(controlOffset,controlOffset+150),
    next_controls_offset:controlOffset+150 < state.controls.length ? controlOffset+150 : null,capture:state.capture};
}

function resolveCourseTarget(targetId, click) {
  const state=window.__courseBridgeView;
  const target=state?.targets.get(targetId);
  if (!target || state.url !== location.href || !target.el.isConnected) throw new Error("页面目标已失效，请重新 browser_read。");
  const {el,href,label,nativeHref,driveId}=target;
  const current=(el.getAttribute("aria-label") || el.innerText || el.getAttribute("title") || "").trim().slice(0,300);
  const actualHref=typeof el.href === "string" ? el.href : "";
  const actualId=el.getAttribute("data-id") || el.querySelector("[data-id]")?.getAttribute("data-id");
  if (actualHref !== nativeHref || current !== label || driveId && actualId !== driveId) throw new Error("目标内容已变化，请重新读取页面。");
  if (click) {
    const forbidden=/^(submit|turn in|unsubmit|mark as done|delete|remove|post|publish|send|share|invite|create|upload|rename|move to trash|提交|交作业|撤回提交|标记为已完成|删除|发布|发送|共享|分享|邀请|上传|重命名|提出|送信|削除)(\b|\s|$|[：:])/i;
    const writeLabel=/^(提交|交作业|撤回提交|标记为已完成|删除|发布|发送|共享|分享|邀请|上传|重命名|提出|送信|削除)/.test(label);
    if (forbidden.test(label) || writeLabel || (el.form && el.type === "submit")) throw new Error("此工具仅用于读取资料，不执行提交、发布、删除或分享。");
    if (href && !href.startsWith("#")) {
      let u;
      try {u=new URL(href,location.href);} catch {throw new Error("此链接无法导航。");}
      if (u.protocol !== "https:" || !["classroom.google.com","drive.google.com","docs.google.com"].includes(u.hostname)) throw new Error("此目标不是已授权的 Google 课程/网盘页面。");
    }
    if(driveId)location.assign(href);else{el.scrollIntoView({block:"center"});el.click();}
    return {clicked:true,label};
  }
  return {href,label,source_url:state.capture.source_url,title:document.title};
}

function scrollCoursePage(amount) {
  const root=document.querySelector('[role="main"]') || document.querySelector("main") || document.body;
  const nodes=[root,...root.querySelectorAll("div,section")];
  const candidates=nodes.filter(el => {const style=getComputedStyle(el);return el.getClientRects().length && el.scrollHeight > el.clientHeight+30 && /auto|scroll/.test(style.overflowY);});
  const scroller=candidates.sort((a,b) => b.clientHeight*b.clientWidth-a.clientHeight*a.clientWidth)[0] || document.scrollingElement;
  scroller.scrollBy({top:amount,behavior:"instant"});
  window.__courseBridgeView=null;
  return {scrolled:amount !== 0,scroll_top:scroller.scrollTop,scroll_height:scroller.scrollHeight,viewport_height:scroller.clientHeight,
    can_scroll:scroller.scrollHeight>scroller.clientHeight+5};
}

function expandCourseSections() {
  let expanded=0;
  const root=document.querySelector('[role="main"]') || document.querySelector("main") || document.body;
  for(const el of root.querySelectorAll('button,[role="button"],[aria-expanded="false"]')) {
    if(!el.isConnected || !el.getClientRects().length || el.getAttribute("aria-haspopup") || el.tagName === "A" || el.form)continue;
    const label=(el.getAttribute("aria-label") || el.innerText || "").trim();
    const more=/^(show more|read more|view more|load more|expand|查看更多|显示更多|加载更多|展开|もっと見る|すべて表示)$/i.test(label);
    if(el.getAttribute("aria-expanded") === "false" || more){el.click();expanded++;if(expanded>=40)break;}
  }
  return {expanded};
}

function planCourseDownload(source, requestedFormat="") {
  if (!isDownloadHost(source)) throw new Error("仅支持当前授权 Google 页面中的附件下载。");
  const u=new URL(source);
  const doc=u.hostname === "docs.google.com" && u.pathname.match(/\/(document|spreadsheets|presentation)(?:\/u\/\d+)?\/d\/([A-Za-z0-9_-]+)/);
  if (doc) {
    const formats={document:["docx","pdf","txt"],spreadsheets:["xlsx","pdf","csv"],presentation:["pptx","pdf","txt"]};
    const format=requestedFormat || formats[doc[1]][0];
    if (!formats[doc[1]].includes(format)) throw new Error("此 Google 文档不支持该导出格式。");
    u.pathname=u.pathname.slice(0,doc.index+doc[0].length)+"/export";
    const params=new URLSearchParams();params.set("format",format);
    for (const key of ["authuser","resourcekey"]) if(u.searchParams.has(key))params.set(key,u.searchParams.get(key));
    u.search=params.toString();u.hash="";
    return {url:u.href,extension:format};
  }
  if (u.hostname === "drive.google.com") {
    const match=u.pathname.match(/\/file(?:\/u\/\d+)?\/d\/([A-Za-z0-9_-]+)/);
    const id=match?.[1] || u.searchParams.get("id");
    if (!id || !/^[A-Za-z0-9_-]+$/.test(id)) throw new Error("当前是 Drive 列表或文件夹，请先打开或选择一个文件链接。");
    const params=new URLSearchParams({export:"download",id});
    for (const key of ["authuser","resourcekey"]) if(u.searchParams.has(key))params.set(key,u.searchParams.get(key));
    const account=courseAccount(source);
    if(account!==null)params.set("authuser",account);
    return {url:`https://drive.google.com/uc?${params}`,extension:""};
  }
  if (u.hostname === "classroom.google.com") throw new Error("请先从课程页面打开具体 Drive 或文档附件。");
  return {url:u.href,extension:""};
}

function safeCourseFilename(value) {
  let name=String(value || "attachment").replace(/[\x00-\x1f\x7f<>:"/\\|?*\u200b-\u200f\u202a-\u202e\u2066-\u2069]/g,"_").slice(0,160).replace(/^[. ]+|[. ]+$/g,"") || "attachment";
  if(/^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)/i.test(name))name="_"+name;
  return name;
}
const COURSE_MIME_EXTENSIONS={"application/pdf":"pdf","image/png":"png","image/jpeg":"jpg","image/webp":"webp","image/gif":"gif","image/tiff":"tif","text/plain":"txt","text/csv":"csv","application/zip":"zip","application/x-zip-compressed":"zip","application/msword":"doc","application/vnd.ms-excel":"xls","application/vnd.ms-powerpoint":"ppt","application/vnd.openxmlformats-officedocument.wordprocessingml.document":"docx","application/vnd.openxmlformats-officedocument.presentationml.presentation":"pptx","application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":"xlsx","video/mp4":"mp4","video/quicktime":"mov","audio/mpeg":"mp3","audio/mp4":"m4a","audio/wav":"wav"};
function courseResponseMetadata(response) {
  const disposition=response.headers.get("Content-Disposition") || "";
  let filename="";
  const encoded=disposition.match(/filename\*=UTF-8''([^;]+)/i);
  if(encoded){try{filename=decodeURIComponent(encoded[1]);}catch{}}
  if(!filename)filename=disposition.match(/filename="([^"]+)"/i)?.[1] || disposition.match(/filename=([^;]+)/i)?.[1]?.trim() || "";
  return {filename:filename ? safeCourseFilename(filename) : "",etag:response.headers.get("ETag"),last_modified:response.headers.get("Last-Modified")};
}
async function courseDownloadConfirmation(response, plan) {
  // Parse one bounded, real Google confirmation form. Never execute returned HTML/scripts.
  const reader=response.body?.getReader();if(!reader)return null;
  const chunks=[];let total=0;
  try {while(true){const {done,value}=await reader.read();if(done)break;total+=value.length;if(total>256*1024)return null;chunks.push(value);}}
  finally{try{await reader.cancel();}catch{}}
  const bytes=new Uint8Array(total);let offset=0;for(const chunk of chunks){bytes.set(chunk,offset);offset+=chunk.length;}
  const doc=new DOMParser().parseFromString(new TextDecoder().decode(bytes),"text/html");
  const form=doc.querySelector('form#download-form');if(!form || (form.getAttribute("method") || "get").toLowerCase()!=="get")return null;
  const url=new URL(form.getAttribute("action") || "",response.url);
  if(url.protocol!=="https:" || url.username || url.password || url.port || !["drive.google.com","drive.usercontent.google.com"].includes(url.hostname) || !["/download","/uc"].includes(url.pathname))return null;
  const expected=new URL(plan.url).searchParams.get("id");if(!expected)return null;
  const fields=new URLSearchParams(url.search);
  const allowed=new Set(["id","export","confirm","uuid","at","authuser","resourcekey"]);
  for(const input of form.querySelectorAll('input[type="hidden"][name]')){
    if(!allowed.has(input.name) || input.value.length>2048 || fields.has(input.name) && fields.get(input.name)!==input.value)return null;
    fields.set(input.name,input.value);
  }
  if([...fields.keys()].some(key=>!allowed.has(key)) || fields.get("id")!==expected || !fields.get("confirm"))return null;
  const original=new URL(plan.url);
  for(const key of ["authuser","resourcekey"]){if(original.searchParams.has(key))fields.set(key,original.searchParams.get(key));else fields.delete(key);}
  fields.set("export","download");url.search=fields.toString();url.hash="";
  return url.href;
}

const CourseBrowserAgent={
  ownedTabs:new Set(),
  async tab(tabId) {
    if (!Number.isInteger(tabId)) throw new Error("无效的标签页编号。");
    const tab=await chrome.tabs.get(tabId);
    if (!isCoursePage(tab.url)) throw new Error("此标签页不在授权 Google 页面中，或浏览器权限尚未授予。");
    return tab;
  },
  async inject(tabId,func,args=[]) {
    await this.tab(tabId);
    const [result]=await chrome.scripting.executeScript({target:{tabId},func,args});
    if (!result || result.error) throw new Error(result?.error?.message || "无法读取页面，请确认已登录且页面已加载。");
    return result.result;
  },
  async settle(tabId) {
    for(let i=0;i<40;i++) {
      const tab=await chrome.tabs.get(tabId);
      if (tab.status === "complete") return tab;
      await new Promise(resolve=>setTimeout(resolve,250));
    }
    return chrome.tabs.get(tabId);
  },
  async execute(job) {
    const args=job.arguments || {};
    if (job.operation === "tabs") {
      return {tabs:(await chrome.tabs.query({})).filter(t=>isCoursePage(t.url)).map(t=>({tab_id:t.id,title:t.title,url:t.url,active:t.active}))};
    }
    if (job.operation === "open") {
      if (!isCoursePage(args.url)) throw new Error("只允许打开 Classroom、Drive 和 Docs 的 HTTPS 页面。");
      if(args.tab_id !== undefined) await this.tab(args.tab_id);
      const tab=args.tab_id === undefined ? await chrome.tabs.create({url:args.url,active:false}) : await chrome.tabs.update(args.tab_id,{url:args.url});
      if(args.tab_id === undefined)this.ownedTabs.add(tab.id);
      await this.settle(tab.id);
      return {tab_id:tab.id,url:(await chrome.tabs.get(tab.id)).url};
    }
    if(job.operation === "close") {
      if(!this.ownedTabs.has(args.tab_id))throw new Error("只能关闭脚本自己打开的临时标签页。");
      await chrome.tabs.remove(args.tab_id);this.ownedTabs.delete(args.tab_id);return {closed:true};
    }
    if(job.operation === "download_url") {
      if(!isCoursePage(args.url))throw new Error("只下载脚本从课程页面中发现的 Google 文件。");
      // Resolve the viewer with the course's account BEFORE navigating, not afterwards.
      const source=withCourseAccount(args.url,args.source_url || args.url);
      // A Drive /file/d/ link may actually be a native Google document. Resolve
      // its viewer using the logged-in browser before choosing download/export.
      if(new URL(source).hostname === "drive.google.com") {
        const opened=await chrome.tabs.create({url:source,active:false});this.ownedTabs.add(opened.id);
        try {
          const resolved=await this.settle(opened.id);
          if(!isCoursePage(resolved.url))throw new Error("附件跳转至登录或未授权页面，请先在 Chrome 确认学校账号。");
          const expected=courseAccount(source),actual=courseAccount(resolved.url);
          if(/^\d+$/.test(expected || "") && /^\d+$/.test(actual || "") && expected!==actual)throw new Error("附件预览跳转到了另一个 Google 账号，已停止下载，请在学校账号所在窗口核对文件。");
          return await this.download(resolved,{source_url:source,cache:args.cache,download_token:args.download_token,download_threshold_bytes:args.download_threshold_bytes,title:args.title});
        }finally{await chrome.tabs.remove(opened.id);this.ownedTabs.delete(opened.id);}
      }
      return this.download({url:source,title:args.title || "attachment"},{source_url:source,cache:args.cache,download_token:args.download_token,download_threshold_bytes:args.download_threshold_bytes});
    }
    const tab=await this.tab(args.tab_id);
    if(job.operation === "collect")return this.collect(tab,args.max_scrolls || 20);
    if (job.operation === "read") return this.inject(tab.id,inspectCoursePage,[args]);
    if (job.operation === "scroll") {
      const result=await this.inject(tab.id,scrollCoursePage,[args.amount ?? 700]);
      await new Promise(resolve=>setTimeout(resolve,500));return result;
    }
    if (job.operation === "click") {
      const result=await this.inject(tab.id,resolveCourseTarget,[args.target_id,true]);
      await new Promise(resolve=>setTimeout(resolve,500));
      await this.settle(tab.id);
      return {...result,tab_id:tab.id,url:(await chrome.tabs.get(tab.id)).url,next_step:"重新 browser_read；若打开新标签页，用 browser_tabs 查看。"};
    }
    if (job.operation === "download") return this.download(tab,args);
    throw new Error("不支持的浏览器操作。");
  },
  async collect(tab,maxScrolls) {
    const seen=new Set(),links=new Map(),pieces=[];let latest=null,stable=0,previousText="",scans=0,ended=false;
    for(let i=0;i<=Math.min(maxScrolls,30);i++) {
      for(let pass=0;pass<2;pass++) {
        const {expanded}=await this.inject(tab.id,expandCourseSections);
        if(!expanded)break;await new Promise(resolve=>setTimeout(resolve,250));
      }
      const snapshot=await this.inject(tab.id,inspectCoursePage,[{}]);latest=snapshot.capture;scans++;
      const text=latest.text,previousCount=links.size;
      for(const link of latest.links)links.set(link.url,link);
      if(!seen.has(text)) {
        seen.add(text);
        const addition=previousText && text.startsWith(previousText) ? text.slice(previousText.length) : text;
        if(addition.trim())pieces.push(addition);
      }
      const unchanged=text === previousText && links.size === previousCount;
      previousText=text;
      const geometry=await this.inject(tab.id,scrollCoursePage,[0]);
      const atEnd=geometry.scroll_top+geometry.viewport_height>=geometry.scroll_height-5;
      stable=unchanged && atEnd ? stable+1 : 0;
      // Read DOM first. Static pages stop after one unchanged verification;
      // scrolling is used only when content actually extends past the viewport.
      if(stable>=(geometry.can_scroll ? 2 : 1)){ended=true;break;}
      if(geometry.can_scroll && !atEnd)await this.inject(tab.id,scrollCoursePage,[Math.max(300,Math.round(geometry.viewport_height*.8))]);
      await new Promise(resolve=>setTimeout(resolve,350));
    }
    const text=pieces.join("\n\n");
    latest.text=text.slice(0,500000);latest.links=[...links.values()].slice(0,1000);
    latest.coverage=latest.coverage === "document_ui_only" ? "document_ui_only" : "rendered_collection";
    latest.warnings.push("已通过脚本展开和滚动去重采集；静止页面末尾不保证覆盖隐藏资料或全部课程。");
    if(!ended)latest.warnings.push("已达到本次滚动次数上限，可能还有内容未加载。");
    if(text.length>500000)latest.warnings.push("累计文字超过 500000 字符，已截断。");
    if(links.size>1000)latest.warnings.push("发现超过 1000 个链接，仅保存前 1000 个。");
    return {capture:latest,scans,reached_stable_end:ended};
  },
  async nativeDownload(plan,selected,args,previousError) {
    if(!/^[a-f0-9]{32}$/.test(args.download_token || ""))throw previousError;
    if(!isDownloadHost(plan.url))throw new Error("下载地址不在授权 Google 主机内。");
    let filename=safeCourseFilename(args.response_metadata?.filename || (args.title || selected.label || "attachment").replace(/\s[-–—]\sGoogle Drive$/i,""));
    if(plan.extension && !filename.toLowerCase().endsWith("."+plan.extension))filename+="."+plan.extension;
    let id;
    try{id=await chrome.downloads.download({url:plan.url,filename:`CourseMaterialAssistant/${args.download_token}/${filename}`,conflictAction:"uniquify",saveAs:false});}
    catch(error){throw new Error("脚本下载未取得文件，Chrome 普通下载也未启动："+error.message);}
    let lastActivity=Date.now(), lastReceived=-1, lastReport=0;
    let complete=false, approved=args.approved_size || 0;
    const threshold=args.download_threshold_bytes ?? 50*1024*1024;
    try{
      while(Date.now()-lastActivity<120000){
        const [item]=await chrome.downloads.search({id});
        if(!item)throw new Error("Chrome 未返回当前附件的下载记录。");
        const received=Number(item.bytesReceived || 0);
        if(received>lastReceived){
          lastReceived=received;lastActivity=Date.now();
          if(Date.now()-lastReport>5000){await this.reportDownload?.({token:args.download_token,bytes:received});lastReport=Date.now();}
        }
        const size=item.fileSize>0 ? item.fileSize : item.totalBytes>0 ? item.totalBytes : null;
        if((size!==null && size>Math.max(threshold,approved)) || (size===null && item.bytesReceived>Math.max(threshold,approved))){
          if(item.state!=="complete"){try{await chrome.downloads.pause(id);}catch{}}
          this.onProgress?.("文件超过询问阈值，正在等待你在助手窗口确认");
          const decision=this.decideDownload ? await this.decideDownload({token:args.download_token,filename,bytes:size}) : {allowed:false};
          lastActivity=Date.now();
          if(!decision.allowed)return {skipped:true,reason:"用户未同意本次大文件下载",filename};
          approved=size===null ? Infinity : size;
          if(item.state!=="complete"){try{await chrome.downloads.resume(id);}catch{}}
        }
        if(item.state==="interrupted")throw new Error("Chrome 普通下载未完成："+(item.error || "下载中断")+"。网页能手动下载时，请保留此错误以检查下载方式。");
        if(item.state==="complete"){
          if(!isDownloadHost(item.finalUrl || item.url || plan.url))throw new Error("Chrome 下载跳转至未授权主机，未导入文件。");
          const expected=courseAccount(plan.url),actual=courseAccount(item.finalUrl || item.url || plan.url);
          if(/^\d+$/.test(expected || "") && /^\d+$/.test(actual || "") && expected!==actual)throw new Error("下载跳转到了另一个 Google 账号，未导入该文件。");
          const mime=(item.mime || "application/octet-stream").split(";")[0];
          if(mime==="text/html" || mime==="application/xhtml+xml")throw new Error("Chrome 返回的是网页，尚未下载附件。");
          const extension=plan.extension || COURSE_MIME_EXTENSIONS[mime];
          if(extension && !filename.toLowerCase().endsWith("."+extension))filename+="."+extension;
          complete=true;
          return {etag:args.response_metadata?.etag,last_modified:args.response_metadata?.last_modified,validator_scope:args.response_metadata?.validator_scope,filename,mime,source_url:selected.href,native_download:{id,path:item.filename,token:args.download_token},download_mode:"chrome_downloads"};
        }
        await new Promise(resolve=>setTimeout(resolve,100));
      }
      throw new Error("Chrome 下载连续两分钟没有进度；请检查下载列表和网络后重试。");
    }finally{
      if(!complete){
        try{await chrome.downloads.cancel(id);}catch{}
        try{await chrome.downloads.removeFile(id);}catch{}
        try{await chrome.downloads.erase({id});}catch{}
      }
    }
  },
  async download(tab,args) {
    const selected=args.target_id ? await this.inject(tab.id,resolveCourseTarget,[args.target_id,false]) : {href:tab.url,label:tab.title,source_url:args.source_url || tab.url};
    if(!selected.href)throw new Error("此按钮没有直接附件链接；请先打开文件再下载。");
    selected.href=withCourseAccount(selected.href,selected.source_url || tab.url);
    let plan=planCourseDownload(selected.href,args.format);
    const validators={};
    if(args.cache?.etag)validators["If-None-Match"]=args.cache.etag;
    else if(args.cache?.last_modified)validators["If-Modified-Since"]=args.cache.last_modified;
    const headers=args.cache?.validator_scope==="confirmed_file" ? {} : validators;
    let response;
    try{response=await fetch(plan.url,{credentials:"include",redirect:"follow",headers,signal:AbortSignal.timeout(60000)});}
    catch(error){return this.nativeDownload(plan,selected,args,error);}
    if(response.status===304)return {not_modified:true};
    if(!response.ok || !isDownloadHost(response.url)){
      try{await response.body?.cancel();}catch{}
      return this.nativeDownload(plan,selected,args,new Error(`脚本直接下载 HTTP ${response.status}，不能据此判断账号没有权限。`));
    }
    let mime=(response.headers.get("Content-Type") || "application/octet-stream").split(";")[0];
    let size=Number(response.headers.get("Content-Length")) || null;
    if(mime==="text/html" || mime==="application/xhtml+xml"){
      size=null;
      const confirmed=await courseDownloadConfirmation(response,plan);
      if(!confirmed)return this.nativeDownload(plan,selected,args,new Error("返回的是登录、权限或确认页面，尚未下载附件。"));
      // HTML validators belong to the confirmation page, NEVER to the actual file.
      args={...args,response_metadata:{}};
      let probe;
      try{
        probe=await fetch(confirmed,{credentials:"include",redirect:"follow",headers:{...validators,Range:"bytes=0-0"},signal:AbortSignal.timeout(60000)});
        if(probe.status===304)return {not_modified:true};
        const probeMime=(probe.headers.get("Content-Type") || "").split(";")[0];
        if(probe.ok && isDownloadHost(probe.url) && probeMime && !["text/html","application/xhtml+xml"].includes(probeMime)){
          args.response_metadata={...courseResponseMetadata(probe),validator_scope:"confirmed_file"};
          const range=probe.headers.get("Content-Range")?.match(/\/(\d+)$/);
          size=range ? Number(range[1]) : probe.status===200 ? Number(probe.headers.get("Content-Length")) || null : null;
          if(args.cache?.etag && args.response_metadata.etag===args.cache.etag)return {not_modified:true};
        }
      }catch{}finally{try{await probe?.body?.cancel();}catch{}}
      const decision=this.decideDownload ? await this.decideDownload({token:args.download_token,filename:args.title || selected.label || "附件",bytes:size,reason:"google_scan_warning"}) : {allowed:false};
      if(!decision.allowed)return {skipped:true,reason:"未确认 Google 无法扫描的文件；超时不记住拒绝"};
      plan={...plan,url:confirmed};
      if(size>(args.download_threshold_bytes ?? 50*1024*1024)){
        const large=this.decideDownload ? await this.decideDownload({token:args.download_token,filename:args.response_metadata.filename || selected.label || "附件",bytes:size}) : {allowed:false};
        if(!large.allowed)return {skipped:true,reason:"未确认大文件下载；超时不记住拒绝"};
        args.approved_size=size;
      }
      return this.nativeDownload(plan,selected,args,new Error("Google 确认后的原文件下载"));
    }
    args={...args,response_metadata:courseResponseMetadata(response)};
    if(args.cache?.etag && args.response_metadata.etag===args.cache.etag){
      try{await response.body?.cancel();}catch{}
      return {not_modified:true};
    }
    if(size>(args.download_threshold_bytes ?? 50*1024*1024)){
      let decision;
      try{decision=this.decideDownload ? await this.decideDownload({token:args.download_token,filename:args.response_metadata.filename || args.title || selected.label || "附件",bytes:size}) : {allowed:false};}
      catch(error){try{await response.body?.cancel();}catch{}throw error;}
      if(!decision.allowed){try{await response.body?.cancel();}catch{}return {skipped:true,reason:"未确认大文件下载；超时不记住拒绝"};}
      args.approved_size=size;
    }
    if(size>20*1024*1024){
      try{await response.body?.cancel();}catch{}
      return this.nativeDownload(plan,selected,args,new Error("原文件转入本机流式下载"));
    }
    try{
      const reader=response.body.getReader();let total=0;const chunks=[];
      while(true){const {done,value}=await reader.read();if(done)break;total+=value.length;if(total>20*1024*1024 || total>(args.download_threshold_bytes ?? 50*1024*1024) && !size){await reader.cancel();throw new Error("原文件转入本机流式下载");}chunks.push(value);}
      if(!total)throw new Error("附件下载结果为空。");
      const bytes=new Uint8Array(total);let offset=0;for(const chunk of chunks){bytes.set(chunk,offset);offset+=chunk.length;}
      let filename=safeCourseFilename(args.response_metadata.filename || selected.label || tab.title || "attachment");
      const extension=plan.extension || COURSE_MIME_EXTENSIONS[mime];
      if(extension && !filename.toLowerCase().endsWith("."+extension))filename+="."+extension;
      let binary="";for(let start=0;start<bytes.length;start+=16384)binary+=String.fromCharCode(...bytes.subarray(start,start+16384));
      return {filename,mime,source_url:selected.href,data_base64:btoa(binary),etag:args.response_metadata.etag,last_modified:args.response_metadata.last_modified};
    }catch(error){
      try{await response.body?.cancel();}catch{}
      return this.nativeDownload(plan,selected,args,error);
    }
  }
};
