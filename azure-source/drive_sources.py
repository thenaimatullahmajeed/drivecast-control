"""One project Drive root, complete folder discovery and verified selection."""
from collections import deque
import json
import time

FOLDER_MIME = 'application/vnd.google-apps.folder'
MAX_FOLDERS = 10000
MAX_PAGES = 1000
RESERVED = {'processing', 'done', 'failed'}


def descendants(drive, root_id):
    """Enumerate nested folders in batches; never silently return a partial tree."""
    frontier = deque([(root_id, '')])
    paths, rows, pages = {root_id: ''}, {}, 0
    while frontier:
        batch = [frontier.popleft() for _ in range(min(40, len(frontier)))]
        parent_paths = dict(batch)
        query = '(' + ' or '.join("'" + item + "' in parents" for item, _ in batch) + ") and mimeType='" + FOLDER_MIME + "' and trashed=false"
        token, tokens = None, set()
        while True:
            pages += 1
            if pages > MAX_PAGES:
                raise ValueError('Folder scan limit reached. Previous folder list retained; narrow the main folder.')
            response = drive.files().list(q=query, fields='nextPageToken,incompleteSearch,files(id,name,parents,webViewLink)',
                pageSize=1000, pageToken=token, supportsAllDrives=True, includeItemsFromAllDrives=True).execute(num_retries=2)
            if response.get('incompleteSearch'):
                raise ValueError('Drive returned an incomplete folder scan. Previous list retained; check access and refresh.')
            for folder in response.get('files', []):
                fid, name = str(folder.get('id', '')), folder.get('name', '')
                if not fid or fid in paths or name.casefold() in RESERVED:
                    continue
                parent = next((p for p in folder.get('parents', []) if p in parent_paths), None)
                if not parent:
                    raise ValueError('Drive folder ancestry could not be verified; scan stopped.')
                path = '/'.join(x for x in (parent_paths[parent], name) if x)
                paths[fid] = path
                rows[fid] = dict(id=fid, name=name, path=path, parent_id=parent,
                                 webViewLink='https://drive.google.com/drive/folders/' + fid)
                if len(rows) > MAX_FOLDERS:
                    raise ValueError('Too many subfolders; previous folder list retained.')
                frontier.append((fid, path))
            token = response.get('nextPageToken')
            if not token:
                break
            if token in tokens:
                raise ValueError('Drive repeated a pagination token. Previous folder list retained.')
            tokens.add(token)
    return sorted(rows.values(), key=lambda row: (row['path'].casefold(), row['id']))


def selected(app, folder_id, connection_id=''):
    root = app.STATE.get('drive_root', {})
    if not root.get('folder_id') or not root.get('connection_id'):
        raise ValueError('Open Google Drive and connect the project main folder first.')
    if connection_id and connection_id != root['connection_id']:
        raise ValueError('Use the Google Drive account connected to the project main folder.')
    if folder_id == root['folder_id']:
        raise ValueError('Choose a channel subfolder, not the project main folder.')
    row = next((row for row in root.get('folders', []) if row['id'] == folder_id), None)
    if not row:
        raise ValueError('Choose a discovered subfolder. Refresh Google Drive if it was added recently.')
    return root, row


def verify_ancestry(drive, folder_id, root_id):
    """Recheck live parents: a cached folder may have moved outside the root."""
    todo, seen = [folder_id], set()
    for _ in range(100):
        if not todo:
            break
        current = todo.pop()
        if current in seen:
            continue
        if current == root_id:
            return
        seen.add(current)
        row = drive.files().get(fileId=current, fields='id,mimeType,parents,trashed', supportsAllDrives=True).execute(num_retries=2)
        if row.get('trashed') or row.get('mimeType') != FOLDER_MIME:
            continue
        todo.extend(p for p in row.get('parents', []) if p not in seen)
    raise ValueError('This folder is no longer inside the project main folder. Refresh Drive and select its subfolder again.')


def discover(app, body=None):
    body = body or {}
    previous = app.STATE.get('drive_root', {})
    cid = body.get('connection_id') or previous.get('connection_id')
    app.connection(cid, 'drive')
    fid = app.helpers.parse_folder(str(body.get('folder_id') or previous.get('folder_id', '')))
    if previous.get('folder_id') and (fid, cid) != (previous['folder_id'], previous['connection_id']) and body.get('confirmed') is not True:
        raise ValueError('Confirm changing the project main folder. Existing channel folders will not be reassigned automatically.')
    with app.build('drive', 'v3', credentials=app.google_credentials(cid), cache_discovery=False) as drive:
        root = drive.files().get(fileId=fid, fields='id,name,mimeType,capabilities(canAddChildren)', supportsAllDrives=True).execute(num_retries=2)
        if root.get('mimeType') != FOLDER_MIME or not root.get('capabilities', {}).get('canAddChildren'):
            raise ValueError('Choose a main folder where this Google account has Editor access.')
        rows = descendants(drive, fid)
    known = {row['id']: row for row in previous.get('folders', [])}
    for row in rows:
        row['detected_at'] = known.get(row['id'], {}).get('detected_at') or app.now()
        client = next((c for c in app.STATE['clients'].values() if c.get('folder_id') == row['id']), None)
        row.update(client_id=client['id'] if client else '', client_name=client.get('channel_title', client['name']) if client else '', new=not bool(client))
    app.STATE['drive_root'] = dict(connection_id=cid, folder_id=fid, name=root['name'], folders=rows, checked_at=app.now())
    app.save_local()
    return dict(updated=len(rows), name=root['name'])


def refresh_if_due(app, force=False, interval=300):
    """Refresh discovery only; never assign a route or start an upload.

    A durable attempt timestamp avoids retry storms across web/scheduler processes.
    Provider errors preserve the last complete inventory and remain visible.
    """
    root = app.STATE.get('drive_root', {})
    if not root.get('folder_id') or not root.get('connection_id'):
        return dict(refreshed=False)
    from datetime import datetime
    from cloud_store import CloudStore
    try: checked = datetime.fromisoformat(root.get('checked_at', '')).timestamp()
    except (ValueError, TypeError): checked = 0
    clock = time.time()
    db = CloudStore(app.WORK/'records.sqlite3')
    with db.db(True) as sql:
        row = sql.execute("SELECT value FROM cloud_meta WHERE key='drive:discovery-attempt'").fetchone()
        attempted = json.loads(row['value']) if row else 0
        if not force and clock-max(checked, attempted) < interval:
            return dict(refreshed=False)
        if force and clock-attempted < 15:
            return dict(refreshed=False)
        sql.execute("INSERT OR REPLACE INTO cloud_meta VALUES('drive:discovery-attempt',?)", (json.dumps(clock),))
    try:
        result = discover(app)
        return dict(result, refreshed=True)
    except Exception as error:
        from records import safe_error
        app.STATE['drive_root']['notice'] = 'Folder refresh failed; previous list kept. '+safe_error(str(error))
        app.save_local()
        return dict(refreshed=False, notice=app.STATE['drive_root']['notice'])
