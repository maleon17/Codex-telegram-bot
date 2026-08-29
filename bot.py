#!/usr/bin/env python3
"""Single-owner Telegram frontend for persistent Codex CLI conversations."""

import json
import os
import signal
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from app_server import AppServerClient, AppServerError
from telegram_format import escape_mdv2, strip_mdv2


EDIT_THROTTLE_S = 1.3
MAX_MESSAGE_LEN = 4000
RICH_MAX_CHARS = 30000
HTTP_TIMEOUT_S = 20
IDLE_TIMEOUT_S = 300
TOTAL_TIMEOUT_S = 1800
THINKING_SPINNER_FRAMES = "⠋⠙⠚⠞⠖⠦⠴⠲⠳⠓"
COMMANDS = [
    ("new", "Начать новую Codex-сессию"),
    ("sessions", "Список последних сессий"),
    ("resume", "Продолжить сессию по id"),
    ("status", "Сессия, модель, sandbox и workspace"),
    ("stop", "Прервать текущий запрос"),
    ("usage", "Токены последнего запроса"),
    ("model", "Выбрать модель Codex"),
    ("mode", "Sandbox: read-only/workspace-write/full"),
    ("workspace", "Рабочая директория"),
    ("restart", "Перезапустить Codex-бота"),
]


def log(message):
    print(message, file=sys.stderr, flush=True)


def require_env(name):
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"codex-telegram-bot: required environment variable {name} is not set")
    return value


BOT_TOKEN = require_env("TELEGRAM_BOT_TOKEN")
try:
    OWNER_ID = int(require_env("OWNER_ID"))
except ValueError as exc:
    raise SystemExit("codex-telegram-bot: OWNER_ID must be an integer") from exc

CODEX_CWD = os.environ.get("CODEX_CWD", "/home/mishin")
CODEX_SANDBOX = os.environ.get("CODEX_SANDBOX", "danger-full-access")
STATE_FILE = Path(
    os.environ.get("CODEX_BOT_STATE_FILE", Path(__file__).with_name("state.json"))
).expanduser()
RESTART_SIGNAL_FILE = Path(os.environ.get(
    "CODEX_BOT_RESTART_FILE", Path(__file__).with_name("restart.request")
)).expanduser()
API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

state_lock = threading.Lock()
process_lock = threading.RLock()
telegram_lock = threading.Lock()
rate_limit_until = 0.0
busy = False
app_server = None
loaded_thread_id = None
loaded_server_pid = None
active_view = None
active_done = None
active_turn_id = None
active_thread_id = None
active_error = None
active_stopped = False
active_last_event_at = None
active_media_paths = []
restart_draining = False


def _rate_limited(result):
    return result.get("error_code") == 429


