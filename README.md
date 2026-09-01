# Codex Telegram Bot

A persistent, multi-account Telegram frontend for [OpenAI Codex](https://developers.openai.com/codex/).
It uses your ChatGPT/Codex subscription through the installed Codex CLI—no
metered OpenAI API key is required.

Unlike wrappers that launch `codex exec` for every message, this bridge keeps
one `codex app-server` alive per Telegram user. Threads survive restarts, and
completed turns are delivered as ordinary Telegram messages with an optional
collapsible process log.

## Features

- Persistent Codex threads: `/new`, `/sessions`, `/resume`.
- Rapid consecutive messages (including forwarded batches) are combined into one prompt.
- Forwarded text and Telegram rich messages keep their source/content context.
- A message sent during an active turn is added to that same turn automatically.
- Clean cancellation through `turn/interrupt` (`/stop`).
- Context compaction through `thread/compact/start` (`/compact`).
- Detailed `/usage`: session tokens, context size, subscription limits and reset times.
- Photos and image documents passed to Codex as `localImage` inputs.
- Model, sandbox and workspace controls per user.
- Deferred restarts that never terminate an active answer.
- Multi-account isolation with a separate `CODEX_HOME` and App Server process per user.
- No Python packages: the bridge itself uses only the standard library.

## Requirements

- Linux with Python 3.10+ and systemd.
- The [Codex CLI](https://developers.openai.com/codex/cli/) installed and available as `codex`.
- The owner's Codex CLI logged in (`codex login`).
- A Telegram bot token from [@BotFather](https://t.me/BotFather).
- Your numeric Telegram ID (for example from [@userinfobot](https://t.me/userinfobot)).

## Quick install

```bash
git clone https://github.com/maleon17/Codex-telegram-bot.git
cd Codex-telegram-bot

# Authenticate the owner's Codex account first.
codex login

# Interactive installer: validates Codex + Telegram, creates a protected
# .env, installs the systemd unit and starts the bot.
chmod +x setup.sh
./setup.sh
```

The installer asks for the bot token, owner ID, default workspace, sandbox,
and service name. The token is stored in `.env` with mode `600`; it is not
embedded in the world-readable systemd unit.

Check the installation:

```bash
./doctor.sh
journalctl -u codex-telegram-bot -f
```

## Manual install

1. Clone the repository and run `codex login` as the Linux user that will run the service.
2. Create `.env` in the repository (mode `600`):

   ```dotenv
   TELEGRAM_BOT_TOKEN=123456:replace_me
   OWNER_ID=123456789
   CODEX_CWD=/home/your-user
   CODEX_SANDBOX=danger-full-access
   CODEX_BOT_STATE_FILE=/absolute/path/Codex-telegram-bot/state.json
   CODEX_BOT_WHITELIST_FILE=/absolute/path/Codex-telegram-bot/whitelist.txt
   CODEX_BOT_ACCOUNTS_DIR=/absolute/path/Codex-telegram-bot/accounts
   CODEX_BOT_RESTART_FILE=/absolute/path/Codex-telegram-bot/restart.request
   ```

3. Copy `codex-telegram-bot.service.example` to
   `/etc/systemd/system/codex-telegram-bot.service` and replace `__USER__`
   and `__INSTALL_DIR__`.
4. Enable it:

   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now codex-telegram-bot.service
   ```

## Commands

| Command | Description |
|---|---|
| `/new` | Start a new Codex thread |
| `/sessions` | List recent threads for this account |
| `/resume <id>` | Resume by full ID or unique prefix |
| `/status` | Thread, account, model, sandbox, workspace and busy state |
| `/stop` | Interrupt the active turn |
| `/compact` | Compact the current thread context |
| `/usage` | Session tokens, context and account rate limits |
| `/model [id]` | Show available models or select one by its real ID |
| `/effort [level]` | Show or select the reasoning power supported by the current model |
| `/mode read-only\|workspace-write\|full` | Select the sandbox policy |
| `/workspace <path>\|default` | Select the working directory |
| `/account` | Show the current isolated Codex account |
| `/login` | Start device-code login for an additional user |
| `/restart` | Owner-only safe deferred restart |
| `/update` | Owner-only: git pull the latest push, then restart the same way `/restart` does |

## Multi-account setup

The owner always uses the existing default `~/.codex` unchanged. To grant
another person access, add their numeric Telegram user ID to `whitelist.txt`
(comma or newline separated). The file is re-read for every update; no restart
is needed.

On their first message, the bot sends an official ChatGPT device-code URL and
one-time code. After browser authorization, Codex stores credentials directly
under `accounts/<telegram_id>/`; credentials are never pasted into Telegram.

Each additional user gets independent:

- OAuth credentials and subscription limits;
- `codex app-server` process;
- sessions and usage;
- model, sandbox and workspace;
- active-turn and stop state.

Removing an ID from `whitelist.txt` blocks new messages immediately. Existing
credentials remain on disk; remove `accounts/<id>/` separately only if you
intend to revoke and delete that local account state.

## Product boundary

This repository is only the standalone Telegram Bot API frontend for Codex.
Jarvis, the Telethon userbot module, Telegram-account actions and triggers live
in the separate `/home/mishin/codex-jarvis` product. The two applications do
not import one another and have independent sessions and runtime processes.

The only shared piece is the small local command queue used to relay actions;
it is transport infrastructure, not part of this bot's model or Telegram
interface.

## Runtime files and backup

These are intentionally ignored by git:

| Path | Contents |
|---|---|
| `.env` | Bot token and deployment configuration |
| `state.json` | Per-user thread IDs, preferences and usage snapshots |
| `whitelist.txt` | Allowed Telegram user IDs |
| `accounts/` | Additional users' complete isolated `CODEX_HOME` data |
| `restart.request` | Short-lived safe-restart signal |

To back up the deployment, stop the service and copy those paths. Treat
`.env` and `accounts/` as secrets.

## Operations

```bash
# Logs and status
systemctl status codex-telegram-bot
journalctl -u codex-telegram-bot -f

# Validate configuration
./doctor.sh

# Restart only after all active turns finish
./request-restart

# Update and schedule a safe deferred restart in one step
./update.sh
```

The owner can run `/update` in Telegram instead; it performs the same update and
schedules the restart through the existing safe watcher. As a shell-access
fallback, or when you do not want to wait for the bot to process a command, run:

```bash
git pull --ff-only
./request-restart
```

The restart watcher sends one status message and edits that same message to
“ready” after systemd starts the new process.

## Troubleshooting

- **Bot is silent:** run `./doctor.sh`, then inspect the journal. Verify the
  BotFather token and that no second process is polling the same bot token.
- **Owner is unauthorized:** run `codex login status` as the service user.
- **Additional user cannot run Codex:** use `/account`, then `/login`. Check
  that `accounts/<id>/` is writable by the service user.
- **Need to send several messages at once:** forward or type them in quick
  succession; the bridge waits briefly and combines them into one prompt.
- **Sandbox warning about bubblewrap:** Codex can use its bundled bubblewrap;
  installing the distribution `bubblewrap` package removes the warning.
- **Rate limit reached:** `/usage` reports whether the five-hour or weekly
  window is exhausted and displays the local reset time.

## Security notes

`danger-full-access` lets Codex act as the service's Linux user. Only whitelist
people you trust; prefer `workspace-write` for additional users if they do not
need host-wide access. `NoNewPrivileges=true` in the unit prevents privilege
escalation but does not restrict access to files already readable by that Unix
user. Strong filesystem isolation requires separate Unix users or containers.

## License

MIT, see [LICENSE](LICENSE). `telegram_format.py` is adapted from
[hermes-agent](https://github.com/NousResearch/hermes-agent) under the MIT
License.
