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
BATCH_DEBOUNCE_S = 1.5
MAX_MESSAGE_LEN = 4000
RICH_MAX_CHARS = 30000
HTTP_TIMEOUT_S = 20
IDLE_TIMEOUT_S = 300
TOTAL_TIMEOUT_S = 1800
COMMANDS = [
    ("new", "Начать новую Codex-сессию"),
    ("sessions", "Список последних сессий"),
    ("resume", "Продолжить сессию по id"),
    ("status", "Сессия, модель, sandbox и workspace"),
    ("stop", "Прервать текущий запрос"),
    ("usage", "Токены последнего запроса"),
    ("compact", "Сжать контекст текущей сессии"),
    ("model", "Выбрать модель Codex"),
    ("effort", "Выбрать мощность модели"),
    ("mode", "Sandbox: read-only/workspace-write/full"),
    ("workspace", "Рабочая директория"),
    ("account", "Состояние аккаунта Codex"),
    ("login", "Подключить свой аккаунт Codex"),
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
WHITELIST_FILE = Path(os.environ.get(
    "CODEX_BOT_WHITELIST_FILE", Path(__file__).with_name("whitelist.txt")
)).expanduser()
ACCOUNTS_DIR = Path(os.environ.get(
    "CODEX_BOT_ACCOUNTS_DIR", Path(__file__).with_name("accounts")
)).expanduser()

state_lock = threading.RLock()
process_lock = threading.RLock()
telegram_lock = threading.Lock()
rate_limit_until = 0.0
restart_draining = False


class TenantRuntime:
    def __init__(self, chat_id):
        self.chat_id = int(chat_id)
        self.busy = False
        self.app_server = None
        self.loaded_thread_id = None
        self.loaded_server_pid = None
        self.active_view = None
        self.active_done = None
        self.active_turn_id = None
        self.active_thread_id = None
        self.active_error = None
        self.active_stopped = False
        self.active_last_event_at = None
        self.active_media_paths = []
        self.last_rate_limits = None
        self.login_id = None
        # Rapid Telegram updates (most visibly a multi-message forward) are
        # held briefly and dispatched as one prompt instead of starting one
        # Codex turn per update.
        self.pending_batch = []
        self.batch_timer = None


tenants = {}


def get_tenant(chat_id):
    chat_id = int(chat_id)
    with process_lock:
        runtime = tenants.get(chat_id)
        if runtime is None:
            runtime = TenantRuntime(chat_id)
            tenants[chat_id] = runtime
        return runtime


def load_whitelist():
    result = {str(OWNER_ID)}
    try:
        raw = WHITELIST_FILE.read_text(encoding="utf-8")
        result.update(part.strip() for part in raw.replace("\n", ",").split(",") if part.strip())
    except FileNotFoundError:
        pass
    except Exception as exc:
        log(f"Could not read whitelist: {exc}")
    return result


def tenant_codex_home(chat_id):
    if int(chat_id) == OWNER_ID:
        return None
    path = ACCOUNTS_DIR / str(chat_id)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


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
        return {"version": 2, "chats": {}, "runtime": {}}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("state root is not an object")
        if data.get("version") == 2 and isinstance(data.get("chats"), dict):
            data.setdefault("runtime", {})
            return data
        # One-time, lossless migration: the old global state belonged to the
        # owner. Additional users start with clean isolated entries.
        runtime_keys = ("restart_completed_chat_id", "restart_message_id")
        runtime = {key: data.pop(key) for key in runtime_keys if key in data}
        return {"version": 2, "chats": {str(OWNER_ID): data}, "runtime": runtime}
    except Exception as exc:
        raise SystemExit(f"codex-telegram-bot: cannot read state file {STATE_FILE}: {exc}") from exc


state_db = load_state()


def chat_state(chat_id):
    with state_lock:
        entry = state_db["chats"].setdefault(str(chat_id), {})
        entry.setdefault("thread_id", None)
        entry.setdefault("model", None)
        entry.setdefault("effort", None)
        entry.setdefault("sandbox", CODEX_SANDBOX)
        entry.setdefault("workspace", CODEX_CWD)
        entry.setdefault("last_usage", None)
        entry.setdefault("session_usage", None)
        entry.setdefault("context_window", None)
        entry.setdefault("account_status", "ready" if int(chat_id) == OWNER_ID else None)
        return entry


def update_state(chat_id=OWNER_ID, **values):
    with state_lock:
        state_db["chats"].setdefault(str(chat_id), {}).update(values)
        _save_state_locked()


def update_runtime_state(**values):
    with state_lock:
        state_db.setdefault("runtime", {}).update(values)
        _save_state_locked()


def _save_state_locked():
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=STATE_FILE.name + ".", dir=STATE_FILE.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state_db, handle, ensure_ascii=False, indent=2)
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


def save_thread_id(chat_id, thread_id):
    update_state(chat_id, thread_id=thread_id)


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


def pretty_tool_value(value, limit=1200):
    """Human-sized tool input; never serialize an entire protocol envelope."""
    if value in (None, "", [], {}):
        return ""
    if isinstance(value, str):
        return compact(value, limit)
    try:
        return compact(json.dumps(value, ensure_ascii=False, indent=2), limit)
    except (TypeError, ValueError):
        return compact(str(value), limit)


def protocol_text(value):
    """Extract readable text from App Server text/summary values."""
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for part in value:
            if isinstance(part, str):
                text = part
            elif isinstance(part, dict):
                text = part.get("text") or part.get("summary") or part.get("content") or ""
            else:
                text = str(part)
            if text:
                parts.append(str(text))
        return "\n".join(parts)
    return str(value)


