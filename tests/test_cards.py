import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import cards


def _item(word_en: str, sentence_en: str, purpose_id: int = 1) -> dict:
    return {
        "_purpose_id": purpose_id,
        "word_en": word_en,
        "word_ipa": "/tɛst/",
        "word_cn": "測試",
        "tips": "現場直接使用。",
        "sentence_en": sentence_en,
        "sentence_ipa": "/tɛst/",
        "sentence_cn": "測試句。",
    }


class ReferenceDeckTests(unittest.TestCase):
    def test_exact_reference_phrase_is_rejected(self):
        candidate = _item(
            "Leave enough to tie it back.",
            "Please leave it long enough for a ponytail.",
        )
        reference = dict(candidate, _source_deck="美髮沙龍_02")

        reason = cards._reference_duplicate_reason(candidate, [reference])

        self.assertIn("美髮沙龍_02", reason)

    def test_near_reference_sentence_is_rejected(self):
        candidate = _item(
            "Keep enough length.",
            "Please leave it long enough to tie it back.",
        )
        reference = _item(
            "Leave enough length.",
            "Please leave it long enough to tie back.",
        )

        self.assertIsNotNone(
            cards._reference_duplicate_reason(candidate, [reference])
        )

    def test_similar_short_words_are_not_false_duplicates(self):
        candidate = _item("Shade", "Could we make the color slightly darker?")
        reference = _item("Fade", "Please start the fade close to my ears.")

        self.assertIsNone(
            cards._reference_duplicate_reason(candidate, [reference])
        )

    def test_deck_path_accepts_absolute_xlsx_path(self):
        handle = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
        handle.close()
        self.addCleanup(os.unlink, handle.name)

        self.assertEqual(
            cards._resolve_deck_path(handle.name),
            os.path.abspath(handle.name),
        )


class ContentGateTests(unittest.TestCase):
    def test_singular_generated_item_is_normalized_to_a_list(self):
        item = _item("Could you repeat that?", "Could you say that one more time?")

        result = cards._extract_generated_items(
            json.dumps({"item": item}, ensure_ascii=False)
        )

        self.assertEqual(result, [item])

    def test_stylist_pain_point_requires_both_fields_to_be_questions(self):
        item = _item(
            "How much would you like off?",
            "Please take off five centimeters.",
        )

        rejected = cards._local_review_deck(
            "剪髮溝通",
            [item],
            ["聽懂髮型師詢問想剪掉多少長度"],
        )

        self.assertIn(0, rejected)
        self.assertIn("直接問句", rejected[0])

    def test_meta_learning_narration_is_invalid(self):
        item = _item(
            "My hair gets puffy when thinned.",
            "I need to mention my hair gets puffy when thinned.",
        )

        self.assertIn(
            "meta-learning narration must be replaced with the actual spoken line",
            cards._validation_issues(item),
        )

    def test_generation_topic_keeps_filename_label_separate_from_focus(self):
        result = cards._generation_topic("美髮沙龍_03", "只教剪壞補救")

        self.assertEqual(
            result,
            "美髮沙龍_03\n本集內容焦點與邊界：只教剪壞補救",
        )


def _pain_point(
    task: str,
    category: str,
    sequence: int,
    priority: int = 5,
    frequency: int = 5,
    friction: int = 5,
) -> dict:
    return {
        "category": category,
        "scenario": f"{category}現場",
        "speaker": "使用者",
        "intent": task,
        "task": task,
        "failure_mode": f"無法完成{task}",
        "priority": priority,
        "frequency": frequency,
        "friction": friction,
        "sequence": sequence,
        "required_terms": [],
    }


