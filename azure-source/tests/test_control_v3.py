import copy
import io
import json
import os
import sys
import threading
from http.server import ThreadingHTTPServer
from urllib.request import Request,urlopen
import unittest
import ssl
from pathlib import Path
from unittest.mock import MagicMock,patch

ROOT=Path(__file__).resolve().parents[1]
(ROOT/'work').mkdir(exist_ok=True)
sys.path.insert(0,str(ROOT/'tests'));sys.path.insert(0,str(ROOT/'work/qa-deps'));sys.path.insert(0,str(ROOT))
import test_control_v2 as base
import app,portable_backup,control_services,video_stats,private_browser
from github_api import Ledger
from records import RecordStore


class V3Tests(unittest.TestCase):
    setUp=base.ControlTests.setUp
    tearDown=base.ControlTests.tearDown

    def test_entire_export_encrypted_preview_import_roundtrip(self):
        self.vault.put('gh:owner',{'token':'SECRET-WILL-NOT-BE-PLAINTEXT'})
        self.state['connections']['gh:owner']=dict(id='gh:owner',kind='github',label='Owner',login='owner')
        self.state['clients']['a']=dict(id='a',name='CONFIDENTIAL-CLIENT')
        self.state['draft']=dict(name='Saved draft',step=3)
        app.store().set_stats('a','youtube','testid',dict(views=99,likes=7,comments=2))
        raw=portable_backup.export(app)
        for secret in (b'SECRET-WILL',b'CONFIDENTIAL-CLIENT',b'Saved draft',base.PASSWORD.encode()):self.assertNotIn(secret,raw)
        before=self.vault.path.read_bytes();old=copy.deepcopy(self.state)
        with self.assertRaises(ValueError):portable_backup.preview(app,raw,'wrong-password-123')
        self.assertEqual(before,self.vault.path.read_bytes());self.assertEqual(old,self.state)
        preview=portable_backup.preview(app,raw,base.PASSWORD)
        self.assertEqual((preview['clients'],preview['connections']),(1,1));self.assertEqual(self.state,old)
        self.state['clients'].clear();self.vault.values.clear()
        portable_backup.restore(app,preview['ticket'],True)
        self.assertEqual(self.state['clients']['a']['name'],'CONFIDENTIAL-CLIENT')
        self.assertIn('gh:owner',self.vault.values);self.assertEqual(self.state['draft']['step'],3)
        self.assertEqual(app.store().data()['video_stats'][0]['views'],'99')
        self.assertTrue(list((self.folder/'recovery-backups').iterdir()))
        with self.assertRaises(ValueError):portable_backup.restore(app,preview['ticket'],True)

    def test_export_locked_no_plaintext_fallback(self):
        self.vault.key=None
        with self.assertRaises(ValueError):portable_backup.export(app)

    def test_http_encrypted_export_and_locked_import_preview(self):
        self.vault.put('gh:owner',{'token':'fake-token-not-plaintext'})
        server=ThreadingHTTPServer(('127.0.0.1',0),app.Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        root='http://127.0.0.1:'+str(server.server_port)
        try:
            headers={'X-Setup-Token':app.TOKEN}
            with urlopen(Request(root+'/api/backup',headers=headers)) as response:raw=response.read()
            self.assertNotIn(b'fake-token',raw);self.assertEqual(json.loads(raw)['format'],portable_backup.FORMAT)
            self.vault.key=None
            import base64
            body=json.dumps(dict(file=base64.b64encode(raw).decode(),password=base.PASSWORD)).encode()
            headers.update({'Content-Type':'application/json','Origin':root})
            with urlopen(Request(root+'/api/import/preview',data=body,headers=headers)) as response:preview=json.load(response)
            self.assertIn('ticket',preview);self.assertIsNone(self.vault.key)
        finally:server.shutdown();server.server_close();thread.join(timeout=5)

    def test_stats_batch_of_fifty_and_missing_not_zero(self):
        yt=MagicMock();ids=['id'+str(i) for i in range(51)]
        yt.videos.return_value.list.return_value.execute.side_effect=[{'items':[dict(id=ids[0],statistics={'viewCount':'42'})]}, {'items':[]}]
        result=video_stats.youtube_stats(yt,ids)
        calls=yt.videos.return_value.list.call_args_list
        self.assertEqual([len(c.kwargs['id'].split(',')) for c in calls],[50,1])
        self.assertEqual(result[ids[0]]['views'],'42');self.assertIsNone(result[ids[0]]['likes']);self.assertIn('notice',result[ids[-1]])

    def test_public_workflow_overrides_sidecar_privacy_but_private_test_wins(self):
        import main
        with patch.object(main,'read_sidecar',return_value=dict(title='Real title',privacy='private')),patch.dict(os.environ,{'FORCE_PUBLIC':'true','FORCE_PRIVATE':'false'}):
            self.assertEqual(main.video_body(None,[],{'name':'1.mp4'},'public','22')['status']['privacyStatus'],'public')
        with patch.object(main,'read_sidecar',return_value=dict(title='Real title',privacy='public')),patch.dict(os.environ,{'FORCE_PUBLIC':'true','FORCE_PRIVATE':'true'}):
            self.assertEqual(main.video_body(None,[],{'name':'1.mp4'},'public','22')['status']['privacyStatus'],'private')

    def test_effective_visibility_is_separate_from_public_request(self):
        import main
        body={'status':{'privacyStatus':'public'}}
        main.record_visibility(body,{'id':'ABCDEFGHIJK','status':{'privacyStatus':'private'}})
        self.assertEqual(body['_effective_privacy'],'private');self.assertEqual(body['_requested_privacy'],'public')
        self.assertEqual(body['status']['privacyStatus'],'public')

    def test_legacy_unknown_visibility_does_not_overwrite_api_verified_private(self):
        client=dict(id='c',name='Client',owner='operator',repo_name='repo')
        ledger={'items':{'drive:f':dict(id='f',source='drive',youtube_id='ABCDEFGHIJK',status='Done',privacy='unknown',filename='Real title')}}
        app.store().import_ledger(client,ledger)
        app.store().set_stats('c','youtube','ABCDEFGHIJK',dict(views=0,privacy='private'))
        app.store().import_ledger(client,ledger)
        self.assertEqual(app.store().data()['uploads'][0]['actual_privacy'],'private')
        self.assertIn('actual_privacy',app.store().csv('uploads').decode('utf-8-sig'))

    def test_instagram_logs_do_not_create_fake_drive_links(self):
        client=dict(id='c',name='Client',owner='operator',repo_name='repo',source_type='instagram')
        run=dict(id=1,created_at='2026-10-17T10:00:00Z',status='completed',conclusion='success')
        app.store().import_run(client,run,"INFO YouTube accepted 'Caption' (123): https://youtu.be/ABCDEFGHIJK")
        row=app.store().data()['uploads'][0]
        self.assertEqual(row['file_id'],'instagram:123');self.assertEqual(row['drive_url'],'')


    def test_unavailable_metrics_keep_previous_value(self):
        app.store().set_stats('a','youtube','video',dict(views=100,likes=9,comments=4))
        app.store().set_stats('a','youtube','video',{},'Video unavailable')
        row=app.store().data()['video_stats'][0];self.assertEqual(row['views'],'100');self.assertEqual(row['notice'],'Video unavailable')
        self.assertIn('youtube_views',app.store().csv('uploads').decode('utf-8-sig'))

    def test_polling_workflow_has_no_publication_slots_or_delay(self):
        for interval in control_services.POLL_INTERVALS:
            text=control_services.workflow(app,dict(check_minutes=interval))
            self.assertIn('37 */6 * * *',text);self.assertNotIn('sleep',text);self.assertNotIn('timezone:',text);self.assertIn('PRIVACY: public',text)
        with self.assertRaises(ValueError):control_services.polling_cron(7)

    def test_empty_github_option_is_removed_instead_of_invalid_blank_variable(self):
        github=MagicMock();github.repo_path.return_value='/repos/operator/repo'
        app.set_variable(github,dict(repo_name='repo'),'FACEBOOK_PAGE_ID','')
        github.request.assert_called_once_with('/repos/operator/repo/actions/variables/FACEBOOK_PAGE_ID','DELETE')

    def test_new_ledger_seeds_skip_old_route_without_erasing_history(self):
        github=base.MemoryGitHub();client=dict(id='c',name='Client',owner='operator',repo_name='repo',channel_id=base.CHANNEL,source_type='drive',folder_id='folder')
        original=Ledger(github,'repo',base.CHANNEL);original.data.update(source_identity=dict(type='drive',id='folder'))
        original.data['items']['drive:f']=dict(id='f',source='drive',youtube_id='ABCDEFGHIJK',filename='Old',status='Done',run_id=1,attempt=1)
        app.store().import_ledger(client,original.data)
        client['ledger_seed']=original.data;client['channel_id']='UC'+'2'*22
        control_services.initialize_ledgers(app,client,github)
        seeded=Ledger(github,'repo',client['channel_id']);seeded.load(required=True)
        self.assertEqual(seeded.data['items']['drive:f']['status'],'Skipped prior route')
        app.store().import_ledger(client,seeded.data)
        self.assertEqual(app.store().data()['uploads'][0]['youtube_id'],'ABCDEFGHIJK')

    def test_configurable_cap_survives_new_workers(self):
        github=base.MemoryGitHub();ledger=Ledger(github,'repo',base.CHANNEL,max_daily=3)
        for i in range(3):self.assertTrue(ledger.reserve(str(i),{'id':str(i)},'2026-10-17',str(i)))
        restarted=Ledger(github,'repo',base.CHANNEL,max_daily=3);restarted.load(required=True)
        self.assertFalse(restarted.reserve('4',{'id':'4'},'2026-10-17','4'))
        with self.assertRaises(ValueError):Ledger(github,'repo',base.CHANNEL,max_daily=101)

    def test_private_browser_uses_separate_profile_and_never_regular_fallback(self):
        with patch.object(private_browser,'candidates',return_value=iter([('chrome.exe','--incognito')])),patch.object(private_browser.subprocess,'Popen') as process,patch.object(private_browser.threading,'Thread'):
            self.assertTrue(private_browser.open_private('https://accounts.google.com/o/oauth2/v2/auth'))
            args=process.call_args.args[0];self.assertIn('--incognito',args);self.assertTrue(any(x.startswith('--user-data-dir=') for x in args))
        with patch.object(private_browser,'candidates',return_value=iter([])):
            self.assertFalse(private_browser.open_private('https://github.com/login/device'))
        with self.assertRaises(ValueError):private_browser.open_private('https://evil.example/login')






if __name__=='__main__':unittest.main(verbosity=2)
