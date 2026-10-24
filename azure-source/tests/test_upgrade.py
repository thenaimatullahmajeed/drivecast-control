"""Upgrade verification uses temporary workspaces and provider doubles only."""
import copy
import hashlib
import http.client
import json
import os
import secrets
import socket
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime,timezone,timedelta
from pathlib import Path
from unittest.mock import patch,MagicMock

ROOT=Path(__file__).resolve().parents[1]
(ROOT/'work').mkdir(exist_ok=True)
SOURCE=ROOT
sys.path.insert(0,str(SOURCE));sys.path.insert(0,str(ROOT/'tests'))
import accounts,upgrade,telegram_service as telegram,app
import oauth_web
import test_control_v2 as base
import test_console_backend as prior
from vault import Vault


class AccountTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(dir=ROOT/'work');self.manager=accounts.Accounts(self.tmp.name,'http://127.0.0.1:8998')
 def tearDown(self):
  self.manager.close()
  # Windows may release a terminated child's SQLite handles asynchronously.
  # Retry only temporary-fixture cleanup; persistent locks still fail the test.
  for attempt in range(10):
   try:self.tmp.cleanup();break
   except PermissionError:
    if attempt==9:raise
    time.sleep(.1)
 def test_argon2id_and_empty_distinct_workspaces_and_sessions(self):
  a=self.manager.signup('a@example.test','a-strong-password-123');b=self.manager.signup('b@example.test','b-strong-password-123')
  self.assertNotEqual(self.manager.workspace(a),self.manager.workspace(b));self.assertEqual(list(self.manager.workspace(b).iterdir()),[])
  token,csrf=self.manager.login('a@example.test','a-strong-password-123','127.0.0.1')
  self.assertEqual(self.manager.session(token)['user_id'],a)
  with self.manager.db() as db:
   row=db.execute('SELECT * FROM users WHERE id=?',(a,)).fetchone();self.assertTrue(row['password'].startswith('$argon2id$'));self.assertNotIn('a-strong',row['password'])
   self.assertNotEqual(db.execute('SELECT token FROM sessions').fetchone()[0],token)
  self.assertIsNone(self.manager.session(token+'wrong'));self.manager.logout(token);self.assertIsNone(self.manager.session(token))
 def test_lockout_and_email_reset_single_use_invalidates_session(self):
  uid=self.manager.signup('a@example.test','a-strong-password-123');token,_=self.manager.login('a@example.test','a-strong-password-123','test')
  for _ in range(8):
   with self.assertRaises(ValueError):self.manager.login('a@example.test','wrong','test')
  with self.assertRaisesRegex(ValueError,'Too many'):self.manager.login('a@example.test','a-strong-password-123','test')
  ticket=secrets.token_urlsafe(30)
  with self.manager.db() as db:db.execute('INSERT INTO tickets VALUES(?,?,?,?)',(self.manager.digest(ticket),uid,'reset',time.time()+60))
  self.manager.consume(ticket,'reset','new-strong-password-123');self.assertIsNone(self.manager.session(token))
  with self.assertRaises(ValueError):self.manager.consume(ticket,'reset','another-password-123')
 def test_backend_process_data_vault_and_jobs_are_isolated(self):
  a=self.manager.signup('a@example.test','a-strong-password-123');b=self.manager.signup('b@example.test','b-strong-password-123')
  pa,ta=self.manager.backend(a);pb,tb=self.manager.backend(b);self.assertNotEqual(pa,pb);self.assertNotEqual(ta,tb)
  def request(port,token,path,body=None):
   c=http.client.HTTPConnection('127.0.0.1',port);c.request('POST' if body else 'GET',path,json.dumps(body) if body else None,
    headers={'X-Setup-Token':token,'Content-Type':'application/json','Origin':f'http://127.0.0.1:{port}'})
   r=c.getresponse();result=(r.status,json.loads(r.read()));c.close();return result
  sa=request(pa,ta,'/api/state')[1];sb=request(pb,tb,'/api/state')[1]
  self.assertNotEqual(sa['workspace_id'],sb['workspace_id']);self.assertEqual(sb['clients'],{});self.assertEqual(sb['connections'],{})
  self.assertEqual(request(pb,ta,'/api/state')[0],403)
  request(pa,ta,'/api/unlock',{'password':'vault-for-a-only-123'})
  self.assertFalse(request(pb,tb,'/api/state')[1]['unlocked'])
  self.assertFalse((self.manager.workspace(b)/'connections.enc.json').exists())
  request(pa,ta,'/api/draft',{'name':'private-route-A'})
  for _ in range(30):
   if not request(pa,ta,'/api/state')[1]['job']['busy']:break
   time.sleep(.03)
  self.assertNotIn('private-route-A',json.dumps(request(pb,tb,'/api/state')[1]))
 def test_hosted_requires_tls_mail(self):
  with patch.dict(os.environ,{},clear=True),self.assertRaises(ValueError):accounts.Accounts(Path(self.tmp.name)/'hosted','http://example.test',True)
 def test_ticket_expiry_cannot_be_used(self):
  uid=self.manager.signup('a@example.test','a-strong-password-123')
  with self.manager.db() as db:db.execute('INSERT INTO tickets VALUES(?,?,?,?)',(self.manager.digest('expired'),uid,'reset',time.time()-1))
  with self.assertRaises(ValueError):self.manager.consume('expired','reset','long-new-password-123')
 def test_hosted_verification_delivers_one_time_email_and_secure_origin(self):
  env=dict(DRIVECAST_SMTP_HOST='smtp.example.test',DRIVECAST_SMTP_FROM='sender@example.test')
  with patch.dict(os.environ,env),patch.object(accounts.smtplib,'SMTP') as smtp:
   hosted=accounts.Accounts(Path(self.tmp.name)/'hosted','https://workspace.example.test',True)
   with hosted.db() as db: db.execute("INSERT INTO account_settings VALUES('public_signup','1')")
   uid=hosted.signup('new@example.test','new-strong-password-123')
   message=smtp.return_value.__enter__.return_value.send_message.call_args.args[0]
   ticket=message.get_content().split('#verify=')[1].split()[0]
   with hosted.db() as db:
    row=db.execute('SELECT * FROM tickets').fetchone();self.assertNotEqual(row['token'],ticket)
   with self.assertRaisesRegex(ValueError,'Verify'):hosted.login('new@example.test','new-strong-password-123','test')
   hosted.consume(ticket,'verify');token,_=hosted.login('new@example.test','new-strong-password-123','test')
   self.assertEqual(hosted.session(token)['user_id'],uid);hosted.close()


class GatewayTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.tmp=tempfile.TemporaryDirectory(dir=ROOT/'work')
  with socket.socket() as s:s.bind(('127.0.0.1',0));cls.port=s.getsockname()[1]
  cls.ready=threading.Event()
  def ready(server,manager):cls.server=server;cls.manager=manager;cls.ready.set()
  cls.thread=threading.Thread(target=accounts.serve,args=(cls.tmp.name,cls.port,ready),daemon=True);cls.thread.start();cls.ready.wait(10)
 @classmethod
 def tearDownClass(cls):cls.server.shutdown();cls.thread.join(10);cls.tmp.cleanup()
 def request(self,path,body=None,cookie='',csrf='',origin=None):
  c=http.client.HTTPConnection('127.0.0.1',self.port,timeout=20)
  headers={'Content-Type':'application/json','Origin':origin or f'http://127.0.0.1:{self.port}','Cookie':cookie,'X-Setup-Token':csrf}
  c.request('POST' if body is not None else 'GET',path,json.dumps(body) if body is not None else None,headers)
  r=c.getresponse();data=r.read();result=(r.status,dict(r.getheaders()),data);c.close();return result
 def test_gateway_requires_login_csrf_same_origin_and_cookie(self):
  self.assertEqual(self.request('/api/state')[0],401)
  self.assertEqual(self.request('/')[0],303)
  self.assertEqual(self.request('/auth/signup',{'email':'web@example.test','password':'web-strong-password-123'})[0],200)
  status,headers,raw=self.request('/auth/login',{'email':'web@example.test','password':'web-strong-password-123'})
  self.assertEqual(status,200);cookie=headers['Set-Cookie'].split(';')[0];csrf=json.loads(raw)['csrf'];self.assertIn('HttpOnly',headers['Set-Cookie']);self.assertIn('SameSite=Strict',headers['Set-Cookie'])
  self.assertEqual(self.request('/api/draft',{'name':'x'},cookie)[0],403)
  self.assertEqual(self.request('/api/draft',{'name':'x'},cookie,csrf,'https://evil.test')[0],403)
  self.assertEqual(self.request('/api/state',cookie=cookie)[0],200)
  html=self.request('/',cookie=cookie)[2].decode();self.assertIn(csrf,html)
  self.assertEqual(self.request('/auth/logout',{},cookie,csrf)[0],200)
  self.assertEqual(self.request('/api/state',cookie=cookie)[0],401)


