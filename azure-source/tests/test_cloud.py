import base64
import concurrent.futures
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from channel_matching import match
from cloud_store import CloudStore
import cloud_backup
import accounts
import workspace_db
from vault import Vault
import cloud_runner
import cloud_migration
from types import SimpleNamespace
from contextlib import nullcontext
import threading


class Matching(unittest.TestCase):
    def state(self,folders=None,channels=None):
        return dict(drive_root={'folders':folders or []},connections={str(i):dict(kind='youtube',id=str(i),label=name,channel_id='UC'+str(i)*22,status='Connected') for i,name in enumerate(channels or [])})
    def test_trim_and_case(self):
        r=match(self.state([dict(id='f',name='  nOrA  ')],['Nora']))[0]
        self.assertEqual(r['status'],'Matched');self.assertTrue(r['ready'])
    def test_missing_channel(self): self.assertIn('waiting for channel',match(self.state([dict(id='f',name='Nora')]))[0]['status'])
    def test_missing_folder(self): self.assertEqual(match(self.state(channels=['Nora']))[0]['status'],'Channel found, no folder')
    def test_duplicate_folders(self):
        self.assertEqual(match(self.state([dict(id='f',name='nora'),dict(id='g',name='Nora')],['Nora']))[0]['status'],'Ambiguous name')
    def test_duplicate_channel_titles(self):
        self.assertEqual(match(self.state([dict(id='f',name='nora')],['Nora','NORA']))[0]['status'],'Ambiguous name')
    def test_no_mutation_or_fuzzy_match(self):
        s=self.state([dict(id='f',name='Nora Fans')],['Nora']);before=json.dumps(s,sort_keys=True)
        self.assertEqual(len(match(s)),2);self.assertEqual(before,json.dumps(s,sort_keys=True))


