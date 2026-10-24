"""Independent Azure runner. Default is dry-only; existing GitHub routes are never adopted."""
import argparse
import contextlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from cloud_store import CloudStore

VERSION = 'azure-v1'


def execute(app, store, job, alive):
    import main as transfer
    import drive_sources
    from records import safe_error
    route = next((r for r in store.routes() if r['id'] == job['route_id']), None)
    if not route: raise ValueError('Route no longer exists.')
    config = route['config']
    dry = job['mode'] == 'dry'
    os.environ['FORCE_PRIVATE']='true' if job['mode']=='private' else 'false'
    os.environ['FORCE_PUBLIC']='true' if job['mode'] in ('public','now') else 'false'
    if not dry and os.getenv('DRIVECAST_LIVE_UPLOADS', '0') != '1':
        raise ValueError('Server is in dry-run-only mode. Explicit go-live approval is required.')
    if not dry and not route['enabled'] and job['mode'] not in ('private','now'): raise ValueError('Channel is paused.')
    yt = app.connection(config['youtube_connection'], 'youtube')
    if yt.get('channel_id') != config['channel_id']: raise ValueError('Saved channel ID mismatch.')
    root = app.STATE.get('drive_root', {})
    if root.get('connection_id') != config['drive_connection'] or root.get('folder_id') != config['drive_root_id']:
        raise ValueError('Project Drive root changed. Revalidate the route first.')
    with contextlib.ExitStack() as stack:
        drive=stack.enter_context(app.build('drive','v3',credentials=app.google_credentials(config['drive_connection']),cache_discovery=False))
        drive_sources.verify_ancestry(drive, config['folder_id'], config['drive_root_id'])
        info = drive.files().get(fileId=config['folder_id'], fields='id,mimeType,capabilities(canAddChildren)', supportsAllDrives=True).execute(num_retries=2)
        if not info.get('capabilities', {}).get('canAddChildren'): raise ValueError('Drive Editor access is required.')
        files = transfer.list_files(drive, config['folder_id'])
        with store.db() as db:
            reserved = {r[0] for r in db.execute('SELECT source_key FROM cloud_reservations WHERE channel_id=?', (config['channel_id'],))}
        candidates = [f for f in files if f['mimeType'] != transfer.FOLDER_MIME and Path(f['name']).suffix.lower() in transfer.VIDEO_TYPES
            and transfer.MARKER not in f.get('description', '') and 'drive:'+f['id'] not in reserved]
        limit = int(config.get('max_uploads_per_day', 2))
        max_bytes = int(os.getenv('DRIVECAST_MAX_VIDEO_MB', '250')) * 1024 * 1024
        candidates = [f for f in candidates if 0 < int(f.get('size') or 0) <= max_bytes]
        def remember_queue():
            with store.db() as sql:
                claimed={r[0] for r in sql.execute('SELECT source_key FROM cloud_reservations WHERE channel_id=?',(config['channel_id'],))}
            remaining=[f for f in candidates if 'drive:'+f['id'] not in claimed]
            store.set_meta('queue:'+config['id'],dict(checked_at=app.now(),eligible=len(remaining),candidates=[dict(id=f['id'],name=f['name'],size=f.get('size')) for f in remaining[:20]]))
        remember_queue()
        if shutil.disk_usage(app.WORK).free < max_bytes * 3: raise ValueError('Low free disk space; transfers paused.')
        if not candidates and not dry: return 'No new eligible videos; no YouTube API request needed.'
        youtube=stack.enter_context(app.build('youtube','v3',credentials=app.google_credentials(config['youtube_connection']),cache_discovery=False))
        transfer.verify_channel(youtube,config['channel_id'])
        if dry:
            preview = [dict(id=f['id'], name=f['name'], size=f.get('size')) for f in candidates[:limit]]
            store.set_meta('dry:'+config['id'], dict(checked_at=app.now(), channel_id=config['channel_id'], folder_id=config['folder_id'],
                candidates=preview, eligible=len(candidates), notice='Read-only check. No uploads, reservations, folders or file moves.'))
            return 'Dry run: '+str(len(preview))+' eligible video(s); no upload started.'
        # All mutating transfer operations are below the dry-run boundary.
        folders = None
        day = datetime.now(ZoneInfo('America/Los_Angeles')).date().isoformat()
        # One video per check, including Force now. Daily limits still govern all modes.
        for video in candidates[:1]:
            if not alive.is_set() or not store.renew(job): raise ValueError('Worker lease lost. No new upload will start.')
            key = 'drive:'+video['id']
            record = dict(id=video['id'], filename=video['name'], source='drive', recorded_at=app.now(),
                privacy='unknown', requested_privacy='private' if job['mode']=='private' else 'public', status='Processing', run_id=job['id'], attempt=1,size=int(video.get('size') or 0))
            if not store.reserve(job, config, key, record, day, limit, int(os.getenv('DRIVECAST_PROJECT_DAILY_LIMIT', '90'))): continue
            def accepted(video_id, body):
                store.update_reservation(job, config['channel_id'], key, 'accepted', youtube_id=video_id,
                    title=body['snippet']['title'], privacy=body.get('_effective_privacy', 'unknown'),
                    requested_privacy=body['status']['privacyStatus'], status='Needs review', recorded_at=app.now())
            try:
                if folders is None: folders = transfer.get_folders(drive, config['folder_id'], files, False)
                if not alive.is_set() or not store.renew(job): raise ValueError('Worker lease lost after reservation; manual review required.')
                done = transfer.process(drive, youtube, video, files, folders, config['folder_id'], record['requested_privacy'], '22', report=accepted)
                store.update_reservation(job, config['channel_id'], key, 'done' if done else 'quarantined', status='Done' if done else 'Needs review')
                if done:
                    with contextlib.suppress(Exception):
                        import telegram_service
                        telegram_service.notify(app,'uploads','DriveCast: '+config['name']+' — YouTube accepted this video. Actual visibility is shown separately in Videos.')
            except Exception as error:
                store.update_reservation(job, config['channel_id'], key, 'uncertain', status='Needs review', error=safe_error(transfer.error_message(error)))
                with contextlib.suppress(Exception):
                    import telegram_service
                    telegram_service.notify(app,'failures','DriveCast: '+config['name']+' needs review. Upload outcome may be uncertain; no automatic retry.')
                raise ValueError('Transfer failed or its outcome is uncertain. Reservation retained; no automatic retry.') from None
            finally:
                mirror_records(app, store, config)
                remember_queue()
        return 'Worker check finished. Accepted uploads are retained; visibility is verified separately.'


