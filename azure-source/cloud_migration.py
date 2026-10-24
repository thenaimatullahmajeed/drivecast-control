"""Explicit GitHub-to-cloud handover: fence the original before importing history."""
import hashlib
import json
import os
from github_api import Ledger
from cloud_control import store


def preview(app, body):
    db=store(app)
    route=next((r for r in db.routes() if r['id']==body.get('id')),None)
    if not route: raise ValueError('Stage and dry-run this cloud route first.')
    config=route['config'];old=app.STATE['clients'].get(config.get('original_client_id'))
    if old and old.get('deployed') and old.get('runner')!='azure':
        gh=app.github_for_client(old)
        workflow=gh.request(gh.repo_path(old['repo_name'])+'/actions/workflows/upload.yml')
        runs=gh.request(gh.repo_path(old['repo_name'])+'/actions/workflows/upload.yml/runs?per_page=100').get('workflow_runs',[])
        ledger=Ledger(gh,old['repo_name'],config['channel_id'],path=old.get('youtube_ledger_path','records/ledger.json')).load(required=True)
        result=dict(ready=not any(r.get('status')!='completed' for r in runs),
            active_runs=sum(r.get('status')!='completed' for r in runs),old_runner=old.get('owner'),
            old_workflow_state=workflow.get('state'),records=len(ledger['items']),
            notice='Confirmation disables the old workflow, verifies no run is active, then imports its complete ledger. Cloud stays paused.')
    else:
        result=dict(ready=True,records=0,active_runs=0,notice='No deployed legacy route recorded. Confirm no other machine publishes this exact folder/channel.')
    result.update(id=config['id'],folder_id=config['folder_id'],channel_id=config['channel_id'])
    db.set_meta('migration-preview:'+config['id'],dict(at=app.now(),result=result))
    return result


def commit(app, body):
    if body.get('confirmed') is not True or body.get('other_runners_stopped') is not True:
        raise ValueError('Explicitly confirm this exact handover and that all other uploaders are stopped.')
    db=store(app);route=next((r for r in db.routes() if r['id']==body.get('id')),None)
    dry=db.meta('dry:'+route['id'],{}) if route else {}
    if not route or not dry.get('checked_at') or (dry.get('folder_id'),dry.get('channel_id'))!=(route['folder_id'],route['channel_id']):
        raise ValueError('A successful dry run is required before handover.')
    config=route['config'];old=app.STATE['clients'].get(config.get('original_client_id'))
    # Recheck the destination and source before pausing the original runner.
    app.check_google(config['youtube_connection'])
    app.scan_source(dict(config,source_type='drive'))
    if old and old.get('deployed') and old.get('runner')!='azure':
        gh=app.github_for_client(old);path=gh.repo_path(old['repo_name'])
        app.set_variable(gh,old,'ENABLED','false')
        gh.request(path+'/actions/workflows/upload.yml/disable','PUT')
        workflow=gh.request(path+'/actions/workflows/upload.yml')
        values=gh.request(path+'/actions/variables?per_page=100').get('variables',[])
        runs=gh.request(path+'/actions/workflows/upload.yml/runs?per_page=100').get('workflow_runs',[])
        if workflow.get('state')!='disabled_manually' or next((r['value'] for r in values if r['name']=='ENABLED'),None)!='false':
            raise ValueError('Old worker pause could not be verified. Cloud remains paused.')
        if any(r.get('status')!='completed' for r in runs): raise ValueError('Wait for the old worker to finish. Cloud remains paused.')
        ledger=Ledger(gh,old['repo_name'],config['channel_id'],path=old.get('youtube_ledger_path','records/ledger.json')).load(required=True)
        db.import_ledger(config,ledger);app.store().import_ledger(old,ledger)
        db.set_meta('migration-ledger:'+config['id'],dict(sha256=hashlib.sha256(json.dumps(ledger,sort_keys=True).encode()).hexdigest(),at=app.now()))
    else:
        records=app.store().data()['uploads'];items={}
        for row in records:
            if row['client_id']==config.get('original_client_id') and row.get('status')!='Dry run':
                items['drive:'+row['file_id']]=dict(id=row['file_id'],filename=row['filename'],status=row['status'],youtube_id=row.get('youtube_id',''),recorded_at=row['recorded_at'])
        db.import_ledger(config,dict(channel_id=config['channel_id'],items=items))
    with db.db(True) as connection: connection.execute('UPDATE cloud_routes SET history_verified=1,enabled=0 WHERE id=?',(config['id'],))
    cid=old['id'] if old else config['id']
    value=dict(old or {});value.update(config);value.update(id=cid,cloud_route_id=config['id'],source_type='drive',enabled=False,
        deployed=True,verified=True,source_verified=True,needs_drive_folder=False,status='Paused',notice='Azure handover complete; publishing stays paused.')
    auto=bool(app.STATE.get('cloud_settings',{}).get('auto_start_confirmed')) and os.getenv('DRIVECAST_LIVE_UPLOADS','0')=='1'
    if auto:
        db.set_enabled(config['id'],True)
        db.enqueue(config['id'],'public')
        value.update(enabled=True,status='Enabled',notice='Exact folder/channel confirmed. Public posting is on within the daily limit.')
    app.STATE['clients'][cid]=value;app.save_local()
    return dict(updated=True,id=cid,notice='Confirmed channel setup. Public posting is on.' if auto else 'Complete history imported. Cloud is paused; no upload started.')
