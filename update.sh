#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

STATUS="$(git status --porcelain --untracked-files=no)"
if [[ -n "$STATUS" ]]; then
    echo "ERROR: tracked files have local changes:" >&2
    echo "$STATUS" >&2
    exit 1
fi

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
git fetch origin "$BRANCH"
git merge --ff-only "origin/$BRANCH"

python3 -m py_compile bot.py app_server.py telegram_format.py

COMMIT="$(git rev-parse --short HEAD)"
echo "Updated to $COMMIT on $BRANCH."