def mirror_records(app, store, config):
    with store.db() as db:
        rows = list(db.execute('SELECT source_key,record,status FROM cloud_reservations WHERE channel_id=?', (config['channel_id'],)))
    records = {r['source_key']: json.loads(r['record']) for r in rows}
    # Existing RecordStore remains the stats/reporting API; the authoritative reservation
    # is already durable even if this display projection fails or the process crashes.
    app.store().import_ledger(dict(config, id=config.get('original_client_id') or config['id'],owner='azure', repo_name='cloud-runner'), dict(channel_id=config['channel_id'], items=records))


def schedule(store):
    store.recover()
    last = store.meta('scheduler:last', 0)
    if time.time() - last < 30: return
    store.set_meta('scheduler:last', time.time())
    if os.getenv('DRIVECAST_LIVE_UPLOADS', '0') != '1': return
    for route in store.routes():
        if route['enabled'] and route['history_verified']:
            day=datetime.now(ZoneInfo('America/Los_Angeles')).date().isoformat()
            with store.db() as db:
                used=store.used(db,route['channel_id'],day)
            if used>=int(route['config'].get('max_uploads_per_day',2)) or time.time()<store.next_due(route['id']): continue
            with contextlib.suppress(ValueError):
                job_id=store.enqueue(route['id'], 'public')
                if job_id:store.set_meta('next-check:'+route['id'],time.time()+300)


