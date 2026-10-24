import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cloud_store import CloudStore
from records import RecordStore
from vault import Vault
import workspace_cleanup as cleanup
import workspace_db


class Cleanup(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.env=patch.dict(os.environ,{'DRIVECAST_HOSTED':'1'});self.env.start()
        self.work=Path(self.temp.name)/'owner';self.work.mkdir()
        self.vault=Vault(self.work/'connections.enc.json');self.vault.unlock('fixture-password-1234')
        for name in ('yt1','yt2','drive','gh','google-desktop'):self.vault.put(name,{'fixture':name})
        self.state=dict(version=2,workspace_id='old',clients={},connections={},drive_root={'folders':[{'id':'f1','client_id':'c1'},{'id':'f2','client_id':'c2'}]},google_app={'project_id':'fixture'})
        self.store=RecordStore(self.work/'records.sqlite3');self.store.data()
        self.cloud=CloudStore(self.work/'records.sqlite3')
        for n in (1,2):
            channel='UC'+str(n)*22
            c=dict(id='c'+str(n),name='Channel '+str(n),youtube_connection='yt'+str(n),channel_id=channel,cloud_route_id='r'+str(n))
            self.state['clients'][c['id']]=c
            self.state['connections']['yt'+str(n)]={'id':'yt'+str(n),'kind':'youtube','channel_id':channel}
            self.cloud.save_route(dict(id='r'+str(n),channel_id=channel,folder_id='f'+str(n),original_client_id=c['id'],youtube_connection='yt'+str(n)))
            self.cloud.enqueue('r'+str(n))
            with self.store.connect() as db:
                db.execute('INSERT INTO uploads VALUES(?,?,?,?,?,?,?,?,?)',(c['id'],n,1,'video','old video','Done','abcdefghijk','','2026-10-23'))
                db.execute('INSERT INTO video_stats(client_id,platform,video_id) VALUES(?,?,?)',(c['id'],'youtube','abcdefghijk'))
        self.state['connections']['drive']={'id':'drive','kind':'drive'}
        self.state['connections']['gh']={'id':'gh','kind':'github'}
        self.app=SimpleNamespace(WORK=self.work,STATE=self.state,VAULT=self.vault,store=lambda:self.store,now=lambda:'2026-10-23T18:00:00Z')
        self.base=copy.deepcopy(self.state)
        workspace_db.save(self.cloud.path,{},self.state)
        def save():
            value=workspace_db.save(self.cloud.path,self.base,self.state)
            self.state.clear();self.state.update(value)
            self.base=copy.deepcopy(value)
        self.app.save_local=save
    def tearDown(self):self.env.stop();self.temp.cleanup()
    def reset(self):return cleanup.clear(self.app,{'reset':True,'confirmed':True,'phrase':'RESET'})
    def test_reset_clears_all_channel_data_keeps_app_and_encrypted_checkpoint(self):
        result=self.reset()
        self.assertEqual(result['removed'],2)
        self.assertEqual(self.state['clients'],{});self.assertEqual(self.state['connections'],{})
        self.assertNotEqual(self.state['workspace_id'],'old');self.assertEqual(self.state['google_app']['project_id'],'fixture')
        self.assertEqual(self.vault.values,{'google-desktop':{'fixture':'google-desktop'}})
        self.assertEqual(self.cloud.routes(),[]);self.assertEqual(self.cloud.snapshot()['jobs'],[])
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM uploads').fetchone()[0],0)
        backups=list((self.work/'recovery-backups').glob('*.drivecast'));self.assertEqual(len(backups),1)
        envelope=json.loads(backups[0].read_text());self.assertIn('ciphertext',envelope);self.assertNotIn('clients',envelope)
    def test_delete_selected_channel_preserves_other_channel_and_drive(self):
        cleanup.clear(self.app,{'ids':['c1'],'confirmed':True})
        self.assertEqual(list(self.state['clients']),['c2']);self.assertNotIn('yt1',self.vault.values)
        self.assertIn('yt2',self.vault.values);self.assertIn('drive',self.vault.values)
        self.assertEqual([r['id'] for r in self.cloud.routes()],['r2'])
        self.assertNotIn('client_id',self.state['drive_root']['folders'][0])
        with self.store.connect() as db:self.assertEqual([r[0] for r in db.execute('SELECT client_id FROM uploads')],['c2'])
    def test_delete_authorization_also_removes_its_route(self):
        cleanup.clear(self.app,{'ids':['yt1'],'confirmed':True})
        self.assertNotIn('c1',self.state['clients']);self.assertNotIn('yt1',self.state['connections'])
        self.assertEqual([r['id'] for r in self.cloud.routes()],['r2'])
    def test_active_worker_blocks_reset_before_backup_or_deletion(self):
        self.cloud.claim('active')
        with self.assertRaisesRegex(ValueError,'worker'):self.reset()
        self.assertEqual(len(self.state['clients']),2)
        self.assertFalse((self.work/'recovery-backups').exists())
    def test_confirmation_required(self):
        for body in ({'reset':True},{'reset':True,'confirmed':True,'phrase':'wrong'},{'ids':[],'confirmed':True}):
            with self.assertRaises(ValueError):cleanup.clear(self.app,body)
        self.assertEqual(len(self.state['clients']),2)
    def test_stale_writer_cannot_resurrect_reset_workspace(self):
        old=copy.deepcopy(self.state);self.reset();changed=copy.deepcopy(old);changed['notice']='stale'
        with self.assertRaisesRegex(ValueError,'reset'):workspace_db.save(self.cloud.path,old,changed)
        self.assertEqual(workspace_db.load(self.cloud.path,{})['clients'],{})
    def test_stale_vault_cannot_restore_deleted_authorizations(self):
        stale=Vault(self.vault.path);stale.unlock('fixture-password-1234');self.reset()
        with self.assertRaisesRegex(ValueError,'reset'):stale.put('yt1',{'fixture':'returned-stale-consent'})
        self.vault.refresh();self.assertNotIn('yt1',self.vault.values)
    def test_checkpoint_failure_preserves_credentials_and_records(self):
        with patch.object(cleanup,'checkpoint',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):self.reset()
        self.assertEqual(len(self.state['clients']),2);self.assertIn('yt1',self.vault.values)
    def test_other_workspace_untouched(self):
        other=CloudStore(Path(self.temp.name)/'other'/'records.sqlite3');other.set_meta('workspace:state',{'clients':{'other':{}}})
        self.reset();self.assertIn('other',other.meta('workspace:state')['clients'])
    def test_reset_removes_cache_and_legacy_import_but_keeps_unrelated_files(self):
        avatars=self.work/'avatars';avatars.mkdir()
        picture=avatars/('a'*64+'.img');picture.write_bytes(b'fixture')
        unrelated=avatars/'keep.txt';unrelated.write_text('keep')
        legacy=self.work/'setup-clients.json';legacy.write_text('{}')
        self.reset();self.assertFalse(picture.exists());self.assertFalse(legacy.exists());self.assertTrue(unrelated.exists())


if __name__=='__main__':unittest.main()
