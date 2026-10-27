import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'server'))
from app import validate_arguments
from browser_rpc import save_download
from collector import CourseCollector
from projects import ProjectManager
from store import Store,utc_now
from storage import migrate,location,recover
from google_api import write_private
from library import local_file,project_library
from bundle import export_task
import updates


class Desktop07Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.control=(Path(self.temp.name).resolve()/'control').resolve()
        self.store=Store(self.control)

    def test_real_collector_and_project_download_with_large_chrome_tab_id(self):
        operations=[]
        class Browser:
            def status(self):return {'connected':True,'pending_commands':0}
            def call(self,operation,args,timeout=80):
                operations.append(operation)
                if operation=='open':return {'tab_id':2147483646}
                if operation=='close':return {'closed':True}
                if operation=='collect':
                    return {'capture':{'title':'数字设计课程','source_url':'https://classroom.google.com/c/course1','text':'课程说明','coverage':'rendered_collection','captured_at':utc_now(),'warnings':[],'links':[{'title':'课件.txt','url':'https://drive.google.com/file/d/file1/view'}]}}
                if operation=='download_url':return {'filename':'课件.txt','source_url':args['url'],'mime':'text/plain','data_base64':base64.b64encode(b'full original').decode()}
                raise AssertionError(operation)
        browser=Browser();collector=CourseCollector(self.store,browser);manager=ProjectManager(self.store,browser,collector)
        project=manager.add('课程','https://classroom.google.com/c/course1')
        manager.request_sync(project['id'])
        deadline=time.monotonic()+5
        while manager.active and time.monotonic()<deadline:time.sleep(.02)
        state=manager.project(project['id'])
        self.assertEqual(state['status'],'ready',state.get('last_error'))
        self.assertEqual(state['files'],1)
        self.assertIn('collect',operations);self.assertIn('download_url',operations)
        file=project_library(manager,project['id'])['files'][0]
        self.assertEqual(local_file(manager,project['id'],file['key']).read_bytes(),b'full original')
        validate_arguments('browser_read',{'tab_id':2147483646})
        with self.assertRaises(ValueError):validate_arguments('browser_read',{'tab_id':2147483648})

    def material_fixture(self):
        class Browser:
            def status(self):return {'connected':False}
        class Collector:
            def status(self,_):raise ValueError('no active task')
        manager=ProjectManager(self.store,Browser(),Collector())
        project=manager.add('课题','https://classroom.google.com/c/course1')
        saved=save_download(self.store,{'filename':'reading.txt','mime':'text/plain','source_url':'https://drive.google.com/file/d/file1/view','data_base64':base64.b64encode('资料正文'.encode()).decode()})
        manager.record_download(project['id'],saved)
        task={'task_id':'a'*24,'status':'completed','pages':[],'downloads':[saved],'errors':[]}
        bundle=export_task(self.store,task,self.store.root/'projects'/project['id']/'runs'/('a'*24))
        manager.project(project['id']).update(latest_bundle=bundle['directory'],material_ids=[saved['material_id']],status='ready')
        manager._save()
        (self.control/'pairing.json').write_text('{"token":"synthetic-only"}')
        (self.control/'google-token.json').write_text('{"token":"synthetic-google-only"}')
        (self.control/'browser-extension').mkdir();(self.control/'browser-extension'/'manifest.json').write_text('{}')
        (self.store.root/'collections').mkdir(exist_ok=True)
        write_private(self.store.root/'collections'/('a'*24+'.json'),task)
        return manager,project,saved

    def test_migrate_preserves_all_material_paths_and_keeps_credentials_at_control_root(self):
        manager,project,saved=self.material_fixture()
        stale=Store(self.control)
        target=Path(self.temp.name).resolve()/'D-drive'/'课题资料'
        result=migrate(self.store,target)
        self.assertTrue(result['moved'])
        reopened=Store(self.control)
        self.assertEqual(reopened.root,target.resolve())
        restored=ProjectManager(reopened,manager.broker,manager.collector)
        file=project_library(restored,project['id'])['files'][0]
        self.assertTrue(Path(file['path']).is_relative_to(target))
        self.assertEqual(local_file(restored,project['id'],file['key']).read_text(),'资料正文')
        self.assertTrue(Path(restored.project(project['id'])['latest_bundle']).is_relative_to(target))
        task=json.loads((target/'collections'/('a'*24+'.json')).read_text())
        self.assertTrue(Path(task['downloads'][0]['download_path']).is_relative_to(target))
        self.assertTrue((self.control/'pairing.json').is_file());self.assertTrue((self.control/'google-token.json').is_file())
        self.assertTrue((self.control/'browser-extension'/'manifest.json').is_file())
        for name in ('pairing.json','google-token.json','browser-extension','projects.json'):
            self.assertFalse((target/name).exists(),name)
        self.assertFalse((self.control/'attachments').exists())
        self.assertEqual(reopened.read(saved['material_id'])['text'],'资料正文')
        self.assertEqual(stale.status()['saved_materials'],reopened.status()['saved_materials'])
        self.assertEqual(stale.root,target.resolve())

    def test_json_attachments_are_opaque_originals_during_migration(self):
        self.material_fixture()
        folder=self.control/'attachments'/'json-fixture';folder.mkdir()
        originals={'bom.json':b'\xef\xbb\xbf {"a": 1}\n', 'invalid.json':b'not JSON', 'paths.json':json.dumps({'library_path':str(self.control/'attachments'/'reading.txt')},indent=4).encode()}
        for name,raw in originals.items():(folder/name).write_bytes(raw)
        target=Path(self.temp.name).resolve()/'new-json-storage'
        migrate(self.store,target)
        for name,raw in originals.items():self.assertEqual((target/'attachments'/'json-fixture'/name).read_bytes(),raw)

    def test_missing_selected_disk_does_not_create_empty_database(self):
        self.material_fixture()
        target=Path(self.temp.name).resolve()/'selected';migrate(self.store,target)
        detached=target.with_name('detached');target.rename(detached)
        reopened=Store(self.control)
        self.assertFalse(reopened.storage_available)
        class Browser:pass
        CourseCollector(reopened,Browser())
        self.assertFalse(target.exists())
        with self.assertRaisesRegex(ValueError,'位置暂不可用'):reopened.status()
        detached.rename(target)
        reopened.require_available()
        self.assertTrue(reopened.storage_available)
        self.assertGreater(reopened.status()['saved_materials'],0)

    def test_other_store_cannot_write_until_migration_finishes(self):
        self.material_fixture()
        stale=Store(self.control)
        started=__import__('threading').Event()
        target=Path(self.temp.name).resolve()/'locked-storage'
        results=[]
        def read():
            started.set();results.append(stale.status())
        with self.store.material_access():
            worker=__import__('threading').Thread(target=read);worker.start();started.wait(1)
            time.sleep(.1);self.assertEqual(results,[])
            migrate(self.store,target)
        worker.join(3)
        self.assertFalse(worker.is_alive());self.assertTrue(results)
        self.assertEqual(stale.root,target.resolve())

    def test_migration_rejects_external_modification_and_cleans_staging(self):
        _,_,saved=self.material_fixture()
        original=Path(saved['download_path'])
        target=Path(self.temp.name).resolve()/'edited-storage'
        def progress(message):
            if message.startswith('正在迁移资料：'):original.write_bytes(b'externally edited')
        with self.assertRaisesRegex(ValueError,'其他程序修改'):migrate(self.store,target,progress=progress)
        self.assertEqual(original.read_bytes(),b'externally edited')
        self.assertFalse(target.exists())

    def test_legacy_defaults_are_restored_after_settings_reload(self):
        manager,_,_=self.material_fixture()
        manager.settings={'projects':[],'automatic':False}
        manager.normalize()
        self.assertEqual(manager.settings['scan_max_files'],1000)
        self.assertEqual(manager.broker.download_settings()['threshold_bytes'],50*1024*1024)
        manager.maintenance=True
        with self.assertRaises(ValueError):manager.add('x','https://classroom.google.com/c/new')

    def test_private_atomic_write_retries_transient_windows_file_lock(self):
        path=self.control/'metadata.json';write_private(path,{'old':True})
        original=Path.replace
        attempts=[]
        def replace(source,destination):
            attempts.append(True)
            if len(attempts)<3:
                self.assertEqual(json.loads(path.read_text()),{'old':True})
                raise PermissionError('synthetic Windows reader')
            return original(source,destination)
        with patch.object(Path,'replace',replace),patch('google_api.time.sleep'):
            write_private(path,{'new':True})
        self.assertEqual(json.loads(path.read_text()),{'new':True})
        self.assertEqual(len(attempts),3)

    def test_recovery_does_not_take_lock_without_pending_journal(self):
        with self.store.material_access(),patch('storage.switch_lock',side_effect=AssertionError('not needed')):
            recover(self.control)

    def test_staging_rename_retries_temporary_windows_lock(self):
        self.material_fixture()
        target=Path(self.temp.name).resolve()/'retry-storage'
        import storage
        original=storage.os.replace
        attempts=[]
        def replace(source,destination):
            if Path(source).name.startswith('.course-move-'):
                attempts.append(True)
                if len(attempts)<3:raise PermissionError('synthetic Windows scanner')
            return original(source,destination)
        with patch('storage.os.replace',replace),patch('storage.time.sleep'):
            migrate(self.store,target)
        self.assertEqual(len(attempts),3)
        self.assertGreater(self.store.status()['saved_materials'],0)

    def test_cloud_copy_is_reused_after_local_storage_migration(self):
        from library import sync_to_folder
        manager,project,_=self.material_fixture()
        cloud=Path(self.temp.name).resolve()/'cloud';cloud.mkdir()
        manager.configure(50,1000,{'Google Drive':{'enabled':True,'path':str(cloud)}})
        first=sync_to_folder(manager,[project['id']],'Google Drive')
        self.assertEqual(first['written'],1)
        target=Path(self.temp.name).resolve()/'cloud-migration'
        migrate(self.store,target)
        manager.settings=json.loads(manager.path.read_text());manager.normalize()
        second=sync_to_folder(manager,[project['id']],'Google Drive')
        self.assertEqual(second['written'],0)
        self.assertEqual(second['conflicts'],[])

    def test_failed_migration_does_not_change_pointer_or_destroy_originals(self):
        _,_,saved=self.material_fixture()
        target=Path(self.temp.name).resolve()/'new-storage'
        with patch('storage.atomic_copy',side_effect=OSError('synthetic full disk')):
            with self.assertRaises(OSError):migrate(self.store,target)
        self.assertEqual(location(self.control),self.control)
        self.assertTrue(Path(saved['download_path']).is_file())
        self.assertFalse(target.exists())
        self.assertFalse(list(target.parent.glob('.course-move-*')))

    def test_migration_rejects_nonempty_and_contained_destinations(self):
        self.material_fixture()
        target=Path(self.temp.name).resolve()/'occupied';target.mkdir();(target/'unrelated.txt').write_text('keep')
        with self.assertRaises(ValueError):migrate(self.store,target)
        with self.assertRaises(ValueError):migrate(self.store,self.control/'nested')
        self.assertEqual((target/'unrelated.txt').read_text(),'keep')

    def test_journal_recovery_uses_atomic_pointer_as_commit_point(self):
        original={'projects':[],'automatic':False}
        changed={'projects':[],'automatic':True}
        target=Path(self.temp.name).resolve()/'other';target.mkdir()
        write_private(self.control/'storage-switch.json',{'target':str(target),'old_projects':original,'new_projects':changed})
        write_private(self.control/'projects.json',changed)
        recover(self.control)
        self.assertEqual(json.loads((self.control/'projects.json').read_text()),original)
        write_private(self.control/'storage-switch.json',{'target':str(target),'old_projects':original,'new_projects':changed})
        write_private(self.control/'storage-location.json',{'schema':1,'path':str(target)})
        recover(self.control)
        self.assertEqual(json.loads((self.control/'projects.json').read_text()),changed)


