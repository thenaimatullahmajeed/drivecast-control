"""Cloud control plane. No remote worker mutation or live adoption is implicit."""
import copy
import os
from pathlib import Path
import re
import secrets
import shutil
import time
from datetime import datetime, date, timedelta, timezone, time as datetime_time
from zoneinfo import ZoneInfo

from channel_matching import match
from cloud_store import CloudStore


def store(app): return CloudStore(app.WORK/'records.sqlite3')


def publication_plan(db,route,jobs,clock=None):
    clock=time.time() if clock is None else clock
    today=datetime.fromtimestamp(clock,ZoneInfo('America/Los_Angeles')).date()
    config=route['config'];maximum=int(config.get('max_uploads_per_day',2))
    with db.db() as sql:
        used=db.used(sql,route['channel_id'],today.isoformat())
        uncertain=sql.execute("SELECT count(*) FROM cloud_reservations WHERE route_id=? AND status IN ('reserved','uncertain')",(route['id'],)).fetchone()[0]
        active=sql.execute("SELECT mode,state FROM cloud_jobs WHERE route_id=? AND state IN ('queued','running') ORDER BY id DESC LIMIT 1",(route['id'],)).fetchone()
    queue=db.meta('queue:'+route['id'],db.meta('dry:'+route['id'],{}))
    result=dict(daily_limit=maximum,daily_used=used,daily_remaining=max(0,maximum-used),next_at='',next_video=(queue.get('candidates') or [None])[0],queue_checked_at=queue.get('checked_at',''),eligible=queue.get('eligible'),label='Paused',kind='paused')
    if active:
        result.update(kind='active',label='Checking Drive' if active['mode']=='dry' else 'Posting now' if active['state']=='running' else 'Queued for posting')
    elif uncertain:result.update(kind='attention',label='Upload needs review')
    elif not route['history_verified']:result.update(kind='setup',label='Confirm channel setup')
    elif not route['enabled']:pass
    elif used>=maximum:
        midnight=datetime.combine(today+timedelta(days=1),datetime_time.min,ZoneInfo('America/Los_Angeles'))
        result.update(kind='limit',label='Daily limit reached · resets at midnight Pacific',next_at=midnight.astimezone(timezone.utc).isoformat())
    else:
        due=db.next_due(route['id'])
        result.update(kind='scheduled',label='Next video check' if result['next_video'] else 'Next Drive check · waiting for videos',next_at=datetime.fromtimestamp(due or clock,timezone.utc).isoformat())
    return result


def state(app):
    db = store(app)
    snapshot = db.snapshot()
    for route in db.routes():
        route['dry_run'] = db.meta('dry:'+route['id'], {})
        route['plan']=publication_plan(db,route,snapshot['jobs'])
        snapshot.setdefault('routes', []).append(route)
    snapshot.update(hosted=os.getenv('DRIVECAST_HOSTED')=='1', live_uploads=os.getenv('DRIVECAST_LIVE_UPLOADS','0')=='1',auto_start_confirmed=bool(app.STATE.get('cloud_settings',{}).get('auto_start_confirmed')),
        version=os.getenv('DRIVECAST_VERSION','azure-v1'), disk_free=shutil.disk_usage(app.WORK).free, disk_total=shutil.disk_usage(app.WORK).total,
        last_backup=db.meta('backup:last', {}), matching=match(app.STATE),
        trial_ends=os.getenv('DRIVECAST_TRIAL_END','2026-11-20'), worker_capacity=int(os.getenv('DRIVECAST_WORKERS','2')))
    snapshot['trial_days_remaining']=((date.fromisoformat(snapshot['trial_ends'])-datetime.now(ZoneInfo('America/Los_Angeles')).date()).days if snapshot['trial_ends'] else None)
    root = Path(os.getenv('DRIVECAST_DATA_DIR', app.WORK))
    snapshot['services'] = {name: (float((root/(name+'-heartbeat')).read_text()) if (root/(name+'-heartbeat')).exists() else None) for name in ('worker','scheduler')}
    return snapshot