def tool_result_text(value, limit=1600):
    """Extract useful text from MCP/dynamic output without IDs and metadata."""
    if not isinstance(value, dict):
        return pretty_tool_value(value, limit)
    content = value.get("content") or value.get("contentItems") or []
    texts = []
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                text = part.get("text") or part.get("outputText")
                if text:
                    texts.append(str(text))
    if texts:
        return compact("\n".join(texts), limit)
    error = value.get("error")
    if error:
        return pretty_tool_value(error, limit)
    # Do not fall back to the full result object: it commonly contains
    # structuredContent duplicates, resources, opaque IDs and base64 data.
    return "результат получен"


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
        return "💬 Ответ", protocol_text(item.get("text", "")), []
    if item_type == "reasoning":
        return "🧠 Размышление", protocol_text(
            item.get("text", item.get("summary", ""))
        ), []
    if item_type == "command_execution":
        command = item.get("command") or item.get("commandLine") or ""
        exit_code = item.get("exit_code")
        output = item.get("aggregated_output")
        results = []
        if output not in (None, ""):
            results.append(("📤 Результат", compact(str(output), 1800)))
        if exit_code is not None:
            try:
                succeeded = int(exit_code) == 0
            except (TypeError, ValueError):
                succeeded = False
            results.append(("✅ Код завершения" if succeeded else "❌ Код завершения", str(exit_code)))
        # Match Claude's live renderer: identify the concrete tool instead of
        # exposing a generic "Выполняю" status.
        return "🔧 Bash", compact(command, 1400), results
    if item_type == "file_change":
        # App Server includes the complete patch in changes[*].kind.diff.  A
        # Progress is a user-facing summary, not a debug console: exposing that payload can
        # fill the whole chat with escaped JSON and partially rendered code.
        changes = item.get("changes") or []
        if not isinstance(changes, list):
            changes = [changes]
        summaries = []
        for change in changes:
            if not isinstance(change, dict):
                continue
            path = change.get("path")
            kind = change.get("kind")
            if isinstance(kind, dict):
                kind = kind.get("type")
            labels = {"add": "создан", "delete": "удалён", "update": "изменён"}
            if path:
                summaries.append(f"{path} — {labels.get(kind, kind or 'изменён')}")
        content = "\n".join(summaries) or str(item.get("path") or "файл изменён")
        return "📝 Изменение файла", compact(content, 1200), []
    if item_type == "web_search":
        query = item.get("query") or "поиск"
        action = item.get("action") or {}
        action_type = action.get("type") if isinstance(action, dict) else None
        labels = {"openPage": "открываю страницу", "findInPage": "ищу на странице",
                  "search": "ищу в интернете"}
        suffix = labels.get(action_type)
        content = f"{suffix}: {query}" if suffix else str(query)
        return "🔎 Поиск", compact(content, 1000), []
    if item_type == "mcp_tool_call":
        name = ".".join(filter(None, (item.get("server"), item.get("tool")))) or "MCP"
        arguments = pretty_tool_value(item.get("arguments"))
        results = []
        if item.get("error"):
            results.append(("❌ Ошибка", pretty_tool_value(item["error"], 1200)))
        elif item.get("result") is not None:
            results.append(("📤 Результат", tool_result_text(item["result"])))
        return f"🔧 {name}", arguments, results
    if item_type == "dynamic_tool_call":
        name = ".".join(filter(None, (item.get("namespace"), item.get("tool")))) or "инструмент"
        arguments = pretty_tool_value(item.get("arguments"))
        results = []
        if item.get("contentItems") is not None:
            results.append(("📤 Результат", tool_result_text(
                {"contentItems": item.get("contentItems")}
            )))
        return f"🔧 {name}", arguments, results
    if item_type in ("collab_agent_tool_call", "sub_agent_activity"):
        tool = item.get("tool") or item.get("kind") or "работа агента"
        prompt = item.get("prompt")
        states = item.get("agentsStates") or {}
        state_text = ", ".join(
            str(value.get("status") if isinstance(value, dict) else value)
            for value in states.values()
        )
        content = pretty_tool_value(prompt, 1000) or state_text or str(tool)
        return f"🤖 Агент · {tool}", content, []
    if item_type == "image_view":
        return "🖼 Просмотр изображения", compact(item.get("path") or "изображение", 1000), []
    if item_type == "image_generation":
        failure = item.get("failure")
        return "🎨 Генерация изображения", (
            pretty_tool_value(failure, 1000) if failure else "изображение создаётся"
        ), []
    if item_type == "context_compaction":
        return "🗜 Сжатие контекста", "контекст сессии сжат", []
    if item_type == "plan":
        return "📋 План", compact(item.get("text") or "план обновлён", 1400), []
    if item_type == "sleep":
        seconds = (item.get("durationMs") or 0) / 1000
        return "⏳ Ожидание", f"{seconds:g} с", []
    if item_type in ("entered_review_mode", "exited_review_mode"):
        text = "режим проверки включён" if item_type.startswith("entered") else "режим проверки завершён"
        return "🔍 Проверка", text, []
    # Future App Server item types must degrade to a short label. Never put
    # the complete protocol object into Telegram: it may contain huge output,
    # patches, base64 media, internal IDs or other implementation details.
    # Unknown/future protocol items still get a useful, neutral label.  Do
    # not leak the product name or invent a fake "action"/"in progress"
    # payload when the protocol did not provide one.
    return "🔧 Инструмент", str(item_type).replace("_", " "), []


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
        "contextCompaction": "context_compaction",
        "subAgentActivity": "sub_agent_activity",
        "imageGeneration": "image_generation",
        "enteredReviewMode": "entered_review_mode",
        "exitedReviewMode": "exited_review_mode",
    }
    result["type"] = type_map.get(result.get("type"), result.get("type", "unknown"))
    if result["type"] == "reasoning":
        summary = result.get("summary") or result.get("content") or []
        result["text"] = protocol_text(summary)
    result["aggregated_output"] = result.get("aggregatedOutput")
    result["exit_code"] = result.get("exitCode")
    if result["type"] == "file_change":
        result["changes"] = result.get("changes", [])
    return result


def user_facing_codex_error(error):
    if not isinstance(error, dict):
        text = str(error)
        info = None
    else:
        text = str(error.get("message") or "Неизвестная ошибка Codex")
        info = error.get("codexErrorInfo")
    combined = f"{info or ''} {text}".lower()
    if "contextwindowexceeded" in combined or "context window" in combined:
        return (
            "Контекст текущей сессии исчерпан. Используй /compact, чтобы сжать "
            "историю и продолжить, либо /new для новой сессии."
        )
    if "sessionbudgetexceeded" in combined:
        return "Бюджет этой сессии исчерпан. Начни новую через /new."
    if "usagelimitexceeded" in combined:
        return usage_limit_exceeded_message()
    return compact(text, 1000)


