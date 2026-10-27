const connection=document.getElementById("connection"),log=document.getElementById("log");
const sessionId=crypto.randomUUID();
const helperPort=chrome.runtime.connect({name:"course-helper"});
helperPort.onMessage.addListener(async message=>{if(message.duplicate){const tab=await chrome.tabs.getCurrent();if(tab)await chrome.tabs.remove(tab.id);}});
setInterval(()=>{try{helperPort.postMessage({alive:true});}catch{}},10000);
let stopped=false,token="",running=false,localReady=false,relayUrl="";
const origins=["http://127.0.0.1/*","https://classroom.google.com/*","https://drive.google.com/*","https://docs.google.com/*","https://drive.usercontent.google.com/*","https://*.googleusercontent.com/*"];
const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
CourseBrowserAgent.onProgress=message=>addLog(message);
CourseBrowserAgent.decideDownload=information=>localRequest("/downloads/decision",information,330000);
CourseBrowserAgent.reportDownload=information=>localRequest("/downloads/progress",information);
async function localRequest(path,payload,timeout=30000) {
  const response=await fetch("http://127.0.0.1:47821"+path,{method:payload===undefined ? "GET" : "POST",headers:{"Content-Type":"application/json",Authorization:`Bearer ${token}`,"X-Course-Extension":chrome.runtime.id},...(payload===undefined ? {} : {body:JSON.stringify(payload)}),signal:AbortSignal.timeout(timeout)});
  const result=await response.json();if(!response.ok)throw new Error(result.error || `连接失败 (${response.status})`);return result;
}
function addLog(message){const li=document.createElement("li");li.textContent=`${new Date().toLocaleTimeString()} · ${message}`;log.prepend(li);while(log.children.length>40)log.lastElementChild.remove();}
async function pair(){const result=await localRequest("/client/pair");token=result.token;await chrome.storage.local.set({token,auto_connect:true});}
async function cleanupOwnDownloads(){
  for(const item of await chrome.downloads.search({state:"complete"})){
    if(item.byExtensionId===chrome.runtime.id && /[\\/]CourseMaterialAssistant[\\/][a-f0-9]{32}[\\/][^\\/]+$/.test(item.filename || "") && Date.now()-Date.parse(item.endTime || item.startTime)>86400000){
      try{await chrome.downloads.removeFile(item.id);}catch{}
      try{await chrome.downloads.erase({id:item.id});}catch{}
    }
  }
}
async function begin(){
  if(running)return;
  if(!await chrome.permissions.contains({origins})){connection.textContent="首次使用，请点击允许访问课程。";document.getElementById("grant").hidden=false;return;}
  running=true;stopped=false;document.getElementById("grant").hidden=true;document.getElementById("retry").hidden=true;
  let heartbeat=null;
  while(!stopped){
    try{
      ({token=""}=await chrome.storage.local.get("token"));if(!token)await pair();
      try{await localRequest("/browser/connect",{session_id:sessionId,extension_version:chrome.runtime.getManifest().version});}catch{await pair();await localRequest("/browser/connect",{session_id:sessionId,extension_version:chrome.runtime.getManifest().version});}
      const info=await localRequest("/client/info");
      if(info.version!==chrome.runtime.getManifest().version){connection.textContent="桌面助手已更新，正在重新加载浏览器扩展…";await delay(500);chrome.runtime.reload();return;}
      localReady=true;connection.textContent=`已连接本机助手 v${info.version} · 自动发现课程和课题，并在后台更新`;connection.className="online";
      heartbeat=setInterval(()=>localRequest("/browser/connect",{session_id:sessionId,extension_version:chrome.runtime.getManifest().version}).catch(()=>{}),10000);
      while(!stopped){
        const {job}=await localRequest("/browser/next",{session_id:sessionId,extension_version:chrome.runtime.getManifest().version});if(!job||stopped)continue;
        addLog(job.operation === "download_url" ? "检查并下载课题附件" : job.operation === "collect" ? "脚本读取与展开课程" : "更新课题资料");
        let result;try{result=await CourseBrowserAgent.execute(job);}catch(error){result={error:error.message};addLog("待处理："+error.message);}
        try{
          await localRequest("/browser/result",{session_id:sessionId,job_id:job.id,result},1800000);
          if(result.native_download)addLog("Chrome 已完成附件下载，本机已接收文件内容");
        }catch(error){
          addLog("本任务归档未完成："+error.message+"；继续连接并处理后续任务");
        }finally{
          if(result.native_download){
            // Remove only this command's own temporary download, including failed archive attempts.
            // A failed import is reported and can be retried from the original Google source.
            try{await chrome.downloads.removeFile(result.native_download.id);}catch{}
            try{await chrome.downloads.erase({id:result.native_download.id});}catch{}
          }
        }

      }
    }catch(error){if(!stopped){connection.textContent="等待本机助手 · "+error.message;connection.className="";}}
    finally{localReady=false;if(heartbeat)clearInterval(heartbeat);heartbeat=null;}
    if(!stopped)await delay(5000);
  }
  running=false;
}
async function cloudLoop(){
  let deviceId=(await chrome.storage.local.get("device_id")).device_id;
  if(!deviceId){deviceId=crypto.randomUUID();await chrome.storage.local.set({device_id:deviceId});}
  while(true){
    let cloud="not_configured";
    if(localReady&&!stopped){
      try{
        const info=await localRequest("/client/info");relayUrl=info.relay_url || "";
        if(relayUrl){
          if(!await chrome.permissions.contains({origins:[relayUrl+"/*"]})){cloud="offline";document.getElementById("grant-cloud").hidden=false;}
          else{
            const response=await fetch(relayUrl+"/device/poll",{method:"POST",credentials:"include",headers:{"Content-Type":"application/json"},body:JSON.stringify({device_id:deviceId}),signal:AbortSignal.timeout(20000)});
            if(!response.ok||!response.headers.get("Content-Type")?.includes("application/json")){cloud="login_required";}
            else{
              const {job}=await response.json();cloud="connected";
              if(job){
                let result;try{result=await localRequest("/projects/command",{operation:job.operation,arguments:job.arguments});}catch(error){result={error:error.message};}
                await fetch(relayUrl+"/device/result",{method:"POST",credentials:"include",headers:{"Content-Type":"application/json"},body:JSON.stringify({device_id:deviceId,job_id:job.id,result}),signal:AbortSignal.timeout(20000)});
              }
            }
          }
        }
        await localRequest("/client/report",{cloud});
      }catch{cloud="offline";}
    }
    document.getElementById("cloud-status").textContent=({connected:"聊天通道在线",login_required:"聊天连接需要登录：请在助手中打开专属连接",offline:"聊天通道暂未连接",not_configured:"普通本地模式 · 不需要 AI"})[cloud];
    await delay(cloud === "connected" ? 1500 : 5000);
  }
}
document.getElementById("grant").onclick=async()=>{if(await chrome.permissions.request({origins}))begin();};
document.getElementById("grant-cloud").onclick=async()=>{if(relayUrl&&await chrome.permissions.request({origins:[relayUrl+"/*"]})){document.getElementById("grant-cloud").hidden=true;const existing=(await chrome.tabs.query({})).find(tab=>tab.url?.startsWith(relayUrl));if(existing)await chrome.tabs.update(existing.id,{active:true});else await chrome.tabs.create({url:relayUrl});}};
document.getElementById("stop").onclick=async()=>{stopped=true;await chrome.storage.local.set({auto_connect:false});try{await localRequest("/browser/disconnect",{session_id:sessionId,extension_version:chrome.runtime.getManifest().version});}catch{}connection.textContent="已断开 · 自动同步等待浏览器重新连接";document.getElementById("retry").hidden=false;};
document.getElementById("retry").onclick=async()=>{await chrome.storage.local.set({auto_connect:true});if(!running)begin();};
window.addEventListener("pagehide",()=>{stopped=true;fetch("http://127.0.0.1:47821/browser/disconnect",{method:"POST",headers:{"Content-Type":"application/json",Authorization:`Bearer ${token}`,"X-Course-Extension":chrome.runtime.id},body:JSON.stringify({session_id:sessionId,extension_version:chrome.runtime.getManifest().version}),keepalive:true}).catch(()=>{});});
cleanupOwnDownloads().catch(()=>{});begin();cloudLoop();
