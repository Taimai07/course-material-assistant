// Public installer distribution only. This bucket never contains course or account data.
const ORIGIN="https://course-material-updates-20261028.daringkiwi32.chatgpt.site";
const REPO="Taimai07/shiroha-workspace";
const BRANCH="refs/heads/course-material-desktop";
const WORKFLOW=REPO+"/.github/workflows/course-material-desktop.yml@"+BRANCH;
const encoder=new TextEncoder();
let keyCache=null, keyCacheUntil=0, keyRefreshAfter=0;
const json=(body,status=200)=>new Response(JSON.stringify(body),{status,headers:{"Content-Type":"application/json; charset=utf-8","Cache-Control":"no-store"}});
function decode(text){return Uint8Array.from(atob(text.replace(/-/g,"+").replace(/_/g,"/")),c=>c.charCodeAt(0));}
function version(text){if(!/^\d{1,5}\.\d{1,5}\.\d{1,5}$/.test(text))throw new Error("invalid version");return text.split(".").map(Number);}
function compare(a,b){const aa=version(a),bb=version(b);for(let i=0;i<3;i++){if(aa[i]!==bb[i])return aa[i]-bb[i];}return 0;}
async function publisher(request){
 const raw=request.headers.get("Authorization") || "";
 if(!raw.startsWith("Bearer ") || raw.length>20000)throw new Error("publisher authentication required");
 const parts=raw.slice(7).split(".");if(parts.length!==3)throw new Error("invalid publisher token");
 const header=JSON.parse(new TextDecoder().decode(decode(parts[0]))),claims=JSON.parse(new TextDecoder().decode(decode(parts[1])));
 if(header.alg!=="RS256" || typeof header.kid!=="string")throw new Error("invalid publisher signature");
 if(!keyCache || Date.now()>keyCacheUntil || Date.now()>keyRefreshAfter && !keyCache.keys.some(k=>k.kid===header.kid)){
  if(Date.now()<keyRefreshAfter)throw new Error("publisher keys temporarily unavailable");
  keyRefreshAfter=Date.now()+60000;
  const response=await fetch("https://token.actions.githubusercontent.com/.well-known/jwks",{signal:AbortSignal.timeout(10000)});
  if(!response.ok)throw new Error("publisher keys unavailable");
  const text=await response.text();if(text.length>100000)throw new Error("invalid publisher keys");
  keyCache=JSON.parse(text);keyCacheUntil=Date.now()+300000;
 }
 const jwk=keyCache.keys.find(k=>k.kid===header.kid&&k.kty==="RSA");if(!jwk)throw new Error("unknown publisher key");
 const key=await crypto.subtle.importKey("jwk",jwk,{name:"RSASSA-PKCS1-v1_5",hash:"SHA-256"},false,["verify"]);
 if(!await crypto.subtle.verify("RSASSA-PKCS1-v1_5",key,decode(parts[2]),encoder.encode(parts[0]+"."+parts[1])))throw new Error("publisher signature rejected");
 const now=Math.floor(Date.now()/1000);
 if(claims.iss!=="https://token.actions.githubusercontent.com" || claims.aud!==ORIGIN || claims.repository!==REPO || String(claims.repository_id)!=="1430943467" || claims.ref!==BRANCH || claims.workflow_ref!==WORKFLOW || claims.sub!=="repo:"+REPO+":ref:"+BRANCH || !["push","workflow_dispatch"].includes(claims.event_name) || !Number.isInteger(claims.exp) || claims.exp<=now || !Number.isInteger(claims.iat) || claims.iat>now+30 || now-claims.iat>600 || claims.nbf && claims.nbf>now+30)throw new Error("publisher identity rejected");
 return claims;
}
const page=`<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>课题资料助手 · 安装与更新</title><style>body{max-width:760px;margin:10vh auto;padding:24px;background:#f6f8fc;color:#26354a;font:17px/1.8 system-ui}main{padding:30px;border:1px solid #dfe6f1;border-radius:18px;background:white}a{color:#1967d2}small{color:#637083}</style><main><small>COURSE MATERIAL ASSISTANT</small><h1>课题资料助手</h1><p>安装一次，以后在应用中检查、下载并安装更新。</p><p id="latest">正在检查最新版本…</p><p>Windows 安装器支持简体中文和日文。覆盖更新保留已有资料；没有旧版本时进行首次安装。</p><p>本服务只分发软件安装包和版本信息，不接收课程资料、Google 登录或配对码。</p></main><script>fetch('/windows/stable.json').then(r=>{if(!r.ok)throw Error();return r.json()}).then(x=>{const p=document.getElementById('latest'),a=document.createElement('a');a.href=x.url;a.textContent='下载 Windows '+x.version+' 更新安装器';p.replaceChildren(a)}).catch(()=>document.getElementById('latest').textContent='安装包发布中，请稍后再试。');</script></html>`;
export {publisher,compare};
export default {async fetch(request,env){
 const url=new URL(request.url),path=url.pathname;
 if(path==="/" && ["GET","HEAD"].includes(request.method))return new Response(request.method==="HEAD"?null:page,{headers:{"Content-Type":"text/html; charset=utf-8","Content-Security-Policy":"default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'"}});
 if(!env.BUCKET)return json({error:"release storage unavailable"},503);
 if(path==="/windows/stable.json" && ["GET","HEAD"].includes(request.method)){
  const item=await env.BUCKET.get("windows/stable.json");if(!item)return json({error:"first release pending"},503);
  return new Response(request.method==="HEAD"?null:item.body,{headers:{"Content-Type":"application/json; charset=utf-8","Cache-Control":"no-store"}});
 }
 const download=path.match(/^\/releases\/(\d{1,5}\.\d{1,5}\.\d{1,5})\/([a-f0-9]{64})\.exe$/);
 if(download&&["GET","HEAD"].includes(request.method)){
  const item=await env.BUCKET.get(path.slice(1));if(!item)return json({error:"release not found"},404);
  return new Response(request.method==="HEAD"?null:item.body,{headers:{"Content-Type":"application/octet-stream","Content-Disposition":'attachment; filename="CourseMaterialAssistant-Update.exe"',"Content-Length":String(item.size),"Cache-Control":"public,max-age=31536000,immutable","ETag":item.httpEtag,"X-Content-Type-Options":"nosniff"}});
 }
 if(!path.startsWith("/publish/"))return json({error:"not found"},404);
 try{
  const identity=await publisher(request);
  const upload=path.match(/^\/publish\/windows\/(\d{1,5}\.\d{1,5}\.\d{1,5})\/([a-f0-9]{64})$/);
  if(upload&&request.method==="PUT"){
   const size=Number(request.headers.get("Content-Length"));if(!Number.isInteger(size)||size<=0||size>1024*1024*1024)return json({error:"invalid package length"},400);
   const key=`releases/${upload[1]}/${upload[2]}.exe`;
   const result=await env.BUCKET.put(key,request.body,{sha256:upload[2],onlyIf:{etagDoesNotMatch:"*"},customMetadata:{sha256:upload[2],run_id:String(identity.run_id)},httpMetadata:{contentType:"application/octet-stream"}});
   const saved=result||await env.BUCKET.head(key);
   if(!saved||saved.size!==size||saved.customMetadata?.sha256!==upload[2])throw new Error("package verification failed");
   return json({uploaded:true,size:saved.size});
  }
  if(path==="/publish/stable"&&request.method==="POST"){
   if(Number(request.headers.get("Content-Length"))>32000)return json({error:"manifest too large"},413);
   const reader=request.body.getReader();let length=0;const chunks=[];
   while(true){const {done,value}=await reader.read();if(done)break;length+=value.length;if(length>32000){await reader.cancel();return json({error:"manifest too large"},413);}chunks.push(value);}
   const bytes=new Uint8Array(length);let offset=0;for(const chunk of chunks){bytes.set(chunk,offset);offset+=chunk.length;}
   const data=JSON.parse(new TextDecoder().decode(bytes));version(data.version);
   if(!/^[a-f0-9]{64}$/.test(data.sha256)||!Number.isInteger(data.size)||data.size<=0||typeof data.notes!=="string"||data.notes.length>8000)return json({error:"invalid manifest"},400);
   const key=`releases/${data.version}/${data.sha256}.exe`,object=await env.BUCKET.head(key);
   if(!object||object.size!==data.size||object.customMetadata?.sha256!==data.sha256)throw new Error("verified package is not uploaded");
   const current=await env.BUCKET.get("windows/stable.json"),old=current?await current.json():null;
   if(old&&(compare(data.version,old.version)<0||data.version===old.version&&data.sha256!==old.sha256))return json({error:"stable release cannot be replaced or downgraded"},409);
   const manifest={schema:1,app_id:"course-material-assistant",platform:"windows",architecture:"x64",version:data.version,url:ORIGIN+"/"+key,sha256:data.sha256,size:data.size,notes:data.notes,published_at:new Date().toISOString()};
   const saved=await env.BUCKET.put("windows/stable.json",JSON.stringify(manifest),{onlyIf:old?{etagMatches:current.etag}:{etagDoesNotMatch:"*"},httpMetadata:{contentType:"application/json"}});
   if(!saved)return json({error:"another release was published; retry after checking stable"},409);
   return json({published:true,version:data.version,url:manifest.url});
  }
  return json({error:"method not allowed"},405);
 }catch(error){return json({error:String(error.message).slice(0,160)},403);}
}};
