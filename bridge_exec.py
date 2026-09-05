#!/usr/bin/env python3
"""Drive the live codex-telegram-bot ("Уголовный Codex") without going
through Telegram at all for the outbound leg.

A bot can never see its own outgoing messages via getUpdates -- Telegram
simply does not deliver them back to the sender, confirmed live
2026-09-01, not something fixable at the code level (and no other identity
can inject into a private 1:1 chat either -- it only ever has its two real
participants). Since bot.py is our own code, the real fix is to skip
Telegram for this leg entirely: this script writes a request file that
bot.py's external_request_watcher() polls and dispatches it into the
dedicated persistent delegate tenant. Real formatting, real /resume, and
real mid-turn steering (the owner typing into the same chat while this runs
still gets genuinely steered into the delegate process) all come from the
actual product for free -- only the INPUT side bypasses Telegram now.

Usage:
    bridge_exec.py [--workspace PATH] [--resume ID] [--chat-id ID]
                    [--timeout SECONDS] [--model KEY] [--effort LEVEL]
                    [--env KEY=VALUE ...]
                    PROMPT...

Every call through this script is a delegated turn by definition, so the
final answer's footer always notes it -- the OWNER's own thread from
before this call touched anything (so they can return to whatever they
were doing) plus a `/resume` hint for the thread this delegated task
itself just used, in case they want to continue THAT specific one
instead. Nothing needs to be passed in for this; bot.py's watcher
captures the prior thread itself.

Prints the final answer to stdout and exits 0, or prints an error to
stderr and exits 1 on timeout/failure.
"""
import argparse
import fcntl
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OWNER_ID = 8480261623


def load_dotenv():
    env = {}
    try:
        with open(os.path.join(ROOT, ".env")) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip()
    except FileNotFoundError:
        pass
    return env


def external_request_path():
    return os.environ.get(
        "CODEX_BOT_EXTERNAL_REQUEST_FILE", os.path.join(ROOT, "external_request.json"),
    )


def last_turn_path(chat_id, delegated=False):
    # Mirrors bot.py's write_last_turn(). Keep the ordinary signal path
    # stable, but never let a delegate poll the owner's signal file.
    state_file = os.environ.get(
        "CODEX_BOT_STATE_FILE", os.path.join(ROOT, "state.json"),
    )
    state_instance_name = os.path.splitext(os.path.basename(state_file))[0]
    filename = (
        f"last_turn_{state_instance_name}_delegate_{chat_id}.json"
        if delegated else f"last_turn_{chat_id}.json"
    )
    return os.path.join(os.path.dirname(os.path.abspath(state_file)), filename)


def poll_until_done(chat_id, baseline_ts, timeout_s, poll_interval=2):
    """Poll the last-turn signal FILE bot.py writes on every completed
    turn, not Telegram's getUpdates -- bot.py already owns that bot
    token's getUpdates stream exclusively (only one consumer ever sees a
    given update), so a second independent poller there would just starve
    forever. See bot.py's write_last_turn()."""
    path = last_turn_path(chat_id, delegated=True)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = None
        if data and data.get("ts", 0) > baseline_ts:
            return data
        time.sleep(poll_interval)
    raise TimeoutError(f'No completed turn signalled via {path} within {timeout_s}s.')


def delegate_lock_path(request_path):
    return request_path + ".delegate.lock"


def parse_env_assignments(assignments):
    result = {}
    for assignment in assignments:
        key, separator, value = assignment.partition("=")
        if not separator or not key:
            raise ValueError("--env expects KEY=VALUE")
        result[key] = value
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", help="switch workspace before the prompt")
    parser.add_argument("--resume", help="resume this thread id before the prompt")
    parser.add_argument("--chat-id", type=int, help="override the target chat (default: OWNER_ID)")
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--model", help="select a model by key, id or display name")
    parser.add_argument("--effort", help="select a reasoning effort supported by the model")
    parser.add_argument("--env", action="append", default=[], metavar="KEY=VALUE",
                        help="pass one environment variable to a fresh delegated turn; repeatable")
    parser.add_argument("prompt", nargs="+")
    args = parser.parse_args()

    if args.resume and args.env:
        print("--env нельзя использовать вместе с --resume.", file=sys.stderr)
        sys.exit(1)
    try:
        requested_env = parse_env_assignments(args.env)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)

    env = load_dotenv()
    chat_id = args.chat_id or int(env.get("OWNER_ID") or os.environ.get("OWNER_ID") or DEFAULT_OWNER_ID)

    request_path = external_request_path()
    # One real chat has one stable delegate tenant. Serialize bridge_exec
    # callers so a second process cannot overwrite the request or consume
    # the first process's delegate signal while that tenant is busy.
    with open(delegate_lock_path(request_path), "a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if os.path.exists(request_path):
            print(f"{request_path} already has an unconsumed request -- "
                  f"bot.py hasn't picked it up yet, or it's stuck. Not overwriting.",
                  file=sys.stderr)
            sys.exit(1)

        # Baseline BEFORE writing the request -- only a delegate signal file
        # written strictly after this counts as ours, not a stale owner turn
        # or a previous delegate completion.
        try:
            with open(last_turn_path(chat_id, delegated=True), encoding="utf-8") as f:
                baseline_ts = json.load(f).get("ts", 0)
        except (FileNotFoundError, json.JSONDecodeError):
            baseline_ts = 0

        request = {"chat_id": chat_id, "text": " ".join(args.prompt)}
        if args.workspace:
            request["workspace"] = args.workspace
        if args.resume:
            request["resume_thread_id"] = args.resume
        if args.model is not None:
            request["model"] = args.model
        if args.effort is not None:
            request["effort"] = args.effort
        if requested_env:
            request["env"] = requested_env
        # Every request through this file channel is a delegated one by
        # definition (a human never writes this file). The server owns the
        # delegate-only state and footer; this file carries only turn inputs
        # and explicit per-turn overrides.

        tmp = request_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(request, f)
        os.replace(tmp, request_path)

        try:
            result = poll_until_done(chat_id, baseline_ts, args.timeout)
        except TimeoutError as exc:
            print(str(exc), file=sys.stderr)
            sys.exit(1)

    if result.get("ok") is False:
        print(result.get("text") or "Делегированная задача завершилась ошибкой.", file=sys.stderr)
        sys.exit(1)
    print(result.get("text", ""))


if __name__ == "__main__":
    main()
