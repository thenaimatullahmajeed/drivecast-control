"""SQLite queue and write-ahead reservations. An expired upload is quarantined, never retried.

One database per workspace on a local persistent volume. SQLite WAL is not supported
over a network filesystem. A reservation's channel/source identity is immutable.
"""
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from recovery import request as request_recovery

SCHEMA = 1


class CloudStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS cloud_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS cloud_routes(id TEXT PRIMARY KEY,channel_id TEXT UNIQUE NOT NULL,
                folder_id TEXT UNIQUE NOT NULL,config TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 0,
                verified INTEGER NOT NULL DEFAULT 0,history_verified INTEGER NOT NULL DEFAULT 0,
                generation INTEGER NOT NULL DEFAULT 1,updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS cloud_jobs(id INTEGER PRIMARY KEY,route_id TEXT NOT NULL,
                mode TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'queued',worker TEXT,lease TEXT,
                lease_until REAL,created REAL NOT NULL,finished REAL,notice TEXT NOT NULL DEFAULT '');
            CREATE UNIQUE INDEX IF NOT EXISTS cloud_one_active_route ON cloud_jobs(route_id)
                WHERE state IN ('queued','running');
            CREATE TABLE IF NOT EXISTS cloud_reservations(channel_id TEXT NOT NULL,source_key TEXT NOT NULL,
                route_id TEXT NOT NULL,job_id INTEGER NOT NULL,lease TEXT NOT NULL,day TEXT NOT NULL,
                project_id TEXT NOT NULL,status TEXT NOT NULL,record TEXT NOT NULL,updated REAL NOT NULL,
                PRIMARY KEY(channel_id,source_key));
            CREATE TABLE IF NOT EXISTS cloud_workers(id TEXT PRIMARY KEY,last_seen REAL NOT NULL,
                capacity INTEGER NOT NULL,status TEXT NOT NULL,version TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS cloud_events(id INTEGER PRIMARY KEY,kind TEXT NOT NULL,
                detail TEXT NOT NULL,created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS cloud_commands(id INTEGER PRIMARY KEY,kind TEXT NOT NULL,
                created REAL NOT NULL,handled REAL);
            ''')
            db.execute('INSERT OR IGNORE INTO cloud_meta VALUES(?,?)', ('schema', str(SCHEMA)))

    @contextmanager
    def db(self, write=False):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA synchronous=FULL')
        db.execute('PRAGMA busy_timeout=30000')
        try:
            if write: db.execute('BEGIN IMMEDIATE')
            yield db
            if write: db.commit()
        except BaseException:
            if write: db.rollback()
            raise
        finally:
            db.close()

    def meta(self, key, default=None):
        with self.db() as db:
            row = db.execute('SELECT value FROM cloud_meta WHERE key=?', (key,)).fetchone()
        return json.loads(row['value']) if row else default

    def set_meta(self, key, value):
        with self.db(True) as db:
            db.execute('INSERT OR REPLACE INTO cloud_meta VALUES(?,?)', (key, json.dumps(value)))

    def routes(self):
        with self.db() as db: rows = list(db.execute('SELECT * FROM cloud_routes ORDER BY id'))
        return [dict(r, config=json.loads(r['config'])) for r in rows]

    def save_route(self, config, history_verified=False):
        with self.db(True) as db:
            old = db.execute('SELECT * FROM cloud_routes WHERE id=?', (config['id'],)).fetchone()
            if old:
                if old['channel_id'] != config['channel_id']:
                    raise ValueError('The verified YouTube destination ID is immutable.')
                if old['folder_id'] != config['folder_id'] and (old['history_verified'] or db.execute('SELECT 1 FROM cloud_reservations WHERE route_id=?',(config['id'],)).fetchone()):
                    raise ValueError('This route has publishing history. Source reassignment needs an explicit handover that preserves every reservation.')
                if old['enabled'] or db.execute("SELECT 1 FROM cloud_jobs WHERE route_id=? AND state IN ('queued','running')", (config['id'],)).fetchone():
                    raise ValueError('Pause this route and wait for its worker before editing.')
            db.execute('''INSERT INTO cloud_routes VALUES(?,?,?,?,0,1,?,1,?)
                ON CONFLICT(id) DO UPDATE SET folder_id=excluded.folder_id,config=excluded.config,verified=1,updated=excluded.updated''',
                (config['id'], config['channel_id'], config['folder_id'], json.dumps(config), int(history_verified), time.time()))
            db.execute('INSERT OR REPLACE INTO cloud_meta VALUES(?,?)',('dry:'+config['id'],json.dumps({'notice':'Waiting for a fresh read-only check.'})))
            request_recovery(db)

    def set_enabled(self, route_id, enabled):
        with self.db(True) as db:
            row = db.execute('SELECT * FROM cloud_routes WHERE id=?', (route_id,)).fetchone()
            if not row: raise ValueError('Cloud route not found.')
            if enabled and not (row['verified'] and row['history_verified']):
                raise ValueError('Exact channel verification and complete history/fencing confirmation are required.')
            if enabled and db.execute("SELECT 1 FROM cloud_reservations WHERE route_id=? AND status IN ('reserved','uncertain')",(route_id,)).fetchone():
                raise ValueError('Review quarantined uploads first. This channel cannot resume while an outcome is uncertain.')
            db.execute('UPDATE cloud_routes SET enabled=?,updated=? WHERE id=?', (int(enabled), time.time(), route_id))
            request_recovery(db)
            if not enabled:
                db.execute("UPDATE cloud_jobs SET state='cancelled',finished=? WHERE route_id=? AND state='queued' AND mode!='dry'", (time.time(), route_id))

    def enqueue(self, route_id, mode='dry'):
        if mode not in ('dry', 'public', 'private','now'): raise ValueError('Invalid worker mode.')
        with self.db(True) as db:
            route = db.execute('SELECT * FROM cloud_routes WHERE id=?', (route_id,)).fetchone()
            if not route: raise ValueError('Cloud route not found.')
            if mode != 'dry' and not ((route['enabled'] or mode in ('private','now')) and route['verified'] and route['history_verified']):
                raise ValueError('This route has not been confirmed for cloud publishing.')
            if mode!='dry' and db.execute("SELECT 1 FROM cloud_reservations WHERE route_id=? AND status IN ('reserved','uncertain')",(route_id,)).fetchone():
                raise ValueError('This route is quarantined until its previous upload is reviewed.')
            if mode=='now':
                import os
                config=json.loads(route['config']);day=datetime.now(ZoneInfo('America/Los_Angeles')).date().isoformat()
                if self.used(db,route['channel_id'],day)>=int(config.get('max_uploads_per_day',2)):
                    raise ValueError('Daily channel limit reached. Post force now does not bypass it.')
                used=db.execute('SELECT count(*) FROM cloud_reservations WHERE project_id=? AND day=?',(config.get('project_id') or 'unknown-project',day)).fetchone()[0]
                if used>=int(os.getenv('DRIVECAST_PROJECT_DAILY_LIMIT','90')):
                    raise ValueError('Daily project limit reached. Wait for the next day.')
            try:
                return db.execute('INSERT INTO cloud_jobs(route_id,mode,created) VALUES(?,?,?)', (route_id, mode, time.time())).lastrowid
            except sqlite3.IntegrityError: return None

    def next_due(self,route_id,interval=300):
        with self.db() as db:
            row=db.execute("SELECT max(created) FROM cloud_jobs WHERE route_id=? AND mode IN ('public','private','now')",(route_id,)).fetchone()
        return max(float(self.meta('next-check:'+route_id,0)),(row[0]+interval) if row and row[0] else 0)

    def recover(self, clock=None):
        clock = time.time() if clock is None else clock
        with self.db(True) as db:
            expired = list(db.execute("SELECT * FROM cloud_jobs WHERE state='running' AND lease_until<?", (clock,)))
            for job in expired:
                reserved = db.execute('SELECT 1 FROM cloud_reservations WHERE job_id=?', (job['id'],)).fetchone()
                state = 'quarantined' if reserved else 'queued'
                db.execute('UPDATE cloud_jobs SET state=?,worker=NULL,lease=NULL,lease_until=NULL,notice=? WHERE id=?',
                    (state, 'Worker lost. Reserved uploads require manual review.' if reserved else 'Worker lost before reservation; safe to reassign.', job['id']))
                db.execute("UPDATE cloud_reservations SET status=CASE WHEN status='reserved' THEN 'uncertain' ELSE status END WHERE job_id=?", (job['id'],))
                if reserved: db.execute('UPDATE cloud_routes SET enabled=0 WHERE id=?',(job['route_id'],))
        return len(expired)

    def heartbeat(self, worker, capacity=1, version='azure-v1', clock=None):
        with self.db(True) as db:
            db.execute('INSERT OR REPLACE INTO cloud_workers VALUES(?,?,?,?,?)',
                (worker, time.time() if clock is None else clock, capacity, 'healthy', version))

    def claim(self, worker, capacity=1, ttl=120, clock=None):
        clock = time.time() if clock is None else clock
        with self.db(True) as db:
            active = db.execute("SELECT count(*) FROM cloud_jobs WHERE worker=? AND state='running'", (worker,)).fetchone()[0]
            if active >= capacity: return None
            row = db.execute("SELECT * FROM cloud_jobs WHERE state='queued' ORDER BY created,id LIMIT 1").fetchone()
            if not row: return None
            lease = secrets.token_hex(24)
            db.execute("UPDATE cloud_jobs SET state='running',worker=?,lease=?,lease_until=? WHERE id=? AND state='queued'",
                (worker, lease, clock + ttl, row['id']))
            return dict(row, worker=worker, lease=lease, lease_until=clock + ttl, state='running')

    def renew(self, job, ttl=120):
        with self.db(True) as db:
            changed = db.execute("UPDATE cloud_jobs SET lease_until=? WHERE id=? AND lease=? AND state='running' AND lease_until>?",
                (time.time() + ttl, job['id'], job['lease'], time.time())).rowcount
        return bool(changed)

    def reserve(self, job, config, source_key, record, day, maximum, project_limit=90):
        """Spend daily attempt limits atomically before any upload or Drive move."""
        with self.db(True) as db:
            live = db.execute("SELECT * FROM cloud_jobs WHERE id=? AND lease=? AND state='running' AND lease_until>?", (job['id'], job['lease'], time.time())).fetchone()
            route = db.execute('SELECT * FROM cloud_routes WHERE id=?', (job['route_id'],)).fetchone()
            if not live or not route or (not route['enabled'] and live['mode'] not in ('private','now')) or live['mode'] == 'dry': return False
            if not (route['history_verified'] and route['verified']): return False
            if (route['channel_id'], route['folder_id']) != (config['channel_id'], config['folder_id']): return False
            if db.execute('SELECT 1 FROM cloud_reservations WHERE channel_id=? AND source_key=?', (config['channel_id'], source_key)).fetchone(): return False
            if self.used(db,config['channel_id'],day)>=maximum: return False
            project = config.get('project_id') or 'unknown-project'
            if db.execute('SELECT count(*) FROM cloud_reservations WHERE project_id=? AND day=?', (project, day)).fetchone()[0] >= project_limit: return False
            import os
            month=day[:7]
            used=db.execute("SELECT COALESCE(sum(CAST(json_extract(record,'$.size') AS INTEGER)),0) FROM cloud_reservations WHERE day LIKE ?",(month+'%',)).fetchone()[0]
            if used+int(record.get('size',0))>int(os.getenv('DRIVECAST_MONTHLY_TRANSFER_GB','100'))*1024**3: return False
            db.execute('INSERT INTO cloud_reservations VALUES(?,?,?,?,?,?,?,?,?,?)',
                (config['channel_id'], source_key, config['id'], job['id'], job['lease'], day, project, 'reserved', json.dumps(record), time.time()))
            request_recovery(db)
        return True

    @staticmethod
    def used(db,channel,day):
        row=db.execute('SELECT value FROM cloud_meta WHERE key=?',('carry:'+channel+':'+day,)).fetchone()
        carry=json.loads(row['value']) if row else 0
        imported=db.execute("SELECT count(*) FROM cloud_reservations WHERE channel_id=? AND day=? AND lease='imported'",(channel,day)).fetchone()[0]
        added=db.execute("SELECT count(*) FROM cloud_reservations WHERE channel_id=? AND day=? AND lease!='imported'",(channel,day)).fetchone()[0]
        return max(carry,imported)+added

    def update_reservation(self, job, channel_id, source_key, reservation_state, **values):
        with self.db(True) as db:
            row = db.execute('SELECT * FROM cloud_reservations WHERE channel_id=? AND source_key=? AND job_id=? AND lease=?', (channel_id, source_key, job['id'], job['lease'])).fetchone()
            if not row: raise ValueError('Reservation identity mismatch; upload stopped.')
            record = json.loads(row['record']); record.update(values)
            db.execute('UPDATE cloud_reservations SET status=?,record=?,updated=? WHERE channel_id=? AND source_key=?', (reservation_state, json.dumps(record), time.time(), channel_id, source_key))
            request_recovery(db)

    def finish(self, job, state='done', notice=''):
        with self.db(True) as db:
            if state in ('attention','quarantined') and db.execute("SELECT 1 FROM cloud_reservations WHERE job_id=? AND status IN ('reserved','uncertain')",(job['id'],)).fetchone():
                db.execute('UPDATE cloud_routes SET enabled=0 WHERE id=?',(job['route_id'],))
            return bool(db.execute("UPDATE cloud_jobs SET state=?,notice=?,finished=? WHERE id=? AND lease=? AND state='running'",
                (state, notice, time.time(), job['id'], job['lease'])).rowcount)

    def import_ledger(self, config, ledger):
        """Idempotent, conservative import. No uploaded or uncertain identity is forgotten."""
        if ledger.get('channel_id') != config['channel_id'] or not isinstance(ledger.get('items'), dict):
            raise ValueError('Ledger channel does not match this exact destination.')
        with self.db(True) as db:
            today=datetime.now(ZoneInfo('America/Los_Angeles')).date()
            # Legacy workers counted New York days. Carry both possibly overlapping
            # dates conservatively so a timezone handover never resets today's cap.
            daily=ledger.get('daily',{})
            carry=sum(int(daily.get((today+timedelta(days=i)).isoformat(),0)) for i in (0,1))
            if carry:
                key='carry:'+config['channel_id']+':'+today.isoformat()
                previous=db.execute('SELECT value FROM cloud_meta WHERE key=?',(key,)).fetchone()
                db.execute('INSERT OR REPLACE INTO cloud_meta VALUES(?,?)',(key,json.dumps(max(carry,json.loads(previous['value']) if previous else 0))))
            for key, record in ledger['items'].items():
                if not isinstance(record, dict): raise ValueError('Damaged ledger record.')
                moment=record.get('reserved_at') or record.get('recorded_at','')
                try: day=datetime.fromisoformat(moment.replace('Z','+00:00')).astimezone(ZoneInfo('America/Los_Angeles')).date().isoformat()
                except (ValueError,TypeError): day=''
                db.execute('INSERT OR IGNORE INTO cloud_reservations VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (config['channel_id'], key, config['id'], 0, 'imported', day,
                     config.get('project_id', 'unknown-project'), 'imported', json.dumps(record), time.time()))
        return len(ledger['items'])

    def snapshot(self):
        with self.db() as db:
            return dict(workers=[dict(r) for r in db.execute('SELECT * FROM cloud_workers ORDER BY last_seen DESC LIMIT 50')],
                jobs=[{k: r[k] for k in ('id','route_id','mode','state','worker','created','finished','notice')} for r in db.execute('SELECT * FROM cloud_jobs ORDER BY id DESC LIMIT 100')],
                reservations=db.execute('SELECT count(*) FROM cloud_reservations').fetchone()[0],
                uncertain=db.execute("SELECT count(*) FROM cloud_reservations WHERE status IN ('uncertain','reserved')").fetchone()[0])

    def export(self):
        with self.db() as db:
            return {table: [dict(r) for r in db.execute('SELECT * FROM '+table)] for table in
                ('cloud_meta','cloud_routes','cloud_jobs','cloud_reservations','cloud_workers','cloud_events','cloud_commands')}

    def restore(self, data):
        """Restore is always paused. Never trust a restored active worker lease."""
        with self.db(True) as db:
            for table in ('cloud_meta','cloud_routes','cloud_jobs','cloud_reservations','cloud_workers','cloud_events','cloud_commands'):
                fields = [r[1] for r in db.execute('PRAGMA table_info('+table+')')]
                for original in data.get(table, []):
                    row = dict(original)
                    if table == 'cloud_routes': row['enabled'] = 0
                    if table == 'cloud_jobs' and row.get('state') in ('queued','running'): row['state'] = 'quarantined'; row['notice'] = 'Restored paused. Verify old runner stopped before resuming.'
                    if table == 'cloud_reservations' and row.get('status') == 'reserved': row['status'] = 'uncertain'
                    if table == 'cloud_workers': row['status'] = 'restored-offline'
                    db.execute('INSERT OR IGNORE INTO '+table+'('+','.join(fields)+') VALUES('+','.join('?' for _ in fields)+')', [row[k] for k in fields])
