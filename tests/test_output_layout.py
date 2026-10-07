import hashlib
import importlib.util
import json
import sys
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

import cards


ROOT = Path(__file__).resolve().parents[1]
SUPPORT = ROOT / "output" / "learning-edit-20261001"


class OutputLayoutTests(unittest.TestCase):
    def setUp(self):
        self.content = json.loads((SUPPORT / "reviewed_content.json").read_text(encoding="utf-8"))
        self.target = ROOT / "output" / (self.content["topic"] + ".xlsx")

    def test_original_source_is_preserved_separately(self):
        source = ROOT / self.content["source"]
        self.assertEqual(source, SUPPORT / "original.xlsx")
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),
                         "90e6876a0188743498304fe3a8b400f1827b2e70f8eea00f8a38f4281e78c50a")
        self.assertEqual(len(cards.load_xlsx_items(str(source))), 50)

    def test_canonical_workbook_matches_reviewed_content(self):
        items = cards.load_xlsx_items(str(self.target))
        state = json.loads(Path(str(self.target) + ".editor.json").read_text(encoding="utf-8"))
        self.assertEqual(len(items), 32)
        self.assertEqual(len(Counter(item["Scenario"] for item in items)), 4)
        self.assertEqual(set(Counter(item["Scenario"] for item in items).values()), {8})
        for actual, expected in zip(items, state["items"]):
            self.assertEqual(len(actual), 12)
            self.assertEqual(actual, {key: expected[key] for key in actual})

    def test_builder_refuses_to_overwrite_canonical_workbook(self):
        spec = importlib.util.spec_from_file_location("reviewed_builder", SUPPORT / "build_reviewed_edition.py")
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        before = hashlib.sha256(self.target.read_bytes()).hexdigest()
        with patch.object(sys, "argv", [str(SUPPORT / "build_reviewed_edition.py")]):
            with patch.object(builder.cards, "write_xlsx") as export, \
                    patch.object(builder.editor, "validate_deck"):
                with self.assertRaisesRegex(ValueError, "\u4fee\u8a02\u7248\u5df2\u5b58\u5728"):
                    builder.main()
                export.assert_not_called()
        self.assertEqual(hashlib.sha256(self.target.read_bytes()).hexdigest(), before)


if __name__ == "__main__":
    unittest.main()
