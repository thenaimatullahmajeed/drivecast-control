#!/usr/bin/env bash
# New server only; never overwrite an active workspace.
set -euo pipefail
umask 077
BACKUP=$(realpath "${1:?Provide encrypted server backup file}")
BASE=/opt/drivecast
test -f "$BASE/secrets/.env" || { echo 'Restore the original private backup key in secrets/.env first.'; exit 2; }
test -d "$BASE/current/azure"
if find "$BASE/data" -mindepth 1 -maxdepth 1 -print -quit | grep -q .; then
  echo 'Destination is not empty. Preserve it and restore on a new VM; no data changed.'; exit 2
fi
cd "$BASE/current/azure"
docker compose stop web scheduler workers backup
docker compose run --interactive=false --rm --no-deps -v "$BACKUP:/restore.encrypted:ro" backup python cloud_backup.py restore --file /restore.encrypted --root /data
chown -R 10001:10001 "$BASE/data"
if [[ -f "$BASE/data/recovery/destination.json" ]]; then
  install -m 600 -o 10001 -g 10001 "$BASE/data/recovery/destination.json" "$BASE/secrets/recovery.json"
fi
docker compose up -d --wait
echo 'Restored paused. Confirm old server stopped, import final records, verify channel IDs, then explicitly resume.'
