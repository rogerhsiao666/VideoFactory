import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import cards
from curated_blueprints import get_curated_blueprint


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

    def test_deck_name_prefers_output_before_legacy_cards_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory) / "output"
            cards_dir = Path(directory) / "cards"
            output_dir.mkdir()
            cards_dir.mkdir()
            output_path = output_dir / "結束話題.xlsx"
            legacy_path = cards_dir / "結束話題.xlsx"
            output_path.touch()
            legacy_path.touch()

            with (
                patch.object(cards, "OUTPUT_DIR", str(output_dir)),
                patch.object(cards, "CARDS_DIR", str(cards_dir)),
            ):
                self.assertEqual(
                    cards._resolve_deck_path("結束話題"),
                    str(output_path),
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

    def test_description_cli_alias_populates_existing_focus_field(self):
        args = cards._build_cli_parser().parse_args([
            "--topic", "結束話題",
            "--description", "只教社交場合優雅離開，不含商務會議",
        ])

        self.assertEqual(
            args.focus,
            "只教社交場合優雅離開，不含商務會議",
        )

    def test_force_cli_option_requests_full_regeneration(self):
        args = cards._build_cli_parser().parse_args([
            "--topic", "結束話題", "--force",
        ])

        self.assertTrue(args.force)

    def test_interactive_topic_description_preserves_paragraph_breaks(self):
        with patch(
            "builtins.input",
            side_effect=[
                "  電梯與派對的社交脫身  ",
                "",
                "  排除商務會議  ",
                "",
                "",
            ],
        ):
            result = cards._prompt_topic_description()

        self.assertEqual(result, "電梯與派對的社交脫身\n\n排除商務會議")

    def test_interactive_topic_description_first_blank_still_skips(self):
        with patch("builtins.input", side_effect=[""]):
            result = cards._prompt_topic_description()

        self.assertEqual(result, "")

    def test_generic_counterpart_must_use_the_planned_quote(self):
        item = _item(
            "Can I add something?",
            "Can I add something before we move to the next point?",
        )
        point = _pain_point(
            '聽懂對方原話：“Let’s move on to the next point.”',
            "對話節奏不熟悉",
            1,
        )
        point["role_type"] = "counterpart_line"

        rejected = cards._local_review_deck("插話藝術", [item], [point])

        self.assertIn(0, rejected)
        self.assertIn("逐字使用對方原話", rejected[0])

    def test_generic_counterpart_word_must_come_from_the_same_quote(self):
        item = _item(
            "Can I add something?",
            "Let’s move on to the next point.",
        )
        point = _pain_point(
            '聽懂對方原話：“Let’s move on to the next point.”',
            "對話節奏不熟悉",
            1,
        )
        point["role_type"] = "counterpart_line"

        rejected = cards._local_review_deck("插話藝術", [item], [point])

        self.assertIn(0, rejected)
        self.assertIn("同一段對方原話", rejected[0])

    def test_generated_youtube_title_removes_rayo_flashcard_suffix(self):
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="結束話題自然收尾｜用 Rayo 智慧閃卡"
                    )
                )
            ]
        )

        with patch.object(cards, "_call_openai", return_value=response):
            title = cards._generate_yt_title("結束話題")

        self.assertEqual(title, "結束話題自然收尾")

    def test_generated_youtube_title_uses_clean_fallback_when_only_rayo_remains(self):
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="用 Rayo 智慧閃卡")
                )
            ]
        )

        with patch.object(cards, "_call_openai", return_value=response):
            title = cards._generate_yt_title("結束話題")

        self.assertEqual(title, "【日常英文】結束話題 英文懶人包｜14 天上手")

    def test_long_plan_task_requires_two_fallback_keywords_not_verbatim_copy(self):
        item = _item(
            "I need a clear answer.",
            "I still need a clear answer about this issue.",
        )
        point = _pain_point(
            "I understand, but I need to know how this will be resolved before I can accept that answer.",
            "面對推託",
            1,
        )

        rejected = cards._local_review_deck(
            "Polite Complaints", [item], [point]
        )

        self.assertEqual(rejected, {})

    def test_polite_counterpart_card_rejects_customer_complaint_voice(self):
        item = _item(
            "This item is defective.",
            "This item is defective, and I need a replacement.",
            purpose_id=1,
        )
        point = _pain_point(
            "聽懂對方原話：“Do you have the receipt for the defective item?”",
            "指出已發生的問題",
            3,
        )

        rejected = cards._local_review_deck(
            "Polite Complaints", [item], [point]
        )

        self.assertIn(0, rejected)
        self.assertIn("必須是店員處理客訴的回應", rejected[0])

    def test_polite_counterpart_rejects_mixed_staff_and_customer_voice(self):
        item = _item(
            "Could you show me the error?",
            "Could you show me the error on my ticket?",
            purpose_id=1,
        )
        point = _pain_point(
            "聽懂對方原話：“Could you show me the error on your ticket?”",
            "指出已發生的問題",
            3,
        )

        rejected = cards._local_review_deck(
            "Polite Complaints", [item], [point]
        )

        self.assertIn(0, rejected)
        self.assertIn("必須是店員處理客訴的回應", rejected[0])

    def test_polite_impact_counterpart_must_address_customer_with_your(self):
        item = _item(
            "The equipment is broken.",
            "The equipment is broken; can you fix it?",
            purpose_id=1,
        )
        point = _pain_point(
            "聽懂對方原話：“I see the broken equipment interrupted your workout.”",
            "描述問題影響",
            3,
        )

        rejected = cards._local_review_deck(
            "Polite Complaints", [item], [point]
        )

        self.assertIn(0, rejected)
        self.assertIn("必須是店員處理客訴的回應", rejected[0])

    def test_locked_blueprint_rejects_keyword_swap_or_paraphrase(self):
        item = _item(
            "Could you repeat that?",
            "The line cut out. Could you repeat that?",
            purpose_id=1,
        )
        point = _pain_point("重聽斷掉的最後一段", "理解失速", 1)
        point.update({
            "job_key": "只重聽斷掉的最後一段",
            "target_phrase": "Could you repeat the last part?",
            "target_sentence": "The line cut out. Could you repeat the last part?",
        })

        rejected = cards._local_review_deck(
            "Phone Call Phobia", [item], [point]
        )

        self.assertIn(0, rejected)
        self.assertIn("逐字使用鎖定實戰短句", rejected[0])

    def test_locked_blueprint_overrides_model_wording_before_validation(self):
        item = _item("Generic phrase", "Generic sentence", purpose_id=1)
        item["purpose_id"] = 1
        point = _pain_point("處理斷線", "通訊失控", 1)
        point.update({
            "job_key": "只重聽斷掉的最後一段",
            "target_phrase": "Could you repeat the last part?",
            "target_sentence": "The line cut out. Could you repeat the last part?",
        })

        result = cards._apply_locked_blueprint_lines(item, [point])

        self.assertEqual(result["word_en"], "Could you repeat the last part?")
        self.assertEqual(
            result["sentence_en"],
            "The line cut out. Could you repeat the last part?",
        )
        self.assertEqual(
            result["_locked_source_mismatch"],
            "word_en, sentence_en",
        )

    def test_locked_blueprint_exact_source_does_not_mark_ipa_as_stale(self):
        item = _item(
            "Could you repeat the last part?",
            "The line cut out. Could you repeat the last part?",
            purpose_id=1,
        )
        point = _pain_point("處理斷線", "通訊失控", 1)
        point.update({
            "job_key": "只重聽斷掉的最後一段",
            "target_phrase": "Could you repeat the last part?",
            "target_sentence": "The line cut out. Could you repeat the last part?",
        })

        result = cards._apply_locked_blueprint_lines(item, [point])

        self.assertNotIn("_locked_source_mismatch", result)

    def test_locked_blueprint_batch_retries_stale_ipa(self):
        point = _pain_point("處理斷線", "通訊失控", 1)
        point.update({
            "job_key": "只重聽斷掉的最後一段",
            "target_phrase": "Could you repeat the last part?",
            "target_sentence": "The line cut out. Could you repeat the last part?",
        })
        stale = dict(
            _item(
                point["target_phrase"],
                point["target_sentence"],
            ),
            purpose_id=1,
        )
        valid = dict(stale)
        valid["word_ipa"] = "/kʊd ju rɪˈpit ðə læst pɑrt/"
        valid["sentence_ipa"] = "/ðə laɪn kʌt aʊt kʊd ju rɪˈpit ðə læst pɑrt/"
        responses = [
            SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(
                    content=json.dumps({"items": [stale]}, ensure_ascii=False)
                ))]
            ),
            SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(
                    content=json.dumps({"items": [valid]}, ensure_ascii=False)
                ))]
            ),
        ]

        with patch.object(cards, "_call_openai", side_effect=responses) as call:
            result = cards._generate_locked_blueprint_items(
                "Phone Call Phobia", [(1, point)]
            )

        self.assertEqual(call.call_count, 2)
        self.assertEqual(result[0]["word_ipa"], valid["word_ipa"])

    def test_locked_blueprint_uses_code_review_without_ai(self):
        item = _item(
            "Could you repeat the last part?",
            "The line cut out. Could you repeat the last part?",
            purpose_id=1,
        )
        point = _pain_point("重聽斷掉的最後一段", "理解失速", 1)
        point.update({
            "job_key": "只重聽斷掉的最後一段",
            "target_phrase": "Could you repeat the last part?",
            "target_sentence": "The line cut out. Could you repeat the last part?",
        })

        for review_mode in ("local", "hybrid", "ai"):
            with self.subTest(review_mode=review_mode), patch.object(
                cards, "REVIEW_MODE", review_mode
            ), patch.object(cards, "_ai_review_deck") as ai_review:
                rejected = cards._review_deck(
                    "Phone Call Phobia", [item], [point]
                )

            self.assertEqual(rejected, {})
            ai_review.assert_not_called()

    def test_planned_deck_ignores_ai_duplicate_rejection(self):
        points = [
            _pain_point("用工作理由離開", "離開理由", 1),
            _pain_point("去拿飲料並離開", "離開理由", 2),
        ]
        items = [
            dict(_item("I need to get back.", "I need to get back to work."), _purpose_id=1),
            dict(_item("I'll grab a drink.", "I'm going to go grab a drink."), _purpose_id=2),
        ]
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps({
                            "reject": [{"id": "02", "reason": "與 01 溝通目的重複"}],
                        })
                    )
                )
            ]
        )

        with patch.object(cards, "_call_openai", return_value=response) as call:
            rejected = cards._ai_review_deck("結束話題", items, points)

        self.assertEqual(rejected, {})
        self.assertEqual(call.call_count, 1)


