"""Schedule rollout and verified GitHub handover."""
import base64
import copy
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from github_api import Ledger, GitHubError
from posting_schedule import options, public_schedule, usage


def schedules(app, body):
    ids = body.get('ids') or [body.get('id')]
    config, _ = options(body)
    clients = [app.STATE['clients'][cid] for cid in ids if cid in app.STATE['clients']]
    if not clients:
        raise ValueError('Choose at least one channel.')
    for client in clients:
        app.JOB['notice']='Updating preferred windows for '+client['name']+'. Existing reservations are retained.'
        if not client.get('deployed'):
            raise ValueError('Prepare '+client['name']+' before activating its schedule.')
        github = app.github_for_client(client)
        app.check_repository(github, client)
        active = github.request(github.repo_path(client['repo_name'])+'/actions/runs?per_page=100').get('workflow_runs', [])
        if any(r['status'] != 'completed' for r in active):
            raise ValueError('Wait for the running worker on '+client['name']+'. No schedule was changed for this channel.')
        ledger = Ledger(github,client['repo_name'],client['channel_id'],path=client.get('youtube_ledger_path','records/ledger.json'))
        ledger.load(required=True)
        was_enabled = bool(client.get('enabled'))
        app.set_variable(github,client,'ENABLED','false')
        client.update(enabled=False, schedule_deployed=False)
        app.save_local()
        # If a workflow raced the pause, leave it paused rather than edit its code.
        active = github.request(github.repo_path(client['repo_name'])+'/actions/runs?per_page=100').get('workflow_runs', [])
        if any(r['status'] != 'completed' for r in active):
            raise ValueError('A worker started during the pause. '+client['name']+' stays paused; retry when it finishes.')
        staged = dict(client, **config, schedule_deployed=True)
        for key,value in dict(SCHEDULE_MODE=config['schedule_mode'], SCHEDULE_TIMEZONE=config['schedule_timezone'], SCHEDULE_WINDOWS=config['schedule_windows']).items():
            app.set_variable(github,staged,key,value)
        files = {name:(app.ROOT/name).read_text(encoding='utf-8') for name in app.RUNTIME}
        files['.github/workflows/upload.yml'] = app.controls.workflow(app,staged)
        github.snapshot(client['repo_name'],files,client.get('branch','main'),message='Use varied audience publication windows; preserve upload ledgers')
        if was_enabled:
            app.set_variable(github,staged,'DRY_RUN','false')
            app.set_variable(github,staged,'ENABLED','true')
        client.update(config, schedule_deployed=True, enabled=was_enabled,worker_version=max(4,client.get('worker_version',1)),status='Enabled' if was_enabled else 'Paused',
            notice='Preferred posting windows saved. Existing upload history and daily limits preserved.')
        client['schedule_usage']=usage(ledger.data,client)
        app.save_local()
    return dict(updated=len(clients))








def secret_values(app, client):
    """GitHub cannot export Actions secrets; require portable local copies first."""
    yt = app.VAULT.get(client['youtube_connection'])
    values = dict(CLIENT_ID=yt['CLIENT_ID'],CLIENT_SECRET=yt['CLIENT_SECRET'],YOUTUBE_REFRESH_TOKEN=yt['REFRESH_TOKEN'])
    saved=app.VAULT.get(client.get('drive_connection',''))
    values.update(REFRESH_TOKEN=saved['REFRESH_TOKEN'],FOLDER_ID=client['folder_id'],DRIVE_CLIENT_ID=saved['CLIENT_ID'],DRIVE_CLIENT_SECRET=saved['CLIENT_SECRET'])
    if not all(values.values()):
        raise ValueError('A portable credential is missing. Reconnect this source before migration.')
    return values


def selected_clients(app, body):
    ids = body.get('ids') or [c['id'] for c in app.STATE['clients'].values() if c['github_connection']==body.get('source')]
    if not ids or len(set(ids))!=len(ids) or any(cid not in app.STATE['clients'] for cid in ids):
        raise ValueError('Choose existing channels to migrate.')
    return [app.STATE['clients'][cid] for cid in ids]


GITHUB_CHANNEL_LIMIT=2


