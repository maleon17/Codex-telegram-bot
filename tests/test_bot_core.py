import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
TEMP = tempfile.TemporaryDirectory()
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:test")
os.environ.setdefault("OWNER_ID", "1")
os.environ["CODEX_BOT_STATE_FILE"] = str(Path(TEMP.name) / "state.json")
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("codex_telegram_bot", ROOT / "bot.py")
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)


class RenderingTests(unittest.TestCase):
    def test_tool_progress_uses_concrete_claude_style_labels(self):
        label, content, _ = bot.item_label_and_blocks({
            "type": "command_execution", "command": "printf hello",
        })
        self.assertEqual(label, "🔧 Bash")
        self.assertEqual(content, "printf hello")
        self.assertNotIn("Выполняю", label)

        label, content, _ = bot.item_label_and_blocks({
            "type": "mcp_tool_call", "server": "telegram", "tool": "lookup",
        })
        self.assertEqual(label, "🔧 telegram.lookup")
        self.assertEqual(content, "")
        self.assertNotIn("выполняется", content)

        label, content, _ = bot.item_label_and_blocks({
            "type": "future_tool", "payload": "opaque",
        })
        self.assertEqual(label, "🔧 Инструмент")
        self.assertNotIn("Действие Codex", label)

    def test_file_change_draft_never_exposes_patch(self):
        item = {
            "type": "file_change",
            "changes": [{
                "path": "/tmp/README.md",
                "kind": {"type": "update", "diff": "PRIVATE PATCH CONTENT"},
            }],
        }
        label, content, blocks = bot.item_label_and_blocks(item)
        self.assertEqual(label, "📝 Изменение файла")
        self.assertEqual(content, "/tmp/README.md — изменён")
        self.assertNotIn("PRIVATE PATCH CONTENT", content)
        self.assertEqual(blocks, [])

    def test_truncated_code_block_stays_balanced(self):
        rendered = bot.truncate_mdv2("```\n" + ("x" * 5000), 100)
        self.assertLessEqual(len(rendered), 100)
        self.assertEqual(rendered.count("```") % 2, 0)

    def test_process_state_keeps_thought_until_next_thought(self):
        view = bot.TurnView(1)
        view.add_event({"type": "item.completed", "item": {
            "type": "reasoning", "id": "reason-1", "text": "Сначала проверю файл",
        }})
        view.add_event({"type": "item.started", "item": {
            "type": "command_execution", "id": "tool-1", "command": "printf one",
        }})
        view.add_event({"type": "item.completed", "item": {
            "type": "command_execution", "id": "tool-1", "command": "printf one",
            "aggregated_output": "one", "exit_code": 0,
        }})
        text = view.live_text()
        self.assertLess(text.index("Сначала проверю файл"), text.index("🔧 Bash"))
        self.assertLess(text.index("🔧 Bash"), text.index("one"))

        view.add_event({"type": "item.started", "item": {
            "type": "command_execution", "id": "tool-2", "command": "printf two",
        }})
        text = view.live_text()
        self.assertIn("Сначала проверю файл", text)
        self.assertIn("printf two", text)
        self.assertNotIn("one", text)

        view.add_thought_delta("Теперь отвечу", item_id="reason-2")
        text = view.live_text()
        self.assertIn("Теперь отвечу", text)
        self.assertNotIn("Сначала проверю файл", text)
        self.assertNotIn("printf two", text)

    def test_progress_uses_one_editable_telegram_message(self):
        calls = []
        original_tg_call = bot.tg_call

        def fake_tg_call(method, params=None, timeout=bot.HTTP_TIMEOUT_S):
            calls.append((method, params or {}))
            return {"ok": True, "result": {"message_id": 42}}

        bot.tg_call = fake_tg_call
        try:
            view = bot.TurnView(1)
            view.flush(force=True)
            view.add_thought_delta("Проверяю", item_id="reason-1")
            view.flush(force=True)
        finally:
            bot.tg_call = original_tg_call

        self.assertEqual([method for method, _ in calls], ["sendMessage", "editMessageText"])
        self.assertEqual(calls[1][1]["message_id"], 42)
        self.assertIn("🤔", calls[0][1]["text"])
        self.assertIn("Проверяю", calls[1][1]["text"])
        self.assertNotIn("sendMessageDraft", [method for method, _ in calls])

    def test_batch_inputs_preserve_order_and_separate_messages(self):
        inputs, paths = bot.combine_input_batch([
            ([{"type": "text", "text": "первое"}], ["/tmp/one.jpg"]),
            ([{"type": "text", "text": "второе"},
              {"type": "localImage", "path": "/tmp/two.jpg"}], ["/tmp/two.jpg"]),
        ])
        self.assertEqual(paths, ["/tmp/one.jpg", "/tmp/two.jpg"])
        self.assertEqual(
            inputs,
            [
                {"type": "text", "text": "первое"},
                {"type": "text", "text": "\n\n---\n\n"},
                {"type": "text", "text": "второе"},
                {"type": "localImage", "path": "/tmp/two.jpg"},
            ],
        )

    def test_forwarded_text_preserves_origin_as_prompt_context(self):
        inputs, paths = bot.message_inputs({
            "text": "Проверь это",
            "forward_origin": {
                "type": "user",
                "sender_user": {"first_name": "Андрей", "username": "andrey"},
            },
        })
        self.assertEqual(paths, [])
        self.assertEqual(len(inputs), 1)
        self.assertIn("Пересланное сообщение от Андрей (@andrey)", inputs[0]["text"])
        self.assertIn("Проверь это", inputs[0]["text"])

    def test_forwarded_rich_message_is_converted_to_prompt_text(self):
        inputs, paths = bot.message_inputs({
            "forward_origin": {"type": "hidden_user", "sender_user_name": "Автор"},
            "rich_message": {"markdown": "**Ответ из другого чата**\n\nПроверь это."},
        })
        self.assertEqual(paths, [])
        self.assertEqual(
            inputs,
            [{"type": "text", "text": "[Пересланное сообщение от Автор]\n\n**Ответ из другого чата**\n\nПроверь это."}],
        )

    def test_direct_rich_message_is_not_filtered_out(self):
        queued = []
        with patch.object(bot, "queue_message", side_effect=lambda runtime, inputs, paths: queued.append(inputs)):
            bot.handle_message({
                "chat": {"id": bot.OWNER_ID},
                "from": {"id": bot.OWNER_ID},
                "rich_message": {
                    "blocks": [{"type": "paragraph", "text": [{"type": "bold", "text": "Ответ"}]}],
                },
            })
        self.assertEqual(len(queued), 1)
        self.assertIn("**Ответ**", queued[0][0]["text"])

    def test_forwarded_non_image_media_is_not_silently_dropped(self):
        inputs, paths = bot.message_inputs({
            "voice": {"duration": 4, "mime_type": "audio/ogg"},
            "forward_origin": {
                "type": "hidden_user", "sender_user_name": "Скрытый автор",
            },
        })
        self.assertEqual(paths, [])
        self.assertEqual(len(inputs), 1)
        self.assertIn("Скрытый автор", inputs[0]["text"])
        self.assertIn("голосовое сообщение", inputs[0]["text"])

    def test_forwarded_command_text_is_queued_as_data(self):
        queued = []
        with patch.object(bot, "handle_command", side_effect=AssertionError("forward became a command")), \
                patch.object(bot, "queue_message", side_effect=lambda runtime, inputs, paths: queued.append(inputs)):
            bot.handle_message({
                "chat": {"id": bot.OWNER_ID},
                "from": {"id": bot.OWNER_ID},
                "text": "/stop",
                "forward_origin": {"type": "hidden_user", "sender_user_name": "автор"},
            })
        self.assertEqual(len(queued), 1)
        self.assertIn("/stop", queued[0][0]["text"])

    def test_steer_is_internal_only(self):
        self.assertNotIn("steer", [command for command, _ in bot.COMMANDS])

    def test_queue_message_debounces_until_one_turn(self):
        timers = []
        started = []

        class FakeTimer:
            def __init__(self, interval, function):
                self.interval = interval
                self.function = function
                self.cancelled = False
                timers.append(self)

            def start(self):
                started.append(self)

            def cancel(self):
                self.cancelled = True

        class FakeThread:
            def __init__(self, target, args=(), daemon=None):
                self.target = target
                self.args = args

            def start(self):
                self.target(*self.args)

        launched = []
        runtime = bot.TenantRuntime(1)
        with patch.object(bot.threading, "Timer", FakeTimer), \
                patch.object(bot.threading, "Thread", FakeThread), \
                patch.object(bot, "run_turn", lambda *args: launched.append(args)):
            bot.queue_message(runtime, [{"type": "text", "text": "раз"}], [])
            bot.queue_message(runtime, [{"type": "text", "text": "два"}], [])
            self.assertEqual(len(runtime.pending_batch), 2)
            self.assertTrue(timers[0].cancelled)
            timers[-1].function()

        self.assertEqual(len(launched), 1)
        self.assertEqual(
            launched[0][1],
            [
                {"type": "text", "text": "раз"},
                {"type": "text", "text": "\n\n---\n\n"},
                {"type": "text", "text": "два"},
            ],
        )
        self.assertFalse(runtime.pending_batch)

    def test_active_messages_are_combined_before_one_steer(self):
        timers = []
        steered = []

        class FakeTimer:
            def __init__(self, interval, function):
                self.interval = interval
                self.function = function
                self.cancelled = False
                timers.append(self)

            def start(self):
                pass

            def cancel(self):
                self.cancelled = True

        runtime = bot.TenantRuntime(1)
        runtime.busy = True
        with patch.object(bot.threading, "Timer", FakeTimer), \
                patch.object(bot, "steer_current_turn",
                             side_effect=lambda runtime, inputs, paths: (
                                 steered.append((inputs, paths)) or (True, None))):
            bot.queue_message(runtime, [{"type": "text", "text": "раз"}], [])
            bot.queue_message(runtime, [{"type": "text", "text": "два"}], [])
            self.assertEqual(len(runtime.pending_batch), 2)
            timers[-1].function()

        self.assertEqual(len(steered), 1)
        self.assertEqual(
            steered[0][0],
            [
                {"type": "text", "text": "раз"},
                {"type": "text", "text": "\n\n---\n\n"},
                {"type": "text", "text": "два"},
            ],
        )
        self.assertFalse(runtime.pending_batch)

    def test_batch_waits_for_turn_id_instead_of_dropping_messages(self):
        timers = []

        class FakeTimer:
            def __init__(self, interval, function):
                self.function = function
                self.cancelled = False
                timers.append(self)

            def start(self):
                pass

            def cancel(self):
                self.cancelled = True

        runtime = bot.TenantRuntime(1)
        runtime.busy = True
        with patch.object(bot.threading, "Timer", FakeTimer), \
                patch.object(bot, "steer_current_turn", return_value=(False, "ещё запускается")):
            bot.queue_message(runtime, [{"type": "text", "text": "не теряй"}], [])
            timers[-1].function()

        self.assertEqual(len(runtime.pending_batch), 1)
        self.assertEqual(runtime.pending_batch[0][0][0]["text"], "не теряй")
        self.assertIs(runtime.batch_timer, timers[-1])

    def test_empty_reasoning_is_not_rendered_as_a_blank_step(self):
        self.assertEqual(bot.render_process_item({"type": "reasoning", "summary": []}), "")

    def test_usage_limit_has_actionable_message(self):
        message = bot.user_facing_codex_error({
            "message": "usage limit reached",
            "codexErrorInfo": "usageLimitExceeded",
        })
        self.assertIn("/usage", message)

    def test_every_app_server_tool_has_a_bounded_human_renderer(self):
        fixtures = [
            ({"type": "web_search", "id": "private-id", "query": "OpenAI",
              "action": {"type": "search"}, "results": [{"opaque": "RAW"}]}, "OpenAI"),
            ({"type": "mcp_tool_call", "id": "private-id", "server": "telegram",
              "tool": "lookup", "arguments": {"query": "Андрей"},
              "result": {"content": [{"type": "text", "text": "Найден"}],
                         "structuredContent": {"opaque": "RAW"}}}, "telegram.lookup"),
            ({"type": "dynamic_tool_call", "id": "private-id", "namespace": "demo",
              "tool": "run", "arguments": {"x": 1},
              "contentItems": [{"type": "text", "text": "готово"}]}, "demo.run"),
            ({"type": "collab_agent_tool_call", "id": "private-id", "tool": "spawn",
              "prompt": "проверить модуль", "receiverThreadIds": ["private-thread"]}, "spawn"),
            ({"type": "sub_agent_activity", "id": "private-id", "kind": "waiting",
              "agentThreadId": "private-thread"}, "waiting"),
            ({"type": "image_view", "id": "private-id", "path": "/tmp/image.png"}, "image.png"),
            ({"type": "image_generation", "id": "private-id"}, "изображение"),
            ({"type": "context_compaction", "id": "private-id"}, "контекст"),
            ({"type": "plan", "id": "private-id", "text": "Шаг 1"}, "Шаг 1"),
            ({"type": "sleep", "id": "private-id", "durationMs": 1500}, "1.5"),
            ({"type": "entered_review_mode", "id": "private-id"}, "проверки"),
            ({"type": "futureTool", "id": "private-id", "payload": "RAW"}, "futureTool"),
        ]
        for item, expected in fixtures:
            with self.subTest(item=item["type"]):
                label, content, results = bot.item_label_and_blocks(item)
                rendered = "\n".join([label, content] + [value for _, value in results])
                self.assertIn(expected, rendered)
                self.assertNotIn("private-id", rendered)
                self.assertNotIn("private-thread", rendered)
                self.assertNotIn('"type":', rendered)
                self.assertLess(len(rendered), 4000)


