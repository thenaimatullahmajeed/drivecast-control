import os,sys,json,time,tempfile,threading
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import patch,MagicMock
from contextlib import nullcontext
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cloud_store import CloudStore
import cloud_control,cloud_runner

class LiveControls(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.db=CloudStore(self.root/'records.sqlite3')
        self.c=dict(id='r',channel_id='UC'+'a'*22,folder_id='folder',drive_connection='d',drive_root_id='root',youtube_connection='yt',name='Test',max_uploads_per_day=2,project_id='p')
        self.db.save_route(self.c,True)
        self.drive=SimpleNamespace(files=lambda:SimpleNamespace(get=lambda **k:SimpleNamespace(execute=lambda **kw:dict(capabilities={'canAddChildren':True}))))
        self.app=SimpleNamespace(WORK=self.root,STATE={'clients':{},'drive_root':{'folder_id':'root','connection_id':'d'}},connection=lambda *a:{'channel_id':self.c['channel_id']},google_credentials=lambda *a:object(),build=lambda *a,**k:nullcontext(self.drive),now=lambda:'2026-10-23T19:00:00+00:00',save_local=MagicMock())
        self.alive=threading.Event();self.alive.set()
        self.videos=[dict(id=str(i),name=str(i)+'.mp4',size='100',mimeType='video/mp4') for i in range(3)]
    def tearDown(self):self.temp.cleanup()
    def plan(self,clock=None):return cloud_control.publication_plan(self.db,self.db.routes()[0],self.db.snapshot()['jobs'],clock)
    def test_force_now_from_paused_is_one_public_video_and_stays_paused(self):
        self.db.enqueue('r','now');job=self.db.claim('worker')
        with patch.dict(os.environ,{'DRIVECAST_LIVE_UPLOADS':'1'}),patch('main.verify_channel'),patch('drive_sources.verify_ancestry'),patch('main.list_files',return_value=self.videos),patch('main.get_folders',return_value={}),patch('main.process',return_value=True) as process,patch('cloud_runner.mirror_records'),patch('telegram_service.notify'):
            cloud_runner.execute(self.app,self.db,job,self.alive)
            self.assertEqual(process.call_count,1);self.assertEqual(process.call_args.args[6],'public');self.assertEqual(os.environ['FORCE_PUBLIC'],'true')
        self.assertFalse(self.db.routes()[0]['enabled']);self.assertEqual(self.db.snapshot()['reservations'],1)
        self.assertEqual(self.db.meta('queue:r')['candidates'][0]['name'],'1.mp4')
    def test_force_now_daily_limit_is_not_bypassed(self):
        self.db.enqueue('r','now');job=self.db.claim('worker')
        day=datetime.now(ZoneInfo('America/Los_Angeles')).date().isoformat()
        for key in ('drive:a','drive:b'):
            self.assertTrue(self.db.reserve(job,self.c,key,{},day,2))
            self.db.update_reservation(job,self.c['channel_id'],key,'done')
        self.db.finish(job)
        with self.assertRaisesRegex(ValueError,'Daily channel limit'):self.db.enqueue('r','now')
    def test_two_force_clicks_share_one_active_job(self):
        one=self.db.enqueue('r','now');self.assertIsNotNone(one);self.assertIsNone(self.db.enqueue('r','now'))
        self.assertEqual(len(self.db.snapshot()['jobs']),1)
    def test_force_requires_verified_setup(self):
        with self.db.db(True) as db:db.execute('UPDATE cloud_routes SET history_verified=0')
        with self.assertRaisesRegex(ValueError,'confirmed'):self.db.enqueue('r','now')
    def test_project_limit_is_not_bypassed(self):
        with patch.dict(os.environ,{'DRIVECAST_PROJECT_DAILY_LIMIT':'0'}):
            with self.assertRaisesRegex(ValueError,'project limit'):self.db.enqueue('r','now')
    def test_live_gate_and_explicit_confirmation_are_enforced(self):
        with patch.dict(os.environ,{'DRIVECAST_LIVE_UPLOADS':'0'}):
            with self.assertRaisesRegex(ValueError,'approved'):cloud_control.action(self.app,dict(id='r',action='post_now',confirmed=True))
        with self.assertRaisesRegex(ValueError,'Confirm'):cloud_control.action(self.app,dict(id='r',action='post_now',confirmed=False))
    def test_uncertain_upload_blocks_force(self):
        self.db.enqueue('r','now');job=self.db.claim('worker');day=datetime.now(ZoneInfo('America/Los_Angeles')).date().isoformat()
        self.db.reserve(job,self.c,'drive:a',{},day,2);self.db.update_reservation(job,self.c['channel_id'],'drive:a','uncertain');self.db.finish(job,'attention')
        with self.assertRaisesRegex(ValueError,'quarantined'):self.db.enqueue('r','now')
        self.assertEqual(self.plan()['kind'],'attention')
    def test_scheduler_ignores_recent_dry_check_and_does_not_double_queue(self):
        self.db.enqueue('r','dry');job=self.db.claim('worker');self.db.finish(job);self.db.set_enabled('r',True)
        with patch.dict(os.environ,{'DRIVECAST_LIVE_UPLOADS':'1'}):cloud_runner.schedule(self.db);self.db.set_meta('scheduler:last',0);cloud_runner.schedule(self.db)
        self.assertEqual(sum(j['mode']=='public' for j in self.db.snapshot()['jobs']),1)
    def test_active_upload_has_no_fake_finish_countdown(self):
        self.db.enqueue('r','now');self.db.claim('worker');p=self.plan()
        self.assertEqual(p['label'],'Posting now');self.assertEqual(p['next_at'],'')
    def test_next_check_and_video_name_come_from_durable_queue(self):
        self.db.set_enabled('r',True);due=time.time()+240;self.db.set_meta('next-check:r',due)
        self.db.set_meta('queue:r',dict(candidates=[dict(id='a',name='Next clip.mp4')],eligible=1,checked_at='now'))
        p=self.plan();self.assertEqual(p['next_video']['name'],'Next clip.mp4');self.assertEqual(p['label'],'Next video check')
        self.assertAlmostEqual(datetime.fromisoformat(p['next_at']).timestamp(),due,places=3)
    def test_opted_in_new_setup_starts_only_after_exact_confirmation(self):
        import cloud_migration
        self.app.STATE['cloud_settings']={'auto_start_confirmed':True}
        records=MagicMock();records.data.return_value={'uploads':[]};self.app.store=lambda:records
        self.app.check_google=MagicMock();self.app.scan_source=MagicMock()
        self.db.set_meta('dry:r',dict(checked_at='now',folder_id='folder',channel_id=self.c['channel_id']))
        with patch.dict(os.environ,{'DRIVECAST_LIVE_UPLOADS':'1'}):
            with self.assertRaisesRegex(ValueError,'Explicitly confirm'):cloud_migration.commit(self.app,dict(id='r',confirmed=False))
            result=cloud_migration.commit(self.app,dict(id='r',confirmed=True,other_runners_stopped=True))
        self.assertTrue(self.db.routes()[0]['enabled']);self.assertTrue(self.app.STATE['clients']['r']['enabled'])
        self.assertEqual(self.db.snapshot()['jobs'][0]['mode'],'public');self.assertIn('on',result['notice'])

class AvatarRefresh(unittest.TestCase):
    def test_explicit_refresh_fetches_photo_even_with_fresh_cache(self):
        import avatars
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);folder=root/'avatars';folder.mkdir();key=__import__('hashlib').sha256(b'yt').hexdigest()
            (folder/(key+'.img')).write_bytes(b'old');(folder/(key+'.type')).write_text('image/png')
            response=MagicMock();response.headers.get_content_type.return_value='image/png';response.read.return_value=b'new-image'
            opener=MagicMock();opener.open.return_value.__enter__.return_value=response
            with patch.object(avatars,'allowed'),patch.object(avatars,'build_opener',return_value=opener):
                avatars.cache(root,'yt','https://yt3.googleusercontent.com/image');opener.open.assert_not_called()
                avatars.cache(root,'yt','https://yt3.googleusercontent.com/image',force=True);opener.open.assert_called_once()
            self.assertEqual((folder/(key+'.img')).read_bytes(),b'new-image')

if __name__=='__main__':unittest.main()

