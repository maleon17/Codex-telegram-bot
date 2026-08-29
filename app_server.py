"""Minimal persistent JSONL client for ``codex app-server``."""

import json
import queue
import subprocess
import threading


class AppServerError(RuntimeError):
    pass


class AppServerClient:
    def __init__(self, notification_handler, log, request_timeout=30):
        self.notification_handler = notification_handler
        self.log = log
        self.request_timeout = request_timeout
        self.process = None
        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._pending = {}
        self._next_id = 1
        self._closed_error = None

    def start(self):
        with self._state_lock:
            if self.process is not None and self.process.poll() is None:
                return
            self._closed_error = None
            self.process = subprocess.Popen(
                ["codex", "app-server", "--listen", "stdio://"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            threading.Thread(target=self._read_stdout, daemon=True).start()
            threading.Thread(target=self._read_stderr, daemon=True).start()
        self.request("initialize", {
            "clientInfo": {"name": "codex-telegram-bot", "version": "0.2.0"},
            "capabilities": {"experimentalApi": True},
        })
        self.notify("initialized")

    def close(self):
        with self._state_lock:
            process = self.process
            self.process = None
        if process is not None and process.poll() is None:
            process.terminate()

    def notify(self, method, params=None):
        message = {"method": method}
        if params is not None:
            message["params"] = params
        self._write(message)

    def request(self, method, params=None, timeout=None):
        self.start_if_needed()
        response_queue = queue.Queue(maxsize=1)
        with self._state_lock:
            request_id = self._next_id
            self._next_id += 1
            self._pending[request_id] = response_queue
        message = {"id": request_id, "method": method, "params": params or {}}
        try:
            self._write(message)
            response = response_queue.get(timeout=timeout or self.request_timeout)
        except queue.Empty as exc:
            raise AppServerError(f"app-server request timed out: {method}") from exc
        finally:
            with self._state_lock:
                self._pending.pop(request_id, None)
        if isinstance(response, Exception):
            raise response
        if "error" in response:
            error = response["error"]
            message = error.get("message", error) if isinstance(error, dict) else error
            raise AppServerError(f"{method}: {message}")
        return response.get("result")

    def start_if_needed(self):
        with self._state_lock:
            alive = self.process is not None and self.process.poll() is None
        if not alive:
            self.start()

    def _write(self, message):
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        with self._write_lock:
            process = self.process
            if process is None or process.poll() is not None or process.stdin is None:
                raise AppServerError(self._closed_error or "app-server is not running")
            process.stdin.write(payload + "\n")
            process.stdin.flush()

    def _read_stdout(self):
        process = self.process
        try:
            for raw_line in process.stdout:
                try:
                    message = json.loads(raw_line)
                except Exception as exc:
                    self.log(f"app-server JSON parse error: {exc}")
                    continue
                request_id = message.get("id")
                if request_id is not None and ("result" in message or "error" in message):
                    with self._state_lock:
                        target = self._pending.get(request_id)
                    if target is not None:
                        target.put(message)
                elif request_id is not None and message.get("method"):
                    self._reply_to_server_request(message)
                elif message.get("method"):
                    try:
                        self.notification_handler(message["method"], message.get("params") or {})
                    except Exception as exc:
                        self.log(f"app-server notification handler failed: {exc}")
        finally:
            code = process.poll()
            self._fail_pending(AppServerError(f"app-server exited with status {code}"))

    def _read_stderr(self):
        process = self.process
        for line in process.stderr:
            line = line.rstrip()
            if line:
                self.log(f"app-server: {line}")

    def _reply_to_server_request(self, message):
        method = message.get("method", "")
        self.log(f"Declining unexpected app-server request: {method}")
        self._write({"id": message["id"], "result": {"decision": "decline"}})

    def _fail_pending(self, error):
        with self._state_lock:
            self._closed_error = str(error)
            pending = list(self._pending.values())
        for target in pending:
            try:
                target.put_nowait(error)
            except queue.Full:
                pass
