#!/usr/bin/env bash
# Ubuntu 24.04 VM on any provider. Does not create resources or change firewall/DNS.
set -euo pipefail
umask 077
[[ $EUID == 0 ]] || { echo 'Run with sudo on your target VM.'; exit 2; }
HOST="${1:?Provide public hostname}"
EMAIL="${2:?Provide your existing owner email}"
SOURCE=$(realpath "${3:-$(dirname "$0")/..}")
test -f "$SOURCE/Dockerfile"
if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
  apt-get update -qq
  apt-get install -y docker.io docker-compose-v2 ca-certificates
fi
systemctl enable --now docker
# Paid hosts have no inherited Azure trial deadline unless explicitly supplied.
export DRIVECAST_TRIAL_END="${DRIVECAST_TRIAL_END-}"
bash "$SOURCE/azure/install.sh" "$HOST" "$EMAIL" "$SOURCE"
