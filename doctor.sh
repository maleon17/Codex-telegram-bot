#!/usr/bin/env bash
set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

failures=0
check() {
    local label="$1"; shift
    if "$@" >/dev/null 2>&1; then
        echo "OK   $label"
    else
        echo "FAIL $label"
        failures=$((failures + 1))
    fi
}

check "python syntax" python3 -m py_compile bot.py app_server.py telegram_format.py
check "codex CLI" codex --version
check "owner Codex login" codex login status
check ".env exists" test -f .env
if [[ -f .env ]]; then
    mode="$(stat -c '%a' .env 2>/dev/null || stat -f '%Lp' .env)"
    if [[ "$mode" == "600" ]]; then
        echo "OK   .env permissions (600)"
    else
        echo "FAIL .env permissions ($mode, expected 600)"
        failures=$((failures + 1))
    fi
fi
if [[ -f state.json ]]; then
    check "state.json is valid JSON" python3 -c 'import json; json.load(open("state.json"))'
fi
if systemctl list-unit-files codex-telegram-bot.service >/dev/null 2>&1; then
    check "codex-telegram-bot.service active" systemctl is-active --quiet codex-telegram-bot.service
fi
exit "$failures"
