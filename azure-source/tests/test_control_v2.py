import copy
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT=Path(__file__).resolve().parents[1]
(ROOT/'work').mkdir(exist_ok=True)
SOURCE=ROOT
sys.path.insert(0,str(ROOT/'work'/'qa-deps'))
sys.path.insert(0,str(SOURCE))
import app
import worker
from github_api import GitHub, GitHubError, Ledger
from metadata import prepare, text_sidecar
from records import RecordStore
from vault import Vault

CHANNEL='UC'+'1'*22
PASSWORD='qa-only-recovery-password-123'
STAMP='2026-10-17T12:00:00+00:00'


class MemoryGitHub:
    def __init__(self,owner='operator'):
        self.owner,self.token=owner,'qa-github-secret'
        self.files={};self.head='head-1';self.writes=0;self.fail_write=0
    def repo_path(self,repo):return f'/repos/{self.owner}/{repo}'
    def repository(self,repo):return dict(id=1,private=True,default_branch='main',html_url=f'https://github.com/{self.owner}/{repo}')
    def read_file(self,repo,path,branch='main',missing=False):
        key=(repo,branch,path)
        if key not in self.files:
            # Snapshots are immutable aliases for the latest QA head.
            key=(repo,'main',path) if branch==self.head else key
        if key not in self.files:
            if missing:return None,None
            raise GitHubError(404)
        raw=self.files[key];return raw,hashlib.sha256(raw).hexdigest()
    def write_file(self,repo,path,content,branch='main',sha=None,message=''):
        key=(repo,branch,path);existing=self.files.get(key)
        if existing and sha!=hashlib.sha256(existing).hexdigest():raise GitHubError(409)
        self.writes+=1
        self.files[key]=content
        if self.writes==self.fail_write:raise GitHubError('network')
        return {'content':{'sha':hashlib.sha256(content).hexdigest()}}
    def snapshot(self,repo,files,branch,expected_head=None,message=''):
        if expected_head and expected_head!=self.head:raise ValueError('Stale workspace')
        for path,value in files.items():self.files[(repo,branch,path)]=value.encode()
        self.head='head-'+str(int(self.head.split('-')[1])+1);return self.head
    def request(self,path,method='GET',body=None):
        if '/git/ref/heads/' in path:return {'object':{'sha':self.head}}
        if method=='POST' and path.endswith('/git/refs'):return {}
        if path=='/user':return {'login':self.owner}
        if '/actions/variables' in path:return {'variables':[{'name':'ENABLED','value':'false'}]}
        if '/runs?' in path:return {'workflow_runs':[]}
        raise AssertionError((path,method))


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'work')
        self.folder=Path(self.temp.name)
        self.vault=Vault(self.folder/'connections.enc.json');self.vault.unlock(PASSWORD)
        self.state=dict(version=2,workspace_id='a'*32,connections={},clients={},remote={},last_sync='',notice='QA')
        self.patches=[patch.object(app,'WORK',self.folder),patch.object(app,'STATE_PATH',self.folder/'workspace.json'),
                      patch.object(app,'STATE',self.state),patch.object(app,'VAULT',self.vault)]
        for p in self.patches:p.start()
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.temp.cleanup()

    def github(self,name='operator'):
        cid='gh:'+name;self.state['connections'][cid]=dict(id=cid,kind='github',login=name,label=name,status='Connected')
        self.vault.put(cid,{'token':'qa-secret-'+name});return cid

    def test_portable_vault_wrong_password_and_tamper(self):
        self.vault.put('meta:1',{'token':'META-TOKEN-DO-NOT-LEAK'})
        raw=self.vault.path.read_bytes();self.assertNotIn(b'META-TOKEN',raw);self.assertNotIn(PASSWORD.encode(),raw)
        other=Vault(self.folder/'new-laptop'/'connections.enc.json');other.path.parent.mkdir();other.path.write_bytes(raw)
        other.unlock(PASSWORD);self.assertEqual(other.get('meta:1')['token'],'META-TOKEN-DO-NOT-LEAK')
        with self.assertRaises(ValueError):Vault(other.path).unlock('incorrect-password-123')
        self.assertEqual(other.path.read_bytes(),raw)
        data=json.loads(raw);data['ciphertext']='AAAA';other.path.write_text(json.dumps(data))
        with self.assertRaises(ValueError):Vault(other.path).unlock(PASSWORD)

    def test_owner_pinned_even_when_other_account_active(self):
        self.github('alice');self.github('bob')
        with patch.dict(os.environ,{'GH_TOKEN':'unrelated-current-account'}):
            selected=app.github_for_client(dict(github_connection='gh:alice',owner='alice'))
        self.assertEqual((selected.owner,selected.token),('alice','qa-secret-alice'))
        with self.assertRaises(ValueError):app.github_for_client(dict(github_connection='gh:bob',owner='alice'))
        serialized=json.dumps(app.public_state());self.assertNotIn('qa-secret-',serialized)

    @unittest.skipUnless(os.name=='nt','Windows DPAPI only')
    def test_windows_remember_unlock_without_plaintext_key(self):
        self.vault.put('connection',{'token':'fake-persisted-token'})
        self.assertTrue(self.vault.remember_windows())
        protected=self.vault.path.with_name('unlock.windows').read_bytes()
        self.assertNotIn(self.vault.key,protected)
        restarted=Vault(self.vault.path)
        self.assertTrue(restarted.try_remembered_windows())
        self.assertEqual(restarted.get('connection')['token'],'fake-persisted-token')

    def test_legacy_migration_keeps_original_state_and_client(self):
        old=dict(owner='legacyowner',clients=[dict(id='legacy',name='Legacy client',folder_id='folder',channel_id=CHANNEL,repo_name='youtube-legacy',deployed=True,enabled=True)])
        path=self.folder/'setup-clients.json';path.write_text(json.dumps(old));before=path.read_bytes()
        self.state.clear();app.load()
        c=self.state['clients']['legacy'];self.assertEqual(c['source_type'],'drive');self.assertEqual(c['owner'],'legacyowner')
        self.assertTrue(c['enabled']);self.assertEqual(path.read_bytes(),before)

    def test_title_txt_first_line_and_private_override(self):
        meta=text_sidecar('Title: Real title\nDescription: Real content\n#mobility')
        self.assertEqual(meta['title'],'Real title');self.assertEqual(meta['description'],'Real content\n#mobility')
        import main
        with patch.object(main,'read_sidecar',return_value=dict(meta,privacy='public')),patch.dict(os.environ,{'FORCE_PRIVATE':'true'}):
            body=main.video_body(None,[],{'name':'009182.mp4'},'public','22')
        self.assertEqual(body['snippet']['title'],'Real title');self.assertEqual(body['status']['privacyStatus'],'private')
        self.assertIn('mobility',body['snippet']['tags'])
        long=prepare('x'*150,'y'*6000,['some tag']*1000)
        self.assertEqual(len(long['snippet']['title']),100);self.assertEqual(len(long['snippet']['description']),5000)
        self.assertEqual(long['snippet']['tags'],['some tag', 'x'*60])



    def test_ledger_reservation_persists_after_ambiguous_ack(self):
        github=MemoryGitHub();ledger=Ledger(github,'repo',CHANNEL);ledger.load(create=True);ledger.save()
        github.fail_write=github.writes+1
        with self.assertRaises(GitHubError):ledger.reserve('drive:1',{'id':'1'},'2026-10-17','attempt')
        next_ledger=Ledger(github,'repo',CHANNEL);next_ledger.load(required=True)
        self.assertFalse(next_ledger.reserve('drive:1',{'id':'1'},'2026-10-17','second'))
        self.assertEqual(next_ledger.data['daily']['2026-10-17'],1)

    def test_missing_ledger_cannot_silently_start_fresh(self):
        with self.assertRaises(ValueError):Ledger(MemoryGitHub(),'repo',CHANNEL).load(required=True)

    def test_remote_backup_and_fetch_roundtrip_without_plaintext_secrets(self):
        self.github();self.vault.put('yt:1',{'REFRESH_TOKEN':'private-google-refresh'})
        self.state['remote']=dict(connection='gh:operator',owner='operator',repo='control',repo_id=1,branch='main',head='')
        self.state['clients']['one']=dict(id='one',name='Original client')
        github=MemoryGitHub()
        with patch.object(app,'gh_for',return_value=github):app.save_remote()
        all_remote=b'\n'.join(github.files.values());self.assertNotIn(b'private-google-refresh',all_remote);self.assertNotIn(PASSWORD.encode(),all_remote)
        self.state['clients']={};app.save_local()
        with patch.object(app,'gh_for',return_value=github):app.fetch_remote(dict(connection='gh:operator',repo='control',password=PASSWORD))
        self.assertEqual(self.state['clients']['one']['name'],'Original client')
        self.assertEqual(self.vault.get('yt:1')['REFRESH_TOKEN'],'private-google-refresh')
        before=copy.deepcopy(self.state)
        with patch.object(app,'gh_for',return_value=github),self.assertRaises(ValueError):
            app.fetch_remote(dict(connection='gh:operator',repo='control',password='wrong-password-123'))
        self.assertEqual(self.state,before)


    def test_http_only_local_nonce_and_origin(self):
        server=app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        url=f'http://127.0.0.1:{server.server_port}'
        try:
            with urlopen(url) as response:self.assertIn(b'DriveCast / Local control',response.read())
            with self.assertRaises(HTTPError):urlopen(url+'/api/state')
            headers={'X-Setup-Token':app.TOKEN,'Content-Type':'application/json','Origin':'https://evil.invalid'}
            with self.assertRaises(HTTPError):urlopen(Request(url+'/api/sync',data=b'{}',headers=headers))
            with urlopen(Request(url+'/api/state',headers={'X-Setup-Token':app.TOKEN})) as response:
                self.assertNotIn(b'private-google-refresh',response.read())
        finally:server.shutdown();server.server_close();thread.join()

    def run_drive(self,github,items,youtube=None,**env):
        youtube=youtube or MagicMock();yc=MagicMock();yc.__enter__.return_value=youtube
        dc=MagicMock();dc.__enter__.return_value=dc
        environment=dict(SOURCE_TYPE='drive',EXPECTED_CHANNEL_ID=CHANNEL,GITHUB_REPOSITORY='operator/repo',
            GITHUB_TOKEN='test',DRY_RUN='false',FORCE_PRIVATE='true',UPLOAD_LIMIT='1',YOUTUBE_REFRESH_TOKEN='test',REFRESH_TOKEN='test',FOLDER_ID='client')
        environment.update(env)
        def process(drive,yt,video,files,folders,root,privacy,category,report):
            body=prepare('A real video title',privacy=privacy,category=category)
            yt.videos().insert(part='snippet,status',body=body)
            report('abcdefghijk',body)
            return True
        with patch.dict(os.environ,environment,clear=True),patch.object(worker,'GitHub',return_value=github), \
             patch.object(worker,'google_credentials',return_value=object()),patch.object(worker,'build',side_effect=[yc,dc]), \
             patch.object(worker.drive_tool,'verify_channel'),patch.object(worker.drive_tool,'list_files',return_value=items), \
             patch.object(worker.drive_tool,'get_folders',return_value={}),patch.object(worker.drive_tool,'daily_budget',return_value=2), \
             patch.object(worker.drive_tool,'video_body',return_value=prepare('A real video title')),patch.object(worker.drive_tool,'process',side_effect=process):
            return worker.run(),youtube

    def seeded(self):
        github=MemoryGitHub();ledger=Ledger(github,'repo',CHANNEL);ledger.load(create=True);ledger.data['source_identity']={'type':'drive','id':'client'};ledger.save();return github
    def item(self,id='1'):
        return dict(id=id,name='A real video title.mp4',mimeType='video/mp4',description='')

    def test_drive_worker_uploads_once_and_forces_private(self):
        github=self.seeded();result,yt=self.run_drive(github,[self.item()]);self.assertEqual(result,0)
        body=yt.videos.return_value.insert.call_args.kwargs['body'];self.assertEqual(body['status']['privacyStatus'],'private')
        self.run_drive(github,[self.item()],yt)
        self.assertEqual(yt.videos.return_value.insert.call_count,1)
        ledger=Ledger(github,'repo',CHANNEL);ledger.load(required=True)
        self.assertEqual(ledger.data['items']['drive:1']['status'],'Done')

    def test_drive_daily_cap_survives_fresh_workers(self):
        github=self.seeded();items=[self.item(str(i)) for i in range(3)]
        for _ in range(3):self.run_drive(github,items)
        ledger=Ledger(github,'repo',CHANNEL);ledger.load(required=True)
        self.assertEqual(len(ledger.data['items']),2);self.assertEqual(sum(ledger.data['daily'].values()),2)

    def test_drive_dry_run_does_not_reserve_or_upload(self):
        github=self.seeded();before=github.writes;result,yt=self.run_drive(github,[self.item()],DRY_RUN='true')
        self.assertEqual(result,0);self.assertEqual(github.writes,before);yt.videos.return_value.insert.assert_not_called()

    def test_lost_record_ack_after_acceptance_never_reuploads(self):
        github=self.seeded();github.fail_write=github.writes+2
        result,yt=self.run_drive(github,[self.item()]);self.assertEqual(result,1)
        ledger=Ledger(github,'repo',CHANNEL);ledger.load(required=True)
        self.assertEqual(ledger.data['items']['drive:1']['status'],'Needs review')
        self.run_drive(github,[self.item()],yt)
        self.assertEqual(yt.videos.return_value.insert.call_count,1)

    def test_upload_failure_is_quarantined_and_never_retried(self):
        github=self.seeded();yt=MagicMock();yt.videos.return_value.insert.side_effect=OSError('ambiguous request failure')
        result,yt=self.run_drive(github,[self.item()],yt);self.assertEqual(result,1)
        self.run_drive(github,[self.item()],yt)
        self.assertEqual(yt.videos.return_value.insert.call_count,1)
        ledger=Ledger(github,'repo',CHANNEL);ledger.load(required=True)
        self.assertEqual(ledger.data['items']['drive:1']['status'],'Failed')

    def test_invalid_boolean_and_missing_ledger_stop_before_upload(self):
        with self.assertRaises(ValueError):self.run_drive(self.seeded(),[self.item()],DRY_RUN='oops')
        with self.assertRaises(ValueError):self.run_drive(MemoryGitHub(),[self.item()])

    def test_github_snapshot_refuses_stale_laptop(self):
        github=GitHub('test','operator')
        with patch.object(github,'request',return_value={'object':{'sha':'new-head'}}) as request, self.assertRaises(ValueError):
            github.snapshot('repo',{'workspace.json':'{}'},'main','old-head')
        self.assertEqual(request.call_count,1)


if __name__=='__main__':unittest.main(verbosity=2)