def once(workspace, role):
    os.environ['DRIVECAST_WORK_DIR'] = str(workspace)
    import app
    app.load()
    if not app.VAULT.unlocked: app.VAULT.try_service_key()
    store = CloudStore(app.WORK/'records.sqlite3')
    if role == 'scheduler':
        schedule(store)
        if app.VAULT.unlocked:
            import drive_sources
            drive_sources.refresh_if_due(app)
        if app.VAULT.unlocked and time.time()-store.meta('stats:last_attempt',0)>21600:
            store.set_meta('stats:last_attempt',time.time())
            with contextlib.suppress(Exception): app.sync()
        # Telegram command processing uses exactly the same guarded cloud API.
        if app.VAULT.unlocked and app.STATE.get('telegram', {}).get('linked'):
            import telegram_service
            telegram_service.poll(app)
            telegram_service.scheduled(app)
            from datetime import date
            today=datetime.now(ZoneInfo('America/Los_Angeles')).date()
            deadline=os.getenv('DRIVECAST_TRIAL_END','2026-11-20')
            ending=date.fromisoformat(deadline) if deadline else None
            if ending and 0<=(ending-today).days<=3 and store.meta('trial:alert')!=today.isoformat():
                telegram_service.notify(app,'reconnect','DriveCast Azure trial ends '+ending.isoformat()+'. Verify encrypted backups and choose paid continuation or restore on another server before expiry.')
                store.set_meta('trial:alert',today.isoformat())
        return
    if not app.VAULT.unlocked: return
    if (Path(os.getenv('DRIVECAST_DATA_DIR',app.WORK))/'draining').exists(): return
    worker = os.getenv('DRIVECAST_WORKER_ID') or socket.gethostname()+':'+str(os.getpid())
    store.heartbeat(worker)
    job = store.claim(worker)
    if not job: return
    alive, stop = threading.Event(), threading.Event()
    alive.set()
    def keepalive():
        while not stop.wait(20):
            try:
                store.heartbeat(worker)
                if not store.renew(job): alive.clear(); return
            except Exception: alive.clear(); return
    thread = threading.Thread(target=keepalive, daemon=True); thread.start()
    try:
        notice = execute(app, store, job, alive)
        store.finish(job, notice=notice)
    except Exception as error:
        # Raw provider exceptions can contain upload session URLs or credentials.
        from records import safe_error
        import main as transfer
        store.finish(job, 'attention', safe_error(transfer.error_message(error)))
        with contextlib.suppress(Exception):
            import telegram_service
            telegram_service.notify(app,'failures','DriveCast worker needs review: '+safe_error(transfer.error_message(error)))
    finally:
        stop.set(); thread.join(2)


def supervise(role):
    root = Path(os.getenv('DRIVECAST_DATA_DIR', '/data')).resolve()
    processes = {};cursor=0
    capacity = max(1, min(4, int(os.getenv('DRIVECAST_WORKERS', '2')))) if role == 'worker' else 1
    while True:
        # The worker service is intentionally a single supervisor: one VM-wide cap,
        # not N workers for every logged-in workspace. No Docker socket is exposed.
        workspaces = sorted(p.parent for p in (root/'accounts'/'users').glob('*/workspace.json'))
        for key, process in list(processes.items()):
            if process.poll() is not None: processes.pop(key)
        if (root/'draining').exists():
            (root/(role+'-heartbeat')).write_text(str(time.time()));time.sleep(5);continue
        if role=='worker':
            requests=[]
            for workspace in workspaces:
                store=CloudStore(workspace/'records.sqlite3')
                with store.db() as db:
                    requests.extend((store,r['id']) for r in db.execute("SELECT id FROM cloud_commands WHERE kind='restart-worker' AND handled IS NULL"))
            if requests and not processes:
                for store,cid in requests:
                    with store.db(True) as db: db.execute('UPDATE cloud_commands SET handled=? WHERE id=?',(time.time(),cid))
                return # Docker restart: always restarts this idle supervisor.
            if requests:
                (root/(role+'-heartbeat')).write_text(str(time.time()));time.sleep(5);continue
        ordered=workspaces[cursor:]+workspaces[:cursor]
        cursor=(cursor+1)%max(1,len(workspaces))
        for workspace in ordered*capacity:
            if len(processes) >= capacity: break
            key=(workspace,next((slot for slot in range(capacity) if not any(k[1]==slot for k in processes)),None))
            if key[1] is None: continue
            processes[key] = subprocess.Popen([sys.executable, __file__, role, '--workspace', str(workspace)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=dict(os.environ,DRIVECAST_WORKER_ID=socket.gethostname()+':'+str(key[1])))
        marker = root/(role+'-heartbeat')
        marker.write_text(str(time.time()))
        time.sleep(5 if role == 'worker' else 30)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('role', choices=('scheduler', 'worker'))
    parser.add_argument('--workspace', type=Path)
    args = parser.parse_args()
    if args.workspace: once(args.workspace, args.role)
    else: supervise(args.role)
