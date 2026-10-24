"""Encrypted whole-server snapshots with online SQLite copies and private Blob storage."""
from contextlib import closing
import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tarfile
import tempfile
import time
from datetime import datetime, timezone

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

AAD=b'drivecast-server-backup-v1'


def key():
    value=base64.b64decode(os.environ['DRIVECAST_BACKUP_KEY'],validate=True)
    if len(value)!=32: raise ValueError('A 32-byte backup key is required.')
    return value


def snapshot(root):
    root=Path(root).resolve()
    with tempfile.TemporaryDirectory() as temp:
        staging=Path(temp)
        for source in root.rglob('*'):
            relative=source.relative_to(root)
            if source.is_symlink() or any(p in ('backups','logs','tmp','recovery-backups','record-backups') for p in relative.parts): continue
            if not source.is_file() or source.name.endswith(('-wal','-shm')) or source.name in ('worker-heartbeat','scheduler-heartbeat','backup-heartbeat','draining'): continue
            target=staging/relative;target.parent.mkdir(parents=True,exist_ok=True)
            if source.suffix=='.sqlite3':
                with closing(sqlite3.connect(source)) as incoming,closing(sqlite3.connect(target)) as outgoing:
                    incoming.backup(outgoing)
                    outgoing.commit()
            else: shutil.copy2(source,target)
        # Hosted SQLite is authoritative; materialize matching JSON/vault copies.
        for path in staging.rglob('records.sqlite3'):
            from cloud_store import CloudStore
            checkpoint=CloudStore(path)
            state=checkpoint.meta('workspace:state',None)
            if state is not None:(path.parent/'workspace.json').write_text(json.dumps(state))
            envelope=checkpoint.meta('vault:envelope',None)
            if envelope is not None:(path.parent/'connections.enc.json').write_text(json.dumps(envelope))
            service=path.parent/'service-unlock.json'
            if envelope is not None and service.exists():
                secret=base64.b64decode(json.loads(service.read_text())['key'],validate=True)
                AESGCM(secret).decrypt(base64.b64decode(envelope['nonce']),base64.b64decode(envelope['ciphertext']),b'drivecast-vault-v1')
        import recovery
        directory=staging/'recovery';directory.mkdir(exist_ok=True)
        (directory/'source.zip').write_bytes(recovery.source_bundle())
        (directory/'destination.json').write_text(json.dumps(recovery.config()))
        (staging/'backup-manifest.json').write_text(json.dumps(dict(format=AAD.decode(),created=datetime.now(timezone.utc).isoformat(),restore_paused=True,version=os.getenv('DRIVECAST_VERSION','azure-v1'))))
        buffer=io.BytesIO()
        with tarfile.open(fileobj=buffer,mode='w:gz') as archive:
            for item in staging.rglob('*'):
                if item.is_file(): archive.add(item,arcname=item.relative_to(staging).as_posix())
        nonce=os.urandom(12)
        return AAD+b'\n'+nonce+AESGCM(key()).encrypt(nonce,buffer.getvalue(),AAD)


def restore(raw,destination):
    prefix=AAD+b'\n'
    if not raw.startswith(prefix): raise ValueError('Unsupported server backup.')
    offset=len(prefix);plain=AESGCM(key()).decrypt(raw[offset:offset+12],raw[offset+12:],AAD)
    destination=Path(destination).resolve()
    if destination.exists() and any(destination.iterdir()): raise ValueError('Restore requires an empty destination; existing data was not overwritten.')
    destination.mkdir(parents=True,exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(plain),mode='r:gz') as archive:
        for member in archive.getmembers():
            target=(destination/member.name).resolve()
            if not target.is_relative_to(destination) or not member.isfile() or member.size>512*1024*1024:
                raise ValueError('Unsafe or unsupported backup entry.')
        archive.extractall(destination,filter='data')
    for name in ('worker-heartbeat','scheduler-heartbeat','backup-heartbeat','draining'):
        (destination/name).unlink(missing_ok=True)
    from cloud_store import CloudStore
    for path in destination.rglob('records.sqlite3'):
        store=CloudStore(path)
        with store.db(True) as db:
            db.execute('UPDATE cloud_routes SET enabled=0')
            db.execute("UPDATE cloud_jobs SET state='quarantined',notice='Restored paused; verify previous server and history.' WHERE state IN ('queued','running')")
            db.execute("UPDATE cloud_reservations SET status='uncertain' WHERE status='reserved'")
    for path in destination.rglob('workspace.json'):
        state=json.loads(path.read_text())
        for client in state.get('clients',{}).values(): client['enabled']=False
        state['remote']={};state['notice']='Restored paused. Confirm final checkpoint and old server fence before resuming.'
        path.write_text(json.dumps(state,ensure_ascii=False))
        from workspace_db import save
        # Replace the database's workspace checkpoint with the paused restore state.
        store=CloudStore(path.parent/'records.sqlite3')
        store.set_meta('workspace:state',state)
    for path in destination.rglob('*'):
        if path.is_file(): path.chmod(0o600)