def tg_call(method, params=None, timeout=HTTP_TIMEOUT_S):
    """Call Telegram once; never retry, or call at all during a known 429 ban.

    `telegram_lock` guards only the tiny rate_limit_until read/write -- NOT
    the network call itself. main()'s own getUpdates is a 30-40s long poll
    that also goes through this function; holding a lock across the actual
    HTTP request would serialize every other thread's sendMessage/edit
    behind that poll for its entire duration, which is exactly what
    happened here (confirmed live via py-spy: run_turn's first live-progress
    edit sat blocked on this lock while the main loop held it inside
    urlopen()). The lock only needs to protect the shared counter.
    """
    global rate_limit_until
    with telegram_lock:
        now = time.monotonic()
        if now < rate_limit_until:
            remaining = max(1, int(rate_limit_until - now + 0.999))
            result = {
                "ok": False,
                "error_code": 429,
                "description": "locally suppressed during Telegram rate limit",
                "parameters": {"retry_after": remaining},
            }
            log(f"Telegram {method} suppressed: rate-limited for ~{remaining} more seconds")
            return result

    request = urllib.request.Request(
        f"{API_BASE}/{method}",
        data=json.dumps(params or {}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            result = json.loads(exc.read().decode("utf-8"))
        except Exception:
            result = {"ok": False, "error": str(exc)}
    except Exception as exc:
        result = {"ok": False, "error": str(exc)}

    if not result.get("ok"):
        if _rate_limited(result):
            retry_after = (result.get("parameters") or {}).get("retry_after")
            try:
                delay = max(1.0, float(retry_after))
            except (TypeError, ValueError):
                delay = 1.0
            with telegram_lock:
                rate_limit_until = max(rate_limit_until, time.monotonic() + delay)
            log(
                f"Telegram {method} not ok: 429 Too Many Requests; "
                f"bot rate-limited for {retry_after} seconds: {result}"
            )
        else:
            log(f"Telegram {method} not ok: {result}")
    return result


def download_telegram_file(file_id, suggested_name="image.jpg"):
    """Download an owner-sent Telegram file for App Server localImage input."""
    result = tg_call("getFile", {"file_id": file_id})
    remote_path = (result.get("result") or {}).get("file_path") if result.get("ok") else None
    if not remote_path:
        raise RuntimeError("Telegram не вернул путь к файлу")
    suffix = Path(suggested_name).suffix or Path(remote_path).suffix or ".jpg"
    media_dir = Path(tempfile.gettempdir()) / "codex-telegram-bot-media"
    media_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, local_path = tempfile.mkstemp(prefix="upload-", suffix=suffix, dir=media_dir)
    try:
        with urllib.request.urlopen(
            f"https://api.telegram.org/file/bot{BOT_TOKEN}/{remote_path}",
            timeout=HTTP_TIMEOUT_S,
        ) as response, os.fdopen(fd, "wb") as handle:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
        return local_path
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(local_path)
        except FileNotFoundError:
            pass
        raise


def message_inputs(message):
    """Build App Server inputs from Telegram text/caption and image media."""
    text = message.get("text") or message.get("caption") or ""
    inputs = []
    paths = []
    if isinstance(text, str) and text.strip():
        inputs.append({"type": "text", "text": text.strip()})
    photo = message.get("photo")
    if isinstance(photo, list) and photo:
        path = download_telegram_file(photo[-1]["file_id"], "photo.jpg")
        paths.append(path)
        inputs.append({"type": "localImage", "path": path})
    document = message.get("document") or {}
    if str(document.get("mime_type", "")).startswith("image/") and document.get("file_id"):
        path = download_telegram_file(document["file_id"], document.get("file_name") or "image")
        paths.append(path)
        inputs.append({"type": "localImage", "path": path})
    if paths and not any(item.get("type") == "text" for item in inputs):
        inputs.insert(0, {"type": "text", "text": "Посмотри на это изображение и ответь по контексту."})
    return inputs, paths


def load_state():
    if not STATE_FILE.exists():
        return {"thread_id": None, "model": None, "sandbox": CODEX_SANDBOX,
                "workspace": CODEX_CWD, "last_usage": None,
                "session_usage": None, "context_window": None,
                "restart_completed_chat_id": None}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        thread_id = data.get("thread_id") if isinstance(data, dict) else None
        return {"thread_id": thread_id if isinstance(thread_id, str) else None,
                "model": data.get("model") if isinstance(data.get("model"), str) else None,
                "sandbox": data.get("sandbox") if data.get("sandbox") in
                ("read-only", "workspace-write", "danger-full-access") else CODEX_SANDBOX,
                "workspace": data.get("workspace") if isinstance(data.get("workspace"), str) else CODEX_CWD,
                "last_usage": data.get("last_usage") if isinstance(data.get("last_usage"), dict) else None,
                "session_usage": data.get("session_usage")
                if isinstance(data.get("session_usage"), dict) else None,
                "context_window": data.get("context_window")
                if isinstance(data.get("context_window"), int) else None,
                "restart_completed_chat_id": data.get("restart_completed_chat_id")
                if isinstance(data.get("restart_completed_chat_id"), int) else None}
    except Exception as exc:
        raise SystemExit(f"codex-telegram-bot: cannot read state file {STATE_FILE}: {exc}") from exc


state = load_state()


def update_state(**values):
    with state_lock:
        state.update(values)
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=STATE_FILE.name + ".", dir=STATE_FILE.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, STATE_FILE)
        except Exception:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise


def save_thread_id(thread_id):
    update_state(thread_id=thread_id)


def compact(value, limit=1000):
    if isinstance(value, str):
        text = value.strip()
    else:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def mdv2_code_block(value):
    """Render safe MarkdownV2 pre content (backslash/backtick are special)."""
    content = str(value).replace("\\", "\\\\").replace("`", "\\`")
    return f"```\n{content}\n```"


def truncate_mdv2(text, limit=MAX_MESSAGE_LEN):
    """Truncate without ever leaving a Telegram MarkdownV2 pre block open."""
    if len(text) <= limit:
        return text
    clipped = text[:limit]
    if clipped.count("```") % 2:
        clipped = clipped[: max(0, limit - 4)].rstrip() + "\n```"
    return clipped


def item_label_and_blocks(item):
    item_type = item.get("type", "unknown")
    if item_type == "agent_message":
        return "💬 Ответ", item.get("text", ""), []
    if item_type == "reasoning":
        return "🧠 Размышление", item.get("text", item.get("summary", "")), []
    if item_type == "command_execution":
        command = item.get("command", "")
        exit_code = item.get("exit_code")
        output = item.get("aggregated_output")
        results = []
        if output not in (None, ""):
            results.append(("📤 Результат", str(output)))
        if exit_code is not None:
            results.append(("🏁 Код завершения", str(exit_code)))
        return "⚙️ Выполняю", command, results
    if item_type == "file_change":
        content = item.get("path", item.get("changes", item))
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        return "📝 Изменение файла", content, []
    return f"🔧 {item_type}", json.dumps(item, ensure_ascii=False), []


def normalize_app_item(item):
    """Convert App Server camelCase thread items to the existing renderer shape."""
    if not isinstance(item, dict):
        return {"type": "unknown", "value": item}
    result = dict(item)
    type_map = {
        "agentMessage": "agent_message",
        "commandExecution": "command_execution",
        "fileChange": "file_change",
        "mcpToolCall": "mcp_tool_call",
        "dynamicToolCall": "dynamic_tool_call",
        "webSearch": "web_search",
        "imageView": "image_view",
        "collabAgentToolCall": "collab_agent_tool_call",
    }
    result["type"] = type_map.get(result.get("type"), result.get("type", "unknown"))
    if result["type"] == "reasoning":
        summary = result.get("summary") or result.get("content") or []
        result["text"] = "\n".join(summary) if isinstance(summary, list) else str(summary)
    result["aggregated_output"] = result.get("aggregatedOutput")
    result["exit_code"] = result.get("exitCode")
    if result["type"] == "file_change":
        result["changes"] = result.get("changes", [])
    return result


def render_process_item(item):
    label, content, results = item_label_and_blocks(item)
    lines = [f"{label}:", mdv2_code_block(content)]
    for result_label, result_content in results:
        lines.extend((f"{result_label}:", mdv2_code_block(result_content)))
    return "\n".join(lines)


def format_usage(usage):
    if not isinstance(usage, dict):
        return compact(usage, 300)
    parts = []
    for key, label in (("input_tokens", "in"), ("cached_input_tokens", "cached"),
                       ("output_tokens", "out"),
                       ("reasoning_output_tokens", "reasoning")):
        if key in usage:
            parts.append(f"{label}: {usage[key]}")
    return ", ".join(parts) if parts else compact(usage, 300)


def send_plain(chat_id, text):
    text = text or "(пусто)"
    last = None
    while text:
        part, text = text[:MAX_MESSAGE_LEN], text[MAX_MESSAGE_LEN:]
        last = tg_call("sendMessage", {"chat_id": chat_id, "text": part})
        if _rate_limited(last):
            break
    return last


def edit_plain(chat_id, message_id, text):
    return tg_call("editMessageText", {
        "chat_id": chat_id, "message_id": message_id, "text": text[:MAX_MESSAGE_LEN]
    })


def send_rich(chat_id, markdown_text):
    text = markdown_text[:RICH_MAX_CHARS]
    result = tg_call("sendRichMessage", {
        "chat_id": chat_id, "rich_message": {"markdown": text}
    })
    if not result.get("ok") and not _rate_limited(result):
        return send_plain(chat_id, markdown_text)
    return result


def edit_rich(chat_id, message_id, markdown_text):
    text = markdown_text[:RICH_MAX_CHARS]
    result = tg_call("editMessageText", {
        "chat_id": chat_id, "message_id": message_id,
        "rich_message": {"markdown": text},
    })
    if not result.get("ok") and not _rate_limited(result):
        description = str(result.get("description", "")).lower()
        if "not modified" not in description:
            return edit_plain(chat_id, message_id, markdown_text)
    return result


class TurnView:
    def __init__(self, chat_id):
        self.chat_id = chat_id
        self.items = []
        self.process_items = []
        self.draft_thought = None
        self.draft_tool = None
        self.draft_id = int(time.time_ns() % 2147483647) or 1
        self.last_edit_at = 0.0
        self.usage = None
        self.completed = False
        self.spinner_i = 0

    def add_event(self, event):
        event_type = event.get("type", "unknown")
        if event_type == "thread.started":
            thread_id = event.get("thread_id")
            if thread_id:
                save_thread_id(thread_id)
        elif event_type == "turn.started":
            pass
        elif event_type in ("item.started", "item.updated"):
            item = event.get("item")
            if isinstance(item, dict):
                item_type = item.get("type")
                if item_type in ("agent_message", "reasoning"):
                    text = item.get("text", item.get("summary", ""))
                    if text:
                        self.draft_thought = text
                else:
                    self.draft_tool = item
        elif event_type == "item.completed":
            item = event.get("item")
            if isinstance(item, dict):
                self.items.append(item)
                item_type = item.get("type")
                if item_type in ("agent_message", "reasoning"):
                    self.draft_thought = item.get("text", item.get("summary", ""))
                else:
                    self.draft_tool = item
                self.process_items.append(item)
        elif event_type == "turn.completed":
            self.completed = True
            self.usage = event.get("usage")
            if isinstance(self.usage, dict):
                update_state(last_usage=self.usage)

    def live_text(self):
        lines = []
        if self.draft_thought:
            lines.append(escape_mdv2(str(self.draft_thought)))
        if self.draft_tool:
            label, content, results = item_label_and_blocks(self.draft_tool)
            lines.extend((escape_mdv2(f"{label}:"), mdv2_code_block(content)))
            for result_label, result_content in results:
                lines.extend((escape_mdv2(f"{result_label}:"),
                              mdv2_code_block(result_content)))
        body = "\n".join(lines) if lines else "Думаю"
        return body  # Telegram animates native drafts itself

    def _send_live_draft(self, text):
        result = tg_call("sendMessageDraft", {
            "chat_id": self.chat_id, "draft_id": self.draft_id,
            "text": text, "parse_mode": "MarkdownV2",
        })
        if not result.get("ok") and not _rate_limited(result):
            tg_call("sendMessageDraft", {
                "chat_id": self.chat_id, "draft_id": self.draft_id,
                "text": strip_mdv2(text).replace("```", ""),
            })

    def flush(self, force=False):
        now = time.monotonic()
        if not force and now - self.last_edit_at < EDIT_THROTTLE_S:
            return
        self.last_edit_at = now
        self._send_live_draft(truncate_mdv2(self.live_text()))

    def deliver(self, stopped=False, error=None):
        final_index = next(
            (i for i in range(len(self.items) - 1, -1, -1)
             if self.items[i].get("type") == "agent_message"), None
        )
        final_text = self.items[final_index].get("text", "") if final_index is not None else ""

        process_items = list(self.process_items)
        if final_index is not None:
            final_item = self.items[final_index]
            for i in range(len(process_items) - 1, -1, -1):
                if process_items[i] is final_item:
                    del process_items[i]
                    break

        if stopped:
            answer = "⏹ Выполнение остановлено."
        elif error:
            answer = f"⚠️ Ошибка Codex: {error}"
        else:
            answer = final_text or "(нет ответа — смотри процесс выше)"
        if self.usage is not None:
            answer += f"\n\nТокены: {format_usage(self.usage)}"

        if process_items:
            process_steps = [render_process_item(item) for item in process_items]
            closing_reserve = 100
            visible = []
            used = 0
            for step in reversed(process_steps):
                cost = len(step) + 1
                if visible and used + cost > RICH_MAX_CHARS - closing_reserve:
                    break
                visible.append(step[: RICH_MAX_CHARS - closing_reserve])
                used += cost
            visible.reverse()
            hidden = len(process_steps) - len(visible)
            if hidden:
                visible.insert(0, f"…и ещё {hidden} шагов выше…")
            body = "\n".join(visible)
            rich = f"<details><summary>🔧 Процесс ({len(process_steps)})</summary>\n{body}\n</details>"
            send_rich(self.chat_id, rich)
            send_rich(self.chat_id, answer)
        else:
            send_rich(self.chat_id, answer)


def get_app_server():
    global app_server
    with process_lock:
        if app_server is None:
            app_server = AppServerClient(handle_app_notification, log)
        return app_server


def _usage_breakdown(usage):
    return {
        "input_tokens": usage.get("inputTokens", 0),
        "cached_input_tokens": usage.get("cachedInputTokens", 0),
        "cache_write_input_tokens": usage.get("cacheWriteInputTokens", 0),
        "output_tokens": usage.get("outputTokens", 0),
        "reasoning_output_tokens": usage.get("reasoningOutputTokens", 0),
        "total_tokens": usage.get("totalTokens", 0),
    }


def _usage_for_renderer(token_usage):
    return _usage_breakdown((token_usage or {}).get("last") or {})


def handle_app_notification(method, params):
    global active_turn_id, active_thread_id, active_error, active_last_event_at
    with process_lock:
        view = active_view
        done = active_done
        if view is None:
            return
        event_turn_id = params.get("turnId") or (params.get("turn") or {}).get("id")
        if active_turn_id and event_turn_id and event_turn_id != active_turn_id:
            return
        active_last_event_at = time.monotonic()
        if event_turn_id:
            active_turn_id = event_turn_id
        if params.get("threadId"):
            active_thread_id = params["threadId"]

    force = False
    if method in ("item/started", "item/completed"):
        item = normalize_app_item(params.get("item"))
        event_type = "item.started" if method.endswith("started") else "item.completed"
        view.add_event({"type": event_type, "item": item})
    elif method == "item/agentMessage/delta":
        view.draft_thought = (view.draft_thought or "") + str(params.get("delta", ""))
    elif method == "thread/tokenUsage/updated":
        token_usage = params.get("tokenUsage") or {}
        view.usage = _usage_for_renderer(token_usage)
        update_state(
            last_usage=view.usage,
            session_usage=_usage_breakdown(token_usage.get("total") or {}),
            context_window=token_usage.get("modelContextWindow"),
        )
    elif method == "error" and not params.get("willRetry"):
        with process_lock:
            active_error = compact(params.get("error", "неизвестная ошибка"), 1000)
    elif method == "turn/completed":
        turn = params.get("turn") or {}
        if turn.get("error"):
            with process_lock:
                active_error = compact(turn["error"], 1000)
        view.completed = True
        force = True
        if done is not None:
            done.set()
    view.flush(force=force)


def _thread_params(thread_id=None):
    with state_lock:
        snapshot = dict(state)
    params = {
        "cwd": snapshot.get("workspace") or CODEX_CWD,
        "sandbox": snapshot.get("sandbox") or CODEX_SANDBOX,
        "approvalPolicy": "never",
    }
    if snapshot.get("model"):
        params["model"] = snapshot["model"]
    if thread_id:
        params["threadId"] = thread_id
    return params


def ensure_thread(client, requested_thread_id):
    global loaded_thread_id, loaded_server_pid
    process_pid = client.process.pid if client.process is not None else None
    with process_lock:
        if (requested_thread_id and requested_thread_id == loaded_thread_id
                and process_pid == loaded_server_pid):
            return requested_thread_id
    method = "thread/resume" if requested_thread_id else "thread/start"
    try:
        result = client.request(method, _thread_params(requested_thread_id), timeout=60)
    except AppServerError:
        if not requested_thread_id:
            raise
        log(f"Could not resume thread {requested_thread_id}; starting a new thread")
        result = client.request("thread/start", _thread_params(), timeout=60)
    thread_id = ((result or {}).get("thread") or {}).get("id")
    if not thread_id:
        raise AppServerError(f"{method} returned no thread id")
    with process_lock:
        loaded_thread_id = thread_id
        loaded_server_pid = process_pid
    save_thread_id(thread_id)
    return thread_id


def sandbox_policy(name, workspace):
    if name == "read-only":
        return {"type": "readOnly", "networkAccess": False}
    if name == "workspace-write":
        return {"type": "workspaceWrite", "writableRoots": [workspace],
                "networkAccess": True}
    return {"type": "dangerFullAccess"}


def run_turn(chat_id, inputs, thread_id, media_paths=None):
    global busy, active_view, active_done, active_turn_id, active_thread_id
    global active_error, active_stopped, active_last_event_at, active_media_paths
    view = TurnView(chat_id)
    done = threading.Event()
    error = None
    stopped = False
    try:
        client = get_app_server()
        client.start_if_needed()
        server_thread_id = ensure_thread(client, thread_id)
        with state_lock:
            snapshot = dict(state)
        with process_lock:
            active_view = view
            active_done = done
            active_turn_id = None
            active_thread_id = server_thread_id
            active_error = None
            active_stopped = False
            active_last_event_at = time.monotonic()
            active_media_paths = list(media_paths or [])
        view.flush(force=True)
        params = {
            "threadId": server_thread_id,
            "input": inputs,
            "cwd": snapshot.get("workspace") or CODEX_CWD,
            "approvalPolicy": "never",
            "sandboxPolicy": sandbox_policy(
                snapshot.get("sandbox") or CODEX_SANDBOX,
                snapshot.get("workspace") or CODEX_CWD,
            ),
        }
        if snapshot.get("model"):
            params["model"] = snapshot["model"]
        result = client.request("turn/start", params, timeout=60)
        turn_id = ((result or {}).get("turn") or {}).get("id")
        with process_lock:
            active_turn_id = active_turn_id or turn_id
        started_at = time.monotonic()
        while not done.wait(1):
            with process_lock:
                last_event = active_last_event_at or started_at
                process = client.process
            now = time.monotonic()
            if process is None or process.poll() is not None:
                error = "постоянный процесс Codex неожиданно завершился"
                break
            if now - last_event > IDLE_TIMEOUT_S or now - started_at > TOTAL_TIMEOUT_S:
                error = "Codex остановлен по таймауту"
                stop_current_process()
                break
        with process_lock:
            error = error or active_error
            stopped = active_stopped
        view.deliver(stopped=stopped, error=error)
    except Exception as exc:
        log(f"Codex worker failed: {exc}")
        view.deliver(error=compact(str(exc), 1000))
    finally:
        with process_lock:
            active_view = None
            active_done = None
            active_turn_id = None
            active_thread_id = None
            active_error = None
            active_stopped = False
            active_last_event_at = None
            paths = active_media_paths
            active_media_paths = []
            busy = False
        for path in paths:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass


def session_files():
    root = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "sessions"
    return sorted(root.glob("**/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)


def session_info(path):
    sid, preview = path.stem, ""
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                obj = json.loads(line)
                payload = obj.get("payload", {})
                if obj.get("type") == "session_meta":
                    sid = payload.get("id", sid)
                elif obj.get("type") == "response_item" and payload.get("role") == "user":
                    texts = [part.get("text", "") for part in payload.get("content", [])
                             if part.get("type") == "input_text"]
                    candidate = " ".join(texts).strip()
                    if candidate and not candidate.startswith("<recommended_plugins>"):
                        preview = compact(candidate, 55)
    except Exception:
        pass
    return sid, preview


def session_message_count(thread_id):
    if not thread_id:
        return None
    for path in session_files():
        sid, _ = session_info(path)
        if sid != thread_id:
            continue
        count = 0
        try:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    obj = json.loads(line)
                    payload = obj.get("payload") or {}
                    if obj.get("type") != "response_item" or payload.get("role") != "user":
                        continue
                    text = " ".join(
                        part.get("text", "") for part in payload.get("content", [])
                        if part.get("type") == "input_text"
                    ).strip()
                    if text and not text.startswith((
                        "<recommended_plugins>", "<environment_context>",
                    )):
                        count += 1
            return count
        except Exception:
            return None
    return None


def fmt_number(value):
    try:
        return f"{int(value):,}".replace(",", " ")
    except (TypeError, ValueError):
        return "—"


def format_reset_time(timestamp):
    if not timestamp:
        return "—"
    try:
        reset = datetime.fromtimestamp(int(timestamp)).astimezone()
        remaining = max(0, int(timestamp) - int(time.time()))
        if remaining < 3600:
            relative = f"через {max(1, remaining // 60)} мин"
        elif remaining < 86400:
            relative = f"через {remaining // 3600} ч {remaining % 3600 // 60} мин"
        else:
            relative = f"через {remaining // 86400} д {remaining % 86400 // 3600} ч"
        return f"{reset:%d.%m %H:%M} ({relative})"
    except (TypeError, ValueError, OSError):
        return "—"


def rate_limit_line(label, window):
    if not isinstance(window, dict):
        return f"{label}: нет данных"
    used = int(window.get("usedPercent") or 0)
    return (
        f"{label}: использовано {used}% · осталось {max(0, 100 - used)}% · "
        f"сброс {format_reset_time(window.get('resetsAt'))}"
    )


def build_usage_report():
    with state_lock:
        snapshot = dict(state)
    thread_id = snapshot.get("thread_id")
    last = snapshot.get("last_usage") or {}
    total = snapshot.get("session_usage") or last
    client = get_app_server()
    limits_result = usage_result = None
    limits_error = usage_error = None
    try:
        limits_result = client.request("account/rateLimits/read", timeout=30)
    except Exception as exc:
        limits_error = compact(str(exc), 300)
    try:
        usage_result = client.request(
            "account/usage/read", {"threadId": thread_id} if thread_id else {}, timeout=30,
        )
    except Exception as exc:
        usage_error = compact(str(exc), 300)

    messages = session_message_count(thread_id)
    context_tokens = last.get("input_tokens")
    context_window = snapshot.get("context_window")
    context = f"~{fmt_number(context_tokens)} tokens" if context_tokens else "нет данных"
    if context_tokens and context_window:
        context += f" / {fmt_number(context_window)} ({context_tokens / context_window:.1%})"
    lines = [
        "📊 Session",
        f"{(thread_id or 'нет активной')[:8]}  •  Model: {snapshot.get('model') or 'default'}",
        f"Messages: {messages if messages is not None else '—'}",
        f"Context: {context}",
        "",
        "🔢 Tokens (this session)",
        f"in {fmt_number(total.get('input_tokens'))}  ·  out {fmt_number(total.get('output_tokens'))}  ·  "
        f"cache-r {fmt_number(total.get('cached_input_tokens'))}  ·  "
        f"cache-w {fmt_number(total.get('cache_write_input_tokens'))}",
    ]
    thread_usage = (usage_result or {}).get("threadUsage") or {}
    usd_micros = thread_usage.get("estimatedUsageUsdMicros")
    if usd_micros is not None:
        lines.append(f"(~${usd_micros / 1_000_000:.4f} эквивалент по API-тарифу)")
    elif usage_error:
        lines.append(f"Стоимость: не удалось получить ({usage_error})")
    else:
        lines.append("Стоимость: недоступна для текущего subscription-маршрута")

    lines.extend(("", "📈 Account limits (subscription, not credits)"))
    limits = (limits_result or {}).get("rateLimits") or {}
    if limits:
        plan = limits.get("planType")
        if plan:
            lines.append(f"Plan: {str(plan).replace('_', ' ').title()}")
        lines.append(rate_limit_line("5-hour", limits.get("primary")))
        lines.append(rate_limit_line("Weekly", limits.get("secondary")))
        credits = limits.get("credits") or {}
        if credits.get("hasCredits") or credits.get("unlimited"):
            balance = "unlimited" if credits.get("unlimited") else credits.get("balance")
            lines.append(f"Credits: {balance}")
    else:
        lines.append(f"Не удалось получить: {limits_error or 'нет данных'}")
    return "\n".join(lines)


def request_restart(chat_id):
    """Persist a restart request; the watcher executes it only between turns."""
    RESTART_SIGNAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=RESTART_SIGNAL_FILE.name + ".", dir=RESTART_SIGNAL_FILE.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"chat_id": chat_id, "requested_at": time.time()}, handle)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, RESTART_SIGNAL_FILE)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def restart_watcher():
    global restart_draining
    while True:
        time.sleep(0.5)
        if not RESTART_SIGNAL_FILE.exists():
            continue
        with process_lock:
            if busy:
                continue
            restart_draining = True
        try:
            request = json.loads(RESTART_SIGNAL_FILE.read_text(encoding="utf-8"))
            chat_id = request.get("chat_id") if isinstance(request, dict) else OWNER_ID
        except Exception:
            chat_id = OWNER_ID
        try:
            RESTART_SIGNAL_FILE.unlink()
        except FileNotFoundError:
            pass
        update_state(restart_completed_chat_id=chat_id)
        send_plain(chat_id, "🔄 Текущий ход завершён. Перезапускаю Codex-бота…")
        time.sleep(0.5)
        os.kill(os.getpid(), signal.SIGTERM)
        return


