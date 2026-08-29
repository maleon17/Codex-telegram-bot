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


if __name__ == "__main__":
    unittest.main()
