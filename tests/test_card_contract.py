import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import cards
import curriculum
import main
from card_contract import CONTENT_RULES, chunk_issues, content_issues, english_field_issues


def card():
    return {
        "word_en": "escalate this to a supervisor",
        "word_ipa": "/ˈɛskəleɪt ðɪs tu ə ˈsupərvaɪzər/",
        "word_cn": "把這件事交給主管處理",
        "sentence_en": "I need to escalate this to a supervisor. This is unacceptable.",
        "sentence_ipa": "/aɪ nid tu ˈɛskəleɪt ðɪs tu ə ˈsupərvaɪzər ðɪs ɪz ˌʌnəkˈsɛptəbəl/",
        "sentence_cn": "這太離譜了，我要請主管出面處理。",
        "tips": "語氣強硬，適合對方屢勸不聽時的最後通牒",
    }


class CardContentContractTests(unittest.TestCase):
    def test_requested_example_passes_all_content_checks(self):
        self.assertEqual(content_issues(card()), [])
        self.assertEqual(cards._validation_issues(card()), [])

    def test_word_count_uses_two_to_six_and_keeps_eight_word_hard_cap(self):
        self.assertEqual(cards.MAX_WORD_EN_WORDS, 8)
        for chunk in ("follow up", "escalate this directly to a supervisor"):
            with self.subTest(chunk=chunk):
                self.assertEqual(chunk_issues(chunk), [])
        for chunk in ("escalate", "escalate this issue directly to a supervisor"):
            with self.subTest(chunk=chunk):
                self.assertTrue(chunk_issues(chunk))
        self.assertTrue(any("9>8" in issue for issue in chunk_issues(
            "escalate this very serious issue directly to a supervisor")))
        long = dict(card(), word_en="escalate this very serious issue directly to a supervisor")
        self.assertTrue(any("9>8" in issue for issue in cards._validation_issues(long, max_word_en_words=12)))

    def test_sentences_questions_and_polite_wrappers_are_not_chunks(self):
        for chunk in ("I need a supervisor", "I'm not interested", "Could you refund it?",
                      "Please refund my deposit", "The refund arrived."):
            with self.subTest(chunk=chunk):
                self.assertTrue(chunk_issues(chunk))
        self.assertEqual(chunk_issues("this time around"), [])
        self.assertEqual(chunk_issues("none of your business"), [])

    def test_local_shape_checks_allow_punctuation_case_and_mild_inflections(self):
        examples = (
            ("keep it to myself", "I'd rather KEEP IT TO MYSELF."),
            ("on someone's behalf", "I'm here on someone’s behalf."),
            ("take charge", "I took charge after nobody stepped up."),
            ("take charge", "She takes charge whenever things go wrong."),
            ("set clear boundaries", "I've set clear boundaries; stop asking."),
            ("turn down the offer", "I turned the offer down yesterday."),
            ("refund my deposit", "They refunded my deposit, finally."),
            ("follow up", "I'll FOLLOW, UP tomorrow."),
        )
        for chunk, sentence in examples:
            with self.subTest(chunk=chunk, sentence=sentence):
                self.assertEqual(english_field_issues(chunk, sentence), [])

    def test_semantic_chunk_application_is_deferred_to_tier_three(self):
        self.assertEqual(english_field_issues("refund my deposit", "They kept all my money."), [])
        self.assertIn("Tier 3 AI Review", CONTENT_RULES)
        self.assertIn("漏用或改變詞塊的核心意思", CONTENT_RULES)

    def test_example_cannot_be_just_the_chunk_or_over_fourteen_words(self):
        value = card()
        value["sentence_en"] = value["word_en"] + "."
        self.assertTrue(content_issues(value))
        value["sentence_en"] = "I need to escalate this to a supervisor because this problem still has not been resolved."
        self.assertTrue(any("sentence_en has" in issue for issue in content_issues(value)))

    def test_phrase_and_sentence_translations_cannot_differ_only_in_punctuation(self):
        for translation in (card()["word_cn"], card()["word_cn"] + "。", "把這件事 交給主管處理！"):
            with self.subTest(translation=translation):
                self.assertTrue(any(issue.startswith("sentence_cn") for issue in content_issues(
                    dict(card(), sentence_cn=translation))))

    def test_tips_limit_and_rejection_of_robot_templates(self):
        self.assertEqual(content_issues(dict(card(), tips="語" * 36)), [])
        self.assertTrue(content_issues(dict(card(), tips="語" * 37)))
        for tip in ("當對方屢勸不聽時，請強硬回應。", "中立：當你想退款時使用。"):
            with self.subTest(tip=tip):
                self.assertTrue(any(issue.startswith("tips") for issue in content_issues(dict(card(), tips=tip))))

    def test_prompts_share_contract_and_do_not_allow_identical_english_fields(self):
        self.assertIn(CONTENT_RULES, cards.FIELD_SPEC)
        prompt = cards._build_prompt("投訴", 1)
        self.assertIn(CONTENT_RULES, prompt)
        self.assertNotIn("sentence_en 可以相同", prompt)
        self.assertEqual(curriculum.MAIN_WORD_LIMIT, 8)

    def test_legacy_sentence_lock_preserves_example_but_releases_invalid_word_field(self):
        point = {"task": "要求主管處理", "target_phrase": "I need to escalate this to a supervisor.",
                 "target_sentence": card()["sentence_en"]}
        exact = cards._exact_generation_point(point)
        self.assertEqual(exact["target_phrase"], "")
        self.assertEqual(exact["target_sentence"], card()["sentence_en"])
        value = dict(card(), purpose_id=1)
        locked = cards._apply_locked_blueprint_lines(value, [point])
        self.assertEqual(locked["word_en"], value["word_en"])
        self.assertIsNone(cards._locked_item_issue(locked, point))

    def test_curriculum_schema_has_chunk_descriptions_and_free_form_tips(self):
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"items":{}}'))])
        with patch.object(cards, "_call_openai", return_value=response) as api:
            curriculum.request_json("Generate", "教材生成 01-01", "gpt-4o-mini", job_ids=["01"])
        fields = api.call_args.kwargs["response_format"]["json_schema"]["schema"]["properties"]["items"]["properties"]["01"]["properties"]
        self.assertIn("lexical chunk", fields["word_en"]["description"])
        self.assertIn("mild inflections", fields["sentence_en"]["description"])
        self.assertNotIn("exact contiguous", fields["sentence_en"]["description"])
        self.assertEqual(fields["tips"]["maxLength"], 36)
        self.assertNotIn("pattern", fields["tips"])

    def test_legacy_checkpoint_fingerprint_does_not_match_new_contract(self):
        plan = {"topic": "test"}
        old = hashlib.sha256(json.dumps({"plan": plan, "references": []}, sort_keys=True,
                                       ensure_ascii=False).encode()).hexdigest()
        self.assertNotEqual(curriculum.fingerprint_for(plan, []), old)

    def test_invalid_saved_advanced_anchor_is_redrafted(self):
        job = {"id": "01", "Scenario": "complaint", "tier": "advanced", "core": "escalate",
               "task": "要求主管處理", "role": "learner", "speaker": "customer"}
        value = dict(card(), **job, progression="Uses richer vocabulary 'escalate'")
        draft = {key: value[key] for key in ("id", "word_en", "sentence_en", "progression")}
        old = dict(draft, word_en="I need to escalate this to a supervisor.")
        with patch.object(curriculum, "review_field_difficulty", return_value={}), \
                patch.object(curriculum, "request_json", side_effect=[{"lines": [draft]}, {"items": [value]}]) as api:
            result = curriculum.generate_batch({"topic": "complaints"}, [job], [], anchors={"01": old})
        self.assertEqual(api.call_args_list[0].args[1], "進階英文骨架")
        self.assertEqual(result[0]["word_en"], value["word_en"])

    def test_video_generator_skips_bad_translation_before_deduplication(self):
        bad = dict(card(), sentence_cn=card()["word_cn"])
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=json.dumps({"items": [bad, card()]})))])
        for context in ("", "news context " * 12):
            with self.subTest(context=bool(context)), tempfile.TemporaryDirectory() as directory:
                with patch.object(main, "USED_WORDS_FILE", str(Path(directory) / "used.json")), \
                        patch.object(main, "_call_openai", return_value=response) as api:
                    result = main.generate_content("投訴", 1, context)
                self.assertEqual(result[0]["sentence_cn"], card()["sentence_cn"])
                self.assertIn(CONTENT_RULES, api.call_args.kwargs["messages"][0]["content"])
                self.assertNotIn("exact contiguous", api.call_args.kwargs["messages"][0]["content"])
                self.assertEqual(api.call_count, 1)

    def test_curriculum_redrafts_sentence_word_field_instead_of_accepting_it(self):
        job = {"id": "01", "Scenario": "photo", "tier": "basic", "core": "retake",
               "task": "要求重拍", "role": "learner", "speaker": "customer"}
        value = dict(card(), **job, word_en="take it again", word_ipa="/teɪk ɪt əˈɡɛn/",
                     word_cn="再拍一次", sentence_en="Could you take it again? This one's blurry.",
                     sentence_cn="這張模糊了，能再拍一次嗎？", progression="")
        bad = copy.deepcopy(value)
        bad["word_en"] = "Could you take it again?"
        with patch.object(curriculum, "request_json", side_effect=[{"items": [bad]}, {"items": [value]}]) as api:
            result = curriculum.generate_batch({"topic": "photos"}, [job], [])
        self.assertEqual(result[0]["word_en"], "take it again")
        self.assertEqual(api.call_count, 2)
        self.assertIn(CONTENT_RULES, api.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