def stop_current_process():
    global active_stopped
    with process_lock:
        client = app_server
        thread_id = active_thread_id
        turn_id = active_turn_id
        if client is None or not thread_id or not turn_id:
            return False
        active_stopped = True
    try:
        client.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
        return True
    except Exception as exc:
        log(f"Could not interrupt turn: {exc}")
        return False


def steer_current_turn(inputs, media_paths=None):
    global active_media_paths
    with process_lock:
        client = app_server
        thread_id = active_thread_id
        turn_id = active_turn_id
        if client is None or not thread_id or not turn_id:
            return False, "активный ход ещё не успел получить ID — повтори через секунду"
        active_media_paths.extend(media_paths or [])
    try:
        client.request("turn/steer", {
            "threadId": thread_id,
            "expectedTurnId": turn_id,
            "input": inputs,
        })
        return True, None
    except Exception as exc:
        with process_lock:
            for path in media_paths or []:
                try:
                    active_media_paths.remove(path)
                except ValueError:
                    pass
        return False, compact(str(exc), 500)


def handle_command(chat_id, command):
    global busy
    raw_cmd, _, arg = command.partition(" ")
    cmd = raw_cmd.split("@", 1)[0].lower().lstrip("/.")
    arg = arg.strip()
    if cmd in ("start", "help"):
        send_plain(chat_id, "Codex Telegram bridge. Команды доступны в меню бота.")
        return True
    if cmd == "new":
        stop_current_process()
        update_state(thread_id=None, last_usage=None, session_usage=None, context_window=None)
        send_plain(chat_id, "🆕 Текущий Codex-тред сброшен. Следующее сообщение начнёт новый.")
        return True
    if cmd == "sessions":
        current = state.get("thread_id")
        rows = []
        for path in session_files()[:10]:
            sid, preview = session_info(path)
            rows.append(f"{sid[:8]}{' ← текущая' if sid == current else ''}  {preview}")
        send_plain(chat_id, "Последние сессии:\n" + ("\n".join(rows) or "не найдены"))
        return True
    if cmd == "resume":
        matches = [sid for path in session_files() for sid, _ in [session_info(path)]
                   if arg and sid.startswith(arg)]
        if len(matches) != 1:
            send_plain(chat_id, "Укажи однозначный id/префикс: /resume <id>" if matches else "Сессия не найдена.")
        else:
            stop_current_process()
            update_state(thread_id=matches[0], last_usage=None, session_usage=None, context_window=None)
            send_plain(chat_id, f"Продолжаю сессию {matches[0][:8]}.")
        return True
    if cmd == "status":
        with state_lock:
            snapshot = dict(state)
        send_plain(chat_id, "ℹ️ Статус\n"
                   f"Сессия: {(snapshot.get('thread_id') or 'нет')[:8]}\n"
                   f"Модель: {snapshot.get('model') or 'default'}\n"
                   f"Sandbox: {snapshot.get('sandbox')}\n"
                   f"Workspace: {snapshot.get('workspace')}\n"
                   f"Занят: {'да' if busy else 'нет'}")
        return True
    if cmd == "usage":
        send_plain(chat_id, build_usage_report())
        return True
    if cmd == "model":
        update_state(model=None if arg.lower() in ("", "default") else arg)
        send_plain(chat_id, f"Модель: {state.get('model') or 'default'}.")
        return True
    if cmd == "mode":
        aliases = {"read": "read-only", "read-only": "read-only", "write": "workspace-write",
                   "workspace-write": "workspace-write", "full": "danger-full-access",
                   "danger-full-access": "danger-full-access"}
        if arg not in aliases:
            send_plain(chat_id, "Использование: /mode read-only|workspace-write|full")
        else:
            update_state(sandbox=aliases[arg])
            send_plain(chat_id, f"Sandbox: {aliases[arg]}.")
        return True
    if cmd == "workspace":
        path = CODEX_CWD if arg.lower() == "default" else os.path.abspath(os.path.expanduser(arg))
        if not arg:
            send_plain(chat_id, f"Workspace: {state.get('workspace')}\nИспользование: /workspace <путь>|default")
        elif not os.path.isdir(path):
            send_plain(chat_id, f"Директория не существует: {path}")
        else:
            update_state(workspace=path)
            send_plain(chat_id, f"Workspace: {path}")
        return True
    if cmd == "restart":
        request_restart(chat_id)
        if busy:
            send_plain(chat_id, "🔁 Перезапуск запланирован после завершения текущего хода.")
        else:
            send_plain(chat_id, "🔁 Перезапуск запланирован между ходами.")
        return True
    if cmd == "stop":
        running = stop_current_process()
        if running:
            send_plain(chat_id, "⏹ Останавливаю текущее выполнение Codex.")
        else:
            send_plain(chat_id, "Сейчас нечего останавливать.")
        return True
    if raw_cmd.startswith(("/", ".")):
        send_plain(chat_id, "Неизвестная команда. Открой меню команд Telegram.")
        return True
    return False


