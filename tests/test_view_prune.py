"""Generated navigation pages must not outlive the cards they describe.

A folder that loses its last card used to keep its generated `总览.md` forever,
still linking at files that no longer exist. The prune is deliberately narrow:
only a page refresh itself wrote, unchanged since, is removed.
"""
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tongle.engine import Engine
from tongle.evolution import Evolution
from tongle.views import refresh


class ViewPruneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.e = Engine(self.root / "state")
        self.e.init(self.root / "vault")
        self.addCleanup(self.e.store.close)
        (self.e.home / "config.json").write_text(
            json.dumps({"knowledge_mode": "evolving", "model_command": ["stub"]}))
        self.ev = Evolution(self.e)
        self.vault = self.root / "vault"

    def _page_for(self, folder):
        return self.vault / folder / "总览.md"

    def test_page_is_pruned_when_its_folder_loses_its_last_card(self):
        card = self.ev.cards.seed("p", "甲", "memory", "方案", "甲", "正文。",
                                 path="项目/甲项目/甲.md")
        refresh(self.e)
        page = self._page_for("项目/甲项目")
        self.assertTrue(page.is_file())
        row = self.e.db.execute(
            "SELECT 1 FROM knowledge_views WHERE path='项目/甲项目/总览.md'").fetchone()
        self.assertIsNotNone(row)
        # The card moves out of that folder: the page now describes nothing.
        self.ev.cards.set_path(card["id"], card["version"], "项目/乙项目/甲.md")
        result = refresh(self.e)
        self.assertNotIn("项目/甲项目/总览.md", result["updated"])
        self.assertIn("项目/甲项目/总览.md", result["pruned"])
        self.assertFalse(page.exists())
        self.assertIsNone(self.e.db.execute(
            "SELECT 1 FROM knowledge_views WHERE path='项目/甲项目/总览.md'").fetchone())
        self.assertTrue(self._page_for("项目/乙项目").is_file())

    def test_hand_edited_page_in_a_cardless_folder_is_kept(self):
        card = self.ev.cards.seed("p", "甲", "memory", "方案", "甲", "正文。",
                                 path="项目/甲项目/甲.md")
        refresh(self.e)
        page = self._page_for("项目/甲项目")
        page.write_text("人工写的内容", encoding="utf8")
        self.ev.cards.set_path(card["id"], card["version"], "项目/乙项目/甲.md")
        result = refresh(self.e)
        self.assertNotIn("项目/甲项目/总览.md", result["pruned"])
        self.assertEqual(page.read_text(), "人工写的内容")
        self.assertIsNotNone(self.e.db.execute(
            "SELECT 1 FROM knowledge_views WHERE path='项目/甲项目/总览.md'").fetchone())

    def test_page_with_cards_left_is_never_pruned(self):
        self.ev.cards.seed("p", "甲", "memory", "方案", "甲", "正文。",
                           path="项目/甲项目/甲.md")
        refresh(self.e)
        result = refresh(self.e)
        self.assertEqual(result["pruned"], [])
        self.assertTrue(self._page_for("项目/甲项目").is_file())

    def test_root_pages_are_never_pruned(self):
        refresh(self.e)
        result = refresh(self.e)
        self.assertEqual(result["pruned"], [])
        for name in ("首页.md", "运行情况.md", "使用说明.md"):
            self.assertTrue((self.vault / name).is_file())


if __name__ == "__main__":
    unittest.main()