class PainPointPlanningTests(unittest.TestCase):
    def test_refill_request_size_scales_any_gap_by_three(self):
        for gap in (1, 2, 3, 4, 10):
            with self.subTest(gap=gap):
                self.assertEqual(cards._generation_request_size(gap, True), gap * 3)
                self.assertEqual(cards._generation_request_size(gap, False), gap)

    def test_four_missing_cards_request_twelve_candidates(self):
        points = [
            _pain_point(f"溝通任務{purpose_id}", f"分類{purpose_id}", purpose_id)
            for purpose_id in range(1, 6)
        ]
        first_item = dict(
            _item("Purpose one", "Use the first purpose now."),
            purpose_id=1,
        )
        refill_items = [
            dict(
                _item(
                    f"Purpose {purpose_id} option {variant}",
                    f"Use purpose {purpose_id} option {variant} now.",
                    purpose_id=purpose_id,
                ),
                purpose_id=purpose_id,
            )
            for purpose_id in range(2, 6)
            for variant in range(1, 4)
        ]
        prompts = []

        def fake_call(**kwargs):
            prompts.append(kwargs["messages"][0]["content"])
            items = [first_item] if len(prompts) == 1 else refill_items
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=json.dumps({"items": items}, ensure_ascii=False)
                        )
                    )
                ]
            )

        with (
            patch.object(cards, "_call_openai", side_effect=fake_call),
            patch.object(cards, "_review_deck", return_value={}),
            patch.object(cards, "_load_used_words", return_value=set()),
            patch.object(cards, "_save_used_words"),
        ):
            result = cards.generate("動態補齊測試", 5, pain_points=points)

        self.assertEqual(len(result), 5)
        self.assertEqual([item["_purpose_id"] for item in result], [1, 2, 3, 4, 5])
        self.assertEqual(len(prompts), 2)
        self.assertIn(
            "Return exactly 12 alternative candidate items in the items array",
            prompts[1],
        )
        self.assertIn("Return 3 materially different candidates for EACH entry", prompts[1])

    def test_structured_plan_flows_through_generation_and_review(self):
        points = [
            _pain_point("請對方拍多張供挑選", "請人幫拍", 1),
            _pain_point("照片模糊時要求重拍", "失敗補救", 2),
        ]
        generated = [
            dict(
                _item(
                    "Take a few, please.",
                    "Could you take a few so we have options?",
                ),
                purpose_id=1,
            ),
            dict(
                _item(
                    "Could you retake it?",
                    "This one is blurry; could you take it again?",
                ),
                purpose_id=2,
            ),
        ]
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps({"items": generated}, ensure_ascii=False)
                    )
                )
            ]
        )

        with (
            patch.object(cards, "REVIEW_MODE", "local"),
            patch.object(cards, "_call_openai", return_value=response),
            patch.object(cards, "_load_used_words", return_value=set()),
            patch.object(cards, "_save_used_words"),
        ):
            result = cards.generate("拍照測試", 2, pain_points=points)

        self.assertEqual([item["id"] for item in result], ["01", "02"])
        self.assertEqual([item["_purpose_id"] for item in result], [1, 2])
        self.assertEqual(
            [item["_pain_point"]["task"] for item in result],
            [point["task"] for point in points],
        )

    def test_planner_overproduces_candidates_then_selects_requested_count(self):
        candidates = [
            _pain_point(
                f"流程任務{index}",
                f"分類{index % 5}",
                index + 1,
                priority=5 - (index % 3),
            )
            for index in range(25)
        ]
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps(
                            {"candidates": candidates}, ensure_ascii=False
                        )
                    )
                )
            ]
        )

        with patch.object(cards, "_call_openai", return_value=response) as call:
            selected = cards._plan_pain_points("測試主題", 5)

        self.assertEqual(len(selected), 5)
        self.assertEqual(len({point["category"] for point in selected}), 5)
        self.assertEqual(call.call_args.kwargs["max_tokens"], 16000)

    def test_selection_keeps_category_coverage_and_journey_order(self):
        candidates = []
        for index in range(8):
            candidates.append(
                _pain_point(f"熱門任務{index}", "熱門分類", index + 1)
            )
        candidates.extend(
            [
                _pain_point("確認限制", "規則確認", 20, priority=3),
                _pain_point("修正錯誤", "出錯補救", 30, priority=3),
                _pain_point("保留紀錄", "完成收尾", 40, priority=3),
            ]
        )

        selected = cards._select_pain_points(candidates, 6)

        categories = [point["category"] for point in selected]
        self.assertIn("規則確認", categories)
        self.assertIn("出錯補救", categories)
        self.assertIn("完成收尾", categories)
        self.assertLessEqual(categories.count("熱門分類"), 3)
        self.assertEqual(
            [point["sequence"] for point in selected],
            sorted(point["sequence"] for point in selected),
        )

    def test_semantic_duplicate_uses_scenario_speaker_and_intent(self):
        current = _pain_point("請路人多拍幾張方便挑選", "請人幫拍", 1)
        previous = _pain_point("請路人連拍數張供自己挑選", "請人幫拍", 1)
        current["scenario"] = previous["scenario"] = "景點請陌生人拍合照"
        current["intent"] = previous["intent"] = "要求多拍幾張"

        reason = cards._reference_duplicate_reason(
            _item("Take several, please.", "Could you take several for us?"),
            [
                dict(
                    _item("Take a few, please.", "Could you take a few for us?"),
                    _source_deck="拍照",
                    _pain_point=previous,
                )
            ],
            current,
        )

        self.assertIn("場景、角色與意圖重複", reason)

    def test_opposite_answer_is_not_a_semantic_duplicate(self):
        positive = _pain_point("回答要烘烤", "客製選擇", 1)
        negative = _pain_point("回答不要烘烤", "客製選擇", 1)
        positive["scenario"] = negative["scenario"] = "店員確認是否烘烤"
        positive["intent"] = negative["intent"] = "回答烘烤選擇"

        self.assertFalse(
            cards._pain_points_semantically_duplicate(positive, negative)
        )

    def test_different_specific_answers_are_not_semantic_duplicates(self):
        cash = _pain_point("回答使用現金付款", "付款", 1)
        card = _pain_point("回答使用信用卡付款", "付款", 1)
        cash["scenario"] = card["scenario"] = "店員詢問付款方式"
        cash["intent"] = "回答使用現金付款"
        card["intent"] = "回答使用信用卡付款"
        cash["failure_mode"] = card["failure_mode"] = "無法完成付款"

        self.assertFalse(cards._pain_points_semantically_duplicate(cash, card))

    def test_planner_rejects_generic_intent(self):
        candidates = [
            dict(
                _pain_point(f"任務{index}", f"分類{index}", index),
                intent="回答",
            )
            for index in range(5)
        ]

        with self.assertRaisesRegex(RuntimeError, "去重後僅有 0/5"):
            cards._select_pain_points(candidates, 5, require_categories=True)

    def test_plan_json_round_trip_preserves_structured_fields(self):
        points = [
            _pain_point(f"任務{index}", f"分類{index % 2}", index)
            for index in range(1, 5)
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "topic.plan.json")
            cards._save_pain_point_plan("測試主題", points, path)

            loaded = cards._load_pain_point_plan(path, expected_count=4)

        self.assertEqual([point["task"] for point in loaded], [
            "任務1", "任務2", "任務3", "任務4"
        ])
        self.assertTrue(all("score" in point for point in loaded))
        self.assertTrue(all("category" in point for point in loaded))

    def test_reference_deck_loads_matching_plan_sidecar(self):
        card = _item("Could you retake it?", "Could we try that one more time?")
        card["id"] = "01"
        point = _pain_point("照片失敗時請對方重拍", "失敗補救", 1)
        with tempfile.TemporaryDirectory() as directory:
            xlsx_path = str(Path(directory) / "拍照.xlsx")
            plan_path = str(Path(directory) / "拍照.plan.json")
            cards.write_xlsx([card], xlsx_path)
            cards._save_pain_point_plan("拍照", [point], plan_path)

            references, paths = cards._load_reference_decks([xlsx_path])

        self.assertEqual(paths, [xlsx_path])
        self.assertEqual(references[0]["_pain_point"]["task"], point["task"])

    def test_legacy_text_plan_remains_supported(self):
        selected = cards._select_pain_points(["任務甲", "任務乙"], 2)

        self.assertEqual([point["task"] for point in selected], ["任務甲", "任務乙"])


if __name__ == "__main__":
    unittest.main()
