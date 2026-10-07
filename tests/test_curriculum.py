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
from artifact_paths import cached_artifact


SOURCE = Path(cards.BASE_DIR) / "output" / "\u63a8\u92b7\u8207\u96b1\u5f62\u6572\u8a50.xlsx.editor.json"


def fixture(topic="Airport check-in", count=10):
    original = json.loads(SOURCE.read_text(encoding="utf-8"))["items"]
    pools = {"basic": [item for item in original if item["tier"] == "basic"],
             "advanced": [item for item in original if item["tier"] == "advanced"][6:]}
    chunks = {
        "What does that mean?": ("in simple English", "用簡單英文", "/ɪn ˈsɪmpəl ˈɪŋɡlɪʃ/"),
        "What does this part do?": ("during normal use", "正常使用期間", "/ˈdʊrɪŋ ˈnɔrməl jus/"),
        "What's wrong with this part?": ("wrong with this part", "這個零件的問題", "/rɔŋ wɪð ðɪs pɑrt/"),
        "What will you do, step by step?": ("step by step", "一步一步", "/stɛp baɪ stɛp/"),
        "I need a written quote.": ("a written quote", "書面報價", "/ə ˈrɪtən koʊt/"),
        "Why do I need this repair?": ("need this repair", "需要這項維修", "/nid ðɪs rɪˈpɛr/"),
        "Could you clarify what this charge covers?": ("clarify what this charge covers", "釐清這筆費用的範圍", "/ˈklærəfaɪ wʌt ðɪs tʃɑrdʒ ˈkʌvərz/"),
        "Could you confirm the final amount?": ("confirm the final amount", "確認最後總額", "/kənˈfɜrm ðə ˈfaɪnəl əˈmaʊnt/"),
        "I won't approve repairs without written evidence.": ("without written evidence", "沒有書面證據", "/wɪˈðaʊt ˈrɪtən ˈɛvɪdəns/"),
        "I'd like an independent inspection report.": ("an independent inspection report", "獨立檢查報告", "/ən ˌɪndɪˈpɛndənt ɪnˈspɛkʃən rɪˈpɔrt/"),
    }
    scenarios = [topic + " arrival", topic + " request", topic + " decision"]
    slots = curriculum.slots_for(count, scenarios)
    jobs, items = [], []
    for slot in slots:
        item = copy.deepcopy(pools[slot["tier"]].pop(0))
        chunk, translation, ipa = chunks[item["word_en"]]
        item.update(word_en=chunk, word_cn=translation, word_ipa=ipa,
                    tips="語氣中立，直接釐清資訊而不帶責備。")
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
        confirmation = patch.object(curriculum, "confirm_equivalent_pairs", side_effect=lambda targets, checks, **kwargs:
                                    {key: dict(check, _confirmed=True) for key, check in checks.items()})
        confirmation.start()
        self.addCleanup(confirmation.stop)
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

    def test_concise_tips_do_not_require_a_duplicate_tone_label(self):
        item = dict(self.items[0], tips="語氣平靜，保留隱私又不顯得生硬。")
        curriculum.validate_item(item, self.plan["jobs"][0])
        item["tips"] += "不要補充額外細節。" * 3
        with self.assertRaisesRegex(curriculum.FieldValidationError, "精簡"):
            curriculum.validate_item(item, self.plan["jobs"][0])

    def test_existing_reviewed_long_tips_are_not_accepted_as_new_content(self):
        item = dict(self.items[0], tips="中立：當同事把私人問題混進工作聊天時，說完這句就停下，不補充私事細節。")
        with self.assertRaises(ValueError):
            curriculum.validate_item(item, self.plan["jobs"][0])

    def test_generated_tips_strip_tone_and_schema_limits_length(self):
        job, source = self.plan["jobs"][0], self.items[0]
        source = dict(source, tips="中立：語氣平靜，保留隱私又不顯得生硬。")
        with patch.object(curriculum, "request_json", return_value={"items": [source]}):
            result = curriculum.generate_batch(self.plan, [job], [])
        self.assertEqual(result[0]["tips"], "語氣平靜，保留隱私又不顯得生硬。")
        from types import SimpleNamespace
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"items": {}}'))])
        with patch.object(cards, "_call_openai", return_value=response) as api:
            curriculum.request_json("Generate", "教材生成 01-01", "gpt-4o-mini", job_ids=[job["id"]])
        fields = api.call_args.kwargs["response_format"]["json_schema"]["schema"]["properties"]["items"]["properties"][job["id"]]["properties"]
        self.assertEqual(fields["tips"]["maxLength"], 36)
        self.assertNotIn("pattern", fields["tips"])

    def test_planning_separates_lesson_activities_from_spoken_tasks(self):
        plan = copy.deepcopy(self.plan)
        plan["jobs"][0]["task"] = "練習在被問私事時保持冷靜的語調"
        plan["jobs"][1]["task"] = "當對方重複追問時，要求對方停止詢問"
        self.assertEqual(set(curriculum.unusable_task_rejections(plan)), {"01"})

    def test_unusable_resumed_task_is_replaced_without_losing_other_cards(self):
        plan = copy.deepcopy(self.plan)
        plan["jobs"][0]["task"] = "整理三句固定回應，適用不同提問情境"
        replacement = dict(plan["jobs"][0], core="new-purpose", task="Report a damaged bag")
        updated = [replacement] + plan["jobs"][1:]
        repaired = dict(self.items[0], core="new-purpose", _semantic_group="new-purpose")
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "checkpoint.json"
            curriculum.save_json(checkpoint, {"version": curriculum.VERSION,
                "fingerprint": curriculum.fingerprint_for(plan, []), "plan": plan,
                "items": self.items, "review_passed": False})
            with patch.object(curriculum, "repair_duplicate_jobs", return_value=updated), \
                    patch.object(curriculum, "backup_generation"), \
                    patch.object(curriculum, "generate_batch", return_value=[repaired]) as generate, \
                    patch.object(curriculum, "review_deck", return_value={}):
                state = curriculum.generate_deck(plan, checkpoint, [], resume=True)
            self.assertEqual([job["id"] for job in generate.call_args.args[1]], ["01"])
            self.assertEqual(len(state["items"]), len(self.items))
            self.assertEqual(state["fingerprint"], curriculum.fingerprint_for(plan, []))

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

    def test_only_video_fields_are_validated(self):
        unused = dict(self.items[0], Tone="invalid", Core_Vocab="wrong", sentence_ipa="bad", vocab=[])
        curriculum.validate_item(unused, self.plan["jobs"][0])
        for change in (lambda i: i.update(tips="Use it whenever you like."),):
            items = copy.deepcopy(self.items)
            change(items[0])
            with self.assertRaises(ValueError):
                curriculum.validate_deck(items, self.plan)

    def test_attested_metalinguistic_and_recheck_errors_are_rejected(self):
        for line in ("I need to say there is no hot water.",
                     "I want to show you my carry-on bag now.",
                     "Am I required to recheck in again?"):
            item = dict(self.items[0], sentence_en=line)
            with self.subTest(line=line), self.assertRaisesRegex(ValueError, "直接說出|check in again|exact contiguous"):
                curriculum.validate_item(item, self.plan["jobs"][0])

    def test_staff_reply_cannot_be_a_learner_request_example(self):
        item = dict(self.items[0], word_en="Could you walk me through filing a complaint?",
                    sentence_en="Yes, I can walk you through filing a complaint.")
        with self.assertRaisesRegex(ValueError, "原說話者|complete sentence|exact contiguous"):
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
        self.assertIn("enum", keyed["properties"]["01"]["properties"]["word_ipa"])
        self.assertNotIn("pattern", keyed["properties"]["01"]["properties"]["tips"])
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

    def test_unavailable_repair_model_falls_back_once_and_preserves_schema(self):
        import httpx
        from openai import PermissionDeniedError
        from types import SimpleNamespace
        denied = PermissionDeniedError("Model unavailable", response=httpx.Response(403,
            request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions")),
            body={"code": "model_not_found"})
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"items":{}}'))])
        token = curriculum._unavailable_models.set(set())
        self.addCleanup(curriculum._unavailable_models.reset, token)
        with patch.object(cards, "_call_openai", side_effect=[denied, response, response]) as api:
            curriculum.request_json("Fix", "教材生成 01-01", "unavailable", job_ids=["01"])
            curriculum.request_json("Fix again", "教材生成 01-01", "unavailable", job_ids=["01"])
        self.assertEqual([call.kwargs["model"] for call in api.call_args_list],
                         ["unavailable", curriculum.AUTHOR_MODEL, curriculum.AUTHOR_MODEL])
        self.assertEqual(api.call_args_list[0].kwargs["response_format"],
                         api.call_args_list[1].kwargs["response_format"])

    def test_non_model_permission_error_does_not_trigger_fallback(self):
        import httpx
        from openai import PermissionDeniedError
        denied = PermissionDeniedError("Permission denied", response=httpx.Response(403,
            request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions")),
            body={"code": "insufficient_permissions"})
        with patch.object(cards, "_call_openai", side_effect=denied) as api, self.assertRaises(PermissionDeniedError):
            curriculum.request_json("Fix", "教材生成 01-01", "unavailable", job_ids=["01"])
        self.assertEqual(api.call_count, 1)

    def test_attested_souvenir_ipa_error_is_not_exported(self):
        english = "I have souvenirs to declare."
        correct = "/aɪ hæv ˌsuːvəˈnɪrz tu dɪˈklɛr/"
        item = dict(self.items[0], word_en=english, sentence_en=english, word_ipa=correct, sentence_ipa=correct, tips="現場提示")
        point = {"target_phrase": english, "target_sentence": english, "task": english, "category": "customs"}
        self.assertIsNone(cards._pronunciation_and_translation_issue(item))
        item["word_ipa"] = "/aɪ hæv ˌsuːˈvɪnɚz tu dɪˈklɛr/"
        self.assertIn("souvenir", cards._pronunciation_and_translation_issue(item))

    def test_attested_airport_ipa_errors_are_rejected_and_corrected(self):
        cases = (("Could my seat be upgraded?", "/kʊd maɪ sit bi ˈʌɡreɪdɪd/", "ʌpˈɡreɪdɪd"),
                 ("Where is my connecting flight?", "/wɛr ɪz maɪ kənˈnɛktɪŋ flaɪt/", "kəˈnɛktɪŋ"))
        for line, wrong, correct in cases:
            item = dict(self.items[0], word_en=line, sentence_en=line, word_ipa=wrong, sentence_ipa=wrong, tips="現場提示")
            point = {"target_phrase": line, "target_sentence": line, "task": line, "category": "airport"}
            with self.subTest(line=line):
                self.assertIsNotNone(cards._pronunciation_and_translation_issue(item))
                fixed = cards._correct_known_locked_pronunciations(item, point)
                self.assertIn(correct, fixed["word_ipa"])
                self.assertIsNone(cards._pronunciation_and_translation_issue(fixed))
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
        curriculum.validate_progression("Uses prompts as a formal but non-basic verb choice")

    def test_basic_examples_cannot_acquire_known_advanced_grammar_during_generation(self):
        job = self.plan["jobs"][0]
        item = copy.deepcopy(self.items[0])
        item.update(word_en="show my ID", word_ipa="/ʃoʊ maɪ aɪ di/",
                    sentence_en="Am I required to show my ID?", sentence_ipa="/æm aɪ rɪˈkwaɪərd tu ʃoʊ maɪ aɪ di/",
                    vocab=[{"en": "show", "cn": "出示"}], Core_Vocab="show 出示")
        with self.assertRaisesRegex(ValueError, "sentence_en.*基礎難度"):
            curriculum.validate_item(item, job)

    def test_real_lexical_and_grammar_evidence_is_not_rejected_for_formal_tone(self):
        for reason in (
            "Contains the nontrivial phrase first-come, first-served basis and a formal construction.",
            "Uses imperative and the formal construction It is imperative that...",
            "Uses coordinate and potential conflicts; lexical complexity beyond politeness.",
        ):
            with self.subTest(reason=reason):
                curriculum.validate_progression(reason)

    def test_truncated_review_splits_ids_and_merges_complete_checks(self):
        def respond(prompt, stage, model, **options):
            ids = options["job_ids"]
            if len(ids) > 1:
                raise curriculum.TruncatedResponseError("length")
            self.assertIn("Output ONLY these IDs", prompt)
            return {"checks": [{"id": ids[0], "valid": True}]}
        with patch.object(curriculum, "_request_json", side_effect=respond):
            result = curriculum.request_json("Check", "教材退回複核", "gpt-5-nano", job_ids=["01", "02"])
        self.assertEqual([row["id"] for row in result["checks"]], ["01", "02"])

    def test_truncated_single_request_increases_completion_limit_once(self):
        with patch.object(curriculum, "_request_json", side_effect=[
                curriculum.TruncatedResponseError("length"), {"checks": []}]) as api:
            curriculum.request_json("Check", "教材難度複核", "gpt-5-nano", job_ids=["01"])
        self.assertEqual(api.call_count, 2)
        self.assertEqual(api.call_args.kwargs["completion_floor"], 20000)

    def test_truncated_single_card_switches_to_repair_model(self):
        with patch.object(curriculum, "_request_json", side_effect=[
                curriculum.TruncatedResponseError("length"), {"items": []}]) as api:
            curriculum.request_json("Generate", "教材生成 01-01", curriculum.AUTHOR_MODEL,
                                    job_ids=["01"], anchors={"01": {"word_en": "Stay quiet."}})
        self.assertEqual([call.args[2] for call in api.call_args_list],
                         [curriculum.AUTHOR_MODEL, curriculum.REPAIR_MODEL])
        self.assertEqual(api.call_args.kwargs["anchors"], {"01": {"word_en": "Stay quiet."}})

    def test_content_truncation_uses_repair_for_later_batches(self):
        jobs = [job for job in self.plan["jobs"] if job["tier"] == "basic"][:2]
        items = [next(item for item in self.items if item["id"] == job["id"]) for job in jobs]
        token = curriculum._truncated_content_models.set(set())
        self.addCleanup(curriculum._truncated_content_models.reset, token)
        def respond(prompt, stage, model, **options):
            if model == curriculum.AUTHOR_MODEL:
                raise curriculum.TruncatedResponseError("length")
            return {"items": [next(item for item in items if item["id"] == options["job_ids"][0])]}
        with patch.object(curriculum, "_request_json", side_effect=respond) as api:
            first = curriculum.generate_batch(self.plan, jobs[:1], [])
            curriculum.generate_batch(self.plan, jobs[1:], first)
        self.assertEqual([call.args[2] for call in api.call_args_list],
                         [curriculum.AUTHOR_MODEL, curriculum.REPAIR_MODEL, curriculum.REPAIR_MODEL])

    def test_content_model_fallback_is_reset_between_runs(self):
        previous = {"previous-run"}
        token = curriculum._truncated_content_models.set(previous)
        self.addCleanup(curriculum._truncated_content_models.reset, token)
        def run(*args):
            self.assertEqual(curriculum._truncated_content_models.get(), set())
            curriculum._truncated_content_models.get().add(curriculum.AUTHOR_MODEL)
        with patch.object(curriculum, "_run", side_effect=run):
            curriculum.run(None, None, True)
            curriculum.run(None, None, True)
        self.assertIs(curriculum._truncated_content_models.get(), previous)

    def test_truncated_repair_card_has_only_one_larger_budget_retry(self):
        with patch.object(curriculum, "_request_json", side_effect=[
                curriculum.TruncatedResponseError("length")] * 2) as api, \
                self.assertRaises(curriculum.TruncatedResponseError):
            curriculum.request_json("Generate", "教材生成 01-01", curriculum.REPAIR_MODEL,
                                    job_ids=["01"])
        self.assertEqual(api.call_count, 2)
        self.assertGreater(api.call_args.kwargs["completion_floor"], 10000)

    def test_difficulty_cache_keys_actual_text_not_arbitrary_field_ids(self):
        token = curriculum._difficulty_checks.set({})
        self.addCleanup(curriculum._difficulty_checks.reset, token)
        check = {"id": "F001", "level": "basic", "progression": "Simple daily words"}
        with patch.object(curriculum, "request_json", return_value={"checks": [check]}) as api:
            curriculum.confirm_difficulty([{"id": "F001", "line_en": "Can you help?"}])
            result = curriculum.confirm_difficulty([{"id": "F007", "line_en": "Can you help?"}])
        self.assertEqual(api.call_count, 1)
        self.assertEqual(result[0]["id"], "F007")

    def test_difficulty_uses_exact_feature_evidence_not_reason_keywords(self):
        target = {"id": "F001", "line_en": "Please inform me before inviting overnight guests."}
        check = {"id": "F001", "level": "advanced", "feature_en": "inform",
                 "progression": "Uses a formal directive with inform, a higher-register verb."}
        with patch.object(curriculum, "request_json", return_value={"checks": [check]}) as api:
            self.assertEqual(curriculum.confirm_difficulty([target])[0], check)
        self.assertEqual(api.call_count, 1)

    def test_difficulty_rejects_invented_or_politeness_only_evidence(self):
        target = {"id": "F001", "line_en": "Could you help me, please?"}
        for feature in ("", "could", "please", "refrain from"):
            check = {"id": "F001", "level": "advanced", "feature_en": feature,
                     "progression": "Uses sophisticated grammar."}
            with self.subTest(feature=feature), patch.object(curriculum, "request_json",
                    return_value={"checks": [check]}):
                self.assertFalse(curriculum.confirm_difficulty([target])[0]["_valid"])

    def test_author_can_cite_non_whitelisted_actual_lexical_feature(self):
        curriculum.validate_progression("Uses 'implement' as a formal verb.", "We should implement a cleaning rota.")
        with self.assertRaises(ValueError):
            curriculum.validate_progression("Uses 'confirm' as a formal verb.", "Please confirm the schedule.")

    def test_generation_cannot_resubmit_a_known_wrong_difficulty_line(self):
        item = copy.deepcopy(self.items[0])
        key = cards._spoken_line_key(item["word_en"])
        token = curriculum._difficulty_checks.set({key: {"level": "advanced", "progression": "Known richer wording"}})
        self.addCleanup(curriculum._difficulty_checks.reset, token)
        with self.assertRaisesRegex(ValueError, "不能重交"):
            curriculum.validate_item(item, self.plan["jobs"][0])

    def test_language_audit_rechecks_changed_card_but_keeps_global_context(self):
        token = curriculum._language_audits.set({})
        self.addCleanup(curriculum._language_audits.reset, token)
        def audit(plan, items, references, context=None):
            if context:
                self.assertEqual(len(context), len(self.items) - 1)
            return {"assignments": [{"id": item["id"], "purpose": item["core"], "level": item["tier"],
                                      "progression": "Actual feature"} for item in items], "reject": [],
                    "groups": [{"ids": [self.items[0]["id"], self.items[1]["id"]], "purpose": "same requested action"}]}
        modified = copy.deepcopy(self.items)
        modified[0]["word_cn"] += "請注意。"
        with patch.object(curriculum, "_audit_curriculum", side_effect=audit) as api:
            curriculum.curriculum_audit(self.plan, self.items, [])
            result = curriculum.curriculum_audit(self.plan, modified, [])
            curriculum.curriculum_audit(self.plan, modified, [])
        self.assertEqual(api.call_count, 2)
        self.assertEqual([item["id"] for item in api.call_args.args[1]], [modified[0]["id"]])
        self.assertEqual(len(result["assignments"]), len(self.items))
        self.assertEqual(len(result["groups"]), 1)

    def test_review_upgrade_invalidates_old_audits_and_semantic_groups(self):
        with patch.object(curriculum, "SEMANTIC_REVIEW_VERSION", 12):
            old_key = curriculum.audit_key(self.plan, self.items[0], [])
        cache = {old_key: {"assignment": {"id": "01", "purpose": "old result", "level": "basic"}, "reject": None},
                 "_groups": [{"ids": ["01", "02"], "purpose": "old equivalence"}], "_groups_review_version": 12}
        token = curriculum._language_audits.set(cache)
        self.addCleanup(curriculum._language_audits.reset, token)
        fresh = {"assignments": [{"id": item["id"], "purpose": item["core"], "level": item["tier"],
                                 "progression": item["progression"]} for item in self.items], "reject": [], "groups": []}
        with patch.object(curriculum, "_audit_curriculum", return_value=fresh) as audit:
            result = curriculum.curriculum_audit(self.plan, self.items, [])
            curriculum.curriculum_audit(self.plan, self.items, [])
        audit.assert_called_once()
        self.assertEqual(len(audit.call_args.args[1]), len(self.items))
        self.assertEqual(result["groups"], [])
        self.assertEqual(cache["_groups_review_version"], curriculum.SEMANTIC_REVIEW_VERSION)

    def test_cached_audit_invalidates_changed_task_and_references(self):
        original = curriculum.audit_key(self.plan, self.items[0], [])
        plan = copy.deepcopy(self.plan)
        plan["jobs"][0]["task"] += " by Friday"
        self.assertNotEqual(original, curriculum.audit_key(plan, self.items[0], []))
        self.assertNotEqual(original, curriculum.audit_key(self.plan, self.items[0], [{"word_en": "Old phrase"}]))

    def test_incremental_audit_schema_can_group_changed_and_unchanged_ids(self):
        from types import SimpleNamespace
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content='{"assignments":[],"reject":[],"groups":[]}'))])
        with patch.object(cards, "_call_openai", return_value=response) as api:
            curriculum.request_json("Audit", "教材綜合審查", "gpt-5-nano", job_ids=["01"],
                                    anchors={"01": {}, "02": {}})
        schema = api.call_args.kwargs["response_format"]["json_schema"]["schema"]["properties"]
        self.assertEqual(schema["assignments"]["items"]["properties"]["id"]["enum"], ["01"])
        self.assertEqual(schema["groups"]["items"]["properties"]["ids"]["items"]["enum"], ["01", "02"])

    def test_truncated_generation_saves_left_half_before_right_timeout(self):
        jobs = self.plan["jobs"][:2]
        item = self.items[0]
        updates = []
        with patch.object(curriculum, "request_json", side_effect=[
                curriculum.TruncatedResponseError("length"), {"items": [item]},
                cards.GenerationTimeoutError("timeout")]) as api, self.assertRaises(cards.GenerationTimeoutError):
            curriculum.generate_batch(self.plan, jobs, [],
                on_progress=lambda items, repairs, anchors: updates.append(copy.deepcopy(items)))
        self.assertIn([item["id"]], [[row["id"] for row in batch] for batch in updates])
        self.assertEqual([call.args[2] for call in api.call_args_list],
                         [curriculum.AUTHOR_MODEL, curriculum.REPAIR_MODEL, curriculum.REPAIR_MODEL])

    def test_truncated_author_rescue_can_use_repair_for_content(self):
        jobs = self.plan["jobs"][:2]
        with patch.object(curriculum, "request_json", side_effect=[
                curriculum.TruncatedResponseError("length"),
                {"items": self.items[:1]}, {"items": self.items[1:2]}]) as api:
            result = curriculum.generate_batch(self.plan, jobs, [], author_first=True)
        self.assertEqual([item["id"] for item in result], [job["id"] for job in jobs])
        self.assertEqual([call.args[2] for call in api.call_args_list],
                         [curriculum.AUTHOR_MODEL, curriculum.REPAIR_MODEL, curriculum.REPAIR_MODEL])

    @patch.object(curriculum, "review_field_difficulty", return_value={})
    def test_advanced_retries_keep_all_failed_lines_and_escalate_model(self, _review):
        job = next(job for job in self.plan["jobs"] if job["tier"] == "advanced")
        item = copy.deepcopy(next(item for item in self.items if item["id"] == job["id"]))
        line = {key: item[key] for key in ("id", "word_en", "sentence_en", "progression")}
        first = dict(line, word_en="One " * 13)
        second = dict(line, word_en="Two " * 13)
        with patch.object(curriculum, "request_json", side_effect=[
                {"lines": [first]}, {"lines": [second]}, {"lines": [line]}, {"items": [item]}]) as api:
            curriculum.generate_batch(self.plan, [job], [])
        prompt = api.call_args_list[2].args[0]
        self.assertIn(first["word_en"], prompt)
        self.assertIn(second["word_en"], prompt)
        self.assertEqual(api.call_args_list[2].args[2], curriculum.AUTHOR_MODEL)

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

    def test_roommate_lexical_features_do_not_flip_with_reviewer_wording(self):
        lines = (
            "It's imperative that we respect shared spaces.",
            "Please adhere to our agreed standards.",
            "Let's implement a first-come, first-served laundry schedule.",
            "Please coordinate kitchen usage to prevent conflicts.",
            "We should assess our progress weekly.",
            "We may need to reconsider our living arrangements.",
        )
        with patch.object(curriculum, "confirm_difficulty", side_effect=AssertionError("Known features are deterministic")):
            for index, line in enumerate(lines):
                with self.subTest(line=line):
                    item = {"id": str(index), "tier": "advanced", "word_en": line, "sentence_en": line}
                    self.assertEqual(curriculum.review_field_difficulty([item]), {})
                    item["tier"] = "basic"
                    self.assertIn(item["id"], curriculum.review_field_difficulty([item]))

    def test_boundary_features_accept_inflections_and_hyphens(self):
        for line in ("I draw the line here.", "I've drawn a line in the sand.",
                     "I'm drawing a firm line.", "We'll draw a hard line.", "I drew a line there."):
            with self.subTest(line=line):
                self.assertIn("draw the line", curriculum.explicit_advanced_features(line))
        self.assertIn("non-negotiable", curriculum.explicit_advanced_features("This boundary is non-negotiable."))

    def test_subject_to_does_not_match_changing_the_subject(self):
        for line in ("Can we change the subject to something lighter?", "Change our subject to rent."):
            self.assertNotIn("subject to", curriculum.explicit_advanced_features(line))
        for line in ("Fees are subject to change.", "Entry is subject to approval.", "Fees subject to change."):
            self.assertIn("subject to", curriculum.explicit_advanced_features(line))

    def test_spelled_initialisms_and_room_numbers_allow_complete_ipa(self):
        cases = (("Do I need to show my ID?", "/du aɪ nid tu ʃoʊ maɪ aɪ di/"),
                 ("I need room 204.", "/aɪ nid rum tu oʊ fɔr/"),
                 ("Stop by 5pm.", "/stɑp baɪ faɪv pi ɛm/"),
                 ("Stop by 5 pm.", "/stɑp baɪ faɪv pi ɛm/"),
                 ("Stop by 11AM.", "/stɑp baɪ ɪˈlɛvən eɪ ɛm/"),
                 ("Stop by 5:30pm.", "/stɑp baɪ faɪv ˈθərti pi ɛm/"))
        for line, ipa in cases:
            item = dict(self.items[0], word_en=line, sentence_en=line, word_ipa=ipa, sentence_ipa=ipa, tips="現場提示")
            point = {"target_phrase": line, "target_sentence": line, "task": line, "category": "hotel"}
            with self.subTest(line=line):
                self.assertIsNone(cards._pronunciation_and_translation_issue(item))
                item["word_ipa"] = "/du aɪ/"
                self.assertIsNotNone(cards._pronunciation_and_translation_issue(item))

    def test_clock_ipa_bounds_do_not_expand_ordinary_am(self):
        self.assertEqual(cards._ipa_token_bounds("I am ready."), (3, 3))
        lower, upper = cards._ipa_token_bounds("Stop by 5pm.")
        self.assertLessEqual(lower, 5)
        self.assertGreaterEqual(upper, 5)
        self.assertLess(upper, 10)

    def test_clock_ipa_field_repair_uses_repair_model_even_in_author_rescue(self):
        job = self.plan["jobs"][0]
        item = dict(self.items[0], word_en="stop by 5pm", sentence_en="Stop by 5pm today.",
                    word_ipa="/stɑp/", word_cn="五點前停止。", sentence_cn="今天五點前停止。")
        repair = {job["id"]: {"item": item, "fields": ["word_ipa"], "reason": "Incomplete IPA"}}
        with patch.object(curriculum, "request_json", return_value={"items": [
                {"id": job["id"], "word_ipa": "/stɑp baɪ faɪv pi ɛm/"}]}) as api:
            result = curriculum.generate_batch(self.plan, [job], [], repairs=repair, author_first=True)
        self.assertEqual(api.call_args.args[2], curriculum.REPAIR_MODEL)
        self.assertEqual(result[0]["word_en"], item["word_en"])
        self.assertEqual(result[0]["word_ipa"], "/stɑp baɪ faɪv pi ɛm/")

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
        with patch.object(curriculum, "request_json", return_value=dict(semantic, groups=[])) as api:
            self.assertEqual(curriculum.review_deck(self.plan, self.items, []), {})
        self.assertEqual(api.call_count, 1)
        self.assertEqual(api.call_args.args[1], "教材綜合審查")
        incomplete = copy.deepcopy(semantic)
        incomplete["assignments"].pop()
        with patch.object(curriculum, "request_json", return_value=dict(incomplete, groups=[])), self.assertRaises(ValueError):
            curriculum.review_deck(self.plan, self.items, [])

    @patch.object(curriculum, "review_field_difficulty", return_value={})
    def test_style_rejection_requires_independent_objective_confirmation(self, _field_review):
        semantic = {"assignments": [{"id": item["id"], "purpose": item["core"], "level": item["tier"],
                                     "progression": item["progression"]} for item in self.items], "reject": []}
        with patch.object(curriculum, "request_json", side_effect=[
            dict(semantic, groups=[], reject=[{"id": "01", "reason": "I prefer a different phrasing"}]),
            {"checks": [{"id": "01", "valid": True, "reason": ""}]}]):
            self.assertEqual(curriculum.review_deck(self.plan, self.items, []), {})
        with patch.object(curriculum, "request_json", side_effect=[
            dict(semantic, groups=[], reject=[{"id": "01", "reason": "IPA wrong"}]),
            {"checks": [{"id": "01", "valid": False, "reason": "Missing a word in the IPA"}]}]):
            self.assertEqual(curriculum.review_deck(self.plan, self.items, [])["01"], "Missing a word in the IPA")

    @patch.object(curriculum, "review_field_difficulty", return_value={})
    def test_copy_edit_prompts_allow_natural_yes_no_questions(self, _field_review):
        semantic = {"assignments": [{"id": item["id"], "purpose": item["core"], "level": item["tier"],
                                     "progression": item["progression"]} for item in self.items], "reject": []}
        with patch.object(curriculum, "request_json", side_effect=[
            dict(semantic, groups=[], reject=[{"id": "01", "reason": "Must ask What/When/Where"}]),
            {"checks": [{"id": "01", "valid": True, "reason": "Natural yes/no question"}]}]) as api:
            self.assertEqual(curriculum.review_deck(self.plan, self.items, []), {})
        self.assertIn("What/When/Where question is NOT mandatory", api.call_args_list[0].args[0])
        self.assertIn("Do NOT require What/When/Where", api.call_args_list[1].args[0])

    @patch.object(curriculum, "review_field_difficulty", return_value={})
    def test_cross_deck_intent_duplicates_are_checked_in_audit_and_confirmation(self, _field_review):
        reference = {"word_en": "break it down", "sentence_en": "Explain that in plain words, please.",
                     "_source_deck": "Phone Call Phobia"}
        self.assertIsNone(cards._reference_duplicate_reason(self.items[0], [reference]))
        references = [{"word_en": f"reference task {i}", "sentence_en": f"Handle task {i}, please.",
                       "_source_deck": "Earlier deck"} for i in range(cards.MAX_REFERENCE_CARDS_IN_PROMPT)] + [reference]
        semantic = {"assignments": [{"id": item["id"], "purpose": item["core"], "level": item["tier"],
                                     "progression": item["progression"]} for item in self.items], "groups": [],
                    "reject": [{"id": "01", "reason": "語意重複：Phone Call Phobia 已教過要求用簡單話解釋"}]}
        reason = "語意重複：Phone Call Phobia 的 Explain that in plain words, please. 同樣要求簡單解釋"
        with patch.object(curriculum, "request_json", side_effect=[
            semantic, {"checks": [{"id": "01", "valid": False, "reason": reason}]}]) as api:
            rejected = curriculum.review_deck(self.plan, self.items, references)
        self.assertEqual(rejected, {"01": reason})
        self.assertEqual(api.call_count, 2)
        for call in api.call_args_list:
            self.assertIn(reference["sentence_en"], call.args[0])
            self.assertIn("[Phone Call Phobia]", call.args[0])
            self.assertIn("即使字面完全不同", call.args[0])
        self.assertIn("valid=true requires BOTH correct language and a distinct outcome", api.call_args.args[0])

    @patch.object(curriculum, "review_field_difficulty", return_value={})
    def test_lexical_reference_duplicate_cannot_be_cleared_by_ai(self, _field_review):
        semantic = {"assignments": [{"id": item["id"], "purpose": item["core"], "level": item["tier"],
                                     "progression": item["progression"]} for item in self.items], "groups": [], "reject": []}
        reference = dict(self.items[0], _source_deck="Earlier deck")
        with patch.object(curriculum, "request_json", return_value=semantic):
            rejected = curriculum.review_deck(self.plan, self.items, [reference])
        self.assertTrue(rejected["01"].startswith("語意重複："))
        self.assertIn("Earlier deck", rejected["01"])

    @patch.object(curriculum, "review_field_difficulty", return_value={})
    def test_chunk_application_errors_require_tier_three_confirmation(self, _field_review):
        self.items[0].update(word_en="take charge", sentence_en="Nobody stepped up yesterday.")
        semantic = {"assignments": [{"id": item["id"], "purpose": item["core"], "level": item["tier"],
                                     "progression": item["progression"]} for item in self.items], "groups": [],
                    "reject": [{"id": "01", "reason": "例句沒有應用 take charge"}]}
        with patch.object(curriculum, "request_json", side_effect=[
            semantic, {"checks": [{"id": "01", "valid": False, "reason": "例句沒有應用 take charge"}]}]) as api:
            rejected = curriculum.review_deck(self.plan, self.items, [])
        self.assertEqual(rejected["01"], "例句沒有應用 take charge")
        for call in api.call_args_list:
            self.assertIn("take charge → took charge", call.args[0])

    @patch.object(curriculum, "review_field_difficulty", return_value={})
    def test_normal_inflection_does_not_trigger_generation_retry(self, _field_review):
        item = dict(self.items[0], word_en="take my bag", word_ipa="/teɪk maɪ bæɡ/",
                    word_cn="拿我的袋子", sentence_en="Someone took my bag while I was waiting.",
                    sentence_cn="我等候的時候有人拿走了我的袋子。")
        with patch.object(curriculum, "request_json", return_value={"items": [item]}) as api:
            result = curriculum.generate_batch(self.plan, self.plan["jobs"][:1], [])
        self.assertEqual(api.call_count, 1)
        self.assertEqual(result[0]["word_en"], "take my bag")
        self.assertIn("took my bag", result[0]["sentence_en"])

    def test_old_review_checkpoint_resumes_inflected_cards_without_redrafting(self):
        self.items[0].update(word_en="take my bag", word_ipa="/teɪk maɪ bæɡ/",
                             word_cn="拿我的袋子", sentence_en="Someone took my bag while I was waiting.",
                             sentence_cn="我等候的時候有人拿走了我的袋子。")
        references = [{"word_en": "check the gate", "sentence_en": "Can you check the gate for me?"}]
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "checkpoint.json"
            curriculum.save_json(checkpoint, {"version": curriculum.VERSION,
                "fingerprint": curriculum.fingerprint_for(self.plan, references), "plan": self.plan,
                "items": self.items, "review_passed": False, "review_version": 12,
                "pending_review_rejections_version": 12,
                "pending_review_rejections": {"01": "exact contiguous word_en required"}})
            with patch.object(curriculum, "generate_batch") as generate, \
                    patch.object(curriculum, "review_deck", return_value={}) as review:
                state = curriculum.generate_deck(self.plan, checkpoint, references, resume=True)
        generate.assert_not_called()
        review.assert_called_once()
        self.assertEqual(review.call_args.args[2], references)
        self.assertEqual(len(state["items"]), 10)
        self.assertTrue(state["review_passed"])
        self.assertEqual(state["review_version"], curriculum.SEMANTIC_REVIEW_VERSION)

    def test_missing_confirmation_cannot_silently_clear_language_rejection(self):
        with patch.object(curriculum, "request_json", side_effect=[
            {"reject": [{"id": "01", "reason": "IPA wrong"}]}, {"checks": []}]), self.assertRaises(ValueError):
            curriculum.review_deck(self.plan, self.items, [])

    @patch.object(curriculum, "review_field_difficulty", return_value={})
    def test_fidelity_rejection_cannot_be_cleared_by_coarse_reviews(self, _field_review):
        semantic = {"assignments": [{"id": item["id"], "purpose": item["core"], "level": item["tier"],
                                     "progression": item["progression"]} for item in self.items], "reject": []}
        with patch.object(curriculum, "request_json", side_effect=[
                dict(semantic, groups=[], reject=[{"id": "01", "reason": "Permission is not obligation"}]),
                {"checks": [{"id": "01", "valid": False, "reason": "Permission is not obligation"}]}]):
            self.assertEqual(curriculum.review_deck(self.plan, self.items, [])["01"], "Permission is not obligation")

    @patch.object(curriculum, "review_field_difficulty", return_value={"01": "word_en is actually advanced"})
    def test_reviewer_difficulty_disagreement_is_not_silently_accepted(self, _field_review):
        semantic = {"assignments": [{"id": item["id"], "purpose": item["core"], "level": item["tier"],
                                     "progression": item["progression"]} for item in self.items], "reject": []}
        semantic["assignments"][0].update(level="advanced", progression="New idiom")
        with patch.object(curriculum, "request_json", return_value=dict(semantic, groups=[])):
            self.assertIn("01", curriculum.review_deck(self.plan, self.items, []))

    def test_coarse_card_guess_cannot_override_successful_individual_field_review(self):
        semantic = {"assignments": [{"id": item["id"], "purpose": item["core"], "level": "basic",
                                     "progression": "coarse guess"} for item in self.items], "reject": []}
        with patch.object(curriculum, "review_field_difficulty", return_value={}), \
                patch.object(curriculum, "request_json", return_value=dict(semantic, groups=[])):
            self.assertEqual(curriculum.review_deck(self.plan, self.items, []), {})
        self.assertTrue(all(item["_semantic_level"] == item["tier"] for item in self.items))

    def test_basic_advanced_pair_uses_verified_progression_not_blank_coarse_guess(self):
        self.plan["jobs"][2]["core"] = self.plan["jobs"][0]["core"]
        self.items[2]["core"] = self.items[0]["core"]
        semantic = {"assignments": [{"id": item["id"], "purpose": item["core"], "level": "basic",
                                     "progression": ""} for item in self.items], "reject": []}
        with patch.object(curriculum, "review_field_difficulty", return_value={}), \
                patch.object(curriculum, "request_json", side_effect=[dict(semantic, groups=[]),
                    {"checks": {"01:03": {"equivalent": True, "reason": "Same outcome"}}}]):
            self.assertEqual(curriculum.review_deck(self.plan, self.items, []), {})

    @patch.object(curriculum, "review_field_difficulty", return_value={})
    def test_cross_label_audit_catches_equivalent_lines_with_different_labels(self, _field_review):
        semantic = {"assignments": [{"id": item["id"], "purpose": item["core"], "level": item["tier"],
                                     "progression": item["progression"]} for item in self.items], "reject": []}
        with patch.object(curriculum, "request_json", side_effect=[dict(semantic,
            groups=[{"ids": ["01", "02"], "purpose": "Same outcome under different labels"}]),
            {"checks": {"01:02": {"equivalent": True, "reason": "Same concrete result"}}}]):
            rejected = curriculum.review_deck(self.plan, self.items, [])
        self.assertTrue(rejected["02"].startswith("語意重複："))

    @patch.object(curriculum, "review_field_difficulty", return_value={})
    def test_broad_labels_do_not_merge_different_information_outcomes(self, _field_review):
        semantic = {"assignments": [{"id": item["id"], "purpose": item["core"], "level": item["tier"],
                                     "progression": item["progression"]} for item in self.items], "reject": []}
        semantic["assignments"][1]["purpose"] = semantic["assignments"][0]["purpose"]
        with patch.object(curriculum, "request_json", side_effect=[dict(semantic, groups=[]),
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
        with patch.object(curriculum, "request_json", side_effect=[{"checks": [valid, invalid]}]
                + [{"checks": [invalid]}] * (curriculum.STALLED_RETRY_LIMIT + 1)) as request:
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
                patch.object(curriculum, "request_json", return_value=dict(semantic, groups=[])):
            self.assertIn("sentence_en is too basic", curriculum.review_deck(self.plan, self.items, [])["03"])

    def test_curriculum_never_allows_main_lines_over_eight_words(self):
        job = next(job for job in self.plan["jobs"] if job["tier"] == "advanced")
        item = next(item for item in self.items if item["id"] == job["id"])
        line = "Would you mind putting all of these details in writing?"
        ipa = "/wʊd ju maɪnd ˈpʊtɪŋ ɔl əv ðiz dɪˈteɪlz ɪn ˈraɪtɪŋ/"
        item.update(word_en=line, sentence_en=line, word_ipa=ipa, sentence_ipa=ipa,
            vocab=[{"en": "in writing", "cn": "書面"}], Core_Vocab="in writing 書面",
            progression="mind + -ing request")
        with self.assertRaisesRegex(ValueError, "word_en has"):
            curriculum.validate_deck(self.items, self.plan, reviewed=True)
        self.assertTrue(any(issue.startswith("word_en has ") for issue in cards._validation_issues(item)))
        with tempfile.TemporaryDirectory() as directory:
            target = str(Path(directory) / "cards.xlsx")
            with self.assertRaisesRegex(ValueError, "word_en has"):
                cards.write_xlsx(self.items, target, curriculum_plan=self.plan)

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

    def test_export_has_seven_video_columns_and_video_loader_compatibility(self):
        import main
        with tempfile.TemporaryDirectory() as directory:
            target = str(Path(directory) / "cards.xlsx")
            cards.write_xlsx(self.items, target, curriculum_plan=self.plan)
            actual = cards.load_xlsx_items(target)
            self.assertEqual(list(actual[0]), curriculum.VIDEO_HEADERS)
            self.assertEqual(actual, [{key: item[key] for key in curriculum.VIDEO_HEADERS} for item in self.items])
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

    def test_batch_can_succeed_after_three_distinct_failed_proposals(self):
        failures = [{"items": [{"id": str(index)}]} for index in range(3)]
        with patch.object(curriculum, "request_json", side_effect=failures + [{"items": self.items[:1]}]) as request:
            result = curriculum.generate_batch(self.plan, self.plan["jobs"][:1], [])
        self.assertEqual(request.call_count, 4)
        self.assertEqual(result[0]["id"], "01")

    def test_final_review_can_succeed_after_three_repaired_rounds(self):
        corrected = [self.items[0], self.items[0], self.items[0]]
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "checkpoint.json"
            with patch.object(curriculum, "generate_batch", side_effect=[self.items[:8], self.items[8:]]
                    + [[item] for item in corrected]), \
                    patch.object(curriculum, "review_deck", side_effect=[{"01": "Incorrect English"}] * 3 + [{}]) as review:
                result = curriculum.generate_deck(self.plan, checkpoint, [])
        self.assertTrue(result["review_passed"])
        self.assertEqual(review.call_count, 4)

    def test_api_budget_stop_keeps_finished_cards_and_resume_only_fills_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "checkpoint.json"
            with patch.object(curriculum, "generate_batch", side_effect=[self.items[:8], cards.APIBudgetError("test budget")]), \
                    self.assertRaises(cards.APIBudgetError):
                curriculum.generate_deck(self.plan, checkpoint, [])
            saved = json.loads(checkpoint.read_text())
            self.assertEqual(saved["items"], self.items[:8])
            self.assertFalse(saved["review_passed"])
            with patch.object(curriculum, "generate_batch", return_value=self.items[8:]) as generate, \
                    patch.object(curriculum, "review_deck", return_value={}):
                result = curriculum.generate_deck(self.plan, checkpoint, [], resume=True)
            self.assertEqual([job["id"] for job in generate.call_args.args[1]], ["09", "10"])
            self.assertTrue(result["review_passed"])

    def test_batch_retry_only_rewrites_invalid_rows(self):
        invalid = copy.deepcopy(self.items[:2])
        invalid[0]["sentence_en"] = "I need to say there is no hot water."
        with patch.object(curriculum, "request_json", side_effect=[{"items": invalid}, {"items": self.items[:1]}]) as request:
            result = curriculum.generate_batch(self.plan, self.plan["jobs"][:2], [])
        self.assertEqual([item["id"] for item in result], ["01", "02"])
        self.assertEqual(request.call_args.kwargs["job_ids"], ["01"])
        self.assertEqual(request.call_args.args[2], curriculum.REPAIR_MODEL)
        self.assertIn("LATEST correction", request.call_args.args[0])

    def test_metadata_failures_collect_all_fields_without_rewriting_english(self):
        item = copy.deepcopy(self.items[0])
        item.update(word_ipa="/ɒ/", tips="中立：保持禮貌。",
                    vocab=[{"en": "nonexistent", "cn": "錯誤"}], Core_Vocab="nonexistent 錯誤")
        with self.assertRaises(curriculum.FieldValidationError) as failure:
            curriculum.validate_item(item, self.plan["jobs"][0])
        self.assertEqual(failure.exception.fields, ["word_ipa", "tips"])

    def test_batch_drops_unused_metadata_without_repair_api_call(self):
        original = copy.deepcopy(self.items[0])
        original.update(vocab=[{"en": "nonexistent", "cn": "錯誤"}], Core_Vocab="nonexistent 錯誤")
        correction = {"id": "01", "vocab": self.items[0]["vocab"]}
        with patch.object(curriculum, "request_json", side_effect=[{"items": [original]},
                {"items": [correction]}]) as request:
            result = curriculum.generate_batch(self.plan, self.plan["jobs"][:1], [])
        self.assertEqual(request.call_count, 1)
        self.assertNotIn("vocab", result[0])
        self.assertNotIn("sentence_ipa", result[0])
        self.assertNotIn("Tone", result[0])
        self.assertEqual(result[0]["word_en"], self.items[0]["word_en"])
        self.assertEqual(result[0]["sentence_en"], self.items[0]["sentence_en"])
        self.assertEqual(result[0]["word_ipa"], self.items[0]["word_ipa"])
        curriculum.validate_item(result[0], self.plan["jobs"][0])

    def test_field_repair_schema_excludes_every_unchanged_field(self):
        from types import SimpleNamespace
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=json.dumps({"items": {"01": {"tips": self.items[0]["tips"]}}})))])
        repair = {"01": {"item": self.items[0], "fields": ["tips"], "reason": "unclear situation"}}
        with patch.object(cards, "_call_openai", return_value=response) as api:
            curriculum.request_json("Fix vocabulary", "教材生成 01-01", "gpt-4o-mini",
                job_ids=["01"], repairs=repair)
        schema = api.call_args.kwargs["response_format"]["json_schema"]["schema"]
        item_schema = schema["properties"]["items"]["properties"]["01"]
        self.assertEqual(set(item_schema["properties"]), {"tips"})
        self.assertEqual(item_schema["required"], ["tips"])
        self.assertFalse(item_schema["additionalProperties"])

    def test_generation_schema_does_not_require_unused_fields(self):
        from types import SimpleNamespace
        source = dict(self.items[0], word_en="Nice try, but that's private.",
                      sentence_en="Nice try; I'm keeping that private.")
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"items":{}}'))])
        with patch.object(cards, "_call_openai", return_value=response) as api:
            curriculum.request_json("Generate", "教材生成 01-01", "gpt-4o-mini", job_ids=["01"])
        fields = api.call_args.kwargs["response_format"]["json_schema"]["schema"]["properties"]["items"]["properties"]["01"]["properties"]
        self.assertEqual(set(fields), set(curriculum.VIDEO_HEADERS) - {"id"} | {"progression"})

    def test_ipa_repair_schema_locks_dictionary_pronunciation_instead_of_padding_tokens(self):
        from types import SimpleNamespace
        source = dict(self.items[0], word_en="Let's talk about something else—what's new with your family?")
        repair = {"01": {"item": source, "fields": ["word_ipa"], "reason": "two words fused"}}
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"items":{}}'))])
        with patch.object(cards, "_call_openai", return_value=response) as api:
            curriculum.request_json("Fix", "教材生成 01-01", "gpt-4o-mini", job_ids=["01"], repairs=repair)
        fields = api.call_args.kwargs["response_format"]["json_schema"]["schema"]["properties"]["items"]["properties"]["01"]["properties"]
        locked = fields["word_ipa"]["enum"]
        self.assertEqual(len(locked), 1)
        self.assertEqual(len(locked[0].strip("/").split()), 10)
        self.assertNotIn("ɒ", locked[0])
        self.assertNotIn("duɪŋ", locked[0])

    def test_boundary_idioms_and_richer_vocabulary_are_real_advanced_features(self):
        for line in ("Please refrain from prying into my personal life.", "I draw the line at sharing personal details.",
                     "I'd rather keep that to myself.", "That's none of your business.",
                     "I prefer not to disclose confidential details."):
            with self.subTest(line=line):
                self.assertTrue(curriculum.explicit_advanced_features(line))
        self.assertIn("switch gears", curriculum.explicit_advanced_features("Could we switch gears?"))
        self.assertEqual(curriculum.explicit_advanced_features("Why do you ask?"), [])

    def test_advanced_draft_schema_leaves_difficulty_to_independent_validation(self):
        from types import SimpleNamespace
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"lines":[]}'))])
        with patch.object(cards, "_call_openai", return_value=response) as api:
            curriculum.request_json("Draft", "進階英文骨架", "gpt-4o-mini", job_ids=["01"])
        fields = api.call_args.kwargs["response_format"]["json_schema"]["schema"]["properties"]["lines"]["items"]["properties"]
        for field in ("word_en", "sentence_en"):
            self.assertNotIn("pattern", fields[field])
            self.assertGreater(fields[field]["maxLength"], fields[field]["minLength"])
            self.assertIn("intermediate", fields[field]["description"])

    def test_field_repair_cannot_sneak_in_changed_english(self):
        original = copy.deepcopy(self.items[0])
        original.update(tips="保持禮貌。")
        changed = {"id": "01", "tips": self.items[0]["tips"], "word_en": "Do something else."}
        correction = {"id": "01", "tips": self.items[0]["tips"]}
        with patch.object(curriculum, "request_json", side_effect=[{"items": [original]},
                {"items": [changed]}, {"items": [correction]}]):
            result = curriculum.generate_batch(self.plan, self.plan["jobs"][:1], [])
        self.assertEqual(result[0]["word_en"], self.items[0]["word_en"])

    def test_dictionary_ipa_repair_keeps_english_and_other_fields_without_another_api_call(self):
        original = dict(self.items[0], word_ipa=self.items[0]["word_ipa"][:-1] + "ɒ/")
        correction = {"id": "01", "word_ipa": self.items[0]["word_ipa"]}
        with patch.object(curriculum, "request_json", side_effect=[{"items": [original]},
                {"items": [correction]}]) as request:
            result = curriculum.generate_batch(self.plan, self.plan["jobs"][:1], [])
        self.assertEqual(request.call_count, 1)
        self.assertNotIn("ɒ", result[0]["word_ipa"])
        self.assertEqual(result[0]["word_en"], self.items[0]["word_en"])
        self.assertEqual(result[0]["sentence_cn"], self.items[0]["sentence_cn"])

    @patch.object(curriculum, "review_field_difficulty", return_value={})
    def test_advanced_line_is_drafted_separately_and_cannot_be_simplified(self, _field_review):
        job = next(job for job in self.plan["jobs"] if job["tier"] == "advanced")
        item = copy.deepcopy(next(item for item in self.items if item["id"] == job["id"]))
        simplified = dict(item, word_en="Can you help?")
        draft = {"lines": [{"id": job["id"], "word_en": item["word_en"], "sentence_en": item["sentence_en"], "progression": item["progression"]}]}
        with patch.object(curriculum, "request_json", side_effect=[draft, {"items": [simplified]}, {"items": [item]}]) as request:
            result = curriculum.generate_batch(self.plan, [job], [])
        self.assertEqual(request.call_args_list[0].args[1], "進階英文骨架")
        self.assertEqual(result[0]["word_en"], item["word_en"])

    @patch.object(curriculum, "review_field_difficulty", return_value={})
    def test_advanced_example_preserves_exact_chunk_word_order(self, _field_review):
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
        with patch.object(curriculum, "request_json", side_effect=[invalid] * (curriculum.STALLED_RETRY_LIMIT + 1)
                + [{"items": [item]}]), \
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
        line = {"id": job["id"], "word_en": "be issued",
                "sentence_en": "Could my onward boarding pass be issued here?", "progression": "Uses be issued passive voice"}
        with patch.object(curriculum, "request_json", side_effect=[{"lines": [line]}]
                + [ValueError("stop")] * (curriculum.STALLED_RETRY_LIMIT + 1)) as request, self.assertRaises(curriculum.BatchGenerationError):
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

    def test_failed_field_repairs_are_checkpointed_and_resumed_without_redrafting(self):
        original = copy.deepcopy(self.items[0])
        original.update(tips="保持禮貌。")
        wrong = {"id": "01", "tips": original["tips"]}
        with patch.object(curriculum, "request_json", side_effect=[{"items": [original]}]
                + [{"items": [wrong]}] * (curriculum.STALLED_RETRY_LIMIT + 1)), self.assertRaises(curriculum.BatchGenerationError) as failure:
            curriculum.generate_batch(self.plan, self.plan["jobs"][:1], [])
        self.assertEqual(failure.exception.repairs["01"]["item"]["word_en"], original["word_en"])
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "checkpoint.json"
            with patch.object(curriculum, "generate_batch", side_effect=failure.exception), \
                    self.assertRaises(curriculum.BatchGenerationError):
                curriculum.generate_deck(self.plan, checkpoint, [])
            saved = json.loads(checkpoint.read_text())
            self.assertEqual(saved["pending_field_repairs"]["01"]["fields"], ["tips"])
            def generate_requested(plan, jobs, accepted, feedback, **kwargs):
                return [item for item in self.items if item["id"] in {job["id"] for job in jobs}]
            with patch.object(curriculum, "generate_batch", side_effect=generate_requested) as generate, \
                    patch.object(curriculum, "review_deck", return_value={}):
                state = curriculum.generate_deck(self.plan, checkpoint, [], resume=True)
            self.assertIn("01", generate.call_args_list[0].kwargs["repairs"])
            self.assertEqual([job["id"] for job in generate.call_args_list[0].args[1]], ["01"])
            self.assertEqual(state["pending_field_repairs"], {})

    def test_deck_rescues_only_unfinished_cards_with_author_model(self):
        first = self.items[:1]
        failure = curriculum.BatchGenerationError(first, "02 failed to shorten")
        def generate(plan, jobs, accepted, feedback, **options):
            if not options.get("author_first") and jobs[0]["id"] == "01":
                raise failure
            if options.get("author_first"):
                self.assertNotIn("01", {job["id"] for job in jobs})
                self.assertIn("01", {item["id"] for item in accepted})
                self.assertEqual(feedback, failure.feedback)
            return [item for item in self.items if item["id"] in {job["id"] for job in jobs}]
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(curriculum, "generate_batch", side_effect=generate) as api, \
                patch.object(curriculum, "review_deck", return_value={}):
            state = curriculum.generate_deck(self.plan, Path(directory) / "checkpoint.json", [])
        self.assertTrue(state["review_passed"])
        self.assertEqual(len(state["items"]), len(self.items))
        self.assertTrue(api.call_args_list[1].kwargs["author_first"])

    def test_author_first_rescue_uses_author_for_advanced_drafts(self):
        job = next(job for job in self.plan["jobs"] if job["tier"] == "advanced")
        item = copy.deepcopy(next(item for item in self.items if item["id"] == job["id"]))
        line = {key: item[key] for key in ("id", "word_en", "sentence_en", "progression")}
        with patch.object(curriculum, "review_field_difficulty", return_value={}), \
                patch.object(curriculum, "request_json", side_effect=[{"lines": [line]}, {"items": [item]}]) as api:
            curriculum.generate_batch(self.plan, [job], [], author_first=True)
        self.assertEqual([call.args[2] for call in api.call_args_list], [curriculum.AUTHOR_MODEL] * 2)

    def test_difficulty_checks_survive_review_timeout_and_resume(self):
        token = curriculum._difficulty_checks.set({})
        self.addCleanup(curriculum._difficulty_checks.reset, token)
        target = {"id": "F001", "line_en": "Can you help?"}
        check = {"id": "F001", "level": "basic", "progression": "Simple daily words"}
        def interrupted_review(*args):
            curriculum.confirm_difficulty([target])
            raise cards.GenerationTimeoutError("timeout")
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "checkpoint.json"
            with patch.object(curriculum, "generate_batch", side_effect=[self.items[:8], self.items[8:]]), \
                    patch.object(curriculum, "request_json", return_value={"checks": [check]}), \
                    patch.object(curriculum, "review_deck", side_effect=interrupted_review), \
                    self.assertRaises(cards.GenerationTimeoutError):
                curriculum.generate_deck(self.plan, checkpoint, [])
            self.assertEqual(len(json.loads(checkpoint.read_text())["difficulty_checks"]), 1)
            curriculum._difficulty_checks.get().clear()
            with patch.object(curriculum, "review_deck", return_value={}), \
                    patch.object(curriculum, "request_json", side_effect=AssertionError("Cached line must not be rechecked")):
                curriculum.generate_deck(self.plan, checkpoint, [], resume=True)
                self.assertEqual(curriculum.confirm_difficulty([target])[0], check)

    @patch.object(curriculum, "review_field_difficulty", return_value={})
    def test_advanced_anchors_survive_timeout_before_dependent_fields(self, _field_review):
        job = next(job for job in self.plan["jobs"] if job["tier"] == "advanced")
        item = next(item for item in self.items if item["id"] == job["id"])
        line = {key: item[key] for key in ("id", "word_en", "sentence_en", "progression")}
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "checkpoint.json"
            curriculum.save_json(checkpoint, {"version": curriculum.VERSION,
                "fingerprint": curriculum.fingerprint_for(self.plan, []), "plan": self.plan,
                "items": [row for row in self.items if row["id"] != job["id"]], "review_passed": False})
            with patch.object(curriculum, "request_json", side_effect=[{"lines": [line]},
                    cards.GenerationTimeoutError("timed out")]), self.assertRaises(cards.GenerationTimeoutError):
                curriculum.generate_deck(self.plan, checkpoint, [], resume=True)
            state = json.loads(checkpoint.read_text())
            self.assertEqual(state["pending_advanced_anchors"][job["id"]]["word_en"], line["word_en"])
            with patch.object(curriculum, "request_json", return_value={"items": [item]}) as api, \
                    patch.object(curriculum, "review_deck", return_value={}):
                state = curriculum.generate_deck(self.plan, checkpoint, [], resume=True)
            self.assertEqual(api.call_count, 1)
            self.assertTrue(api.call_args.args[1].startswith("教材生成 "))
            self.assertEqual(state["pending_advanced_anchors"], {})

    def test_timeout_during_field_repair_keeps_valid_cards_and_invalid_draft(self):
        original = copy.deepcopy(self.items[:2])
        original[0].update(tips="保持禮貌。")
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "checkpoint.json"
            with patch.object(curriculum, "BATCH_SIZE", 2), \
                    patch.object(curriculum, "request_json", side_effect=[{"items": original},
                        cards.GenerationTimeoutError("timed out")]), \
                    self.assertRaises(cards.GenerationTimeoutError):
                curriculum.generate_deck(self.plan, checkpoint, [])
            state = json.loads(checkpoint.read_text())
            self.assertEqual([item["id"] for item in state["items"]], ["02"])
            self.assertEqual(state["pending_field_repairs"]["01"]["fields"], ["tips"])
            self.assertFalse(state["review_passed"])

    def test_final_rejection_is_saved_and_resume_rewrites_only_failed_card(self):
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "checkpoint.json"
            with patch.object(curriculum, "generate_batch", side_effect=[self.items[:8], self.items[8:]]
                    + [self.items[:1]] * curriculum.STALLED_RETRY_LIMIT), \
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
            invalid[0]["tips"] = "保持禮貌。"
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
            self.assertEqual(audit.call_count, curriculum.STALLED_RETRY_LIMIT + 1)
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
            with patch.object(curriculum, "plan_curriculum") as plan, patch.object(cards, "REVIEW_MODE", "hybrid"), \
                 patch.object(cards, "write_youtube_description"):
                cards.main(["--topic", self.plan["topic"], "--count", "10", "--output", str(output)])
            plan.assert_not_called()

    def test_old_twelve_column_workbook_and_checkpoint_are_preserved_without_api_calls(self):
        import openpyxl
        with tempfile.TemporaryDirectory() as directory, patch.object(cards, "BASE_DIR", directory):
            output = Path(directory) / "old.xlsx"
            workbook = openpyxl.Workbook()
            workbook.active.append(LEARNING_HEADERS)
            for item in self.items:
                workbook.active.append([item[key] for key in LEARNING_HEADERS])
                item["_semantic_review_version"] = 11
            workbook.save(output)
            legacy = Path(str(output) + ".curriculum.json")
            curriculum.save_json(legacy, {"version": curriculum.VERSION, "review_passed": True,
                "review_version": 11, "fingerprint": curriculum.fingerprint_for(self.plan, []),
                "plan": self.plan, "items": self.items})
            original_excel, original_json = output.read_bytes(), legacy.read_bytes()
            with patch.object(cards, "REVIEW_MODE", "hybrid"), \
                    patch.object(cards, "_call_openai", side_effect=AssertionError("Paid API must not run")), \
                    patch.object(curriculum, "plan_curriculum") as planner:
                cards.main(["--topic", self.plan["topic"], "--count", "10", "--output", str(output), "--no-youtube"])
            planner.assert_not_called()
            self.assertEqual(output.read_bytes(), original_excel)
            self.assertEqual(legacy.read_bytes(), original_json)
            cached = cached_artifact(legacy, directory, "curriculum")
            self.assertEqual(cached.read_bytes(), original_json)

    def test_old_unused_field_repairs_can_resume_without_api_calls(self):
        source = dict(self.items[0], vocab=[], Core_Vocab="", sentence_ipa="bad", Tone="bad")
        repairs = {"01": {"item": source, "fields": ["vocab", "sentence_ipa"], "reason": "legacy rejection"}}
        with patch.object(curriculum, "request_json", side_effect=AssertionError("Unneeded field repair")):
            result = curriculum.generate_batch(self.plan, self.plan["jobs"][:1], [], repairs=repairs)
        self.assertEqual(result[0]["word_en"], source["word_en"])

    def test_consolidated_review_missing_groups_is_not_treated_as_success(self):
        semantic = {"assignments": [{"id": item["id"], "purpose": item["core"], "level": item["tier"],
                                     "progression": item["progression"]} for item in self.items], "reject": []}
        with patch.object(curriculum, "request_json", return_value=semantic), \
                patch.object(curriculum, "review_field_difficulty", return_value={}), self.assertRaisesRegex(ValueError, "groups"):
            curriculum.review_deck(self.plan, self.items, [])

    def test_consolidated_review_schema_requires_complete_decisions(self):
        from types import SimpleNamespace
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{}'))])
        with patch.object(cards, "_call_openai", return_value=response) as api:
            curriculum.request_json("Review", "教材綜合審查", "gpt-5-nano", job_ids=["01", "02"])
        schema = api.call_args.kwargs["response_format"]["json_schema"]["schema"]
        self.assertEqual(set(schema["required"]), {"assignments", "reject", "groups"})
        self.assertEqual(schema["properties"]["assignments"]["minItems"], 2)
        self.assertFalse(schema["additionalProperties"])

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
            self.assertTrue(cached_artifact(output.with_suffix(".plan.json"), cards.BASE_DIR, "curriculum").exists())
            self.assertFalse(output.with_suffix(".plan.json").exists())


class PlanningRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.plan, self.items = fixture()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.checkpoint = Path(self.directory.name) / "planning.json"
        for variable in (cards._generation_deadline, curriculum._pair_review_store):
            token = variable.set(None)
            self.addCleanup(variable.reset, token)
        self.messages = []
        progress = patch.object(cards, "_progress", side_effect=self.messages.append)
        progress.start()
        self.addCleanup(progress.stop)
        offline = patch.object(cards, "_call_openai", side_effect=AssertionError("Unexpected real API call"))
        offline.start()
        self.addCleanup(offline.stop)
        confirmation = patch.object(curriculum, "confirm_equivalent_pairs", side_effect=lambda targets, checks, **kwargs:
                                    {key: dict(check, _confirmed=True) for key, check in checks.items()})
        confirmation.start()
        self.addCleanup(confirmation.stop)

    def audit(self, plan=None):
        return {"assignments": [{"id": job["id"], "purpose": job["core"], "in_scope": True, "reason": ""}
                                for job in (plan or self.plan)["jobs"]]}

    def responses(self):
        return [{"scenarios": self.plan["scenarios"]}, {"jobs": self.plan["jobs"]}, self.audit()]

    def dense_candidates(self, count=50):
        scenarios = [f"scene-{index}" for index in range(curriculum.scenario_count_for(count))]
        slots = curriculum.slots_for(count, scenarios)
        plan = {"topic": "Privacy boundaries", "jobs": [
            dict(slot, core="boundary", task="具體任務" + slot["id"]) for slot in slots]}
        items = [{"id": job["id"], "word_en": job["task"], "sentence_en": job["task"]}
                 for job in plan["jobs"]]
        semantic = {"assignments": [{"id": job["id"], "purpose": job["core"]} for job in plan["jobs"]]}
        return plan, items, semantic

    def checks(self, equivalent):
        def respond(prompt, stage, model, **kwargs):
            self.assertEqual(stage, "教材同義逐對複核")
            return {"checks": {key: {"equivalent": equivalent, "reason": "Concrete result comparison"}
                               for key in kwargs["job_ids"]}}
        return respond

    def test_targeted_fifty_card_group_has_linear_candidate_count(self):
        plan, items, semantic = self.dense_candidates()
        with patch.object(curriculum, "request_json", side_effect=self.checks(False)) as request:
            curriculum.verify_semantic_pairs(plan, items, semantic, {"groups": []}, targeted=True)
        self.assertEqual(request.call_count, 4)
        self.assertEqual(sum(len(call.kwargs["job_ids"]) for call in request.call_args_list), 97)
        self.assertEqual(len({entry["purpose"] for entry in semantic["assignments"]}), 50)

    def test_targeted_review_does_not_expand_shared_verbs_or_planner_labels(self):
        plan, items, semantic = self.dense_candidates()
        for item, entry in zip(items, semantic["assignments"]):
            item["word_en"] = item["sentence_en"] = "Please respect " + item["id"]
            entry["purpose"] = "different outcome " + item["id"]
        with patch.object(curriculum, "request_json") as request:
            curriculum.verify_semantic_pairs(plan, items, semantic, {"groups": []}, targeted=True)
        request.assert_not_called()

    def test_review_version_upgrade_reuses_completed_planning(self):
        for old_version in (11, 12):
            checkpoint = self.checkpoint.with_name(f"planning-{old_version}.json")
            with self.subTest(old_version=old_version), \
                    patch.object(curriculum, "SEMANTIC_REVIEW_VERSION", old_version), \
                    patch.object(curriculum, "request_json", side_effect=self.responses()):
                curriculum.plan_curriculum(self.plan["topic"], 10, [], checkpoint=checkpoint)
            with patch.object(curriculum, "request_json") as api:
                plan = curriculum.plan_curriculum(self.plan["topic"], 10, [], checkpoint=checkpoint, resume=True)
            api.assert_not_called()
            curriculum.validate_plan(plan, self.plan["topic"], 10)

    def test_duplicate_planning_repairs_only_surplus_id_and_reuses_other_audits(self):
        duplicate = copy.deepcopy(self.plan)
        duplicate["jobs"][1].update(core=duplicate["jobs"][0]["core"], task="Ask what that means.")
        replacement = dict(duplicate["jobs"][1], core="report broken lock", task="Report the broken lock.")
        repaired = False

        def respond(prompt, stage, model, **kwargs):
            nonlocal repaired
            if stage == "主題情境策劃":
                return {"scenarios": duplicate["scenarios"]}
            if stage == "逐句教材策劃":
                return {"jobs": duplicate["jobs"]}
            if stage == "替換重複教材目的":
                self.assertEqual(kwargs["job_ids"], ["02"])
                repaired = True
                return {"jobs": [replacement]}
            if stage == "教材退回複核":
                return {"checks": [{"id": "02", "valid": True, "reason": "New repair report"}]}
            if stage == "教材策劃獨立審查":
                jobs = [replacement] if repaired else duplicate["jobs"]
                return self.audit(dict(duplicate, jobs=jobs))
            if stage == "教材同義逐對複核":
                return {"checks": {key: {"equivalent": key == "01:02" and not repaired,
                                          "reason": "Same request" if key == "01:02" else "Distinct outcome"}
                                   for key in kwargs["job_ids"]}}
            self.fail(stage)

        pair_token = curriculum._pair_review_store.set((Path(self.directory.name) / "pairs.json", {}))
        self.addCleanup(curriculum._pair_review_store.reset, pair_token)
        with patch.object(curriculum, "request_json", side_effect=respond) as request:
            result = curriculum.plan_curriculum(duplicate["topic"], 10, [], checkpoint=self.checkpoint)
        calls = request.call_args_list
        self.assertEqual(sum(call.args[1] == "逐句教材策劃" for call in calls), 1)
        audits = [call.kwargs["job_ids"] for call in calls if call.args[1] == "教材策劃獨立審查"]
        self.assertEqual(audits, [[job["id"] for job in duplicate["jobs"]], ["02"]])
        for before, after in zip(duplicate["jobs"], result["jobs"]):
            self.assertEqual(after["task"], replacement["task"] if after["id"] == "02" else before["task"])
        self.assertTrue(json.loads(self.checkpoint.read_text())["complete_plan"])

    def test_replacement_can_succeed_on_fourth_new_proposal(self):
        proposals = [dict(self.plan["jobs"][0], core=f"new-result-{index}", task=f"Report problem {index}.")
                     for index in range(4)]
        with patch.object(curriculum, "request_json", side_effect=[{"jobs": [job]} for job in proposals]) as request, \
                patch.object(curriculum, "validate_replacement_outcomes", side_effect=[ValueError("Overlap")] * 3 + [None]):
            result = curriculum.repair_duplicate_jobs(self.plan, {"01"}, {"01": "Duplicate"}, [])
        self.assertEqual(request.call_count, 4)
        self.assertEqual(result[0]["task"], proposals[-1]["task"])
        self.assertEqual(result[1:], self.plan["jobs"][1:])

    def test_old_attempt_three_checkpoint_can_continue_without_force(self):
        with patch.object(curriculum, "request_json", side_effect=cards.APIBudgetError("test budget")):
            with self.assertRaises(cards.APIBudgetError):
                curriculum.plan_curriculum(self.plan["topic"], 10, [], checkpoint=self.checkpoint)
        saved = json.loads(self.checkpoint.read_text())
        saved["attempt"] = 3
        curriculum.save_json(self.checkpoint, saved)
        with patch.object(curriculum, "request_json", side_effect=self.responses()) as request:
            result = curriculum.plan_curriculum(self.plan["topic"], 10, [], checkpoint=self.checkpoint)
        self.assertEqual(request.call_count, 3)
        curriculum.validate_plan(result, self.plan["topic"], 10)

    def test_relabeling_the_same_failed_task_does_not_count_as_progress(self):
        guard = curriculum.RepeatedFailureGuard()
        for index in range(curriculum.STALLED_RETRY_LIMIT):
            guard.reject({"task": "Ask for permission.", "core": f"label-{index}", "reason": str(index)}, "Rejected")
        with self.assertRaisesRegex(cards.CheckpointGenerationError, "沒有進展"):
            guard.reject({"task": "Ask for permission.", "core": "yet another label"}, "Different review wording")

    def test_budget_exhaustion_during_replacement_keeps_plan_and_pending_repair(self):
        rejection = curriculum.PlanningTaskError("Duplicate 01/02", {"02": "Duplicate 01/02"})
        with patch.object(curriculum, "request_json", side_effect=self.responses()) as request, \
                patch.object(curriculum, "verify_semantic_pairs", side_effect=rejection), \
                patch.object(curriculum, "repair_duplicate_jobs", side_effect=cards.APIBudgetError("test budget")):
            with self.assertRaises(cards.APIBudgetError):
                curriculum.plan_curriculum(self.plan["topic"], 10, [], checkpoint=self.checkpoint)
        saved = json.loads(self.checkpoint.read_text())
        self.assertEqual(saved["pending_task_repairs"], rejection.rejected)
        self.assertEqual(saved["jobs"], self.plan["jobs"])
        self.assertNotIn("complete_plan", saved)
        updated = copy.deepcopy(self.plan["jobs"])
        updated[1].update(core="new-result", task="Report a broken lock.")
        with patch.object(curriculum, "repair_duplicate_jobs", return_value=updated) as repair, \
                patch.object(curriculum, "verify_semantic_pairs"), \
                patch.object(curriculum, "request_json", return_value=self.audit(dict(self.plan, jobs=updated[1:2]))) as request:
            result = curriculum.plan_curriculum(self.plan["topic"], 10, [], checkpoint=self.checkpoint)
        self.assertEqual(request.call_args.kwargs["job_ids"], ["02"])
        self.assertEqual(result["jobs"][1]["task"], updated[1]["task"])
        repair.assert_called_once()

    def test_fifty_duplicate_tasks_fail_in_one_batch_not_thirty_nine(self):
        plan, items, semantic = self.dense_candidates()
        with patch.object(curriculum, "request_json", side_effect=self.checks(True)) as request:
            with self.assertRaisesRegex(ValueError, "立即退回"):
                curriculum.verify_semantic_pairs(plan, items, semantic, {"groups": []}, fail_fast=True)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(len(request.call_args.kwargs["job_ids"]), 32)
        self.assertIn("1225 對候選", self.messages[0])

    def test_three_confirmed_duplicates_fail_even_when_first_pair_has_valid_tiers(self):
        plan, items, semantic = self.dense_candidates(4)
        plan["jobs"][1]["tier"] = "advanced"
        with patch.object(curriculum, "request_json", side_effect=self.checks(True)):
            with self.assertRaisesRegex(ValueError, "01,02,03"):
                curriculum.verify_semantic_pairs(plan, items, semantic, {"groups": []}, fail_fast=True)

    def test_fifty_duplicate_tasks_stop_repeated_failed_repairs_and_never_export(self):
        plan, _, _ = self.dense_candidates()
        plan.update(version=curriculum.VERSION, count=50,
                    scenarios=list(dict.fromkeys(job["Scenario"] for job in plan["jobs"])))
        for job in plan["jobs"]:
            job.update(role="learner", speaker="neighbor")
        stages = []

        def respond(prompt, stage, model, **kwargs):
            stages.append(stage)
            if stage == "主題情境策劃":
                return {"scenarios": plan["scenarios"]}
            if stage == "逐句教材策劃":
                return {"jobs": plan["jobs"]}
            if stage == "教材策劃獨立審查":
                return self.audit(dict(plan, jobs=[job for job in plan["jobs"] if job["id"] in kwargs["job_ids"]]))
            if stage == "替換重複教材目的":
                return {"jobs": [job for job in plan["jobs"] if job["id"] in kwargs["job_ids"]]}
            if stage == "教材退回複核":
                return {"checks": [{"id": identifier, "valid": False, "reason": "Same old task"}
                                   for identifier in kwargs["job_ids"]]}
            return self.checks(True)(prompt, stage, model, **kwargs)

        output = Path(self.directory.name) / "privacy.xlsx"
        arguments = ["--topic", plan["topic"], "--count", "50", "--output", str(output), "--no-youtube"]
        with patch.object(cards, "REVIEW_MODE", "hybrid"), \
             patch.object(curriculum, "request_json", side_effect=respond), \
             patch.object(curriculum, "generate_deck") as generate, \
             patch.object(cards, "write_xlsx") as export:
            with self.assertRaisesRegex(cards.CheckpointGenerationError, "沒有進展"):
                cards.main(arguments)
        self.assertEqual(stages.count("主題情境策劃"), 1)
        self.assertEqual(stages.count("逐句教材策劃"), 1)
        self.assertEqual(stages.count("替換重複教材目的"), curriculum.STALLED_RETRY_LIMIT + 1)
        self.assertEqual(stages.count("教材同義逐對複核"), 1)
        generate.assert_not_called()
        export.assert_not_called()
        self.assertFalse(output.exists())
        saved = json.loads(cached_artifact(Path(str(output) + ".planning.json"), cards.BASE_DIR, "curriculum").read_text())
        self.assertEqual(len(saved["jobs"]), 50)
        self.assertEqual(set(saved["pending_task_repairs"]), {"02"})
        with patch.object(cards, "REVIEW_MODE", "hybrid"), \
                patch.object(curriculum, "request_json", side_effect=cards.APIBudgetError("test budget")) as request:
            with self.assertRaises(cards.APIBudgetError):
                cards.main(arguments)
        self.assertEqual(request.call_args.args[1], "替換重複教材目的")

    def test_confirmed_equivalence_skips_redundant_pairs_in_full_deck_review(self):
        plan, items, semantic = self.dense_candidates()
        with patch.object(curriculum, "request_json", side_effect=self.checks(True)) as request:
            curriculum.verify_semantic_pairs(plan, items, semantic, {"groups": []})
        self.assertEqual(request.call_count, 2)
        self.assertEqual(len({row["purpose"] for row in semantic["assignments"]}), 1)
        self.assertTrue(any("同組略過" in line for line in self.messages))

    def test_broad_labels_do_not_skip_unconfirmed_pairs_or_reject_distinct_tasks(self):
        plan, items, semantic = self.dense_candidates()
        with patch.object(curriculum, "request_json", side_effect=self.checks(False)) as request:
            curriculum.verify_semantic_pairs(plan, items, semantic, {"groups": []}, fail_fast=True)
        self.assertEqual(request.call_count, 39)
        self.assertEqual(sum(len(call.kwargs["job_ids"]) for call in request.call_args_list), 1225)
        self.assertEqual(len({row["purpose"] for row in semantic["assignments"]}), 50)

    def test_pair_cache_survives_timeout_and_only_unfinished_pairs_are_requested(self):
        plan, items, semantic = self.dense_candidates(10)
        pair_path = Path(self.directory.name) / "pairs.json"
        curriculum._pair_review_store.set((pair_path, {}))
        with patch.object(curriculum, "request_json", side_effect=[
            self.checks(False)("", "教材同義逐對複核", "", job_ids=[
                f"{left:02d}:{right:02d}" for left in range(1, 11) for right in range(left + 1, 11)][:32]),
            cards.GenerationTimeoutError("test timeout")]):
            with self.assertRaises(cards.GenerationTimeoutError):
                curriculum.verify_semantic_pairs(plan, items, semantic, {"groups": []}, fail_fast=True)
        saved = json.loads(pair_path.read_text())
        self.assertEqual(len(saved["checks"]), 32)
        curriculum._pair_review_store.set((pair_path, saved["checks"]))
        with patch.object(curriculum, "request_json", side_effect=self.checks(False)) as request:
            curriculum.verify_semantic_pairs(plan, items, semantic, {"groups": []}, fail_fast=True)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(len(request.call_args.kwargs["job_ids"]), 13)

    def test_pair_cache_invalidates_changed_text_brief_model_and_review_version(self):
        plan, items, semantic = self.dense_candidates(4)
        curriculum._pair_review_store.set((Path(self.directory.name) / "pairs.json", {}))
        with patch.object(curriculum, "request_json", side_effect=self.checks(False)) as request:
            curriculum.verify_semantic_pairs(plan, items, copy.deepcopy(semantic), {"groups": []})
            curriculum.verify_semantic_pairs(plan, items, copy.deepcopy(semantic), {"groups": []})
            self.assertEqual(request.call_count, 1)
            changed = copy.deepcopy(items)
            changed[0]["sentence_en"] += " new information"
            curriculum.verify_semantic_pairs(plan, changed, copy.deepcopy(semantic), {"groups": []})
            self.assertEqual(len(request.call_args.kwargs["job_ids"]), 3)
            curriculum.verify_semantic_pairs(dict(plan, topic="Different brief"), items,
                                              copy.deepcopy(semantic), {"groups": []})
            with patch.object(curriculum, "REVIEW_MODEL", "new-model"):
                curriculum.verify_semantic_pairs(plan, items, copy.deepcopy(semantic), {"groups": []})
            with patch.object(curriculum, "SEMANTIC_REVIEW_VERSION", 999):
                curriculum.verify_semantic_pairs(plan, items, copy.deepcopy(semantic), {"groups": []})
        self.assertEqual(request.call_count, 5)

    def test_pair_review_uses_literal_outcomes_without_brief_instructions(self):
        plan, items, semantic = self.dense_candidates(4)
        plan["topic"] = "PRIVATE BRIEF INSTRUCTIONS THAT MUST NOT ENTER THE COMPARISON"
        with patch.object(curriculum, "request_json", side_effect=self.checks(False)) as request:
            curriculum.verify_semantic_pairs(plan, items, semantic, {"groups": []})
        prompt, _, model = request.call_args.args
        self.assertNotIn(plan["topic"], prompt)
        self.assertIn("LITERAL speech act", prompt)
        self.assertIn("do not RECORD", prompt)
        self.assertEqual(model, curriculum.PAIR_MODEL)

    def test_pair_cache_invalidates_changed_pair_model(self):
        plan, items, semantic = self.dense_candidates(4)
        curriculum._pair_review_store.set((Path(self.directory.name) / "pairs.json", {}))
        with patch.object(curriculum, "request_json", side_effect=self.checks(False)) as request:
            curriculum.verify_semantic_pairs(plan, items, copy.deepcopy(semantic), {"groups": []})
            with patch.object(curriculum, "PAIR_MODEL", "different-pair-model"):
                curriculum.verify_semantic_pairs(plan, items, copy.deepcopy(semantic), {"groups": []})
        self.assertEqual(request.call_count, 2)

    def test_invalid_pair_responses_are_never_cached(self):
        plan, items, semantic = self.dense_candidates(4)
        pair_path = Path(self.directory.name) / "pairs.json"
        cache = {}
        curriculum._pair_review_store.set((pair_path, cache))
        for payload in ({"checks": {}}, {"checks": {f"{left:02d}:{right:02d}": {
                "equivalent": "false", "reason": "Invalid boolean"}
                for left in range(1, 5) for right in range(left + 1, 5)}}):
            with patch.object(curriculum, "request_json", return_value=payload), self.assertRaises(ValueError):
                curriculum.verify_semantic_pairs(plan, items, semantic, {"groups": []})
            self.assertEqual(cache, {})
            self.assertFalse(pair_path.exists())

    def test_expired_deadline_does_not_start_a_pair_request(self):
        import time
        plan, items, semantic = self.dense_candidates()
        cards._generation_deadline.set(time.monotonic() - 1)
        with patch.object(curriculum, "request_json") as request:
            with self.assertRaises(cards.GenerationTimeoutError):
                curriculum.verify_semantic_pairs(plan, items, semantic, {"groups": []})
        request.assert_not_called()

    def test_planning_resumes_each_validated_stage_after_timeout_or_interrupt(self):
        for completed in (1, 2, 3):
            for error in (cards.GenerationTimeoutError("test timeout"), KeyboardInterrupt()):
                with self.subTest(completed=completed, error=type(error).__name__):
                    checkpoint = self.checkpoint.with_name(f"stage-{completed}-{type(error).__name__}.json")
                    responses = self.responses()
                    with patch.object(curriculum, "request_json", side_effect=responses[:completed] + [error]), \
                         patch.object(curriculum, "verify_semantic_pairs", side_effect=error if completed == 3 else None):
                        with self.assertRaises(type(error)):
                            curriculum.plan_curriculum(self.plan["topic"], 10, [], checkpoint=checkpoint)
                    saved = json.loads(checkpoint.read_text())
                    self.assertEqual(saved["attempt"], 0)
                    self.assertEqual("audit" in saved, completed == 3)
                    with patch.object(curriculum, "request_json", side_effect=responses[completed:]) as request:
                        result = curriculum.plan_curriculum(self.plan["topic"], 10, [], checkpoint=checkpoint, resume=True)
                    self.assertEqual(request.call_count, 3 - completed)
                    curriculum.validate_plan(result, self.plan["topic"], 10)
                    with patch.object(curriculum, "request_json") as request:
                        curriculum.plan_curriculum(self.plan["topic"], 10, [], checkpoint=checkpoint)
                    request.assert_not_called()

    def test_planning_retry_count_survives_restart_without_permanent_exhaustion(self):
        with patch.object(curriculum, "request_json", side_effect=[
            {"scenarios": ["invalid"]}, cards.GenerationTimeoutError("test timeout")]):
            with self.assertRaises(cards.GenerationTimeoutError):
                curriculum.plan_curriculum(self.plan["topic"], 10, [], checkpoint=self.checkpoint)
        self.assertEqual(json.loads(self.checkpoint.read_text())["attempt"], 1)
        with patch.object(curriculum, "request_json", return_value={"scenarios": []}) as request:
            with self.assertRaisesRegex(cards.CheckpointGenerationError, "沒有進展"):
                curriculum.plan_curriculum(self.plan["topic"], 10, [], checkpoint=self.checkpoint)
            self.assertEqual(request.call_count, curriculum.STALLED_RETRY_LIMIT + 1)
        self.assertGreater(json.loads(self.checkpoint.read_text())["attempt"], 3)
        with patch.object(curriculum, "request_json", side_effect=self.responses()) as request:
            result = curriculum.plan_curriculum(self.plan["topic"], 10, [], checkpoint=self.checkpoint)
        curriculum.validate_plan(result, self.plan["topic"], 10)
        self.assertEqual(request.call_count, 3)

    def test_changed_planning_inputs_rejected_on_explicit_resume_and_replanned_otherwise(self):
        with patch.object(curriculum, "request_json", side_effect=self.responses()):
            curriculum.plan_curriculum(self.plan["topic"], 10, [], checkpoint=self.checkpoint)
        for changes in ({"topic": "Changed brief"}, {"count": 15}, {"references": [{"word_en": "old"}]}):
            arguments = dict(topic=self.plan["topic"], count=10, references=[], checkpoint=self.checkpoint, resume=True)
            arguments.update(changes)
            with patch.object(curriculum, "request_json") as request, self.assertRaisesRegex(ValueError, "已變更"):
                curriculum.plan_curriculum(**arguments)
            request.assert_not_called()
        with patch.object(curriculum, "REVIEW_MODEL", "new-model"), \
             patch.object(curriculum, "request_json", side_effect=self.responses()) as request:
            curriculum.plan_curriculum(self.plan["topic"], 10, [], checkpoint=self.checkpoint)
        self.assertEqual(request.call_count, 3)

    def test_new_spoken_task_policy_invalidates_old_planning_cache(self):
        with patch.object(curriculum, "request_json", side_effect=self.responses()):
            curriculum.plan_curriculum(self.plan["topic"], 10, [], checkpoint=self.checkpoint)
        original = self.checkpoint.read_bytes()
        with patch.object(curriculum, "SPOKEN_TASK_POLICY", "Revised spoken-task contract"), \
                patch.object(curriculum, "request_json") as request:
            with self.assertRaisesRegex(ValueError, "已變更"):
                curriculum.plan_curriculum(self.plan["topic"], 10, [], checkpoint=self.checkpoint, resume=True)
        request.assert_not_called()
        self.assertEqual(self.checkpoint.read_bytes(), original)

    def test_completed_planning_cache_cannot_hide_lesson_activities(self):
        with patch.object(curriculum, "request_json", side_effect=self.responses()):
            curriculum.plan_curriculum(self.plan["topic"], 10, [], checkpoint=self.checkpoint)
        saved = json.loads(self.checkpoint.read_text())
        saved["complete_plan"]["jobs"][0]["task"] = "培養自我價值感"
        curriculum.save_json(self.checkpoint, saved)
        with patch.object(curriculum, "request_json") as request:
            with self.assertRaisesRegex(cards.CheckpointGenerationError, "教學活動"):
                curriculum.plan_curriculum(self.plan["topic"], 10, [], checkpoint=self.checkpoint, resume=True)
        request.assert_not_called()

    def test_cli_resume_works_before_approved_plan_exists(self):
        output = Path(self.directory.name) / "topic.xlsx"
        arguments = ["--topic", self.plan["topic"], "--count", "10", "--output", str(output), "--plan-only"]
        with patch.object(cards, "REVIEW_MODE", "hybrid"), \
             patch.object(curriculum, "request_json", side_effect=self.responses()[:2] + [cards.GenerationTimeoutError("test")]):
            with self.assertRaises(cards.GenerationTimeoutError):
                cards.main(arguments)
        self.assertFalse(output.with_suffix(".plan.json").exists())
        self.assertTrue(cached_artifact(Path(str(output) + ".planning.json"), cards.BASE_DIR, "curriculum").exists())
        with patch.object(cards, "REVIEW_MODE", "hybrid"), \
             patch.object(curriculum, "request_json", return_value=self.audit()) as request:
            cards.main(arguments + ["--resume"])
        self.assertEqual(request.call_count, 1)
        self.assertTrue(cached_artifact(output.with_suffix(".plan.json"), cards.BASE_DIR, "curriculum").exists())
        self.assertFalse(output.exists())
        self.assertIsNone(curriculum._pair_review_store.get())

    def test_normal_cli_rerun_continues_generation_without_replanning_or_rewriting_good_cards(self):
        output = Path(self.directory.name) / "topic.xlsx"
        arguments = ["--topic", self.plan["topic"], "--count", "10", "--output", str(output), "--no-youtube"]
        with patch.object(cards, "REVIEW_MODE", "hybrid"), \
             patch.object(curriculum, "plan_curriculum", return_value=self.plan), \
             patch.object(curriculum, "generate_batch", side_effect=[self.items[:8], cards.GenerationTimeoutError("test")]):
            with self.assertRaises(cards.GenerationTimeoutError):
                cards.main(arguments)
        self.assertFalse(output.exists())
        saved = json.loads(cached_artifact(Path(str(output) + ".curriculum.json"), cards.BASE_DIR, "curriculum").read_text())
        self.assertEqual(len(saved["items"]), 8)
        with patch.object(cards, "REVIEW_MODE", "hybrid"), \
             patch.object(curriculum, "plan_curriculum") as planner, \
             patch.object(curriculum, "generate_batch", return_value=self.items[8:]) as generate, \
             patch.object(curriculum, "review_deck", return_value={}):
            cards.main(arguments)
        planner.assert_not_called()
        self.assertEqual([job["id"] for job in generate.call_args.args[1]], ["09", "10"])
        self.assertEqual(len(cards.load_xlsx_items(str(output))), 10)

    def test_force_backs_up_planning_and_pair_checkpoints_and_starts_fresh(self):
        output = Path(self.directory.name) / "topic.xlsx"
        planning = cached_artifact(Path(str(output) + ".planning.json"), self.directory.name, "curriculum")
        pairs = cached_artifact(Path(str(output) + ".pairs.json"), self.directory.name, "curriculum")
        with patch.object(curriculum, "request_json", side_effect=self.responses()):
            curriculum.plan_curriculum(self.plan["topic"], 10, [], checkpoint=planning)
        curriculum.save_json(pairs, {"version": 1, "checks": {"old": {"equivalent": True, "reason": "old"}}})
        old_planning, old_pairs = planning.read_bytes(), pairs.read_bytes()
        with patch.object(cards, "BASE_DIR", self.directory.name), patch.object(cards, "REVIEW_MODE", "hybrid"), \
             patch.object(curriculum, "request_json", side_effect=self.responses()) as request:
            cards.main(["--topic", self.plan["topic"], "--count", "10", "--output", str(output), "--force", "--plan-only"])
        self.assertEqual(request.call_count, 3)
        backups = list((Path(self.directory.name) / ".cleanup-backups").glob("generation-*"))
        self.assertEqual((backups[0] / ("00-" + planning.name)).read_bytes(), old_planning)
        self.assertEqual((backups[0] / ("01-" + pairs.name)).read_bytes(), old_pairs)
        self.assertEqual(json.loads(pairs.read_text())["checks"], {})


class PairConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.target = {"key": "01:02", "a": {"word_en": "Why do you ask?", "sentence_en": "Why ask about my salary?"},
                       "b": {"word_en": "Please stop asking.", "sentence_en": "Stop asking about my salary."}}
        self.initial = {"01:02": {"equivalent": True, "reason": "Both protect privacy"}}
        self.proof = {"equivalent": False, "reason": "Question motive versus stop questions",
                      "a_evidence": "Why do you ask?", "b_evidence": "Please stop asking.",
                      "a_outcome": "Ask the reason for the question", "b_outcome": "Request an end to questioning"}

    def test_positive_candidate_requires_source_grounded_second_verdict(self):
        with patch.object(curriculum, "request_json", return_value={"checks": {"01:02": self.proof}}) as request:
            result = curriculum.confirm_equivalent_pairs([self.target], self.initial)
        self.assertFalse(result["01:02"]["equivalent"])
        self.assertTrue(result["01:02"]["_confirmed"])
        self.assertTrue(self.initial["01:02"]["equivalent"])
        self.assertEqual(request.call_args.args[1], "教材同義複核確認")
        self.assertEqual(request.call_args.kwargs["anchors"], {"01:02": self.target})

    def test_negative_candidates_need_no_second_request(self):
        initial = {"01:02": dict(self.initial["01:02"], equivalent=False)}
        with patch.object(curriculum, "request_json") as request:
            self.assertEqual(curriculum.confirm_equivalent_pairs([self.target], initial), initial)
        request.assert_not_called()

    def test_confirmation_cannot_quote_invented_or_other_card_text(self):
        proof = dict(self.proof, a_evidence="Do not record this.")
        with patch.object(curriculum, "request_json", return_value={"checks": {"01:02": proof}}), \
                self.assertRaisesRegex(ValueError, "不是該配對的原文"):
            curriculum.confirm_equivalent_pairs([self.target], self.initial)

    def test_unconfirmed_positive_is_not_cached_on_timeout(self):
        plan = {"topic": "Privacy", "jobs": [{"id": "01", "core": "a"}, {"id": "02", "core": "b"}]}
        items = [dict(id=identifier, **self.target[side]) for identifier, side in (("01", "a"), ("02", "b"))]
        semantic = {"assignments": [{"id": identifier, "purpose": "privacy"} for identifier in ("01", "02")]}
        cache = {}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pairs.json"
            token = curriculum._pair_review_store.set((path, cache))
            try:
                with patch.object(curriculum, "request_json", side_effect=[{"checks": self.initial},
                        cards.GenerationTimeoutError("test timeout")]), \
                        self.assertRaises(cards.GenerationTimeoutError):
                    curriculum.verify_semantic_pairs(plan, items, semantic, {"groups": []})
            finally:
                curriculum._pair_review_store.reset(token)
            self.assertEqual(cache, {})
            self.assertFalse(path.exists())

    def test_confirmation_schema_locks_each_side_to_its_own_actual_text(self):
        from types import SimpleNamespace
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"checks":{}}'))])
        with patch.object(cards, "_call_openai", return_value=response) as api:
            curriculum.request_json("Check", "教材同義複核確認", "gpt-4o-mini", job_ids=["01:02"],
                                    anchors={"01:02": self.target})
        fields = api.call_args.kwargs["response_format"]["json_schema"]["schema"]["properties"]["checks"]["properties"]["01:02"]["properties"]
        self.assertEqual(fields["a_evidence"]["enum"], list(self.target["a"].values()))
        self.assertEqual(fields["b_evidence"]["enum"], list(self.target["b"].values()))


