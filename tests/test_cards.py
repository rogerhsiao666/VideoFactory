import asyncio
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from openai import AsyncOpenAI, APITimeoutError, AuthenticationError, BadRequestError
from prompt_toolkit import PromptSession
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

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


class OpenAIProgressTests(unittest.TestCase):
    def setUp(self):
        self.real_progress = cards._progress
        self.messages = []
        self.clients = []
        self.client_options = []
        for name, value in (
            ("OPENAI_KEYS", ["test-only-key"]),
            ("API_REQUEST_TIMEOUT", 0.2),
            ("API_CALL_TIMEOUT", 0.4),
            ("PROGRESS_INTERVAL", 0.01),
            ("API_RETRY_DELAY", 0.001),
        ):
            patcher = patch.object(cards, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        logger = patch.object(cards, "_progress", side_effect=self.messages.append)
        logger.start()
        self.addCleanup(logger.stop)
        token = cards._generation_deadline.set(None)
        self.addCleanup(cards._generation_deadline.reset, token)

    def mock_transport(self, handler):
        def factory(**kwargs):
            self.client_options.append(kwargs)
            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            self.clients.append(client)
            return AsyncOpenAI(**kwargs, http_client=client)

        patcher = patch.object(cards, "AsyncOpenAI", side_effect=factory)
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def response():
        return httpx.Response(200, json={
            "id": "test", "object": "chat.completion", "created": 0,
            "model": "test", "choices": [{
                "index": 0, "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }],
        })

    def call(self):
        return cards._call_openai(
            [{"role": "user", "content": "test"}], stage="測試策劃", model="test"
        )

    def test_success_reports_start_wait_and_finish_without_leaking_stage_to_api(self):
        async def handler(request):
            self.assertNotIn("stage", json.loads(request.content))
            await asyncio.sleep(0.04)
            return self.response()

        self.mock_transport(handler)
        result = self.call()

        self.assertEqual(result.choices[0].message.content, "ok")
        self.assertTrue(any("開始請求" in line for line in self.messages))
        self.assertTrue(any("等待 API 回應" in line for line in self.messages))
        self.assertIn("API 回應完成", self.messages[-1])
        self.assertEqual(self.client_options[0]["max_retries"], 0)
        self.assertTrue(all(client.is_closed for client in self.clients))
        self.assertNotIn("test-only-key", "\n".join(self.messages))

    def test_explicit_stage_budget_is_local_and_not_sent_to_api(self):
        async def handler(request):
            self.assertNotIn("budget_seconds", json.loads(request.content))
            await asyncio.sleep(0.02)
            return self.response()

        self.mock_transport(handler)
        with patch.object(cards, "API_REQUEST_TIMEOUT", 0.001):
            response = cards._call_openai([], stage="大型教材審稿", model="test", budget_seconds=0.1)
        self.assertEqual(response.choices[0].message.content, "ok")
        self.assertLessEqual(self.client_options[0]["timeout"].read, 0.1)

    def test_timeouts_cancel_pending_requests_and_stop_after_two_attempts(self):
        cancelled = []

        async def handler(request):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(True)

        self.mock_transport(handler)
        started = time.monotonic()
        with patch.object(cards, "API_REQUEST_TIMEOUT", 0.03):
            with self.assertRaises(APITimeoutError):
                self.call()

        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(len(cancelled), 2)
        self.assertTrue(any("秒後重試" in line for line in self.messages))
        self.assertTrue(all(client.is_closed for client in self.clients))

    def test_call_budget_caps_retries_across_two_keys(self):
        calls = []

        async def handler(request):
            calls.append(True)
            await asyncio.Event().wait()

        self.mock_transport(handler)
        with (
            patch.object(cards, "OPENAI_KEYS", ["test-key-a", "test-key-b"]),
            patch.object(cards, "API_CALL_TIMEOUT", 0.03),
        ):
            with self.assertRaises(APITimeoutError):
                self.call()

        self.assertEqual(len(calls), 1)
        self.assertTrue(all(client.is_closed for client in self.clients))

    def test_run_deadline_interrupts_request_without_retry(self):
        async def handler(request):
            await asyncio.Event().wait()

        self.mock_transport(handler)
        cards._generation_deadline.set(time.monotonic() + 0.03)
        with self.assertRaises(cards.GenerationTimeoutError):
            self.call()

        self.assertEqual(len(self.clients), 1)
        self.assertFalse(any("秒後重試" in line for line in self.messages))
        self.assertTrue(self.clients[0].is_closed)

    def test_expired_run_does_not_start_a_request(self):
        self.mock_transport(lambda request: self.response())
        cards._generation_deadline.set(time.monotonic() - 1)
        with self.assertRaises(cards.GenerationTimeoutError):
            self.call()
        self.assertEqual(self.clients, [])

    def test_rate_limit_has_visible_retry_and_no_sdk_retries(self):
        attempts = []

        def handler(request):
            attempts.append(True)
            if len(attempts) == 1:
                return httpx.Response(429, json={"error": {"message": "test limit"}})
            return self.response()

        self.mock_transport(handler)
        self.assertEqual(self.call().choices[0].message.content, "ok")
        self.assertEqual(len(attempts), 2)
        self.assertTrue(any("HTTP 429" in line for line in self.messages))

    def test_authentication_failure_uses_backup_key(self):
        attempts = []

        def handler(request):
            attempts.append(True)
            if len(attempts) == 1:
                return httpx.Response(401, json={"error": {"message": "test auth"}})
            return self.response()

        self.mock_transport(handler)
        with patch.object(cards, "OPENAI_KEYS", ["test-key-a", "test-key-b"]):
            self.call()
        self.assertEqual([options["api_key"] for options in self.client_options],
                         ["test-key-a", "test-key-b"])

    def test_authentication_failure_without_backup_does_not_retry(self):
        self.mock_transport(lambda request: httpx.Response(
            401, json={"error": {"message": "test auth"}}
        ))
        with self.assertRaises(AuthenticationError):
            self.call()
        self.assertEqual(len(self.clients), 1)

    def test_bad_request_does_not_retry(self):
        self.mock_transport(lambda request: httpx.Response(
            400, json={"error": {"message": "test bad request"}}
        ))
        with self.assertRaises(BadRequestError):
            self.call()
        self.assertEqual(len(self.clients), 1)

    def test_cancellation_closes_client_and_stops_heartbeat(self):
        cancelled = []

        async def handler(request):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(True)

        self.mock_transport(handler)

        async def run():
            task = asyncio.create_task(cards._call_openai_async([], "測試取消", {"model": "test"}))
            await asyncio.sleep(0.03)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            log_count = len(self.messages)
            await asyncio.sleep(0.03)
            self.assertEqual(len(self.messages), log_count)

        asyncio.run(run())
        self.assertEqual(cancelled, [True])
        self.assertTrue(self.clients[0].is_closed)

    def test_global_timeout_is_not_swallowed_by_planning_review_or_youtube(self):
        error = cards.GenerationTimeoutError("test deadline")
        with patch.object(cards, "_call_openai", side_effect=error) as call:
            for function, arguments in (
                (cards._request_pain_point_candidates, ("test", {"pain_categories": ["test"]}, 5, "")),
                (cards._plan_pain_points, ("test", 5)),
                (cards.generate, ("test", 1)),
                (cards._ai_review_deck, ("test", [_item("Test", "Test now.")])),
                (cards._generate_yt_title, ("test",)),
                (cards._generate_yt_topic_paragraph, ("test",)),
                (cards._generate_yt_hashtags, ("test",)),
            ):
                with self.subTest(function=function.__name__):
                    call.reset_mock()
                    with self.assertRaises(cards.GenerationTimeoutError):
                        function(*arguments)
                    self.assertEqual(call.call_count, 1)

    def test_main_restores_deadline_even_on_failure(self):
        original = cards._generation_deadline.get()

        def fail(argv):
            cards._generation_deadline.set(time.monotonic() + 10)
            raise cards.GenerationTimeoutError("test deadline")

        with patch.object(cards, "_main", side_effect=fail):
            with self.assertRaises(cards.GenerationTimeoutError):
                cards.main([])
        self.assertEqual(cards._generation_deadline.get(), original)

    def test_progress_flushes_output_immediately(self):
        with patch("builtins.print") as output:
            self.real_progress("test")
        self.assertTrue(output.call_args.kwargs["flush"])

    def test_main_reports_ordered_stages_and_completion(self):
        for existing, plan_only, no_youtube in (
            (False, False, False),
            (False, False, True),
            (True, False, False),
            (False, True, False),
        ):
            with self.subTest(existing=existing, plan_only=plan_only, no_youtube=no_youtube):
                self.messages.clear()
                with tempfile.TemporaryDirectory() as directory:
                    output_path = Path(directory) / "test.xlsx"
                    plan = cards.PainPointPlan([{"task": "Test now."}])
                    items = [_item("Test", "Test now.")]
                    if existing:
                        output_path.touch()
                    with (
                        patch.object(cards, "_existing_topics", return_value=[]),
                        patch.object(cards, "_load_reference_decks", return_value=([], [])),
                        patch.object(cards, "_plan_pain_points", return_value=plan),
                        patch.object(cards, "_save_pain_point_plan"),
                        patch.object(cards, "_load_used_words", return_value=set()),
                        patch.object(cards, "load_xlsx_items", return_value=items),
                        patch.object(cards, "_review_deck", return_value={}),
                        patch.object(cards, "generate", return_value=items) as generate,
                        patch.object(cards, "write_xlsx") as write,
                        patch.object(cards, "write_youtube_description") as youtube,
                    ):
                        arguments = ["--legacy", "--topic", "test", "--count", "1", "--output", str(output_path)]
                        if plan_only:
                            arguments.append("--plan-only")
                        if no_youtube:
                            arguments.append("--no-youtube")
                        cards.main(arguments)

                    self.assertIn("階段 1/3：", self.messages[1])
                    self.assertIn("流程已完成", self.messages[-1])
                    if plan_only:
                        generate.assert_not_called()
                        write.assert_not_called()
                        youtube.assert_not_called()
                        self.assertFalse(any("階段 2/3" in line for line in self.messages))
                    else:
                        stages = [next(i for i, line in enumerate(self.messages) if text in line)
                                  for text in ("階段 1/3 完成", "階段 2/3：", "正在寫入 XLSX",
                                               "階段 2/3 完成", "階段 3/3：", "流程已完成")]
                        self.assertEqual(stages, sorted(stages))
                        write.assert_called_once_with(items, str(output_path))
                        self.assertEqual(youtube.call_count, 0 if no_youtube else 1)

    def test_cli_timeout_and_interrupt_print_status_close_clients_and_exit(self):
        script = textwrap.dedent('''
            import asyncio, atexit, httpx, openai, os, runpy, signal, sys
            real_client = openai.AsyncOpenAI
            clients = []
            mode = sys.argv[2]
            async def handler(request):
                try:
                    if mode == "interrupt":
                        asyncio.get_running_loop().call_later(0.05, os.kill, os.getpid(), signal.SIGINT)
                    await asyncio.Event().wait()
                finally:
                    print("TEST_REQUEST_CANCELLED", flush=True)
            def factory(**kwargs):
                client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
                clients.append(client)
                return real_client(**kwargs, http_client=client)
            openai.AsyncOpenAI = factory
            atexit.register(lambda: print("TEST_CLIENTS_CLOSED", all(c.is_closed for c in clients)))
            sys.argv = ["cards.py", "--legacy", "--topic", "runtime_timeout_test", "--count", "5",
                        "--output", sys.argv[1], "--no-youtube"]
            runpy.run_path("cards.py", run_name="__main__")
        ''')
        environment = dict(os.environ, OPENAI_API_KEY="test-only-key", OPENAI_API_KEY_2="",
                           CARD_GENERATION_TIMEOUT="0.2", CARD_API_REQUEST_TIMEOUT="2",
                           CARD_API_CALL_TIMEOUT="4", CARD_PROGRESS_INTERVAL="0.02")
        for mode, code, status in (("timeout", 1, "逾時停止"), ("interrupt", 130, "已取消")):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                output = str(Path(directory) / "test.xlsx")
                result = subprocess.run(
                    [sys.executable, "-c", script, output, mode], env=environment,
                    cwd=cards.BASE_DIR, capture_output=True, text=True, timeout=10,
                )
                self.assertFalse(Path(output).exists())
                self.assertFalse(Path(output).with_suffix(".plan.json").exists())
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                self.assertIn("主題範圍策劃：開始請求", result.stdout)
                self.assertIn("等待 API 回應", result.stdout)
                self.assertIn(status, result.stdout)
                self.assertIn("TEST_REQUEST_CANCELLED", result.stdout)
                self.assertIn("TEST_CLIENTS_CLOSED True", result.stdout)
                self.assertNotIn("Traceback", result.stderr)

    def test_invalid_time_limits_are_rejected(self):
        for value in ("0", "-1", "nan", "inf", "not-a-number"):
            with self.subTest(value=value), patch.dict(os.environ, CARD_PROGRESS_INTERVAL=value):
                with self.assertRaises(ValueError):
                    cards._positive_seconds("CARD_PROGRESS_INTERVAL", 15)


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


class SentenceDiversityTests(unittest.TestCase):
    def test_single_noun_substitution_is_rejected_by_local_review(self):
        items = [
            _item("Change the bread.", "Could I change the bread?"),
            _item("Different sauce.", "Could I change the sauce?"),
        ]
        rejected = cards._local_review_deck("更換餐點", items)
        self.assertEqual(set(rejected), {1})
        self.assertIn("句型過度相似", rejected[1])
        self.assertIn("保留原本場景", rejected[1])

    def test_natural_rephrasing_is_allowed(self):
        first = _item("Change the bread.", "Could I change the bread?")
        second = _item("Different sauce.", "I'd prefer a different sauce.")
        self.assertIsNone(cards._sentence_pattern_issue(second, [first]))

    def test_negation_quantity_modality_and_short_phrases_are_not_template_swaps(self):
        pairs = [
            ("Could I change the bread?", "Could I change the bread without cheese?"),
            ("Please use some sauce on this.", "Please use no sauce on this."),
            ("Could I have two extra towels?", "Could I have three extra towels?"),
            ("Could I change the bread?", "Should I change the bread?"),
            ("Please add extra sauce to this.", "Please add less sauce to this."),
            ("I'd like that grilled.", "I'd like that toasted."),
        ]
        for left, right in pairs:
            with self.subTest(left=left, right=right):
                self.assertIsNone(cards._sentence_pattern_issue(
                    _item("Second", right), [_item("First", left)]
                ))

    def test_distinct_roles_are_not_template_swaps(self):
        left = _item("First", "Could I change the bread?")
        right = _item("Second", "Could I change the sauce?")
        left["_pain_point"] = dict(_pain_point("更換麵包", "餐點", 1), role_type="learner_line")
        right["_pain_point"] = dict(_pain_point("詢問醬料", "餐點", 2), role_type="counterpart_line")
        self.assertIsNone(cards._sentence_pattern_issue(right, [left]))

    def test_sentence_pattern_rejection_is_not_ignored_for_planned_decks(self):
        items = [
            _item("Change the bread.", "Could I change the bread?", 1),
            _item("Different sauce.", "Could I change the sauce?", 2),
        ]
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=json.dumps({"reject": [{
                "id": "02", "kind": "sentence_pattern", "reason": "與 01 句型重複，改用不同結構",
            }], "groups": [
                {"purpose": "更換麵包", "ids": ["01"], "basic_id": "01", "advanced_id": None},
                {"purpose": "更換醬料", "ids": ["02"], "basic_id": "02", "advanced_id": None},
            ]}, ensure_ascii=False)
        ))])
        with patch.object(cards, "_call_openai", return_value=response) as call:
            rejected = cards._ai_review_deck("更換餐點", items, [
                _pain_point("更換麵包", "餐點", 1), _pain_point("更換醬料", "餐點", 2),
            ])
        self.assertIn(1, rejected)
        self.assertIn("kind 填 sentence_pattern", call.call_args_list[0].kwargs["messages"][0]["content"])

    def test_ai_mode_still_runs_deterministic_sentence_pattern_check(self):
        items = [
            _item("Change the bread.", "Could I change the bread?"),
            _item("Different sauce.", "Could I change the sauce?"),
        ]
        with patch.object(cards, "REVIEW_MODE", "ai"), patch.object(cards, "_ai_review_deck") as ai_review:
            rejected = cards._review_deck("更換餐點", items)
        self.assertIn(1, rejected)
        ai_review.assert_not_called()

    def test_generate_refills_with_different_structure_and_preserves_purpose(self):
        points = [_pain_point("更換麵包", "餐點", 1), _pain_point("更換醬料", "餐點", 2)]
        first = dict(_item("Change the bread.", "Could I change the bread?", 1), purpose_id=1)
        similar = dict(_item("Different sauce.", "Could I change the sauce?", 2), purpose_id=2)
        rewritten = dict(_item("I'd prefer a different sauce.", "I'd prefer a different sauce.", 2), purpose_id=2)
        responses = [
            SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content=json.dumps({"items": batch}, ensure_ascii=False)
            ))])
            for batch in ([first, similar], [rewritten])
        ]
        with (
            patch.object(cards, "REVIEW_MODE", "local"),
            patch.object(cards, "_call_openai", side_effect=responses) as call,
            patch.object(cards, "_load_used_words", return_value=set()),
            patch.object(cards, "_save_used_words"),
        ):
            result = cards.generate("更換餐點", 2, pain_points=points)
        self.assertEqual(call.call_count, 2)
        self.assertEqual(result[0]["sentence_en"], first["sentence_en"])
        self.assertEqual(result[1]["sentence_en"], rewritten["sentence_en"])
        self.assertEqual(result[1]["_purpose_id"], 2)
        prompt = call.call_args.kwargs["messages"][0]["content"]
        self.assertIn("SENTENCE DIVERSITY", prompt)
        self.assertIn("句型過度相似", prompt)

    def test_locked_sentence_conflict_fails_without_silent_rephrasing(self):
        items = [
            _item("Change the bread.", "Could I change the bread?", 1),
            _item("Different sauce.", "Could I change the sauce?", 2),
        ]
        items[0].update(word_ipa="/tʃeɪndʒ ðə brɛd/", sentence_ipa="/kʊd aɪ tʃeɪndʒ ðə brɛd/")
        items[1].update(word_ipa="/ˈdɪfərənt sɔs/", sentence_ipa="/kʊd aɪ tʃeɪndʒ ðə sɔs/")
        points = []
        for i, item in enumerate(items):
            point = _pain_point(("更換麵包", "更換醬料")[i], f"餐點{i}", i + 1)
            point.update(job_key=f"change{i}", target_phrase=item["word_en"], target_sentence=item["sentence_en"])
            points.append(point)
        with (
            patch.object(cards, "REVIEW_MODE", "hybrid"),
            patch.object(cards, "_call_openai") as call,
            patch.object(cards, "_load_used_words", return_value=set()),
            patch.object(cards, "_save_used_words") as save,
        ):
            with self.assertRaisesRegex(RuntimeError, "需先調整策劃中的鎖定原話"):
                cards.generate("更換餐點", 2, seed_items=items, pain_points=points)
        call.assert_not_called()
        save.assert_not_called()

    def test_excel_write_blocks_single_slot_templates(self):
        items = [
            dict(_item("Change the bread.", "Could I change the bread?"), id="01"),
            dict(_item("Different sauce.", "Could I change the sauce?"), id="02"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.xlsx"
            with self.assertRaisesRegex(ValueError, "句型過度相似"):
                cards.write_xlsx(items, str(path))
            self.assertFalse(path.exists())


class ContentGateTests(unittest.TestCase):
    def test_pain_point_required_terms_keep_only_english_constraints(self):
        point = _pain_point("我想補充數據", "插話困難", 1)
        point["required_terms"] = ["補充", "數據", "data", "support"]

        normalized = cards._normalize_pain_point(point)

        self.assertEqual(normalized["required_terms"], ["data", "support"])

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
            "cards.terminal_prompt",
            return_value="  電梯與派對的社交脫身\n\n排除商務會議  ",
        ):
            result = cards._prompt_topic_description()

        self.assertEqual(result, "電梯與派對的社交脫身\n\n排除商務會議")

    def test_interactive_topic_description_first_blank_still_skips(self):
        with patch("cards.terminal_prompt", return_value=""):
            result = cards._prompt_topic_description()

        self.assertEqual(result, "")

    def test_interactive_topic_description_treats_done_as_regular_text(self):
        with patch("cards.terminal_prompt", return_value="第一段\n:DONE"):
            result = cards._prompt_topic_description()

        self.assertEqual(result, "第一段\n:DONE")

    def test_interactive_topic_description_empty_eof_skips(self):
        with patch("cards.terminal_prompt", side_effect=EOFError):
            result = cards._prompt_topic_description()

        self.assertEqual(result, "")

    def test_interactive_description_shows_waiting_state_without_done_instructions(self):
        with (
            patch("cards.terminal_prompt", return_value="第一段\n\n第二段") as prompt,
            patch("builtins.print") as output,
            patch.object(cards, "_progress") as progress,
            patch.object(cards, "_call_openai") as api,
        ):
            result = cards._prompt_topic_description()

        self.assertEqual(result, "第一段\n\n第二段")
        prompt.assert_called_once()
        messages = "\n".join(call.args[0] for call in output.call_args_list)
        self.assertIn("按 Enter 送出", messages)
        self.assertNotIn(":done", messages)
        statuses = [call.args[0] for call in progress.call_args_list]
        self.assertIn("尚未開始 AI 生成", statuses[0])
        self.assertIn("描述輸入已完成（8 字）", statuses[-1])
        api.assert_not_called()

    def test_interactive_description_does_not_truncate_long_input(self):
        paragraphs = ["長描述內容" * 1000, "Do not proceed without my approval. " * 200]
        with (
            patch("cards.terminal_prompt", return_value="\n".join(paragraphs)),
            patch.object(cards, "_progress"),
        ):
            result = cards._prompt_topic_description()

        self.assertEqual(result, "\n".join(paragraphs).strip())

    def test_terminal_description_paste_needs_only_one_enter(self):
        descriptions = ["單行描述", "第一段\n\n\n第二段\n", "長描述內容" * 3000]
        for description in descriptions:
            with self.subTest(length=len(description)), create_pipe_input() as pipe:
                def prompt_with_pipe(*args, **kwargs):
                    return PromptSession(input=pipe, output=DummyOutput()).prompt(*args, **kwargs)

                pipe.send_text("\x1b[200~" + description + "\x1b[201~\r")
                with patch("cards.terminal_prompt", side_effect=prompt_with_pipe):
                    result = cards._prompt_topic_description()

                self.assertEqual(result, description.strip())

    def test_terminal_description_manual_newline_and_empty_submit(self):
        for keys, expected in (("\r", ""), ("第一段\x1b\r第二段\r", "第一段\n第二段")):
            with self.subTest(keys=keys), create_pipe_input() as pipe:
                def prompt_with_pipe(*args, **kwargs):
                    return PromptSession(input=pipe, output=DummyOutput()).prompt(*args, **kwargs)

                pipe.send_text(keys)
                with patch("cards.terminal_prompt", side_effect=prompt_with_pipe):
                    result = cards._prompt_topic_description()

                self.assertEqual(result, expected)

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

    def test_youtube_generators_receive_the_specific_content_context(self):
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="測試輸出"))]
        )
        context = "海外租屋時向房東報修，並交涉不合理的押金扣款"

        with patch.object(cards, "_call_openai", return_value=response) as call:
            cards._generate_yt_title("捍衛權益", context)

        prompt = call.call_args.kwargs["messages"][0]["content"]
        self.assertIn(context, prompt)
        self.assertIn("不得擴寫成泛用或相鄰主題", prompt)

    def test_youtube_context_falls_back_to_the_saved_plan_contract(self):
        points = cards.PainPointPlan(
            [],
            contract={
                "audience": "在海外租屋的亞洲租客",
                "core_pain": "房東不修繕或不合理扣押金",
                "promised_transformation": "能用英文報修與交涉",
                "in_scope": ["向房東報修", "押金爭議"],
                "out_of_scope": ["泛用人權議題"],
                "required_moments": ["暖氣故障", "退租扣押金"],
                "pain_categories": ["報修", "押金"],
                "learner_only": False,
            },
        )

        context = cards._youtube_content_context("", points)

        self.assertIn("在海外租屋的亞洲租客", context)
        self.assertIn("房東不修繕或不合理扣押金", context)

    def test_youtube_description_includes_placeholder_progress_without_srt(self):
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "youtube_測試.txt"
            with (
                patch.object(cards, "OUTPUT_DIR", directory),
                patch.object(cards, "_generate_yt_title", return_value="測試標題"),
                patch.object(cards, "_generate_yt_topic_paragraph", return_value="測試文案"),
                patch.object(cards, "_generate_yt_hashtags", return_value=["測試標籤"]),
            ):
                cards.write_youtube_description("測試", 50, str(output_path))

            description = output_path.read_text(encoding="utf-8")

        self.assertIn("00:00 開始學習！", description)
        self.assertIn("00:00 25%繼續加油！", description)
        self.assertIn("00:00 50% 再複習一次  GO! GO!", description)
        self.assertIn("00:00 75% 最後衝刺！", description)
        self.assertNotIn("📑 完整章節", description)
        self.assertIn("測試標題", description)
        self.assertIn("測試文案", description)

    def test_youtube_description_uses_existing_srt_times(self):
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "youtube_Karen.txt"
            srt_path = Path(directory) / "final_karen.srt"
            srt_path.write_text(
                "\n\n".join(
                    f"{i + 1}\n00:{i:02d}:13,130 --> 00:{i:02d}:28,570\nTest"
                    for i in range(8)
                ),
                encoding="utf-8",
            )
            with (
                patch.object(cards, "OUTPUT_DIR", directory),
                patch.object(cards, "_generate_yt_title", return_value="測試標題"),
                patch.object(cards, "_generate_yt_topic_paragraph", return_value="測試文案"),
                patch.object(cards, "_generate_yt_hashtags", return_value=[]),
            ):
                cards.write_youtube_description("Karen", 4, str(output_path))
            description = output_path.read_text(encoding="utf-8")
        self.assertIn("00:00 開始學習！", description)
        self.assertIn("02:13 25%繼續加油！", description)
        self.assertIn("04:13 50% 再複習一次  GO! GO!", description)
        self.assertIn("06:13 75% 最後衝刺！", description)

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
        retry_prompt = call.call_args.kwargs["messages"][0]["content"]
        self.assertIn("previous_error", retry_prompt)
        self.assertIn("詞數", retry_prompt)

    def test_locked_ipa_rejects_observed_errors_but_accepts_dialect_variants(self):
        cases = [
            ("A quote.", "/ə kwoʊt/", "/ə koʊt/"),
            ("A quote.", "/ə kwəʊt/", "/ə kəʊt/"),
            ("A charge.", "/ə tʃɑrd/", "/ə tʃɑrdʒ/"),
            ("I was charged.", "/aɪ wəz tʃɑrd/", "/aɪ wəz tʃɑrdʒd/"),
            ("I was charged.", "/aɪ wəz tʃɑrdʒ/", "/aɪ wəz tʃɑrdʒd/"),
            ("Refund it.", "/ˈriːfʌnd ɪt/", "/rɪˈfʌnd ɪt/"),
            ("A cancellation.", "/ə ˌkænsləˈeɪʃən/", "/ə ˌkænsəˈleɪʃən/"),
            ("Unauthorized work.", "/ˌʌnəˈθɔraɪzd wɜrk/", "/ˌʌnˈɔθəraɪzd wɜrk/"),
        ]
        for english, bad, good in cases:
            with self.subTest(english=english, bad=bad):
                point = dict(_pain_point("測試", "測試", 1),
                             target_phrase=english, target_sentence=english)
                item = _item(english, english)
                item.update(word_ipa=bad, sentence_ipa=bad)
                self.assertIsNotNone(cards._locked_item_issue(item, point))
                item.update(word_ipa=good, sentence_ipa=good)
                self.assertIsNone(cards._locked_item_issue(item, point))

    def test_locked_ipa_does_not_apply_verb_stress_to_refund_noun(self):
        english = "The refund arrived."
        point = dict(_pain_point("退款進度", "退款", 1),
                     target_phrase=english, target_sentence=english)
        item = _item(english, english)
        item.update(word_ipa="/ðə ˈriːfʌnd əˈraɪvd/", sentence_ipa="/ðə ˈriːfʌnd əˈraɪvd/")
        self.assertIsNone(cards._locked_item_issue(item, point))

    def test_locked_translation_rejects_literal_store_credit(self):
        english = "Not as store credit."
        point = dict(_pain_point("退款方式", "退款", 1),
                     target_phrase=english, target_sentence=english)
        item = _item(english, english)
        item.update(word_ipa="/nɑt æz stɔr ˈkrɛdɪt/", sentence_ipa="/nɑt æz stɔr ˈkrɛdɪt/",
                    sentence_cn="不是商店信用。")
        self.assertIn("店內購物金", cards._locked_item_issue(item, point))
        item["sentence_cn"] = "不要退成店內購物金。"
        self.assertIsNone(cards._locked_item_issue(item, point))

    def test_known_ipa_correction_preserves_punctuation_and_refund_noun(self):
        english = "Refund the charge, not the quote."
        point = dict(_pain_point("測試", "測試", 1),
                     target_phrase=english, target_sentence=english)
        item = _item(english, english)
        item.update(word_ipa="/ˈriːfʌnd ðə tʃɑrd, nɑt ðə kwoʊt./",
                    sentence_ipa="/ˈriːfʌnd ðə tʃɑrd, nɑt ðə kwoʊt./")
        corrected = cards._correct_known_locked_pronunciations(item, point)
        self.assertEqual(corrected["word_ipa"], "/rɪˈfʌnd ðə tʃɑrdʒ, nɑt ðə koʊt./")
        self.assertNotEqual(item["word_ipa"], corrected["word_ipa"])
        item["sentence_en"] = "The refund arrived."
        item["sentence_ipa"] = "/ðə ˈriːfʌnd əˈraɪvd/"
        point["target_sentence"] = item["sentence_en"]
        corrected = cards._correct_known_locked_pronunciations(item, point)
        self.assertEqual(corrected["sentence_ipa"], item["sentence_ipa"])
        item["sentence_en"] = "I authorized it."
        item["sentence_ipa"] = "/aɪ ɔˈθɔraɪzd ɪt/"
        point["target_sentence"] = item["sentence_en"]
        corrected = cards._correct_known_locked_pronunciations(item, point)
        self.assertEqual(corrected["sentence_ipa"], "/aɪ ˈɔθəraɪzd ɪt/")

    def test_known_ipa_correction_does_not_hide_stale_source_or_alignment(self):
        english = "A quote."
        point = dict(_pain_point("報價", "報價", 1),
                     target_phrase=english, target_sentence=english)
        for source, ipa in (("The charge.", "/ðə tʃɑrd/"), (english, "/kwoʊt/")):
            item = _item(source, source)
            item.update(word_ipa=ipa, sentence_ipa=ipa)
            self.assertEqual(cards._correct_known_locked_pronunciations(item, point), item)
            self.assertIsNotNone(cards._locked_item_issue(item, point))

    def test_locked_blueprint_still_gets_semantic_review_in_ai_modes(self):
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
            ), patch.object(cards, "_ai_review_deck", return_value={}) as ai_review:
                rejected = cards._review_deck(
                    "Phone Call Phobia", [item], [point]
                )

            self.assertEqual(rejected, {})
            if review_mode == "local":
                ai_review.assert_not_called()
            else:
                ai_review.assert_called_once()

    def test_planned_deck_does_not_ignore_ai_duplicate_rejection(self):
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
                            "groups": [
                                {"purpose": "工作離場", "ids": ["01"], "basic_id": "01", "advanced_id": None},
                                {"purpose": "飲料離場", "ids": ["02"], "basic_id": "02", "advanced_id": None},
                            ],
                        })
                    )
                )
            ]
        )

        with patch.object(cards, "_call_openai", return_value=response) as call:
            rejected = cards._ai_review_deck("結束話題", items, points)

        self.assertIn(1, rejected)
        self.assertTrue(rejected[1].startswith("語意重複："))
        self.assertEqual(call.call_count, 2)


