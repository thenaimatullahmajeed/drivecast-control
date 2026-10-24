"""Workspace tools for the control-room UI; existing publishing backend stays authoritative."""
import copy
import csv
import io
import json
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from googleapiclient.discovery import build
from github_api import GitHub, GitHubError
from records import safe_error
import avatars
import operations
import telegram_service as telegram
import google_identity


def avatar(app, row, url, force=False):
    try:
        row['avatar']=avatars.cache(app.WORK,row['id'],url,force=force)
        row['avatar_notice']='Photo returned by YouTube/Google.' if row['avatar'] else 'Google has not provided a channel photo.'
    except Exception:row['avatar_notice']='Photo refresh unavailable. Your current photo or initials are kept.'


def refresh_avatars(app):
    """Read-only provider metadata; no worker rollout, source moves or uploads."""
    for row in list(app.STATE['connections'].values()):
        try:
            kind=row.get('kind')
            if kind=='github':
                me=app.gh_for(row['id']).request('/user')
                if me.get('login','').casefold()!=row['login'].casefold(): continue
                avatar(app,row,me.get('avatar_url',''))
            elif kind=='youtube':
                with build('youtube','v3',credentials=app.google_credentials(row['id']),cache_discovery=False) as yt:
                    items=yt.channels().list(part='snippet',mine=True).execute(num_retries=1).get('items',[])
                    if len(items)!=1 or items[0]['id']!=row.get('channel_id'): continue
                    thumbs=items[0]['snippet'].get('thumbnails',{})
                    avatar(app,row,(thumbs.get('high') or thumbs.get('medium') or thumbs.get('default') or {}).get('url',''),force=True)
        except Exception: continue
    app.save_local()
    return dict(updated=True)


def google_app(app, body):
    config=body.get('oauth')
    if not config:
        section='web' if os.getenv('DRIVECAST_PUBLIC_CALLBACK_BASE') else 'installed'
        config={section:dict(client_id=str(body.get('client_id','')).strip(),client_secret=str(body.get('client_secret','')).strip(),
                                 project_id=str(body.get('project_id','')).strip())}
    value=config.get('installed') or config.get('web') or {}
    if os.getenv('DRIVECAST_PUBLIC_CALLBACK_BASE') and not config.get('web'):raise ValueError('Use a Web application OAuth JSON in hosted mode and register the callback shown here.')
    if config.get('web') and not os.getenv('DRIVECAST_PUBLIC_CALLBACK_BASE'):raise ValueError('Web OAuth clients need the configured hosted HTTPS gateway. Use Desktop JSON in local mode.')
    if not value.get('client_id','').endswith('.apps.googleusercontent.com') or not value.get('client_secret'):
        raise ValueError('Use your Google Desktop OAuth app client ID and secret, or its downloaded JSON.')
    app.VAULT.put('google-desktop',config)
    app.STATE['google_app']=dict(client_id=value['client_id'],project_id=value.get('project_id',''),saved_at=app.now())
    app.save_local()
    return dict(updated=True)


def google_emails(app,body=None):
    body=body or {};rows=[]
    if body.get('id'):app.connection(body['id'],'youtube')
    for row in app.STATE['connections'].values():
        if row.get('kind')!='youtube' or body.get('id') not in (None,'',row['id']):continue
        try:
            credentials=app.google_credentials(row['id'])
            saved=google_identity.update(row,credentials,app.VAULT.get(row['id']).get('scopes',()),app.now())
        except Exception:
            saved=False
            row.update(google_email_status='Check needed',google_email_notice='Google email is unavailable. Check or reconnect this channel to verify it.')
        rows.append(dict(id=row['id'],name=row['label'],status='Email verified' if saved else row['google_email_status']))
    app.save_local()
    return dict(rows=rows,updated=sum(r['status']=='Email verified' for r in rows))




