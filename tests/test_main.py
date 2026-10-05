import json
import asyncio
import os
import tempfile
import unittest
from contextlib import redirect_stdout, ExitStack
from io import StringIO
from pathlib import Path
from unittest.mock import patch, Mock

import main
from artifact_paths import VIDEO_HEADERS, cached_artifact, preserve_legacy_cache


def _write_json(path: Path, word: str) -> None:
    path.write_text(
        json.dumps([{"id": "old", "word_en": word}], ensure_ascii=False),
        encoding="utf-8",
    )


class LocalCardPathTests(unittest.TestCase):
    def test_checkpoints_and_snapshots_are_not_listed_as_decks(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory) / "output"
            output_dir.mkdir()
            _write_json(output_dir / "deck.json", "real card")
            for name in ("data.json", "deck.plan.json", "deck.xlsx.curriculum.json",
                         "deck.xlsx.pairs.json", "deck.xlsx.planning.json", "review_deck.xlsx"):
                (output_dir / name).touch()
            stream = StringIO()
            with patch.object(main, "OUTPUT_DIR", str(output_dir)), \
                    patch.object(main, "CARDS_DIR", str(Path(directory) / "cards")), \
                    redirect_stdout(stream), self.assertRaises(FileNotFoundError):
                main.load_local_cards("missing")
            self.assertIn("deck.json", stream.getvalue())
            self.assertNotIn("data.json", stream.getvalue())
            self.assertNotIn("planning.json", stream.getvalue())
            self.assertNotIn("review_deck.xlsx", stream.getvalue())

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
    def _timing_entries(self, card_count=50):
        chapters = [(0.0, "Intro")]
        chapters.extend((i * 60.0, f"{i + 1:02d} - Test") for i in range(card_count))
        chapters.append((card_count * 60.0, "Break"))
        chapters.extend(
            ((card_count + i) * 60.0, f"🔄 {i + 1:02d} - Test")
            for i in range(card_count)
        )
        chapters.append((card_count * 120.0, "Outro"))
        return chapters, [(i * 60.0, "test") for i in range(card_count * 2)]

    def test_existing_description_updates_progress_and_preserves_content(self):
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
                *self._timing_entries(),
                str(output_path),
            )
            description = output_path.read_text(encoding="utf-8")

        self.assertIn("00:00 開始學習！", description)
        self.assertIn("25:00 25%繼續加油！", description)
        self.assertIn("50:00 50% 再複習一次  GO! GO!", description)
        self.assertIn("75:00 75% 最後衝刺！", description)
        self.assertNotIn("09:10 25%繼續加油！", description)
        self.assertIn("📑 完整章節", description)
        self.assertIn("01 - Test sentence.", description)
        self.assertIn("測試標題", description)
        self.assertIn("測試文案", description)
        self.assertIn("#英文學習", description)

    def test_new_description_includes_progress_without_full_chapters(self):
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "youtube_測試.txt"
            with (
                patch.object(main, "_generate_yt_title", return_value="測試標題"),
                patch.object(main, "_generate_yt_topic_paragraph", return_value="測試文案"),
                patch.object(main, "_generate_yt_hashtags", return_value=["測試標籤"]),
            ):
                main.write_youtube_description(
                    "測試",
                    *self._timing_entries(),
                    str(output_path),
                )
            description = output_path.read_text(encoding="utf-8")

        self.assertIn("00:00 開始學習！", description)
        self.assertIn("25:00 25%繼續加油！", description)
        self.assertIn("50:00 50% 再複習一次  GO! GO!", description)
        self.assertIn("75:00 75% 最後衝刺！", description)
        self.assertNotIn("📑 完整章節", description)
        self.assertNotIn("01 - Test sentence.", description)
        self.assertIn("測試標題", description)

    def test_missing_progress_is_inserted_once_without_regenerating_metadata(self):
        original = (
            "原有標題\n\n原有文案\n\nChrome 插件：https://example.com/\n\n"
            "✅ 訂閱頻道並開啟小鈴鐺\n\n#英文學習\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "youtube_測試.txt"
            output_path.write_text(original, encoding="utf-8")
            with patch.object(main, "_generate_yt_title") as title_generator:
                main.write_youtube_description("測試", *self._timing_entries(), str(output_path))
                first = output_path.read_text(encoding="utf-8")
                main.write_youtube_description("測試", *self._timing_entries(), str(output_path))
                second = output_path.read_text(encoding="utf-8")
            title_generator.assert_not_called()

        self.assertEqual(first, second)
        self.assertEqual(first.count("00:00 開始學習！"), 1)
        self.assertIn("25:00 25%繼續加油！", first)
        self.assertIn("原有標題\n\n原有文案", first)
        self.assertIn("#英文學習", first)
        self.assertLess(first.index("Chrome 插件"), first.index("開始學習"))
        self.assertLess(first.index("最後衝刺"), first.index("✅ 訂閱"))

    def test_progress_adapts_to_card_count(self):
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "youtube_測試.txt"
            output_path.write_text("原有文案\n", encoding="utf-8")
            main.write_youtube_description("測試", *self._timing_entries(4), str(output_path))
            description = output_path.read_text(encoding="utf-8")
        self.assertIn("02:00 25%繼續加油！", description)
        self.assertIn("04:00 50% 再複習一次  GO! GO!", description)
        self.assertIn("06:00 75% 最後衝刺！", description)

    def test_empty_subtitles_use_placeholder_times(self):
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "youtube_測試.txt"
            output_path.write_text("原有文案\n", encoding="utf-8")
            main.write_youtube_description("測試", [], [], str(output_path))
            description = output_path.read_text(encoding="utf-8")
        self.assertEqual(description.count("00:00 "), 4)


class VideoContractTests(unittest.TestCase):
    def card(self):
        return dict(zip(VIDEO_HEADERS, ("01", "Can you help me?", "/kæn ju hɛlp mi/", "你能幫我嗎？",
                                      "當需要協助時，直接開口。", "Can you help me with this?", "你能幫我處理這個嗎？")))

    def test_review_excel_exports_only_video_fields_in_cache(self):
        card = self.card()
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(main, "BASE_DIR", directory), \
                patch.object(main, "OUTPUT_DIR", str(Path(directory) / "output")):
            path = main.export_review_excel([dict(card, sentence_ipa="unused", Core_Vocab="unused")], "test")
            self.assertTrue(Path(path).is_relative_to(Path(directory) / "temp" / "cache" / "reviews"))
            self.assertEqual(main.import_review_excel(path), [card])
            self.assertEqual(list(Path(directory, "output").iterdir()), [])

    def test_cache_identity_uses_full_path_and_legacy_copy_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "one" / "same.xlsx.curriculum.json"
            other = Path(directory) / "two" / source.name
            cached = cached_artifact(source, directory, "curriculum")
            self.assertNotEqual(cached, cached_artifact(other, directory, "curriculum"))
            source.parent.mkdir()
            source.write_text("original")
            preserve_legacy_cache(source, cached)
            self.assertEqual(source.read_text(), "original")
            self.assertEqual(cached.read_text(), "original")
            cached.write_text("new progress")
            preserve_legacy_cache(source, cached)
            self.assertEqual(cached.read_text(), "new progress")

    def test_seven_fields_render_both_main_and_example_images(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(main, "_create_base_image", side_effect=lambda _: Image.new("RGB", (1920, 1080), "black")):
            for renderer in (main.create_word_card_image, main.create_sentence_card_image):
                path = Path(directory) / (renderer.__name__ + ".png")
                renderer(self.card(), str(path), [])
                with Image.open(path) as rendered:
                    self.assertEqual(rendered.size, (1920, 1080))
                    self.assertIsNotNone(rendered.getbbox())

    def test_video_workflow_keeps_timing_with_subtitles_off_and_on(self):
        for subtitles in (False, True):
            with self.subTest(subtitles=subtitles), tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                root = Path(directory)
                output = root / "output"
                output.mkdir()
                stack.enter_context(redirect_stdout(StringIO()))
                for name, value in (("TEMP_DIR", directory), ("OUTPUT_DIR", str(output)),
                                    ("BASE_DIR", directory), ("DATA_FILE", str(root / "snapshot.json")),
                                    ("INTRO_VIDEO", str(root / "no-intro.mp4")),
                                    ("BREAK_VIDEO", str(root / "no-break.mp4")),
                                    ("OUTRO_VIDEO", str(root / "no-outro.mp4")), ("PEXELS_KEY", "")):
                    stack.enter_context(patch.object(main, name, value))
                stack.enter_context(patch("builtins.input", side_effect=["Test", "", "2", "", "", ""]))
                stack.enter_context(patch.object(main, "route_input", return_value=("", "normal")))
                stack.enter_context(patch.object(main, "_flush_stdin"))
                stack.enter_context(patch.object(main, "check_assets", return_value=True))
                stack.enter_context(patch.object(main, "load_local_cards", return_value=[self.card()]))

                async def group(data, group_idx, images, cumulative, timing, chapters, **kwargs):
                    timing.append((cumulative, cumulative + 10, "English", "中文"))
                    chapters.append((cumulative, "Test"))
                    return [str(root / "chunk.mp4")], cumulative + 10

                stack.enter_context(patch.object(main, "process_group", side_effect=group))
                def ffmpeg(*args, **kwargs):
                    (root / "merged_no_bgm.mp4").write_bytes(b"offline simulated video")
                    return Mock(returncode=0)
                stack.enter_context(patch.object(main.subprocess, "run", side_effect=ffmpeg))
                srt = stack.enter_context(patch.object(main, "write_srt"))
                youtube = stack.enter_context(patch.object(main, "write_youtube_description"))
                asyncio.run(main.main(subtitles=True) if subtitles else main.main())
                self.assertEqual(srt.call_count, int(subtitles))
                youtube.assert_called_once()
                self.assertEqual(len(youtube.call_args.args[2]), 2)
                self.assertTrue((output / "final_test.mp4").exists())
                self.assertFalse((output / "data.json").exists())
                self.assertEqual(len(list(output.iterdir())), 1)


if __name__ == "__main__":
    unittest.main()
