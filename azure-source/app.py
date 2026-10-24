"""DriveCast control panel: Azure hosted queue, with an optional legacy GitHub adapter."""
import argparse
import base64
import copy
import io
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, unquote
from urllib.request import Request as HTTPRequest, urlopen

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

import local_helpers as helpers
from get_token import authorize
from github_api import GitHub, GitHubError, Ledger
from records import RecordStore, safe_error
from vault import Vault
import control_services as controls
import operations
import upgrade
import google_identity
import drive_sources
import workspace_schema
import cloud_control
import workspace_cleanup
from posting_schedule import public_schedule, usage as schedule_usage
import portable_backup

ROOT = Path(__file__).resolve().parent
WORK = Path(os.environ.get('DRIVECAST_WORK_DIR', ROOT / 'work')).resolve()
STATE_PATH = WORK / "workspace.json"
STATE = {}
BASE_STATE = {}
VAULT = Vault(WORK / "connections.enc.json")
LOCK = threading.RLock()
TOKEN = secrets.token_urlsafe(32)
JOB = {"busy": False, "label": "", "notice": "Ready", "auth_url": "", "output": "", "outcome": "idle"}
RUNTIME = ("main.py", "get_token.py", "worker.py", "metadata.py", "github_api.py", "records.py",
           "video_stats.py", "private_browser.py", "posting_schedule.py", "telegram_service.py",
           "google_identity.py", "drive_sources.py", "requirements.txt", ".github/workflows/upload.yml", "README.md")


def context():
    return sys.modules[__name__]


def open_current_auth():
    from private_browser import open_private
    if not JOB.get('auth_url'):raise ValueError('No pending sign-in link. Start authorization first.')
    return open_private(JOB['auth_url'])


def now():
    return datetime.now(timezone.utc).isoformat()


def store():
    return RecordStore(WORK / "records.sqlite3")