class SemanticDuplicateTests(unittest.TestCase):
    @staticmethod
    def response(reject, count=3, groups=None):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=json.dumps({"reject": reject, "groups": groups if groups is not None else [
                {"purpose": f"測試目的{i}", "ids": [str(i)], "basic_id": str(i), "advanced_id": None}
                for i in range(1, count + 1)
            ]}, ensure_ascii=False)
        ))])

    def quote_items(self):
        return [
            _item("I need a written quote.", "Please give me a written quote.", 1),
            _item("Could you put that in writing?", "Could you put the price and details in writing?", 2),
            _item("Send me a written estimate.", "Can you send me an estimate for this service?", 3),
        ]

    def test_planned_deck_always_runs_full_semantic_review(self):
        items = self.quote_items()
        points = [
            _pain_point("索取維修書面報價", "維修", 1),
            _pain_point("索取會員書面細節", "會員", 2),
            _pain_point("要求服務 estimate", "服務", 3),
        ]
        with patch.object(cards, "_call_openai", side_effect=[
            self.response([]),
            self.response([], groups=[{"purpose": "取得書面報價", "ids": ["01", "02", "03"],
                                      "basic_id": "01", "advanced_id": "02", "pair_reason": "02使用片語put in writing"}]),
        ]) as call:
            rejected = cards._ai_review_deck("推銷與隱形敲詐", items, points)

        self.assertEqual(set(rejected), {2})
        self.assertIn("語意重複", rejected[2])
        self.assertEqual(call.call_count, 2)
        prompt = call.call_args.kwargs["messages"][0]["content"]
        self.assertIn("先建立跨分類、跨 purpose_id 的語意群組", prompt)
        self.assertIn("每組第三張起一律退回", prompt)
        self.assertIn("written estimate", prompt)

    def test_simple_advanced_pair_can_pass_without_a_blueprint(self):
        with patch.object(cards, "_call_openai", return_value=self.response([], count=2, groups=[
            {"purpose": "取得報價", "ids": ["01", "02"], "basic_id": "01", "advanced_id": "02", "pair_reason": "02使用片語"},
        ])) as call:
            rejected = cards._ai_review_deck("推銷", self.quote_items()[:2])
        self.assertEqual(rejected, {})
        self.assertEqual(call.call_count, 2)
        self.assertIn("沒有明顯難度差異則只保留一張", call.call_args.kwargs["messages"][0]["content"])

    def test_cross_deck_paraphrases_are_still_excluded(self):
        reference = dict(self.quote_items()[0], _source_deck="上一集")
        with patch.object(cards, "_call_openai", side_effect=[
            self.response([], count=1), self.response([{"id": "01", "reason": "與上一集書面報價重複"}], count=1),
        ]) as call:
            rejected = cards._ai_review_deck("推銷_02", [self.quote_items()[1]], reference_items=[reference])
        self.assertIn(0, rejected)
        self.assertIn("兩句上限只適用於當前牌組", call.call_args.kwargs["messages"][0]["content"])

    def test_invalid_semantic_review_response_fails_closed(self):
        invalid = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=json.dumps({"items": []})
        ))])
        with patch.object(cards, "_call_openai", side_effect=[self.response([]), invalid, invalid, invalid]):
            with self.assertRaisesRegex(RuntimeError, "語意去重審稿必須回傳 reject 陣列"):
                cards._ai_review_deck("推銷", self.quote_items())

    def test_duplicate_blueprint_is_replaced_before_refilling(self):
        points = [_pain_point("索取書面報價", "報價", 1)]
        item = self.quote_items()[0]
        replacement = _pain_point("限定本次授權金額", "報價", 1)
        new_item = dict(_item("My limit is fifty dollars.", "Do not spend more than fifty dollars.", 1), purpose_id=1)

        def repair(topic, plan, items, rejected, references):
            plan[0] = replacement

        with (
            patch.object(cards, "_review_deck", side_effect=[{0: "語意重複：要求書面報價"}, {}]),
            patch.object(cards, "_replace_duplicate_pain_points", side_effect=repair) as replace,
            patch.object(cards, "_load_used_words", return_value=set()),
            patch.object(cards, "_save_used_words") as save,
            patch.object(cards, "_call_openai", return_value=SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=json.dumps({"items": [new_item]}, ensure_ascii=False))
            )])) as call,
        ):
            result = cards.generate("推銷", 1, seed_items=[item], pain_points=points)
        replace.assert_called_once()
        call.assert_called_once()
        save.assert_called_once()
        self.assertEqual(result[0]["_pain_point"]["task"], replacement["task"])
        self.assertEqual(result[0]["word_en"], new_item["word_en"])

    def test_invalid_semantic_rejection_ids_fail_closed(self):
        for rejected_id in (None, "bad", "00", "04"):
            with self.subTest(rejected_id=rejected_id), patch.object(cards, "_call_openai", side_effect=[
                self.response([]), self.response([{"id": rejected_id, "reason": "重複"}]),
            ]):
                with self.assertRaisesRegex(RuntimeError, "語意去重退回"):
                    cards._ai_review_deck("推銷", self.quote_items())

    def test_generation_prompt_uses_meaning_not_ids_or_document_names(self):
        prompt = cards._build_prompt("推銷與隱形敲詐", 50)
        self.assertIn(cards.SEMANTIC_DUPLICATE_POLICY, prompt)
        self.assertIn("同一溝通目的最多兩種說法", prompt)
        self.assertIn("不同 purpose_id、分類、場所、商品、文件名稱", prompt)
        self.assertIn("不得用同義改寫、換商品或新增理由填補句數", prompt)

    def test_group_cap_is_enforced_even_when_model_reject_list_is_empty(self):
        payload = {"reject": [], "groups": [{"purpose": "取得書面報價", "ids": ["01", "02", "03"],
                    "basic_id": "01", "advanced_id": "02", "pair_reason": "片語進階"}]}
        rejected = cards._semantic_group_rejections(payload, 3)
        self.assertEqual(set(rejected), {2})
        self.assertIn("同組 01,02,03", rejected[2])

    def test_group_partition_must_be_complete_and_unambiguous(self):
        one = {"purpose": "報價", "ids": ["01"], "basic_id": "01", "advanced_id": None}
        cases = [
            {}, {"groups": []}, {"groups": [one]},
            {"groups": [one, dict(one, purpose="其他")]},
            {"groups": [one, dict(one, ids=["02"], basic_id="02")]},
            {"groups": [dict(one, ids=["01", "02"], advanced_id="02")]},
            {"groups": [dict(one, ids=["01", "02"], basic_id="03")]},
        ]
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                cards._semantic_group_rejections(payload, 2)

    def test_repair_keeps_good_jobs_and_replaces_only_duplicate_job(self):
        points = [_pain_point("索取書面報價", "報價", 1), _pain_point("再次索取報價", "報價", 2)]
        good = dict(points[0])
        new = dict(_pain_point("限制最高授權金額", "報價", 2), id=2)
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=json.dumps({"replacements": [new]}, ensure_ascii=False)
        ))])
        with patch.object(cards, "_call_openai", return_value=response):
            cards._replace_duplicate_pain_points("推銷", points, self.quote_items()[:2], {1: "語意重複：報價"})
        self.assertEqual(points[0], good)
        self.assertEqual(points[1]["task"], new["task"])

    def test_repair_does_not_silently_change_locked_english(self):
        point = _pain_point("索取報價", "報價", 1)
        point.update(job_key="quote", target_phrase="I need a quote.", target_sentence="I need a written quote.")
        with patch.object(cards, "_call_openai") as call, self.assertRaisesRegex(RuntimeError, "人工策劃"):
            cards._replace_duplicate_pain_points("推銷", [point], self.quote_items()[:1], {0: "語意重複：報價"})
        call.assert_not_called()

    def test_incomplete_group_response_is_corrected_before_passing(self):
        items = self.quote_items()
        with patch.object(cards, "_call_openai", side_effect=[
            self.response([]), self.response([], count=2), self.response([]),
        ]) as call:
            self.assertEqual(cards._ai_review_deck("測試", items), {})
        self.assertEqual(call.call_count, 3)
        prompt = call.call_args.kwargs["messages"][0]["content"]
        self.assertIn("漏列編號 [3]", prompt)
        self.assertIn("不能把缺漏卡片隨意獨立成組", prompt)

    def test_resume_preserves_missing_purpose_ids_and_completed_jobs(self):
        points = [_pain_point("控制授權金額", "金額", 1), _pain_point("保留更換零件", "證據", 2)]
        second = _item("Keep the old part.", "Please keep the old part for me.", 2)
        first = dict(_item("My limit is fifty dollars.", "Do not spend more than fifty dollars.", 1), purpose_id=1)
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "deck.draft.json"
            checkpoint.write_text(json.dumps({
                "version": cards.PLAN_VERSION, "topic": "測試", "count": 2,
                "pain_points": points, "contract": {}, "items": [second],
            }, ensure_ascii=False), encoding="utf-8")
            with (
                patch.object(cards, "_call_openai", return_value=SimpleNamespace(choices=[SimpleNamespace(
                    message=SimpleNamespace(content=json.dumps({"items": [first]}))
                )])) as call,
                patch.object(cards, "_review_deck", return_value={}),
                patch.object(cards, "_load_used_words", return_value=set()),
                patch.object(cards, "_save_used_words"),
            ):
                result = cards.generate("測試", 2, pain_points=points, checkpoint_path=str(checkpoint), resume=True)
            self.assertEqual([item["_purpose_id"] for item in result], [1, 2])
            self.assertEqual(result[1]["_pain_point"]["task"], points[1]["task"])
            self.assertEqual(result[1]["word_en"], second["word_en"])
            self.assertEqual(len(json.loads(checkpoint.read_text())["items"]), 2)
            call.assert_called_once()

    def test_per_card_assignments_merge_synonymous_jobs_before_enforcing_cap(self):
        payload = {"assignments": [
            {"id": "01", "purpose": "取得書面報價", "level": "basic", "progression": ""},
            {"id": "02", "purpose": "取得書面報價", "level": "advanced", "progression": "進階片語"},
            {"id": "03", "purpose": "取得書面報價", "level": "basic", "progression": ""},
        ]}
        self.assertEqual(set(cards._semantic_group_rejections(payload, 3)), {2})

    def test_hybrid_does_not_skip_semantic_review_due_to_sentence_patterns(self):
        with (
            patch.object(cards, "REVIEW_MODE", "hybrid"),
            patch.object(cards, "_local_review_deck", return_value={1: "句型過度相似"}),
            patch.object(cards, "_ai_review_deck", return_value={2: "語意重複：報價"}) as ai_review,
        ):
            result = cards._review_deck("推銷", self.quote_items())
        self.assertEqual(set(result), {1, 2})
        ai_review.assert_called_once()

    def test_sales_blueprint_has_fifty_distinct_jobs_and_only_two_quote_variants(self):
        contract, points = get_curated_blueprint("推銷與隱形敲詐", 50)
        plan = cards.PainPointPlan(points, contract=contract)
        self.assertEqual(cards._plan_quality_issues(plan, 50), [])
        self.assertTrue(contract["learner_only"])
        self.assertEqual(len({point["job_key"] for point in points}), 50)
        items = [{"word_en": point["target_phrase"], "sentence_en": point["target_sentence"]} for point in points]
        quotes = [index + 1 for index, item in enumerate(items) if cards._sales_communication_intent(item) == "取得書面報價與費用明細"]
        self.assertEqual(quotes, [17, 18])
        self.assertTrue(all(cards._sentence_pattern_issue(item, items[:index]) is None for index, item in enumerate(items)))

    def test_sales_rules_do_not_conflate_billing_conditions_with_generic_extra_fees(self):
        generic = _item("Are there any extra charges?", "Are there any additional costs?")
        self.assertEqual(cards._sales_communication_intent(generic), "確認是否存在額外費用")
        for sentence in (
            "Is the call-out fee payable if I decline the repair?",
            "Is the diagnostic fee waived if I approve the repair?",
            "This charge is higher than we agreed.",
        ):
            self.assertNotIn(cards._sales_communication_intent(_item(sentence, sentence)), ("確認是否存在額外費用", "了解不明收費原因"))


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

    def test_focus_must_teach_phrases_support_inline_scenario_format(self):
        topic = cards._generation_topic(
            "微歧視與文化刻板印象",
            '必教 4 大場景：\n'
            '1. 對方追問 "Where are you really from?"。'
            '必教 "Are you asking about my family’s heritage?"。\n'
            '2. 對方稱讚英文。必教 "Thank you! I use it every day for work." '
            '與界線較強的 “What makes you say that?”。',
        )

        self.assertEqual(
            cards._required_focus_phrases(topic),
            [
                "Are you asking about my family’s heritage?",
                "Thank you! I use it every day for work.",
                "What makes you say that?",
            ],
        )

    def test_focus_must_teach_phrases_ignore_chinese_translation_with_name(self):
        topic = cards._generation_topic(
            "插話藝術",
            "必教金句：\n"
            "Sorry to interrupt, but I’d like to add something here. "
            "(不好意思打斷一下，我想補充一點。)\n"
            "Building on what John just said... (延續 John 剛剛說的...)",
        )

        self.assertEqual(
            cards._required_focus_phrases(topic),
            [
                "Sorry to interrupt, but I’d like to add something here.",
                "Building on what John just said...",
            ],
        )

    def test_counterpart_quote_becomes_code_owned_english(self):
        point = _pain_point(
            '聽懂對方原話：“I think we should focus on the budget first.”',
            "會議參與感低",
            1,
        )
        point["role_type"] = "counterpart_line"

        exact = cards._exact_generation_point(point)

        self.assertEqual(
            exact["target_sentence"],
            "I think we should focus on the budget first.",
        )
        self.assertEqual(
            exact["target_phrase"],
            "Think we should focus on the budget first.",
        )
        self.assertLessEqual(
            cards._english_word_count(exact["target_phrase"]),
            cards.MAX_WORD_EN_WORDS,
        )

    def test_learner_point_without_lock_stays_in_general_generation(self):
        point = _pain_point("我想補充自己的觀點", "插話困難", 1)
        point["role_type"] = "learner_line"

        self.assertIsNone(cards._exact_generation_point(point))

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

    def test_refill_request_size_uses_deeper_pool_for_small_gaps(self):
        expected = {1: 6, 2: 12, 3: 18, 4: 24, 10: 30}
        for gap, request_size in expected.items():
            with self.subTest(gap=gap):
                self.assertEqual(
                    cards._generation_request_size(gap, True), request_size
                )
                self.assertEqual(cards._generation_request_size(gap, False), gap)

    def test_four_missing_cards_request_six_candidates_each(self):
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
            "Return exactly 24 alternative candidate items in the items array",
            prompts[1],
        )
        self.assertIn("Return 6 materially different candidates for EACH entry", prompts[1])

    def test_generation_skips_required_term_miss_and_keeps_later_candidate(self):
        point = _pain_point("我想告訴房東，應該退還我的押金。", "語言障礙", 1)
        point["required_terms"] = ["refund my deposit"]
        invalid = dict(
            _item(
                "I want my deposit back.",
                "I want my deposit back after moving out.",
            ),
            purpose_id=1,
        )
        valid = dict(
            _item(
                "Please refund my deposit.",
                "Please refund my deposit within the agreed timeframe.",
            ),
            purpose_id=1,
        )
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(
                content=json.dumps({"items": [invalid, valid]}, ensure_ascii=False)
            ))]
        )

        with (
            patch.object(cards, "_call_openai", return_value=response) as call,
            patch.object(cards, "_review_deck", return_value={}),
            patch.object(cards, "_load_used_words", return_value=set()),
            patch.object(cards, "_save_used_words"),
        ):
            result = cards.generate("捍衛權益", 1, pain_points=[point])

        self.assertEqual(result[0]["word_en"], "Please refund my deposit.")
        prompt = call.call_args.kwargs["messages"][0]["content"]
        self.assertIn("硬性英文關鍵詞=refund my deposit", prompt)

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
        generated["word_ipa"] = "/wʌt du ju du fɔr fʌn/"
        generated["sentence_ipa"] = "/wʌt du ju du fɔr fʌn/"
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
        self.assertIn("word_en 必須逐字等於 target_phrase", prompt)
        self.assertIn("sentence_en 必須逐字等於 target_sentence", prompt)

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
                "Let’s move on to the next point.",
                "Let’s move on to the next point.",
            ),
            purpose_id=1,
        )
        valid["word_ipa"] = "/lɛts muv ɑn tə ðə nɛkst pɔɪnt/"
        valid["sentence_ipa"] = "/lɛts muv ɑn tə ðə nɛkst pɔɪnt/"
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

        self.assertEqual(
            result[0]["word_en"],
            "Let’s move on to the next point.",
        )
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