def github_token(app, body):
    value=str(body.get('token','')).strip()
    gh=GitHub(value)
    me=gh.request('/user');expected=str(body.get('login','')).strip()
    if expected and expected.casefold()!=me['login'].casefold(): raise ValueError('This token belongs to a different GitHub account. Nothing saved.')
    cid='gh:'+me['login'].lower()
    app.VAULT.put(cid,dict(token=value))
    row=app.STATE['connections'].setdefault(cid,{})
    row.update(id=cid,kind='github',label=me['login'],login=me['login'],status='Connected',checked_at=app.now(),notice='Token identity verified. Infrastructure only.')
    avatar(app,row,me.get('avatar_url',''))
    app.save_local()
    return dict(connection_id=cid)


def drive_discover(app,body=None):
    from drive_sources import discover
    return discover(app,body)




def pool(app):
    result=[]
    for row in app.STATE['connections'].values():
        if row.get('kind')!='github': continue
        used=sum(c.get('github_connection')==row['id'] for c in app.STATE['clients'].values())
        maximum=min(operations.GITHUB_CHANNEL_LIMIT,max(1,int(row.get('capacity',2))));health=row.get('health',{})
        percent=health.get('usage_percent')
        try: fresh=(datetime.now(timezone.utc)-datetime.fromisoformat(health['checked_at'])).total_seconds()<1800
        except (KeyError,ValueError,TypeError): fresh=False
        # Unknown billing is displayed honestly. Auth/rate health must be fresh for automatic placement.
        threshold=min(95,float(app.STATE.get('automation',{}).get('failover_threshold',95)))
        healthy=fresh and row.get('status')=='Connected' and health.get('token_valid') is True and not health.get('quota_issue') and not health.get('issue') and (health.get('rate_remaining') or 0)>100 and (percent is None or percent<threshold)
        result.append(dict(id=row['id'],login=row.get('login',row['label']),used=used,capacity=maximum,free=max(0,maximum-used),
                           healthy=healthy,health=copy.deepcopy(health),avatar=row.get('avatar','')))
    return result


def assign(app, excluding='', excluded_accounts=()):
    choices=[r for r in pool(app) if r['healthy'] and r['free'] and r['id'] not in excluded_accounts]
    if not choices: raise ValueError('No healthy GitHub account has a free slot. Add/check an account in GitHub. Maximum 2 channels per account.')
    return min(choices,key=lambda r:(r['used'],r['login'].lower()))['id']


def ensure_slot(app, connection_id, excluding=''):
    account=app.connection(connection_id,'github')
    capacity=min(operations.GITHUB_CHANNEL_LIMIT,max(1,int(account.get('capacity',2))))
    used=sum(c.get('github_connection')==connection_id and c.get('id')!=excluding for c in app.STATE['clients'].values())
    if used>=capacity:raise ValueError('This GitHub account has no free slot. Maximum 2 channels per GitHub account.')


