"""Explicit, workspace-scoped channel deletion. Never deletes provider media."""
import copy
import json
import os
import secrets
import re
from datetime import datetime, timezone

from cloud_store import CloudStore
import portable_backup

RECORD_TABLES=('uploads','runs','activity','video_stats','destinations','video_details')


def checkpoint(app):
    raw=portable_backup.export(app)
    directory=app.WORK/'recovery-backups'
    directory.mkdir(parents=True,exist_ok=True)
    path=directory/('before-cleanup-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+secrets.token_hex(3)+'.drivecast')
    path.write_bytes(raw)
    if os.name!='nt': path.chmod(0o600)
    return path.name


def clear(app,body):
    reset=body.get('reset') is True
    if body.get('confirmed') is not True or (reset and body.get('phrase')!='RESET'):
        raise ValueError('Confirm deletion. Type RESET for a fresh workspace.')
    if not app.VAULT.unlocked: raise ValueError('Unlock saved connections before cleanup.')
    state=app.STATE
    ids=body.get('ids',[])
    if not reset and (not isinstance(ids,list) or not ids or len(ids)>1000 or any(not isinstance(v,str) for v in ids)):
        raise ValueError('Select the channel or channels to delete.')
    app.store().data()  # Ensure reporting tables exist, including for a fresh workspace.
    db=CloudStore(app.WORK/'records.sqlite3')
    routes=db.routes()
    yt_ids={cid for cid,c in state.get('connections',{}).items() if c.get('kind')=='youtube' and (reset or cid in ids)}
    clients={cid:c for cid,c in state.get('clients',{}).items() if reset or cid in ids or c.get('youtube_connection') in yt_ids}
    route_ids={r['id'] for r in routes if reset or r['id'] in ids or r['config'].get('original_client_id') in clients or r['config'].get('youtube_connection') in yt_ids}
    for r in routes:
        if r['id'] in route_ids and r['config'].get('youtube_connection'): yt_ids.add(r['config']['youtube_connection'])
    yt_ids.update(c['youtube_connection'] for c in clients.values() if c.get('youtube_connection'))
    clients.update({cid:c for cid,c in state.get('clients',{}).items() if c.get('youtube_connection') in yt_ids})
    channel_ids={state['connections'][cid]['channel_id'] for cid in yt_ids if state.get('connections',{}).get(cid,{}).get('channel_id')}
    channel_ids.update(c['channel_id'] for c in clients.values() if c.get('channel_id'))
    if not reset and not (clients or yt_ids or route_ids): raise ValueError('Channel no longer exists. Refresh the page.')
    def relevant(row):return reset or row['route_id'] in route_ids
    with db.db(True) as sql:
        if any(relevant(row) for row in sql.execute("SELECT route_id FROM cloud_jobs WHERE state='running'")):
            raise ValueError('A worker is still using this channel. Pause it and wait for the active check to finish before deleting.')
        # Fence queued work before exporting/deleting anything. No worker can claim it.
        for rid in route_ids:
            sql.execute('UPDATE cloud_routes SET enabled=0 WHERE id=?',(rid,))
            sql.execute("UPDATE cloud_jobs SET state='cancelled',notice='Channel cleanup requested' WHERE route_id=? AND state='queued'",(rid,))
    backup=checkpoint(app)  # Failure here stops cleanup; provider credentials/media are untouched.
    removed=len(yt_ids)
    if reset:
        keep={k:copy.deepcopy(v) for k,v in state.items() if k in ('google_app','telegram','primary_origin')}
        app.VAULT.refresh()
        app.VAULT.values={k:v for k,v in app.VAULT.values.items() if k=='google-desktop' or k.startswith('telegram')}
        app.VAULT.save(replace=True)
        with db.db(True) as sql:
            for table in RECORD_TABLES+('cloud_jobs','cloud_reservations','cloud_routes','cloud_workers','cloud_events','cloud_commands'):
                sql.execute('DELETE FROM '+table)
            sql.execute("DELETE FROM cloud_meta WHERE key NOT IN ('schema','vault:envelope','vault:generation','workspace:state','backup:last')")
        state.clear()
        state.update(version=2,workspace_id=secrets.token_hex(16),connections={},clients={},remote={},last_sync='',
            notice='Fresh workspace. Connect Google Drive, authorize YouTube, then match a folder. Posting is off.',
            cleanup={'at':app.now(),'kind':'reset','backup':backup},**keep)
        avatars=app.WORK/'avatars'
        if avatars.is_dir() and not avatars.is_symlink() and avatars.resolve().parent==app.WORK.resolve():
            for file in avatars.iterdir():
                if not file.is_symlink() and re.fullmatch(r'[a-f0-9]{64}\.(img|type)',file.name):file.unlink()
        legacy=app.WORK/'setup-clients.json'
        if legacy.is_file() and not legacy.is_symlink():legacy.unlink()
    else:
        with db.db(True) as sql:
            for cid in clients.keys()|route_ids:
                for table in RECORD_TABLES: sql.execute('DELETE FROM '+table+' WHERE client_id=?',(cid,))
            for channel in channel_ids: sql.execute('DELETE FROM cloud_reservations WHERE channel_id=?',(channel,))
            for channel in channel_ids: sql.execute('DELETE FROM cloud_meta WHERE key LIKE ?',('carry:'+channel+':%',))
            for rid in route_ids:
                sql.execute('DELETE FROM cloud_jobs WHERE route_id=?',(rid,))
                sql.execute('DELETE FROM cloud_routes WHERE id=?',(rid,))
                for prefix in ('dry:','queue:','next-check:','migration-preview:','migration-ledger:'):
                    sql.execute('DELETE FROM cloud_meta WHERE key=?',(prefix+rid,))
        for cid in clients: state['clients'].pop(cid,None)
        for cid in yt_ids: state.get('connections',{}).pop(cid,None)
        app.VAULT.refresh()
        for cid in yt_ids: app.VAULT.values.pop(cid,None)
        app.VAULT.save()
        if state.get('draft',{}).get('id') in clients: state.pop('draft',None)
        for folder in state.get('drive_root',{}).get('folders',[]):
            if folder.get('client_id') in clients:
                for key in ('client_id','client_name','channel_id','youtube_connection'):folder.pop(key,None)
        state['saved_views']={}
        state['cleanup']={'at':app.now(),'kind':'delete','backup':backup}
    app.save_local()
    return dict(updated=True,removed=removed,notice='Channel data removed from this workspace. YouTube videos and Drive files were not deleted.')


def routes(app,body):
    return {'/api/workspace/cleanup':lambda:clear(app,body)}