def stage(app, body):
    """Creates an independent paused cloud proposal, including for existing channels."""
    import drive_sources
    root, folder = drive_sources.selected(app, str(body.get('folder_id','')))
    yt = app.connection(body.get('youtube_connection'), 'youtube')
    if not re.fullmatch(r'UC[A-Za-z0-9_-]{22}', yt.get('channel_id','')): raise ValueError('Verify an exact YouTube channel ID first.')
    if body.get('channel_id') != yt['channel_id'] or body.get('confirmed') is not True:
        raise ValueError('Confirm the displayed folder and exact destination channel ID.')
    maximum = int(body.get('max_uploads_per_day',2))
    if not 1 <= maximum <= 20: raise ValueError('Choose a daily limit between 1 and 20 upload attempts.')
    # Fresh read-only identity verification; no existing client is edited.
    app.check_google(yt['id'])
    original = next((c for c in app.STATE.get('clients',{}).values() if c.get('channel_id') == yt['channel_id']), None)
    config = dict(id='az-'+yt['channel_id'], name=yt['label'], channel_title=yt['label'], channel_id=yt['channel_id'],
        youtube_connection=yt['id'], folder_id=folder['id'], folder_path=folder.get('path',folder['name']),
        drive_connection=root['connection_id'], drive_root_id=root['folder_id'], privacy='public',
        max_uploads_per_day=maximum, project_id=yt.get('project_id','unknown-project'),
        original_client_id=original['id'] if original else '', runner='azure', owner='Azure', repo_name='cloud-runner')
    db = store(app)
    if any(r['folder_id']==folder['id'] and r['channel_id']!=yt['channel_id'] for r in db.routes()):
        raise ValueError('This Drive folder already belongs to another exact YouTube destination. Review its route before reassigning it.')
    db.save_route(config, history_verified=False)
    db.enqueue(config['id'], 'dry')
    return dict(updated=True, id=config['id'], notice='Cloud preview saved. Existing route unchanged. Only a read-only dry run was queued.')


def action(app, body):
    db = store(app); rid = body.get('id'); operation = body.get('action')
    route = next((r for r in db.routes() if r['id']==rid), None)
    if not route: raise ValueError('Cloud route not found.')
    if operation == 'dry_run': db.enqueue(rid, 'dry')
    elif operation == 'pause': db.set_enabled(rid, False)
    elif operation in ('enable','private_test','post_now'):
        if body.get('confirmed') is not True: raise ValueError('Confirm this exact cloud route before publishing.')
        if os.getenv('DRIVECAST_LIVE_UPLOADS','0') != '1': raise ValueError('Dry-only deployment: live publishing has not been approved.')
        if operation in ('private_test','post_now'):
            job_id=db.enqueue(rid,'private' if operation=='private_test' else 'now')
            if job_id:db.set_meta('next-check:'+rid,time.time()+300)
        else:
            db.set_enabled(rid,True)
            job_id=db.enqueue(rid,'public')
            if job_id:db.set_meta('next-check:'+rid,time.time()+300)
    else: raise ValueError('Choose Dry run, Pause or approved Start.')
    for client in app.STATE.get('clients',{}).values():
        if client.get('cloud_route_id')==rid:
            enabled=operation=='enable'
            if operation in ('enable','pause'): client.update(enabled=enabled,status='Enabled' if enabled else 'Paused')
    app.save_local()
    return dict(updated=True,notice='Checking Drive for one new Public video. Daily limits and duplicate protection still apply.' if operation=='post_now' else 'Channel settings updated.')


def settings(app,body):
    enabled=body.get('auto_start_confirmed')
    if not isinstance(enabled,bool):raise ValueError('Choose whether confirmed new routes start automatically.')
    app.STATE.setdefault('cloud_settings',{})['auto_start_confirmed']=enabled
    app.save_local()
    return dict(updated=True)


def server_command(app, body):
    kind = body.get('action')
    if kind not in ('backup','restart-worker'): raise ValueError('Unknown server command.')
    # Scheduler handles this durable request, never arbitrary shell input.
    with store(app).db(True) as db:
        db.execute('INSERT INTO cloud_commands(kind,created) VALUES(?,?)', (kind,time.time()))
    return dict(updated=True, notice='Server request queued.')


def routes(app, body):
    import cloud_migration
    return {'/api/cloud/stage':lambda:stage(app,body), '/api/cloud/action':lambda:action(app,body),
            '/api/cloud/migration/preview':lambda:cloud_migration.preview(app,body),
            '/api/cloud/migration/confirm':lambda:cloud_migration.commit(app,body),
            '/api/cloud/server':lambda:server_command(app,body),'/api/cloud/settings':lambda:settings(app,body)}
