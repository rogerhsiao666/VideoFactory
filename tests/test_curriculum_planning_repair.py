import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import cards
import curriculum


def plan_for(count=50, scenarios=None):
    scenarios = scenarios or [f"roommate incident {index}" for index in range(curriculum.scenario_count_for(count))]
    jobs = [dict(slot, core="request specific result " + slot["id"],
                 task="Request specific roommate action " + slot["id"], role="learner", speaker="tenant")
            for slot in curriculum.slots_for(count, scenarios)]
    return dict(version=curriculum.VERSION, topic="Toxic roommate boundaries", count=count,
                scenarios=scenarios, jobs=jobs)


def audit_for(plan, ids):
    return {"assignments": [dict(id=job["id"], purpose=job["core"], in_scope=True, reason="")
                            for job in plan["jobs"] if job["id"] in ids]}


class PlanningDiversityRepairTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.checkpoint = Path(self.directory.name) / "planning.json"
        for variable in (cards._generation_deadline, curriculum._pair_review_store):
            token = variable.set(None)
            self.addCleanup(variable.reset, token)
        for name, value in (("BASE_DIR", self.directory.name),):
            mock = patch.object(cards, name, value)
            mock.start()
            self.addCleanup(mock.stop)
        mock = patch.object(cards, "_call_openai", side_effect=AssertionError("Unexpected paid API request"))
        mock.start()
        self.addCleanup(mock.stop)
        mock = patch.object(cards, "_progress")
        self.progress = mock.start()
        self.addCleanup(mock.stop)

    def respond(self, plan):
        def response(prompt, stage, model, **options):
            if stage == "主題情境策劃":
                self.assertIn(f"剛好 {len(plan['scenarios'])} 個", prompt)
                return {"scenarios": plan["scenarios"]}
            if stage == "逐句教材策劃":
                self.assertNotIn('"core":"實際目的"', prompt)
                return {"jobs": plan["jobs"]}
            if stage == "教材策劃獨立審查":
                return audit_for(plan, options["job_ids"])
            self.fail("Unexpected stage: " + stage)
        return response

    def test_dynamic_scenarios_keep_five_or_fewer_rows_and_sixty_forty(self):
        for count in range(3, 101):
            plan = plan_for(count)
            with self.subTest(count=count):
                curriculum.validate_plan(plan, plan["topic"], count)
                self.assertLessEqual(max(sum(job["Scenario"] == scene for job in plan["jobs"])
                                         for scene in plan["scenarios"]), 5)
                self.assertEqual(sum(job["tier"] == "basic" for job in plan["jobs"]),
                                 curriculum.basic_count(count))
        plan = plan_for()
        self.assertEqual(len(plan["scenarios"]), 10)
        for scene in plan["scenarios"]:
            self.assertEqual([job["tier"] for job in plan["jobs"] if job["Scenario"] == scene],
                             ["basic"] * 3 + ["advanced"] * 2)

    def test_fifty_card_planner_uses_ten_scenarios_and_batched_concrete_audits(self):
        plan = plan_for()
        with patch.object(curriculum, "request_json", side_effect=self.respond(plan)) as request:
            result = curriculum.plan_curriculum(plan["topic"], 50, [], checkpoint=self.checkpoint)
        self.assertEqual(len(result["scenarios"]), 10)
        self.assertEqual(len({job["task"] for job in result["jobs"]}), 50)
        audits = [call for call in request.call_args_list if call.args[1] == "教材策劃獨立審查"]
        self.assertEqual([len(call.kwargs["job_ids"]) for call in audits], [16, 16, 16, 2])
        self.assertEqual(len(json.loads(self.checkpoint.read_text())["complete_plan"]["jobs"]), 50)

    def test_new_fifty_card_plan_rejects_four_scenarios(self):
        plan = plan_for()
        responses = [{"scenarios": ["food", "trash", "noise", "talk"]}]
        def response(*args, **kwargs):
            return responses.pop(0) if responses else self.respond(plan)(*args, **kwargs)
        with patch.object(curriculum, "request_json", side_effect=response):
            result = curriculum.plan_curriculum(plan["topic"], 50, [], checkpoint=self.checkpoint)
        self.assertEqual(len(result["scenarios"]), 10)
        self.assertTrue(any("必須規劃 10 個情境" in call.args[0] for call in self.progress.call_args_list))

    def test_identical_tasks_are_repaired_before_any_ai_audit(self):
        plan = plan_for(10)
        plan["jobs"][1]["task"] = plan["jobs"][0]["task"]
        updated = copy.deepcopy(plan["jobs"])
        updated[1]["task"] = "Request reimbursement for the missing food"
        fixed = dict(plan, jobs=updated)
        audited = []
        def response(prompt, stage, model, **options):
            if stage == "教材策劃獨立審查":
                self.assertTrue(repair.called)
                audited.append(options["job_ids"])
                return audit_for(fixed, options["job_ids"])
            return self.respond(plan)(prompt, stage, model, **options)
        with patch.object(curriculum, "repair_duplicate_jobs", return_value=updated) as repair, \
                patch.object(curriculum, "request_json", side_effect=response):
            result = curriculum.plan_curriculum(plan["topic"], 10, [], checkpoint=self.checkpoint)
        self.assertEqual(repair.call_args.args[1], {"02"})
        self.assertEqual(len(audited), 1)
        self.assertEqual(result["jobs"][1]["task"], updated[1]["task"])

    def test_same_text_allows_one_basic_advanced_pair_but_rejects_third(self):
        plan = plan_for(10)
        basic = [job for job in plan["jobs"] if job["tier"] == "basic"]
        advanced = next(job for job in plan["jobs"] if job["tier"] == "advanced")
        advanced["task"] = basic[0]["task"]
        self.assertEqual(curriculum.repeated_task_rejections(plan), {})
        basic[1]["task"] = basic[0]["task"]
        self.assertEqual(set(curriculum.repeated_task_rejections(plan)), {basic[1]["id"]})

    def test_core_placeholder_is_rejected_at_contract_gate(self):
        plan = plan_for(10)
        for value in ("實際目的", "核心目的", "purpose", "placeholder"):
            plan["jobs"][0]["core"] = value
            with self.subTest(value=value), self.assertRaisesRegex(curriculum.PlanningTaskError, "佔位"):
                curriculum.validate_plan(plan, plan["topic"], 10)

    def test_placeholder_audit_retries_review_without_regenerating_good_tasks(self):
        plan = plan_for(10)
        audits = 0
        def response(prompt, stage, model, **options):
            nonlocal audits
            if stage == "教材策劃獨立審查":
                audits += 1
                result = audit_for(plan, options["job_ids"])
                if audits == 1:
                    for entry in result["assignments"]:
                        entry["purpose"] = "實際目的"
                return result
            return self.respond(plan)(prompt, stage, model, **options)
        with patch.object(curriculum, "request_json", side_effect=response) as request:
            result = curriculum.plan_curriculum(plan["topic"], 10, [], checkpoint=self.checkpoint)
        self.assertEqual(audits, 2)
        self.assertEqual(sum(call.args[1] == "逐句教材策劃" for call in request.call_args_list), 1)
        self.assertNotIn("實際目的", {job["core"] for job in result["jobs"]})

    def test_cached_placeholder_audit_is_not_reused(self):
        plan = plan_for(10)
        with patch.object(curriculum, "request_json", side_effect=self.respond(plan)):
            curriculum.plan_curriculum(plan["topic"], 10, [], checkpoint=self.checkpoint)
        saved = json.loads(self.checkpoint.read_text())
        saved.pop("complete_plan")
        for entry in saved["audit"]["assignments"]:
            entry["purpose"] = "實際目的"
        curriculum.save_json(self.checkpoint, saved)
        with patch.object(curriculum, "request_json", side_effect=self.respond(plan)) as request:
            curriculum.plan_curriculum(plan["topic"], 10, [], checkpoint=self.checkpoint, resume=True)
        self.assertEqual([call.args[1] for call in request.call_args_list], ["教材策劃獨立審查"])

    def test_legacy_failed_four_scenario_checkpoint_is_backed_up_and_rebuilt_without_force(self):
        plan = plan_for()
        old = plan_for(50, ["food", "trash", "noise", "talk"])
        identity = {"topic": plan["topic"], "count": 50, "version": curriculum.VERSION,
                    "review_version": curriculum.SEMANTIC_REVIEW_VERSION,
                    "checkpoint_version": curriculum.PLANNING_CHECKPOINT_VERSION,
                    "models": [curriculum.PLAN_MODEL, curriculum.REPAIR_MODEL, curriculum.REVIEW_MODEL],
                    "policies": [cards.SEMANTIC_DUPLICATE_POLICY, curriculum.PURPOSE_POLICY,
                                 curriculum.DIFFICULTY_POLICY, curriculum.SPOKEN_TASK_POLICY]}
        saved = dict(version=curriculum.PLANNING_CHECKPOINT_VERSION,
                     fingerprint=curriculum.fingerprint_for(identity, []), attempt=8, feedback="length",
                     scenarios=old["scenarios"], jobs=old["jobs"], audit=audit_for(old, [job["id"] for job in old["jobs"]]))
        curriculum.save_json(self.checkpoint, saved)
        original = self.checkpoint.read_bytes()
        with patch.object(curriculum, "request_json", side_effect=self.respond(plan)):
            result = curriculum.plan_curriculum(plan["topic"], 50, [], checkpoint=self.checkpoint, resume=True)
        self.assertEqual(len(result["scenarios"]), 10)
        backup = list((Path(self.directory.name) / ".cleanup-backups").glob("generation-*"))
        self.assertEqual(len(backup), 1)
        self.assertEqual((backup[0] / "00-planning.json").read_bytes(), original)
        self.assertEqual(json.loads(self.checkpoint.read_text())["attempt"], 0)

    def test_audit_truncation_splits_requests_without_content_rejections(self):
        plan = plan_for(10)
        sizes = []
        def response(prompt, stage, model, **options):
            if stage == "教材策劃獨立審查":
                sizes.append(len(options["job_ids"]))
                if len(options["job_ids"]) > 5:
                    raise curriculum.TruncatedResponseError("length")
            return self.respond(plan)(prompt, stage, model, **options)
        with patch.object(curriculum, "request_json", side_effect=response):
            curriculum.plan_curriculum(plan["topic"], 10, [], checkpoint=self.checkpoint)
        self.assertEqual(sizes, [10, 5, 5])
        self.assertEqual(json.loads(self.checkpoint.read_text())["attempt"], 0)

    def test_audit_completed_batches_survive_timeout(self):
        plan = plan_for()
        audits = 0
        def response(prompt, stage, model, **options):
            nonlocal audits
            if stage == "教材策劃獨立審查":
                audits += 1
                if audits == 2:
                    raise cards.GenerationTimeoutError("test timeout")
            return self.respond(plan)(prompt, stage, model, **options)
        with patch.object(curriculum, "request_json", side_effect=response), self.assertRaises(cards.GenerationTimeoutError):
            curriculum.plan_curriculum(plan["topic"], 50, [], checkpoint=self.checkpoint)
        saved = json.loads(self.checkpoint.read_text())
        self.assertEqual(len(saved["audit"]["assignments"]), 16)
        with patch.object(curriculum, "request_json", side_effect=self.respond(plan)) as request:
            curriculum.plan_curriculum(plan["topic"], 50, [], checkpoint=self.checkpoint)
        self.assertEqual([len(call.kwargs["job_ids"]) for call in request.call_args_list], [16, 16, 2])

    def test_audit_split_subbatch_is_saved_before_later_subbatch_timeout(self):
        plan = plan_for(10)
        def response(prompt, stage, model, **options):
            if stage == "教材策劃獨立審查":
                ids = options["job_ids"]
                if len(ids) == 10:
                    raise curriculum.TruncatedResponseError("length")
                if ids[0] == "06":
                    raise cards.GenerationTimeoutError("test timeout")
            return self.respond(plan)(prompt, stage, model, **options)
        with patch.object(curriculum, "request_json", side_effect=response), self.assertRaises(cards.GenerationTimeoutError):
            curriculum.plan_curriculum(plan["topic"], 10, [], checkpoint=self.checkpoint)
        self.assertEqual(len(json.loads(self.checkpoint.read_text())["audit"]["assignments"]), 5)
        with patch.object(curriculum, "request_json", side_effect=self.respond(plan)) as request:
            curriculum.plan_curriculum(plan["topic"], 10, [], checkpoint=self.checkpoint)
        self.assertEqual(request.call_args.kwargs["job_ids"], ["06", "07", "08", "09", "10"])
        self.assertEqual(request.call_count, 1)

    def test_replacement_cannot_hide_identical_task_with_new_core(self):
        plan = plan_for(10)
        replacement = dict(plan["jobs"][1], task=plan["jobs"][0]["task"], core="different label only")
        with patch.object(curriculum, "request_json", return_value={"jobs": [replacement]}) as request, \
                patch.object(curriculum, "validate_replacement_outcomes") as review:
            with self.assertRaisesRegex(cards.CheckpointGenerationError, "沒有進展"):
                curriculum.repair_duplicate_jobs(plan, {"02"}, {"02": "duplicate"}, [], planning=True)
        self.assertEqual(request.call_count, curriculum.STALLED_RETRY_LIMIT + 1)
        review.assert_not_called()


