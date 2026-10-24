"""Provider-independent recovery targets. No network call belongs in an upload path."""
import hashlib
import io
import json
import os
from pathlib import Path
import re
import zipfile


def config():
    path=Path(os.getenv('DRIVECAST_RECOVERY_CONFIG','/run/drivecast-recovery.json'))
    return json.loads(path.read_text()) if path.is_file() else {}


def request(db):
    import time
    db.execute("INSERT OR REPLACE INTO cloud_meta VALUES('recovery:dirty',?)",(json.dumps(time.time()),))


def source_bundle(source=None):
    root=Path(source or Path(__file__).parent).resolve()
    manifest=json.loads((root/'source-files.json').read_text())
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED) as archive:
        for name in manifest['files']:
            path=(root/name).resolve()
            if not path.is_relative_to(root) or path.is_symlink() or any(p in ('work','.git','__pycache__') for p in Path(name).parts):
                raise ValueError('Unsafe source manifest.')
            if Path(name).name=='.env' or name.endswith(('.key','.encrypted','.enc.json')):
                raise ValueError('Private runtime file in source manifest.')
            raw=path.read_bytes()
            if name.endswith(('.sh','.yml','.py','.js','.html','.css','.md')):raw=raw.replace(b'\r\n',b'\n')
            entry=zipfile.ZipInfo(name,date_time=(2020,1,1,0,0,0));entry.compress_type=zipfile.ZIP_DEFLATED
            entry.create_system=3;entry.external_attr=0o100644<<16
            archive.writestr(entry,raw,compresslevel=9)
    return buffer.getvalue()


def drive_service(root,settings):
    from vault import Vault
    from cloud_store import CloudStore
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    uid=settings.get('workspace','')
    if not re.fullmatch('[a-f0-9]{32}',uid):raise ValueError('Invalid recovery workspace.')
    work=Path(root)/'accounts'/'users'/uid
    state=CloudStore(work/'records.sqlite3').meta('workspace:state',{})
    connection=state.get('connections',{}).get(settings.get('connection_id'),{})
    if connection.get('kind')!='drive':raise ValueError('Recovery Drive authorization missing.')
    vault=Vault(work/'connections.enc.json')
    if not vault.try_service_key():raise ValueError('Recovery Drive vault cannot be unlocked.')
    value=vault.get(settings['connection_id'])
    credentials=Credentials(None,refresh_token=value['REFRESH_TOKEN'],token_uri='https://oauth2.googleapis.com/token',
        client_id=value['CLIENT_ID'],client_secret=value['CLIENT_SECRET'])
    return build('drive','v3',credentials=credentials,cache_discovery=False)


def upload_drive(root,settings,raw,name,deduplicate=False):
    from googleapiclient.http import MediaIoBaseUpload
    folder=settings.get('folder_id','')
    if not re.fullmatch('[A-Za-z0-9_-]{10,200}',folder):raise ValueError('Invalid recovery folder.')
    with drive_service(root,settings) as drive:
        info=drive.files().get(fileId=folder,fields='id,mimeType,trashed').execute(num_retries=1)
        if info.get('trashed') or info.get('mimeType')!='application/vnd.google-apps.folder':raise ValueError('Recovery folder unavailable.')
        permissions=drive.permissions().list(fileId=folder,fields='permissions(type,role)').execute(num_retries=1).get('permissions',[])
        if not permissions or any(p.get('role')!='owner' or p.get('type')!='user' for p in permissions):
            raise ValueError('Recovery folder must remain private.')
        sha=hashlib.sha256(raw).hexdigest()
        if deduplicate:
            matches=drive.files().list(q="'"+folder+"' in parents and trashed=false and appProperties has { key='sha256' and value='"+sha+"' }",fields='files(id,size,md5Checksum,webViewLink)',pageSize=100).execute(num_retries=1).get('files',[])
            for match in matches:
                if int(match.get('size',-1))==len(raw) and match.get('md5Checksum')==hashlib.md5(raw).hexdigest():
                    return dict(verified=True,file_id=match['id'],file_url=match.get('webViewLink',''),sha256=sha)
        body=dict(name=name,parents=[folder],appProperties={'drivecastRecovery':'snapshot-v1','sha256':sha})
        media=MediaIoBaseUpload(io.BytesIO(raw),mimetype='application/octet-stream',resumable=False)
        created=drive.files().create(body=body,media_body=media,fields='id,size,md5Checksum,webViewLink').execute(num_retries=1)
        observed=drive.files().get(fileId=created['id'],fields='id,size,md5Checksum,webViewLink').execute(num_retries=1)
        if int(observed.get('size',-1))!=len(raw) or observed.get('md5Checksum')!=hashlib.md5(raw).hexdigest():
            raise ValueError('Drive backup verification failed.')
        return dict(verified=True,file_id=observed['id'],file_url=observed.get('webViewLink',''),sha256=sha)
