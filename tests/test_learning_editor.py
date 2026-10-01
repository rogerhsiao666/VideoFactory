import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import openpyxl

import cards
import learning_editor as editor


def group(ids=None):
    return {"core": "取得書面報價與明細", "scenario": editor.SCENARIOS[1], "source_ids": ids or [1]}


def pair():
    shared = {"core": group()["core"], "Scenario": group()["scenario"], "Tone": "中立",
              "word_cn": "我需要書面報價。", "sentence_cn": "施工前，我需要書面報價。",
              "tips": "中立：對方只口頭說價格時，先要求寫下金額，再確認是否接受施工。",
              "vocab": [{"en": "quote", "cn": "報價"}], "Core_Vocab": "quote 報價"}
    basic = dict(shared, id="01", tier="basic", Level="⭐", progression="",
                 word_en="I need a written quote.", word_ipa="/aɪ nid ə ˈrɪtən koʊt/",
                 sentence_en="I need a written quote before you start.",
                 sentence_ipa="/aɪ nid ə ˈrɪtən koʊt bɪˈfɔr ju stɑrt/")
    advanced = dict(shared, id="02", tier="advanced", Level="⭐⭐", progression="使用片語put in writing",
                    word_en="Put the quote in writing.", word_ipa="/pʊt ðə koʊt ɪn ˈraɪtɪŋ/",
                    sentence_en="Put the quote in writing before I approve the work.",
                    sentence_ipa="/pʊt ðə koʊt ɪn ˈraɪtɪŋ bɪˈfɔr aɪ əˈpruv ðə wɜrk/")
    return [basic, advanced]