class AdaptivePairRepairTests(unittest.TestCase):
    def setUp(self):
        for variable in (cards._generation_deadline, curriculum._pair_review_store):
            token = variable.set(None)
            self.addCleanup(variable.reset, token)
        mock = patch.object(cards, "_call_openai", side_effect=AssertionError("Unexpected paid API request"))
        mock.start()
        self.addCleanup(mock.stop)
        mock = patch.object(cards, "_progress")
        mock.start()
        self.addCleanup(mock.stop)

    def targets(self, count=8):
        return [dict(key=f"01:{index + 2:02d}",
                     a=dict(word_en="Please stop the music", sentence_en="Turn the music off"),
                     b=dict(word_en=f"Request result {index}", sentence_en=f"Ask for result {index}"))
                for index in range(count)]

    def proof(self, targets):
        return {target["key"]: dict(equivalent=False, reason="Different concrete actions",
                                    a_evidence=target["a"]["word_en"], b_evidence=target["b"]["word_en"],
                                    a_outcome="Stop music", b_outcome=target["b"]["word_en"])
                for target in targets}

    def test_confirmation_length_recursively_splits_and_preserves_proven_results(self):
        targets = self.targets()
        initial = {target["key"]: dict(equivalent=True, reason="Suspected synonym") for target in targets}
        sizes, completed = [], {}
        def response(prompt, stage, model, **options):
            sizes.append(len(options["job_ids"]))
            if len(options["job_ids"]) > 2:
                raise curriculum.TruncatedResponseError("length")
            return {"checks": self.proof(list(options["anchors"].values()))}
        with patch.object(curriculum, "request_json", side_effect=response):
            result = curriculum.confirm_equivalent_pairs(targets, initial, on_checked=completed.update)
        self.assertEqual(sizes, [8, 4, 2, 2, 4, 2, 2])
        self.assertEqual(len(completed), 8)
        self.assertTrue(all(check["_confirmed"] and not check["equivalent"] for check in result.values()))
        self.assertTrue(all(check["equivalent"] for check in initial.values()))

    def test_single_pair_truncation_stops_without_rejecting_or_looping(self):
        with patch.object(curriculum, "request_json", side_effect=curriculum.TruncatedResponseError("length")) as request:
            with self.assertRaisesRegex(cards.CheckpointGenerationError, "不是教材內容退回"):
                curriculum.pair_check_batch("Check", self.targets(1), "教材同義逐對複核", "gpt-4o-mini")
        self.assertEqual(request.call_count, 1)

    def test_negative_first_verdicts_are_cached_even_when_positive_confirmation_times_out(self):
        plan = plan_for(4)
        items = [dict(id=job["id"], word_en=job["task"], sentence_en=job["task"]) for job in plan["jobs"]]
        semantic = {"assignments": [dict(id=job["id"], purpose="same suspected purpose") for job in plan["jobs"]]}
        with tempfile.TemporaryDirectory() as directory:
            path, cache = Path(directory) / "pairs.json", {}
            token = curriculum._pair_review_store.set((path, cache))
            self.addCleanup(curriculum._pair_review_store.reset, token)
            def response(prompt, stage, model, **options):
                if stage == "教材同義逐對複核":
                    return {"checks": {key: dict(equivalent=(key == "01:02"), reason="Concrete actions")
                                       for key in options["job_ids"]}}
                raise cards.GenerationTimeoutError("test timeout")
            with patch.object(curriculum, "request_json", side_effect=response), self.assertRaises(cards.GenerationTimeoutError):
                curriculum.verify_semantic_pairs(plan, items, semantic, {"groups": []})
            self.assertEqual(len(cache), 5)
            self.assertEqual(len(json.loads(path.read_text())["checks"]), 5)
            with patch.object(curriculum, "request_json", return_value={"checks": {
                    "01:02": dict(equivalent=False, reason="Different outcomes")}}) as request:
                curriculum.verify_semantic_pairs(plan, items, semantic, {"groups": []})
            self.assertEqual(request.call_args.kwargs["job_ids"], ["01:02"])
            self.assertEqual(request.call_count, 1)

    def test_reasoning_confirmation_reserves_tokens_for_hidden_reasoning(self):
        target = self.targets(1)[0]
        response = SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop",
                                   message=SimpleNamespace(content='{"checks":{}}'))])
        with patch.object(cards, "_call_openai", return_value=response) as request:
            curriculum.request_json("Check", "教材同義複核確認", "gpt-5-nano",
                                    job_ids=[target["key"]], anchors={target["key"]: target})
        self.assertGreaterEqual(request.call_args.kwargs["max_completion_tokens"], 5500)

    def test_actual_length_response_raises_typed_error_without_accepting_partial_json(self):
        response = SimpleNamespace(choices=[SimpleNamespace(finish_reason="length",
                                   message=SimpleNamespace(content='{"checks":{'))])
        with patch.object(cards, "_call_openai", return_value=response):
            with self.assertRaises(curriculum.TruncatedResponseError):
                curriculum.request_json("Check", "教材同義逐對複核", "gpt-4o-mini", job_ids=["01:02"])