def handle_message(message):
    global busy
    chat_id = message.get("chat", {}).get("id")
    if chat_id != OWNER_ID:
        return
    text = message.get("text") or message.get("caption") or ""
    has_image = bool(message.get("photo")) or str(
        (message.get("document") or {}).get("mime_type", "")
    ).startswith("image/")
    if (not isinstance(text, str) or not text.strip()) and not has_image:
        return
    text = text.strip()
    with process_lock:
        draining = restart_draining
    if draining:
        send_plain(chat_id, "🔄 Уже начинаю перезапуск; сообщение пока не принято.")
        return
    if text.startswith(("/", ".")):
        handle_command(chat_id, text)
        return
    try:
        inputs, media_paths = message_inputs(message)
    except Exception as exc:
        send_plain(chat_id, f"Не смог скачать изображение: {compact(str(exc), 500)}")
        return
    with process_lock:
        if busy:
            already_busy = True
        else:
            busy = True
            already_busy = False
    if already_busy:
        steered, error = steer_current_turn(inputs, media_paths)
        if steered:
            send_plain(chat_id, "↪️ Добавил сообщение в текущий ход Codex.")
        else:
            for path in media_paths:
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass
            send_plain(chat_id, f"Не получилось добавить в текущий ход: {error}")
        return
    with state_lock:
        thread_id = state.get("thread_id")
    threading.Thread(
        target=run_turn, args=(chat_id, inputs, thread_id, media_paths), daemon=True
    ).start()


