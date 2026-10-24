"""Transactional field-level workspace updates across web, scheduler and worker processes."""
import copy
import json
from cloud_store import CloudStore

MISSING=object()


def merge(current, before, after):
    result=copy.deepcopy(current)
    for key in before.keys() | after.keys():
        old,new=before.get(key,MISSING),after.get(key,MISSING)
        if old==new: continue
        if new is MISSING: result.pop(key,None)
        elif isinstance(old,dict) and isinstance(new,dict) and isinstance(result.get(key),dict):
            result[key]=merge(result[key],old,new)
        else: result[key]=copy.deepcopy(new)
    return result


def load(path, fallback):
    return CloudStore(path).meta('workspace:state',None) or copy.deepcopy(fallback)


def save(path, before, after):
    store=CloudStore(path)
    with store.db(True) as db:
        row=db.execute("SELECT value FROM cloud_meta WHERE key='workspace:state'").fetchone()
        current=json.loads(row['value']) if row else {}
        if (current.get('workspace_id') and before.get('workspace_id') and
                current['workspace_id']!=before['workspace_id'] and
                after.get('workspace_id')==before['workspace_id']):
            raise ValueError('Workspace was reset. Refresh before making another change.')
        # A deliberate restore of a different workspace is a replacement, never a merge.
        if current.get('workspace_id')!=after.get('workspace_id'): current=copy.deepcopy(after)
        else: current=merge(current,before,after)
        db.execute("INSERT OR REPLACE INTO cloud_meta VALUES('workspace:state',?)",(json.dumps(current,ensure_ascii=False),))
        # Folder discovery/stat refreshes are covered by the hourly snapshot.
        significant=('connections','clients','cloud_settings','telegram','google_app','workspace_id')
        if any(before.get(k)!=after.get(k) for k in significant):
            from recovery import request
            request(db)
    return current