def save_local():
    global BASE_STATE
    with LOCK:
        WORK.mkdir(parents=True, exist_ok=True)
        if os.getenv('DRIVECAST_HOSTED')=='1':
            import workspace_db
            merged=workspace_db.save(WORK/'records.sqlite3',BASE_STATE,STATE)
            STATE.clear();STATE.update(merged)
        temporary = STATE_PATH.with_suffix('.'+secrets.token_hex(4)+'.tmp')
        temporary.write_text(json.dumps(STATE, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(STATE_PATH)
        BASE_STATE=copy.deepcopy(STATE)


def load():
    global BASE_STATE
    if STATE_PATH.exists():
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if os.getenv('DRIVECAST_HOSTED')=='1':
            import workspace_db
            data=workspace_db.load(WORK/'records.sqlite3',data)
        if data.get("version") != 2:
            raise ValueError("Unsupported workspace format; existing data left unchanged.")
        STATE.update(data)
        BASE_STATE=copy.deepcopy(STATE)
        VAULT.try_remembered_windows()
        if not VAULT.unlocked: VAULT.try_service_key()
        if workspace_schema.drive_only(STATE, VAULT):save_local()
        return
    STATE.update(version=2, workspace_id=secrets.token_hex(16), connections={}, clients={}, remote={},
                 last_sync="", notice="First: set Recovery Password, then add your connections.")
    legacy = WORK / "setup-clients.json"
    if legacy.exists():
        old = json.loads(legacy.read_text(encoding="utf-8"))
        owner = old.get("owner", "thenaimatullahmajeed")
        connection_id = "gh:" + owner.lower()
        STATE["connections"][connection_id] = dict(id=connection_id, kind="github", label=owner, login=owner,
            status="Check connection", checked_at="", notice="Existing CLI login can be imported after vault unlock.")
        for original in old.get("clients", []):
            c = {k: original[k] for k in helpers.PUBLIC_KEYS if k in original}
            c.update(source_type="drive", owner=owner, github_connection=connection_id,
                youtube_connection="", drive_connection="",
                stats={}, source_stats={}, credential_status="Reconnect Google for live stats/portable recovery; existing GitHub secrets preserved.",
                worker_version=1)
            STATE["clients"][c["id"]] = c
        STATE["notice"] = "Existing clients and local records preserved. Reconnect Google once to include credentials in encrypted recovery. Existing repo secrets are unchanged."
    save_local()


def public_state():
    global BASE_STATE
    with LOCK:
        if os.getenv('DRIVECAST_HOSTED')=='1' and not JOB['busy']:
            import workspace_db
            updated=workspace_db.load(WORK/'records.sqlite3',STATE)
            STATE.clear();STATE.update(updated);BASE_STATE=copy.deepcopy(STATE)
            VAULT.refresh()
        result = copy.deepcopy(STATE)
        result.update(vault_exists=VAULT.path.exists(), unlocked=VAULT.unlocked, job=dict(JOB), cli_available=bool(helpers.find_gh()))
        result.update(google_json_saved=bool(VAULT.unlocked and 'google-desktop' in VAULT.values),ui_version=5)
        result['primary_origin']=os.getenv('DRIVECAST_PRIMARY_ORIGIN',STATE.get('primary_origin',''))
        result.pop('retired_sources',None)
        result['github_pool']=upgrade.pool(context())
        result['isolated_user']=os.getenv('DRIVECAST_ISOLATED_USER')=='1'
        result['ui_version']=5
        result['cloud']=cloud_control.state(context())
        for route in result['cloud'].get('routes',[]):
            for client in result['clients'].values():
                if client.get('cloud_route_id')==route['id']:
                    client.update(enabled=bool(route['enabled']),status='Enabled' if route['enabled'] else 'Paused')
        if VAULT.unlocked and VAULT.values.get('google-desktop'):
            config=VAULT.values['google-desktop'].get('installed') or VAULT.values['google-desktop'].get('web') or {}
            result['google_app']=dict(client_id=config.get('client_id',''),project_id=config.get('project_id',''))
            result['google_app']['client_type']='web' if VAULT.values['google-desktop'].get('web') else 'installed'
            result['google_app_ready']=bool(VAULT.values['google-desktop'].get('web')) if os.getenv('DRIVECAST_PUBLIC_CALLBACK_BASE') else bool(VAULT.values['google-desktop'].get('installed'))
        if os.getenv('DRIVECAST_PUBLIC_CALLBACK_BASE'):
            result['web_callbacks']={'google':os.getenv('DRIVECAST_OAUTH_REDIRECT') or os.environ['DRIVECAST_PUBLIC_CALLBACK_BASE']+'/google'}
        for client in result['clients'].values():
            try:client['next_schedule']=public_schedule(client)
            except ValueError:client['next_schedule']=dict(next_at='',label='Check schedule settings')
            if client.get('cloud_route_id'):
                route=next((r for r in result['cloud'].get('routes',[]) if r['id']==client['cloud_route_id']),None)
                if route:
                    client['next_schedule']=route['plan']
                    client['next_video']=route['plan'].get('next_video')
        return result


def cli(*args, token=None, data=None, timeout=120):
    executable = helpers.find_gh()
    if not executable:
        raise ValueError("Use Prepare GitHub CLI first.")
    environment = dict(os.environ, GH_PROMPT_DISABLED="1", GH_PAGER="cat")
    for key in ("GH_DEBUG", "GH_TOKEN", "GITHUB_TOKEN"):
        environment.pop(key, None)
    if token:
        environment["GH_TOKEN"] = token
    result = subprocess.run([executable, *args], input=data, capture_output=True, text=True,
        encoding="utf-8", timeout=timeout, env=environment,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise ValueError("GitHub CLI request failed. Check selected account authorization, repo/workflow permissions and internet.")
    return result.stdout.strip()


def connection(connection_id, kind=None):
    value = STATE["connections"].get(connection_id)
    if not value or (kind and value["kind"] != kind):
        raise ValueError("Select a valid " + (kind or "saved") + " connection.")
    return value


def gh_for(connection_id):
    c = connection(connection_id, "github")
    return GitHub(VAULT.get(connection_id)["token"], c["login"])


def github_for_client(client):
    github = gh_for(client["github_connection"])
    if github.owner.casefold() != client["owner"].casefold():
        raise ValueError("Pinned repository owner/connection mismatch. Stopped without changing client.")
    return github


def begin(label, callback, audit_client=None):
    with LOCK:
        if JOB["busy"]:
            raise ValueError("Wait for the current operation to finish. Workers continue independently on GitHub.")
        JOB.update(busy=True, label=label, notice=label, auth_url="", output="", outcome="running",result={})
    def work():
        try:
            result = callback()
            JOB['result']=({'connection_id':result} if isinstance(result,str) else
                {k:v for k,v in (result or {}).items() if k in ('id','connection_id','linked','source_stats','source_verified','rows','ready','preserves','updated','migrated','app_id','notice','records','channel_id','folder_id','active_runs','old_runner')} if isinstance(result,dict) else {})
            JOB["notice"] = label + " complete."
            store().activity(result if isinstance(result,dict) and "id" in result else (audit_client or {}), label, "Completed")
            JOB["outcome"] = "success"
        except Exception as error:
            JOB["notice"] = safe_error(str(error))
            store().activity(audit_client or {}, label, "Needs attention")
            JOB["outcome"] = "error"
        finally:
            try:
                save_local()
                if os.getenv("DRIVECAST_HOSTED")!="1" and not label.startswith("Cloud") and STATE.get("remote") and label not in ("Sync connections, stats and records", "Automatic sync", "Save encrypted GitHub recovery", "Fetch GitHub workspace", "Automation checks"):
                    try:
                        save_remote()
                    except Exception as error:
                        JOB["notice"] += " Local data saved; GitHub recovery checkpoint needs Sync: " + safe_error(str(error))
            finally:
                JOB.update(busy=False, auth_url="")
    threading.Thread(target=work, daemon=True).start()


def import_github(login=""):
    if not VAULT.unlocked:
        raise ValueError("Unlock connections with your unlock password.")
    if login and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", login):
        raise ValueError("Enter a valid GitHub username.")
    token = cli("auth", "token", "--hostname", "github.com", *(["--user", login] if login else []))
    identity = GitHub(token).request("/user")
    if login and identity["login"].casefold() != login.casefold():
        raise ValueError("Authorized GitHub identity did not match requested account.")
    name = identity["login"]
    cid = "gh:" + name.lower()
    VAULT.put(cid, {"token": token})
    STATE["connections"][cid] = dict(id=cid, kind="github", label=name, login=name,
        status="Connected", checked_at=now(), notice="Verified account; every client stays pinned to its selected owner.")
    upgrade.avatar(context(), STATE['connections'][cid], identity.get('avatar_url',''))
    save_local()
    return cid


def login_github(body=None):
    if not helpers.find_gh():
        helpers.install_gh()
    environment = dict(os.environ, GH_PROMPT_DISABLED="1", GH_PAGER="cat",
        GH_BROWSER='"'+sys.executable.replace('\\','/')+'" "'+str(ROOT/'private_browser.py').replace('\\','/')+'"')
    JOB['auth_url']='https://github.com/login/device'
    JOB['notice']='Sign in with the GitHub account that will own this client repository. A separate private browser profile will open.'
    for key in ("GH_DEBUG", "GH_TOKEN", "GITHUB_TOKEN"):
        environment.pop(key, None)
    process = subprocess.Popen([helpers.find_gh(), "auth", "login", "--hostname", "github.com", "--web",
        "--git-protocol", "https", "--scopes", "repo,workflow"], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", env=environment,
        creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
    timer = threading.Timer(600, process.kill)
    timer.start()
    try:
        for line in process.stdout:
            # gh device flow emits a short device code and a public verification URL, never the access token.
            JOB["output"] = (JOB["output"] + line)[-1800:]
        if process.wait():
            raise ValueError("GitHub authorization did not complete. Try Connect another account again.")
    finally:
        timer.cancel()
    return import_github((body or {}).get('login',''))


def google_credentials(cid):
    c = connection(cid)
    scopes = [helpers.SCOPES[0]] if c["kind"] == "drive" else [helpers.SCOPES[1], helpers.CHANNEL_SCOPE]
    value = VAULT.get(cid)
    if c['kind']=='youtube': scopes=value.get('scopes') or scopes
    credentials = Credentials(token=None, client_id=value["CLIENT_ID"], client_secret=value["CLIENT_SECRET"],
        refresh_token=value["REFRESH_TOKEN"], token_uri="https://oauth2.googleapis.com/token", scopes=scopes)
    try:
        credentials.refresh(Request())
    except RefreshError:
        raise ValueError("Google permission expired/revoked. Reconnect this saved connection using the same Desktop JSON.") from None
    return credentials


def check_google(cid):
    c = connection(cid)
    credentials = google_credentials(cid)
    if c["kind"] == "youtube":
        with build("youtube", "v3", credentials=credentials, cache_discovery=False) as yt:
            items = yt.channels().list(part="id,snippet,statistics", mine=True).execute(num_retries=2).get("items", [])
            if len(items) != 1:
                raise ValueError("Exactly one YouTube channel must be selected in Google consent.")
            item = items[0]
            if c.get("channel_id") and c["channel_id"] != item["id"]:
                raise ValueError("Wrong channel on reconnect. Original channel identity preserved.")
            c.update(channel_id=item["id"], label=item["snippet"]["title"], stats=item.get("statistics", {}))
            thumbnails=item['snippet'].get('thumbnails',{})
            upgrade.avatar(context(),c,(thumbnails.get('medium') or thumbnails.get('default') or {}).get('url',''))
        google_identity.update(c,credentials,VAULT.get(cid).get('scopes',()),now())
        try:
            from datetime import timedelta
            end=datetime.now(timezone.utc).date()-timedelta(days=2)
            with build('youtubeAnalytics','v2',credentials=credentials,cache_discovery=False) as analytics:
                report=analytics.reports().query(ids='channel==MINE',startDate=(end-timedelta(days=29)).isoformat(),endDate=end.isoformat(),metrics='views').execute(num_retries=1)
                c['analytics']=dict(views_30d=(report.get('rows') or [[None]])[0][0],start=(end-timedelta(days=29)).isoformat(),end=end.isoformat(),checked_at=now())
        except Exception:
            c.setdefault('analytics',{})['notice']='30-day analytics unavailable. Enable YouTube Analytics API and reconnect with analytics permission.'
    else:
        with build("drive", "v3", credentials=credentials, cache_discovery=False) as drive:
            user = drive.about().get(fields="user(displayName,emailAddress)").execute(num_retries=2).get("user", {})
            c["label"] = user.get("emailAddress") or user.get("displayName") or c["label"]
    c.update(status="Connected", checked_at=now(), notice="Google access verified.")


def connect_google(body):
    kind = body.get("kind")
    if kind not in ("youtube", "drive"):
        raise ValueError("Choose YouTube or Drive connection.")
    expected_channel=str(body.get('expected_channel_id','')).strip()
    if expected_channel and (kind!='youtube' or not re.fullmatch(r'UC[A-Za-z0-9_-]{22}',expected_channel)):
        raise ValueError('Reconnect requires the original valid YouTube channel ID.')
    config = body.get("oauth") or VAULT.values.get('google-desktop')
    if not isinstance(config, dict) or not isinstance(config.get('installed') or config.get('web'),dict):
        raise ValueError('Choose your Google OAuth JSON. Local mode uses Desktop; hosted mode uses Web application.')
    credentials_config=config.get('installed') or config.get('web')
    if os.getenv('DRIVECAST_PUBLIC_CALLBACK_BASE') and not config.get('web'):
        raise ValueError('Hosted browser authorization requires a Web OAuth client with the callback shown in YouTube.')
    if config.get('web') and not os.getenv('DRIVECAST_PUBLIC_CALLBACK_BASE'):
        raise ValueError('Use Desktop OAuth JSON in local mode. Web OAuth requires the HTTPS accounts gateway.')
    for key in ("client_id", "client_secret"):
        if not credentials_config.get(key):
            raise ValueError("OAuth JSON missing " + key)
    existing = body.get("id", "")
    if existing:
        connection(existing, kind)
    cid = existing or kind + ":" + secrets.token_hex(8)
    def announce(message):
        if message.startswith("https://accounts.google.com/"):
            JOB["auth_url"] = message
    scopes = [helpers.SCOPES[0]] if kind == "drive" else [helpers.SCOPES[1], helpers.CHANNEL_SCOPE]
    if kind=='youtube':scopes=[*scopes,*google_identity.SCOPES]
    if kind=='youtube' and body.get('analytics') is True: scopes=[*scopes,'https://www.googleapis.com/auth/yt-analytics.readonly']
    values = authorize(config, scopes=scopes, announce=announce, open_browser=False)
    values['scopes']=scopes
    previous = copy.deepcopy(STATE["connections"].get(cid))
    if not previous:
        STATE["connections"][cid] = dict(id=cid, kind=kind, label=body.get("label") or kind.title(), status="Checking")
    # Verify identity using temporary credentials before replacing an existing connection.
    old_secret = copy.deepcopy(VAULT.values.get(cid))
    VAULT.values[cid] = values
    # A new consent can be from another manager of the same channel. Never carry
    # the previous manager's email into that new grant if identity lookup fails.
    for key in google_identity.FIELDS:STATE['connections'][cid].pop(key,None)
    try:
        check_google(cid)
        if kind == "youtube":
            channel = STATE["connections"][cid]["channel_id"]
            requested=str(body.get('label','')).strip()
            if body.get('require_exact_name') is True and not existing and (not requested or requested.casefold()!=STATE['connections'][cid]['label'].strip().casefold()):
                raise ValueError('Google returned '+STATE['connections'][cid]['label']+' instead of '+(requested or 'a named channel')+'. No connection saved. Select the intended channel in Google sign-in.')
            if expected_channel and channel!=expected_channel:
                raise ValueError('Google returned '+STATE['connections'][cid]['label']+' instead of the intended channel. Select the original channel '+expected_channel+' in Google consent. Original client preserved.')
            duplicate = next((c for c in STATE["connections"].values() if c["id"] != cid and c["kind"] == "youtube" and c.get("channel_id") == channel), None)
            if duplicate:
                raise ValueError("This channel is already saved. Use Reconnect on its existing connection.")
        VAULT.save()
        if body.get('save_default',True):
            VAULT.put('google-desktop',config)
        STATE['connections'][cid]['requested_name']=str(body.get('label','')).strip()
        STATE['connections'][cid]['project_id']=credentials_config.get('project_id') or credentials_config['client_id'].split('-')[0]
    except Exception:
        if previous:
            STATE["connections"][cid] = previous
        else:
            STATE["connections"].pop(cid, None)
        if old_secret is None:
            VAULT.values.pop(cid, None)
        else:
            VAULT.values[cid] = old_secret
        raise
    JOB["auth_url"] = ""
    for c in STATE["clients"].values():
        # Explicitly adopt a migrated legacy client only when exact original channel matches.
        if kind == "youtube" and not c.get("youtube_connection") and c.get("channel_id") == STATE["connections"][cid].get("channel_id"):
            c["youtube_connection"] = cid
        if c.get("deployed") and c.get('runner','github')=='github' and os.getenv('DRIVECAST_HOSTED')!='1' and cid in (c.get("youtube_connection"), c.get("drive_connection")):
            provision_google_secrets(c,cid)
    save_local()

    if kind == 'drive' and STATE.get('drive_project',{}).get('folder_id') and not STATE.get('drive_root'):
        try:
            drive_sources.discover(context(),dict(connection_id=cid,folder_id=STATE['drive_project']['folder_id']))
            STATE['drive_project'].pop('notice',None)
        except Exception as error:
            STATE['drive_project']['notice']=safe_error(str(error))
        save_local()
    if kind == 'youtube':
        # A channel can be authorized remotely after its folder was created.
        # Update discovery without turning a successful grant into an OAuth error.
        drive_sources.refresh_if_due(context(), force=True)
    return {'connection_id':cid}




def scan_source(client):
    client['source_verified']=False
    if client.get('source_type')!='drive':raise ValueError('Only Google Drive sources are supported.')
    root, selected=drive_sources.selected(context(),client.get('folder_id',''),client.get('drive_connection',''))
    connection(root['connection_id'],'drive')
    credentials=google_credentials(root['connection_id'])
    with build('drive','v3',credentials=credentials,cache_discovery=False) as drive:
        drive_sources.verify_ancestry(drive,client['folder_id'],root['folder_id'])
        folder=drive.files().get(fileId=client['folder_id'],fields='id,name,mimeType,capabilities(canAddChildren)',supportsAllDrives=True).execute(num_retries=2)
        if folder.get('mimeType')!=helpers.FOLDER_MIME or not folder.get('capabilities',{}).get('canAddChildren'):
            raise ValueError('The project Drive account needs Editor access to this subfolder.')
        from main import list_files
        files=list_files(drive,client['folder_id'])
        videos=[f for f in files if Path(f['name']).suffix.lower() in ('.mp4','.mov','.mkv') and f['mimeType']!=helpers.FOLDER_MIME]
        client.update(folder_name=folder['name'],folder_path=selected.get('path',folder['name']),drive_connection=root['connection_id'],drive_root_id=root['folder_id'])
        client['source_stats']=dict(video_count=len(videos),available=len(videos),checked_at=now(),
            preview=[dict(id=f['id'],title=f['name'],timestamp=f.get('createdTime',''),available=True) for f in videos[:30]])
    client.update(source_verified=True,needs_drive_folder=False)


def add_client(body):
    source = body.get("source_type", "drive")
    if source != "drive":
        raise ValueError("This project uses Google Drive only.")
    existing = STATE["clients"].get(body.get("id", ""))
    if body.get("id") and not existing:
        raise ValueError("Client not found.")
    if existing and existing.get("enabled"):
        if body.get('confirmed') is not True:
            raise ValueError('Confirm Save and redeploy. The existing pipeline will be paused before changing its settings.')
    if existing and existing.get('deployed') and body.get('confirmed') is not True:
        raise ValueError('Confirm Save and redeploy before changing a deployed client.')
    name = str(body.get("name", "")).strip()
    if not name or len(name) > 120:
        raise ValueError("Use a client name between 1 and 120 characters.")
    gh_connection = connection(body.get("github_connection") or upgrade.assign(context()), "github")
    if existing and existing.get('github_connection')!=gh_connection['id']:
        previous_account=connection(existing['github_connection'],'github')
        if previous_account.get('status')=='Account suspended' or previous_account.get('health',{}).get('issue')=='suspended':
            raise ValueError('GitHub account suspended. Appeal through GitHub before changing the worker account.')
    if not existing or existing.get('github_connection')!=gh_connection['id']:
        upgrade.ensure_slot(context(),gh_connection['id'],existing['id'] if existing else '')
    yt = connection(body.get("youtube_connection"), "youtube")
    if not yt.get("channel_id"):
        raise ValueError("Authorize and verify the YouTube connection first.")
    repo = str(body.get("repo_name", "")).strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", repo):
        raise ValueError("Use letters, numbers or dashes in the repository name.")
    privacy = 'public'
    client = dict(existing or {}, id=existing["id"] if existing else secrets.token_hex(8), name=name,
        source_type=source, owner=gh_connection["login"], github_connection=gh_connection["id"],
        youtube_connection=yt["id"], channel_id=yt["channel_id"], channel_title=yt["label"], repo_name=repo,
        privacy=privacy, category="22", stats=yt.get("stats", {}),
        verified=False, source_verified=False, notice="Checking source and exact channel.")
    root, selected=drive_sources.selected(context(),helpers.parse_folder(str(body.get('folder_id',''))),body.get('drive_connection',''))
    client.update(drive_connection=root['connection_id'],folder_id=selected['id'],folder_path=selected.get('path',selected['name']),drive_root_id=root['folder_id'])
    controls.validate_options(context(),client,body)
    tags=body.get('tags',client.get('tags',[]))
    if isinstance(tags,str):tags=tags.replace('|',',').split(',')
    if not isinstance(tags,list):raise ValueError('Tags must be a list or comma-separated names.')
    client['tags']=list(dict.fromkeys(str(t).strip()[:60] for t in tags if str(t).strip()))[:30]
    for other in STATE["clients"].values():
        if other["id"] == client["id"]:
            continue
        if other.get("channel_id") == client["channel_id"] or (other.get("owner", "").lower() == client["owner"].lower() and other["repo_name"].lower() == repo.lower()):
            raise ValueError("Channel/repository already assigned. Duplicate client pipelines are blocked.")
        if source == "drive" and other.get("folder_id") == client["folder_id"]:
            raise ValueError("This Drive source folder already has a client pipeline.")
    check_google(yt["id"])
    github_for_client(client).request("/user")
    scan_source(client)
    controls.preserve_route(context(),existing,client)
    client.update(verified=True, status="Ready" if not client.get("deployed") else "Paused", enabled=False,
                  notice="Source and exact destination verified. Normal publishing is Public; private test is optional.")
    STATE["clients"][client["id"]] = client
    save_local()
    STATE.pop('draft',None)
    if existing and existing.get('deployed'):
        deploy(client)
    return client


def set_variable(github, client, key, value):
    path = github.repo_path(client["repo_name"]) + "/actions/variables"
    if not str(value).strip():
        try:github.request(path+'/'+key,'DELETE')
        except GitHubError as error:
            if error.status!=404:raise
        return
    try:
        github.request(path, "POST", {"name": key, "value": str(value)})
    except GitHubError as error:
        if error.status not in (409,422):
            raise
        if error.status==422:
            github.request(path+'/'+key)  # Only patch a confirmed existing variable, not an unrelated validation failure.
        github.request(path + "/" + key, "PATCH", {"name": key, "value": str(value)})


def provision_secrets(client):
    github = github_for_client(client)
    yt = VAULT.get(client["youtube_connection"])
    values = {"CLIENT_ID": yt["CLIENT_ID"], "CLIENT_SECRET": yt["CLIENT_SECRET"], "YOUTUBE_REFRESH_TOKEN": yt["REFRESH_TOKEN"]}
    drive = VAULT.get(client['drive_connection'])
    values.update(REFRESH_TOKEN=drive['REFRESH_TOKEN'],FOLDER_ID=client['folder_id'],
                  DRIVE_CLIENT_ID=drive['CLIENT_ID'],DRIVE_CLIENT_SECRET=drive['CLIENT_SECRET'])
    bot=VAULT.values.get('telegram-bot',{})
    if bot.get('chat_id'):
        values.update(TELEGRAM_BOT_TOKEN=bot['token'],TELEGRAM_CHAT_ID=bot['chat_id'],
            TELEGRAM_SETTINGS=json.dumps(dict(STATE.get('telegram',{}).get('settings',{}),not_before=STATE.get('telegram',{}).get('linked_at',now()))))
    for key, value in values.items():
        cli("secret", "set", key, "--repo", client["owner"] + "/" + client["repo_name"], token=github.token, data=value)


def provision_google_secrets(client,cid):
    """Reconnect one Google account without requiring unrelated legacy secrets."""
    github=github_for_client(client)
    check_repository(github,client)
    saved=VAULT.get(cid)
    if cid==client.get('youtube_connection'):
        values=dict(CLIENT_ID=saved['CLIENT_ID'],CLIENT_SECRET=saved['CLIENT_SECRET'],YOUTUBE_REFRESH_TOKEN=saved['REFRESH_TOKEN'])
    elif cid==client.get('drive_connection'):
        values=dict(REFRESH_TOKEN=saved['REFRESH_TOKEN'],FOLDER_ID=client['folder_id'],DRIVE_CLIENT_ID=saved['CLIENT_ID'],DRIVE_CLIENT_SECRET=saved['CLIENT_SECRET'])
    else:raise ValueError('Google connection does not belong to this client.')
    for key,value in values.items():
        cli('secret','set',key,'--repo',client['owner']+'/'+client['repo_name'],token=github.token,data=value)


def check_repository(github, client):
    repo = github.repository(client["repo_name"])
    if not repo["private"] or (client.get("repo_id") and repo["id"] != client["repo_id"]):
        raise ValueError("Repository privacy/identity changed. Stopped.")
    return repo


def deploy(client):
    if not client.get("verified") or not client.get("source_verified"):
        raise ValueError("Client source and exact YouTube channel must pass validation first.")
    client.setdefault('schedule_mode','smart')
    github = github_for_client(client)
    if not client.get("repo_id"):
        try:
            github.repository(client["repo_name"])
        except GitHubError as error:
            if error.status != 404:
                raise
        else:
            raise ValueError("Repository name already exists. Choose a new name; existing files will not be overwritten.")
        repo = github.create_private(client["repo_name"], "DriveCast client pipeline: " + client["name"])
        client.update(repo_id=repo["id"], branch=repo["default_branch"], repo_url=repo["html_url"], enabled=False)
        save_local()
    repo = check_repository(github, client)
    active = github.request(github.repo_path(client["repo_name"]) + "/actions/runs?per_page=30").get("workflow_runs", [])
    if any(r["status"] != "completed" for r in active):
        raise ValueError("A workflow is active. Wait until it finishes before upgrading/deploying.")
    set_variable(github, client, "ENABLED", "false")
    client["enabled"] = False
    save_local()
    provision_secrets(client)
    for key, value in dict(SOURCE_TYPE="drive", EXPECTED_CHANNEL_ID=client["channel_id"], CLIENT_NAME=client["name"],
        PRIVACY='public', CATEGORY_ID="22", UPLOAD_LIMIT=str(client.get('max_uploads_per_day',2)), DRY_RUN="true",
        MAX_UPLOADS_PER_DAY=str(client.get('max_uploads_per_day',2)),CHECK_MINUTES=str(client.get('check_minutes',60)),
        DRIVE_MAIN_FOLDER_ID=client.get('drive_root_id',''),YOUTUBE_LEDGER_PATH=client.get('youtube_ledger_path','records/ledger.json'),
        SCHEDULE_MODE=client.get('schedule_mode','smart'),SCHEDULE_TIMEZONE=client.get('schedule_timezone','America/Los_Angeles'),
        SCHEDULE_WINDOWS=client.get('schedule_windows','12:00-14:00,18:00-21:00')).items():
        set_variable(github, client, key, value)
    controls.initialize_ledgers(context(),client,github)
    files={name:(ROOT/name).read_text(encoding='utf-8') for name in RUNTIME}
    files['.github/workflows/upload.yml']=controls.workflow(context(),client)
    github.snapshot(client["repo_name"], files,repo['default_branch'], message='Update DriveCast guided Public publishing and independent destinations')
    github.request(github.repo_path(client["repo_name"])+"/actions/workflows/upload.yml/enable","PUT")
    client.update(deployed=True, worker_version=5, schedule_deployed=True, branch=repo["default_branch"], status="Deployed — paused", test_passed=False,
                  notice="Read-only dry run requested. Review it, then Enable Public publishing; private test is optional.")
    save_local()
    dispatch(client, dry=True)


def dispatch(client, dry=True, private=True):
    github = github_for_client(client)
    path = github.repo_path(client["repo_name"]) + "/actions/workflows/upload.yml/dispatches"
    for attempt in range(5):
        try:
            github.request(path, "POST", {"ref": client.get("branch", "main"), "inputs": {"dry_run": dry, **({"private_test": private} if client.get("worker_version", 1) >= 2 else {})}})
            return
        except GitHubError as error:
            if error.status != 404 or attempt == 4:
                raise
            time.sleep(3)


def client_action(body):
    target=STATE["clients"].get(body.get("id"),{})
    if target.get("runner")=="azure":
        return cloud_control.action(context(),dict(body,id=target.get("cloud_route_id",target["id"])))
    if os.getenv("DRIVECAST_HOSTED")=="1":
        raise ValueError("Legacy GitHub worker is read-only here. Review an explicit Azure handover first.")
    c = STATE["clients"].get(body.get("id"))
    if not c:
        raise ValueError("Client not found.")
    action = body.get("action")
    if c.get("needs_drive_folder") and action not in ("pause",):raise ValueError("Select and confirm a Drive subfolder for this channel first.")
    github = github_for_client(c)
    if action == "deploy":
        deploy(c)
    elif action == "pause":
        set_variable(github, c, "ENABLED", "false")
        c.update(enabled=False, status="Paused", notice="Future runs paused. Already-running jobs may finish.")
    elif action == "dry_run":
        dispatch(c, dry=True)
        c["notice"] = "Read-only dry run requested; inspect workflow records."
    elif action == "test":
        if c.get("worker_version", 1) < 2:
            raise ValueError("Upgrade this pipeline before requesting a test with guaranteed private metadata.")
        set_variable(github, c, "ENABLED", "false")
        set_variable(github, c, "DRY_RUN", "false")
        c.update(enabled=False, test_passed=False, test_started=now(), status="Private test requested")
        save_local()
        dispatch(c, dry=False, private=True)
    elif action in ("enable","start"):
        if body.get("confirmed") is not True or not c.get("verified"):
            raise ValueError('Confirm Start Public publishing for this verified YouTube channel.')
        if c.get("worker_version", 1) < 2:
            raise ValueError("Upgrade the pipeline before enabling from this panel.")
        if not c.get('deployed'):
            raise ValueError('Save and prepare this channel before starting posting.')
        check_google(c["youtube_connection"])
        scan_source(c)
        Ledger(github,c["repo_name"],c["channel_id"],path=c.get('youtube_ledger_path','records/ledger.json')).load(required=True)
        for key,value in dict(PRIVACY='public',DRY_RUN="false",ENABLED="true").items():
            set_variable(github,c,key,value)
        c.update(enabled=True,status="Enabled",notice='Public publishing enabled. New complete videos publish when the next source check finds them.')
        if action=='start':
            dispatch(c,dry=False,private=False)
            c['notice']='First Public upload check queued. Automatic checks continue at the configured interval.'
    else:
        raise ValueError("Unknown client action.")
    save_local()


def save_remote():
    remote = STATE.get("remote", {})
    if not remote:
        return
    github = gh_for(remote["connection"])
    repo = github.repository(remote["repo"])
    if not repo["private"] or repo["id"] != remote["repo_id"]:
        raise ValueError("Recovery repository privacy/identity changed; backup stopped.")
    files = {"workspace.json": json.dumps(STATE, ensure_ascii=False, indent=2),
        "records.json": json.dumps(store().data(), ensure_ascii=False),
        "README.md": "# DriveCast private recovery\nLocal-only panel. workspace.json and records.json contain client metadata. connections.enc.json is AES-GCM encrypted; Recovery Password is never uploaded. Keep this repository private.\n"}
    if VAULT.path.exists():
        files["connections.enc.json"] = VAULT.path.read_text()
    head = github.snapshot(remote["repo"], files, remote["branch"], remote.get("head"))
    remote.update(head=head, saved_at=now())
    save_local()


def configure_remote(body):
    if not VAULT.unlocked:
        raise ValueError("Unlock/create Recovery Password first.")
    cid = body.get("connection")
    github = gh_for(cid)
    name = str(body.get("repo", "drivecast-control")).strip()
    existing = STATE.get("remote", {})
    if existing:
        if existing["connection"] != cid or existing["repo"] != name:
            raise ValueError("Recovery destination is pinned. Use a new workspace to select another destination.")
        save_remote()
        return
    try:
        repo = github.repository(name)
    except GitHubError as error:
        if error.status != 404:
            raise
        repo = github.create_private(name, "Private DriveCast settings, encrypted connections and records")
        STATE["remote"] = dict(connection=cid, owner=github.owner, repo=name, repo_id=repo["id"], branch=repo["default_branch"], head="")
        save_local()
    else:
        raise ValueError("Repository already exists. Use Fetch from GitHub to recover it; nothing overwritten.")
    save_remote()


def fetch_remote(body):
    github = gh_for(body.get("connection"))
    repo_name = str(body.get("repo", "drivecast-control")).strip()
    repo = github.repository(repo_name)
    if not repo["private"]:
        raise ValueError("Recovery repository must be private.")
    head = github.request(github.repo_path(repo_name) + "/git/ref/heads/" + repo["default_branch"])["object"]["sha"]
    raw, _ = github.read_file(repo_name, "workspace.json", head)
    restored = json.loads(raw)
    if restored.get("version") != 2 or not restored.get("workspace_id"):
        raise ValueError("This is not a DriveCast recovery workspace.")
    if STATE.get("clients") and restored["workspace_id"] != STATE["workspace_id"] and body.get("replace") is not True:
        raise ValueError("Different workspace. Tick Replace local workspace only if you intend to restore it; a private local backup will be kept.")
    encrypted, _ = github.read_file(repo_name, "connections.enc.json", head)
    record_data, _ = github.read_file(repo_name, "records.json", head)
    if not isinstance(restored.get("clients"),dict) or not isinstance(restored.get("connections"),dict):
        raise ValueError("Invalid workspace schema; restore stopped.")
    for key,value in restored["connections"].items():
        if value.get("id") != key or value.get("kind") not in ("github","youtube","drive"):
            raise ValueError("Invalid connection schema in recovery file.")
    trial_path = WORK / "restore-check.enc.json"
    trial_path.write_bytes(encrypted)
    trial = Vault(trial_path)
    try:
        trial.unlock(body.get("password", ""))
    finally:
        trial_path.unlink(missing_ok=True)
    validated_records = WORK / "restore-check.sqlite3"
    try:
        RecordStore(validated_records).restore_data(json.loads(record_data))
    except Exception:
        validated_records.unlink(missing_ok=True)
        raise ValueError("Record backup invalid; local workspace not replaced.") from None
    backup = WORK / "recovery-backups" / now().replace(":", "-")
    backup.mkdir(parents=True, exist_ok=True)
    for path in (STATE_PATH, VAULT.path, WORK / "records.sqlite3"):
        if path.exists():
            shutil.copy2(path, backup / path.name)
    with LOCK:
        STATE.clear()
        STATE.update(restored)
        remote = STATE["remote"]
        remote.update(head=head, repo_id=repo["id"], branch=repo["default_branch"], owner=github.owner, repo=repo_name)
        VAULT.key, VAULT.salt, VAULT.values = trial.key, trial.salt, trial.values
        current_id = "gh:" + github.owner.lower()
        if current_id in STATE["connections"]:
            VAULT.values[current_id] = {"token":github.token}
        VAULT.save()
        validated_records.replace(WORK/"records.sqlite3")
        STATE["notice"] = "Workspace and encrypted connections recovered. Sync will test current platform validity; expired permissions need reconnect."
        save_local()


def sync():
    failures = []
    for cid, c in list(STATE["connections"].items()):
        JOB["notice"] = "Testing " + c["label"]
        try:
            if c["kind"] == "github":
                if os.getenv("DRIVECAST_HOSTED")=="1": continue
                if cid not in VAULT.values:
                    import_github(c["login"])
                identity = gh_for(cid).request("/user")["login"]
                if identity.casefold() != c["login"].casefold():
                    raise ValueError("GitHub identity mismatch.")
            elif c["kind"] in ("drive", "youtube"):
                check_google(cid)
            c.update(status="Connected", checked_at=now(), notice="Connection test passed.")
        except Exception as error:
            suspended=c.get('kind')=='github' and getattr(error,'reason','')=='suspended'
            c.update(status="Account suspended" if suspended else "Needs reconnect", checked_at=now(), notice=safe_error(str(error)))
            if c.get('kind')=='github':
                c['health']=dict(checked_at=now(),token_valid=False,issue=getattr(error,'reason','unknown'),
                    quota_issue=getattr(error,'reason','')=='rate_limit',notice=c['notice'])
            failures.append(c["label"])
    for c in STATE["clients"].values():
        JOB["notice"] = "Syncing " + c["name"]
        client_errors = []
        if c.get("youtube_connection"):
            try:
                yt = connection(c["youtube_connection"], "youtube")
                if yt.get("status") != "Connected":
                    raise ValueError("YouTube connection needs attention; cached stats retained.")
                if yt.get("channel_id") != c["channel_id"]:
                    raise ValueError("YouTube channel identity mismatch.")
                c["stats"] = dict(yt.get("stats", {}), checked_at=yt["checked_at"])
                c["credential_status"] = "YouTube connection valid"
            except Exception as error:
                client_errors.append(safe_error(str(error)))
        if c.get("drive_connection"):
            try:
                c["source_verified"] = False
                scan_source(c)
            except Exception as error:
                client_errors.append(safe_error(str(error)))
        if c.get("deployed") and c.get("runner","github")=="github" and os.getenv("DRIVECAST_HOSTED")!="1":
            try:
                github = github_for_client(c)
                check_repository(github, c)
                variables = github.request(github.repo_path(c["repo_name"]) + "/actions/variables?per_page=100")
                values = {v["name"]:v["value"] for v in variables.get("variables",[])}
                c.update(enabled=values.get("ENABLED", "false") == "true", actual_privacy=values.get("PRIVACY", "private"))
                runs = github.request(github.repo_path(c["repo_name"]) + "/actions/workflows/upload.yml/runs?per_page=100").get("workflow_runs", [])
                if runs:
                    latest=runs[0]
                    c['last_run']={k:latest.get(k) for k in ('id','status','conclusion','created_at','updated_at','html_url')}
                for run in runs:
                    for attempt in range(1,int(run.get("run_attempt",1))+1):
                        if store().cached(c["id"], run["id"], attempt):
                            continue
                        metadata = run if attempt == int(run.get("run_attempt",1)) else github.request(github.repo_path(c["repo_name"])+f"/actions/runs/{run['id']}/attempts/{attempt}")
                        try:
                            log = cli("run", "view", str(run["id"]), "--repo", c["owner"]+"/"+c["repo_name"],
                                "--attempt", str(attempt), "--log", token=github.token) if metadata["status"] == "completed" else None
                            store().import_run(c,metadata,log,"imported" if log is not None else "pending")
                        except Exception:
                            store().import_run(c,metadata,logs_state="unavailable")
                ledger = Ledger(github,c["repo_name"],c["channel_id"],path=c.get('youtube_ledger_path','records/ledger.json')).load(required=c.get("worker_version",1)>=2)
                c['schedule_usage']=schedule_usage(ledger,c)
                store().import_ledger(c,ledger)
                controls.import_remote_stats(context(),c,github)
                if c.get("test_started"):
                    c["test_passed"] = any(item.get("status") == "Done" and item.get("privacy") == "private" and item.get("recorded_at", "") >= c["test_started"]
                        for item in ledger.get("items",{}).values())
                c["status"] = "Enabled" if c["enabled"] else "Paused"
            except Exception as error:
                client_errors.append(safe_error(str(error)))
        c["notice"] = "; ".join(client_errors) if client_errors else "Available connections, channel stats, source and GitHub records updated."
        if client_errors:
            failures.append(c["name"])
    controls.sync_stats(context())
    STATE.update(last_sync=now(), notice="Sync finished. " + ("Needs attention: " + ", ".join(failures) if failures else "Connections, video/channel stats and records checked."))
    store().backup()
    save_local()
    if STATE.get("remote") and os.getenv("DRIVECAST_HOSTED")!="1":
        save_remote()


def backup_bytes():
    return portable_backup.export(context())


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*_):
        pass

    def safe_host(self):
        return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

    def authorized(self):
        return self.safe_host() and secrets.compare_digest(self.headers.get("X-Setup-Token",""),TOKEN)

    def send(self,status,payload,kind="application/json"):
        raw = payload if isinstance(payload,bytes) else (json.dumps(payload,ensure_ascii=False).encode() if kind=="application/json" else payload.encode())
        self.send_response(status)
        self.send_header("Content-Type",kind + ("; charset=utf-8" if kind != "application/zip" else ""))
        self.send_header("Content-Length",str(len(raw)))
        self.send_header("Cache-Control","no-store")
        self.send_header("X-Content-Type-Options","nosniff")
        self.send_header("Content-Security-Policy",f"default-src 'self'; script-src 'nonce-{TOKEN}'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if not self.safe_host():
            return self.send(403,{"error":"Invalid host"})
        if self.path=='/favicon.ico': return self.send(204,b'','image/x-icon')
        if self.path=='/healthz': return self.send(200,dict(status='ok',version='azure-v1'))
        if self.path=='/auth/session': return self.send(200,dict(signed_in=False,local_mode=True))
        assets = {"/":("panel.html","text/html"),"/panel.js":("panel.js","text/javascript"),"/panel.css":("panel.css","text/css"),
                  '/login':('login.html','text/html'),'/auth.js':('auth.js','text/javascript')}
        if re.fullmatch(r'/channels/[A-Za-z0-9_-]+',self.path):
            return self.send(200,(ROOT/'panel.html').read_text(encoding='utf-8').replace('__TOKEN__',TOKEN),'text/html')
        if re.fullmatch(r'/avatars/[a-f0-9]{64}\.img',self.path):
            path=WORK/self.path.lstrip('/')
            if path.exists(): return self.send(200,path.read_bytes(),path.with_suffix('.type').read_text())
            return self.send(404,{'error':'Image unavailable'})
        if self.path in assets:
            name,kind = assets[self.path]
            return self.send(200,(ROOT/name).read_text(encoding="utf-8").replace("__TOKEN__",TOKEN),kind)
        if not self.authorized():
            return self.send(403,{"error":"Panel session changed. Refresh this page."})
        if self.path == "/api/state":
            return self.send(200,public_state())
        if self.path == "/api/records":
            with LOCK:
                return self.send(200,store().data())
        if self.path == "/api/backup":
            try:
                with LOCK:
                    if JOB['busy']:raise ValueError('Wait for the current change to finish, then Export.')
                    return self.send(200,backup_bytes(),'application/octet-stream')
            except ValueError as error:return self.send(400,{'error':str(error)})
        if self.path.startswith("/api/export/"):
            kind = self.path.rsplit("/",1)[-1]
            if kind in ("uploads","runs","activity"):
                with LOCK:return self.send(200,store().csv(kind),"text/csv")
        self.send(404,{"error":"Not found"})

    def do_POST(self):
        if not self.authorized() or self.headers.get("Origin",f"http://127.0.0.1:{self.server.server_port}") != f"http://127.0.0.1:{self.server.server_port}":
            return self.send(403,{"error":"Invalid local session/origin. Refresh this page."})
        try:
            length = int(self.headers.get("Content-Length","0"))
            maximum=48*1024*1024 if self.path=='/api/import/preview' else 2*1024*1024
            if self.headers.get("Content-Type") != "application/json" or not 0 < length <= maximum:
                raise ValueError("Small JSON request required.")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body,dict):
                raise ValueError("JSON object required.")
            if self.path=='/api/oauth/callback':
                import oauth_web
                return self.send(200,oauth_web.receive(body))
            if self.path in ('/api/import/preview','/api/import/restore'):
                if JOB['busy']:raise ValueError('Wait for the current operation to finish before importing.')
                with LOCK:
                    result=(portable_backup.preview(context(),base64.b64decode(body.get('file',''),validate=True),body.get('password','')) if self.path.endswith('/preview') else
                            portable_backup.restore(context(),body.get('ticket',''),body.get('confirmed')))
                return self.send(200,result)
            if self.path=='/api/auth/open':
                return self.send(200,{'opened':open_current_auth()})
            if self.path == "/api/unlock":
                if JOB["busy"]:
                    raise ValueError("Wait for current operation to finish.")
                VAULT.unlock(body.get("password",""))
                if workspace_schema.drive_only(STATE,VAULT):save_local()
                if body.get("remember") is True:
                    VAULT.remember_windows()
                else:
                    VAULT.path.with_name('unlock.windows').unlink(missing_ok=True)
                if os.getenv('DRIVECAST_HOSTED')=='1':
                    key_path=VAULT.path.with_name('service-unlock.json')
                    key_path.write_text(json.dumps({'key':base64.b64encode(VAULT.key).decode()}))
                    key_path.chmod(0o600)
                return self.send(200,{"ok":True})
            if not VAULT.unlocked and self.path != "/api/prepare-gh":
                raise ValueError("Unlock connections first.")
            routes = {
                "/api/prepare-gh":("Prepare GitHub",helpers.install_gh),
                "/api/github/login":("Connect GitHub",lambda:login_github(body)),
                "/api/github/import":("Import signed-in GitHub",lambda:import_github(body.get("login",""))),
                "/api/google":("Connect " + str(body.get("kind","Google")),lambda:connect_google(body)),
                '/api/schedule':('Update publication windows',lambda:operations.schedules(context(),body)),
                '/api/migration/preview':('Check migration readiness',lambda:operations.migration_preview(context(),body)),
                '/api/migration':('Migrate verified GitHub pipelines',lambda:operations.migrate(context(),body)),
                '/api/draft':('Save draft',lambda:controls.draft(context(),body)),
                '/api/source/validate':('Validate source',lambda:controls.validate_source(context(),body)),
                '/api/auth/open':('Open private sign-in',lambda:open_current_auth()),
                "/api/client":("Validate client",lambda:add_client(body)),
                "/api/client/action":("Client " + str(body.get("action","")),lambda:client_action(body)),
                "/api/sync":("Sync connections, stats and records",sync),
                "/api/remote/save":("Save encrypted GitHub recovery",lambda:configure_remote(body)),
                "/api/remote/fetch":("Fetch GitHub workspace",lambda:fetch_remote(body)),
            }
            routes.update({path:('Workspace update',callback) for path,callback in upgrade.routes(context(),body).items()})
            routes.update({path:('Cloud update',callback) for path,callback in cloud_control.routes(context(),body).items()})
            routes.update({path:('Workspace cleanup',callback) for path,callback in workspace_cleanup.routes(context(),body).items()})
            if self.path.startswith('/api/github') or self.path in ('/api/prepare-gh','/api/migration','/api/migration/preview','/api/remote/save','/api/remote/fetch'):
                raise ValueError('GitHub integration was removed. Use the Azure worker pool.')
            if os.getenv('DRIVECAST_HOSTED')=='1' and self.path in ('/api/client','/api/bulk','/api/csv/import','/api/migration','/api/schedule','/api/remote/save','/api/remote/fetch','/api/automation','/api/prepare-gh'):
                raise ValueError('Azure uses Folder matching and Cloud previews. Legacy GitHub mutation is disabled in hosted mode.')
            if self.path not in routes:
                return self.send(404,{"error":"Unknown action"})
            label, callback = routes[self.path]
            begin(label,callback,STATE["clients"].get(body.get("id")))
            self.send(202,{"ok":True})
        except (ValueError,TypeError,KeyError) as error:
            self.send(400,{"error":safe_error(str(error))})
        except Exception:
            self.send(500,{"error":"Request failed. Existing data preserved; check local connection/disk space."})


def periodic():
    while True:
        time.sleep(21600)
        if VAULT.unlocked and not JOB["busy"]:
            try:
                begin("Automatic sync",sync)
            except ValueError:
                pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port",type=int,default=8765)
    parser.add_argument("--no-browser",action="store_true")
    parser.add_argument("--chrome",action="store_true")
    parser.add_argument('--accounts',action='store_true',help='Enable isolated user accounts instead of the legacy local workspace')
    args = parser.parse_args()
    if args.accounts or os.getenv('DRIVECAST_AUTH_MODE')=='accounts':
        from accounts import serve
        return serve(WORK/'accounts',args.port)
    url = f"http://127.0.0.1:{args.port}/"
    try:
        with urlopen(url,timeout=2) as response:
            html = response.read(128*1024).decode()
        if "DriveCast / Local control" in html:
            if not args.no_browser:
                helpers.open_panel(url,args.chrome)
            return
        raise RuntimeError("Older panel or another app is using port 8765. Close its server window, then launch again.")
    except OSError:
        pass
    load()
    server = ThreadingHTTPServer(("127.0.0.1",args.port),Handler)
    url = f"http://127.0.0.1:{server.server_port}/"
    print("DriveCast local control:",url,flush=True)
    print("Private loopback workspace. Unlock password protects saved connections; platform expiry remains separate.",flush=True)
    threading.Thread(target=periodic,daemon=True).start()
    threading.Thread(target=upgrade.maintenance,args=(context(),),daemon=True).start()
    if not args.no_browser:
        helpers.open_panel(url,args.chrome)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
