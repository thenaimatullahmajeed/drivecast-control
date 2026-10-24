"""Retire removed providers without deleting historical upload records."""
import copy

CONNECTION_KINDS = ('github', 'youtube', 'drive')
REMOVED_FIELDS = ('instagram_username', 'instagram_fetch_mode', 'meta_connection',
                  'public_source_issue', 'source_avatar', 'facebook_connection',
                  'facebook_page_id', 'facebook_title', 'facebook_format',
                  'facebook_initialized', 'facebook_seed', 'source_since')


def drive_only(state, vault=None):
    changed = False
    removed_connections = {key for key,row in state.get('connections',{}).items() if row.get('kind') not in CONNECTION_KINDS}
    for client in state.get('clients', {}).values():
        if client.get('source_type', 'drive') != 'drive':
            state.setdefault('retired_sources', {}).setdefault(client['id'],
                {k:copy.deepcopy(client[k]) for k in ('source_type','source_stats',*REMOVED_FIELDS) if k in client})
            client.update(source_type='drive', folder_id='', drive_connection='', folder_name='',
                          enabled=False, verified=False, source_verified=False, source_stats={},
                          needs_drive_folder=True, status='Choose Drive folder',
                          notice='Select and confirm a subfolder from the project main folder. YouTube access and upload history are preserved.')
            changed = True
        for key in REMOVED_FIELDS:
            if key in client:
                client.pop(key)
                changed = True
    for key, row in list(state.get('connections', {}).items()):
        if row.get('kind') not in CONNECTION_KINDS:
            state['connections'].pop(key)
            changed = True
    for key in ('instagram_sources','meta_apps','active_meta_app_id','meta_diagnostics'):
        if key in state:
            state.pop(key)
            changed = True
    draft = state.get('draft')
    if draft:
        if draft.get('source_type','drive') != 'drive':
            state.pop('draft')
            changed = True
        else:
            for key in REMOVED_FIELDS:
                if key in draft:
                    draft.pop(key)
                    changed = True
    if vault is not None and vault.unlocked:
        old = [key for key in vault.values if key in removed_connections or key.startswith(('meta:', 'meta-', 'facebook:'))]
        if old:
            for key in old:
                vault.values.pop(key)
            vault.save()
            changed = True
    state['source_schema'] = 'drive-only-v1'
    return changed
