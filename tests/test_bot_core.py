import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main()