def health(app, body=None):
    rows=[]
    for c in app.STATE['connections'].values():
        if c.get('kind')!='github' or (body or {}).get('id') not in (None,'',c['id']): continue
        gh=app.gh_for(c['id']);h=dict(checked_at=app.now(),token_valid=False,minutes=None,usage_percent=None,rate_remaining=None,workflow_failures=None)
        try:
            me=gh.request('/user')
            if me['login'].casefold()!=c['login'].casefold(): raise ValueError('Identity mismatch.')
            h['token_valid']=True;avatar(app,c,me.get('avatar_url',''))
            rate=gh.request('/rate_limit').get('resources',{}).get('core',{})
            h.update(rate_remaining=rate.get('remaining'),rate_limit=rate.get('limit'),rate_reset=rate.get('reset'))
            try:
                moment=datetime.now(timezone.utc)
                billing=gh.request('/users/'+c['login']+'/settings/billing/usage?year='+str(moment.year)+'&month='+str(moment.month))
                items=[r for r in billing.get('usageItems',[]) if r.get('product')=='Actions' and r.get('unitType')=='minutes']
                h['minutes']=sum(float(r.get('quantity',0)) for r in items)
                limit=c.get('minutes_budget')
                if limit: h['usage_percent']=round(h['minutes']/float(limit)*100,1)
                h['billing_notice']='Current-month reported minutes. Percentage uses your configured budget.'
            except Exception:
                h['billing_notice']='Unavailable: this token/account cannot read billing usage. No quota estimate is invented.'
            failures=0
            for client in app.STATE['clients'].values():
                if client.get('github_connection')!=c['id'] or not client.get('deployed'): continue
                runs=gh.request(gh.repo_path(client['repo_name'])+'/actions/workflows/upload.yml/runs?per_page=10').get('workflow_runs',[])
                failures+=sum(r.get('conclusion') in ('failure','timed_out','cancelled') for r in runs)
            h['workflow_failures']=failures
            c.update(status='Connected',checked_at=app.now())
        except Exception as error:
            h['issue']=getattr(error,'reason','unknown')
            h['notice']=str(error) if h['issue']!='unknown' else 'GitHub health could not be verified. Reconnect/check permissions before assigning new channels.'
            h['quota_issue']=h['issue']=='rate_limit'
            if h['issue'] in ('suspended','authentication'):h['token_valid']=False
            c.update(status='Account suspended' if h['issue']=='suspended' else 'Check connection',checked_at=app.now(),notice=h['notice'])
        c['health']=h;rows.append(h)
    app.save_local()
    return dict(updated=len(rows))


def pool_settings(app, body):
    c=app.connection(body.get('id'),'github')
    capacity=int(body.get('capacity',2));budget=body.get('minutes_budget')
    if not 1<=capacity<=operations.GITHUB_CHANNEL_LIMIT: raise ValueError('Capacity must be 1–2 channels. Maximum 2 per GitHub account.')
    if budget not in (None,'') and float(budget)<=0: raise ValueError('Minutes budget must be positive or blank.')
    c.update(capacity=capacity,minutes_budget=float(budget) if budget not in (None,'') else None)
    app.save_local();return dict(updated=True)


def failover(app, execute=False):
    config=app.STATE.get('automation',{})
    rows=[]
    for account in pool(app):
        h=account['health'];percent=h.get('usage_percent')
        issue=h.get('token_valid') is False or h.get('quota_issue') or (percent is not None and percent>=float(config.get('failover_threshold',95))) or h.get('rate_remaining')==0
        if not issue: continue
        for client in list(app.STATE['clients'].values()):
            if client.get('github_connection')!=account['id'] or not client.get('deployed'): continue
            result=dict(id=client['id'],name=client['name'],source=account['login'],status='Preview',reason='Authentication, rate limit or usage threshold')
            try:
                source_account=app.connection(account['id'],'github')
                if source_account.get('status')=='Account suspended' or h.get('issue')=='suspended':
                    raise ValueError('GitHub account suspended. Appeal through GitHub; automatic reassignment is blocked.')
                if h.get('quota_issue') or h.get('issue')=='rate_limit' or h.get('rate_remaining')==0:
                    raise ValueError('GitHub rate limit reached. Wait for the limit to reset; no account switching to bypass this limit.')
                target=assign(app,excluded_accounts=(account['id'],));result['target']=target
                preview=operations.migration_preview(app,dict(ids=[client['id']],target=target))
                if not preview['ready']: raise ValueError('; '.join(b for r in preview['rows'] for b in r['blockers']))
                if execute:
                    if not config.get('armed') or config.get('auto_failover',True) is not True:
                        raise ValueError('Automation is staged. Confirm activation in Automation first.')
                    operations.migrate(app,dict(ids=[client['id']],target=target,confirmed=True))
                    result['status']='Moved';telegram.notify(app,'failover','DriveCast failover: '+client['name']+' moved from '+account['login']+' to '+target+'. Upload history verified; original worker stopped.')
            except Exception as error:
                result.update(status='Blocked',notice=safe_error(str(error)))
                # If the source cannot be fenced/read, do not create or resume a second worker.
            rows.append(result)
    if execute and rows:
        app.STATE.setdefault('failover_history',[]).extend(dict(r,at=app.now()) for r in rows)
        app.STATE['failover_history']=app.STATE['failover_history'][-1000:]
        old_alerts=app.STATE.setdefault('failover_alerts',{})
        for row in rows:
            app.store().activity(app.STATE['clients'][row['id']],'Automatic failover',row['status']+': '+row.get('notice',row['reason']))
            if row['status']=='Blocked':
                signature=row['id']+':'+row['notice']
                if old_alerts.get(signature,'')<app.now()[:10]:
                    telegram.notify(app,'failover','DriveCast needs you: '+row['name']+' could not fail over safely. '+row['notice'])
                    old_alerts[signature]=app.now()[:10]
        app.save_local()
    return dict(rows=rows,ready=not any(r['status']=='Blocked' for r in rows))


