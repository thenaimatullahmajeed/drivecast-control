"""Local records only: SQLite, read-only workflow imports, and portable exports."""

import ast
import csv
import io
import json
import re
import sqlite3
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


def safe_error(value):
    # Never retain raw logs, request URLs, headers, or OAuth credentials.
    value = re.sub(r"https?://\S+", "[link removed]", str(value))
    value = re.sub(r"(?i)(access_token|refresh_token|client_secret|authorization|code)\s*[:=]\s*\S+",
                   r"\1=[redacted]", value)
    value = re.sub(r"(?:ya29\.|gh[pousr]_|github_pat_|1//)[A-Za-z0-9._/-]+", "[redacted]", value)
    return value[:1000]


def parse_uploads(log, fallback_time):
    """Only recognized uploader messages count; a green run is not an upload."""
    files = {}
    patterns = (
        ("accepted", re.compile(r"^YouTube accepted (.+) \(([A-Za-z0-9_-]+)\): https://youtu\.be/([A-Za-z0-9_-]{11})$")),
        ("Done", re.compile(r"^Done: (.+) \(([A-Za-z0-9_-]+)\)$")),
        ("Failed", re.compile(r"^Failed: (.+) \(([A-Za-z0-9_-]+)\): (.*)$")),
        ("Processing", re.compile(r"^STUCK IN PROCESSING .+?never retried: (.+) \(([A-Za-z0-9_-]+)\)\. Review YouTube manually\.$")),
        ("Dry run", re.compile(r"^DRY RUN .+?would upload: (.+) \(([A-Za-z0-9_-]+)\)$")),
    )
    for line in log.splitlines():
        message = re.search(r"\b(?:INFO|WARNING|ERROR) (.*)$", line)
        if not message:
            continue
        for status, pattern in patterns:
            match = pattern.fullmatch(message[1])
            if not match or len(match[1]) > 4096:
                continue
            try:
                filename = ast.literal_eval(match[1])
            except (ValueError, SyntaxError):
                continue
            if not isinstance(filename, str):
                continue
            stamp = re.search(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z", line)
            row = files.setdefault(match[2], dict(file_id=match[2], filename=filename,
                status="Needs review", youtube_id="", error="", recorded_at=fallback_time))
            row["recorded_at"] = stamp[0] if stamp else fallback_time
            if status == "accepted":
                row["youtube_id"] = match[3]
                row["status"] = "Needs review"
            else:
                row["status"] = status
            if status == "Failed":
                row["error"] = safe_error(match[3])
                if row["youtube_id"]:
                    row["status"] = "Needs review"
            if status == "Processing":
                row["error"] = "Left in Processing; never auto-retry. Check YouTube Studio."
            break
    return list(files.values())


class RecordStore:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    client_id TEXT, run_id INTEGER, attempt INTEGER, client_name TEXT,
                    repo_name TEXT, created_at TEXT, updated_at TEXT, status TEXT,
                    conclusion TEXT, event TEXT, url TEXT, logs_state TEXT DEFAULT 'pending',
                    PRIMARY KEY(client_id, run_id, attempt));
                CREATE TABLE IF NOT EXISTS uploads (
                    client_id TEXT, run_id INTEGER, attempt INTEGER, file_id TEXT,
                    filename TEXT, status TEXT, youtube_id TEXT, error TEXT, recorded_at TEXT,
                    PRIMARY KEY(client_id, run_id, attempt, file_id));
                CREATE TABLE IF NOT EXISTS activity (
                    id INTEGER PRIMARY KEY, client_id TEXT, client_name TEXT,
                    action TEXT, outcome TEXT, recorded_at TEXT);
                CREATE TABLE IF NOT EXISTS video_stats (
                    client_id TEXT, platform TEXT, video_id TEXT, views TEXT, likes TEXT, comments TEXT,
                    checked_at TEXT, notice TEXT, PRIMARY KEY(client_id,platform,video_id));
                CREATE TABLE IF NOT EXISTS destinations (
                    client_id TEXT, source_key TEXT, platform TEXT, target_id TEXT, video_id TEXT,
                    title TEXT, status TEXT, error TEXT, recorded_at TEXT, source_url TEXT,
                    run_id INTEGER, attempt INTEGER, PRIMARY KEY(client_id,source_key,platform,target_id));
                CREATE TABLE IF NOT EXISTS video_details (
                    client_id TEXT, video_id TEXT, privacy TEXT, requested_privacy TEXT,
                    PRIMARY KEY(client_id,video_id));
            """)
            yield db
            db.commit()
        finally:
            db.close()

    def activity(self, client, action, outcome):
        with self.connect() as db:
            db.execute("INSERT INTO activity(client_id,client_name,action,outcome,recorded_at) VALUES(?,?,?,?,?)",
                       (client.get("id", ""), client.get("name", "GitHub"), action, outcome, now()))

    def cached(self, client_id, run_id, attempt):
        with self.connect() as db:
            row = db.execute("SELECT status,logs_state FROM runs WHERE client_id=? AND run_id=? AND attempt=?",
                             (client_id, run_id, attempt)).fetchone()
        return bool(row and row["status"] == "completed" and row["logs_state"] == "imported")

    def import_run(self, client, run, log=None, logs_state="pending"):
        run_id, attempt = int(run["id"]), int(run.get("run_attempt", 1))
        # Build trusted links from identity instead of rendering URLs found in logs.
        url = f"https://github.com/{client['owner']}/{client['repo_name']}/actions/runs/{run_id}/attempts/{attempt}"
        with self.connect() as db:
            db.execute("""INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(client_id,run_id,attempt) DO UPDATE SET
                client_name=excluded.client_name,updated_at=excluded.updated_at,status=excluded.status,
                conclusion=excluded.conclusion,logs_state=excluded.logs_state""",
                (client["id"], run_id, attempt, client["name"], client["repo_name"], run["created_at"],
                 run.get("updated_at", run["created_at"]), run["status"], run.get("conclusion") or "",
                 run.get("event", ""), url, logs_state))
            if log is not None:
                for row in parse_uploads(log, run["created_at"]):
                    if client.get('source_type')=='instagram' and not row['file_id'].startswith('instagram:'):
                        row['file_id']='instagram:'+row['file_id']
                    db.execute("""INSERT INTO uploads VALUES(?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(client_id,run_id,attempt,file_id) DO UPDATE SET
                        filename=excluded.filename,status=excluded.status,
                        youtube_id=CASE WHEN excluded.youtube_id='' THEN uploads.youtube_id ELSE excluded.youtube_id END,
                        error=excluded.error,recorded_at=excluded.recorded_at""",
                        (client["id"], run_id, attempt, row["file_id"], row["filename"], row["status"],
                         row["youtube_id"], row["error"], row["recorded_at"]))

    def data(self):
        with self.connect() as db:
            runs = [dict(r) for r in db.execute("SELECT * FROM runs ORDER BY created_at DESC,run_id DESC,attempt DESC")]
            uploads = [dict(r) for r in db.execute("""SELECT u.*,r.client_name,r.repo_name,r.url AS run_url
                FROM uploads u JOIN runs r USING(client_id,run_id,attempt)
                ORDER BY u.recorded_at DESC,u.run_id DESC,u.attempt DESC,u.file_id""")]
            activity = [dict(r) for r in db.execute("SELECT * FROM activity ORDER BY id DESC")]
            stats = [dict(r) for r in db.execute("SELECT * FROM video_stats")]
            destinations = [dict(r) for r in db.execute("SELECT * FROM destinations ORDER BY recorded_at DESC")]
            details = [dict(r) for r in db.execute('SELECT * FROM video_details')]
        visibility={(r['client_id'],r['video_id']):r for r in details}
        counters={(s['client_id'],s['platform'],s['video_id']):s for s in stats}
        facebook={}
        for item in destinations:
            if item['platform']=='facebook':
                facebook.setdefault((item['client_id'],item['source_key']),item)
        for row in uploads:
            row["youtube_url"] = "https://youtu.be/" + row["youtube_id"] if row["youtube_id"] else ""
            row["drive_url"] = "" if row["file_id"].startswith("instagram:") else "https://drive.google.com/file/d/" + row["file_id"] + "/view"
            yt=counters.get((row['client_id'],'youtube',row['youtube_id']),{})
            source_key=row['file_id'] if row['file_id'].startswith('instagram:') else 'drive:'+row['file_id']
            fb=facebook.get((row['client_id'],source_key),{})
            fc=counters.get((row['client_id'],'facebook',fb.get('video_id','')),{})
            row.update(youtube_views=yt.get('views'),youtube_likes=yt.get('likes'),youtube_comments=yt.get('comments'),
                stats_checked_at=yt.get('checked_at',''),stats_notice=yt.get('notice',''),facebook_id=fb.get('video_id',''),
                facebook_url='https://www.facebook.com/'+fb['video_id'] if fb.get('video_id') else '',
                facebook_status=fb.get('status','Not used'),facebook_views=fc.get('views'),
                facebook_checked_at=fc.get('checked_at',''),facebook_notice=fc.get('notice',''))
            known=visibility.get((row['client_id'],row['youtube_id']),{})
            row.update(actual_privacy=known.get('privacy','unknown'),requested_privacy=known.get('requested_privacy','unknown'))
        return dict(uploads=uploads, runs=runs, activity=activity,video_stats=stats,destinations=destinations,video_details=details)

    def set_stats(self, client_id, platform, video_id, values, notice=''):
        with self.connect() as db:
            previous=db.execute('SELECT * FROM video_stats WHERE client_id=? AND platform=? AND video_id=?',
                                (client_id,platform,video_id)).fetchone()
            checked_at=values.get('last_success') or values.get('checked_at') or now()
            if notice and previous:
                values={k:previous[k] for k in ('views','likes','comments')}
                checked_at=previous['checked_at']  # A failed check must not make cached counters look fresh.
            db.execute('INSERT OR REPLACE INTO video_stats VALUES(?,?,?,?,?,?,?,?)',
                  (client_id,platform,video_id,*[str(values[k]) if values.get(k) is not None else None for k in ('views','likes','comments')],checked_at,safe_error(notice)))
            if platform=='youtube' and values.get('privacy'):
                db.execute('INSERT INTO video_details VALUES(?,?,?,?) ON CONFLICT(client_id,video_id) DO UPDATE SET privacy=excluded.privacy',
                    (client_id,video_id,values['privacy'],'unknown'))

    def import_destination(self,client,ledger,platform='facebook'):
        with self.connect() as db:
            for source_key,item in ledger.get('items',{}).items():
                if item.get('status')=='Skipped prior route':continue
                db.execute('INSERT OR REPLACE INTO destinations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    (client['id'],source_key,platform,ledger.get('channel_id',''),item.get(platform+'_id',''),
                     item.get('title') or item.get('filename',''),item.get('status','Needs review'),safe_error(item.get('error','')),
                     item.get('recorded_at') or now(),item.get('source_url',''),int(item.get('run_id',0)),int(item.get('attempt',1))))

    def import_ledger(self, client, ledger):
        for item in ledger.get("items", {}).values():
            if item.get('status')=='Skipped prior route':continue
            run_id, attempt = int(item.get("run_id", 0)), int(item.get("attempt", 1))
            created = item.get("recorded_at") or now()
            with self.connect() as db:
                if item.get('youtube_id'):
                    db.execute('INSERT INTO video_details VALUES(?,?,?,?) ON CONFLICT(client_id,video_id) DO UPDATE SET requested_privacy=excluded.requested_privacy,privacy=CASE WHEN excluded.privacy IN (\'public\',\'private\',\'unlisted\') THEN excluded.privacy ELSE video_details.privacy END',
                        (client['id'],item['youtube_id'],item.get('privacy','unknown'),item.get('requested_privacy','unknown')))
                if not db.execute("SELECT 1 FROM runs WHERE client_id=? AND run_id=? AND attempt=?",
                                  (client["id"], run_id, attempt)).fetchone():
                    url = '' if client.get('runner')=='azure' else f"https://github.com/{client['owner']}/{client['repo_name']}/actions/runs/{run_id}"
                    db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (client["id"],run_id,attempt,
                        client["name"],client["repo_name"],created,created,"recorded","","worker",url,"ledger"))
                source_id = ("instagram:" if item.get("source") == "instagram" else "") + item["id"]
                db.execute("""INSERT INTO uploads VALUES(?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(client_id,run_id,attempt,file_id) DO UPDATE SET
                    filename=excluded.filename,status=excluded.status,youtube_id=excluded.youtube_id,
                    error=excluded.error,recorded_at=excluded.recorded_at""", (client["id"], run_id, attempt,
                    source_id, item.get("title") or item.get("filename", item["id"]),item.get("status","Needs review"),
                    item.get("youtube_id", ""),safe_error(item.get("error", "")),created))

    def restore_data(self, data):
        """Merge a versioned private backup using explicit columns only."""
        tables = {
            "runs": ("client_id","run_id","attempt","client_name","repo_name","created_at","updated_at","status","conclusion","event","url","logs_state"),
            "uploads": ("client_id","run_id","attempt","file_id","filename","status","youtube_id","error","recorded_at"),
            "activity": ("id","client_id","client_name","action","outcome","recorded_at"),
            "video_stats": ("client_id","platform","video_id","views","likes","comments","checked_at","notice"),
            "destinations": ("client_id","source_key","platform","target_id","video_id","title","status","error","recorded_at","source_url","run_id","attempt"),
            "video_details": ("client_id","video_id","privacy","requested_privacy"),
        }
        with self.connect() as db:
            for table, fields in tables.items():
                for row in data.get(table, []):
                    if not isinstance(row, dict) or any(field not in row for field in fields):
                        raise ValueError("Incomplete record backup; restore stopped.")
                    db.execute(f"INSERT OR REPLACE INTO {table}({','.join(fields)}) VALUES({','.join('?' for _ in fields)})",
                               [row[field] for field in fields])

    def csv(self, kind):
        if kind not in ("uploads", "runs", "activity"):
            raise ValueError("Unknown export type.")
        fields = {
            "uploads": ("client_name", "recorded_at", "filename", "status", "actual_privacy", "requested_privacy", "youtube_url", "facebook_id", "facebook_url", "facebook_status", "drive_url", "youtube_views", "youtube_likes", "youtube_comments", "stats_checked_at", "stats_notice", "facebook_views", "facebook_checked_at", "facebook_notice", "error", "run_url", "attempt"),
            "runs": ("client_name", "created_at", "status", "conclusion", "event", "logs_state", "url", "attempt"),
            "activity": ("client_name", "recorded_at", "action", "outcome"),
        }[kind]
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(fields)
        for row in self.data()[kind]:
            values = [str(row.get(key, "")) for key in fields]
            # Excel/Sheets formula injection protection, including leading whitespace.
            writer.writerow(["'" + value if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r"))
                             else value for value in values])
        return output.getvalue().encode("utf-8-sig")

    def backup(self):
        destination = self.path.parent / "record-backups" / ("records-" + now()[:10] + ".sqlite3")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as source:
            target = sqlite3.connect(destination)
            try:
                source.backup(target)
            finally:
                target.close()
        # A bounded set of our own daily snapshots; never touch unrelated files.
        for old in sorted(destination.parent.glob("records-????-??-??.sqlite3"), reverse=True)[7:]:
            old.unlink()

    def export_backup(self, clients):
        data = self.data()
        data.update(format="drivecast-records-v1", exported_at=now(), clients=clients)
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("records.json", json.dumps(data, ensure_ascii=False, indent=2))
            for kind in ("uploads", "runs", "activity"):
                archive.writestr(kind + ".csv", self.csv(kind))
        return output.getvalue()
