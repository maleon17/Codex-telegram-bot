# Codex Telegram Bot

Standalone, multi-account Telegram frontend for continuing Codex threads.

`bot.py` owns Telegram polling, native draft progress, commands, process
lifecycle, and state persistence. The project has no source-level dependency
on the Claude bridge.

The bot keeps one `codex app-server` child alive for its whole lifetime. A
Telegram message starts a turn; another ordinary message received while that
turn is active is appended with `turn/steer`. `/stop` uses `turn/interrupt`.
`/restart` and `./request-restart` only schedule a restart: the watcher exits
after the active turn and final Telegram delivery, never during an answer.

Runtime state is stored in `state.json` and is ignored by git.

Install the example systemd unit after supplying the real Telegram token and
owner ID. Runtime state is stored in `state.json` and ignored by git.

## Multi-account isolation

The owner keeps the existing default `~/.codex` account and sessions. Add
other numeric Telegram user IDs to `whitelist.txt` (comma or newline
separated). Each additional user gets:

- an independent `accounts/<telegram_id>/` `CODEX_HOME`;
- a separate persistent `codex app-server` process;
- separate OAuth credentials, sessions, model, sandbox, workspace and usage;
- independent busy/steer/interrupt state.

On the first message, or via `/login`, the bot returns an official ChatGPT
device-code URL and one-time code. Credentials never pass through Telegram;
Codex writes them directly into that user's isolated `CODEX_HOME` after the
browser flow completes. `/account` reports the current login.