class LearningEditorTests(unittest.TestCase):
    def test_json_mode_always_mentions_json_in_messages(self):
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"groups": []}'))])
        with patch.object(cards, "_call_openai", return_value=response) as call:
            self.assertEqual(editor.request_json("請合併句子", "test"), {"groups": []})
        self.assertIn("JSON", call.call_args.kwargs["messages"][0]["content"])

    def test_complete_source_partition_is_required(self):
        source = [pair()[0], pair()[0]]
        editor.validate_groups([group([1, 2])], source)
        for ids in ([1], [1, 1], [0, 2], [True, 2], [1, 3]):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                editor.validate_groups([group(ids)], source)

    def test_assignment_grouping_merges_all_source_rows_and_maps_scenario_ids(self):
        source = [pair()[0], pair()[0]]
        assignments = [{"id": i, "core": group()["core"], "scenario_id": 2} for i in (1, 2)]
        with patch.object(editor, "request_json", side_effect=[{"assignments": assignments}, {"issues": []}]):
            self.assertEqual(editor.group_source("test", source), [group([1, 2])])

    def test_assignment_grouping_rejects_incomplete_duplicate_and_inconsistent_rows(self):
        source = [pair()[0], pair()[0]]
        valid = [{"id": i, "core": group()["core"], "scenario_id": 2} for i in (1, 2)]
        invalids = [valid[:1], [valid[0], valid[0]],
                    [valid[0], dict(valid[1], scenario_id=5)],
                    [valid[0], dict(valid[1], scenario_id=1)],
                    [valid[0], dict(valid[1], core=[])]]
        for invalid in invalids:
            with self.subTest(invalid=invalid), patch.object(editor, "request_json", return_value={"assignments": invalid}):
                with self.assertRaises(RuntimeError):
                    editor.group_source("test", source)

    def test_fee_synonyms_merge_but_total_and_process_remain_distinct(self):
        anchors = [editor.source_intent({"word_en": text}) for text in (
            "What does this charge cover?", "Why is there an extra charge?",
            "Why is this service so expensive?", "Please clarify these extra charges.")]
        self.assertEqual(len(set(anchors)), 1)
        self.assertNotEqual(anchors[0], editor.source_intent({"word_en": "Can you confirm the total cost?"}))
        self.assertNotEqual(editor.source_intent({"word_en": "Can you explain the process simply?"}),
                            editor.source_intent({"word_en": "Can you explain this simply?"}))

    def test_generated_level_is_derived_from_tier(self):
        candidate = pair()
        for item in candidate:
            item.pop("Level")
        with patch.object(editor, "request_json", return_value={"items": candidate}):
            result = editor.generate_pair("test", group(), [pair()[0]])
        self.assertEqual([item["Level"] for item in result], ["⭐", "⭐⭐"])

    def test_tips_prefix_is_derived_from_tone_without_changing_action(self):
        candidate = pair()
        candidate[0]["tips"] = candidate[0]["tips"].split("：", 1)[1]
        candidate[1]["tips"] = candidate[1]["tips"].replace("中立：", "委婉: ")
        with patch.object(editor, "request_json", return_value={"items": candidate}):
            result = editor.generate_pair("test", group(), [pair()[0]])
        self.assertEqual([item["tips"] for item in result], [item["tips"] for item in pair()])

    def test_different_observable_results_cannot_merge(self):
        source = [{"word_en": "Can you confirm the total cost?"},
                  {"word_en": "What does this charge cover?"}]
        with self.assertRaisesRegex(ValueError, "誤合併"):
            editor.validate_groups([group([1, 2])], source)

    def test_quote_synonyms_cannot_split_across_groups(self):
        source = [{"word_en": "I need a written quote."}, {"word_en": "I need an itemized invoice."}]
        second = dict(group([2]), core="索取发票")
        with self.assertRaisesRegex(ValueError, "必須合併"):
            editor.validate_groups([group([1]), second], source)

    def test_conditional_refusal_is_not_a_quote_request(self):
        item = {"word_en": "I won't accept extra repairs."}
        self.assertEqual(editor.source_intent(item), "限定原先同意的服務範圍")
        self.assertEqual(editor.source_intent({"word_en": "I need a detailed breakdown."}), group()["core"])

    def test_pair_requires_one_basic_and_one_advanced(self):
        valid = pair()
        editor.validate_pair(group(), valid)
        for invalid in (valid[:1], valid + [valid[0]], [valid[0], valid[0]]):
            with self.assertRaises(ValueError):
                editor.validate_pair(group(), invalid)

    def test_scene_tone_and_progression_are_required(self):
        for field, value in (("Scenario", "其他"), ("Tone", "禮貌"), ("Level", "⭐⭐⭐"), ("progression", "")):
            invalid = copy.deepcopy(pair())
            invalid[1][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                editor.validate_pair(group(), invalid)

    def test_tips_must_be_actionable_and_tone_labeled(self):
        for tip in ("報價時使用。", "中立：當你想確認報價時使用。", "中立：先要求報價。"):
            invalid = copy.deepcopy(pair())
            invalid[0]["tips"] = tip
            with self.subTest(tip=tip), self.assertRaises(ValueError):
                editor.validate_pair(group(), invalid)

    def test_core_vocab_must_occur_in_spoken_line_and_have_chinese(self):
        for vocab in ([{"en": "authorize", "cn": "授權"}], [{"en": "quote", "cn": "quote"}], []):
            invalid = copy.deepcopy(pair())
            invalid[0]["vocab"] = vocab
            with self.subTest(vocab=vocab), self.assertRaises(ValueError):
                editor.validate_pair(group(), invalid)

    def test_export_keeps_legacy_default_and_learning_fields_are_opt_in(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "learning.xlsx")
            cards.write_xlsx(pair(), path, learning=True)
            workbook = openpyxl.load_workbook(path)
            sheet = workbook.active
            self.assertEqual([c.value for c in sheet[1]], editor.LEARNING_HEADERS)
            self.assertEqual(sheet.auto_filter.ref, "A1:L3")
            self.assertEqual(sheet.freeze_panes, "E2")
            self.assertEqual(sheet["H2"].value, "quote 報價")
            self.assertTrue(sheet["I2"].alignment.wrap_text)
            self.assertGreaterEqual(sheet.row_dimensions[2].height, 45)
            workbook.close()
            basic = pair()[0]
            basic["tips"] = "施工前要求書面報價。"
            cards.write_xlsx([basic], path)
            workbook = openpyxl.load_workbook(path)
            self.assertEqual([c.value for c in workbook.active[1]], cards.HEADERS)
            workbook.close()

    def test_export_rejects_incomplete_learning_pair(self):
        with self.assertRaises(ValueError):
            cards.write_xlsx(pair()[:1], "unused.xlsx", learning=True)

    def test_same_core_pair_review_requests_rewrite_not_cross_core_merge(self):
        reason = "合併：basic與advanced沒有真正進階差異"
        with patch.object(editor, "request_json", return_value={"issues": [{"core": group()["core"], "reason": reason}]}):
            self.assertEqual(editor.review_edition("test", pair()), {group()["core"]: reason})

    def test_cross_core_merge_must_name_another_existing_core(self):
        for other in (group()["core"], "missing"):
            issue = {"core": group()["core"], "reason": "合併：目的重複", "merge_with": other}
            with patch.object(editor, "request_json", return_value={"issues": [issue]}):
                with self.assertRaises(ValueError):
                    editor.review_edition("test", pair())

    def test_resume_requires_exact_source(self):
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "edit.json"
            checkpoint.write_text(json.dumps({"version": editor.VERSION, "topic": "test", "source_sha256": "wrong"}))
            with self.assertRaisesRegex(ValueError, "原始資料"):
                editor.edit_deck("test", [pair()[0]], checkpoint, resume=True)

    def test_resume_revalidates_pair_and_runs_independent_reviews(self):
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "edit.json"
            def semantic_review(topic, items):
                for item in items:
                    item.update(_semantic_group="書面報價", _semantic_level=item["tier"])
                return {}
            with (patch.object(editor, "group_source", return_value=[group()]) as grouping,
                  patch.object(editor, "generate_pair", side_effect=lambda *args: pair()) as generating,
                  patch.object(editor, "review_edition", return_value={}) as review,
                  patch.object(cards, "_ai_review_deck", side_effect=semantic_review)):
                editor.edit_deck("test", [pair()[0]], checkpoint)
                resumed = editor.edit_deck("test", [pair()[0]], checkpoint, resume=True)
            grouping.assert_called_once()
            generating.assert_called_once()
            self.assertEqual(review.call_count, 2)
            self.assertTrue(resumed["review_passed"])
            self.assertEqual(len(resumed["items"]), 2)


if __name__ == "__main__":
    unittest.main()