class UpgradeTests(unittest.TestCase):
 def setUp(self):self.h=base.ControlTests();self.h.setUp();self.state=self.h.state
 def tearDown(self):self.h.tearDown()
 def pool_account(self,name,count=0,**health):
  cid=self.h.github(name);self.state['connections'][cid].update(capacity=2,health=dict(checked_at=app.now(),token_valid=True,rate_remaining=4000,usage_percent=20,**health))
  for i in range(count):self.state['clients'][name+str(i)]=dict(id=name+str(i),github_connection=cid)
  return cid
 def test_pool_fewest_healthy_free_slots_and_capacity(self):
  a=self.pool_account('a',1);b=self.pool_account('b',0);self.assertEqual(upgrade.assign(app),b)
  self.state['connections'][b]['health']['token_valid']=False;self.assertEqual(upgrade.assign(app),a)
  self.state['connections'][a]['capacity']=1
  with self.assertRaisesRegex(ValueError,'free slot'):upgrade.assign(app)
 def test_stale_health_cannot_auto_assign(self):
  cid=self.pool_account('a');self.state['connections'][cid]['health']['checked_at']=(datetime.now(timezone.utc)-timedelta(hours=2)).isoformat()
  with self.assertRaises(ValueError):upgrade.assign(app)
 def test_partial_health_and_lower_failover_threshold_are_not_healthy_targets(self):
  cid=self.pool_account('a');self.state['connections'][cid]['health']['rate_remaining']=None
  self.assertFalse(upgrade.pool(app)[0]['healthy'])
  self.state['connections'][cid]['health'].update(rate_remaining=4000,usage_percent=85)
  self.state['automation']=dict(failover_threshold=80)
  with self.assertRaises(ValueError):upgrade.assign(app)
 def test_bulk_keeps_going_after_one_failure_and_requires_confirmation(self):
  for cid in ('a','b'):self.state['clients'][cid]=dict(id=cid,name=cid,deployed=True)
  with patch.object(app,'client_action',side_effect=[ValueError('failed safely'),None]) as action:
   with self.assertRaises(ValueError):upgrade.bulk(app,dict(ids=['a','b'],action='pause'))
   result=upgrade.bulk(app,dict(ids=['a','b'],action='pause',confirmed=True))
   self.assertEqual([r['status'] for r in result['rows']],['Failed','Completed']);self.assertEqual(action.call_count,2)
 def test_csv_invalid_unknown_connections_and_duplicate_targets_previewed(self):
  self.state['connections']['yt']=dict(id='yt',kind='youtube',label='A',channel_id=base.CHANNEL)
  self.state['connections']['drive']=dict(id='drive',kind='drive',label='Drive')
  self.state['drive_root']=dict(folder_id='root',connection_id='drive',folders=[dict(id='child',name='Child')])
  text='name,youtube_connection,source_type,folder_id\nA,yt,drive,child\nB,yt,drive,child\nC,missing,drive,child\n'
  rows=upgrade.csv_preview(app,dict(csv=text))['rows'];self.assertFalse(rows[0]['blockers']);self.assertTrue(rows[1]['blockers']);self.assertTrue(rows[2]['blockers'])
 def test_failover_preview_never_executes_and_blocked_source_never_moves(self):
  a=self.pool_account('a');b=self.pool_account('b');self.state['connections'][a]['health']['usage_percent']=96
  self.state['clients']['c']=dict(id='c',name='C',github_connection=a,deployed=True)
  with patch.object(upgrade.operations,'migration_preview',return_value={'ready':False,'rows':[{'blockers':['Source token invalid; cannot fence']}]}) as preview,patch.object(upgrade.operations,'migrate') as migrate:
   self.assertEqual(upgrade.failover(app)['rows'][0]['status'],'Blocked');migrate.assert_not_called()
   self.state['automation']=dict(armed=True,auto_failover=True)
   self.assertEqual(upgrade.failover(app,True)['rows'][0]['status'],'Blocked');migrate.assert_not_called()
 def test_failover_requires_arm_and_uses_verified_handover(self):
  a=self.pool_account('a');b=self.pool_account('b');self.state['connections'][a]['health']['usage_percent']=96
  self.state['clients']['c']=dict(id='c',name='C',github_connection=a,deployed=True)
  with patch.object(upgrade.operations,'migration_preview',return_value={'ready':True,'rows':[{'blockers':[]}]}),patch.object(upgrade.operations,'migrate') as migrate:
   upgrade.failover(app,True);migrate.assert_not_called()
   self.state['automation']=dict(armed=True,auto_failover=True);upgrade.failover(app,True)
   self.assertEqual(migrate.call_args.args[1],dict(ids=['c'],target=b,confirmed=True))
 def test_automation_not_armed_without_confirmation(self):
  with self.assertRaises(ValueError):upgrade.automation(app,dict(armed=True))
  self.assertFalse(self.state.get('automation',{}).get('armed',False))
 def test_telegram_chunk_sizes_and_unknown_stats(self):
  message='🟢'*10000;chunks=list(telegram.chunks(message));self.assertEqual(''.join(chunks),message)
  self.assertTrue(all(len(c.encode('utf-16-le'))//2<=3500 for c in chunks))
  self.state['clients']['c']=dict(id='c',name='C',channel_id='exact-id',source_type='drive',folder_name='Creator videos')
  report=telegram.report(self.state,dict(uploads=[]));self.assertIn('Unavailable',report);self.assertNotIn('qa-secret',report);self.assertIn('exact-id',report)
 def test_telegram_pairing_only_private_chat_with_nonce(self):
  token='123456789:'+('a'*35)
  self.h.vault.put('telegram-bot',dict(token=token,chat_id='',pairing='one-time-code',offset=0))
  self.state['telegram']=dict(settings=dict(telegram.DEFAULTS))
  updates=[dict(update_id=1,message=dict(chat={'id':99,'type':'private'},text='/start wrong')),
           dict(update_id=2,message=dict(chat={'id':88,'type':'group'},text='/start one-time-code')),
           dict(update_id=3,message=dict(chat={'id':77,'type':'private','first_name':'QA'},text='/start one-time-code'))]
  with patch.object(telegram,'call',return_value=updates),patch.object(telegram,'send') as send:
   telegram.poll(app,True);self.assertEqual(self.h.vault.get('telegram-bot')['chat_id'],'77');self.assertTrue(self.state['telegram']['linked']);self.assertEqual(send.call_count,1)
  self.assertNotIn('one-time-code',json.dumps(app.public_state()))
 def test_telegram_worker_dry_run_never_sends(self):
  with patch.dict(os.environ,dict(DRY_RUN='true',TELEGRAM_BOT_TOKEN='present',TELEGRAM_CHAT_ID='1')),patch.object(telegram,'send') as send:
   telegram.worker_notifications();send.assert_not_called()
 def test_cached_stats_keep_provider_timestamp_after_failed_or_repeated_import(self):
  saved=app.store();saved.set_stats('c','youtube','video',dict(views=12,checked_at='2026-10-10T10:00:00+00:00'))
  saved.set_stats('c','youtube','video',dict(checked_at='2026-10-20T10:00:00+00:00'),'Provider unavailable')
  row=saved.data()['video_stats'][0];self.assertEqual(row['views'],'12');self.assertEqual(row['checked_at'],'2026-10-10T10:00:00+00:00')
 def test_avatar_rejects_local_host_and_private_dns_before_fetch(self):
  import avatars
  with self.assertRaises(ValueError):avatars.allowed('http://127.0.0.1:8765/api/state')
  with patch.object(avatars.socket,'getaddrinfo',return_value=[(2,1,6,'',('127.0.0.1',443))]):
   with self.assertRaises(ValueError):avatars.allowed('https://avatars.githubusercontent.com/example')
 def test_worker_notifications_reserve_before_send_and_do_not_repeat(self):
  gh=base.MemoryGitHub('operator')
  ledger=dict(version=1,channel_id=base.CHANNEL,items={'instagram:1':dict(youtube_id='abcdefghijk',status='Done',privacy='public',recorded_at='2026-10-20T10:00:00+00:00',title='Real title')},daily={},source_identity={'type':'instagram','id':'creator'})
  gh.files['repo','drivecast-state','records/ledger.json']=json.dumps(ledger).encode()
  env=dict(GITHUB_REPOSITORY='operator/repo',GITHUB_TOKEN='private-gh-token',EXPECTED_CHANNEL_ID=base.CHANNEL,DRY_RUN='false',FORCE_PRIVATE='false',
           TELEGRAM_BOT_TOKEN='123456789:'+('a'*35),TELEGRAM_CHAT_ID='77',TELEGRAM_SETTINGS=json.dumps(dict(not_before='2026-10-20T00:00:00+00:00')))
  import github_api
  with patch.dict(os.environ,env,clear=True),patch.object(github_api,'GitHub',return_value=gh),patch.object(telegram,'send') as send:
   telegram.worker_notifications();telegram.worker_notifications();self.assertEqual(send.call_count,1)
   self.assertIn('actual public',send.call_args.args[1]);self.assertNotIn('private-gh-token',send.call_args.args[1])
   self.assertIn(('repo','drivecast-state','records/telegram-notifications.json'),gh.files)


class FailoverLedgerTests(unittest.TestCase):
 def test_automatic_handover_keeps_reservations_no_double_worker_no_reupload(self):
  fixture=prior.MigrationTests();fixture.setUp()
  try:
   fixture.s['automation']=dict(armed=True,auto_failover=True)
   for cid in ('gh:operator','gh:target'):
    fixture.s['connections'][cid].update(capacity=2,health=dict(checked_at=app.now(),token_valid=True,rate_remaining=4000,usage_percent=96 if cid=='gh:operator' else 5))
   result=upgrade.failover(app,execute=True)
   self.assertEqual(result['rows'][0]['status'],'Moved')
   self.assertEqual(fixture.variables['operator','ENABLED'],'false');self.assertEqual(fixture.variables['target','ENABLED'],'true')
   self.assertLess(fixture.events.index('disable-source-workflow'),fixture.events.index(('target','ENABLED','true')))
   from github_api import Ledger
   ledger=Ledger(fixture.dest,'test-pipeline',base.CHANNEL);ledger.load(required=True)
   self.assertEqual(ledger.data,fixture.original)
   before=fixture.dest.writes
   self.assertFalse(ledger.reserve('instagram:1',dict(title='Must never repeat'),'2026-10-20','new-reservation'))
   self.assertEqual(fixture.dest.writes,before)
  finally:fixture.tearDown()


class WebOAuthTests(unittest.TestCase):
 def test_one_time_state_requires_matching_provider_and_expires(self):
  event=threading.Event();oauth_web.PENDING['nonce']=dict(provider='google',event=event,deadline=time.monotonic()+30)
  try:
   with self.assertRaises(ValueError):oauth_web.receive(dict(state='nonce',provider='meta',code='wrong'))
   self.assertFalse(event.is_set())
   oauth_web.receive(dict(state='nonce',provider='google',code='one-use-code'));self.assertTrue(event.is_set())
   with self.assertRaises(ValueError):oauth_web.receive(dict(state='nonce',provider='google',code='second-code'))
   self.assertEqual(oauth_web.PENDING['nonce']['code'],'one-use-code')
  finally:oauth_web.PENDING.pop('nonce',None)
  oauth_web.PENDING['expired']=dict(provider='meta',event=threading.Event(),deadline=time.monotonic()-1)
  try:
   with self.assertRaises(ValueError):oauth_web.receive(dict(state='expired',provider='meta',code='code'))
  finally:oauth_web.PENDING.pop('expired',None)
 def test_hosted_google_uses_exact_redirect_pkce_and_offline_scope_check(self):
  import io
  config={'web':dict(client_id='client.apps.googleusercontent.com',client_secret='not-printed')}
  response=MagicMock();response.__enter__.return_value=io.BytesIO(json.dumps(dict(refresh_token='fixture-refresh',scope='one two')).encode())
  with patch.dict(os.environ,dict(DRIVECAST_PUBLIC_CALLBACK_BASE='https://workspace.example.test/oauth/'+'a'*32)),patch.object(oauth_web,'wait',return_value='fixture-code') as wait,patch.object(oauth_web,'urlopen',return_value=response):
   result=oauth_web.google(config,['one','two'],lambda _:None)
   params=wait.call_args.args[1];self.assertTrue(params['redirect_uri'].endswith('/google'));self.assertEqual(params['code_challenge_method'],'S256');self.assertEqual(params['access_type'],'offline');self.assertEqual(result['REFRESH_TOKEN'],'fixture-refresh')
 def test_web_oauth_requires_https_gateway(self):
  with patch.dict(os.environ,{},clear=True),self.assertRaises(ValueError):oauth_web.callback_url('google')
 def test_hosted_gateway_delivers_decline_to_exact_pending_workspace_without_provider_calls(self):
  with tempfile.TemporaryDirectory(dir=ROOT/'work') as temp,patch.dict(os.environ,dict(DRIVECAST_HOSTED='1',DRIVECAST_ORIGIN='https://workspace.example.test',
       DRIVECAST_SMTP_HOST='smtp.example.test',DRIVECAST_SMTP_FROM='sender@example.test')),patch.object(accounts.smtplib,'SMTP'):
   with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
   ready=threading.Event();holder={}
   def callback(server,manager):holder.update(server=server,manager=manager);ready.set()
   thread=threading.Thread(target=accounts.serve,args=(temp,port,callback),daemon=True);thread.start();self.assertTrue(ready.wait(10))
   try:
    manager=holder['manager'];uid=manager.signup('hosted@example.test','hosted-password-123',manager.invite('hosted@example.test'))
    with manager.db() as db:db.execute('UPDATE users SET verified=1 WHERE id=?',(uid,))
    token,csrf=manager.login('hosted@example.test','hosted-password-123','test')
    def request(path,body=None):
     c=http.client.HTTPConnection('127.0.0.1',port,timeout=20)
     c.request('POST' if body is not None else 'GET',path,json.dumps(body) if body is not None else None,headers={
       'Host':'workspace.example.test','Origin':'https://workspace.example.test','Cookie':'drivecast_session='+token,
       'X-Setup-Token':csrf,'Content-Type':'application/json'})
     r=c.getresponse();raw=r.read();status=r.status;c.close();return status,json.loads(raw) if raw else {}
    request('/api/unlock',dict(password='hosted-vault-password-123'))
    result=request('/api/google',dict(kind='youtube',label='Intended channel',oauth={'web':dict(client_id='test.apps.googleusercontent.com',client_secret='fixture-secret')}))
    self.assertEqual(result[0],202)
    state={}
    for _ in range(60):
     state=request('/api/state')[1]
     if state['job'].get('auth_url'):break
     time.sleep(.05)
    from urllib.parse import urlparse,parse_qs,urlencode
    params=parse_qs(urlparse(state['job']['auth_url']).query)
    self.assertEqual(params['redirect_uri'][0],'https://workspace.example.test/oauth/'+uid+'/google')
    self.assertEqual(request('/oauth/'+('f'*32)+'/google?state=wrong&error=access_denied')[0],410)
    self.assertTrue(request('/api/state')[1]['job']['busy'])
    query=urlencode(dict(state=params['state'][0],error='access_denied'))
    self.assertEqual(request('/oauth/'+uid+'/google?'+query)[0],303)
    for _ in range(60):
     state=request('/api/state')[1]
     if not state['job']['busy']:break
     time.sleep(.05)
    self.assertFalse(state['job']['busy']);self.assertEqual(state['job']['outcome'],'error');self.assertEqual(state['connections'],{})
    self.assertEqual(request('/oauth/'+uid+'/google?'+query)[0],400)
   finally:holder['server'].shutdown();thread.join(10)


if __name__=='__main__':unittest.main()
