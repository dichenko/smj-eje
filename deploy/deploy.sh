#!/usr/bin/env bash
# Run as root through the restricted deployment SSH key, or manually with a commit SHA.
set -Eeuo pipefail
umask 077

APP_DIR=${SMJ_APP_DIR:-/opt/smj-eje}
STATE_DIR=${SMJ_DEPLOY_STATE_DIR:-/opt/smj-eje-deploy}
TARGET=${1:-}
[[ $EUID == 0 ]] || { echo 'Deployment requires root.' >&2; exit 1; }
[[ $TARGET =~ ^[0-9a-f]{40}$ ]] || { echo 'Expected a full commit SHA.' >&2; exit 1; }
mkdir -p "$STATE_DIR/venvs"
exec 9>"$STATE_DIR/deploy.lock"
flock -w 900 9
cd "$APP_DIR"
[[ -z $(git status --porcelain) ]] || { echo 'Server checkout has local changes; refusing to overwrite.' >&2; exit 1; }
git fetch --prune origin main
if [[ $(git rev-parse origin/main) != "$TARGET" ]]; then
    echo 'A newer push exists; skipping this superseded deployment.'
    exit 0
fi
PREVIOUS=$(git rev-parse HEAD)
git merge-base --is-ancestor "$PREVIOUS" "$TARGET"
SYNC_TIMER=0
BACKUP_TIMER=0
systemctl is-active --quiet smj-sync.timer && SYNC_TIMER=1
systemctl is-active --quiet smj-backup.timer && BACKUP_TIMER=1
UNITS_DIR=$(mktemp -d "$STATE_DIR/units-XXXXXX")
cp /etc/systemd/system/smj-*.service /etc/systemd/system/smj-*.timer "$UNITS_DIR/"
OLD_VENV=$(readlink -f "$APP_DIR/.venv")
NEW_VENV=$(mktemp -d "$STATE_DIR/venvs/venv-XXXXXX")
CODE_CHANGED=0
VENV_CHANGED=0

restore_timers() {
    if [[ $SYNC_TIMER == 1 ]]; then systemctl start smj-sync.timer; fi
    if [[ $BACKUP_TIMER == 1 ]]; then systemctl start smj-backup.timer; fi
}

rollback() {
    local result=$?
    trap - ERR
    set +e
    echo "Deployment failed; rolling code back to $PREVIOUS." >&2
    systemctl stop smj-web.service
    if [[ $CODE_CHANGED == 1 ]]; then git reset --hard "$PREVIOUS"; fi
    if [[ $VENV_CHANGED == 1 ]]; then ln -sfn "$OLD_VENV" "$APP_DIR/.venv"; fi
    cp "$UNITS_DIR/"* /etc/systemd/system/
    systemctl daemon-reload
    systemctl restart smj-web.service
    restore_timers
    curl --fail --silent --show-error --retry 10 --retry-connrefused --retry-delay 2 \
        --max-time 5 http://127.0.0.1:8081/healthz || echo 'Rollback health check failed; inspect smj-web.service.' >&2
    exit "$result"
}
trap rollback ERR

systemctl stop smj-sync.timer smj-backup.timer smj-sync.service smj-backup.service
systemctl start smj-backup.service
systemctl stop smj-web.service
CODE_CHANGED=1
git merge --ff-only "$TARGET"
python3 -m venv "$NEW_VENV"
"$NEW_VENV/bin/python" -m pip install --disable-pip-version-check -r requirements.txt
"$NEW_VENV/bin/python" -m pip check
# Preserve the original environment on the first automated deployment.
if [[ ! -L "$APP_DIR/.venv" ]]; then
    mv "$APP_DIR/.venv" "$STATE_DIR/bootstrap-venv"
    OLD_VENV="$STATE_DIR/bootstrap-venv"
    ln -s "$OLD_VENV" "$APP_DIR/.venv"
fi
VENV_CHANGED=1
ln -sfn "$NEW_VENV" "$APP_DIR/.venv"
install -m 0644 deploy/smj-*.service deploy/smj-*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl restart smj-web.service
curl --fail --silent --show-error --retry 10 --retry-connrefused --retry-delay 2 \
    --max-time 5 http://127.0.0.1:8081/healthz
restore_timers
trap - ERR
printf '\nDeployed %s successfully. Previous commit: %s\n' "$TARGET" "$PREVIOUS"
# Retain current and previous environments; old dependencies remain available for rollback.
for directory in "$STATE_DIR"/venvs/venv-*; do
    [[ -d $directory && $directory != "$NEW_VENV" && $directory != "$OLD_VENV" ]] || continue
    rm -rf -- "$directory"
done
rm -rf -- "$UNITS_DIR"