def usage_limit_exceeded_message(runtime=None):
    with process_lock:
        limits = dict((runtime.last_rate_limits if runtime else None) or {})
    windows = [
        ("5-часовой лимит", limits.get("primary")),
        ("недельный лимит", limits.get("secondary")),
    ]
    reached = [(label, window) for label, window in windows
               if isinstance(window, dict) and int(window.get("usedPercent") or 0) >= 100]
    if not reached:
        available = [(label, window) for label, window in windows if isinstance(window, dict)]
        reached = sorted(
            available, key=lambda pair: int(pair[1].get("usedPercent") or 0), reverse=True
        )[:1]
    if not reached:
        return "Лимит сессии Codex исчерпан. Попробуй позже; актуальное состояние — /usage."
    details = "; ".join(
        f"{label}, сброс {format_reset_time(window.get('resetsAt'))}"
        for label, window in reached
    )
    return f"⏳ Лимит сессии Codex исчерпан: {details}. После сброса можно продолжить этот же тред."


def render_process_item(item):
    label, content, results = item_label_and_blocks(item)
    if item.get("type") in ("reasoning", "agent_message") and not str(content).strip():
        return ""
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
        self.current_thought = None
        self.current_thought_id = None
        self.current_tool = None
        self.last_edit_at = 0.0
        self.usage = None
        self.completed = False
        self.context_notice = None
        # A real Telegram message is the live process card.  It is edited in
        # place as App Server events arrive, like the Claude bridge; this is
        # deliberately not Telegram's ephemeral sendMessageDraft API.
        self.progress_msg_id = None
        self.progress_attempted = False
        self.progress_lock = threading.Lock()

    @staticmethod
    def _thought_id(item):
        return item.get("id") or item.get("itemId") or item.get("item_id")

    def _set_thought(self, item):
        text = protocol_text(item.get("text", item.get("summary", "")))
        if not text.strip():
            return False
        self.current_thought = text
        self.current_thought_id = self._thought_id(item)
        # A new thought starts the next model phase.  Until this point the
        # previous thought remains visible while a following tool runs.
        self.current_tool = None
        return True

    def add_thought_delta(self, delta, item_id=None):
        delta = protocol_text(delta)
        if not delta:
            return
        # App Server normally supplies itemId.  If an older server omits it,
        # the presence of a tool is enough to identify this as the next
        # thought phase.  Subsequent deltas then append to the same thought.
        if item_id and item_id != self.current_thought_id:
            self.current_thought = ""
            self.current_tool = None
            self.current_thought_id = item_id
        elif self.current_tool is not None:
            self.current_thought = ""
            self.current_tool = None
            self.current_thought_id = item_id
        self.current_thought = (self.current_thought or "") + delta

    def add_event(self, event):
        event_type = event.get("type", "unknown")
        if event_type == "thread.started":
            thread_id = event.get("thread_id")
            if thread_id:
                save_thread_id(self.chat_id, thread_id)
        elif event_type == "turn.started":
            pass
        elif event_type in ("item.started", "item.updated"):
            item = event.get("item")
            if isinstance(item, dict):
                item_type = item.get("type")
                if item_type in ("agent_message", "reasoning"):
                    self._set_thought(item)
                else:
                    self.current_tool = item
        elif event_type == "item.completed":
            item = event.get("item")
            if isinstance(item, dict):
                self.items.append(item)
                item_type = item.get("type")
                if item_type in ("agent_message", "reasoning"):
                    self._set_thought(item)
                else:
                    self.current_tool = item
                self.process_items.append(item)
        elif event_type == "turn.completed":
            self.completed = True
            self.usage = event.get("usage")
            if isinstance(self.usage, dict):
                update_state(self.chat_id, last_usage=self.usage)

    def live_text(self):
        lines = []
        if self.current_thought:
            lines.append(escape_mdv2(self.current_thought))
        if self.current_tool:
            label, content, results = item_label_and_blocks(self.current_tool)
            lines.append(escape_mdv2(f"{label}:"))
            if content:
                lines.append(mdv2_code_block(content))
            for result_label, result_content in results:
                lines.extend((escape_mdv2(f"{result_label}:"),
                              mdv2_code_block(result_content)))
        body = "\n".join(lines) if lines else "Думаю"
        return f"🤔 {body}"

    def _send_or_edit_live(self, text, force=False):
        """Publish one persistent process message and update it in place."""
        with self.progress_lock:
            now = time.monotonic()
            if (
                self.progress_msg_id is not None
                and not force
                and now - self.last_edit_at < EDIT_THROTTLE_S
            ):
                return
            if self.progress_msg_id is None:
                # A failed first send must not cause a Telegram request for
                # every token delta.  A later forced flush can retry once.
                if self.progress_attempted and not force:
                    return
                self.progress_attempted = True
                result = tg_call("sendMessage", {
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": "MarkdownV2",
                })
                if not result.get("ok") and not _rate_limited(result):
                    result = tg_call("sendMessage", {
                        "chat_id": self.chat_id,
                        "text": strip_mdv2(text).replace("```", ""),
                    })
                if result.get("ok"):
                    self.progress_msg_id = (result.get("result") or {}).get("message_id")
            else:
                params = {
                    "chat_id": self.chat_id,
                    "message_id": self.progress_msg_id,
                    "text": text,
                    "parse_mode": "MarkdownV2",
                }
                result = tg_call("editMessageText", params)
                if not result.get("ok"):
                    description = str(result.get("description", "")).lower()
                    if "not modified" not in description and not _rate_limited(result):
                        params["text"] = strip_mdv2(text).replace("```", "")
                        params.pop("parse_mode", None)
                        tg_call("editMessageText", params)
            self.last_edit_at = now

    def flush(self, force=False):
        """Render the current thought/tool snapshot into one live message."""
        now = time.monotonic()
        if not force and self.progress_msg_id is not None:
            if now - self.last_edit_at < EDIT_THROTTLE_S:
                return
        try:
            self._send_or_edit_live(truncate_mdv2(self.live_text()), force=force)
        except Exception as exc:
            # Telegram hiccups must never stop App Server event consumption.
            log(f"Live progress update failed: {exc}")

    def replace_progress(self, text):
        """Replace the live process card in place, returning success."""
        with self.progress_lock:
            if self.progress_msg_id is None:
                return False
            result = edit_rich(self.chat_id, self.progress_msg_id, text)
            return bool(result and result.get("ok"))

    def deliver(self, stopped=False, error=None):
        final_index = next(
            (i for i in range(len(self.items) - 1, -1, -1)
             if self.items[i].get("type") == "agent_message"), None
        )
        final_text = (
            protocol_text(self.items[final_index].get("text", ""))
            if final_index is not None else ""
        )

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
        if self.context_notice:
            answer += f"\n\n{self.context_notice}"

        if process_items:
            process_steps = [
                step for step in (render_process_item(item) for item in process_items)
                if step
            ]
        else:
            process_steps = []
        if process_steps:
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
            if not self.replace_progress(rich):
                send_rich(self.chat_id, rich)
            send_rich(self.chat_id, answer)
        elif self.progress_msg_id is not None:
            if not self.replace_progress(answer):
                send_rich(self.chat_id, answer)
        else:
            send_rich(self.chat_id, answer)


