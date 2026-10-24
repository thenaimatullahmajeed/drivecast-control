#!/usr/bin/env bash
# sudo bash azure/deploy.sh /path/to/extracted/release
set -euo pipefail
umask 077
RELEASE=$(realpath "${1:?Provide an extracted source release directory}")
BASE=/opt/drivecast
[[ "$RELEASE" == "$BASE/releases/"* ]] || { echo 'Release must be inside /opt/drivecast/releases'; exit 2; }
test -f "$BASE/secrets/.env"
if [[ ! -f "$BASE/secrets/recovery.json" ]]; then
  printf '{}\n' > "$BASE/secrets/recovery.json"
  chown 10001:10001 "$BASE/secrets/recovery.json"
  chmod 600 "$BASE/secrets/recovery.json"
fi
chmod 600 "$BASE/secrets/.env"
install -m 600 "$BASE/secrets/.env" "$RELEASE/azure/.env"
PREVIOUS=$(readlink -f "$BASE/current" || true)
VERSION=$(cd "$RELEASE"; find . -type f ! -name .env ! -name '*.pyc' -print0 | sort -z | xargs -0 sha256sum | sha256sum | cut -c1-12)
export DRIVECAST_VERSION="$VERSION"
cd "$RELEASE/azure"
docker compose build --quiet web
# Schema updates are additive. The pre-deploy encrypted snapshot is mandatory for upgrades.
if [[ -n "$PREVIOUS" && -d "$PREVIOUS" ]]; then
  (cd "$PREVIOUS/azure"; docker compose run --interactive=false --rm --no-deps backup python cloud_backup.py run)
fi
rollback() {
  echo 'Deployment health check failed; restoring previous application image. Uploads stay paused.'
  if [[ -n "$PREVIOUS" && -d "$PREVIOUS" ]]; then
    ln -sfn "$PREVIOUS" "$BASE/current"
    (unset DRIVECAST_VERSION; cd "$PREVIOUS/azure"; docker compose up -d --no-build)
  else docker compose stop web scheduler workers; fi
  rm -f "$BASE/data/draining"
  exit 1
}
trap rollback ERR
if [[ -n "$PREVIOUS" && -d "$PREVIOUS" ]]; then
  touch "$BASE/data/draining"
  (cd "$PREVIOUS/azure"; docker compose stop scheduler)
  for attempt in $(seq 1 60); do
    ACTIVE=$(python3 - "$BASE/data" <<'PY'
import pathlib,sqlite3,sys
from contextlib import closing
total=0
for path in pathlib.Path(sys.argv[1]).glob('accounts/users/*/records.sqlite3'):
    with closing(sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True)) as db:
        total+=db.execute("SELECT count(*) FROM cloud_jobs WHERE state='running'").fetchone()[0]
print(total)
PY
)
    [[ "$ACTIVE" == 0 ]] && break
    sleep 5
  done
  [[ "$ACTIVE" == 0 ]] || { echo 'Uploads still running. Deployment cancelled without killing them.'; false; }
  (cd "$PREVIOUS/azure"; docker compose stop workers)
fi
printf '\nDRIVECAST_VERSION=%s\n' "$VERSION" >> .env
docker compose up -d --wait --wait-timeout 240
docker compose exec --interactive=false -T web python -c "import urllib.request,os; assert urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8080/healthz',headers={'Host':os.environ['DRIVECAST_HOST']}),timeout=5).status==200"
ln -sfn "$RELEASE" "$BASE/current"
rm -f "$BASE/data/draining"
trap - ERR
echo 'Application healthy. Verify HTTPS externally. The existing approved publishing setting was preserved.'
if [[ -f "$BASE/secrets/github-source.json" ]]; then
  if bash "$RELEASE/hosting/push-source.sh" "$RELEASE"; then
    echo 'Private GitHub source mirror verified.'
  else
    echo 'Source mirror needs review; application remains healthy and encrypted Drive backups continue.'
  fi
fi