def automation(app, body):
    config=app.STATE.setdefault('automation',{})
    if body.get('armed') is True and body.get('confirmed') is not True:
        raise ValueError('Confirm activation. This allows automatic pauses, failovers and Telegram commands/reports.')
    for key in ('armed','auto_failover','auto_protect'):
        if key in body: config[key]=body[key] is True
    for key,default,low,high in [('failover_threshold',95,80,100),('pause_failures',3,1,100)]:
        value=int(body.get(key,config.get(key,default)))
        if not low<=value<=high: raise ValueError('Invalid automation threshold.')
        config[key]=value
    app.save_local();return dict(updated=True)


def bulk_preview(app,body):
    ids=body.get('ids',[])
    if not isinstance(ids,list) or not ids or len(ids)>1000 or len(set(ids))!=len(ids): raise ValueError('Select 1–1000 unique channels.')
    action=body.get('action')
    if action not in ('enable','pause','dry_run','limit','tag','move'): raise ValueError('Unknown bulk action.')
    rows=[]
    for cid in ids:
        c=app.STATE['clients'].get(cid)
        blockers=[]
        if not c: blockers.append('Channel not found.')
        elif action in ('enable','pause','dry_run','move','limit') and not c.get('deployed'): blockers.append('Prepare this channel first.')
        if action=='limit' and not 1<=int(body.get('limit',0))<=100: blockers.append('Daily limit must be 1–100.')
        if action=='tag' and not str(body.get('tag','')).strip(): blockers.append('Enter a tag.')
        if action=='move' and c:
            try:
                target=app.connection(body.get('target'),'github')
                capacity=next(r for r in pool(app) if r['id']==target['id'])
                if capacity['free']<len(ids): blockers.append('Destination has insufficient free slots.')
                if not capacity['healthy']: blockers.append('Check destination health first.')
                p=operations.migration_preview(app,dict(ids=[cid],target=target['id']))
                blockers.extend(b for r in p['rows'] for b in r['blockers'])
            except Exception as error: blockers.append(safe_error(str(error)))
        rows.append(dict(id=cid,name=c['name'] if c else cid,action=action,blockers=blockers))
    return dict(rows=rows,ready=all(not r['blockers'] for r in rows))


def bulk(app,body):
    if body.get('confirmed') is not True: raise ValueError('Preview and confirm this bulk change first.')
    preview=bulk_preview(app,body);rows=[]
    for row in preview['rows']:
        if row['blockers']: rows.append(dict(row,status='Skipped'));continue
        c=app.STATE['clients'][row['id']]
        try:
            action=body['action']
            if action in ('enable','pause','dry_run'):
                app.client_action(dict(id=c['id'],action=action,confirmed=True))
            elif action=='tag': c['tags']=list(dict.fromkeys(c.get('tags',[])+[str(body['tag']).strip()[:60]]))
            elif action=='limit':
                gh=app.github_for_client(c);app.check_repository(gh,c)
                app.set_variable(gh,c,'MAX_UPLOADS_PER_DAY',str(int(body['limit'])))
                c['max_uploads_per_day']=int(body['limit'])
            elif action=='move': operations.migrate(app,dict(ids=[c['id']],target=body['target'],confirmed=True))
            app.store().activity(c,'Bulk '+action,'Completed')
            rows.append(dict(row,status='Completed'))
        except Exception as error: rows.append(dict(row,status='Failed',notice=safe_error(str(error))))
        app.save_local()
    return dict(rows=rows)