class TaskFidelityTests(unittest.TestCase):
    def test_bilingual_confirmation_locks_the_four_evidence_fields(self):
        from types import SimpleNamespace
        plan, items = fixture()
        item = items[0]
        fields = ("word_en", "word_cn", "sentence_en", "sentence_cn")
        check = {field + "_evidence": item[field] for field in fields}
        check.update(word_cn_backtranslation=item["word_en"], sentence_cn_backtranslation=item["sentence_en"],
                     valid=True, reason="Equivalent meanings")
        for field in ("main_translation_matches", "example_translation_matches", "main_fulfills_task",
                      "example_fulfills_task", "same_action", "no_other_objective_errors"):
            check[field] = True
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=json.dumps({"checks": {"01": check}})))])
        with patch.object(cards, "_call_openai", return_value=response) as api:
            result = curriculum.request_json("Check", "教材雙語確認", "gpt-4o-mini",
                                             job_ids=["01"], anchors={"01": item})
        schema = api.call_args.kwargs["response_format"]["json_schema"]["schema"]
        for field in fields:
            self.assertEqual(schema["properties"]["checks"]["properties"]["01"]["properties"][field + "_evidence"]["enum"],
                             [item[field]])
        self.assertEqual(result["checks"][0]["id"], "01")
        check["sentence_en_evidence"] = "Invented words not in the example"
        response.choices[0].message.content = json.dumps({"checks": {"01": check}})
        with patch.object(cards, "_call_openai", return_value=response), self.assertRaisesRegex(ValueError, "不存在"):
            curriculum.request_json("Check", "教材雙語確認", "gpt-4o-mini", job_ids=["01"], anchors={"01": item})

    def test_bilingual_false_positive_requires_independent_confirmation(self):
        plan, items = fixture()
        items = items[:1]
        initial = {"checks": [{"id": "01", "valid": False, "reason": "English is not identical to Chinese"}]}
        confirmed = {"checks": [{"id": "01", "valid": True, "reason": "Equivalent translation"}]}
        with patch.object(curriculum, "request_json", side_effect=[initial, confirmed]) as api:
            self.assertEqual(curriculum.review_task_fidelity(plan, items), {})
        self.assertEqual(api.call_count, 2)
        for call in api.call_args_list:
            self.assertIn("must NOT be identical strings", call.args[0])
            self.assertIn("Never replace an English field with Chinese", call.args[0])
        self.assertEqual(api.call_args_list[1].args[2], curriculum.SEMANTIC_MODEL)

    def test_confirmation_must_cover_every_rejected_card(self):
        plan, items = fixture()
        with patch.object(curriculum, "request_json", side_effect=[
                {"checks": [{"id": "01", "valid": False, "reason": "Wrong role"}]}, {"checks": []}]), \
                self.assertRaises(ValueError):
            curriculum.review_task_fidelity(plan, items[:1])

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
        with patch.object(curriculum, "request_json", side_effect=lambda *args, **kwargs:
                {"checks": [check for check in checks if check["id"] in kwargs["job_ids"]]}) as api:
            self.assertEqual(curriculum.review_task_fidelity(plan, items), {"01": "Permission is not obligation"})
        prompt = api.call_args_list[0].args[0]
        context = json.loads(prompt[prompt.index('{"cards"'):])
        self.assertNotIn("word_ipa", context["cards"][0])
        self.assertEqual(context["cards"][0]["task"], plan["jobs"][0]["task"])


if __name__ == "__main__":
    unittest.main()
