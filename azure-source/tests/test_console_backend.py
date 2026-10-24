import copy,json,os,sys,unittest
from datetime import datetime,date,timedelta,timezone
from pathlib import Path
from unittest.mock import patch,MagicMock
ROOT=Path(__file__).resolve().parents[1]
(ROOT/'work').mkdir(exist_ok=True)
sys.path.insert(0,str(ROOT/'tests'))
import test_control_v2 as base
import app,operations,posting_schedule as scheduling
from github_api import GitHubError


class ScheduleTests(unittest.TestCase):
 def test_varied_deterministic_dates_channels_and_zones(self):
  c=dict(channel_id='channel-one',schedule_timezone='America/Los_Angeles',max_uploads_per_day=2)
  slots=scheduling.slots(c,date(2026,11,1))
  self.assertEqual(slots,scheduling.slots(c,date(2026,11,1)))
  self.assertNotEqual(slots,scheduling.slots(dict(c,channel_id='other'),date(2026,11,1)))
  self.assertNotEqual(slots,scheduling.slots(c,date(2026,11,2)))
  for t in slots:
   local=t.astimezone(scheduling.ZoneInfo(c['schedule_timezone']))
   self.assertIn(local.hour,(12,13,18,19,20))
  self.assertEqual(len(set(slots)),2)
 def test_due_slot_never_batches_or_repeats_a_failed_reservation(self):
  c=dict(channel_id='one',schedule_timezone='UTC',schedule_windows='18:00-21:00')
  first,second=scheduling.slots(c,date(2026,10,20))
  env=dict(SCHEDULE_MODE='smart',SCHEDULE_TIMEZONE='UTC',SCHEDULE_WINDOWS='18:00-21:00',EXPECTED_CHANNEL_ID='one',DRY_RUN='false')
  ledger={'items':{}}
  with patch.dict(os.environ,env,clear=True):
   self.assertEqual(scheduling.worker_allowance(ledger,2,first-timedelta(seconds=1)),0)
   self.assertEqual(scheduling.worker_allowance(ledger,2,first+timedelta(seconds=1)),1)
   ledger['items']['x']=dict(reserved_at=first.isoformat(),recorded_at=first.isoformat(),status='Failed')
   self.assertEqual(scheduling.worker_allowance(ledger,2,first+timedelta(seconds=1)),0)
   self.assertEqual(scheduling.worker_allowance(ledger,2,second+timedelta(seconds=1)),1)
   self.assertEqual(scheduling.worker_allowance(ledger,2,datetime(2026,10,21,1,tzinfo=timezone.utc)),0)
 def test_next_slot_rolls_to_tomorrow_when_daily_slots_used(self):
  c=dict(schedule_timezone='UTC',schedule_windows='18:00-21:00',channel_id='one')
  now=datetime(2026,10,20,10,tzinfo=timezone.utc)
  self.assertEqual(datetime.fromisoformat(scheduling.next_slot(c,now,2)).date(),date(2026,10,21))
 def test_invalid_windows_or_timezone_rejected(self):
  for value in [{'schedule_timezone':'bad-zone'},{'schedule_windows':'22:00-01:00'},{'schedule_windows':'12:00-13:00,12:30-14:00'},{'schedule_mode':'random'}]:
   with self.assertRaises(ValueError):scheduling.options(value)
 def test_cron_covers_standard_and_dst_hours(self):
  c=dict(schedule_timezone='America/Los_Angeles',schedule_windows='18:00-21:00')
  hours=set(map(int,scheduling.smart_cron(c).split()[1].split(',')))
  self.assertTrue({1,2,3,4}.issubset(hours))
 def test_stopped_and_legacy_do_not_claim_a_publication_time(self):
  self.assertEqual(scheduling.public_schedule({'enabled':False})['next_at'],'')
  legacy=scheduling.public_schedule({'enabled':True,'check_minutes':60},datetime(2026,10,20,10,18,tzinfo=timezone.utc))
  self.assertEqual(legacy['label'],'Next source check')
  self.assertEqual(legacy['next_at'],'2026-10-20T11:17:00+00:00')