class Queue(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=CloudStore(Path(self.tmp.name)/'records.sqlite3')
        self.config=dict(id='r',channel_id='UC'+'a'*22,folder_id='folder',project_id='p',max_uploads_per_day=2)
        self.db.save_route(self.config,history_verified=True)
    def tearDown(self):self.tmp.cleanup()
    def job(self):
        self.db.set_enabled('r',True);self.db.enqueue('r','public');return self.db.claim('w')
    def reserve(self,job,key='drive:one',maximum=2):
        return self.db.reserve(job,self.config,key,dict(id=key,filename=key,size=20),'2026-10-22',maximum)
    def test_one_job_per_route(self):
        self.assertIsNotNone(self.db.enqueue('r'));self.assertIsNone(self.db.enqueue('r'))
    def test_atomic_claim_from_two_workers(self):
        self.db.enqueue('r')
        with concurrent.futures.ThreadPoolExecutor(2) as pool: results=list(pool.map(self.db.claim,['a','b']))
        self.assertEqual(sum(r is not None for r in results),1)
    def test_dry_job_cannot_reserve(self):
        self.db.enqueue('r');job=self.db.claim('w');self.assertFalse(self.reserve(job))
        self.assertEqual(self.db.snapshot()['reservations'],0)
    def test_duplicate_reservation_across_restart(self):
        job=self.job();self.assertTrue(self.reserve(job));other=CloudStore(self.db.path)
        self.assertFalse(other.reserve(job,self.config,'drive:one',{},'2026-10-22',2))
    def test_expired_job_without_reservation_reassigned(self):
        job=self.job();self.db.recover(job['lease_until']+1)
        self.assertIsNotNone(self.db.claim('new'));self.assertFalse(self.reserve(job))
    def test_crash_after_reservation_never_requeued(self):
        job=self.job();self.assertTrue(self.reserve(job));self.db.recover(job['lease_until']+1)
        self.assertIsNone(self.db.claim('new'));self.assertEqual(self.db.snapshot()['jobs'][0]['state'],'quarantined')
    def test_late_acceptance_retains_id_after_lease_expiry(self):
        job=self.job();self.reserve(job);self.db.recover(job['lease_until']+1)
        self.db.update_reservation(job,self.config['channel_id'],'drive:one','accepted',youtube_id='abcdefghijk')
        self.assertIn('abcdefghijk',json.dumps(self.db.export()))
    def test_channel_daily_limit_counts_uncertain(self):
        job=self.job();self.assertTrue(self.reserve(job,'drive:a',1));self.assertFalse(self.reserve(job,'drive:b',1))
    def test_project_quota_is_shared(self):
        job=self.job();self.assertTrue(self.db.reserve(job,self.config,'a',{},'2026-10-22',2,1))
        c=dict(self.config,id='r2',folder_id='f2',channel_id='UC'+'b'*22)
        self.db.save_route(c,True);self.db.set_enabled('r2',True);self.db.enqueue('r2','public');j=self.db.claim('w2')
        self.assertFalse(self.db.reserve(j,c,'b',{},'2026-10-22',2,1))
    def test_pause_fences_unstarted_upload(self):
        job=self.job();self.db.set_enabled('r',False);self.assertFalse(self.reserve(job))
    def test_route_identity_cannot_be_overwritten(self):
        with self.assertRaises(ValueError): self.db.save_route(dict(self.config,channel_id='different'))
    def test_unpublished_preview_can_correct_its_source(self):
        config=dict(self.config,id='preview',channel_id='preview-channel',folder_id='preview-folder')
        self.db.save_route(config)
        self.db.save_route(dict(config,folder_id='corrected-folder'))
        self.assertEqual(next(r for r in self.db.routes() if r['id']=='preview')['folder_id'],'corrected-folder')
    def test_missing_history_blocks_live(self):
        c=dict(self.config,id='new',folder_id='new',channel_id='new');self.db.save_route(c)
        with self.assertRaises(ValueError):self.db.set_enabled('new',True)
    def test_import_is_idempotent_and_identity_bound(self):
        ledger=dict(channel_id=self.config['channel_id'],items={'drive:one':dict(id='one',status='Processing')})
        self.db.import_ledger(self.config,ledger);self.db.import_ledger(self.config,ledger)
        self.assertEqual(self.db.snapshot()['reservations'],1)
        with self.assertRaises(ValueError):self.db.import_ledger(self.config,dict(ledger,channel_id='wrong'))
    def test_restore_always_paused_and_uncertain(self):
        job=self.job();self.reserve(job);other=CloudStore(Path(self.tmp.name)/'restore.sqlite3');other.restore(self.db.export())
        self.assertFalse(other.routes()[0]['enabled']);self.assertEqual(other.snapshot()['jobs'][0]['state'],'quarantined')
        self.assertEqual(other.snapshot()['uncertain'],1)
    def test_worker_capacity(self):
        self.job();self.assertIsNone(self.db.claim('w',capacity=1))
    def test_monthly_transfer_cap(self):
        job=self.job()
        with patch.dict(os.environ,{'DRIVECAST_MONTHLY_TRANSFER_GB':'0'}):self.assertFalse(self.reserve(job))


class Recovery(unittest.TestCase):
    def test_encrypted_whole_server_roundtrip_pauses_routes(self):
        with tempfile.TemporaryDirectory() as temp,patch.dict(os.environ,{'DRIVECAST_BACKUP_KEY':base64.b64encode(os.urandom(32)).decode()}):
            root=Path(temp)/'in';root.mkdir();(root/'workspace.json').write_text(json.dumps({'clients':{'a':{'enabled':True}},'remote':{'x':1}}))
            db=CloudStore(root/'records.sqlite3');db.save_route(dict(id='a',channel_id='c',folder_id='f'),True);db.set_enabled('a',True)
            raw=cloud_backup.snapshot(root);self.assertNotIn(b'workspace.json',raw)
            out=Path(temp)/'out';cloud_backup.restore(raw,out)
            self.assertFalse(json.loads((out/'workspace.json').read_text())['clients']['a']['enabled'])
            self.assertFalse(CloudStore(out/'records.sqlite3').routes()[0]['enabled'])
            with self.assertRaises(ValueError):cloud_backup.restore(raw,out)
    def test_wrong_key_or_tamper_fails_before_restore(self):
        with tempfile.TemporaryDirectory() as temp,patch.dict(os.environ,{'DRIVECAST_BACKUP_KEY':base64.b64encode(os.urandom(32)).decode()}):
            root=Path(temp)/'in';root.mkdir();raw=cloud_backup.snapshot(root)
            with self.assertRaises(Exception):cloud_backup.restore(raw[:-1]+bytes([raw[-1]^1]),Path(temp)/'out')
            self.assertFalse((Path(temp)/'out').exists())


class HostedAccounts(unittest.TestCase):
    def test_closed_signup_invite_is_email_bound_single_use(self):
        with tempfile.TemporaryDirectory() as temp:
            a=accounts.Accounts(temp,'https://example.test',True)
            with self.assertRaises(ValueError):a.signup('owner@example.test','strong-password-123')
            token=a.invite('owner@example.test')
            with self.assertRaises(ValueError):a.signup('other@example.test','strong-password-123',token)
            uid=a.signup('owner@example.test','strong-password-123',token)
            with self.assertRaises(ValueError):a.signup('owner@example.test','strong-password-123',token)
            login,_=a.login('owner@example.test','strong-password-123','127.0.0.1')
            self.assertEqual(a.session(login)['user_id'],uid);a.logout_all(uid);self.assertIsNone(a.session(login))

    def test_recovery_invitation_binds_only_one_exact_workspace(self):
        with tempfile.TemporaryDirectory() as temp:
            a=accounts.Accounts(temp,'https://example.test',True);uid='a'*32
            token=a.invite('owner@example.test',uid)
            self.assertEqual(a.signup('owner@example.test','strong-password-123',token),uid)


class WorkspaceConcurrency(unittest.TestCase):
    def test_independent_fields_merge_without_lost_updates(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'records.sqlite3';before={'workspace_id':'w','clients':{'a':{'name':'old','enabled':False}},'telegram':{}}
            workspace_db.save(path,{},before)
            one=json.loads(json.dumps(before));one['telegram']['last_daily']='today'
            two=json.loads(json.dumps(before));two['clients']['a']['name']='new'
            workspace_db.save(path,before,one);result=workspace_db.save(path,before,two)
            self.assertEqual(result['clients']['a']['name'],'new');self.assertEqual(result['telegram']['last_daily'],'today')
    def test_encrypted_vault_updates_do_not_overwrite_new_consent(self):
        with tempfile.TemporaryDirectory() as temp,patch.dict(os.environ,{'DRIVECAST_HOSTED':'1'}):
            path=Path(temp)/'connections.enc.json';one=Vault(path);one.unlock('long-recovery-password-123');one.put('telegram',{'offset':1})
            two=Vault(path);two.unlock('long-recovery-password-123')
            one.put('new-google',{'REFRESH_TOKEN':'test-placeholder'})
            two.put('telegram',{'offset':2})
            one.refresh();self.assertEqual(one.values['telegram']['offset'],2);self.assertIn('new-google',one.values)
            self.assertNotIn('test-placeholder',(Path(temp)/'records.sqlite3').read_bytes().decode('latin1'))


class Execution(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.db=CloudStore(self.root/'records.sqlite3')
        self.config=dict(id='r',channel_id='UC'+'a'*22,folder_id='folder',drive_connection='d',drive_root_id='root',
            youtube_connection='yt',name='Test channel',max_uploads_per_day=2,project_id='p')
        self.db.save_route(self.config,True)
        self.youtube=SimpleNamespace();self.drive=SimpleNamespace()
        self.drive.files=lambda:SimpleNamespace(get=lambda **kw:SimpleNamespace(execute=lambda **k:dict(capabilities={'canAddChildren':True})))
        self.app=SimpleNamespace(WORK=self.root,STATE={'drive_root':{'folder_id':'root','connection_id':'d'}},
            connection=lambda *a:{'channel_id':self.config['channel_id']},google_credentials=lambda *a:object(),
            build=lambda api,*a,**k:nullcontext(self.youtube if api=='youtube' else self.drive),
            now=lambda:'2026-10-23T12:00:00+00:00')
        self.alive=threading.Event();self.alive.set()
        self.videos=[dict(id=str(i),name=str(i)+'.mp4',size='100',mimeType='video/mp4') for i in range(3)]
    def tearDown(self):self.temp.cleanup()
    def test_dry_run_has_no_mutating_boundary_calls(self):
        self.db.enqueue('r','dry');job=self.db.claim('w')
        with patch('main.verify_channel'),patch('drive_sources.verify_ancestry'),patch('main.list_files',return_value=self.videos),patch('main.process') as process,patch('main.get_folders') as folders:
            message=cloud_runner.execute(self.app,self.db,job,self.alive)
        self.assertIn('no upload',message);process.assert_not_called();folders.assert_not_called()
        self.assertEqual(self.db.snapshot()['reservations'],0)
    def test_global_dry_gate_stops_before_provider_calls(self):
        self.db.set_enabled('r',True);self.db.enqueue('r','public');job=self.db.claim('w')
        with patch.dict(os.environ,{'DRIVECAST_LIVE_UPLOADS':'0'}),patch('main.verify_channel') as verify:
            with self.assertRaisesRegex(ValueError,'dry-run-only'):cloud_runner.execute(self.app,self.db,job,self.alive)
            verify.assert_not_called()
    def test_empty_folders_do_not_spend_youtube_quota(self):
        self.db.set_enabled('r',True);self.db.enqueue('r','public');job=self.db.claim('w')
        with patch.dict(os.environ,{'DRIVECAST_LIVE_UPLOADS':'1'}),patch('main.verify_channel') as verify,patch('drive_sources.verify_ancestry'),patch('main.list_files',return_value=[]):
            cloud_runner.execute(self.app,self.db,job,self.alive)
        verify.assert_not_called()
    def test_private_test_is_one_video_and_forces_private(self):
        self.db.enqueue('r','private');job=self.db.claim('w')
        with patch.dict(os.environ,{'DRIVECAST_LIVE_UPLOADS':'1'}),patch('main.verify_channel'),patch('drive_sources.verify_ancestry'),patch('main.list_files',return_value=self.videos),patch('main.get_folders',return_value={}),patch('main.process',return_value=True) as process,patch('cloud_runner.mirror_records'),patch('telegram_service.notify'):
            cloud_runner.execute(self.app,self.db,job,self.alive)
            self.assertEqual(process.call_count,1)
            self.assertEqual(os.environ['FORCE_PRIVATE'],'true')
            self.assertEqual(process.call_args.args[6],'private')
        self.assertEqual(self.db.snapshot()['reservations'],1)
    def test_accepted_id_survives_post_upload_drive_failure(self):
        self.db.set_enabled('r',True);self.db.enqueue('r','public');job=self.db.claim('w')
        def accepted_then_error(*args,**kw):
            kw['report']('abcdefghijk',dict(snippet={'title':'test'},status={'privacyStatus':'public'},_effective_privacy='public'))
            raise RuntimeError('Drive move unconfirmed')
        with patch.dict(os.environ,{'DRIVECAST_LIVE_UPLOADS':'1'}),patch('main.verify_channel'),patch('drive_sources.verify_ancestry'),patch('main.list_files',return_value=self.videos),patch('main.get_folders',return_value={}),patch('main.process',side_effect=accepted_then_error),patch('cloud_runner.mirror_records'),patch('telegram_service.notify'):
            with self.assertRaisesRegex(ValueError,'uncertain'):cloud_runner.execute(self.app,self.db,job,self.alive)
        self.assertIn('abcdefghijk',json.dumps(self.db.export()));self.assertEqual(self.db.snapshot()['reservations'],1)


class Handover(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.work=Path(self.temp.name);self.db=CloudStore(self.work/'records.sqlite3')
        self.config=dict(id='az',original_client_id='old',channel_id='UC'+'a'*22,folder_id='folder',name='Channel',youtube_connection='yt')
        self.db.save_route(self.config)
        self.db.set_meta('dry:az',dict(checked_at='now',folder_id='folder',channel_id=self.config['channel_id']))
        self.old=dict(id='old',name='Channel',owner='legacy',repo_name='worker',deployed=True,channel_id=self.config['channel_id'])
        self.gh=MagicMock();self.gh.repo_path.return_value='/repos/legacy/worker'
        self.active=[]
        def request(path,method='GET'):
            if path.endswith('/disable'):return {}
            if 'variables?' in path:return {'variables':[{'name':'ENABLED','value':'false'}]}
            if '/runs?' in path:return {'workflow_runs':self.active}
            return {'state':'disabled_manually'}
        self.gh.request.side_effect=request
        self.records=MagicMock()
        self.app=SimpleNamespace(WORK=self.work,STATE={'clients':{'old':dict(self.old)}},check_google=MagicMock(),scan_source=MagicMock(),
            github_for_client=MagicMock(return_value=self.gh),set_variable=MagicMock(),store=lambda:self.records,save_local=MagicMock(),now=lambda:'now')
        self.body=dict(id='az',confirmed=True,other_runners_stopped=True)
        self.ledger=dict(channel_id=self.config['channel_id'],items={'drive:file':dict(id='file',status='Done',youtube_id='abcdefghijk')})
    def tearDown(self):self.temp.cleanup()
    def test_confirmation_is_required_before_any_legacy_mutation(self):
        with self.assertRaises(ValueError):cloud_migration.commit(self.app,dict(self.body,confirmed=False))
        self.app.set_variable.assert_not_called();self.gh.request.assert_not_called()
    def test_active_old_run_prevents_import_and_activation(self):
        self.active=[{'status':'in_progress'}]
        with self.assertRaisesRegex(ValueError,'Wait for the old worker'):cloud_migration.commit(self.app,self.body)
        self.assertFalse(self.db.routes()[0]['history_verified']);self.assertEqual(self.db.snapshot()['reservations'],0)
    def test_history_import_preserves_accepted_ids_and_stays_paused(self):
        with patch.object(cloud_migration,'Ledger') as ledger:
            ledger.return_value.load.return_value=self.ledger
            result=cloud_migration.commit(self.app,self.body)
        self.assertEqual(result['id'],'old');self.assertTrue(self.db.routes()[0]['history_verified'])
        self.assertFalse(self.db.routes()[0]['enabled']);self.assertFalse(self.app.STATE['clients']['old']['enabled'])
        self.assertIn('abcdefghijk',json.dumps(self.db.export()));self.app.check_google.assert_called_once()
    def test_stale_dry_run_for_different_folder_blocks_handover(self):
        self.db.set_meta('dry:az',dict(checked_at='now',folder_id='wrong',channel_id=self.config['channel_id']))
        with self.assertRaisesRegex(ValueError,'successful dry run'):cloud_migration.commit(self.app,self.body)
        self.app.set_variable.assert_not_called()


if __name__=='__main__':unittest.main()
