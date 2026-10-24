"""Deterministic proposals only: a name never authorizes publishing or a migration."""
from collections import defaultdict


def normalized(value):
    return str(value or '').strip().casefold()


def match(state):
    folders, channels = defaultdict(list), defaultdict(list)
    for folder in state.get('drive_root', {}).get('folders', []):
        folders[normalized(folder.get('name'))].append(folder)
    for connection in state.get('connections', {}).values():
        if connection.get('kind') == 'youtube':
            channels[normalized(connection.get('label'))].append(connection)
    clients = list(state.get('clients', {}).values())
    rows = []
    for name in sorted(folders.keys() | channels.keys()):
        fs, cs = folders[name], channels[name]
        ambiguous = not name or len(fs) > 1 or len(cs) > 1
        status = ('Ambiguous name' if ambiguous else 'Matched' if fs and cs else
                  'Folder found, waiting for channel authorization' if fs else 'Channel found, no folder')
        folder, channel = (fs[0] if len(fs) == 1 else {}), (cs[0] if len(cs) == 1 else {})
        existing = next((c for c in clients if channel.get('channel_id') and c.get('channel_id') == channel['channel_id']), {})
        rows.append(dict(name=(folder.get('name') or channel.get('label') or '(empty name)'), status=status,
            folders=[dict(id=f['id'], name=f['name'], path=f.get('path', f['name'])) for f in fs],
            channels=[dict(id=c['id'], title=c.get('label'), channel_id=c.get('channel_id'),
                verified=c.get('status') == 'Connected' and bool(c.get('channel_id'))) for c in cs],
            folder_id=folder.get('id', ''), youtube_connection=channel.get('id', ''),
            channel_id=channel.get('channel_id', ''), existing_client_id=existing.get('id', ''),
            live_migration_required=bool(existing and existing.get('runner', 'github') != 'azure'),
            authorization_action='connect-youtube' if fs and not cs else '',
            ready=status == 'Matched' and channel.get('status') == 'Connected' and bool(channel.get('channel_id'))))
    return rows
