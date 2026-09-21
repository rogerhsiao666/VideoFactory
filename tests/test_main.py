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


class YouTubeDescriptionTests(unittest.TestCase):
    def test_existing_description_has_legacy_timeline_removed(self):
        legacy = """測試標題

測試文案

00:00 開始學習！
09:10 25%繼續加油！
19:04 50% 再複習一次  GO! GO!
25:37 75% 最後衝刺！

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📑 完整章節
00:00 Intro
00:02 01 - Test sentence.
32:38 Outro
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

#英文學習
"""
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "youtube_測試.txt"
            output_path.write_text(legacy, encoding="utf-8")

            main.write_youtube_description(
                "測試",
                [(0.0, "Intro"), (2.0, "01 - Test sentence.")],
                [(0.0, "test")],
                str(output_path),
            )
            description = output_path.read_text(encoding="utf-8")

        self.assertNotIn("00:00 開始學習！", description)
        self.assertNotIn("25%繼續加油！", description)
        self.assertNotIn("📑 完整章節", description)
        self.assertNotIn("01 - Test sentence.", description)
        self.assertIn("測試標題", description)
        self.assertIn("測試文案", description)
        self.assertIn("#英文學習", description)

    def test_new_description_does_not_include_timeline(self):
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "youtube_測試.txt"
            with (
                patch.object(main, "_generate_yt_title", return_value="測試標題"),
                patch.object(main, "_generate_yt_topic_paragraph", return_value="測試文案"),
                patch.object(main, "_generate_yt_hashtags", return_value=["測試標籤"]),
            ):
                main.write_youtube_description(
                    "測試",
                    [(0.0, "Intro"), (2.0, "01 - Test sentence.")],
                    [(0.0, "test")],
                    str(output_path),
                )
            description = output_path.read_text(encoding="utf-8")

        self.assertNotIn("00:00 開始學習！", description)
        self.assertNotIn("📑 完整章節", description)
        self.assertNotIn("01 - Test sentence.", description)
        self.assertIn("測試標題", description)


if __name__ == "__main__":
    unittest.main()
