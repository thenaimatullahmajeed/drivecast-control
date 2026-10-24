#!/usr/bin/env bash
# sudo bash azure/install.sh <public-hostname> <owner-email> [extracted-source-directory]
# Optional environment: RECOVERY_KEY_FILE and SERVER_BACKUP_FILE for a new-VM restore.
set -euo pipefail
umask 077
[[ $EUID == 0 ]] || { echo 'Run with sudo on the Azure VM.'; exit 2; }
HOST="${1:?Provide public DNS hostname, without https://}"
EMAIL="${2:?Provide owner email for an invitation}"
SOURCE=$(realpath "${3:-$(dirname "$0")/..}")
[[ "$HOST" =~ ^[a-zA-Z0-9][a-zA-Z0-9.-]+[a-zA-Z0-9]$ ]] || { echo 'Invalid hostname'; exit 2; }
test -f "$SOURCE/Dockerfile"
BASE=/opt/drivecast
RELEASE="$BASE/releases/$(date -u +%Y%m%dT%H%M%SZ)"
[[ "$RELEASE" != "$SOURCE/"* ]] || { echo 'Use an extracted source directory, not its parent.'; exit 2; }
mkdir -p "$BASE/secrets" "$BASE/data" "$RELEASE"
chmod 700 "$BASE/secrets"
if [[ ! -f "$BASE/secrets/recovery.json" ]]; then
  printf '{}\n' > "$BASE/secrets/recovery.json"
  chown 10001:10001 "$BASE/secrets/recovery.json"
  chmod 600 "$BASE/secrets/recovery.json"
fi
chown 10001:10001 "$BASE/data"
cp -a "$SOURCE/." "$RELEASE/"
if [[ ! -f "$BASE/secrets/.env" ]]; then
  if find "$BASE/data" -mindepth 1 -maxdepth 1 -print -quit | grep -q .; then
    echo 'Existing data has no service configuration. Restore its original private .env first.'; exit 2
  fi
  export DRIVECAST_INSTALL_HOST="$HOST"
  python3 - "$BASE/secrets/.env" <<'PY'
import base64,os,pathlib,secrets,sys
host=os.environ['DRIVECAST_INSTALL_HOST']
file=os.getenv('RECOVERY_KEY_FILE')
key=pathlib.Path(file).read_text().strip() if file else base64.b64encode(secrets.token_bytes(32)).decode()
assert len(base64.b64decode(key,validate=True))==32,'Invalid recovery key'
values=dict(DRIVECAST_HOST=host,DRIVECAST_ORIGIN='https://'+host,DRIVECAST_OAUTH_REDIRECT='https://'+host+'/oauth/callback',
 DRIVECAST_HOSTED='1',DRIVECAST_DATA_DIR='/data',DRIVECAST_LIVE_UPLOADS='0',DRIVECAST_WORKERS='2',
 DRIVECAST_PROJECT_DAILY_LIMIT='90',DRIVECAST_MAX_VIDEO_MB='250',DRIVECAST_MONTHLY_TRANSFER_GB='100',
 DRIVECAST_TRIAL_END=os.getenv('DRIVECAST_TRIAL_END','2026-11-20'),DRIVECAST_BACKUP_KEY=key,
 DRIVECAST_BACKUP_STORAGE=os.getenv('DRIVECAST_BACKUP_STORAGE',''),DRIVECAST_BACKUP_CONTAINER='drivecast-backups')
path=pathlib.Path(sys.argv[1]);path.write_text('\n'.join(k+'='+v for k,v in values.items())+'\n');path.chmod(0o600)
if not file:
 recovery=path.with_name('offline-recovery.key');recovery.write_text(key);recovery.chmod(0o600)
PY
fi
install -m 600 "$BASE/secrets/.env" "$RELEASE/azure/.env"
cd "$RELEASE/azure"
docker compose build --quiet web
if [[ -n "${SERVER_BACKUP_FILE:-}" ]]; then
  if find "$BASE/data" -mindepth 1 -maxdepth 1 -print -quit | grep -q .; then
    echo 'Restore destination is not empty; existing data preserved.'; exit 2
  fi
  ARCHIVE=$(realpath "$SERVER_BACKUP_FILE")
  docker compose run --interactive=false --rm --no-deps -v "$ARCHIVE:/restore.encrypted:ro" backup python cloud_backup.py restore --file /restore.encrypted --root /data
  if [[ -f "$BASE/data/recovery/destination.json" ]]; then
    install -m 600 -o 10001 -g 10001 "$BASE/data/recovery/destination.json" "$BASE/secrets/recovery.json"
  fi
fi
USERS=$(python3 - "$BASE/data/accounts/accounts.sqlite3" <<'PY'
import pathlib,sqlite3,sys
from contextlib import closing
path=pathlib.Path(sys.argv[1])
if not path.exists(): print(0)
else:
 with closing(sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True)) as db: print(db.execute('SELECT count(*) FROM users').fetchone()[0])
PY
)
if [[ "$USERS" == 0 ]]; then
  OWNER_WORKSPACE_ID="${OWNER_WORKSPACE_ID:-}"
  if [[ -z "$OWNER_WORKSPACE_ID" ]]; then
    OWNER_WORKSPACE_ID=$(python3 - "$BASE/data/accounts/users" <<'PY'
import pathlib,re,sys
rows=[p.name for p in pathlib.Path(sys.argv[1]).glob('*') if p.is_dir() and re.fullmatch('[a-f0-9]{32}',p.name)]
assert len(rows)<=1,'Multiple restored workspaces: set OWNER_WORKSPACE_ID explicitly; no account was guessed.'
print(rows[0] if rows else '')
PY
)
  fi
  ARGS=(); [[ -z "$OWNER_WORKSPACE_ID" ]] || ARGS=(--workspace-id "$OWNER_WORKSPACE_ID")
  docker compose run --interactive=false --rm --no-deps web python cloud_admin.py invite --email "$EMAIL" "${ARGS[@]}" --output /data/owner-invitation.txt
fi
bash "$RELEASE/azure/deploy.sh" "$RELEASE"
echo 'Ready. Private invitation, if needed: /opt/drivecast/data/owner-invitation.txt'
echo 'Keep the offline recovery key separately. Restored channels remain paused.'
