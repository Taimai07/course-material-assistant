"""GitHub release ordering: stable feed changes only after anonymous byte verification."""
from contextlib import redirect_stdout
import base64
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('course_publisher',ROOT/'packaging'/'publish_update.py')
publisher=importlib.util.module_from_spec(spec);spec.loader.exec_module(publisher)

class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)
        (self.root/'installer').mkdir();(self.root/'packaging').mkdir()
        self.data=b'MZ synthetic verified installer'
        (self.root/'installer'/'CourseMaterialAssistant-Update.exe').write_bytes(self.data)
        (self.root/'packaging'/'更新说明.txt').write_text('Synthetic notes',encoding='utf-8')
        self.sha=hashlib.sha256(self.data).hexdigest()
        self.manifest={'schema':1,'app_id':'course-material-assistant','platform':'windows','architecture':'x64','version':publisher.VERSION,'sha256':self.sha,'size':len(self.data),'url':publisher.ORIGIN+f'/releases/download/v{publisher.VERSION}/{self.sha}.exe','notes':'Synthetic'}

    def run_publish(self,download=None,current=None,advanced=None):
        events=[]
        def request(url,data=None,headers=None,method=None):
            if url==publisher.FEED_URL:
                if current:return current
                raise HTTPError(url,404,'first release pending',{},None)
            if url.startswith('https://uploads.github.com/'):
                self.assertEqual(method,'POST');self.assertEqual(data.read(),self.data)
                events.append('upload');return {'size':len(self.data)}
            self.fail('Unexpected JSON request '+url)
        def api(path,data=None,method=None):
            if path.startswith('/releases/tags/'):
                raise HTTPError(path,404,'first release',{},None)
            if path=='/releases':
                self.assertTrue(data['draft']);return {'id':7,'assets':[],'draft':True}
            if path=='/releases/7':
                self.assertEqual(method,'PATCH');events.append('visible');return {}
            if path.startswith('/contents/'):
                if method=='PUT':
                    self.assertEqual(events,['upload','visible','anonymous_download'])
                    manifest=json.loads(base64.b64decode(data['content']))
                    self.assertEqual(manifest['sha256'],self.sha)
                    events.append('stable');return {'commit':{'sha':'a'*40}}
                if advanced:return {'sha':'b'*40,'content':base64.b64encode(json.dumps(advanced).encode()).decode()}
                raise HTTPError(path,404,'first manifest',{},None)
            self.fail('Unexpected API '+path)
        def public_download(request,timeout):
            self.assertEqual(request.full_url,self.manifest['url'])
            self.assertNotIn('Authorization',request.headers)
            events.append('anonymous_download')
            return io.BytesIO(self.data if download is None else download)
        with patch.object(publisher,'ROOT',self.root),patch.object(publisher,'request_json',request),patch.object(publisher,'api',api),patch.object(publisher,'urlopen',public_download),patch.dict(os.environ,{'GITHUB_REPOSITORY':publisher.REPO,'GITHUB_SHA':'c'*40,'GITHUB_TOKEN':'synthetic-only'}),redirect_stdout(io.StringIO()):
            publisher.main()
        return events

    def test_binary_is_publicly_verified_before_stable_changes(self):
        self.assertEqual(self.run_publish(),['upload','visible','anonymous_download','stable'])
        self.assertTrue((self.root/'installer'/'update-manifest.json').is_file())

    def test_corrupt_public_binary_does_not_change_stable_feed(self):
        with self.assertRaisesRegex(SystemExit,'Anonymous installer bytes'):
            self.run_publish(download=b'corrupt')
        self.assertFalse((self.root/'installer'/'update-manifest.json').exists())

    def test_changed_same_version_is_rejected_before_upload(self):
        changed=self.manifest|{'sha256':'a'*64,'url':publisher.ORIGIN+f'/releases/download/v{publisher.VERSION}/'+('a'*64)+'.exe'}
        with self.assertRaisesRegex(SystemExit,'Increase VERSION'):self.run_publish(current=changed)

    def test_concurrently_advanced_feed_is_not_overwritten(self):
        advanced=self.manifest|{'version':'0.8.0','url':publisher.ORIGIN+'/releases/download/v0.8.0/'+self.sha+'.exe'}
        with self.assertRaisesRegex(SystemExit,'advanced during publication'):self.run_publish(advanced=advanced)