def migration_preview(app, body):
    target=app.connection(body.get('target'),'github')
    clients=selected_clients(app,body)
    used=sum(c.get('github_connection')==target['id'] for c in app.STATE['clients'].values())
    capacity=min(GITHUB_CHANNEL_LIMIT,int(target.get('capacity',GITHUB_CHANNEL_LIMIT)))
    dest=app.gh_for(target['id'])
    if dest.request('/user')['login'].casefold()!=target['login'].casefold():
        raise ValueError('Destination GitHub login does not match the saved account.')
    rows=[]
    for c in clients:
        blockers=[]
        try:
            source_account=app.connection(c['github_connection'],'github')
            if source_account.get('status')=='Account suspended' or source_account.get('health',{}).get('issue')=='suspended':
                raise ValueError('GitHub account suspended. Appeal through GitHub before reassigning; suspension cannot be bypassed with another account.')
            source_health=source_account.get('health',{})
            if source_health.get('issue')=='rate_limit' or source_health.get('quota_issue') or source_health.get('rate_remaining')==0:
                raise ValueError('GitHub rate limit reached. Wait for the limit to reset before reassigning.')
            if target.get('status')!='Connected':
                raise ValueError('Check or reconnect the destination GitHub account first.')
            if used+len(clients)>capacity:
                raise ValueError('Destination has insufficient free slots. Maximum 2 channels per GitHub account.')
            if c['github_connection']==target['id'] or c['owner'].casefold()==target['login'].casefold():
                raise ValueError('Choose a different GitHub account.')
            if not c.get('deployed'):
                raise ValueError('Prepare this channel first.')
            secret_values(app,c)
            source=app.github_for_client(c)
            app.check_repository(source,c)
            active=source.request(source.repo_path(c['repo_name'])+'/actions/runs?per_page=100').get('workflow_runs',[])
            if any(r['status']!='completed' for r in active):
                raise ValueError('A worker is running. Wait for it to finish.')
            Ledger(source,c['repo_name'],c['channel_id'],path=c.get('youtube_ledger_path','records/ledger.json')).load(required=True)
            try:
                dest.repository(c['repo_name'])
            except GitHubError as error:
                if error.status!=404:
                    raise
            else:
                raise ValueError('Destination repository already exists. It will not be overwritten.')
            if not shutil.which('git'):
                raise ValueError('Git is required to preserve all repository history and branches.')
        except (ValueError, KeyError, GitHubError) as error:
            blockers.append(app.safe_error(str(error)))
        rows.append(dict(id=c['id'],name=c['name'],source=c['owner']+'/'+c['repo_name'],
            destination=target['login']+'/'+c['repo_name'],resume=bool(c.get('enabled')),blockers=blockers))
    return dict(rows=rows,ready=all(not r['blockers'] for r in rows),
        preserves='All Git branches, tags, upload reservations, video IDs, daily counters and local records. The source repository stays as an archive. Existing Actions runs/issues stay at their original links.')


