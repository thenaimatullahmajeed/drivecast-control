#!/usr/bin/env bash
# Root-only, repo-scoped deploy key. Mirrors an allowlist; never runtime data.
set -euo pipefail
umask 077
RELEASE=$(realpath "${1:?Provide source release}")
BASE=/opt/drivecast
CONFIG="$BASE/secrets/github-source.json"
test -f "$CONFIG"
REPO=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["repository"])' "$CONFIG")
[[ "$REPO" =~ ^[A-Za-z0-9-]+/[A-Za-z0-9_.-]+$ ]] || exit 2
export GIT_SSH_COMMAND="ssh -i $BASE/secrets/source-deploy-key -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=$BASE/secrets/github-known-hosts"
MIRROR="$BASE/source-mirror"
if [[ ! -d "$MIRROR/.git" ]]; then
  git clone --quiet "ssh://git@ssh.github.com:443/$REPO.git" "$MIRROR"
fi
git -C "$MIRROR" fetch --quiet origin main
git -C "$MIRROR" merge --quiet --ff-only origin/main
python3 - "$RELEASE" "$MIRROR" <<'PY'
import json,pathlib,shutil,sys
source,destination=map(lambda p:pathlib.Path(p).resolve(),sys.argv[1:])
for name in json.loads((source/'source-files.json').read_text())['files']:
 path=(source/name).resolve()
 assert path.is_relative_to(source) and not path.is_symlink() and path.name!='.env'
 assert not any(p in ('work','.git','__pycache__') for p in pathlib.Path(name).parts)
 target=destination/'azure-source'/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,target)
shutil.copy2(source/'RECOVER_DRIVECAST.md',destination/'RECOVER_DRIVECAST.md')
PY
git -C "$MIRROR" add -- azure-source RECOVER_DRIVECAST.md
if ! git -C "$MIRROR" diff --cached --quiet; then
  git -C "$MIRROR" -c user.name='DriveCast Recovery' -c user.email='drivecast-recovery@localhost' commit --quiet -m 'Preserve verified DriveCast recovery source'
  git -C "$MIRROR" push --quiet origin HEAD:main
fi
git -C "$MIRROR" rev-parse HEAD > "$BASE/data/source-mirror-commit"