class Response(io.BytesIO):
    def __init__(self,data,url,headers=None):super().__init__(data);self.url=url;self.headers=headers or {}
    def geturl(self):return self.url


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.payload=b'MZ synthetic installer bytes'
        sha=hashlib.sha256(self.payload).hexdigest()
        self.manifest={'schema':1,'app_id':'course-material-assistant','platform':'windows','architecture':'x64','version':'0.7.0','sha256':sha,'size':len(self.payload),'url':updates.ORIGIN+'/releases/download/v0.7.0/'+sha+'.exe','notes':'更新说明'}
    def opener(self,body=None):
        manifest=self.manifest;payload=self.payload if body is None else body
        class Opener:
            def open(self,request,timeout):return Response(payload,manifest['url'],{'Content-Length':str(len(payload)),'Content-Type':'application/octet-stream'})
        return Opener()
    def test_verified_download_and_launch_are_bound_to_publisher_hash(self):
        root=Path(self.temp.name)
        path=updates.download(self.manifest,root,opener=self.opener())
        self.assertEqual(path.read_bytes(),self.payload)
        with patch('updates._windows_shell_open') as start:
            updates.launch(path,self.manifest,root);start.assert_called_once_with(path.resolve())
        path.write_bytes(b'changed')
        with self.assertRaises(ValueError):updates.launch(path,self.manifest,root)
    def test_corrupt_or_cancelled_download_is_not_published(self):
        root=Path(self.temp.name)
        with self.assertRaises(ValueError):updates.download(self.manifest,root,opener=self.opener(b'X'*len(self.payload)))
        with self.assertRaises(ValueError):updates.download(self.manifest,root,opener=self.opener(),cancelled=lambda:True)
        self.assertFalse(list((root/'updates').glob('*.exe')));self.assertFalse(list((root/'updates').glob('.update-*')))
    def test_untrusted_feed_download_urls_and_wrong_apps_are_rejected(self):
        for changes in ({'url':'https://evil.example/setup.exe'},{'app_id':'other-app'},{'platform':'macos'},{'size':-1}):
            with self.assertRaises(ValueError):updates.validate_manifest(self.manifest|changes)
        self.assertFalse(updates.trusted_url(updates.ORIGIN+'@evil.example/file.exe'))
    def test_redirects_allow_only_github_asset_host_and_pin_initial_repository(self):
        asset='https://release-assets.githubusercontent.com/github-production-release-asset/123/asset?sig=synthetic'
        self.assertTrue(updates.trusted_url(asset))
        self.assertFalse(updates.trusted_url('https://release-assets.githubusercontent.com.evil.example/github-production-release-asset/123'))
        with self.assertRaises(ValueError):updates.validate_manifest(self.manifest|{'url':asset})
        with self.assertRaises(ValueError):updates.validate_manifest(self.manifest|{'url':self.manifest['url'].replace('Taimai07','SomeoneElse')})
        class Opener:
            def open(self,request,timeout):return Response(self_payload,asset,{'Content-Length':str(len(self_payload))})
        self_payload=self.payload
        path=updates.download(self.manifest,Path(self.temp.name),opener=Opener())
        self.assertEqual(path.read_bytes(),self.payload)

    def test_version_check_does_not_downgrade_and_stays_noninteractive(self):
        data=json.dumps(self.manifest).encode()
        class Opener:
            def open(self,request,timeout):return Response(data,updates.FEED_URL)
        self.assertEqual(updates.check('0.6.0',opener=Opener())['version'],'0.7.0')
        self.assertIsNone(updates.check('0.7.0',opener=Opener()))
        self.assertIsNone(updates.check('0.8.0',opener=Opener()))
