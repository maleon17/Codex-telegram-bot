#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

die() { echo "ERROR: $*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "$1 not found"; }

echo "== Codex Telegram Bot setup =="
need python3
need systemctl

CODEX_BIN="$(command -v codex || true)"
if [[ -z "$CODEX_BIN" && -x "$HOME/.local/bin/codex" ]]; then
    CODEX_BIN="$HOME/.local/bin/codex"
fi
[[ -n "$CODEX_BIN" ]] || die "Codex CLI not found. Install it, then rerun setup."
echo "Codex: $($CODEX_BIN --version)"

if ! "$CODEX_BIN" login status >/dev/null 2>&1; then
    echo "The owner's Codex account is not logged in."
    echo "Run: codex login"
    exit 1
fi
echo "Owner Codex account: authenticated"

read -rsp "Telegram bot token (from @BotFather): " BOT_TOKEN
echo
[[ "$BOT_TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]+$ ]] || die "Bot token format is invalid"
read -rp "Owner Telegram numeric ID: " OWNER_ID
[[ "$OWNER_ID" =~ ^[0-9]+$ ]] || die "OWNER_ID must be numeric"

read -rp "Default Codex workspace [$HOME]: " CODEX_CWD
CODEX_CWD="${CODEX_CWD:-$HOME}"
[[ -d "$CODEX_CWD" ]] || die "Workspace does not exist: $CODEX_CWD"
CODEX_CWD="$(cd "$CODEX_CWD" && pwd)"

read -rp "Default sandbox (read-only/workspace-write/danger-full-access) [danger-full-access]: " CODEX_SANDBOX
CODEX_SANDBOX="${CODEX_SANDBOX:-danger-full-access}"
case "$CODEX_SANDBOX" in
    read-only|workspace-write|danger-full-access) ;;
    *) die "Unknown sandbox: $CODEX_SANDBOX" ;;
esac

read -rp "systemd service name [codex-telegram-bot]: " SERVICE_NAME
SERVICE_NAME="${SERVICE_NAME:-codex-telegram-bot}"
[[ "$SERVICE_NAME" =~ ^[A-Za-z0-9_.@-]+$ ]] || die "Invalid service name"

INSTALL_USER="$(id -un)"
INSTALL_DIR="$SCRIPT_DIR"

python3 - "$BOT_TOKEN" <<'PY'
import json, sys, urllib.request
token = sys.argv[1]
try:
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/getMe", timeout=15) as response:
        result = json.load(response)
except Exception as exc:
    raise SystemExit(f"Telegram token check failed: {exc}")
if not result.get("ok"):
    raise SystemExit("Telegram rejected this bot token")
print("Telegram bot token: valid (@%s)" % result["result"].get("username", "unknown"))
PY

umask 077
{
    printf 'TELEGRAM_BOT_TOKEN=%s\n' "$BOT_TOKEN"
    printf 'OWNER_ID=%s\n' "$OWNER_ID"
    printf 'CODEX_CWD=%s\n' "$CODEX_CWD"
    printf 'CODEX_SANDBOX=%s\n' "$CODEX_SANDBOX"
    printf 'CODEX_BOT_STATE_FILE=%s/state.json\n' "$INSTALL_DIR"
    printf 'CODEX_BOT_WHITELIST_FILE=%s/whitelist.txt\n' "$INSTALL_DIR"
    printf 'CODEX_BOT_ACCOUNTS_DIR=%s/accounts\n' "$INSTALL_DIR"
    printf 'CODEX_BOT_RESTART_FILE=%s/restart.request\n' "$INSTALL_DIR"
} > .env
chmod 600 .env
touch whitelist.txt
chmod 600 whitelist.txt
mkdir -p accounts
chmod 700 accounts

UNIT_FILE="$(mktemp "/tmp/${SERVICE_NAME}.service.XXXXXX")"
trap 'rm -f "$UNIT_FILE"' EXIT
sed \
    -e "s|__USER__|${INSTALL_USER}|g" \
    -e "s|__INSTALL_DIR__|${INSTALL_DIR}|g" \
    codex-telegram-bot.service.example > "$UNIT_FILE"

python3 -m py_compile bot.py app_server.py telegram_format.py
echo "Installing /etc/systemd/system/${SERVICE_NAME}.service (sudo required)"
sudo install -m 0644 "$UNIT_FILE" "/etc/systemd/system/${SERVICE_NAME}.service"
sudo systemctl daemon-reload
sudo systemctl enable --now "${SERVICE_NAME}.service"

echo
sudo systemctl --no-pager --full status "${SERVICE_NAME}.service" || true
echo
echo "Installed."
echo "Logs:      journalctl -u ${SERVICE_NAME}.service -f"
echo "Restart:   $INSTALL_DIR/request-restart"
echo "Whitelist: $INSTALL_DIR/whitelist.txt (one Telegram ID per line; no restart needed)"
