import assert from 'node:assert/strict';
import worker from './index.js';
const origin='https://course-material-updates-20261028.daringkiwi32.chatgpt.site';
const {privateKey,publicKey}=await crypto.subtle.generateKey({name:'RSASSA-PKCS1-v1_5',modulusLength:2048,publicExponent:new Uint8Array([1,0,1]),hash:'SHA-256'},true,['sign','verify']);
const key=await crypto.subtle.exportKey('jwk',publicKey);key.kid='fixture';
globalThis.fetch=async url=>{assert.equal(url,'https://token.actions.githubusercontent.com/.well-known/jwks');return Response.json({keys:[key]});};
const base=value=>Buffer.from(typeof value==='string'?value:JSON.stringify(value)).toString('base64url');
const now=Math.floor(Date.now()/1000);
async function token(overrides={}){const a=base({alg:'RS256',kid:'fixture'}),b=base({iss:'https://token.actions.githubusercontent.com',aud:origin,repository:'Taimai07/shiroha-workspace',repository_id:'1430943467',ref:'refs/heads/course-material-desktop',workflow_ref:'Taimai07/shiroha-workspace/.github/workflows/course-material-desktop.yml@refs/heads/course-material-desktop',sub:'repo:Taimai07/shiroha-workspace:ref:refs/heads/course-material-desktop',event_name:'push',iat:now,exp:now+300,run_id:'fixture',...overrides});return a+'.'+b+'.'+Buffer.from(await crypto.subtle.sign('RSASSA-PKCS1-v1_5',privateKey,new TextEncoder().encode(a+'.'+b))).toString('base64url');}
const objects=new Map();let sequence=0;
const BUCKET={async get(key){const value=objects.get(key);return value?{...value,body:new Response(value.bytes).body,json:async()=>JSON.parse(new TextDecoder().decode(value.bytes))}:null;},async head(key){return objects.get(key)||null;},async put(key,body,options={}){const old=objects.get(key);if(options.onlyIf?.etagDoesNotMatch==='*'&&old||options.onlyIf?.etagMatches&&old?.etag!==options.onlyIf.etagMatches)return null;const bytes=typeof body==='string'?new TextEncoder().encode(body):new Uint8Array(await new Response(body).arrayBuffer());if(options.sha256)assert.equal(Buffer.from(await crypto.subtle.digest('SHA-256',bytes)).toString('hex'),options.sha256);const obj={key,bytes,size:bytes.length,etag:String(++sequence),httpEtag:'"'+sequence+'"',customMetadata:options.customMetadata};objects.set(key,obj);return obj;}};
const call=(path,options={})=>worker.fetch(new Request(origin+path,options),{BUCKET});
assert.equal((await call('/windows/stable.json')).status,503);
assert.equal((await call('/publish/stable',{method:'POST',body:'{}'})).status,403);
for(const override of [{repository:'someone/fork'},{aud:'https://wrong.example'},{exp:now-1},{event_name:'pull_request'},{workflow_ref:'other'}]){const response=await call('/publish/stable',{method:'POST',headers:{Authorization:'Bearer '+await token(override)},body:'{}'});assert.equal(response.status,403);}
const bytes=new TextEncoder().encode('MZ synthetic installer'),sha=Buffer.from(await crypto.subtle.digest('SHA-256',bytes)).toString('hex');
const auth={Authorization:'Bearer '+await token()};
const upload=await call('/publish/windows/0.7.0/'+sha,{method:'PUT',headers:{...auth,'Content-Length':String(bytes.length)},body:bytes});assert.equal(upload.status,200);
const publish=await call('/publish/stable',{method:'POST',headers:auth,body:JSON.stringify({version:'0.7.0',sha256:sha,size:bytes.length,notes:'fixture'})});assert.equal(publish.status,200);
const manifest=await (await call('/windows/stable.json')).json();assert.equal(manifest.sha256,sha);assert.equal(manifest.platform,'windows');
const download=await worker.fetch(new Request(manifest.url),{BUCKET});assert.deepEqual(new Uint8Array(await download.arrayBuffer()),bytes);
assert.equal((await call('/publish/stable',{method:'POST',headers:auth,body:JSON.stringify({version:'0.6.0',sha256:sha,size:bytes.length,notes:'downgrade'})})).status,403); // Package for that version does not exist.
console.log('Public update service: OIDC signature/identity rejection, checksum upload, stable publication and exact download passed (synthetic).');
