async function openHelper(){
  const {auto_connect}=await chrome.storage.local.get("auto_connect");
  if(auto_connect===false)return;
  const url=chrome.runtime.getURL("connect.html");
  const tabs=await chrome.tabs.query({});
  if(!tabs.some(tab=>tab.url===url))await chrome.tabs.create({url,active:false});
}
chrome.runtime.onStartup.addListener(()=>openHelper().catch(()=>{}));
let helper=null;
chrome.runtime.onConnect.addListener(port=>{
  if(port.name!=="course-helper")return;
  if(helper){port.postMessage({duplicate:true});return;}
  helper=port;
  port.onMessage.addListener(()=>{});
  port.onDisconnect.addListener(()=>{if(helper===port)helper=null;});
});

chrome.runtime.onInstalled.addListener(()=>{chrome.alarms.create("keep-helper",{periodInMinutes:1});openHelper().catch(()=>{});});
chrome.alarms.onAlarm.addListener(alarm=>{if(alarm.name==="keep-helper")openHelper().catch(()=>{});});
chrome.alarms.create("keep-helper",{periodInMinutes:1});
