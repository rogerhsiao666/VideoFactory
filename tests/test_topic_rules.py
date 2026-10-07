import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import cards
import curriculum
import topic_rules


def response(payload):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content=json.dumps(payload, ensure_ascii=False)))])


class TopicRuleTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "topic_rules.json"
        self.data = {"Fresh Topic": {"must_include": ["read back numbers"],
                                    "forbidden": ["invented guarantees"]}}
        self.write_rules(self.data)
        configured = patch.object(topic_rules, "TOPIC_RULES_FILE", self.path)
        configured.start()
        self.addCleanup(configured.stop)
        topic_rules._load.cache_clear()
        self.addCleanup(topic_rules._load.cache_clear)
        token = cards._generation_deadline.set(None)
        self.addCleanup(cards._generation_deadline.reset, token)

    def write_rules(self, data):
        self.path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def test_json_only_topic_is_injected_as_a_system_message(self):
        messages = cards.topic_messages("Fresh Topic", "Generate three cards")
        self.assertEqual([m["role"] for m in messages], ["system", "user"])
        self.assertIn("read back numbers", messages[0]["content"])
        self.assertIn("invented guarantees", messages[0]["content"])
        self.assertEqual(messages[1]["content"], "Generate three cards")
        self.assertIn("read back numbers", cards._build_prompt("Fresh Topic", 3))

    def test_unknown_topic_keeps_the_generic_prompt(self):
        self.assertEqual(topic_rules.get_topic_rules("Unknown"), {})
        self.assertEqual(cards.topic_messages("Unknown", "Generate"),
                         [{"role": "user", "content": "Generate"}])
        self.assertNotIn("read back numbers", cards._build_prompt("Unknown", 3))

    def test_default_system_prompt_is_preserved_for_unknown_topics(self):
        self.assertEqual(cards.topic_messages("Unknown", "Generate", system="Existing editor"),
                         [{"role": "system", "content": "Existing editor"},
                          {"role": "user", "content": "Generate"}])

    def test_matches_bilingual_titles_slugs_and_case(self):
        self.data["Fresh Topic"]["aliases"] = ["新主題"]
        self.write_rules(self.data)
        for title in ("fresh topic", "FRESH_TOPIC", "fresh-topic_02",
                      "實戰_(Fresh_Topic)", "新主題第2集"):
            with self.subTest(title=title):
                self.assertIn("read back numbers", topic_rules.topic_rule_text(title))

    def test_matching_does_not_read_topic_names_from_the_focus(self):
        title = cards._generation_topic("Unknown", "不要 Phone Call Phobia，也不是 Fresh Topic")
        self.assertEqual(topic_rules.get_topic_rules(title), {})

    def test_english_aliases_do_not_match_inside_unrelated_words(self):
        self.assertEqual(topic_rules.get_topic_rules("Fresh Topics"), {})

    def test_more_specific_topic_wins_over_broad_alias(self):
        self.data["Topic"] = {"must_include": ["broad rule"], "forbidden": []}
        self.write_rules(self.data)
        text = cards.topic_rule_text("Fresh Topic")
        self.assertIn("read back numbers", text)
        self.assertNotIn("broad rule", text)

    def test_json_changes_are_reloaded_without_modifying_code(self):
        self.assertIn("read back numbers", cards.topic_rule_text("Fresh Topic"))
        self.data["Fresh Topic"]["must_include"] = ["new disconnection policy"]
        self.write_rules(self.data)
        self.assertIn("new disconnection policy", cards.topic_rule_text("Fresh Topic"))
        self.assertNotIn("read back numbers", cards.topic_rule_text("Fresh Topic"))

    def test_callers_cannot_mutate_cached_configuration(self):
        topic_rules.get_topic_rules("Fresh Topic")["must_include"].clear()
        self.assertIn("read back numbers", cards.topic_rule_text("Fresh Topic"))

    def test_load_path_does_not_depend_on_working_directory(self):
        previous = Path.cwd()
        try:
            os.chdir(self.path.parent)
            with patch.object(topic_rules, "TOPIC_RULES_FILE", Path(cards.BASE_DIR) / "topic_rules.json"):
                self.assertIn("Allergy Safety", topic_rules.load_topic_rules())
        finally:
            os.chdir(previous)

    def test_shared_safety_rules_remain_active_for_every_topic(self):
        with patch.object(topic_rules, "TOPIC_RULES_FILE", Path(cards.BASE_DIR) / "topic_rules.json"):
            self.assertTrue(topic_rules.default_content_issues("guaranteed allergy-free"))
            self.assertEqual(topic_rules.default_content_issues("ask about cross-contact"), [])
            self.assertIn("交叉接觸", topic_rules.default_rule_text("card_review"))

    def test_invalid_configuration_fails_before_any_request(self):
        cases = [[], {"Fresh Topic": {"must_include": "not an array"}},
                 {"Fresh Topic": {"forbidden": [5]}},
                 {"Fresh Topic": {"coverage": [{"any": ["x"]}]}},
                 {"Fresh Topic": {"plan_rejections": [{"patterns_all": ["["], "message": "bad"}]}},
                 {"Fresh Topic": {"candidate_roles": {"counterpart_share": 2}}},
                 {"Fresh Topic": {"must_inlcude": []}}]
        for data in cases:
            with self.subTest(data=data):
                self.write_rules(data)
                with patch.object(cards, "_call_openai") as api, self.assertRaisesRegex(ValueError, "topic_rules.json"):
                    curriculum.plan_curriculum("Fresh Topic", 3, [])
                api.assert_not_called()

    def test_missing_or_malformed_file_reports_the_exact_config_path(self):
        for path in (self.path.parent / "missing.json", self.path):
            self.path.write_text("{", encoding="utf-8")
            with patch.object(topic_rules, "TOPIC_RULES_FILE", path):
                with self.assertRaisesRegex(ValueError, str(path)):
                    topic_rules.load_topic_rules()

    def test_new_topic_can_define_coverage_and_forbidden_plan_patterns(self):
        rules = self.data["Fresh Topic"]
        rules["coverage"] = [{"any": ["spell"], "message": "missing spelling"},
                             {"source": "learner", "any": ["confirm"], "message": "missing learner confirmation"}]
        rules["plan_rejections"] = [{"any": ["guarantee"], "unless_any": ["no guarantee"],
                                     "message": "forbidden guarantee"}]
        self.write_rules(self.data)
        points = [{"category": "clarification", "task": "spell the name", "role_type": "learner_line"},
                  {"category": "confirmation", "task": "confirm the number", "role_type": "counterpart_line"}]
        self.assertEqual(cards._topic_specific_plan_coverage_issues("Fresh Topic", points),
                         ["missing learner confirmation"])
        self.assertEqual(cards._topic_specific_plan_violation("Fresh Topic", {"task": "guarantee"}),
                         "forbidden guarantee")
        self.assertIsNone(cards._topic_specific_plan_violation("Fresh Topic", {"task": "no guarantee"}))

    def test_contract_category_rules_support_all_of_any_groups(self):
        self.data["Fresh Topic"]["contract_categories"] = [{"all": [["unreasonable"], ["remedy", "solution"]],
                                                            "message": "missing remedy category"}]
        self.write_rules(self.data)
        self.assertEqual(cards._topic_specific_contract_issues("Fresh Topic", {"pain_categories": ["unreasonable request"]}),
                         ["missing remedy category"])
        self.assertEqual(cards._topic_specific_contract_issues("Fresh Topic", {"pain_categories": ["unreasonable remedy"]}), [])

    def test_candidate_planning_gets_dynamic_system_rules(self):
        with patch.object(cards, "_call_openai", side_effect=cards.GenerationTimeoutError("stop")) as api:
            with self.assertRaises(cards.GenerationTimeoutError):
                cards._request_pain_point_candidates("Fresh Topic", {"pain_categories": ["clarification"]}, 1, "")
        self.assertIn("read back numbers", api.call_args.kwargs["messages"][0]["content"])

    def test_curriculum_planning_injects_rules_and_restores_topic_on_timeout(self):
        def stop(*args, **kwargs):
            curriculum._request_json("Plan", "rule regression", "test-model")
            raise cards.GenerationTimeoutError("stop")
        with patch.object(cards, "_call_openai", return_value=response({})) as api:
            with patch.object(curriculum, "request_json", side_effect=stop):
                with self.assertRaises(cards.GenerationTimeoutError):
                    curriculum.plan_curriculum("Fresh Topic", 3, [])
        system = api.call_args.kwargs["messages"][0]["content"]
        self.assertIn("meticulous ESL curriculum editor", system)
        self.assertIn("read back numbers", system)
        self.assertEqual(topic_rules.current_topic(), "")

    def test_curriculum_generation_injects_rules_and_restores_scope(self):
        def stop(*args, **kwargs):
            curriculum._request_json("Generate", "rule regression", "test-model")
            raise cards.GenerationTimeoutError("stop")
        with patch.object(cards, "_call_openai", return_value=response({})) as api:
            with patch.object(curriculum, "validate_plan", side_effect=stop):
                with self.assertRaises(cards.GenerationTimeoutError):
                    curriculum.generate_deck({"topic": "Fresh Topic", "count": 3}, self.path.parent / "draft.json", [])
        self.assertIn("read back numbers", api.call_args.kwargs["messages"][0]["content"])
        self.assertEqual(topic_rules.current_topic(), "")

    def test_nested_topic_scopes_restore_the_outer_topic(self):
        with topic_rules.topic_rule_scope("Outer"):
            with topic_rules.topic_rule_scope("Inner"):
                self.assertEqual(topic_rules.current_topic(), "Inner")
            self.assertEqual(topic_rules.current_topic(), "Outer")
        self.assertEqual(topic_rules.current_topic(), "")

    def test_external_rules_do_not_change_api_budget_limits(self):
        budget = cards.APIBudget(200, 450000)
        messages = cards.topic_messages("Fresh Topic", "Generate")
        for _ in range(200):
            budget.reserve(messages, {"max_tokens": 1})
        with self.assertRaises(cards.APIBudgetError):
            budget.reserve(messages, {"max_tokens": 1})
        self.assertEqual(budget.requests, 200)

    def test_draft_resume_keeps_completed_cards_with_json_only_rules(self):
        points = [{"task": "確認配送日期", "category": "日期", "role_type": "learner_line"},
                  {"task": "請對方重複參考編號", "category": "編號", "role_type": "learner_line"}]
        completed = {"word_en": "confirm the delivery date", "word_ipa": "/kənˈfərm ðə dɪˈlɪvəri deɪt/",
                     "word_cn": "確認配送日期", "tips": "date 指日期，不是配送的時間點。",
                     "sentence_en": "Could you confirm the delivery date before I book leave?",
                     "sentence_ipa": "/kʊd ju kənˈfərm ðə dɪˈlɪvəri deɪt bɪˈfɔr aɪ bʊk liv/",
                     "sentence_cn": "我安排請假之前，能先確認配送日期嗎？", "_purpose_id": 1}
        missing = {"word_en": "repeat the reference number", "word_ipa": "/rɪˈpit ðə ˈrɛfərəns ˈnʌmbər/",
                   "word_cn": "重複參考編號", "tips": "reference number 有助於銜接案件紀錄。",
                   "sentence_en": "Could you repeat the reference number? I missed it.",
                   "sentence_ipa": "/kʊd ju rɪˈpit ðə ˈrɛfərəns ˈnʌmbər aɪ mɪst ɪt/",
                   "sentence_cn": "能再說一次參考編號嗎？我剛剛沒聽到。", "purpose_id": 2}
        checkpoint = self.path.parent / "deck.draft.json"
        checkpoint.write_text(json.dumps({"version": cards.PLAN_VERSION, "topic": "Fresh Topic", "count": 2,
                                         "pain_points": points, "contract": {}, "items": [completed]}), encoding="utf-8")
        with (patch.object(cards, "_call_openai", return_value=response({"items": [missing]})) as api,
              patch.object(cards, "_review_deck", return_value={}),
              patch.object(cards, "_load_used_words", return_value=set()),
              patch.object(cards, "_save_used_words")):
            result = cards.generate("Fresh Topic", 2, checkpoint_path=str(checkpoint), resume=True)
        self.assertEqual([item["_purpose_id"] for item in result], [1, 2])
        self.assertEqual(result[0]["word_en"], completed["word_en"])
        self.assertEqual(result[0]["sentence_en"], completed["sentence_en"])
        self.assertEqual(len(json.loads(checkpoint.read_text())["items"]), 2)
        api.assert_called_once()
        self.assertIn("read back numbers", api.call_args.kwargs["messages"][0]["content"])


if __name__ == "__main__":
    unittest.main()