class MigrationTests(unittest.TestCase):
 def setUp(self):
  self.h=base.ControlTests();self.h.setUp();self.s=self.h.state
  source=self.h.github('operator');target=self.h.github('target')
  self.h.vault.put('yt',dict(CLIENT_ID='client',CLIENT_SECRET='secret',REFRESH_TOKEN='refresh'))
  self.h.vault.put('drive',dict(CLIENT_ID='drive-client',CLIENT_SECRET='drive-secret',REFRESH_TOKEN='drive-refresh'))
  self.s['connections']['drive']=dict(id='drive',kind='drive',status='Connected')
  self.client=dict(id='one',name='Channel one',source_type='drive',channel_id=base.CHANNEL,youtube_connection='yt',drive_connection='drive',folder_id='client',
    github_connection=source,owner='operator',repo_name='test-pipeline',repo_id=1,branch='main',enabled=True,deployed=True)
  self.s['clients']['one']=self.client
  self.source=base.MemoryGitHub('operator');self.dest=base.MemoryGitHub('target');self.target_created=False;self.variables={};self.events=[]
  ledger=dict(version=1,channel_id=base.CHANNEL,items={'instagram:1':dict(youtube_id='abcdefghijk',status='Done',reserved_at=base.STAMP)},daily={'2026-10-17':1},source_identity={'type':'instagram','id':'creator'})
  self.source.files[('test-pipeline','drivecast-state','records/ledger.json')]=json.dumps(ledger).encode()
  self.original=copy.deepcopy(ledger)
  def repository(repo):
   if not self.target_created:raise GitHubError(404)
   return dict(id=2,private=True,default_branch='main',html_url='https://github.com/target/test-pipeline')
  self.dest.repository=repository
  def create(repo,description,auto_init=True):self.assertFalse(auto_init);self.target_created=True;self.events.append('create');return repository(repo)
  self.dest.create_private=create
  source_request=self.source.request;dest_request=self.dest.request
  def request_source(path,method='GET',body=None):
   if path.endswith('/actions/workflows/upload.yml/disable') and method=='PUT':self.events.append('disable-source-workflow');return {}
   if path.endswith('/actions/workflows/upload.yml'):return {'state':'disabled_manually'}
   if path.endswith('/actions/variables/ENABLED'):return {'value':self.variables.get(('operator','ENABLED'),'true')}
   return source_request(path,method,body)
  self.source.request=request_source
  def request_dest(path,method='GET',body=None):
   if method=='PATCH' and path==self.dest.repo_path('test-pipeline'):return {}
   return dest_request(path,method,body)
  self.dest.request=request_dest
  def set_variable(g,c,k,v):self.events.append((g.owner,k,v));self.variables[g.owner,k]=v
  def git(args,token,cwd=None):
   self.events.append(args[0])
   if args[0]=='push':self.dest.files.update(copy.deepcopy(self.source.files))
   if args[0] in ('show-ref','ls-remote'):return 'abc refs/heads/main\nxyz refs/heads/drivecast-state\nttt refs/tags/v1\n'
   return ''
  self.patches=[patch.object(app,'gh_for',side_effect=lambda cid:self.dest if cid=='gh:target' else self.source),
    patch.object(app,'github_for_client',side_effect=lambda c:self.dest if c['owner']=='target' else self.source),
    patch.object(app,'set_variable',side_effect=set_variable),patch.object(app,'cli',return_value=''),patch.object(operations,'git_command',side_effect=git),patch.object(operations.shutil,'which',return_value='git')]
  for p in self.patches:p.start()
 def tearDown(self):
  for p in reversed(self.patches):p.stop()
  self.h.tearDown()
 def test_missing_source_secret_blocks_before_pause_or_repo_creation(self):
  self.client.update(source_type='drive',drive_connection='',folder_id='folder')
  preview=operations.migration_preview(app,dict(source='gh:operator',target='gh:target'))
  self.assertFalse(preview['ready']);self.assertEqual(self.events,[]);self.assertTrue(self.client['enabled'])
 def test_existing_target_is_never_overwritten(self):
  self.target_created=True
  self.assertFalse(operations.migration_preview(app,dict(source='gh:operator',target='gh:target'))['ready'])
  self.assertEqual(self.events,[])
 def test_confirm_required(self):
  with self.assertRaises(ValueError):operations.migrate(app,dict(source='gh:operator',target='gh:target'))
  self.assertEqual(self.events,[])
 def test_complete_handover_preserves_all_refs_ledgers_and_original(self):
  out=operations.migrate(app,dict(source='gh:operator',target='gh:target',confirmed=True))
  self.assertEqual(out,dict(migrated=1));self.assertEqual(self.client['owner'],'target');self.assertTrue(self.client['enabled'])
  self.assertLess(self.events.index(('operator','ENABLED','false')),self.events.index('clone'))
  self.assertLess(self.events.index('disable-source-workflow'),self.events.index('clone'))
  self.assertLess(self.events.index('ls-remote'),self.events.index(('target','ENABLED','true')))
  self.assertEqual(self.variables['operator','ENABLED'],'false')
  self.assertEqual(json.loads(self.dest.files['test-pipeline','drivecast-state','records/ledger.json']),self.original)
  self.assertEqual(json.loads(self.source.files['test-pipeline','drivecast-state','records/ledger.json']),self.original)
 def test_copy_failure_never_enables_target_and_preserves_original_records(self):
  operations.git_command.side_effect=ValueError('copy failed')
  with self.assertRaisesRegex(ValueError,'copy failed'):operations.migrate(app,dict(source='gh:operator',target='gh:target',confirmed=True))
  self.assertFalse(self.client['enabled']);self.assertEqual(self.client['owner'],'operator')
  self.assertNotIn(('target','ENABLED','true'),self.events)
  self.assertEqual(self.s['migrations'][-1]['status'],'Needs attention')
 def test_mismatched_mirrored_refs_stop_before_activation(self):
  old=operations.git_command.side_effect
  operations.git_command.side_effect=lambda args,*a,**kw:'different refs/heads/main' if args[0]=='ls-remote' else old(args,*a,**kw)
  with self.assertRaisesRegex(ValueError,'verification failed'):operations.migrate(app,dict(source='gh:operator',target='gh:target',confirmed=True))
  self.assertNotIn(('target','ENABLED','true'),self.events)




