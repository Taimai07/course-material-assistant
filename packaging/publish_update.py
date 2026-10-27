"""CI-only release upload using short-lived GitHub Actions OIDC; no publisher secret."""
import hashlib
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlencode
from urllib.request import Request,urlopen
from urllib.error import HTTPError

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'server'))
from updates import ORIGIN,validate_manifest,version_tuple
from version import VERSION


def request_json(url,data=None,headers=None,method=None):
    with urlopen(Request(url,data=data,headers=headers or {},method=method),timeout=60) as response:
        return json.loads(response.read(65536))


def publisher_token():
    issuer=os.environ['ACTIONS_ID_TOKEN_REQUEST_URL']
    if not issuer.startswith('https://'):raise SystemExit('OIDC endpoint must use HTTPS')
    endpoint=issuer+('&' if '?' in issuer else '?')+urlencode({'audience':ORIGIN})
    return request_json(endpoint,headers={'Authorization':'Bearer '+os.environ['ACTIONS_ID_TOKEN_REQUEST_TOKEN']})['value']


def main():
    path=ROOT/'installer'/'CourseMaterialAssistant-Update.exe'
    if not path.is_file():raise SystemExit('Installer is missing; build and verify before publishing.')
    with path.open('rb') as handle:digest=hashlib.file_digest(handle,'sha256').hexdigest()
    size=path.stat().st_size
    try:
        current=validate_manifest(request_json(ORIGIN+'/windows/stable.json'))
    except HTTPError as error:
        if error.code!=503:raise
        current=None
    if current and (version_tuple(VERSION)<version_tuple(current['version']) or VERSION==current['version'] and digest!=current['sha256']):
        raise SystemExit('Increase VERSION before publishing a different installer; stable releases are immutable.')
    token=publisher_token()
    headers={'Authorization':'Bearer '+token,'Content-Type':'application/octet-stream','Content-Length':str(size)}
    with path.open('rb') as handle:
        result=request_json(ORIGIN+f'/publish/windows/{VERSION}/{digest}',data=handle,headers=headers,method="PUT")
    if not result.get('uploaded') or result.get('size')!=size:raise SystemExit('Update upload did not verify')
    # Verify anonymous access to the immutable binary before changing what clients see.
    public_url=ORIGIN+f'/releases/{VERSION}/{digest}.exe'
    with urlopen(public_url,timeout=60) as response:
        downloaded=hashlib.sha256();received=0
        while chunk:=response.read(1024*1024):downloaded.update(chunk);received+=len(chunk)
    if received!=size or downloaded.hexdigest()!=digest:raise SystemExit('Public installer bytes did not match the verified CI build')
    notes=(ROOT/'packaging'/'更新说明.txt').read_text(encoding='utf-8')[:8000]
    body=json.dumps({'version':VERSION,'sha256':digest,'size':size,'notes':notes},ensure_ascii=False).encode()
    result=request_json(ORIGIN+'/publish/stable',data=body,headers={'Authorization':'Bearer '+publisher_token(),'Content-Type':'application/json'})
    if not result.get('published'):raise SystemExit('Stable update manifest was not published')
    manifest=validate_manifest(request_json(ORIGIN+'/windows/stable.json'))
    if manifest['version']!=VERSION or manifest['sha256']!=digest:raise SystemExit('Published feed did not match the verified build')
    (ROOT/'installer'/'update-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'published_version':VERSION,'size':size,'sha256':digest,'download_url':manifest['url'],'anonymous_download_verified':True},ensure_ascii=False))

if __name__=='__main__':main()