def git_command(args, token, cwd=None):
    # Credentials exist only in child environment; never in command arguments,
    # repository URLs, Git config on disk, stderr or the migration journal.
    env=dict(os.environ,GIT_TERMINAL_PROMPT='0',GIT_CONFIG_NOSYSTEM='1',
        GIT_CONFIG_COUNT='2',GIT_CONFIG_KEY_0='http.https://github.com/.extraheader',
        GIT_CONFIG_VALUE_0='Authorization: Basic '+base64.b64encode(('x-access-token:'+token).encode()).decode(),
        GIT_CONFIG_KEY_1='credential.helper',GIT_CONFIG_VALUE_1='')
    result=subprocess.run(['git',*args],cwd=cwd,env=env,capture_output=True,timeout=300,
        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if result.returncode:
        raise ValueError('Repository copy was not confirmed. No new worker was enabled; original history is preserved.')
    return result.stdout.decode('utf-8')


def migrate(app, body):
    if body.get('confirmed') is not True:
        raise ValueError('Review the migration preview, then confirm the exact destination account.')
    preview=migration_preview(app,body)
    if not preview['ready']:
        raise ValueError('Migration blocked: '+'; '.join(x for r in preview['rows'] for x in r['blockers']))
    target=app.connection(body['target'],'github');dest=app.gh_for(target['id'])
    journal=app.STATE.setdefault('migrations',[])
    completed=[]
    for client in selected_clients(app,body):
        app.JOB['notice']='Preparing migration for '+client['name']+'. Checking original history before handover.'
        source=app.github_for_client(client);original=copy.deepcopy(client)
        entry=dict(id=client['id'],name=client['name'],from_account=client['owner'],to_account=target['login'],
            original_route={k:client.get(k) for k in ('owner','repo_name','repo_id','github_connection','branch','repo_url','enabled')},
            started_at=app.now(),status='Preparing')
        journal.append(entry);app.save_local()
        staged=None
        try:
            values=secret_values(app,client)
            app.set_variable(source,client,'ENABLED','false')
            # Confirm the server-side fence, then drain queued/in-progress workers.
            enabled=source.request(source.repo_path(client['repo_name'])+'/actions/variables/ENABLED')
            if enabled.get('value')!='false':
                raise ValueError('Original worker pause was not confirmed.')
            # Disable the old workflow too: manual dispatch must not bypass the
            # ENABLED schedule fence after ownership changes.
            workflow_path=source.repo_path(client['repo_name'])+'/actions/workflows/upload.yml'
            source.request(workflow_path+'/disable','PUT')
            if source.request(workflow_path).get('state')!='disabled_manually':
                raise ValueError('Original workflow disable was not confirmed. No destination enabled.')
            client.update(enabled=False,notice='Migration in progress; original worker paused.')
            app.save_local()
            runs=source.request(source.repo_path(client['repo_name'])+'/actions/runs?per_page=100').get('workflow_runs',[])
            if any(r['status']!='completed' for r in runs):
                raise ValueError('A worker is still finishing; retry after it completes. The original stays paused.')
            # Clone after the fence, so even the final reservation/accepted ID is copied.
            with tempfile.TemporaryDirectory(prefix='drivecast-migration-') as temp:
                app.JOB['notice']='Copying all branches, tags and upload history for '+client['name']+'. Both routes stay stopped until verification.'
                path=Path(temp)/'repo.git'
                git_command(['clone','--mirror','https://github.com/'+client['owner']+'/'+client['repo_name']+'.git',str(path)],source.token)
                before=git_command(['show-ref','--heads','--tags'],source.token,cwd=path).strip()
                repo=dest.create_private(client['repo_name'],'DriveCast migrated pipeline: '+client['name'],auto_init=False)
                staged=dict(client,owner=target['login'],github_connection=target['id'],repo_id=repo['id'],repo_url=repo['html_url'],branch=original.get('branch','main'),enabled=False)
                entry.update(destination_repo_id=repo['id'],destination_repo=repo['html_url'],status='Copying');app.save_local()
                variables=source.request(source.repo_path(client['repo_name'])+'/actions/variables?per_page=100').get('variables',[])
                for v in variables:
                    if v['name'] not in ('ENABLED','DRY_RUN'):
                        app.set_variable(dest,staged,v['name'],v['value'])
                for key,value in dict(ENABLED='false',DRY_RUN='true').items():
                    app.set_variable(dest,staged,key,value)
                bot=app.VAULT.values.get('telegram-bot',{})
                if bot.get('chat_id'):
                    values.update(TELEGRAM_BOT_TOKEN=bot['token'],TELEGRAM_CHAT_ID=bot['chat_id'],
                        TELEGRAM_SETTINGS=json.dumps(dict(app.STATE.get('telegram',{}).get('settings',{}),not_before=app.STATE.get('telegram',{}).get('linked_at',app.now()))))
                for key,value in values.items():
                    app.cli('secret','set',key,'--repo',target['login']+'/'+client['repo_name'],token=dest.token,data=value)
                url='https://github.com/'+target['login']+'/'+client['repo_name']+'.git'
                # Ignore read-only pull-request refs; every branch/tag and its complete ancestry is retained.
                git_command(['push',url,'+refs/heads/*:refs/heads/*','+refs/tags/*:refs/tags/*'],dest.token,cwd=path)
                after=git_command(['ls-remote','--heads','--tags',url],dest.token).strip()
                def refs(text):
                    return {tuple(line.split()) for line in text.splitlines() if line.strip() and not line.endswith('^{}')}
                if refs(before)!=refs(after):
                    raise ValueError('Branch/tag verification failed. Destination remains stopped.')
                dest.request(dest.repo_path(client['repo_name']),'PATCH',{'default_branch':staged['branch']})
                for pin,prefix in [(client['channel_id'],client.get('youtube_ledger_path','records/ledger.json'))]:
                    old=Ledger(source,client['repo_name'],pin,path=prefix).load(required=True)
                    new=Ledger(dest,client['repo_name'],pin,path=prefix).load(required=True)
                    if old!=new:
                        raise ValueError('Upload history changed during verification; destination remains stopped.')
            entry.update(status='Verified',verified_at=app.now());app.save_local()
            staged.setdefault('route_history',[]).append(dict(entry['original_route']))
            staged['notice']='Migrated with verified history. Original automation is stopped.'
            # Adopt the verified route before resuming, so crash recovery always
            # points at the destination; never have two enabled workers.
            client.clear();client.update(staged);app.save_local()
            if original.get('enabled'):
                app.set_variable(dest,client,'DRY_RUN','false')
                app.set_variable(dest,client,'ENABLED','true')
                client['enabled']=True
            entry.update(status='Complete',finished_at=app.now());completed.append(client['id'])
            client['status']='Enabled' if client.get('enabled') else 'Paused'
            app.store().activity(client,'Migrate GitHub','Verified all branches, tags and ledgers; source stopped; destination '+target['login'])
            app.save_local()
        except Exception as error:
            if staged:
                try:app.set_variable(dest,staged,'ENABLED','false')
                except Exception:pass
            client['enabled']=False
            client['status']='Needs attention'
            client['notice']='Migration needs attention. Original data preserved; do not start a second copy.'
            entry.update(status='Needs attention',error=app.safe_error(str(error)),finished_at=app.now())
            app.save_local()
            raise ValueError(entry['error']) from None
    return dict(migrated=len(completed))