class MirrorTests(unittest.TestCase):
 def test_real_git_mirror_preserves_state_branch_history_and_annotated_tag(self):
  import tempfile,subprocess
  with tempfile.TemporaryDirectory(dir=ROOT/'work') as directory:
   folder=Path(directory);source=folder/'source';copyrepo=folder/'mirror.git';dest=folder/'destination.git'
   source.mkdir()
   def git(*args):
    return subprocess.run(['git','-c','user.name=QA','-c','user.email=qa@example.invalid',*map(str,args)],cwd=source,check=True,capture_output=True).stdout.decode()
   git('init');(source/'ledger.json').write_text('{"items":{"original":{"youtube_id":"abcdefghijk"}},"daily":{"2026-10-20":1}}')
   git('add','ledger.json');git('commit','-m','Original reserved history');git('branch','drivecast-state');git('tag','-a','v1','-m','Original tag')
   git('init','--bare',dest)
   operations.git_command(['clone','--mirror',str(source),str(copyrepo)],'qa-token')
   before=operations.git_command(['show-ref','--heads','--tags'],'qa-token',cwd=copyrepo)
   operations.git_command(['push',str(dest),'+refs/heads/*:refs/heads/*','+refs/tags/*:refs/tags/*'],'qa-token',cwd=copyrepo)
   after=operations.git_command(['ls-remote','--heads','--tags',str(dest)],'qa-token')
   refs=lambda text:{tuple(line.split()) for line in text.splitlines() if not line.endswith('^{}')}
   self.assertEqual(refs(before),refs(after));self.assertIn('drivecast-state',after);self.assertIn('refs/tags/v1',after)

if __name__=='__main__':unittest.main()
