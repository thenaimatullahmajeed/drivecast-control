"""Guided local connections, safe routing edits, polling settings and video stats."""
import copy
import json
import secrets
from github_api import Ledger
from video_stats import youtube_stats
from posting_schedule import options as schedule_options, smart_cron

POLL_INTERVALS=(5,10,15,20,30,60,120,180,360,720,1440)


def polling_cron(minutes):
    minutes=int(minutes)
    if minutes not in POLL_INTERVALS:raise ValueError('Choose one of the supported check intervals.')
    if minutes<60:return f'3-59/{minutes} * * * *'
    if minutes==1440:return '17 0 * * *'
    return '17 '+('*' if minutes==60 else '*/'+str(minutes//60))+' * * *'


def workflow(app,client):
    text=(app.ROOT/'.github/workflows/upload.yml').read_text(encoding='utf-8')
    cron=smart_cron(client) if client.get('schedule_mode','interval')=='smart' else polling_cron(client.get('check_minutes',60))
    return text.replace("cron: '17 * * * *'","cron: '"+cron+"'",1)


def draft(app,body):
    allowed=('id','name','folder_id','drive_connection','youtube_connection','requested_channel_name','channel_name_confirmed',
        'github_connection','repo_name','check_minutes','max_uploads_per_day','step','schedule_mode','schedule_timezone','schedule_windows','tags')
    previous=app.STATE.get('draft',{})
    value={k:body[k] for k in allowed if k in body}
    value['source_type']='drive'
    fields=('folder_id','drive_connection')
    if previous.get('source_verified') and all(previous.get(k,'')==value.get(k,'') for k in fields):
        value.update(source_verified=True,source_stats=previous.get('source_stats',{}))
    app.STATE['draft']=value
    app.save_local()


def validate_source(app,body):
    if body.get('source_type','drive')!='drive':raise ValueError('Only Google Drive sources are supported.')
    from drive_sources import selected
    root,folder=selected(app,app.helpers.parse_folder(str(body.get('folder_id',''))),body.get('drive_connection',''))
    client=dict(source_type='drive',drive_connection=root['connection_id'],folder_id=folder['id'])
    draft(app,dict(body,drive_connection=root['connection_id']))
    app.scan_source(client)
    app.STATE['draft'].update(source_verified=True,source_stats=client['source_stats'])
    app.save_local()
    return dict(source_verified=True,source_stats=client['source_stats'])






def validate_options(app,client,body):
    maximum=int(body.get('max_uploads_per_day',2));interval=int(body.get('check_minutes',60))
    if not 1<=maximum<=100:raise ValueError('Max uploads per day must be between 1 and 100.')
    polling_cron(interval)
    requested=str(body.get('requested_channel_name','')).strip()
    if requested and requested.casefold()!=client['channel_title'].strip().casefold() and body.get('channel_name_confirmed') is not True:
        raise ValueError('Authorized channel name differs. Confirm the displayed verified name and ID before saving.')
    client.update(privacy='public',max_uploads_per_day=maximum,check_minutes=interval,requested_channel_name=requested)
    scheduling,_=schedule_options({'schedule_mode':'interval',**client,**{k:body[k] for k in ('schedule_mode','schedule_timezone','schedule_windows') if k in body}})
    client.update(scheduling)


def preserve_route(app,old,new):
    """Pause old route only after validation; seed new routes without reuploading history."""
    if not old or not old.get('deployed'):return
    github=app.github_for_client(old)
    app.check_repository(github,old)
    runs=github.request(github.repo_path(old['repo_name'])+'/actions/runs?per_page=30').get('workflow_runs',[])
    if any(r['status']!='completed' for r in runs):raise ValueError('A worker is active. Wait for it to finish before saving edits.')
    old_ledger=Ledger(github,old['repo_name'],old['channel_id'],path=old.get('youtube_ledger_path','records/ledger.json'))
    old_ledger.load(required=True)
    new['ledger_seed']=copy.deepcopy(old_ledger.data)
    app.set_variable(github,old,'ENABLED','false')
    route_changed=any(old.get(k,'')!=new.get(k,'') for k in ('owner','repo_name','channel_id','source_type','folder_id'))
    if route_changed:
        new.setdefault('route_history',[]).append({k:old.get(k) for k in ('owner','repo_name','repo_id','channel_id','youtube_ledger_path','source_type','folder_id')})
    if (old['owner'],old['repo_name'])!=(new['owner'],new['repo_name']):
        known=next((r for r in new.get('route_history',[]) if (r.get('owner'),r.get('repo_name'))==(new['owner'],new['repo_name']) and r.get('repo_id')),None)
        if known:
            repo=app.github_for_client(new).repository(new['repo_name'])
            if not repo['private'] or repo['id']!=known['repo_id']:raise ValueError('Previous route repository identity/privacy changed. Stopped.')
            new.update(repo_id=repo['id'],repo_url=repo['html_url'],branch=repo['default_branch'],deployed=True,worker_version=3,
                youtube_ledger_path=known.get('youtube_ledger_path') or 'records/ledger.json')
            if known.get('channel_id')!=new['channel_id']:new['youtube_ledger_path']='records/youtube-'+new['channel_id']+'.json'
        else:
            for key in ('repo_id','repo_url','initial_head','branch'):new.pop(key,None)
            new.update(worker_version=1,deployed=False)
    elif old['channel_id']!=new['channel_id']:
        new['youtube_ledger_path']='records/youtube-'+new['channel_id']+'.json'


def initialize_ledgers(app,client,github):
    maximum=client.get('max_uploads_per_day',2)
    identity=dict(type='drive',id=client['folder_id'])
    for target,path,seed in [(client['channel_id'],client.get('youtube_ledger_path','records/ledger.json'),client.get('ledger_seed'))]:
        ledger=Ledger(github,client['repo_name'],target,path=path,max_daily=maximum)
        # A brand-new route may create a ledger, but an unchanged deployed route may not silently replace one.
        new_route=not client.get('deployed') or bool(seed)
        ledger.load(create=new_route,required=not new_route)
        if seed:
            for day,count in seed.get('daily',{}).items():ledger.data['daily'][day]=max(count,ledger.data['daily'].get(day,0))
            for key,item in seed.get('items',{}).items():
                if key in ledger.data['items']:continue
                if seed.get('channel_id')==target:
                    ledger.data['items'][key]=copy.deepcopy(item)
                else:
                    ledger.data['items'][key]=dict(item,status='Skipped prior route',youtube_id='',facebook_id='',error='Already processed before destination change; no automatic backfill.')
        identities=ledger.data.setdefault('source_identities',[])
        if identity not in identities:identities.append(identity)
        ledger.data.setdefault('source_identity',identity)
        ledger.save()
    client.pop('ledger_seed',None)


def sync_stats(app):
    rows=app.store().data()['uploads']
    for client in app.STATE['clients'].values():
        try:
            app.JOB['notice']='Refreshing recorded-video counters for '+client['name']
            cid=client.get('youtube_connection')
            if cid:
                with app.build('youtube','v3',credentials=app.google_credentials(cid),cache_discovery=False) as yt:
                    result=youtube_stats(yt,[r['youtube_id'] for r in rows if r['client_id']==client['id']])
                for video_id,values in result.items():app.store().set_stats(client['id'],'youtube',video_id,values,values.get('notice',''))
            client['video_stats_checked_at']=app.now()
        except Exception as error:
            client['stats_notice']=app.safe_error(str(error))
    app.STATE['last_stats_sync']=app.now()


def import_remote_stats(app,client,github):
    raw,_=github.read_file(client['repo_name'],'records/stats.json','drivecast-state',missing=True)
    if raw:
        stats=json.loads(raw)
        for platform in ('youtube',):
            for video_id,values in stats.get(platform,{}).items():
                app.store().set_stats(client['id'],platform,video_id,values,values.get('notice',''))
        if stats.get('channel'):client['stats']=dict(stats['channel'],checked_at=stats['checked_at'])