def register_commands():
    payload = {"commands": [{"command": c, "description": d} for c, d in COMMANDS]}
    tg_call("setMyCommands", payload)
    tg_call("setMyCommands", {**payload, "scope": {"type": "all_private_chats"}})


def main():
    offset = None
    register_commands()
    with state_lock:
        completed_restart_chat_id = state.get("restart_completed_chat_id")
    if completed_restart_chat_id:
        update_state(restart_completed_chat_id=None)
        send_plain(completed_restart_chat_id, "✅ Перезагрузка окончена, бот готов к работе.")
    try:
        get_app_server().start_if_needed()
        log("Persistent Codex app-server is ready")
    except Exception as exc:
        log(f"Codex app-server warm start failed; will retry on first message: {exc}")
    threading.Thread(target=restart_watcher, daemon=True).start()
    log(f"Codex Telegram bot started; owner={OWNER_ID}, cwd={CODEX_CWD}")
    while True:
        params = {"timeout": 30, "allowed_updates": ["message"]}
        if offset is not None:
            params["offset"] = offset
        result = tg_call("getUpdates", params, timeout=40)
        if not result.get("ok"):
            time.sleep(1)
            continue
        for update in result.get("result", []):
            try:
                offset = max(offset or 0, update["update_id"] + 1)
                message = update.get("message")
                if isinstance(message, dict):
                    handle_message(message)
            except Exception as exc:
                log(f"Unexpected update handler error: {exc}")
                chat_id = (update.get("message") or {}).get("chat", {}).get("id")
                if chat_id == OWNER_ID:
                    send_plain(chat_id, "⚠️ Ошибка моста. Подробности записаны в лог.")


if __name__ == "__main__":
    main()
