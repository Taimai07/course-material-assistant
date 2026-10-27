"""Release ordering: clients see only an already anonymously verified installer."""
from contextlib import redirect_stdout
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
        self.manifest={'schema':1,'app_id':'course-material-assistant','platform':'windows','architecture':'x64','version':publisher.VERSION,'sha256':self.sha,'size':len(self.data),'url':publisher.ORIGIN+f'/releases/{publisher.VERSION}/{self.sha}.exe','notes':'Synthetic'}

    def run_publish(self,download=None,current=None):
        events=[];feeds=[]
        def request(url,data=None,headers=None,method=None):
            if url.endswith('/windows/stable.json'):
                feeds.append(True)
                if len(feeds)==1:
                    if current:return current
                    raise HTTPError(url,503,'first release pending',{},None)
                return self.manifest
            if url.startswith('https://oidc.example/'):
                return {'value':'synthetic-short-lived-token'}
            if '/publish/windows/' in url:
                self.assertEqual(method,'PUT');self.assertEqual(data.read(),self.data)
                events.append('upload');return {'uploaded':True,'size':len(self.data)}
            if url.endswith('/publish/stable'):
                self.assertEqual(events,['upload','anonymous_download'])
                events.append('stable');return {'published':True}
            self.fail('Unexpected request '+url)
        def public_download(url,timeout):
            self.assertEqual(url,self.manifest['url'])
            events.append('anonymous_download')
            return io.BytesIO(self.data if download is None else download)
        with patch.object(publisher,'ROOT',self.root),patch.object(publisher,'request_json',request),patch.object(publisher,'urlopen',public_download),patch.dict(os.environ,{'ACTIONS_ID_TOKEN_REQUEST_URL':'https://oidc.example/token','ACTIONS_ID_TOKEN_REQUEST_TOKEN':'synthetic-only'}):
            with redirect_stdout(io.StringIO()):publisher.main()
        return events

    def test_binary_is_publicly_verified_before_stable_changes(self):
        self.assertEqual(self.run_publish(),['upload','anonymous_download','stable'])
        self.assertTrue((self.root/'installer'/'update-manifest.json').is_file())

    def test_corrupt_public_binary_does_not_change_stable_feed(self):
        with self.assertRaisesRegex(SystemExit,'Public installer bytes'):
            self.run_publish(download=b'corrupt')
        self.assertFalse((self.root/'installer'/'update-manifest.json').exists())

    def test_changed_same_version_is_rejected_before_upload(self):
        with self.assertRaisesRegex(SystemExit,'Increase VERSION'):
            self.run_publish(current=self.manifest|{'sha256':'a'*64,'url':publisher.ORIGIN+f'/releases/{publisher.VERSION}/'+('a'*64)+'.exe'})
