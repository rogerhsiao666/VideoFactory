import copy
import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

import cards
import curriculum
from learning_editor import LEARNING_HEADERS


SOURCE = Path(cards.BASE_DIR) / "output" / "\u63a8\u92b7\u8207\u96b1\u5f62\u6572\u8a50.xlsx.editor.json"


def fixture(topic="Airport check-in", count=10):
    original = json.loads(SOURCE.read_text(encoding="utf-8"))["items"]
    pools = {"basic": [item for item in original if item["tier"] == "basic"],
             "advanced": [item for item in original if item["tier"] == "advanced"][6:]}
    scenarios = [topic + " arrival", topic + " request", topic + " decision"]
    slots = curriculum.slots_for(count, scenarios)
    jobs, items = [], []
    for slot in slots:
        item = copy.deepcopy(pools[slot["tier"]].pop(0))
        item.update(slot, core="purpose-" + slot["id"])
        item["_semantic_group"] = item["core"]
        item["_semantic_level"] = item["tier"]
        item["_semantic_review_version"] = curriculum.SEMANTIC_REVIEW_VERSION
        jobs.append(dict(slot, core=item["core"], task=item["sentence_en"], role="learner", speaker="customer"))
        items.append(item)
    return {"version": curriculum.VERSION, "topic": topic, "count": count, "scenarios": scenarios, "jobs": jobs}, items


