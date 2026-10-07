#!/usr/bin/env bash
set -euo pipefail
# Installed as a root-owned file outside the checkout by setup-autodeploy.sh.
if [[ ${SSH_ORIGINAL_COMMAND:-} =~ ^deploy\ ([0-9a-f]{40})$ ]]; then
    COMMIT=${BASH_REMATCH[1]}
    git -C /opt/smj-eje fetch origin main
    if [[ $(git -C /opt/smj-eje rev-parse origin/main) != "$COMMIT" ]]; then
        echo 'A newer push exists; skipping this superseded deployment.'
        exit 0
    fi
    SCRIPT=$(mktemp)
    trap 'rm -f "$SCRIPT"' EXIT
    git -C /opt/smj-eje show "$COMMIT:deploy/deploy.sh" > "$SCRIPT"
    /bin/bash "$SCRIPT" "$COMMIT"
    exit 0
fi
echo 'Only deploy <full commit SHA> is allowed.' >&2
exit 1
