#!/usr/bin/env bash
# One-time setup: sudo bash deploy/setup-autodeploy.sh /path/to/deployment-key.pub
set -euo pipefail
[[ $EUID == 0 && $# == 1 ]] || { echo 'Usage: sudo bash setup-autodeploy.sh key.pub' >&2; exit 1; }
KEY=$(cat "$1")
[[ $KEY =~ ^ssh-ed25519\ [A-Za-z0-9+/=]+(\ .*)?$ ]] || { echo 'Expected an Ed25519 public key.' >&2; exit 1; }
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
id smj-deploy >/dev/null 2>&1 || useradd --create-home --shell /bin/bash smj-deploy
install -d -o smj-deploy -g smj-deploy -m 0700 /home/smj-deploy/.ssh
install -o root -g root -m 0755 "$SCRIPT_DIR/ssh-dispatch.sh" /usr/local/sbin/smj-deploy-dispatch
TEMP_SUDOERS=$(mktemp)
trap 'rm -f "$TEMP_SUDOERS"' EXIT
printf '%s\n' 'Defaults:smj-deploy env_keep += "SSH_ORIGINAL_COMMAND"' \
    'smj-deploy ALL=(root) NOPASSWD: /usr/local/sbin/smj-deploy-dispatch' > "$TEMP_SUDOERS"
visudo -cf "$TEMP_SUDOERS"
install -o root -g root -m 0440 "$TEMP_SUDOERS" /etc/sudoers.d/smj-deploy
printf 'restrict,command="sudo -n /usr/local/sbin/smj-deploy-dispatch" %s\n' "$KEY" \
    > /home/smj-deploy/.ssh/authorized_keys
chown smj-deploy:smj-deploy /home/smj-deploy/.ssh/authorized_keys
chmod 0600 /home/smj-deploy/.ssh/authorized_keys
echo 'Restricted deployment access configured.'
