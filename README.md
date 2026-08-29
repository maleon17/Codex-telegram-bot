# Codex Telegram Bot

Standalone, single-owner Telegram frontend for continuing Codex threads.

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