def upload(raw,name):
    from azure.identity import ManagedIdentityCredential
    from azure.storage.blob import BlobServiceClient
    account=os.environ['DRIVECAST_BACKUP_STORAGE']
    service=BlobServiceClient('https://'+account+'.blob.core.windows.net',credential=ManagedIdentityCredential())
    container=service.get_container_client(os.getenv('DRIVECAST_BACKUP_CONTAINER','drivecast-backups'))
    blob=container.get_blob_client(name)
    blob.upload_blob(raw,overwrite=False,metadata={'sha256':hashlib.sha256(raw).hexdigest()})
    if blob.get_blob_properties().size!=len(raw): raise ValueError('Remote backup verification failed.')
    # Retention is deliberately delegated to an explicit Azure lifecycle policy.
    # The application never deletes cloud backups implicitly.


def run(root):
    started=time.time();root=Path(root);stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+os.urandom(3).hex()
    raw=snapshot(root);name='drivecast-'+stamp+'.encrypted'
    target=root/'backups'/name;target.parent.mkdir(parents=True,exist_ok=True)
    target.write_bytes(raw);target.chmod(0o600)
    import recovery
    settings=recovery.config();targets={}
    if os.getenv('DRIVECAST_BACKUP_STORAGE'):
        try:upload(raw,name);targets['azure']={'verified':True}
        except Exception:targets['azure']={'verified':False,'notice':'Azure Blob backup failed; other targets were still attempted.'}
    if settings.get('drive'):
        try:
            targets['drive']=dict(recovery.upload_drive(root,settings['drive'],raw,name))
            source=recovery.source_bundle()
            targets['drive']['source']=recovery.upload_drive(root,settings['drive'],source,'DriveCast-Source-'+hashlib.sha256(source).hexdigest()[:12]+'.zip',deduplicate=True)
        except Exception:
            if not targets.get('drive',{}).get('verified'):targets['drive']={'verified':False,'notice':'Private Drive backup failed. Check recovery authorization, folder privacy and space.'}
            else:targets['drive']['notice']='Encrypted snapshot verified, including source; standalone source copy could not be refreshed.'
    remote=any(t.get('verified') for t in targets.values())
    independent=bool(targets.get('drive',{}).get('verified'))
    result=dict(at=datetime.now(timezone.utc).isoformat(),file=name,sha256=hashlib.sha256(raw).hexdigest(),remote_verified=remote,
        off_azure_verified=independent,targets=targets,
        notice='Encrypted backup verified outside Azure in private Google Drive.' if independent else 'Encrypted Azure Blob backup verified; independent Drive backup is not verified.' if targets.get('azure',{}).get('verified') else 'Encrypted local backup only; remote backup is not verified.')
    from cloud_store import CloudStore
    for path in (root/'accounts'/'users').glob('*/records.sqlite3'):
        store=CloudStore(path);store.set_meta('backup:last',result)
        if independent or not settings.get('drive') and remote:store.set_meta('recovery:covered_through',started)
    return result


def loop(root):
    root=Path(root);last=0;attempted=0
    while True:
        requested=[];dirty=False
        from cloud_store import CloudStore
        for path in (root/'accounts'/'users').glob('*/records.sqlite3'):
            store=CloudStore(path)
            dirty=dirty or store.meta('recovery:dirty',0)>store.meta('recovery:covered_through',0)
            with store.db() as db:
                requested.extend((store,r['id']) for r in db.execute("SELECT id FROM cloud_commands WHERE kind='backup' AND handled IS NULL"))
        if (requested or dirty or time.time()-last>int(os.getenv('DRIVECAST_BACKUP_INTERVAL_SECONDS','3600'))) and time.time()-attempted>60:
            attempted=time.time()
            try:
                result=run(root)
                if result.get('off_azure_verified') or not __import__('recovery').config().get('drive') and result['remote_verified']:
                    last=time.time()
                    for store,cid in requested:
                        with store.db(True) as db: db.execute('UPDATE cloud_commands SET handled=? WHERE id=?',(time.time(),cid))
            except Exception:
                for path in (root/'accounts'/'users').glob('*/records.sqlite3'):
                    store=CloudStore(path)
                    previous=store.meta('backup:last',{})
                    previous['notice']='Backup attempt failed. Last successful timestamp is retained; check storage access and free disk.'
                    store.set_meta('backup:last',previous)
        (root/'backup-heartbeat').write_text(str(time.time()))
        time.sleep(60)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=('run','loop','restore'));parser.add_argument('--root',default=os.getenv('DRIVECAST_DATA_DIR','/data'));parser.add_argument('--file');args=parser.parse_args()
    if args.action=='restore': restore(Path(args.file).read_bytes(),args.root)
    elif args.action=='loop': loop(args.root)
    else: run(args.root)