def get_app_server(runtime):
    with process_lock:
        if runtime.app_server is None:
            env = dict(os.environ)
            codex_home = tenant_codex_home(runtime.chat_id)
            if codex_home is not None:
                env["CODEX_HOME"] = str(codex_home)
            runtime.app_server = AppServerClient(
                lambda method, params: handle_app_notification(runtime, method, params),
                lambda message: log(f"tenant={runtime.chat_id} {message}"),
                env=env,
            )
        return runtime.app_server


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


def handle_app_notification(runtime, method, params):
    if method == "account/rateLimits/updated":
        update = params.get("rateLimits") or {}
        with process_lock:
            merged = dict(runtime.last_rate_limits or {})
            for key, value in update.items():
                if value is not None:
                    merged[key] = value
            runtime.last_rate_limits = merged
        return
    if method == "account/login/completed":
        success = bool(params.get("success"))
        update_state(runtime.chat_id, account_status="ready" if success else "login_failed")
        if success:
            send_plain(runtime.chat_id, "✅ Вход в аккаунт Codex завершён.")
        else:
            send_plain(runtime.chat_id, f"❌ Вход не завершён: {params.get('error') or 'неизвестная ошибка'}")
        return
    with process_lock:
        view = runtime.active_view
        done = runtime.active_done
        if view is None:
            return
        event_turn_id = params.get("turnId") or (params.get("turn") or {}).get("id")
        if runtime.active_turn_id and event_turn_id and event_turn_id != runtime.active_turn_id:
            return
        runtime.active_last_event_at = time.monotonic()
        if event_turn_id:
            runtime.active_turn_id = event_turn_id
        if params.get("threadId"):
            runtime.active_thread_id = params["threadId"]

    force = False
    if method in ("item/started", "item/updated", "item/completed"):
        item = normalize_app_item(params.get("item"))
        # User messages and internal hook prompts are protocol bookkeeping,
        # not model actions. Rendering them exposed raw JSON after a mid-turn
        # message was injected.
        if item.get("type") not in ("userMessage", "hookPrompt"):
            event_type = {
                "item/started": "item.started",
                "item/updated": "item.updated",
                "item/completed": "item.completed",
            }[method]
            view.add_event({"type": event_type, "item": item})
            if item.get("type") == "context_compaction" and method.endswith("completed"):
                view.context_notice = "🗜 Контекст сессии автоматически сжат."
    elif method == "item/agentMessage/delta":
        view.add_thought_delta(
            params.get("delta", ""),
            params.get("itemId") or params.get("item_id") or params.get("id"),
        )
    elif method == "thread/tokenUsage/updated":
        token_usage = params.get("tokenUsage") or {}
        view.usage = _usage_for_renderer(token_usage)
        context_window = token_usage.get("modelContextWindow")
        context_tokens = view.usage.get("input_tokens") or 0
        if context_window and context_tokens:
            ratio = context_tokens / context_window
            if ratio >= 0.9:
                view.context_notice = (
                    f"⚠️ Контекст заполнен на {ratio:.0%}. Рекомендуется /compact или /new."
                )
            elif ratio >= 0.8:
                view.context_notice = (
                    f"⚠️ Контекст заполнен на {ratio:.0%}; скоро понадобится /compact."
                )
        update_state(
            runtime.chat_id,
            last_usage=view.usage,
            session_usage=_usage_breakdown(token_usage.get("total") or {}),
            context_window=token_usage.get("modelContextWindow"),
        )
    elif method == "error" and not params.get("willRetry"):
        with process_lock:
            error = params.get("error", "неизвестная ошибка")
            if isinstance(error, dict) and error.get("codexErrorInfo") == "usageLimitExceeded":
                runtime.active_error = usage_limit_exceeded_message(runtime)
            else:
                runtime.active_error = user_facing_codex_error(error)
    elif method == "thread/compacted":
        view.context_notice = "🗜 Контекст сессии сжат."
        force = True
        if done is not None:
            done.set()
    elif method == "turn/completed":
        turn = params.get("turn") or {}
        if turn.get("error"):
            with process_lock:
                turn_error = turn["error"]
                if (isinstance(turn_error, dict)
                        and turn_error.get("codexErrorInfo") == "usageLimitExceeded"):
                    runtime.active_error = usage_limit_exceeded_message(runtime)
                else:
                    runtime.active_error = user_facing_codex_error(turn_error)
        view.completed = True
        force = True
        if done is not None:
            done.set()
    view.flush(force=force)


def _thread_params(runtime, thread_id=None):
    with state_lock:
        snapshot = dict(chat_state(runtime.chat_id))
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


def available_models(runtime):
    """Return the visible live model catalog advertised by App Server."""
    result = get_app_server(runtime).request(
        "model/list", {"limit": 100, "includeHidden": False}, timeout=30,
    ) or {}
    return [model for model in result.get("data", [])
            if isinstance(model, dict) and not model.get("hidden")]


def model_key(model):
    return str(model.get("model") or model.get("id") or "")


def selected_model(runtime, models=None, persist=True):
    models = models if models is not None else available_models(runtime)
    with state_lock:
        snapshot = dict(chat_state(runtime.chat_id))
    current = snapshot.get("model")
    chosen = next((model for model in models if model_key(model) == current), None)
    if chosen is None:
        chosen = next((model for model in models if model.get("isDefault")), None)
    if chosen is None and models:
        chosen = models[0]
    if chosen and persist:
        updates = {}
        key = model_key(chosen)
        if current != key:
            updates["model"] = key
        supported = [option.get("reasoningEffort") for option in
                     chosen.get("supportedReasoningEfforts", []) if isinstance(option, dict)]
        if snapshot.get("effort") not in supported:
            updates["effort"] = chosen.get("defaultReasoningEffort") or (supported[0] if supported else None)
        if updates:
            update_state(runtime.chat_id, **updates)
    return chosen