def csv_preview(app,body):
    text=str(body.get('csv',''))
    if len(text)>1024*1024: raise ValueError('CSV must be under 1 MB.')
    reader=csv.DictReader(io.StringIO(text))
    allowed={'name','youtube_connection','source_type','drive_connection','folder_id','github_connection','repo_name','max_uploads_per_day','tags'}
    if not reader.fieldnames or not {'name','youtube_connection','source_type'}.issubset(reader.fieldnames) or set(reader.fieldnames)-allowed:
        raise ValueError('CSV needs name,youtube_connection,source_type; use the template columns.')
    rows=[];seen=set()
    for index,row in enumerate(reader,2):
        if index>1001: raise ValueError('Import at most 1000 channels per CSV.')
        errors=[]
        try:
            if not row['name'].strip(): raise ValueError('Channel name is required.')
            yt=app.connection(row['youtube_connection'],'youtube')
            if yt['id'] in seen or any(c.get('youtube_connection')==yt['id'] for c in app.STATE['clients'].values()): raise ValueError('YouTube channel already assigned or duplicated in CSV.')
            seen.add(yt['id'])
            if row.get('source_type','drive')!='drive':raise ValueError('Only Google Drive sources are supported.')
            from drive_sources import selected
            root,folder=selected(app,app.helpers.parse_folder(row.get('folder_id','')),row.get('drive_connection',''))
            row['drive_connection']=root['connection_id']
            if not 1<=int(row.get('max_uploads_per_day') or 2)<=100: raise ValueError('Invalid daily limit.')
        except Exception as error: errors.append(safe_error(str(error)))
        rows.append(dict(line=index,data=row,blockers=errors))
    return dict(rows=rows,ready=bool(rows) and all(not r['blockers'] for r in rows))


def csv_import(app,body):
    if body.get('confirmed') is not True: raise ValueError('Preview CSV before importing.')
    result=csv_preview(app,body)
    rows=[]
    for row in result['rows']:
        if row['blockers']: rows.append(dict(row,status='Skipped'));continue
        data=dict(row['data']);data['max_uploads_per_day']=int(data.get('max_uploads_per_day') or 2)
        try:
            data['github_connection']=data.get('github_connection') or assign(app)
            data['repo_name']=data.get('repo_name') or 'youtube-'+re.sub('[^a-z0-9]+','-',data['name'].lower()).strip('-')[:60]
            data.update(check_minutes=10,schedule_mode='interval')
            c=app.add_client(data);c['tags']=[t.strip()[:60] for t in data.get('tags','').split('|') if t.strip()]
            rows.append(dict(line=row['line'],name=c['name'],status='Saved; prepare separately'))
        except Exception as error: rows.append(dict(line=row['line'],name=data['name'],status='Failed',notice=safe_error(str(error))))
        app.save_local()
    return dict(rows=rows)


def views(app,body):
    name=str(body.get('name','')).strip()[:80]
    if not name: raise ValueError('Name this saved view.')
    filters={k:str(body.get(k,''))[:200] for k in ('search','status','github','source','tag')}
    app.STATE.setdefault('saved_views',{})[name]=filters;app.save_local();return dict(updated=True)


