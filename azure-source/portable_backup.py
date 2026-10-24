"""One authenticated encrypted file: workspace, secrets, records and stats."""
import base64
import copy
import json
import os
import secrets
import shutil
import time
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from records import RecordStore, now

FORMAT = "drivecast-full-backup-v1"
PENDING = {}
MAX_BYTES = 32 * 1024 * 1024


def export(app):
    if not app.VAULT.unlocked:
        raise ValueError("Unlock connections once before Export. Your existing unlock password protects this file.")
    app.VAULT.refresh()
    payload = dict(format=FORMAT, exported_at=now(), workspace=app.STATE,
                   connections=app.VAULT.values, records=app.store().data())
    from cloud_store import CloudStore
    payload['cloud']=CloudStore(app.WORK/'records.sqlite3').export()
    nonce = os.urandom(12)
    encrypted = AESGCM(app.VAULT.key).encrypt(nonce,json.dumps(payload,ensure_ascii=False).encode(),FORMAT.encode())
    return json.dumps(dict(format=FORMAT,salt=base64.b64encode(app.VAULT.salt).decode(),
        nonce=base64.b64encode(nonce).decode(),ciphertext=base64.b64encode(encrypted).decode())).encode()


def preview(app, raw, password):
    if len(raw) > MAX_BYTES or not isinstance(password,str) or len(password)<12:
        raise ValueError("Choose a DriveCast encrypted backup and enter its original unlock password.")
    try:
        envelope=json.loads(raw)
        if envelope.get("format") != FORMAT:
            raise ValueError()
        salt=base64.b64decode(envelope['salt'],validate=True)
        if len(salt)!=16:
            raise ValueError()
        key=Scrypt(salt=salt,length=32,n=2**15,r=8,p=1).derive(password.encode())
        payload=json.loads(AESGCM(key).decrypt(base64.b64decode(envelope['nonce'],validate=True),
            base64.b64decode(envelope['ciphertext'],validate=True),FORMAT.encode()))
        state=payload['workspace']
        if payload['format']!=FORMAT or state.get('version')!=2 or not state.get('workspace_id'):
            raise ValueError()
        if not isinstance(state['clients'],dict) or not isinstance(state['connections'],dict) or not isinstance(payload['connections'],dict):
            raise ValueError()
        for cid,c in state['connections'].items():
            if c.get('id')!=cid or c.get('kind') not in ('github','youtube','drive','meta','facebook'):
                raise ValueError()
        check=app.WORK/'import-validation.sqlite3'
        check.unlink(missing_ok=True)
        try:
            RecordStore(check).restore_data(payload['records'])
            from cloud_store import CloudStore
            CloudStore(check).restore(payload.get('cloud',{}))
        finally:
            check.unlink(missing_ok=True)
    except Exception:
        raise ValueError("Wrong unlock password or damaged/unsupported backup. Existing data has not changed.") from None
    PENDING.clear()
    ticket=secrets.token_urlsafe(32)
    PENDING[ticket]=(time.monotonic()+600,copy.deepcopy(payload),key,salt)
    return dict(ticket=ticket,clients=len(state['clients']),connections=len(state['connections']),
        records=len(payload['records'].get('uploads',[])),exported_at=payload.get('exported_at',''))


def restore(app, ticket, confirmed):
    value=PENDING.pop(ticket,None)
    if confirmed is not True or not value or value[0]<time.monotonic():
        raise ValueError("Preview the backup again, then confirm Restore.")
    _,payload,key,salt=value
    backup=app.WORK/'recovery-backups'/('before-import-'+now().replace(':','-'))
    backup.mkdir(parents=True,exist_ok=False)
    paths=(app.STATE_PATH,app.VAULT.path,app.WORK/'records.sqlite3',app.VAULT.path.with_name('unlock.windows'))
    for path in paths:
        if path.exists():
            shutil.copy2(path,backup/path.name)
    old_state=copy.deepcopy(app.STATE)
    old_vault=(app.VAULT.key,app.VAULT.salt,app.VAULT.values)
    try:
        staged=app.WORK/'import-records.sqlite3'
        staged.unlink(missing_ok=True)
        RecordStore(staged).restore_data(payload['records'])
        from cloud_store import CloudStore
        CloudStore(staged).restore(payload.get('cloud',{}))
        with app.LOCK:
            app.VAULT.key,app.VAULT.salt,app.VAULT.values=key,salt,payload['connections']
            app.VAULT.save()
            staged.replace(app.WORK/'records.sqlite3')
            app.STATE.clear();app.STATE.update(payload['workspace'])
            import workspace_schema
            workspace_schema.drive_only(app.STATE,app.VAULT)
            for client in app.STATE['clients'].values(): client['enabled']=False
            app.STATE['remote']={}
            CloudStore(app.WORK/'records.sqlite3').set_meta('workspace:state',app.STATE)
            key_path=app.VAULT.path.with_name('service-unlock.json')
            if os.getenv('DRIVECAST_HOSTED')=='1':
                key_path.write_text(json.dumps({'key':base64.b64encode(key).decode()}));key_path.chmod(0o600)
            app.STATE['notice']='Import complete. Press Sync everything to test current permissions. Existing GitHub workers were not changed.'
            app.save_local()
            paths[-1].unlink(missing_ok=True)
    except Exception:
        for path in paths:
            saved=backup/path.name
            if saved.exists():shutil.copy2(saved,path)
            else:path.unlink(missing_ok=True)
        app.STATE.clear();app.STATE.update(old_state)
        app.VAULT.key,app.VAULT.salt,app.VAULT.values=old_vault
        raise ValueError("Import could not finish. Original local workspace restored.") from None
    return dict(clients=len(app.STATE['clients']),notice='Restored. Sync to verify connections.')