def render_model_picker(runtime):
    models = available_models(runtime)
    chosen = selected_model(runtime, models)
    current = model_key(chosen) if chosen else None
    lines = ["🧠 Доступные модели:"]
    for model in models:
        key = model_key(model)
        name = model.get("displayName") or key
        lines.append(f"{'●' if key == current else '○'} {name} — /model {key}")
    if not models:
        lines.append("Список моделей пуст.")
    return "\n".join(lines)


def render_effort_picker(runtime):
    models = available_models(runtime)
    chosen = selected_model(runtime, models)
    if not chosen:
        return "Codex не вернул доступных моделей."
    current = chat_state(runtime.chat_id).get("effort")
    lines = [f"⚡ Мощность модели {chosen.get('displayName') or model_key(chosen)}:"]
    for option in chosen.get("supportedReasoningEfforts", []):
        if not isinstance(option, dict):
            continue
        effort = option.get("reasoningEffort")
        if not effort:
            continue
        description = option.get("description")
        line = f"{'●' if effort == current else '○'} {effort} — /effort {effort}"
        if description:
            line += f"\n   {description}"
        lines.append(line)
    return "\n".join(lines)


def ensure_thread(runtime, client, requested_thread_id):
    process_pid = client.process.pid if client.process is not None else None
    with process_lock:
        if (requested_thread_id and requested_thread_id == runtime.loaded_thread_id
                and process_pid == runtime.loaded_server_pid):
            return requested_thread_id
    method = "thread/resume" if requested_thread_id else "thread/start"
    try:
        result = client.request(method, _thread_params(runtime, requested_thread_id), timeout=60)
    except AppServerError:
        if not requested_thread_id:
            raise
        log(f"Could not resume thread {requested_thread_id}; starting a new thread")
        result = client.request("thread/start", _thread_params(runtime), timeout=60)
    thread_id = ((result or {}).get("thread") or {}).get("id")
    if not thread_id:
        raise AppServerError(f"{method} returned no thread id")
    with process_lock:
        runtime.loaded_thread_id = thread_id
        runtime.loaded_server_pid = process_pid
    save_thread_id(runtime.chat_id, thread_id)
    return thread_id


def sandbox_policy(name, workspace):
    if name == "read-only":
        return {"type": "readOnly", "networkAccess": False}
    if name == "workspace-write":
        return {"type": "workspaceWrite", "writableRoots": [workspace],
                "networkAccess": True}
    return {"type": "dangerFullAccess"}


def run_turn(runtime, inputs, thread_id, media_paths=None):
    chat_id = runtime.chat_id
    view = TurnView(chat_id)
    done = threading.Event()
    error = None
    stopped = False
    try:
        client = get_app_server(runtime)
        client.start_if_needed()
        with state_lock:
            needs_model_settings = not (
                chat_state(chat_id).get("model") and chat_state(chat_id).get("effort")
            )
        if needs_model_settings:
            selected_model(runtime)
        server_thread_id = ensure_thread(runtime, client, thread_id)
        with state_lock:
            snapshot = dict(chat_state(chat_id))
        with process_lock:
            runtime.active_view = view
            runtime.active_done = done
            runtime.active_turn_id = None
            runtime.active_thread_id = server_thread_id
            runtime.active_error = None
            runtime.active_stopped = False
            runtime.active_last_event_at = time.monotonic()
            runtime.active_media_paths = list(media_paths or [])
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
        if snapshot.get("effort"):
            params["effort"] = snapshot["effort"]
        result = client.request("turn/start", params, timeout=60)
        turn_id = ((result or {}).get("turn") or {}).get("id")
        with process_lock:
            runtime.active_turn_id = runtime.active_turn_id or turn_id
        started_at = time.monotonic()
        while not done.wait(1):
            with process_lock:
                last_event = runtime.active_last_event_at or started_at
                process = client.process
            now = time.monotonic()
            if process is None or process.poll() is not None:
                error = "постоянный процесс Codex неожиданно завершился"
                break
            if now - last_event > IDLE_TIMEOUT_S or now - started_at > TOTAL_TIMEOUT_S:
                error = "Codex остановлен по таймауту"
                stop_current_process(runtime)
                break
        with process_lock:
            error = error or runtime.active_error
            stopped = runtime.active_stopped
        view.deliver(stopped=stopped, error=error)
    except Exception as exc:
        log(f"Codex worker failed: {exc}")
        view.deliver(error=compact(str(exc), 1000))
    finally:
        with process_lock:
            runtime.active_view = None
            runtime.active_done = None
            runtime.active_turn_id = None
            runtime.active_thread_id = None
            runtime.active_error = None
            runtime.active_stopped = False
            runtime.active_last_event_at = None
            paths = runtime.active_media_paths
            runtime.active_media_paths = []
            runtime.busy = False
        for path in paths:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass


def run_compaction(runtime, thread_id):
    chat_id = runtime.chat_id
    view = TurnView(chat_id)
    done = threading.Event()
    error = None
    try:
        client = get_app_server(runtime)
        client.start_if_needed()
        server_thread_id = ensure_thread(runtime, client, thread_id)
        with process_lock:
            runtime.active_view = view
            runtime.active_done = done
            runtime.active_turn_id = None
            runtime.active_thread_id = server_thread_id
            runtime.active_error = None
            runtime.active_stopped = False
            runtime.active_last_event_at = time.monotonic()
        client.request("thread/compact/start", {"threadId": server_thread_id}, timeout=60)
        if not done.wait(TOTAL_TIMEOUT_S):
            error = "Сжатие контекста не завершилось за отведённое время."
        with process_lock:
            error = error or runtime.active_error
        if error:
            message = f"🗜 Не удалось сжать контекст: {error}"
            send_plain(chat_id, message)
        else:
            update_state(chat_id, last_usage=None, session_usage=None, context_window=None)
            message = "🗜 Контекст сессии сжат. Можно продолжать."
            send_plain(chat_id, message)
    except Exception as exc:
        message = f"🗜 Не удалось сжать контекст: {user_facing_codex_error(exc)}"
        send_plain(chat_id, message)
    finally:
        with process_lock:
            runtime.active_view = None
            runtime.active_done = None
            runtime.active_turn_id = None
            runtime.active_thread_id = None
            runtime.active_error = None
            runtime.active_stopped = False
            runtime.active_last_event_at = None
            runtime.busy = False