class ModelCommandTests(unittest.TestCase):
    CATALOG = [
        {"id": "gpt-5.6-sol", "model": "gpt-5.6-sol", "displayName": "GPT-5.6-Sol",
         "isDefault": True, "hidden": False, "defaultReasoningEffort": "low",
         "supportedReasoningEfforts": [
             {"reasoningEffort": "low", "description": "Fast"},
             {"reasoningEffort": "high", "description": "Deep"},
         ]},
        {"id": "gpt-5.6-luna", "model": "gpt-5.6-luna", "displayName": "GPT-5.6-Luna",
         "isDefault": False, "hidden": False, "defaultReasoningEffort": "high",
         "supportedReasoningEfforts": [
             {"reasoningEffort": "high", "description": "Deep"},
         ]},
    ]

    def setUp(self):
        self.original_models = bot.available_models
        self.original_send = bot.send_plain
        bot.available_models = lambda runtime: self.CATALOG
        self.messages = []
        bot.send_plain = lambda chat_id, text: self.messages.append(text)
        bot.update_state(1, model=None, effort=None)

    def tearDown(self):
        bot.available_models = self.original_models
        bot.send_plain = self.original_send

    def test_model_without_argument_lists_real_models_and_selects_actual_default(self):
        bot.handle_command(1, "/model")
        text = self.messages[-1]
        self.assertIn("/model gpt-5.6-sol", text)
        self.assertIn("/model gpt-5.6-luna", text)
        self.assertNotIn("default", text.lower())
        self.assertEqual(bot.chat_state(1)["model"], "gpt-5.6-sol")
        self.assertEqual(bot.chat_state(1)["effort"], "low")

    def test_effort_without_argument_lists_only_current_model_levels(self):
        bot.handle_command(1, "/model gpt-5.6-sol")
        bot.handle_command(1, "/effort")
        self.assertIn("/effort low", self.messages[-1])
        self.assertIn("/effort high", self.messages[-1])
        bot.handle_command(1, "/effort high")
        self.assertEqual(bot.chat_state(1)["effort"], "high")


if __name__ == "__main__":
    unittest.main()