class CurriculumContractTests(unittest.TestCase):
    def setUp(self):
        self.plan, self.items = fixture()
        fidelity = patch.object(curriculum, "review_task_fidelity", return_value={})
        fidelity.start()
        self.addCleanup(fidelity.stop)
        token = cards._generation_deadline.set(None)
        self.addCleanup(cards._generation_deadline.reset, token)

    def test_balanced_scenarios_and_rounded_sixty_forty(self):
        for count in range(3, 101):
            for scenarios in (["a", "b", "c"], ["a", "b", "c", "d"]):
                if len(scenarios) > count:
                    continue
                with self.subTest(count=count, scenarios=scenarios):
                    slots = curriculum.slots_for(count, scenarios)
                    sizes = Counter(item["Scenario"] for item in slots)
                    self.assertLessEqual(max(sizes.values()) - min(sizes.values()), 1)
                    basic = sum(item["tier"] == "basic" for item in slots)
                    self.assertLessEqual(abs(basic - count * 0.6), 0.5)
                    if count % 5 == 0:
                        self.assertEqual(basic, count * 3 // 5)

    def test_small_or_invalid_scenario_inputs_fail(self):
        for count, scenarios in ((2, ["a", "b", "c"]), (3, ["a", "b"]),
                                 (4, ["a", "a", "b"]), (4, ["a", "", "b"])):
            with self.subTest(count=count, scenarios=scenarios), self.assertRaises(ValueError):
                curriculum.slots_for(count, scenarios)

    def test_missing_row_or_changed_difficulty_is_rejected(self):
        for change in (lambda p: p["jobs"].pop(), lambda p: p["jobs"][0].update(tier="advanced"),
                       lambda p: p.update(topic="different topic"), lambda p: p.update(version=0)):
            plan = copy.deepcopy(self.plan)
            change(plan)
            with self.assertRaises(ValueError):
                curriculum.validate_plan(plan, self.plan["topic"], 10)

    def test_same_meaning_allows_only_basic_advanced_pair(self):
        plan = copy.deepcopy(self.plan)
        plan["jobs"][2]["core"] = plan["jobs"][0]["core"]
        curriculum.validate_plan(plan, plan["topic"], 10)
        plan["jobs"][1]["core"] = plan["jobs"][0]["core"]
        with self.assertRaises(ValueError):
            curriculum.validate_plan(plan, plan["topic"], 10)
        plan = copy.deepcopy(self.plan)
        plan["jobs"][1]["core"] = plan["jobs"][0]["core"]
        with self.assertRaises(ValueError):
            curriculum.validate_plan(plan, plan["topic"], 10)

    def test_cross_label_semantic_duplicates_are_rejected_at_export_gate(self):
        self.items[1]["_semantic_group"] = self.items[0]["_semantic_group"]
        with self.assertRaises(ValueError):
            curriculum.validate_deck(self.items, self.plan, reviewed=True)

    def test_missing_semantic_review_or_level_mismatch_is_rejected(self):
        for change in (lambda i: i.pop("_semantic_group"), lambda i: i.update(_semantic_level="advanced")):
            items = copy.deepcopy(self.items)
            change(items[0])
            with self.assertRaises(ValueError):
                curriculum.validate_deck(items, self.plan, reviewed=True)

    def test_vocabulary_and_tone_must_match(self):
        for change in (lambda i: i.update(Tone="invalid"), lambda i: i.update(Core_Vocab="wrong"),
                       lambda i: i.update(tips="Use it whenever you like."),
                       lambda i: i.update(vocab=[{"en": "nonexistent", "cn": "\u932f\u8aa4"}])):
            items = copy.deepcopy(self.items)
            change(items[0])
            with self.assertRaises(ValueError):
                curriculum.validate_deck(items, self.plan)

    def test_attested_metalinguistic_and_recheck_errors_are_rejected(self):
        for line in ("I need to say there is no hot water.",
                     "I want to show you my carry-on bag now.",
                     "Am I required to recheck in again?"):
            item = dict(self.items[0], sentence_en=line)
            with self.subTest(line=line), self.assertRaisesRegex(ValueError, "直接說出|check in again"):
                curriculum.validate_item(item, self.plan["jobs"][0])

    def test_staff_reply_cannot_be_a_learner_request_example(self):
        item = dict(self.items[0], word_en="Could you walk me through filing a complaint?",
                    sentence_en="Yes, I can walk you through filing a complaint.")
        with self.assertRaisesRegex(ValueError, "原說話者"):
            curriculum.validate_item(item, self.plan["jobs"][0])

    def test_simplified_chinese_is_normalized_without_changing_english_or_source(self):
        item = dict(self.items[0], word_cn="请问海关审查需要多长时间？", sentence_cn="我可以在哪里提取行李？",
                    tips="中立：当需要护照时，向工作人员询问。", vocab=[{"en": "passport", "cn": "护照"}],
                    Core_Vocab="passport 护照")
        normalized = curriculum.normalize_chinese(item)
        self.assertEqual(normalized["word_cn"], "請問海關審查需要多長時間？")
        self.assertEqual(normalized["sentence_cn"], "我可以在哪裡提取行李？")
        self.assertEqual(normalized["vocab"][0]["cn"], "護照")
        self.assertEqual(normalized["Core_Vocab"], "passport 護照")
        self.assertEqual(normalized["word_en"], item["word_en"])
        self.assertEqual(item["vocab"][0]["cn"], "护照")
        self.assertEqual(curriculum.normalize_chinese(normalized), normalized)
        with self.assertRaisesRegex(ValueError, "繁體中文"):
            curriculum.validate_item(item, self.plan["jobs"][0])

    def test_actual_passive_carry_exchange_and_repair_are_advanced_features(self):
        for line in ("How much formula may be carried?", "Could this item be exchanged?", "Can the air conditioning be repaired?"):
            with self.subTest(line=line):
                self.assertIn("passive voice", curriculum.explicit_advanced_features(line))

    def test_separable_phrasal_verbs_are_real_core_vocabulary(self):
        self.assertTrue(curriculum.vocab_occurs("take out", " do i need to take this out "))
        self.assertTrue(curriculum.vocab_occurs("tone down", " could you tone the spice down "))
        self.assertTrue(curriculum.vocab_occurs("put in writing", " please put it in writing "))
        self.assertFalse(curriculum.vocab_occurs("take out", " could you bring this out "))
        self.assertFalse(curriculum.vocab_occurs("credit card", " i have credit but no card "))
        self.assertTrue(curriculum.vocab_occurs("upgrade", " any chance of upgrading my seat "))
        self.assertTrue(curriculum.vocab_occurs("seat", " could we be seated by the window "))
        self.assertTrue(curriculum.vocab_occurs("take out", " we are taking these items out "))

    def test_generation_schema_owns_ids_and_advanced_main_lines(self):
        from types import SimpleNamespace
        phrase = "Could you walk me through baggage claim?"
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=json.dumps({"items": {"01": {"word_en": phrase}}})))])
        with patch.object(cards, "_call_openai", return_value=response) as api:
            result = curriculum.request_json("Write card", "教材生成 01-01", "gpt-4o-mini",
                job_ids=["01"], anchors={"01": {"word_en": phrase}})
        schema = api.call_args.kwargs["response_format"]["json_schema"]["schema"]
        keyed = schema["properties"]["items"]
        self.assertEqual(keyed["required"], ["01"])
        self.assertEqual(keyed["properties"]["01"]["properties"]["word_en"]["enum"], [phrase])
        self.assertNotIn("pattern", keyed["properties"]["01"]["properties"]["sentence_en"])
        self.assertEqual(result["items"][0]["id"], "01")

    def test_incomplete_empty_or_refused_api_response_is_not_parsed_as_curriculum(self):
        from types import SimpleNamespace
        for content, finish, refusal in (("", "length", None), ("", "stop", None),
                                         ("{}", "stop", "refused"), ("{}", "content_filter", None)):
            response = SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish,
                message=SimpleNamespace(content=content, refusal=refusal))])
            with self.subTest(finish=finish, refusal=refusal), \
                 patch.object(cards, "_call_openai", return_value=response), self.assertRaises(ValueError):
                curriculum.request_json("Write card", "教材生成 01-01", "gpt-4o-mini", job_ids=["01"])

    def test_attested_souvenir_ipa_error_is_not_exported(self):
        english = "I have souvenirs to declare."
        correct = "/aɪ hæv ˌsuːvəˈnɪrz tu dɪˈklɛr/"
        item = dict(self.items[0], word_en=english, sentence_en=english, word_ipa=correct, sentence_ipa=correct, tips="現場提示")
        point = {"target_phrase": english, "target_sentence": english, "task": english, "category": "customs"}
        self.assertIsNone(cards._locked_item_issue(item, point))
        item["word_ipa"] = "/aɪ hæv ˌsuːˈvɪnɚz tu dɪˈklɛr/"
        self.assertIn("souvenir", cards._locked_item_issue(item, point))

    def test_attested_airport_ipa_errors_are_rejected_and_corrected(self):
        cases = (("Could my seat be upgraded?", "/kʊd maɪ sit bi ˈʌɡreɪdɪd/", "ʌpˈɡreɪdɪd"),
                 ("Where is my connecting flight?", "/wɛr ɪz maɪ kənˈnɛktɪŋ flaɪt/", "kəˈnɛktɪŋ"))
        for line, wrong, correct in cases:
            item = dict(self.items[0], word_en=line, sentence_en=line, word_ipa=wrong, sentence_ipa=wrong, tips="現場提示")
            point = {"target_phrase": line, "target_sentence": line, "task": line, "category": "airport"}
            with self.subTest(line=line):
                self.assertIsNotNone(cards._locked_item_issue(item, point))
                fixed = cards._correct_known_locked_pronunciations(item, point)
                self.assertIn(correct, fixed["word_ipa"])
                self.assertIsNone(cards._locked_item_issue(fixed, point))
                self.assertEqual(item["word_ipa"], wrong)

    def test_attested_product_british_vowel_is_corrected_with_exact_alignment(self):
        line = "What is in this product?"
        wrong = "/wʌt ɪz ɪn ðɪs ˈprɒdʌkt/"
        item = dict(self.items[0], word_en=line, sentence_en=line, word_ipa=wrong, sentence_ipa=wrong)
        point = {"target_phrase": line, "target_sentence": line, "task": line, "category": "shopping"}
        fixed = cards._correct_known_locked_pronunciations(item, point)
        self.assertIn("ˈprɑdʌkt", fixed["word_ipa"])
        self.assertEqual(item["word_ipa"], wrong)
        self.assertEqual(cards._correct_known_locked_pronunciations(item, dict(point, target_phrase="Other line"))["word_ipa"], wrong)

    def test_attested_what_quantity_and_product_possessive_vowels(self):
        line = "What is the product's quantity?"
        wrong = "/wɒt ɪz ðə ˈprɒdʌkts ˈkwɒntəti/"
        item = dict(self.items[0], word_en=line, sentence_en=line, word_ipa=wrong, sentence_ipa=wrong)
        point = {"target_phrase": line, "target_sentence": line, "task": line, "category": "shopping"}
        fixed = cards._correct_known_locked_pronunciations(item, point)
        self.assertEqual(fixed["word_ipa"], "/wʌt ɪz ðə ˈprɑdʌkts ˈkwɑntəti/")
        self.assertEqual(item["word_ipa"], wrong)

    def test_advanced_progression_cannot_be_just_a_tier_label(self):
        job = next(job for job in self.plan["jobs"] if job["tier"] == "advanced")
        item = copy.deepcopy(next(item for item in self.items if item["id"] == job["id"]))
        for placeholder in ("", "advanced", "basic"):
            item["progression"] = placeholder
            with self.subTest(placeholder=placeholder), self.assertRaises(ValueError):
                curriculum.validate_item(item, job)

    def test_politeness_alone_is_not_advanced_progression(self):
        for reason in ("Use of 'could' for polite request.", "使用 could 表達委婉請求", "May is polite",
                       "The term confirm makes the request more formal"):
            with self.subTest(reason=reason), self.assertRaises(ValueError):
                curriculum.validate_progression(reason)
        curriculum.validate_progression("Passive voice in a polite request with could")
        curriculum.validate_progression("使用 mind + 動名詞的委婉請求")
        curriculum.validate_progression("使用 clarify、covers 與嵌入問句，不只增加禮貌詞。")
        curriculum.validate_progression("Uses outline, a formal verb, and applicable surcharges")

    def test_basic_examples_cannot_acquire_known_advanced_grammar_during_generation(self):
        job = self.plan["jobs"][0]
        item = copy.deepcopy(self.items[0])
        item.update(word_en="Do I need to show my ID?", word_ipa="/du aɪ nid tu ʃoʊ maɪ aɪ di/",
                    sentence_en="Am I required to show my ID?", sentence_ipa="/æm aɪ rɪˈkwaɪərd tu ʃoʊ maɪ aɪ di/",
                    vocab=[{"en": "show", "cn": "出示"}], Core_Vocab="show 出示")
        with self.assertRaisesRegex(ValueError, "sentence_en.*基礎難度"):
            curriculum.validate_item(item, job)

    def test_explicit_advanced_features_are_actual_language_not_politeness(self):
        for line in ("Would you mind sending up extra towels?", "Could extra towels be delivered?",
                     "Could my boarding pass be issued?", "Am I eligible for online check-in?",
                     "Could you clarify which ingredients are used?", "Could you point me toward my gate?"):
            with self.subTest(line=line):
                self.assertTrue(curriculum.explicit_advanced_features(line))
        for line in ("Could you bring extra towels, please?", "Would it be possible to check in online?",
                     "Can I check in now?"):
            with self.subTest(line=line):
                self.assertEqual(curriculum.explicit_advanced_features(line), [])

    def test_spelled_initialisms_and_room_numbers_allow_complete_ipa(self):
        cases = (("Do I need to show my ID?", "/du aɪ nid tu ʃoʊ maɪ aɪ di/"),
                 ("I need room 204.", "/aɪ nid rum tu oʊ fɔr/"))
        for line, ipa in cases:
            item = dict(self.items[0], word_en=line, sentence_en=line, word_ipa=ipa, sentence_ipa=ipa, tips="現場提示")
            point = {"target_phrase": line, "target_sentence": line, "task": line, "category": "hotel"}
            with self.subTest(line=line):
                self.assertIsNone(cards._locked_item_issue(item, point))
                item["word_ipa"] = "/du aɪ/"
                self.assertIsNotNone(cards._locked_item_issue(item, point))

    def test_lexical_candidates_cannot_hide_synonyms_under_different_labels(self):
        plan = {"jobs": [{"id": "01", "core": "bring an item"}, {"id": "02", "core": "carry liquids"}]}
        items = [{"id": "01", "word_en": "Can I bring this on the plane?", "sentence_en": "Can I bring this item?"},
                 {"id": "02", "word_en": "Can I bring liquids on board?", "sentence_en": "Can I bring liquids?"}]
        semantic = {"assignments": [{"id": "01", "purpose": "item permission"}, {"id": "02", "purpose": "liquid permission"}]}
        with patch.object(curriculum, "request_json", return_value={"checks": {
            "01:02": {"equivalent": True, "reason": "Same carry-permission outcome, only object changes"}}}) as request:
            curriculum.verify_semantic_pairs(plan, items, semantic, {"groups": []})
        self.assertEqual(request.call_args.kwargs["job_ids"], ["01:02"])
        self.assertEqual(semantic["assignments"][0]["purpose"], semantic["assignments"][1]["purpose"])

    def test_attested_british_vowels_are_corrected_only_for_known_words(self):
        line = "Should I remove my laptop?"
        item = dict(self.items[0], word_en=line, sentence_en=line,
                    word_ipa="/ʃʊd aɪ rɪˈmuv maɪ ˈlæptɒp/", sentence_ipa="/ʃʊd aɪ rɪˈmuv maɪ ˈlæptɒp/")
        point = {"target_phrase": line, "target_sentence": line, "task": line, "category": "security"}
        corrected = cards._correct_known_locked_pronunciations(item, point)
        self.assertIn("ˈlæptɑp", corrected["word_ipa"])
        self.assertIn("ˈlæptɒp", item["word_ipa"])

    def test_cross_label_audit_has_adequate_output_budget_without_heavy_reasoning(self):
        from types import SimpleNamespace
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"groups":[]}'))])
        with patch.object(cards, "_call_openai", return_value=response) as api:
            curriculum.request_json("Find duplicates", "教材跨標籤同義複核", "gpt-5-nano", job_ids=["01", "02"])
        self.assertEqual(api.call_args.kwargs["max_completion_tokens"], 20000)
        self.assertEqual(api.call_args.kwargs["reasoning_effort"], "low")

    def test_full_semantic_review_uses_configurable_reasoning_budget(self):
        from types import SimpleNamespace
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{}'))])
        with patch.object(cards, "_call_openai", return_value=response) as api, \
             patch.object(curriculum, "SEMANTIC_REASONING", "low"):
            curriculum.request_json("Classify", "教材完整語意分組", "gpt-5-nano", job_ids=["01"])
        self.assertEqual(api.call_args.kwargs["reasoning_effort"], "low")
        self.assertEqual(api.call_args.kwargs["max_completion_tokens"], 20000)

    def test_review_requests_use_strict_complete_assignment_schema(self):
        from types import SimpleNamespace
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{}'))])
        with patch.object(cards, "_call_openai", return_value=response) as api:
            curriculum.request_json("Review", "教材完整語意分組", "gpt-4o-mini", job_ids=["01", "02"])
        schema = api.call_args.kwargs["response_format"]["json_schema"]
        self.assertTrue(schema["strict"])
        array = schema["schema"]["properties"]["assignments"]
        self.assertEqual((array["minItems"], array["maxItems"]), (2, 2))
        self.assertEqual(array["items"]["properties"]["id"]["enum"], ["01", "02"])

    def test_force_backup_preserves_workbook_and_both_sidecars(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / name for name in ("cards.xlsx", "cards.plan.json", "cards.xlsx.curriculum.json")]
            for path in paths:
                path.write_bytes(path.name.encode())
            with patch.object(cards, "BASE_DIR", directory):
                backup = curriculum.backup_generation(paths)
            manifest = json.loads((backup / "manifest.json").read_text())
            self.assertEqual(len(manifest["files"]), 3)
            for record, original in zip(manifest["files"], paths):
                self.assertEqual((backup / record["backup"]).read_bytes(), original.read_bytes())

    def test_malformed_reviewer_payload_does_not_pass(self):
        for response in ({}, {"reject": "none"}, {"reject": [{"id": "99", "reason": "bad"}]},
                         {"reject": [{"id": "01", "reason": ""}]},
                         {"reject": [{"id": "01", "reason": "bad"}, {"id": "01", "reason": "bad"}]}):
            with self.subTest(response=response), self.assertRaises(ValueError):
                curriculum.parse_rejections(response, {"01"})

    @patch.object(curriculum, "review_field_difficulty", return_value={})
    def test_independent_review_checks_every_semantic_assignment(self, _field_review):
        semantic = {"assignments": [{"id": item["id"], "purpose": item["core"], "level": item["tier"],
                                     "progression": item["progression"]} for item in self.items], "reject": []}
        with patch.object(curriculum, "request_json", side_effect=[{"reject": []}, semantic, {"groups": []}]):
            self.assertEqual(curriculum.review_deck(self.plan, self.items, []), {})
        incomplete = copy.deepcopy(semantic)
        incomplete["assignments"].pop()
        with patch.object(curriculum, "request_json", side_effect=[{"reject": []}, incomplete]), self.assertRaises(ValueError):
            curriculum.review_deck(self.plan, self.items, [])

    @patch.object(curriculum, "review_field_difficulty", return_value={})
    def test_style_rejection_requires_independent_objective_confirmation(self, _field_review):
        semantic = {"assignments": [{"id": item["id"], "purpose": item["core"], "level": item["tier"],
                                     "progression": item["progression"]} for item in self.items], "reject": []}
        with patch.object(curriculum, "request_json", side_effect=[
            {"reject": [{"id": "01", "reason": "I prefer a different phrasing"}]},
            {"checks": [{"id": "01", "valid": True, "reason": ""}]}, semantic, {"groups": []}]):
            self.assertEqual(curriculum.review_deck(self.plan, self.items, []), {})
        with patch.object(curriculum, "request_json", side_effect=[
            {"reject": [{"id": "01", "reason": "IPA wrong"}]},
            {"checks": [{"id": "01", "valid": False, "reason": "Missing a word in the IPA"}]}, semantic, {"groups": []}]):
            self.assertEqual(curriculum.review_deck(self.plan, self.items, [])["01"], "Missing a word in the IPA")

    @patch.object(curriculum, "review_field_difficulty", return_value={})
    def test_copy_edit_prompts_allow_natural_yes_no_questions(self, _field_review):
        semantic = {"assignments": [{"id": item["id"], "purpose": item["core"], "level": item["tier"],
                                     "progression": item["progression"]} for item in self.items], "reject": []}
        with patch.object(curriculum, "request_json", side_effect=[
            {"reject": [{"id": "01", "reason": "Must ask What/When/Where"}]},
            {"checks": [{"id": "01", "valid": True, "reason": "Natural yes/no question"}]},
            semantic, {"groups": []}]) as api:
            self.assertEqual(curriculum.review_deck(self.plan, self.items, []), {})
        self.assertIn("What/When/Where question is NOT mandatory", api.call_args_list[0].args[0])
        self.assertIn("Do NOT require What/When/Where", api.call_args_list[1].args[0])

    def test_missing_confirmation_cannot_silently_clear_language_rejection(self):
        with patch.object(curriculum, "request_json", side_effect=[
            {"reject": [{"id": "01", "reason": "IPA wrong"}]}, {"checks": []}]), self.assertRaises(ValueError):
            curriculum.review_deck(self.plan, self.items, [])

    @patch.object(curriculum, "review_field_difficulty", return_value={})
    def test_fidelity_rejection_cannot_be_cleared_by_coarse_reviews(self, _field_review):
        semantic = {"assignments": [{"id": item["id"], "purpose": item["core"], "level": item["tier"],
                                     "progression": item["progression"]} for item in self.items], "reject": []}
        with patch.object(curriculum, "review_task_fidelity", return_value={"01": "Permission is not obligation"}), \
                patch.object(curriculum, "request_json", side_effect=[{"reject": []}, semantic, {"groups": []}]):
            self.assertEqual(curriculum.review_deck(self.plan, self.items, [])["01"], "Permission is not obligation")

    @patch.object(curriculum, "review_field_difficulty", return_value={"01": "word_en is actually advanced"})
    def test_reviewer_difficulty_disagreement_is_not_silently_accepted(self, _field_review):
        semantic = {"assignments": [{"id": item["id"], "purpose": item["core"], "level": item["tier"],
                                     "progression": item["progression"]} for item in self.items], "reject": []}
        semantic["assignments"][0].update(level="advanced", progression="New idiom")
        with patch.object(curriculum, "request_json", side_effect=[{"reject": []}, semantic, {"groups": []}]):
            self.assertIn("01", curriculum.review_deck(self.plan, self.items, []))

    def test_coarse_card_guess_cannot_override_successful_individual_field_review(self):
        semantic = {"assignments": [{"id": item["id"], "purpose": item["core"], "level": "basic",
                                     "progression": "coarse guess"} for item in self.items], "reject": []}
        with patch.object(curriculum, "review_field_difficulty", return_value={}), \
                patch.object(curriculum, "request_json", side_effect=[{"reject": []}, semantic, {"groups": []}]):
            self.assertEqual(curriculum.review_deck(self.plan, self.items, []), {})
        self.assertTrue(all(item["_semantic_level"] == item["tier"] for item in self.items))

    def test_basic_advanced_pair_uses_verified_progression_not_blank_coarse_guess(self):
        self.plan["jobs"][2]["core"] = self.plan["jobs"][0]["core"]
        self.items[2]["core"] = self.items[0]["core"]
        semantic = {"assignments": [{"id": item["id"], "purpose": item["core"], "level": "basic",
                                     "progression": ""} for item in self.items], "reject": []}
        with patch.object(curriculum, "review_field_difficulty", return_value={}), \
                patch.object(curriculum, "request_json", side_effect=[{"reject": []}, semantic, {"groups": []},
                    {"checks": {"01:03": {"equivalent": True, "reason": "Same outcome"}}}]):
            self.assertEqual(curriculum.review_deck(self.plan, self.items, []), {})

    @patch.object(curriculum, "review_field_difficulty", return_value={})
    def test_cross_label_audit_catches_equivalent_lines_with_different_labels(self, _field_review):
        semantic = {"assignments": [{"id": item["id"], "purpose": item["core"], "level": item["tier"],
                                     "progression": item["progression"]} for item in self.items], "reject": []}
        with patch.object(curriculum, "request_json", side_effect=[{"reject": []}, semantic,
            {"groups": [{"ids": ["01", "02"], "purpose": "Same outcome under different labels"}]},
            {"checks": {"01:02": {"equivalent": True, "reason": "Same concrete result"}}}]):
            rejected = curriculum.review_deck(self.plan, self.items, [])
        self.assertTrue(rejected["02"].startswith("語意重複："))

    @patch.object(curriculum, "review_field_difficulty", return_value={})
    def test_broad_labels_do_not_merge_different_information_outcomes(self, _field_review):
        semantic = {"assignments": [{"id": item["id"], "purpose": item["core"], "level": item["tier"],
                                     "progression": item["progression"]} for item in self.items], "reject": []}
        semantic["assignments"][1]["purpose"] = semantic["assignments"][0]["purpose"]
        with patch.object(curriculum, "request_json", side_effect=[{"reject": []}, semantic, {"groups": []},
            {"checks": {"01:02": {"equivalent": False, "reason": "Name and dates are different information"}}}]):
            self.assertEqual(curriculum.review_deck(self.plan, self.items, []), {})
        self.assertNotEqual(self.items[0]["_semantic_group"], self.items[1]["_semantic_group"])

    def test_difficulty_placeholder_is_retried_without_rewriting_valid_cards(self):
        with patch.object(curriculum, "request_json", side_effect=[
            {"checks": [{"id": "01", "level": "basic", "progression": "N/A"}]},
            {"checks": [{"id": "01", "level": "advanced", "progression": "Passive obligation: am I required to"}]}]) as request:
            checks = curriculum.confirm_difficulty([{"id": "01"}])
        self.assertEqual(checks[0]["level"], "advanced")
        self.assertEqual(request.call_count, 2)

    def test_difficulty_retries_preserve_valid_checks_and_fail_closed_per_field(self):
        valid = {"id": "01", "level": "basic", "progression": "Simple daily words"}
        invalid = {"id": "02", "level": "advanced", "progression": "Could makes it polite"}
        with patch.object(curriculum, "request_json", side_effect=[{"checks": [valid, invalid]},
                {"checks": [invalid]}, {"checks": [invalid]}]) as request:
            checks = curriculum.confirm_difficulty([{"id": "01"}, {"id": "02"}])
        self.assertEqual(request.call_args_list[1].kwargs["job_ids"], ["02"])
        self.assertEqual(checks[0], valid)
        self.assertFalse(checks[1]["_valid"])

    def test_unverified_basic_field_cannot_pass_via_default_level(self):
        item = {"id": "01", "tier": "basic", "word_en": "Can you help?", "sentence_en": "Can you help me?"}
        with patch.object(curriculum, "confirm_difficulty", return_value=[
                {"id": "F001", "level": "basic", "_valid": False, "progression": "Missing evidence"},
                {"id": "F002", "level": "basic", "progression": "Simple help request"}]):
            self.assertIn("未能核定難度", curriculum.review_field_difficulty([item])["01"])

    def test_advanced_main_cannot_rescue_basic_example(self):
        item = {"id": "09", "tier": "advanced", "word_en": "Am I eligible to check in online?",
                "sentence_en": "Could you confirm if I can check in online today?"}
        with patch.object(curriculum, "confirm_difficulty", return_value=[{
                "id": "F001", "level": "basic", "progression": "Simple confirm question"}]) as confirm:
            rejected = curriculum.review_field_difficulty([item])
        self.assertIn("sentence_en", rejected["09"])
        self.assertEqual(confirm.call_args.args[0][0]["line_en"], item["sentence_en"])
        self.assertNotIn("word_en", confirm.call_args.args[0][0])
        self.assertNotIn("sentence_en", confirm.call_args.args[0][0])

    def test_basic_main_cannot_hide_advanced_example(self):
        item = {"id": "01", "tier": "basic", "word_en": "Do I need to show my ID?",
                "sentence_en": "Am I required to show my ID?"}
        with patch.object(curriculum, "confirm_difficulty", return_value=[{
                "id": "F001", "level": "basic", "progression": "Simple need question"}]):
            rejected = curriculum.review_field_difficulty([item])
        self.assertIn("passive voice", rejected["01"])

    def test_identical_english_is_rated_once_without_field_role_labels(self):
        item = {"id": "01", "tier": "basic", "word_en": "Can you help me?", "sentence_en": "Can you help me?"}
        with patch.object(curriculum, "confirm_difficulty", return_value=[{
                "id": "F001", "level": "basic", "progression": "Simple help request"}]) as confirm:
            self.assertEqual(curriculum.review_field_difficulty([item]), {})
        self.assertEqual(len(confirm.call_args.args[0]), 1)
        self.assertNotIn("word_en", confirm.call_args.args[0][0]["id"])
        self.assertNotIn("sentence_en", confirm.call_args.args[0][0]["id"])

    def test_review_deck_enforces_individual_field_rejections(self):
        semantic = {"assignments": [{"id": item["id"], "purpose": item["core"], "level": item["tier"],
                                     "progression": item["progression"]} for item in self.items], "reject": []}
        with patch.object(curriculum, "review_field_difficulty", return_value={"03": "sentence_en is too basic"}), \
                patch.object(curriculum, "request_json", side_effect=[{"reject": []}, semantic, {"groups": []}]):
            self.assertIn("sentence_en is too basic", curriculum.review_deck(self.plan, self.items, [])["03"])

    def test_only_new_curriculum_allows_natural_main_lines_over_eight_words(self):
        job = next(job for job in self.plan["jobs"] if job["tier"] == "advanced")
        item = next(item for item in self.items if item["id"] == job["id"])
        line = "Would you mind putting all of these details in writing?"
        ipa = "/wʊd ju maɪnd ˈpʊtɪŋ ɔl əv ðiz dɪˈteɪlz ɪn ˈraɪtɪŋ/"
        item.update(word_en=line, sentence_en=line, word_ipa=ipa, sentence_ipa=ipa,
            vocab=[{"en": "in writing", "cn": "書面"}], Core_Vocab="in writing 書面",
            progression="mind + -ing request")
        curriculum.validate_deck(self.items, self.plan, reviewed=True)
        self.assertTrue(any(issue.startswith("word_en has ") for issue in cards._validation_issues(item)))
        with tempfile.TemporaryDirectory() as directory:
            target = str(Path(directory) / "cards.xlsx")
            cards.write_xlsx(self.items, target, curriculum_plan=self.plan)
            self.assertEqual(cards.load_xlsx_items(target)[int(job["id"]) - 1]["word_en"], line)

    def test_equivalence_group_merges_are_transitive_and_validate_ids(self):
        semantic = {"assignments": [{"id": job["id"], "purpose": job["core"]} for job in self.plan["jobs"]]}
        curriculum.merge_equivalent_groups(semantic, {"groups": [
            {"ids": ["01", "02"], "purpose": "first pair"},
            {"ids": ["02", "03"], "purpose": "second pair"}]})
        self.assertEqual(len({entry["purpose"] for entry in semantic["assignments"][:3]}), 1)
        for payload in ({}, {"groups": [{"ids": ["01", "99"], "purpose": "bad"}]},
                        {"groups": [{"ids": ["01", "01"], "purpose": "bad"}]},
                        {"groups": [{"ids": ["01", "02"], "purpose": ""}]}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                curriculum.merge_equivalent_groups(semantic, payload)

    def test_equivalence_merge_preserves_already_normalized_groups(self):
        semantic = {"assignments": [
            {"id": "01", "purpose": "other outcome"}, {"id": "02", "purpose": "Gate information"},
            {"id": "03", "purpose": "gate information!"}]}
        curriculum.merge_equivalent_groups(semantic, {"groups": [
            {"ids": ["01", "02"], "purpose": "same outcome"}]})
        self.assertEqual(len({entry["purpose"] for entry in semantic["assignments"]}), 1)

    def test_basic_advanced_pair_can_span_scenarios_but_still_counts_together(self):
        first = self.plan["jobs"][0]
        other = next(job for job in self.plan["jobs"] if job["tier"] == "advanced" and job["Scenario"] != first["Scenario"])
        other["core"] = first["core"]
        curriculum.validate_plan(self.plan, self.plan["topic"], 10)
        self.plan["jobs"][1]["core"] = first["core"]
        with self.assertRaises(ValueError):
            curriculum.validate_plan(self.plan, self.plan["topic"], 10)

    def test_renaming_review_groups_cannot_split_a_planned_synonym_pair(self):
        advanced = next(job for job in self.plan["jobs"] if job["tier"] == "advanced")
        advanced["core"] = self.plan["jobs"][0]["core"]
        semantic = {"assignments": [{"id": job["id"], "purpose": "new-label-" + job["id"]}
                                     for job in self.plan["jobs"]]}
        curriculum.reconcile_semantic_groups(self.plan, semantic)
        by_id = {entry["id"]: entry["purpose"] for entry in semantic["assignments"]}
        self.assertEqual(by_id["01"], by_id[advanced["id"]])

    def test_semantic_merges_are_transitive_across_plan_and_actual_meaning(self):
        self.plan["jobs"][2]["core"] = self.plan["jobs"][0]["core"]
        semantic = {"assignments": [{"id": job["id"], "purpose": job["id"]}
                                     for job in self.plan["jobs"]]}
        semantic["assignments"][2]["purpose"] = semantic["assignments"][1]["purpose"]
        curriculum.reconcile_semantic_groups(self.plan, semantic)
        self.assertEqual(len({entry["purpose"] for entry in semantic["assignments"][:3]}), 1)

    def test_learner_only_brief_is_enforced_by_code(self):
        plan = copy.deepcopy(self.plan)
        plan["topic"] += "\n\u4e0d\u6536\u9304\u5e97\u54e1\u539f\u8a71"
        plan["jobs"][0]["role"] = "counterpart"
        with self.assertRaises(ValueError):
            curriculum.validate_plan(plan, plan["topic"], 10)

    def test_planner_uses_real_topic_scenarios_not_sales_constants(self):
        for topic in ("Restaurant ordering", "Airport immigration"):
            plan, _ = fixture(topic)
            with patch.object(curriculum, "request_json", side_effect=[
                {"scenarios": plan["scenarios"]}, {"jobs": plan["jobs"]},
                {"assignments": [{"id": job["id"], "purpose": job["core"], "in_scope": True, "reason": ""}
                                 for job in plan["jobs"]]}
            ]) as request:
                result = curriculum.plan_curriculum(topic, 10, [])
            self.assertEqual(result["scenarios"], plan["scenarios"])
            self.assertTrue(all(topic in call.args[0] for call in request.call_args_list))

    def test_export_preserves_all_twelve_columns_and_video_loader_compatibility(self):
        import main
        with tempfile.TemporaryDirectory() as directory:
            target = str(Path(directory) / "cards.xlsx")
            cards.write_xlsx(self.items, target, curriculum_plan=self.plan)
            actual = cards.load_xlsx_items(target)
            self.assertEqual(list(actual[0]), LEARNING_HEADERS)
            self.assertEqual(actual, [{key: item[key] for key in LEARNING_HEADERS} for item in self.items])
            self.assertEqual(len(main.import_review_excel(target)), 10)

    def test_invalid_export_never_writes_workbook(self):
        self.items[0]["Level"] = "wrong"
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "cards.xlsx"
            with self.assertRaises(ValueError):
                cards.write_xlsx(self.items, str(target), curriculum_plan=self.plan)
            self.assertFalse(target.exists())

    def test_batch_retry_rejects_incomplete_or_duplicate_ids(self):
        responses = [{"items": self.items[:1]}, {"items": [self.items[0], self.items[0]]}, {"items": self.items[:2]}]
        with patch.object(curriculum, "request_json", side_effect=responses) as request:
            result = curriculum.generate_batch(self.plan, self.plan["jobs"][:2], [])
        self.assertEqual(len(result), 2)
        self.assertEqual(request.call_count, 3)

    def test_batch_retry_only_rewrites_invalid_rows(self):
        invalid = copy.deepcopy(self.items[:2])
        invalid[0]["Tone"] = "invalid"
        with patch.object(curriculum, "request_json", side_effect=[{"items": invalid}, {"items": self.items[:1]}]) as request:
            result = curriculum.generate_batch(self.plan, self.plan["jobs"][:2], [])
        self.assertEqual([item["id"] for item in result], ["01", "02"])
        self.assertEqual(request.call_args.kwargs["job_ids"], ["01"])
        self.assertEqual(request.call_args.args[2], curriculum.REPAIR_MODEL)
        self.assertIn("LATEST correction", request.call_args.args[0])

    @patch.object(curriculum, "review_field_difficulty", return_value={})
    def test_advanced_line_is_drafted_separately_and_cannot_be_simplified(self, _field_review):
        job = next(job for job in self.plan["jobs"] if job["tier"] == "advanced")
        item = copy.deepcopy(next(item for item in self.items if item["id"] == job["id"]))
        item.update(sentence_en=item["word_en"], sentence_ipa=item["word_ipa"], sentence_cn=item["word_cn"])
        simplified = dict(item, word_en="Can you help?")
        draft = {"lines": [{"id": job["id"], "word_en": item["word_en"], "sentence_en": item["sentence_en"], "progression": item["progression"]}]}
        with patch.object(curriculum, "request_json", side_effect=[draft, {"items": [simplified]}, {"items": [item]}]) as request:
            result = curriculum.generate_batch(self.plan, [job], [])
        self.assertEqual(request.call_args_list[0].args[1], "進階英文骨架")
        self.assertEqual(result[0]["word_en"], item["word_en"])

    @patch.object(curriculum, "review_field_difficulty", return_value={})
    def test_advanced_example_may_paraphrase_without_exact_word_order(self, _field_review):
        job = next(job for job in self.plan["jobs"] if job["tier"] == "advanced")
        item = copy.deepcopy(next(item for item in self.items if item["id"] == job["id"]))
        draft = {"lines": [{"id": job["id"], "word_en": item["word_en"], "sentence_en": item["sentence_en"], "progression": item["progression"]}]}
        with patch.object(curriculum, "request_json", side_effect=[draft, {"items": [item]}]):
            result = curriculum.generate_batch(self.plan, [job], [], "sentence_en 必須保留 word_en")
        self.assertEqual(result[0]["sentence_en"], item["sentence_en"])

    @patch.object(curriculum, "review_field_difficulty", return_value={})
    def test_resumed_advanced_draft_failure_feedback_reaches_next_draft(self, _field_review):
        job = next(job for job in self.plan["jobs"] if job["tier"] == "advanced")
        item = copy.deepcopy(next(item for item in self.items if item["id"] == job["id"]))
        draft = {"lines": [{key: item[key] for key in ("id", "word_en", "sentence_en", "progression")}]}
        feedback = json.dumps({"stage": "advanced_draft", "issues": {job["id"]: "Preserve passive obligation, not permission"}})
        with patch.object(curriculum, "request_json", side_effect=[draft, {"items": [item]}]) as api:
            curriculum.generate_batch(self.plan, [job], [], feedback)
        self.assertIn("Preserve passive obligation, not permission", api.call_args_list[0].args[0])

    @patch.object(curriculum, "review_field_difficulty", return_value={})
    def test_advanced_draft_retries_only_invalid_ids(self, _field_review):
        jobs = [job for job in self.plan["jobs"] if job["tier"] == "advanced"][:2]
        items = [copy.deepcopy(next(item for item in self.items if item["id"] == job["id"])) for job in jobs]
        lines = [{key: item[key] for key in ("id", "word_en", "sentence_en", "progression")} for item in items]
        invalid = dict(lines[1], word_en="Could you help me, please?", sentence_en="Could you help me now, please?",
                       progression="Use could for a polite request")
        with patch.object(curriculum, "request_json", side_effect=[{"lines": [lines[0], invalid]},
                {"lines": [lines[1]]}, {"items": items}]) as request:
            result = curriculum.generate_batch(self.plan, jobs, [])
        self.assertEqual(request.call_args_list[1].kwargs["job_ids"], [jobs[1]["id"]])
        self.assertEqual([item["id"] for item in result], [job["id"] for job in jobs])

    def test_failed_advanced_draft_still_preserves_basic_cards(self):
        basic = next(job for job in self.plan["jobs"] if job["tier"] == "basic")
        advanced = next(job for job in self.plan["jobs"] if job["tier"] == "advanced")
        item = copy.deepcopy(next(item for item in self.items if item["id"] == basic["id"]))
        invalid = {"lines": [{"id": advanced["id"], "word_en": "Could you help me?",
            "sentence_en": "Could you help me, please?", "progression": "Could is polite"}]}
        with patch.object(curriculum, "request_json", side_effect=[invalid, invalid, invalid, {"items": [item]}]), \
                self.assertRaises(curriculum.BatchGenerationError) as failure:
            curriculum.generate_batch(self.plan, [basic, advanced], [])
        self.assertEqual([row["id"] for row in failure.exception.items], [basic["id"]])

    def test_basic_example_is_rewritten_during_drafting_before_ipa_generation(self):
        job = next(job for job in self.plan["jobs"] if job["tier"] == "advanced")
        item = copy.deepcopy(next(item for item in self.items if item["id"] == job["id"]))
        line = {key: item[key] for key in ("id", "word_en", "sentence_en", "progression")}
        with patch.object(curriculum, "review_field_difficulty", side_effect=[{job["id"]: "sentence_en is basic"}, {}]), \
                patch.object(curriculum, "request_json", side_effect=[{"lines": [line]}, {"lines": [line]},
                    {"items": [item]}]) as request:
            result = curriculum.generate_batch(self.plan, [job], [])
        self.assertEqual(request.call_args_list[1].args[1], "進階英文骨架")
        self.assertEqual(result[0]["id"], job["id"])

    def test_explicit_draft_features_repair_inaccurate_progression(self):
        job = next(job for job in self.plan["jobs"] if job["tier"] == "advanced")
        line = {"id": job["id"], "word_en": "Could my boarding pass be issued?",
                "sentence_en": "Could my onward boarding pass be issued here?", "progression": "Could is polite"}
        with patch.object(curriculum, "request_json", side_effect=[{"lines": [line]}, ValueError("stop"),
                ValueError("stop"), ValueError("stop")]) as request, self.assertRaises(curriculum.BatchGenerationError):
            curriculum.generate_batch(self.plan, [job], [])
        self.assertIn("passive voice", request.call_args_list[1].kwargs["anchors"][job["id"]]["progression"])
        self.assertEqual(request.call_args_list[1].args[1], "教材生成 " + job["id"] + "-" + job["id"])

    def test_failed_batch_persists_valid_rows_for_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "checkpoint.json"
            error = curriculum.BatchGenerationError(self.items[:1], "02: invalid IPA")
            with patch.object(curriculum, "generate_batch", side_effect=error), self.assertRaises(curriculum.BatchGenerationError):
                curriculum.generate_deck(self.plan, checkpoint, [])
            saved = json.loads(checkpoint.read_text())
            self.assertEqual(saved["items"], self.items[:1])
            self.assertEqual(saved["feedback"], "02: invalid IPA")
            self.assertFalse(saved["review_passed"])

    def test_final_rejection_is_saved_and_resume_rewrites_only_failed_card(self):
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "checkpoint.json"
            with patch.object(curriculum, "generate_batch", side_effect=[self.items[:8], self.items[8:], self.items[:1], self.items[:1]]), \
                    patch.object(curriculum, "review_deck", return_value={"01": "Incorrect English"}), \
                    self.assertRaises(RuntimeError):
                curriculum.generate_deck(self.plan, checkpoint, [])
            state = json.loads(checkpoint.read_text())
            self.assertEqual(state["pending_review_rejections"], {"01": "Incorrect English"})
            self.assertIn("Incorrect English", state["feedback"])
            with patch.object(curriculum, "generate_batch", return_value=self.items[:1]) as generate, \
                    patch.object(curriculum, "review_deck", return_value={}):
                curriculum.generate_deck(self.plan, checkpoint, [], resume=True)
            self.assertEqual([job["id"] for job in generate.call_args.args[1]], ["01"])

    def test_resume_requires_matching_contract_and_reaudits_completed_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "checkpoint.json"
            with patch.object(curriculum, "generate_batch", side_effect=[self.items[:8], self.items[8:]]), \
                 patch.object(curriculum, "review_deck", return_value={}):
                curriculum.generate_deck(self.plan, checkpoint, [])
            with patch.object(curriculum, "generate_batch") as generate, \
                 patch.object(curriculum, "review_deck", return_value={}) as audit:
                curriculum.generate_deck(self.plan, checkpoint, [], resume=True)
            generate.assert_not_called()
            audit.assert_called_once()
            changed = copy.deepcopy(self.plan)
            changed["topic"] = "Restaurant ordering"
            with self.assertRaises(ValueError):
                curriculum.generate_deck(changed, checkpoint, [], resume=True)

    def test_resume_reaudits_obsolete_rejections_without_rewriting_valid_cards(self):
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "checkpoint.json"
            curriculum.save_json(checkpoint, {"version": curriculum.VERSION,
                "fingerprint": curriculum.fingerprint_for(self.plan, []), "plan": self.plan,
                "items": self.items, "review_passed": False,
                "pending_review_rejections": {"01": "Obsolete difficulty guess"},
                "pending_review_rejections_version": curriculum.SEMANTIC_REVIEW_VERSION - 1})
            with patch.object(curriculum, "generate_batch") as generate, \
                    patch.object(curriculum, "review_deck", return_value={}):
                state = curriculum.generate_deck(self.plan, checkpoint, [], resume=True)
            generate.assert_not_called()
            self.assertTrue(state["review_passed"])

    def test_resume_keeps_generation_feedback_without_a_review_rejection_version(self):
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "checkpoint.json"
            curriculum.save_json(checkpoint, {"version": curriculum.VERSION,
                "fingerprint": curriculum.fingerprint_for(self.plan, []), "plan": self.plan,
                "items": self.items[:1], "review_passed": False, "feedback": "Missing IPA in pending card"})
            with patch.object(curriculum, "generate_batch", side_effect=[self.items[1:9], self.items[9:]]) as generate, \
                    patch.object(curriculum, "review_deck", return_value={}):
                curriculum.generate_deck(self.plan, checkpoint, [], resume=True)
            self.assertEqual(generate.call_args_list[0].args[3], "Missing IPA in pending card")

    def test_duplicate_task_is_replaced_and_checkpoint_stays_resumable(self):
        replacement = dict(self.plan["jobs"][0], core="new-purpose", task="New actual decision")
        repaired = dict(self.items[0], core="new-purpose", _semantic_group="new-purpose")
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "checkpoint.json"
            with patch.object(curriculum, "generate_batch", side_effect=[self.items[:8], self.items[8:], [repaired]]), \
                 patch.object(curriculum, "review_deck", side_effect=[{"01": "\u8a9e\u610f\u91cd\u8907\uff1a same task"}, {}]), \
                 patch.object(curriculum, "request_json", return_value={"jobs": [replacement]}), \
                 patch.object(curriculum, "validate_replacement_outcomes") as replacement_audit:
                state = curriculum.generate_deck(self.plan, checkpoint, [])
            replacement_audit.assert_called_once()
            self.assertEqual(state["plan"]["jobs"][0], replacement)
            self.assertEqual(state["fingerprint"], curriculum.fingerprint_for(self.plan, []))
            curriculum.validate_deck(state["items"], self.plan, reviewed=True)
            with patch.object(curriculum, "generate_batch") as generate, patch.object(curriculum, "review_deck", return_value={}):
                curriculum.generate_deck(self.plan, checkpoint, [], resume=True)
            generate.assert_not_called()

    def test_replacement_requires_a_new_outcome_not_renamed_synonyms(self):
        identifiers = {"01"}
        with patch.object(curriculum, "request_json", return_value={"checks": [
                {"id": "01", "valid": False, "reason": "Same old request under a new label"}]}), \
                self.assertRaisesRegex(ValueError, "Same old request"):
            curriculum.validate_replacement_outcomes(self.plan, self.plan["jobs"], identifiers)

    def test_repair_prompt_separates_withdrawn_outcomes_from_empty_slots(self):
        replacement = dict(self.plan["jobs"][0], task="Report a damaged bag", core="Report damage")
        payload = {"jobs": [{key: replacement[key] for key in ("id", "core", "task", "role", "speaker")}]}
        with patch.object(curriculum, "request_json", return_value=payload) as api, \
                patch.object(curriculum, "validate_replacement_outcomes"):
            curriculum.repair_duplicate_jobs(self.plan, {"01"}, {"01": "Duplicate"}, [])
        prompt = api.call_args.args[0]
        context = json.loads(prompt[prompt.index('{"retained_tasks"'):])
        self.assertNotIn("01", {job["id"] for job in context["retained_tasks"]})
        self.assertEqual(context["withdrawn_outcomes_DO_NOT_REUSE"], [self.plan["jobs"][0]["task"]])
        self.assertNotIn("task", context["empty_slots_to_fill"][0])

    def test_replacement_confirmation_must_cover_every_id(self):
        for checks in ([], [{"id": "01", "valid": True}, {"id": "01", "valid": True}],
                       [{"id": "99", "valid": True}]):
            with self.subTest(checks=checks), patch.object(curriculum, "request_json", return_value={"checks": checks}), \
                    self.assertRaises(ValueError):
                curriculum.validate_replacement_outcomes(self.plan, self.plan["jobs"], {"01"})

    def test_resume_rewrites_invalid_cards_and_synchronizes_repaired_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "checkpoint.json"
            invalid = copy.deepcopy(self.items)
            invalid[0]["Tone"] = "invalid"
            curriculum.save_json(checkpoint, {"version": curriculum.VERSION,
                "fingerprint": curriculum.fingerprint_for(self.plan, []), "plan": {"stale": True},
                "items": invalid, "review_passed": False})
            with patch.object(curriculum, "generate_batch", return_value=self.items[:1]) as generate, \
                 patch.object(curriculum, "review_deck", return_value={}):
                result = curriculum.generate_deck(self.plan, checkpoint, [], resume=True)
            self.assertEqual(result["plan"], self.plan)
            self.assertEqual(len(generate.call_args.args[1]), 1)
            curriculum.validate_deck(result["items"], self.plan, reviewed=True)

    def test_malformed_review_is_retried_without_exporting_unreviewed_content(self):
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "checkpoint.json"
            with patch.object(curriculum, "generate_batch", side_effect=[self.items[:8], self.items[8:]]), \
                 patch.object(curriculum, "review_deck", side_effect=ValueError("missing assignments")) as audit:
                with self.assertRaises(RuntimeError):
                    curriculum.generate_deck(self.plan, checkpoint, [])
            self.assertEqual(audit.call_count, 3)
            self.assertFalse(json.loads(checkpoint.read_text())["review_passed"])

    def test_default_cli_runs_curriculum_pipeline_and_reuses_verified_workbook(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "new-topic.xlsx"
            with patch.object(curriculum, "plan_curriculum", return_value=self.plan), \
                 patch.object(curriculum, "generate_batch", side_effect=[self.items[:8], self.items[8:]]), \
                 patch.object(curriculum, "review_deck", return_value={}), \
                 patch.object(cards, "REVIEW_MODE", "hybrid"), \
                 patch.object(cards, "write_youtube_description") as youtube:
                cards.main(["--topic", self.plan["topic"], "--count", "10", "--output", str(output)])
            youtube.assert_called_once()
            self.assertEqual(len(cards.load_xlsx_items(str(output))), 10)
            with patch.object(curriculum, "plan_curriculum") as plan, patch.object(cards, "REVIEW_MODE", "hybrid"):
                cards.main(["--topic", self.plan["topic"], "--count", "10", "--output", str(output)])
            plan.assert_not_called()

    def test_cached_workbook_can_generate_missing_caption_without_regeneration(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "new-topic.xlsx"
            with patch.object(curriculum, "plan_curriculum", return_value=self.plan), \
                 patch.object(curriculum, "generate_deck", return_value={"items": self.items}), \
                 patch.object(cards, "REVIEW_MODE", "hybrid"):
                cards.main(["--topic", self.plan["topic"], "--count", "10", "--output", str(output), "--no-youtube"])
            curriculum.save_json(Path(str(output) + ".curriculum.json"), {
                "version": curriculum.VERSION, "review_passed": True,
                "review_version": curriculum.SEMANTIC_REVIEW_VERSION,
                "fingerprint": curriculum.fingerprint_for(self.plan, []), "plan": self.plan, "items": self.items})
            original = output.read_bytes()
            with patch.object(curriculum, "generate_deck") as generate, \
                 patch.object(cards, "REVIEW_MODE", "hybrid"), \
                 patch.object(cards, "write_youtube_description") as youtube:
                cards.main(["--topic", self.plan["topic"], "--count", "10", "--output", str(output)])
            generate.assert_not_called()
            youtube.assert_called_once()
            self.assertEqual(output.read_bytes(), original)

    def test_existing_unreviewed_workbook_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "existing.xlsx"
            output.write_bytes(b"preserve existing workbook")
            with patch.object(cards, "REVIEW_MODE", "hybrid"), self.assertRaises(SystemExit):
                cards.main(["--topic", "Restaurant ordering", "--count", "10", "--output", str(output)])
            self.assertEqual(output.read_bytes(), b"preserve existing workbook")

    def test_plan_only_and_no_youtube_do_not_run_extra_stages(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "new-topic.xlsx"
            with patch.object(curriculum, "plan_curriculum", return_value=self.plan), \
                 patch.object(curriculum, "generate_deck") as generate, \
                 patch.object(cards, "write_youtube_description") as youtube, patch.object(cards, "REVIEW_MODE", "hybrid"):
                cards.main(["--topic", self.plan["topic"], "--count", "10", "--output", str(output), "--plan-only"])
            generate.assert_not_called()
            youtube.assert_not_called()
            self.assertFalse(output.exists())
            self.assertTrue(output.with_suffix(".plan.json").exists())


class TaskFidelityTests(unittest.TestCase):
    def test_task_fidelity_requires_complete_explicit_checks(self):
        plan, items = fixture()
        for checks in ([], [{"id": "01", "valid": True, "reason": ""}] * 10,
                       [{"id": item["id"], "valid": "true", "reason": ""} for item in items]):
            with self.subTest(checks=checks), patch.object(curriculum, "request_json", return_value={"checks": checks}), \
                    self.assertRaises(ValueError):
                curriculum.review_task_fidelity(plan, items)

    def test_task_fidelity_checks_only_meaning_fields_and_preserves_rejections(self):
        plan, items = fixture()
        checks = [{"id": item["id"], "valid": item["id"] != "01",
                   "reason": "Permission is not obligation" if item["id"] == "01" else ""} for item in items]
        with patch.object(curriculum, "request_json", return_value={"checks": checks}) as api:
            self.assertEqual(curriculum.review_task_fidelity(plan, items), {"01": "Permission is not obligation"})
        prompt = api.call_args.args[0]
        context = json.loads(prompt[prompt.index('{"brief"'):])
        self.assertNotIn("word_ipa", context["cards"][0])
        self.assertEqual(context["cards"][0]["task"], plan["jobs"][0]["task"])


if __name__ == "__main__":
    unittest.main()