def session_files(chat_id=OWNER_ID):
    codex_home = tenant_codex_home(chat_id)
    root = (codex_home or Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))) / "sessions"
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


def session_message_count(chat_id, thread_id):
    if not thread_id:
        return None
    for path in session_files(chat_id):
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


def build_usage_report(runtime):
    chat_id = runtime.chat_id
    try:
        if not chat_state(chat_id).get("model"):
            selected_model(runtime)
    except Exception as exc:
        log(f"Could not resolve model for usage: {exc}")
    with state_lock:
        snapshot = dict(chat_state(chat_id))
    thread_id = snapshot.get("thread_id")
    last = snapshot.get("last_usage") or {}
    total = snapshot.get("session_usage") or last
    client = get_app_server(runtime)
    limits_result = usage_result = None
    limits_error = usage_error = None
    try:
        limits_result = client.request("account/rateLimits/read", timeout=30)
        with process_lock:
            runtime.last_rate_limits = (limits_result or {}).get("rateLimits") or runtime.last_rate_limits
    except Exception as exc:
        limits_error = compact(str(exc), 300)
    try:
        usage_result = client.request(
            "account/usage/read", {"threadId": thread_id} if thread_id else {}, timeout=30,
        )
    except Exception as exc:
        usage_error = compact(str(exc), 300)

    messages = session_message_count(chat_id, thread_id)
    context_tokens = last.get("input_tokens")
    context_window = snapshot.get("context_window")
    context = f"~{fmt_number(context_tokens)} tokens" if context_tokens else "нет данных"
    if context_tokens and context_window:
        context += f" / {fmt_number(context_window)} ({context_tokens / context_window:.1%})"
    lines = [
        "📊 Session",
        f"{(thread_id or 'нет активной')[:8]}  •  Model: {snapshot.get('model') or 'не определена'}"
        f"  •  Effort: {snapshot.get('effort') or 'не определён'}",
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


def refresh_rate_limits(runtime):
    try:
        result = get_app_server(runtime).request("account/rateLimits/read", timeout=30)
        with process_lock:
            runtime.last_rate_limits = (result or {}).get("rateLimits") or runtime.last_rate_limits
    except Exception as exc:
        log(f"Could not preload Codex rate limits: {exc}")


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
            if any(runtime.busy or runtime.pending_batch for runtime in tenants.values()):
                continue
            restart_draining = True
        try:
            request = json.loads(RESTART_SIGNAL_FILE.read_text(encoding="utf-8"))
            chat_id = (request.get("chat_id") if isinstance(request, dict) else None) or OWNER_ID
        except Exception:
            chat_id = OWNER_ID
        try:
            RESTART_SIGNAL_FILE.unlink()
        except FileNotFoundError:
            pass
        result = send_plain(chat_id, "🔄 Текущий ход завершён. Перезапускаю Codex-бота…")
        message_id = (result.get("result") or {}).get("message_id") if result else None
        update_runtime_state(
            restart_completed_chat_id=chat_id,
            restart_message_id=message_id if isinstance(message_id, int) else None,
        )
        time.sleep(0.5)
        os.kill(os.getpid(), signal.SIGTERM)
        return


def stop_current_process(runtime):
    with process_lock:
        client = runtime.app_server
        thread_id = runtime.active_thread_id
        turn_id = runtime.active_turn_id
        if client is None or not thread_id or not turn_id:
            return False
        runtime.active_stopped = True
    try:
        client.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
        return True
    except Exception as exc:
        log(f"Could not interrupt turn: {exc}")
        return False


def steer_current_turn(runtime, inputs, media_paths=None):
    with process_lock:
        client = runtime.app_server
        thread_id = runtime.active_thread_id
        turn_id = runtime.active_turn_id
        if client is None or not thread_id or not turn_id:
            return False, "активный ход ещё не успел получить ID — повтори через секунду"
        runtime.active_media_paths.extend(media_paths or [])
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
                    runtime.active_media_paths.remove(path)
                except ValueError:
                    pass
        return False, compact(str(exc), 500)


def cleanup_media_paths(paths):
    for path in paths or []:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def combine_input_batch(entries):
    """Combine rapid Telegram messages into one ordered App Server input.

    Text messages are separated visibly for the model.  Images and other
    local inputs stay in their original order, so a forwarded caption/image
    pair remains associated with the message that supplied it.
    """
    combined = []
    media_paths = []
    for index, (inputs, paths) in enumerate(entries):
        if index:
            combined.append({"type": "text", "text": "\n\n---\n\n"})
        combined.extend(inputs or [])
        media_paths.extend(paths or [])
    return combined, media_paths


def cancel_pending_batch(runtime):
    """Cancel an idle debounce batch and remove any downloaded media."""
    with process_lock:
        timer = runtime.batch_timer
        runtime.batch_timer = None
        entries = runtime.pending_batch
        runtime.pending_batch = []
    if timer is not None:
        timer.cancel()
    cleanup_media_paths(
        path for _, paths in entries for path in (paths or [])
    )


def flush_pending_batch(runtime, timer):
    """Start the one turn represented by the current debounce window."""
    with process_lock:
        # A canceled timer can still wake up concurrently.  Only the newest
        # timer that is still registered for this chat may consume the batch.
        if runtime.batch_timer is not timer:
            return
        runtime.batch_timer = None
        entries = runtime.pending_batch
        runtime.pending_batch = []
        if not entries:
            return
        already_busy = runtime.busy
        if not already_busy:
            runtime.busy = True

    inputs, media_paths = combine_input_batch(entries)
    if already_busy:
        steered, error = steer_current_turn(runtime, inputs, media_paths)
        if not steered:
            cleanup_media_paths(media_paths)
            send_plain(runtime.chat_id, f"Не получилось добавить сообщения в текущий ход: {error}")
        return

    with state_lock:
        thread_id = chat_state(runtime.chat_id).get("thread_id")
    threading.Thread(
        target=run_turn,
        args=(runtime, inputs, thread_id, media_paths),
        daemon=True,
    ).start()


def queue_message(runtime, inputs, media_paths=None):
    """Debounce idle messages; inject directly when a turn is already live."""
    with process_lock:
        if runtime.busy:
            already_busy = True
            timer = None
        else:
            already_busy = False
            runtime.pending_batch.append((inputs, media_paths or []))
            old_timer = runtime.batch_timer

            def fire():
                flush_pending_batch(runtime, timer)

            timer = threading.Timer(BATCH_DEBOUNCE_S, fire)
            timer.daemon = True
            runtime.batch_timer = timer
    if already_busy:
        steered, error = steer_current_turn(runtime, inputs, media_paths)
        if not steered:
            cleanup_media_paths(media_paths)
            send_plain(runtime.chat_id, f"Не получилось добавить сообщение в текущий ход: {error}")
        return
    if old_timer is not None:
        old_timer.cancel()
    timer.start()


def start_account_login(runtime):
    if runtime.chat_id == OWNER_ID:
        send_plain(runtime.chat_id, "Владелец использует основной аккаунт ~/.codex; отдельный вход не требуется.")
        return
    try:
        client = get_app_server(runtime)
        client.start_if_needed()
        result = client.request(
            "account/login/start", {"type": "chatgptDeviceCode"}, timeout=60,
        ) or {}
        runtime.login_id = result.get("loginId")
        update_state(runtime.chat_id, account_status="awaiting_login")
        send_plain(
            runtime.chat_id,
            "🔐 Подключение отдельного аккаунта Codex\n\n"
            f"1. Открой: {result.get('verificationUrl')}\n"
            f"2. Введи код: {result.get('userCode')}\n\n"
            "Токены сохранятся только в твоём изолированном CODEX_HOME. "
            "Бот сообщит, когда вход завершится.",
        )
    except Exception as exc:
        update_state(runtime.chat_id, account_status="login_failed")
        send_plain(runtime.chat_id, f"Не удалось начать вход в Codex: {compact(str(exc), 500)}")


def account_status_report(runtime):
    try:
        result = get_app_server(runtime).request("account/read", {"refreshToken": False}, timeout=30) or {}
        account = result.get("account") or {}
        if not account:
            update_state(runtime.chat_id, account_status=None)
            return "Аккаунт Codex не подключён. Используй /login."
        update_state(runtime.chat_id, account_status="ready")
        label = account.get("email") or account.get("type") or "подключён"
        plan = account.get("planType") or account.get("plan_type")
        return f"Аккаунт: {label}" + (f"\nПлан: {plan}" if plan else "")
    except Exception as exc:
        return f"Не удалось прочитать аккаунт: {compact(str(exc), 500)}"


def account_is_ready(runtime):
    if runtime.chat_id == OWNER_ID:
        return True
    try:
        result = get_app_server(runtime).request(
            "account/read", {"refreshToken": False}, timeout=30,
        ) or {}
        ready = bool(result.get("account"))
        update_state(runtime.chat_id, account_status="ready" if ready else None)
        return ready
    except Exception as exc:
        log(f"tenant={runtime.chat_id} account readiness check failed: {exc}")
        return False


def handle_command(chat_id, command):
    runtime = get_tenant(chat_id)
    raw_cmd, _, arg = command.partition(" ")
    cmd = raw_cmd.split("@", 1)[0].lower().lstrip("/.")
    arg = arg.strip()
    if cmd in {"new", "resume", "compact", "model", "effort", "mode",
               "workspace", "restart", "stop"}:
        cancel_pending_batch(runtime)
    if cmd in ("start", "help"):
        send_plain(chat_id, "Codex Telegram bridge. Команды доступны в меню бота.")
        return True
    if cmd == "login":
        threading.Thread(target=start_account_login, args=(runtime,), daemon=True).start()
        return True
    if cmd == "account":
        send_plain(chat_id, account_status_report(runtime))
        return True
    if cmd == "new":
        stop_current_process(runtime)
        update_state(chat_id, thread_id=None, last_usage=None, session_usage=None, context_window=None)
        send_plain(chat_id, "🆕 Текущий Codex-тред сброшен. Следующее сообщение начнёт новый.")
        return True
    if cmd == "sessions":
        current = chat_state(chat_id).get("thread_id")
        rows = []
        for path in session_files(chat_id)[:10]:
            sid, preview = session_info(path)
            rows.append(f"{sid[:8]}{' ← текущая' if sid == current else ''}  {preview}")
        send_plain(chat_id, "Последние сессии:\n" + ("\n".join(rows) or "не найдены"))
        return True
    if cmd == "resume":
        matches = [sid for path in session_files(chat_id) for sid, _ in [session_info(path)]
                   if arg and sid.startswith(arg)]
        if len(matches) != 1:
            send_plain(chat_id, "Укажи однозначный id/префикс: /resume <id>" if matches else "Сессия не найдена.")
        else:
            stop_current_process(runtime)
            update_state(chat_id, thread_id=matches[0], last_usage=None, session_usage=None, context_window=None)
            send_plain(chat_id, f"Продолжаю сессию {matches[0][:8]}.")
        return True
    if cmd == "status":
        try:
            selected_model(runtime)
        except Exception as exc:
            log(f"Could not resolve model for status: {exc}")
        with state_lock:
            snapshot = dict(chat_state(chat_id))
        send_plain(chat_id, "ℹ️ Статус\n"
                   f"Сессия: {(snapshot.get('thread_id') or 'нет')[:8]}\n"
                   f"Модель: {snapshot.get('model') or 'не определена'}\n"
                   f"Мощность: {snapshot.get('effort') or 'не определена'}\n"
                   f"Sandbox: {snapshot.get('sandbox')}\n"
                   f"Workspace: {snapshot.get('workspace')}\n"
                   f"Занят: {'да' if (runtime.busy or runtime.pending_batch) else 'нет'}\n"
                   f"Аккаунт: {snapshot.get('account_status') or 'не подключён'}")
        return True
    if cmd == "usage":
        send_plain(chat_id, build_usage_report(runtime))
        return True
    if cmd == "compact":
        with state_lock:
            thread_id = chat_state(chat_id).get("thread_id")
        if not thread_id:
            send_plain(chat_id, "Нет активной сессии для сжатия.")
            return True
        with process_lock:
            if runtime.busy:
                already_busy = True
            else:
                runtime.busy = True
                already_busy = False
        if already_busy:
            send_plain(chat_id, "Сначала дождись завершения текущего хода или используй /stop.")
        else:
            threading.Thread(
                target=run_compaction, args=(runtime, thread_id), daemon=True
            ).start()
        return True
    if cmd == "model":
        try:
            models = available_models(runtime)
            if not arg:
                send_plain(chat_id, render_model_picker(runtime))
                return True
            chosen = next((model for model in models if arg.lower() in {
                model_key(model).lower(), str(model.get("id") or "").lower(),
                str(model.get("displayName") or "").lower(),
            }), None)
            if chosen is None:
                send_plain(chat_id, f"Модель «{arg}» недоступна.\n\n{render_model_picker(runtime)}")
                return True
            supported = [option.get("reasoningEffort") for option in
                         chosen.get("supportedReasoningEfforts", []) if isinstance(option, dict)]
            current_effort = chat_state(chat_id).get("effort")
            effort = (current_effort if current_effort in supported else
                      chosen.get("defaultReasoningEffort") or (supported[0] if supported else None))
            update_state(chat_id, model=model_key(chosen), effort=effort)
            send_plain(chat_id, f"🧠 Модель: {chosen.get('displayName') or model_key(chosen)}\n"
                       f"Мощность: {effort or 'не поддерживается'}")
        except Exception as exc:
            send_plain(chat_id, f"Не удалось получить список моделей: {compact(str(exc), 500)}")
        return True
    if cmd == "effort":
        try:
            models = available_models(runtime)
            chosen = selected_model(runtime, models)
            if not chosen:
                send_plain(chat_id, "Codex не вернул доступных моделей.")
                return True
            options = [option for option in chosen.get("supportedReasoningEfforts", [])
                       if isinstance(option, dict) and option.get("reasoningEffort")]
            if not arg:
                send_plain(chat_id, render_effort_picker(runtime))
                return True
            effort = next((option["reasoningEffort"] for option in options
                           if option["reasoningEffort"].lower() == arg.lower()), None)
            if effort is None:
                send_plain(chat_id, f"Мощность «{arg}» недоступна для {model_key(chosen)}.\n\n"
                           f"{render_effort_picker(runtime)}")
                return True
            update_state(chat_id, effort=effort)
            send_plain(chat_id, f"⚡ Мощность {chosen.get('displayName') or model_key(chosen)}: {effort}")
        except Exception as exc:
            send_plain(chat_id, f"Не удалось получить уровни мощности: {compact(str(exc), 500)}")
        return True
    if cmd == "mode":
        aliases = {"read": "read-only", "read-only": "read-only", "write": "workspace-write",
                   "workspace-write": "workspace-write", "full": "danger-full-access",
                   "danger-full-access": "danger-full-access"}
        if arg not in aliases:
            send_plain(chat_id, "Использование: /mode read-only|workspace-write|full")
        else:
            update_state(chat_id, sandbox=aliases[arg])
            send_plain(chat_id, f"Sandbox: {aliases[arg]}.")
        return True
    if cmd == "workspace":
        path = CODEX_CWD if arg.lower() == "default" else os.path.abspath(os.path.expanduser(arg))
        if not arg:
            send_plain(chat_id, f"Workspace: {chat_state(chat_id).get('workspace')}\nИспользование: /workspace <путь>|default")
        elif not os.path.isdir(path):
            send_plain(chat_id, f"Директория не существует: {path}")
        else:
            update_state(chat_id, workspace=path)
            send_plain(chat_id, f"Workspace: {path}")
        return True
    if cmd == "restart":
        if chat_id != OWNER_ID:
            send_plain(chat_id, "Перезапуск доступен только владельцу бота.")
            return True
        request_restart(chat_id)
        if runtime.busy or runtime.pending_batch:
            send_plain(chat_id, "🔁 Перезапуск запланирован после завершения текущего хода.")
        else:
            send_plain(chat_id, "🔁 Перезапуск запланирован между ходами.")
        return True
    if cmd == "stop":
        running = stop_current_process(runtime)
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
    chat_id = message.get("chat", {}).get("id")
    user_id = message.get("from", {}).get("id")
    if str(user_id) not in load_whitelist():
        if chat_id:
            send_plain(chat_id, "⛔ Доступ к Codex-боту не разрешён. Попроси владельца добавить твой Telegram ID в whitelist.txt.")
        return
    runtime = get_tenant(chat_id)
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
    account_status = chat_state(chat_id).get("account_status")
    if chat_id != OWNER_ID and account_status != "ready" and not account_is_ready(runtime):
        if account_status == "awaiting_login":
            send_plain(chat_id, "Сначала заверши вход в Codex по ранее выданной ссылке.")
        else:
            threading.Thread(target=start_account_login, args=(runtime,), daemon=True).start()
        return
    try:
        inputs, media_paths = message_inputs(message)
    except Exception as exc:
        send_plain(chat_id, f"Не смог скачать изображение: {compact(str(exc), 500)}")
        return
    queue_message(runtime, inputs, media_paths)


def register_commands():
    payload = {"commands": [{"command": c, "description": d} for c, d in COMMANDS]}
    tg_call("setMyCommands", payload)
    tg_call("setMyCommands", {**payload, "scope": {"type": "all_private_chats"}})


def main():
    offset = None
    register_commands()
    with state_lock:
        runtime_state = state_db.get("runtime", {})
        completed_restart_chat_id = runtime_state.get("restart_completed_chat_id")
        completed_restart_message_id = runtime_state.get("restart_message_id")
    if completed_restart_chat_id:
        update_runtime_state(restart_completed_chat_id=None, restart_message_id=None)
        if completed_restart_message_id:
            result = edit_plain(
                completed_restart_chat_id, completed_restart_message_id,
                "✅ Перезагрузка окончена, бот готов к работе.",
            )
            if not result.get("ok"):
                send_plain(completed_restart_chat_id, "✅ Перезагрузка окончена, бот готов к работе.")
        else:
            send_plain(completed_restart_chat_id, "✅ Перезагрузка окончена, бот готов к работе.")
    try:
        owner_runtime = get_tenant(OWNER_ID)
        get_app_server(owner_runtime).start_if_needed()
        refresh_rate_limits(owner_runtime)
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