def maintenance(app):
    """Opt-in background work; upgrade itself never arms it or mutates live workers."""
    while True:
        time.sleep(60)
        if os.getenv('DRIVECAST_HOSTED')=='1': continue
        if not app.VAULT.unlocked or app.JOB['busy']: continue
        config=app.STATE.get('automation',{})
        def tick():
            if app.STATE.get('drive_root'):
                last=app.STATE['drive_root'].get('checked_at','')
                if not last or (datetime.now(timezone.utc)-datetime.fromisoformat(last)).total_seconds()>300:
                    try: drive_discover(app)
                    except Exception: app.STATE['drive_root']['notice']='Folder discovery needs a connection check.'
            if not config.get('armed'): return
            if config.get('auto_failover',True):
                last=app.STATE.get('pool_checked_at','')
                if not last or (datetime.now(timezone.utc)-datetime.fromisoformat(last)).total_seconds()>300:
                    health(app);app.STATE['pool_checked_at']=app.now();failover(app,execute=True)
            if config.get('auto_protect',True):
                for c in app.STATE['clients'].values():
                    if not c.get('enabled') or not c.get('deployed'): continue
                    gh=app.github_for_client(c)
                    runs=gh.request(gh.repo_path(c['repo_name'])+'/actions/workflows/upload.yml/runs?per_page=20').get('workflow_runs',[])
                    consecutive=0
                    for run in runs:
                        if run.get('status')!='completed': continue
                        if run.get('conclusion') not in ('failure','timed_out'): break
                        consecutive+=1
                    if consecutive>=config.get('pause_failures',3):
                        app.client_action(dict(id=c['id'],action='pause'))
                        telegram.notify(app,'failures','DriveCast auto-protect paused '+c['name']+' after repeated failures. Review uncertain uploads before resuming.')
            if app.STATE.get('telegram',{}).get('linked'):
                alerts=app.STATE.setdefault('connection_alerts',{})
                for c in app.STATE['connections'].values():
                    reason=c.get('notice') or c.get('status')
                    try: expiring=(datetime.fromisoformat(c.get('expires_at') or c.get('user_login_expires_at'))-datetime.now(timezone.utc)).total_seconds()<7*86400
                    except (TypeError,ValueError): expiring=False
                    if expiring: reason='Saved access/login expires within 7 days. Reconnect before expiry.'
                    if (c.get('status')!='Connected' or expiring) and alerts.get(c['id'])!=reason:
                        telegram.notify(app,'reconnect','DriveCast reconnect needed: '+c['label']+'\n'+safe_error(reason or 'Connection check failed.'))
                        alerts[c['id']]=reason
                telegram.poll(app);telegram.scheduled(app)
        try: app.begin('Automation checks',tick)
        except ValueError: pass


def routes(app,body):
    def scan_channel():
        c=app.STATE['clients'].get(body.get('id'))
        if not c: raise ValueError('Channel not found.')
        app.scan_source(c);app.save_local()
        return dict(source_stats=c['source_stats'],source_verified=True)
    return {
        '/api/channel/source':scan_channel,
        '/api/google/emails':lambda:google_emails(app,body),
        '/api/avatars/refresh':lambda:refresh_avatars(app),
        '/api/settings/google':lambda:google_app(app,body),
        '/api/github/token':lambda:github_token(app,body),'/api/github/health':lambda:health(app,body),
        '/api/github/settings':lambda:pool_settings(app,body),'/api/failover/preview':lambda:failover(app),
        '/api/drive/root':lambda:drive_discover(app,body),
        '/api/automation':lambda:automation(app,body),'/api/bulk/preview':lambda:bulk_preview(app,body),'/api/bulk':lambda:bulk(app,body),
        '/api/csv/preview':lambda:csv_preview(app,body),'/api/csv/import':lambda:csv_import(app,body),'/api/views':lambda:views(app,body),
        '/api/telegram/connect':lambda:telegram.connect(app,body),'/api/telegram/link':lambda:telegram.poll(app,True),
        '/api/telegram/settings':lambda:telegram.settings(app,body),
        '/api/telegram/test':lambda:telegram.send(app.VAULT.get('telegram-bot'),'DriveCast test message. Your bot and chat are connected.'),
    }