def _pain_point(
    task: str,
    category: str,
    sequence: int,
    priority: int = 5,
    frequency: int = 5,
    friction: int = 5,
) -> dict:
    counterpart = sequence % 3 == 0
    return {
        "category": category,
        "scenario": f"{category}現場",
        "speaker": "店員" if counterpart else "使用者",
        "intent": task,
        "task": task,
        "pain_trigger": f"現場發生{task}的溝通需求",
        "user_stakes": f"處理不好會造成{task}的具體失誤",
        "desired_outcome": f"當場完成{task}",
        "role_type": "counterpart_line" if counterpart else "learner_line",
        "failure_mode": f"無法完成{task}",
        "priority": priority,
        "frequency": frequency,
        "friction": friction,
        "sequence": sequence,
        "required_terms": [],
    }


def _topic_contract() -> dict:
    return {
        "audience": "在高摩擦現場容易卡住的台灣成人",
        "core_pain": "臨場聽不懂或說不清楚而無法完成溝通",
        "promised_transformation": "能聽懂關鍵原話並立即完成下一步",
        "in_scope": ["理解對方問句", "直接回答", "出錯補救"],
        "out_of_scope": ["泛用寒暄", "背景知識", "只背孤立名詞"],
        "required_moments": ["開始溝通", "關鍵確認", "失敗補救"],
        "pain_categories": ["理解壓力", "即時回應", "資訊確認", "失誤修正", "完成收尾"],
    }


