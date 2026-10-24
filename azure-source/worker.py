"""Drive-to-YouTube worker: verify, reserve once, upload, retain every outcome."""
import logging
import os
import re
import secrets
from datetime import datetime,timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
import main as drive_tool
from github_api import GitHub,Ledger

LOG=logging.getLogger('drivecast.worker')

def google_credentials(token,scopes):
    drive=scopes==[drive_tool.SCOPES[0]]
    credentials=Credentials(token=None,refresh_token=token,
        client_id=(os.environ.get('DRIVE_CLIENT_ID') if drive else None) or os.environ['CLIENT_ID'],
        client_secret=(os.environ.get('DRIVE_CLIENT_SECRET') if drive else None) or os.environ['CLIENT_SECRET'],
        token_uri='https://oauth2.googleapis.com/token',scopes=scopes)
    credentials.refresh(Request())
    return credentials

def stamp():return datetime.now(timezone.utc).isoformat()

def run():
    if os.environ.get('SOURCE_TYPE','drive')!='drive':raise ValueError('Only Google Drive sources are supported. No upload started.')
    expected=os.environ.get('EXPECTED_CHANNEL_ID','')
    if not re.fullmatch(r'UC[A-Za-z0-9_-]{22}',expected):raise ValueError('An exact verified YouTube channel ID is required.')
    repository=os.environ.get('GITHUB_REPOSITORY','')
    if repository.count('/')!=1:raise ValueError('A pinned private worker repository is required.')
    owner,repo=repository.split('/')
    github=GitHub(os.environ.get('GITHUB_TOKEN',''),owner)
    maximum=int(os.environ.get('MAX_UPLOADS_PER_DAY','2'));limit=int(os.environ.get('UPLOAD_LIMIT','1'))
    if not 1<=maximum<=100 or not 1<=limit<=100:raise ValueError('Invalid daily upload limit.')
    dry_value=os.environ.get('DRY_RUN','true').lower();private_value=os.environ.get('FORCE_PRIVATE','false').lower()
    if dry_value not in ('true','false') or private_value not in ('true','false'):raise ValueError('Invalid publishing mode.')
    dry=dry_value=='true';privacy='private' if private_value=='true' else 'public'
    category=os.environ.get('CATEGORY_ID','22')
    if not category.isdigit():raise ValueError('Invalid video category.')
    root=os.environ.get('FOLDER_ID','')
    if not root:raise ValueError('Select and confirm the channel Drive subfolder first.')
    ledger=Ledger(github,repo,expected,path=os.environ.get('YOUTUBE_LEDGER_PATH','records/ledger.json'),max_daily=maximum)
    token=os.environ.get('YOUTUBE_REFRESH_TOKEN') or os.environ.get('REFRESH_TOKEN','')
    with build('youtube','v3',credentials=google_credentials(token,[drive_tool.SCOPES[1],drive_tool.CHANNEL_SCOPE]),cache_discovery=False) as youtube:
        drive_tool.verify_channel(youtube,expected)
        ledger.load(required=not dry)
        identity={'type':'drive','id':root}
        if ledger.sha is not None and ledger.data.get('source_identity')!=identity and identity not in ledger.data.get('source_identities',[]):
            raise ValueError('Source folder differs from the pinned upload history. Revalidate before publishing.')
        from posting_schedule import worker_allowance
        allowance=worker_allowance(ledger.data,maximum)
        if not allowance:
            LOG.info('Waiting for the next posting window; no upload requested.')
            return 0
        for key,row in ledger.data['items'].items():
            if row.get('status') in ('Processing','Needs review','Failed'):LOG.warning('QUARANTINED - never retried automatically: %s / %s',key,row['status'])
        auth=google_credentials(os.environ['REFRESH_TOKEN'],[drive_tool.SCOPES[0]])
        with build('drive','v3',credentials=auth,cache_discovery=False) as drive:
            if project_root:=os.environ.get('DRIVE_MAIN_FOLDER_ID'):
                from drive_sources import verify_ancestry
                verify_ancestry(drive,root,project_root)
            files=drive_tool.list_files(drive,root)
            folders=drive_tool.get_folders(drive,root,files,dry)
            if 'Processing' in folders:
                for row in drive_tool.list_files(drive,folders['Processing']):LOG.warning('STUCK IN PROCESSING - never retried: %s',row['id'])
            candidates=[f for f in files if f['mimeType']!=drive_tool.FOLDER_MIME and Path(f['name']).suffix.lower() in drive_tool.VIDEO_TYPES
                        and drive_tool.MARKER not in f.get('description','') and 'drive:'+f['id'] not in ledger.data['items']]
            day=datetime.now(ZoneInfo('America/New_York')).date().isoformat()
            remaining=max(0,maximum-ledger.data['daily'].get(day,0))
            if drive_tool.daily_budget(drive,root)==0:candidates=[]
            candidates=candidates[:min(limit,allowance,remaining)]
            if dry:
                for video in candidates:
                    drive_tool.video_body(drive,files,video,privacy,category)
                    LOG.info('DRY RUN - would upload: %r (%s)',video['name'],video['id'])
                LOG.info('Dry run complete: %s candidates; no files, uploads or records changed.',len(candidates))
                return 0
            for video in candidates:
                key='drive:'+video['id']
                record=dict(id=video['id'],filename=video['name'],source='drive',recorded_at=stamp(),
                    run_id=int(os.environ.get('GITHUB_RUN_ID','0')),attempt=int(os.environ.get('GITHUB_RUN_ATTEMPT','1')),
                    youtube_id='',error='',privacy=privacy)
                if not ledger.reserve(key,record,day,secrets.token_hex(16)):break
                def accepted(video_id,body):
                    ledger.update(key,youtube_id=video_id,title=body['snippet']['title'],privacy=body.get('_effective_privacy','unknown'),
                        requested_privacy=body['status']['privacyStatus'],status='Needs review',recorded_at=stamp())
                try:
                    if drive_tool.daily_budget(drive,root,reserve=True)==0:
                        ledger.update(key,status='Failed',error='Drive daily cap used; reserved attempt stays quarantined.')
                        break
                    done=drive_tool.process(drive,youtube,video,files,folders,root,privacy,category,report=accepted)
                    ledger.update(key,status='Done' if done else 'Failed',error='' if done else 'Source changed or was already processed.',recorded_at=stamp())
                except Exception as error:
                    from records import safe_error
                    row=ledger.data['items'][key]
                    try:ledger.update(key,status='Needs review' if row.get('youtube_id') else 'Failed',error=safe_error(drive_tool.error_message(error)),recorded_at=stamp())
                    except Exception:
                        LOG.error('Final record unconfirmed; original reservation retained. No automatic retry.')
                        return 1
                    if drive_tool.quota_exceeded(error):return 0
                    LOG.error('Upload failed or uncertain; review the saved record before retrying.')
                    return 1
    return 0

def cli():
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    logging.getLogger('googleapiclient.http').setLevel(logging.ERROR)
    try:
        if os.environ.get('STATS_ONLY','false')=='true':
            from video_stats import worker_sync
            return worker_sync(google_credentials)
        return run()
    except RefreshError:
        os.environ['RUN_FAILURE_KIND']='reconnect'
        LOG.error('Google access expired or was revoked. Reconnect only the affected Google account.')
        return 1
    except Exception as error:
        from records import safe_error
        if drive_tool.quota_exceeded(error):return 0
        LOG.error('Worker stopped: %s',safe_error(drive_tool.error_message(error)))
        return 1

if __name__=='__main__':
    result=cli()
    try:
        from telegram_service import worker_notifications
        worker_notifications(result)
    except Exception:LOG.warning('Notification unconfirmed; upload records unchanged.')
    raise SystemExit(result)
