# Codex Telegram Bot

A persistent, multi-account Telegram frontend for [OpenAI Codex](https://developers.openai.com/codex/).
It uses your ChatGPT/Codex subscription through the installed Codex CLI—no
metered OpenAI API key is required.

Unlike wrappers that launch `codex exec` for every message, this bridge keeps
one `codex app-server` alive per Telegram user. Threads survive restarts,
events stream into Telegram native drafts, and a follow-up can be injected
into a running turn with App Server's `turn/steer`.

## Features

- Native Telegram draft animation with live reasoning/tool progress.
- Persistent Codex threads: `/new`, `/sessions`, `/resume`.
- Mid-turn input: select `/steer`, then send a follow-up message.
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
   CODEX_TELEGRAM_INSTANCE_ID=andrey
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
| `/steer` | Pause the native draft and free the input field for a mid-turn follow-up |
| `/compact` | Compact the current thread context |
| `/usage` | Session tokens, context and account rate limits |
| `/model [id]` | Show available models or select one by its real ID |
| `/effort [level]` | Show or select the reasoning power supported by the current model |
| `/mode read-only\|workspace-write\|full` | Select the sandbox policy |
| `/workspace <path>\|default` | Select the working directory |
| `/account` | Show the current isolated Codex account |
| `/login` | Start device-code login for an additional user |
| `/restart` | Owner-only safe deferred restart |

### Why `/steer` exists

Telegram's native `sendMessageDraft` occupies the client's composer, so the
client cannot simultaneously display that animation and let you type. Select
`/steer` from the command menu: the bot dismisses and pauses the draft for the
rest of that turn; your next ordinary message is appended to the active turn.

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
- active-turn, steer and stop state.

Removing an ID from `whitelist.txt` blocks new messages immediately. Existing
credentials remain on disk; remove `accounts/<id>/` separately only if you
intend to revoke and delete that local account state.

## Shared Telegram tools and triggers

Claude and Codex intentionally use one trigger engine. The engine runs in the
Telethon userbot (`ClaudeAsk`), where it can see incoming messages in your real
Telegram chats. Both assistants reach it through the same `telegram_actions`
MCP server and the userbot's command queue; there is no second Codex-only
trigger database to drift out of sync.

The Codex App Server must have that MCP server in the Linux user's
`~/.codex/config.toml` (the existing Claude deployment already provides it):

See [codex-config.toml.example](codex-config.toml.example) for a copyable
configuration block.

```toml
[mcp_servers.telegram_actions]
command = "/absolute/path/to/mcp-venv/bin/python3"
args = ["/absolute/path/to/Codex-telegram-bot/telegram_actions_mcp.py"]
startup_timeout_sec = 20.0
tool_timeout_sec = 40.0
default_tools_approval_mode = "auto"
```

Create the optional MCP environment with `python3 -m venv /absolute/path/to/mcp-venv`
and `.../bin/pip install -r requirements-mcp.txt`. The regular bot does not
need this package unless Telegram actions/triggers are enabled.

Keep the remote userbot and its `cmd_queue.py` running. Codex supplies the
current Telegram chat through process-local `CODEX_TELEGRAM_CHAT_ID` and
`CODEX_TELEGRAM_INSTANCE_ID` variables, so a multi-account Codex user cannot
silently inherit the owner's origin chat. The shared MCP exposes
`register_trigger`, `edit_trigger`, `remove_trigger` and `list_triggers` along
with the other Telegram actions. Ask Codex to create a rule in plain language;
it will call the same backend Claude uses.

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

# Update
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
- **Draft blocks typing:** select `/steer`; this is a Telegram native-draft
  limitation, not an App Server limitation.
- **No code formatting in an old draft screenshot:** current versions balance
  and escape MarkdownV2 pre blocks. Check the journal for fresh Bot API 400s.
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
