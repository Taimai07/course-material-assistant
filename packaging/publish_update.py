"""Publish verified GitHub Releases; credentials exist only in the Actions publisher job."""
import base64
import hashlib
import json
import os
from pathlib import Path
import sys
from urllib.error import HTTPError
from urllib.request import Request,urlopen

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'server'))
from updates import ORIGIN,FEED_URL,validate_manifest,version_tuple
from version import VERSION
REPO='Taimai07/course-material-assistant'


def request_json(url,data=None,headers=None,method=None):
    with urlopen(Request(url,data=data,headers={'User-Agent':'CourseMaterialAssistant-Publisher',**(headers or {})},method=method),timeout=120) as response:
        raw=response.read(1024*1024+1)
        if len(raw)>1024*1024:raise ValueError('Publisher response too large')
        return json.loads(raw)


def api(path,data=None,method=None):
    return request_json('https://api.github.com/repos/'+REPO+path,
                        data=json.dumps(data,ensure_ascii=False).encode() if data is not None else None,
                        headers={'Authorization':'Bearer '+os.environ['GITHUB_TOKEN'],'Accept':'application/vnd.github+json','Content-Type':'application/json'},method=method)


def optional(call):
    try:return call()
    except HTTPError as error:
        if error.code!=404:raise
        return None


def main():
    if os.environ.get('GITHUB_REPOSITORY')!=REPO:
        raise SystemExit('Publish only from the official public repository workflow.')
    path=ROOT/'installer'/'CourseMaterialAssistant-Update.exe'
    with path.open('rb') as handle:digest=hashlib.file_digest(handle,'sha256').hexdigest()
    size=path.stat().st_size
    current=optional(lambda:request_json(FEED_URL))
    if current:
        current=validate_manifest(current)
        if version_tuple(VERSION)<version_tuple(current['version']) or VERSION==current['version'] and digest!=current['sha256']:
            raise SystemExit('Increase VERSION before publishing a different installer; stable releases are immutable.')
    notes=(ROOT/'packaging'/'更新说明.txt').read_text(encoding='utf-8')[:8000]
    tag='v'+VERSION
    release=optional(lambda:api('/releases/tags/'+tag))
    if not release:
        release=api('/releases',{'tag_name':tag,'target_commitish':os.environ['GITHUB_SHA'],'name':'课题资料助手 '+VERSION,'body':notes,'draft':True,'prerelease':False})
    asset_name=digest+'.exe'
    existing=next((a for a in release.get('assets',[]) if a['name']==asset_name),None)
    if existing and existing['size']!=size:raise SystemExit('Existing release asset has unexpected size')
    if not existing:
        url='https://uploads.github.com/repos/'+REPO+f"/releases/{release['id']}/assets?name="+asset_name
        with path.open('rb') as handle:
            result=request_json(url,data=handle,method='POST',headers={'Authorization':'Bearer '+os.environ['GITHUB_TOKEN'],'Content-Type':'application/octet-stream','Content-Length':str(size)})
        if result.get('size')!=size:raise SystemExit('Release upload size mismatch')
    if release.get('draft'):
        api('/releases/'+str(release['id']),{'draft':False,'make_latest':'true'},method='PATCH')
    public_url=ORIGIN+f'/releases/download/{tag}/{asset_name}'
    with urlopen(Request(public_url,headers={'User-Agent':'CourseMaterialAssistant-Publisher'}),timeout=120) as response:
        downloaded=hashlib.sha256();received=0
        while chunk:=response.read(1024*1024):downloaded.update(chunk);received+=len(chunk)
    if received!=size or downloaded.hexdigest()!=digest:raise SystemExit('Anonymous installer bytes did not match the verified CI build')
    manifest=validate_manifest({'schema':1,'app_id':'course-material-assistant','platform':'windows','architecture':'x64','version':VERSION,'sha256':digest,'size':size,'url':public_url,'notes':notes})
    # Re-read the file SHA and version immediately before the conditional Git contents update.
    entry=optional(lambda:api('/contents/updates/windows/stable.json?ref=main'))
    if entry:
        old=validate_manifest(json.loads(base64.b64decode(entry['content'])))
        if version_tuple(old['version'])>version_tuple(VERSION) or old['version']==VERSION and old['sha256']!=digest:
            raise SystemExit('Stable feed advanced during publication; no overwrite performed')
    body={'message':'Publish verified Windows '+VERSION+' update manifest','branch':'main','content':base64.b64encode((json.dumps(manifest,ensure_ascii=False,indent=2)+'\n').encode()).decode()}
    if entry:body['sha']=entry['sha']
    result=api('/contents/updates/windows/stable.json',body,method='PUT')
    if not result.get('commit',{}).get('sha'):raise SystemExit('Stable update manifest was not committed')
    (ROOT/'installer'/'update-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'published_version':VERSION,'size':size,'sha256':digest,'download_url':public_url,'anonymous_download_verified':True,'manifest_commit':result['commit']['sha']},ensure_ascii=False))

if __name__=='__main__':main()