def _pain_point_plan(points: list[dict]) -> cards.PainPointPlan:
    contract = _topic_contract()
    categories = list(dict.fromkeys(point["category"] for point in points))
    while len(categories) < 5:
        categories.append(f"補充機制{len(categories) + 1}")
    contract["pain_categories"] = categories[:8]
    return cards.PainPointPlan(points, contract=contract)


class PainPointPlanningTests(unittest.TestCase):
    def test_explicit_focus_exclusion_rejects_slowdown_plan(self):
        topic = cards._generation_topic(
            "插話藝術",
            "只教主動插話；不要收錄請別人重複或放慢速度。",
        )
        point = _pain_point(
            "Could you please slow down a bit?",
            "插話困難",
            1,
        )
        point["intent"] = "請發言者放慢速度"
        point["desired_outcome"] = "對方說慢一點"

        issues = cards._topic_specific_plan_coverage_issues(topic, [point])

        self.assertTrue(any("不要請別人放慢速度" in issue for issue in issues))

    def test_explicit_focus_exclusion_rejects_inviting_others_to_speak(self):
        topic = cards._generation_topic(
            "插話藝術",
            "只教學習者插話；不要收錄請別人發言。",
        )
        point = _pain_point(
            "What do you think about this?",
            "溝通不暢",
            1,
        )
        point["intent"] = "邀請對方分享看法"

        issues = cards._topic_specific_plan_coverage_issues(topic, [point])

        self.assertTrue(any("不要請別人發言" in issue for issue in issues))

    def test_explicit_learner_only_focus_allows_learner_questions(self):
        topic = cards._generation_topic(
            "結束話題",
            "50 張全部是學習者自己開口的句子，不要收錄對方延伸話題的原話。",
        )
        points = [
            dict(
                _pain_point(f"離場任務{index}", f"分類{index}", index),
                role_type="learner_line",
                speaker="學習者",
            )
            for index in range(1, 6)
        ]
        contract = _topic_contract()
        contract["pain_categories"] = [point["category"] for point in points]
        contract["learner_only"] = True
        plan = cards.PainPointPlan(points, contract=contract)

        issues = cards._plan_quality_issues(plan, 5)
        question_item = _item(
            "What do you think?",
            "I need to get back to work now.",
        )
        question_item["_purpose_id"] = 1
        rejected = cards._local_review_deck("結束話題", [question_item], plan)

        self.assertTrue(cards._topic_requests_learner_only(topic))
        self.assertFalse(any("對方原話僅" in issue for issue in issues))
        self.assertNotIn(0, rejected)

    def test_focus_must_teach_phrases_become_partial_plan_locks(self):
        phrases = [
            "It was great talking to you, but I should get going.",
            "I'm going to go grab a drink.",
            "I'll let you get back to your day.",
        ]
        topic = cards._generation_topic(
            "結束話題",
            "必教金句：" + "；".join(phrases) + "。內容邊界：只教社交脫身。",
        )
        points = [
            dict(
                _pain_point(f"離場任務{index}", f"分類{index}", index),
                role_type="learner_line",
                speaker="學習者",
            )
            for index in range(1, 4)
        ]

        locked = cards._apply_focus_phrase_locks(topic, points)

        self.assertEqual(locked, 3)
        self.assertEqual(points[0]["target_phrase"], "I should get going.")
        self.assertEqual(
            [point["target_sentence"] for point in points],
            phrases,
        )
        self.assertNotIn(
            "鎖定牌組的每個痛點都必須提供 target_phrase 與 target_sentence",
            cards._plan_quality_issues(points, 3),
        )

    def test_focus_must_teach_phrases_support_translations_and_list_commas(self):
        topic = cards._generation_topic(
            "高情商的明確拒絕",
            "必教金句：我目前不需要，謝謝 (I'm good for now, thanks.)、"
            "我很想去，但我已經有安排了 (I'd love to, but I have plans.)。"
            "內容須自然、口語，不要把 Maybe 當成推薦答案。",
        )

        self.assertEqual(
            cards._required_focus_phrases(topic),
            [
                "I'm good for now, thanks.",
                "I'd love to, but I have plans.",
            ],
        )

    def test_curated_topics_have_fifty_unique_jobs_and_locked_lines(self):
        for topic in ("Phone Call Phobia", "Polite Complaints"):
            with self.subTest(topic=topic):
                contract, points = get_curated_blueprint(topic, 50)
                plan = cards.PainPointPlan(points, contract=contract)

                self.assertEqual(len(points), 50)
                self.assertEqual(len({point["job_key"] for point in points}), 50)
                self.assertEqual(
                    len({point["target_phrase"].casefold() for point in points}), 50
                )
                self.assertEqual(cards._plan_quality_issues(plan, 50), [])

    def test_curated_topic_planning_does_not_call_ai(self):
        with patch.object(
            cards, "_request_topic_contract"
        ) as request_contract, patch.object(
            cards, "_ai_review_pain_point_plan"
        ) as ai_review:
            points = cards._plan_pain_points("Phone Call Phobia", 50)

        self.assertEqual(len(points), 50)
        request_contract.assert_not_called()
        ai_review.assert_not_called()

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

    def test_generic_generation_prompt_locks_counterpart_quote_and_role(self):
        point = _pain_point(
            '聽懂對方原話：“What do you do for fun?”',
            "對方延伸話題",
            1,
        )
        point["role_type"] = "counterpart_line"
        generated = dict(
            _item(
                "What do you do for fun?",
                "What do you do for fun?",
            ),
            purpose_id=1,
        )
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps({"items": [generated]}, ensure_ascii=False)
                    )
                )
            ]
        )

        with (
            patch.object(cards, "_call_openai", return_value=response) as call,
            patch.object(cards, "_review_deck", return_value={}),
            patch.object(cards, "_load_used_words", return_value=set()),
            patch.object(cards, "_save_used_words"),
        ):
            cards.generate("結束話題", 1, pain_points=[point])

        prompt = call.call_args.kwargs["messages"][0]["content"]
        self.assertIn("copy the complete English quote", prompt)
        self.assertIn("Never write the learner's answer or reaction", prompt)

    def test_generation_skips_misaligned_counterpart_candidate(self):
        point = _pain_point(
            '聽懂對方原話：“Let’s move on to the next point.”',
            "對話節奏不熟悉",
            1,
        )
        point["role_type"] = "counterpart_line"
        invalid = dict(
            _item(
                "Can I add something?",
                "Can I add something before we move to the next point?",
            ),
            purpose_id=1,
        )
        valid = dict(
            _item(
                "Let’s move on",
                "Let’s move on to the next point.",
            ),
            purpose_id=1,
        )
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps(
                            {"items": [invalid, valid]}, ensure_ascii=False
                        )
                    )
                )
            ]
        )

        with (
            patch.object(cards, "_call_openai", return_value=response),
            patch.object(cards, "_review_deck", return_value={}),
            patch.object(cards, "_load_used_words", return_value=set()),
            patch.object(cards, "_save_used_words"),
        ):
            result = cards.generate("插話藝術", 1, pain_points=[point])

        self.assertEqual(result[0]["word_en"], "Let’s move on")
        self.assertEqual(
            result[0]["sentence_en"],
            "Let’s move on to the next point.",
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
        with (
            patch.object(
                cards,
                "_request_topic_contract",
                return_value=_pain_point_plan(candidates).contract,
            ),
            patch.object(
                cards, "_request_pain_point_candidates", return_value=candidates
            ) as request_candidates,
            patch.object(cards, "_ai_review_pain_point_plan", return_value=[]),
        ):
            selected = cards._plan_pain_points("測試主題", 5)

        self.assertEqual(len(selected), 5)
        self.assertEqual(len({point["category"] for point in selected}), 5)
        self.assertEqual(request_candidates.call_count, 1)
        self.assertEqual(request_candidates.call_args.args[2], 10)

    def test_planner_rejects_four_categories_when_contract_requires_five(self):
        candidates = [
            _pain_point(
                f"流程任務{index}",
                f"分類{index % 4}",
                index + 1,
                priority=5 - (index % 3),
            )
            for index in range(26)
        ]
        with (
            patch.object(
                cards,
                "_request_topic_contract",
                return_value=_pain_point_plan(candidates).contract,
            ),
            patch.object(
                cards, "_request_pain_point_candidates", return_value=candidates
            ) as request_candidates,
            patch.object(cards, "_ai_review_pain_point_plan", return_value=[]),
            self.assertRaisesRegex(RuntimeError, "痛點規劃連續 3 次失敗"),
        ):
            cards._plan_pain_points("測試主題", 6)

        self.assertEqual(request_candidates.call_count, 3)

    def test_planner_still_rejects_fallback_with_too_few_categories(self):
        candidates = [
            _pain_point(f"流程任務{index}", f"分類{index % 2}", index + 1)
            for index in range(25)
        ]
        with (
            patch.object(
                cards,
                "_request_topic_contract",
                return_value=_pain_point_plan(candidates).contract,
            ),
            patch.object(
                cards, "_request_pain_point_candidates", return_value=candidates
            ) as request_candidates,
            patch.object(cards, "_ai_review_pain_point_plan", return_value=[]),
            self.assertRaisesRegex(RuntimeError, "痛點規劃連續 3 次失敗"),
        ):
            cards._plan_pain_points("測試主題", 5)

        self.assertEqual(request_candidates.call_count, 3)

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

    def test_identical_task_is_duplicate_even_when_speaker_label_drifts(self):
        first = _pain_point("Could you repeat that?", "聽漏資訊補救", 1)
        second = _pain_point("Could you repeat that?", "聽漏資訊補救", 2)
        first["speaker"] = "使用者"
        second["speaker"] = "來電者"
        first["role_type"] = second["role_type"] = "learner_line"

        self.assertTrue(cards._pain_points_semantically_duplicate(first, second))

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

    def test_pain_evidence_gate_rejects_generic_flow_categories(self):
        candidates = [
            dict(_pain_point(f"例行任務{index}", "確認", index), role_type="learner_line")
            for index in range(1, 6)
        ]

        with self.assertRaisesRegex(RuntimeError, "去重後僅有 0/5"):
            cards._select_pain_points(
                candidates,
                5,
                require_categories=True,
                require_pain_evidence=True,
            )

    def test_plan_quality_requires_counterpart_lines(self):
        points = [
            dict(_pain_point(f"電話焦慮任務{index}", f"焦慮機制{index}", index),
                 role_type="learner_line", speaker="使用者")
            for index in range(1, 11)
        ]

        issues = cards._plan_quality_issues(points, 10)

        self.assertTrue(any("對方原話" in issue for issue in issues))

    def test_plan_reviewer_includes_only_matching_topic_rules(self):
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps({"pass": True, "issues": [], "reject": []})
                    )
                )
            ]
        )
        points = _pain_point_plan([
            _pain_point("請對方放慢速度", "語速壓力補救", 1),
        ])

        with patch.object(cards, "_call_openai", return_value=response) as call:
            issues = cards._ai_review_pain_point_plan(
                "Phone Call Phobia", points.contract, points
            )

        prompt = call.call_args.kwargs["messages"][0]["content"]
        self.assertEqual(issues, [])
        self.assertIn("只因為可以透過電話完成", prompt)
        self.assertNotIn("不代表符合 Polite Complaints", prompt)
        self.assertNotIn("問 Wi-Fi、問折扣等若沒有已發生的問題", prompt)

    def test_generic_topic_contract_prompt_omits_other_topic_rules(self):
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps(
                            {"topic_contract": _topic_contract()},
                            ensure_ascii=False,
                        )
                    )
                )
            ]
        )

        with patch.object(cards, "_call_openai", return_value=response) as call:
            cards._request_topic_contract(
                cards._generation_topic(
                    "結束話題",
                    "只教社交脫身，不含客訴或電話恐懼",
                ),
                "",
            )

        prompt = call.call_args.kwargs["messages"][0]["content"]
        self.assertNotIn("題名含 phobia", prompt)
        self.assertNotIn("Polite Complaints 的 pain_categories", prompt)
        self.assertNotIn("拒絕不合理補救方案", prompt)

    def test_non_actionable_plan_review_rejection_is_ignored(self):
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps({
                            "pass": False,
                            "issues": ["整副牌問題"],
                            "reject": [{"id": 1, "reason": "偏離核心痛點"}],
                        })
                    )
                )
            ]
        )
        points = _pain_point_plan([
            _pain_point("請對方放慢速度", "語速壓力補救", 1),
        ])

        with patch.object(cards, "_call_openai", return_value=response):
            issues = cards._ai_review_pain_point_plan(
                "Phone Call Phobia", points.contract, points
            )

        self.assertEqual(issues, [])

    def test_phone_required_moment_is_not_rejected_as_generic_courtesy(self):
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps({
                            "pass": False,
                            "issues": [],
                            "reject": [{
                                "id": 1,
                                "reason": (
                                    "請求放慢語速的內容不符合焦點，"
                                    "應該專注於確認信息。"
                                ),
                            }],
                        })
                    )
                )
            ]
        )
        points = _pain_point_plan([
            _pain_point(
                "I'm sorry, could you please speak a bit slower?",
                "語速壓力補救",
                1,
            ),
        ])

        with patch.object(cards, "_call_openai", return_value=response):
            issues = cards._ai_review_pain_point_plan(
                "Phone Call Phobia", points.contract, points
            )

        self.assertEqual(issues, [])

    def test_unlisted_phone_adjacent_request_keeps_actionable_rejection(self):
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps({
                            "pass": False,
                            "issues": [],
                            "reject": [{
                                "id": 1,
                                "reason": (
                                    "請對方解釋專業術語是一般性問題，"
                                    "不直接針對因害怕英語電話而卡住的現場溝通。"
                                ),
                            }],
                        })
                    )
                )
            ]
        )
        points = _pain_point_plan([
            _pain_point("What does that term mean?", "術語理解", 1),
        ])

        with patch.object(cards, "_call_openai", return_value=response):
            issues = cards._ai_review_pain_point_plan(
                "Phone Call Phobia", points.contract, points
            )

        self.assertEqual(len(issues), 1)
        self.assertIn("專業術語", issues[0])

    def test_phone_polite_ending_is_protected_by_explicit_focus(self):
        point = _pain_point("謝謝你的幫助，我們下次再聯絡。", "失控感收尾", 1)

        self.assertTrue(cards._review_rejection_conflicts_with_contract(
            "Phone Call Phobia",
            point,
            "此項目涉及結束通話，偏離核心痛點。",
        ))

    def test_phone_plan_requires_each_high_friction_moment(self):
        points = [
            _pain_point("Hello, this is Mei calling.", "腦袋空白", 1),
            _pain_point("Could you repeat that more slowly?", "聽不懂", 2),
            _pain_point("Could you give me a moment to think?", "腦袋空白", 4),
            _pain_point("Could you repeat that name and number?", "資訊確認", 5),
            _pain_point("I can't hear you. Could you call back?", "溝通失控", 7),
            _pain_point("Could you transfer me or take a message?", "溝通失控", 8),
        ]

        issues = cards._topic_specific_plan_coverage_issues(
            "Phone Call Phobia", points
        )

        self.assertEqual(issues, ["缺少必要痛點時刻：禮貌結束"])

    def test_polite_complaints_requires_escalation_and_written_record(self):
        points = [
            _pain_point("This item is broken.", "指出問題", 1),
            _pain_point("The delay caused me to miss my booking.", "描述影響", 2),
            _pain_point("Could you replace it?", "提出補救", 4),
            _pain_point("That policy doesn't address the problem.", "回應推託", 5),
            _pain_point("A voucher is not an acceptable solution.", "拒絕方案", 7),
        ]

        issues = cards._topic_specific_plan_coverage_issues(
            "Polite Complaints", points
        )

        self.assertIn("缺少使用者直接開口的必要痛點時刻：要求主管升級", issues)
        self.assertIn("缺少使用者直接開口的必要痛點時刻：要求書面確認", issues)

    def test_polite_contract_cannot_confuse_solution_with_incoming_request(self):
        contract = _topic_contract()
        contract["pain_categories"] = [
            "指出問題", "描述影響", "提出補救", "面對推託",
            "拒絕不合理要求", "要求主管", "書面留存",
        ]

        issues = cards._topic_specific_contract_issues(
            "Polite Complaints", contract
        )

        self.assertTrue(any("不合理補救方案" in issue for issue in issues))

    def test_polite_gate_rejects_refusing_overtime_as_a_complaint(self):
        point = _pain_point(
            "我目前工作量很大，無法再加班。",
            "拒絕不合理要求的困難",
            1,
        )

        reason = cards._topic_specific_plan_violation(
            "Polite Complaints", point
        )

        self.assertIn("不是拒絕客訴中的不合理補救方案", reason)

    def test_polite_counterpart_line_must_be_staff_response(self):
        point = _pain_point(
            "聽懂對方原話：“I received the wrong item.”",
            "指出已發生的問題",
            3,
        )

        reason = cards._topic_specific_plan_violation(
            "Polite Complaints", point
        )

        self.assertIn("必須是店員處理客訴的回應", reason)

    def test_topic_specific_gate_separates_pain_from_adjacent_routines(self):
        phone_repeat = _pain_point("Could you repeat the date, please?", "資訊確認", 1)
        phone_order = _pain_point("確認訂單號碼", "資訊確認", 2)
        polite_wifi = _pain_point("詢問飯店有沒有 Wi-Fi", "降低指責感", 1)
        polite_broken_wifi = _pain_point("反映 Wi-Fi 壞掉並要求修復", "提出補救", 2)

        self.assertIsNone(
            cards._topic_specific_plan_violation("Phone Call Phobia", phone_repeat)
        )
        self.assertIsNotNone(
            cards._topic_specific_plan_violation("Phone Call Phobia", phone_order)
        )
        self.assertIsNotNone(
            cards._topic_specific_plan_violation("Polite Complaints", polite_wifi)
        )
        self.assertIsNone(
            cards._topic_specific_plan_violation(
                "Polite Complaints", polite_broken_wifi
            )
        )

    def test_v2_plan_is_rejected_instead_of_silently_reused(self):
        payload = {
            "version": 2,
            "topic": "Phone Call Phobia",
            "count": 1,
            "pain_points": [_pain_point("詢問退貨政策", "詢問", 1)],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old.plan.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            with self.assertRaisesRegex(cards.PlanVersionError, "已過期"):
                cards._load_pain_point_plan(str(path), expected_count=1)

    def test_plan_json_round_trip_preserves_structured_fields(self):
        points = [
            _pain_point(f"任務{index}", f"分類{index}", index)
            for index in range(1, 5)
        ]
        points = _pain_point_plan(points)
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "topic.plan.json")
            cards._save_pain_point_plan("測試主題", points, path)

            loaded = cards._load_pain_point_plan(path, expected_count=4)

        self.assertEqual([point["task"] for point in loaded], [
            "任務1", "任務2", "任務3", "任務4"
        ])
        self.assertTrue(all("score" in point for point in loaded))
        self.assertTrue(all("category" in point for point in loaded))
        self.assertEqual(loaded.contract["core_pain"], _topic_contract()["core_pain"])

    def test_saved_plan_is_rejected_when_topic_description_changes(self):
        points = _pain_point_plan([
            _pain_point(f"任務{index}", f"分類{index}", index)
            for index in range(1, 5)
        ])
        original_topic = cards._generation_topic("結束話題", "只教商務會議收尾")
        requested_topic = cards._generation_topic("結束話題", "只教派對社交脫身")
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "結束話題.plan.json")
            cards._save_pain_point_plan(original_topic, points, path)

            with self.assertRaisesRegex(ValueError, "主題描述與本次輸入不一致"):
                cards._load_pain_point_plan(
                    path,
                    expected_count=4,
                    expected_topic=requested_topic,
                )

    def test_reference_deck_loads_matching_plan_sidecar(self):
        card = _item("Could you retake it?", "Could we try that one more time?")
        card["id"] = "01"
        point = _pain_point("照片失敗時請對方重拍", "失敗補救", 1)
        with tempfile.TemporaryDirectory() as directory:
            xlsx_path = str(Path(directory) / "拍照.xlsx")
            plan_path = str(Path(directory) / "拍照.plan.json")
            cards.write_xlsx([card], xlsx_path)
            cards._save_pain_point_plan("拍照", _pain_point_plan([point]), plan_path)

            references, paths = cards._load_reference_decks([xlsx_path])

        self.assertEqual(paths, [xlsx_path])
        self.assertEqual(references[0]["_pain_point"]["task"], point["task"])

    def test_written_deck_wraps_text_and_freezes_header(self):
        card = _item(
            "Sorry to interrupt.",
            "Sorry to interrupt, but I’d like to add something here.",
        )
        card["id"] = "01"
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "插話藝術.xlsx")
            cards.write_xlsx([card], path)
            sheet = cards.openpyxl.load_workbook(path).active

        self.assertEqual(sheet.freeze_panes, "A2")
        self.assertEqual(sheet.auto_filter.ref, "A1:H2")
        self.assertTrue(sheet["F2"].alignment.wrap_text)
        self.assertEqual(sheet["F2"].alignment.vertical, "top")
        self.assertGreaterEqual(sheet.row_dimensions[2].height, 30)
        self.assertEqual(sheet.page_setup.orientation, "landscape")
        self.assertEqual(sheet.page_setup.fitToWidth, 1)

    def test_legacy_text_plan_remains_supported(self):
        selected = cards._select_pain_points(["任務甲", "任務乙"], 2)

        self.assertEqual([point["task"] for point in selected], ["任務甲", "任務乙"])


if __name__ == "__main__":
    unittest.main()
