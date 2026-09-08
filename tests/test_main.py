import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import main


def _write_json(path: Path, word: str) -> None:
    path.write_text(
        json.dumps([{"id": "old", "word_en": word}], ensure_ascii=False),
        encoding="utf-8",
    )


class LocalCardPathTests(unittest.TestCase):
    def test_output_deck_takes_priority_over_legacy_cards_deck(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory) / "output"
            cards_dir = Path(directory) / "cards"
            output_dir.mkdir()
            cards_dir.mkdir()
            _write_json(output_dir / "測試主題.json", "from output")
            _write_json(cards_dir / "測試主題.json", "from cards")

            with (
                patch.object(main, "OUTPUT_DIR", str(output_dir)),
                patch.object(main, "CARDS_DIR", str(cards_dir)),
            ):
                result = main.load_local_cards("測試主題")

        self.assertEqual(result[0]["word_en"], "from output")
        self.assertEqual(result[0]["id"], "01")

    def test_cards_deck_is_used_as_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory) / "output"
            cards_dir = Path(directory) / "cards"
            output_dir.mkdir()
            cards_dir.mkdir()
            _write_json(cards_dir / "舊牌組.json", "legacy")

            with (
                patch.object(main, "OUTPUT_DIR", str(output_dir)),
                patch.object(main, "CARDS_DIR", str(cards_dir)),
            ):
                result = main.load_local_cards("舊牌組")

        self.assertEqual(result[0]["word_en"], "legacy")

    def test_missing_deck_reports_both_search_locations(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory) / "output"
            cards_dir = Path(directory) / "cards"
            output_dir.mkdir()
            cards_dir.mkdir()
            _write_json(output_dir / "output現有.json", "output")
            _write_json(cards_dir / "cards現有.json", "cards")

            stream = StringIO()
            with (
                patch.object(main, "OUTPUT_DIR", str(output_dir)),
                patch.object(main, "CARDS_DIR", str(cards_dir)),
                redirect_stdout(stream),
                self.assertRaises(FileNotFoundError) as raised,
            ):
                main.load_local_cards("不存在")

        message = str(raised.exception)
        self.assertIn(os.path.join(str(output_dir), "不存在.xlsx"), message)
        self.assertIn(os.path.join(str(cards_dir), "不存在.json"), message)
        self.assertIn("output/ 資料夾中可用的卡片", stream.getvalue())
        self.assertIn("cards/ 資料夾中可用的卡片", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
