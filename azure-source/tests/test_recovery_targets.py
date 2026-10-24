import base64,hashlib,importlib.util,json,os,sys,tempfile,time,unittest,zipfile,io
from pathlib import Path
from unittest.mock import patch,MagicMock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import recovery,cloud_backup
from cloud_store import CloudStore


class RecoveryTargets(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.env=patch.dict(os.environ,{'DRIVECAST_BACKUP_KEY':base64.b64encode(os.urandom(32)).decode(),'DRIVECAST_RECOVERY_CONFIG':str(self.root/'config.json'),'DRIVECAST_BACKUP_STORAGE':'blob'},clear=False);self.env.start()
        self.settings={'drive':{'workspace':'a'*32,'connection_id':'drive','folder_id':'private-folder'}}
        (self.root/'config.json').write_text(json.dumps(self.settings))
        self.db=CloudStore(self.root/'accounts/users'/('a'*32)/'records.sqlite3')
    def tearDown(self):self.env.stop();self.temp.cleanup()

    def test_blob_failure_still_writes_verified_drive_and_marks_covered(self):
        with patch.object(cloud_backup,'upload',side_effect=RuntimeError('provider detail never exposed')),patch.object(recovery,'upload_drive',return_value={'verified':True,'file_id':'safe'}) as drive:
            result=cloud_backup.run(self.root)
        self.assertTrue(result['off_azure_verified']);self.assertFalse(result['targets']['azure']['verified'])
        self.assertEqual(drive.call_count,2);self.assertGreater(self.db.meta('recovery:covered_through',0),0)
        self.assertNotIn('provider detail',json.dumps(result))

    def test_drive_failure_does_not_claim_independent_backup_or_cover_dirty(self):
        self.db.set_meta('recovery:dirty',time.time())
        with patch.object(cloud_backup,'upload'),patch.object(recovery,'upload_drive',side_effect=RuntimeError('secret-response')):
            result=cloud_backup.run(self.root)
        self.assertTrue(result['remote_verified']);self.assertFalse(result['off_azure_verified'])
        self.assertEqual(self.db.meta('recovery:covered_through',0),0);self.assertNotIn('secret-response',json.dumps(result))

    def test_no_azure_account_can_use_drive_recovery(self):
        with patch.dict(os.environ,{'DRIVECAST_BACKUP_STORAGE':''}),patch.object(recovery,'upload_drive',return_value={'verified':True}):
            self.assertTrue(cloud_backup.run(self.root)['off_azure_verified'])

    def test_snapshot_contains_source_and_consistent_database_workspace(self):
        workspace=self.root/'accounts/users'/('a'*32)
        (workspace/'workspace.json').write_text(json.dumps({'clients':{'stale':{}},'remote':{}}))
        state={'clients':{'current':{'enabled':True}},'remote':{}}
        self.db.set_meta('workspace:state',state)
        raw=cloud_backup.snapshot(self.root);destination=self.root/'restore'
        cloud_backup.restore(raw,destination)
        restored=json.loads((destination/'accounts/users'/('a'*32)/'workspace.json').read_text())
        self.assertIn('current',restored['clients']);self.assertNotIn('stale',restored['clients'])
        self.assertFalse(restored['clients']['current']['enabled'])
        self.assertTrue((destination/'recovery/source.zip').is_file())
        self.assertEqual(json.loads((destination/'recovery/destination.json').read_text()),self.settings)

    def test_source_allowlist_refuses_runtime_files(self):
        source=self.root/'source';source.mkdir();(source/'.env').write_text('PRIVATE=never')
        (source/'source-files.json').write_text(json.dumps({'files':['.env']}))
        with self.assertRaisesRegex(ValueError,'Private runtime'):recovery.source_bundle(source)

    def test_source_bundle_is_readable_and_has_no_runtime_secrets(self):
        self.assertEqual(recovery.source_bundle(),recovery.source_bundle())
        with zipfile.ZipFile(io.BytesIO(recovery.source_bundle())) as archive:
            self.assertIn('RECOVER_DRIVECAST.md',archive.namelist());self.assertIn('hosting/bootstrap.sh',archive.namelist())
            self.assertFalse(any('/work/' in f or f.endswith('.key') or Path(f).name=='.env' for f in archive.namelist()))

    def test_docker_image_copies_every_allowlisted_source_file(self):
        import shlex,fnmatch
        root=Path(recovery.__file__).parent;patterns=[]
        for line in (root/'Dockerfile').read_text().splitlines():
            if not line.startswith('COPY '):continue
            body=line[5:]
            while body.startswith('--'):body=body.split(' ',1)[1]
            parts=json.loads(body) if body.startswith('[') else shlex.split(body)
            patterns.extend(parts[:-1])
        for name in json.loads((root/'source-files.json').read_text())['files']:
            self.assertTrue(any(name.startswith(pattern+'/') or fnmatch.fnmatchcase(name,pattern) and ('/' not in name or '/' in pattern) for pattern in patterns),name)

    def test_source_bootstrap_can_extract_without_starting_workers(self):
        spec=importlib.util.spec_from_file_location('extract_source',Path(recovery.__file__).parent/'hosting/extract_source.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        backup=self.root/'snapshot.encrypted';backup.write_bytes(cloud_backup.snapshot(self.root))
        key=self.root/'key.txt';key.write_text(os.environ['DRIVECAST_BACKUP_KEY'])
        dest=self.root/'source';module.extract(backup,key,dest)
        self.assertTrue((dest/'Dockerfile').is_file());self.assertFalse((dest/'records.sqlite3').exists())
        with self.assertRaises(ValueError):module.extract(backup,key,dest)

    def test_uploaded_and_authorization_events_mark_dirty_without_network(self):
        self.db.save_route(dict(id='route',channel_id='channel',folder_id='folder'),True)
        self.assertGreater(self.db.meta('recovery:dirty',0),0)
        self.db.set_meta('recovery:dirty',0);self.db.set_enabled('route',True)
        self.assertGreater(self.db.meta('recovery:dirty',0),0)

    def test_drive_upload_rejects_shared_backup_folder(self):
        drive=MagicMock();drive.__enter__.return_value=drive
        drive.files.return_value.get.return_value.execute.return_value={'mimeType':'application/vnd.google-apps.folder'}
        drive.permissions.return_value.list.return_value.execute.return_value={'permissions':[{'type':'anyone','role':'writer'}]}
        with patch.object(recovery,'drive_service',return_value=drive),self.assertRaisesRegex(ValueError,'remain private'):
            recovery.upload_drive(self.root,self.settings['drive'],b'encrypted','snapshot.encrypted')
        drive.files.return_value.create.assert_not_called()

    def test_drive_upload_checks_uploaded_bytes_after_creation(self):
        raw=b'encrypted';drive=MagicMock();drive.__enter__.return_value=drive
        drive.files.return_value.get.return_value.execute.side_effect=[{'mimeType':'application/vnd.google-apps.folder'},{'id':'file','size':str(len(raw)),'md5Checksum':'wrong'}]
        drive.permissions.return_value.list.return_value.execute.return_value={'permissions':[{'type':'user','role':'owner'}]}
        drive.files.return_value.create.return_value.execute.return_value={'id':'file'}
        with patch.object(recovery,'drive_service',return_value=drive),self.assertRaisesRegex(ValueError,'verification failed'):
            recovery.upload_drive(self.root,self.settings['drive'],raw,'snapshot.encrypted')


if __name__=='__main__':unittest.main()
