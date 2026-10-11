"""Synthetic recovery and budget invariants, not live model quality scores."""
import copy
import json
from unittest import TestCase
from unittest.mock import Mock, patch

from scripts import review_retrieval as retrieval, semantic_review as review
from summary_workflow import digest, read, write
from tests import test_linear_review_regressions as fixtures
from tests.test_summary_workflow import WorkflowFixture


class RetrievalTests(WorkflowFixture):
    def test_provenance_hints_use_validated_original_leaf_segments(self):
        import summary_workflow as workflow
        self.set_source(["First original mechanism.", "Second original example.", "Third original condition."])
        workflow.prepare(self.result_path)
        self.complete_extracts()
        task = self.state()["tasks"]["write"]
        payload = read(task["input"])
        units = review.source_units(read(self.result["segments_path"]))
        hints = retrieval.part_parents(payload, units)
        self.assertEqual(list(hints.values()), [{0}, {1}, {2}])
        source = self.extracts()[0]
        changed = read(source["input"])
        changed["segments"][0]["text"] = "Changed source cannot become a hint"
        write(source["input"], changed)
        with self.assertRaisesRegex(ValueError, "Task input changed"):
            retrieval.part_parents(payload, units)

    def test_clauses_preserve_decimal_numbers_and_every_character(self):
        text = "A needs 3.5 units; B needs consent；C has feedback.\n"
        parts = retrieval.assertions(text)
        self.assertEqual("".join(parts), text)
        self.assertIn("3.5", parts[0])
        self.assertGreaterEqual(len(parts), 3)

    def test_multiple_claims_reserve_their_rare_terms_and_numbers(self):
        from scripts.linear_review import _terms
        segments = [{"start": 0, "end": 2, "text": "The platform matters. " * 20},
                    {"start": 2, "end": 4, "text": "Uploads to B站 receive 1000 plays."},
                    {"start": 4, "end": 6, "text": "海星说这个人物没有抓手。"},
                    {"start": 6, "end": 8, "text": "片尾曲与小号制作了一整套。"}]
        units = review.source_units(segments)
        atom = {"kind": "body", "part_names": ["Summary"],
                "text": "The platform matters; B站 has 1000 plays；海星没有抓手；片尾曲和小号做了一整套。"}
        chosen = retrieval.required(atom, units, [_terms(u["text"]) for u in units], {})
        parents = {retrieval._parent(units[i]) for i in chosen}
        self.assertTrue({1, 2, 3} <= parents)

    def test_hint_scope_does_not_hide_an_out_of_scope_claim(self):
        from scripts.linear_review import _terms
        units = review.source_units([{"start": 0, "end": 2, "text": "Relevant example needs context."},
                                     {"start": 2, "end": 4, "text": "A rare control requires 1987 feedback."}])
        atom = {"kind": "body", "part_names": ["Part"], "text": "The rare control requires 1987 feedback."}
        chosen = retrieval.required(atom, units, [_terms(u["text"]) for u in units], {"Part": {0}})
        self.assertIn(1, {retrieval._parent(units[i]) for i in chosen})


class RecoveryTests(TestCase):
    plan = fixtures.LinearReviewRegressions.plan
    assert_bounds_and_coverage = fixtures.LinearReviewRegressions.assert_bounds_and_coverage
    data = fixtures.LinearReviewRegressions.data
    setup_review = fixtures.LinearReviewRegressions.setup_review
    part = fixtures.LinearReviewRegressions.part
    run_review = fixtures.LinearReviewRegressions.run_review

    def setUp(self):
        fixtures.LinearReviewRegressions.setUp(self)
        self.payload.update(semantic_review_contract=retrieval.CONTRACT,
                            part_retry_contract="independent_parts_v1")

    def respond(self, *args):
        value, record = fixtures.LinearReviewRegressions.respond(self, *args)
        value["source_requests"] = []
        return value, record

    def test_soft_shard_budget_does_not_truncate_an_indivisible_complete_sentence(self):
        segments = [{"start": 0, "end": 1, "text": "x" * 7900 + "."}]
        body = "## Summary\n\n" + segments[0]["text"]
        plan = self.plan(segments, body)
        self.assert_bounds_and_coverage(plan, segments, body)
        self.assertTrue(any(segments[0]["text"] in atom.get("text", "") for atom in plan["artifacts"]))

    def test_optional_escaped_neighbor_cannot_block_a_source_supported_claim(self):
        segments = [{"start": 0, "end": 1, "text": "Alpha mechanism requires consent."},
                    {"start": 1, "end": 2, "text": '"' * 7400}]
        body = "## Summary\n\nAlpha mechanism requires consent " + "a" * 7900 + "."
        plan = self.plan(segments, body)
        self.assert_bounds_and_coverage(plan, segments, body)
        job = next(job for job in plan["jobs"] if job["kind"] == "grounding"
                   and any("a" * 7900 in atom.get("text", "") for atom in job["shard"]["items"]))
        self.assertIn("s0", job["source_addresses"])
        self.assertNotIn("s1", job["source_addresses"])
        self.assertLessEqual(len(job["prompt"].encode()), retrieval.PROMPT_BUDGET)

    def test_new_plan_preserves_all_artifacts_and_reserves_feedback_budget(self):
        segments = [{"start": i, "end": i + 1, "text": f"Topic{i} needs condition{i}."} for i in range(40)]
        body = "## Summary\n\n" + "\n\n".join(s["text"] + " " + "x" * 200 for s in segments)
        plan = self.plan(segments, body)
        self.assert_bounds_and_coverage(plan, segments, body)
        self.assertEqual(plan["contract"], retrieval.CONTRACT)
        for job in plan["jobs"]:
            self.assertLessEqual(len(job["prompt"].encode()), 22000)

    def request_source(self, job):
        atom = next(atom for atom in job["artifact_addresses"].values()
                    if atom["kind"] == "body" and "researcher" in atom["text"])
        return {"status": "needs_source", "reason": "The original claim context was not supplied",
                "findings": [], "approved_name_translations": [],
                "source_requests": [{"part": atom["part_names"][0], "artifact_id": atom["artifact_id"],
                                     "artifact_quote": atom["text"],
                                     "reason": "Need the original mechanism and role context"}]}

    def deficient_plan(self):
        plan = self.plan()
        job = next(j for j in plan["jobs"] if j["kind"] == "grounding" and j["shard"]["kind"] == "body")
        unit = plan["units"][0]
        job.update(grounding_source=[unit], source_addresses={"s0": unit}, required_source_indexes=[0])
        job["prompt"] = retrieval.prompt("grounding", {
            "retrieved_source": [{"id": "s0", **{k: unit[k] for k in ("start", "end", "text")}}],
            "artifact_shard": job["shard"], "allowed_parts": job["parts"]})
        return plan, job

    def test_needs_source_must_bind_real_artifact_and_cannot_pass_aggregate(self):
        plan, job = self.deficient_plan()
        response = self.request_source(job)
        self.assertEqual(review._resolve_source_addresses(response, job), response)
        review._validate_verdict(response, job, self.segments)
        for updates in ({"artifact_id": "a999"}, {"artifact_quote": "invented artifact text"}, {"part": "knowledge"}):
            wrong = copy.deepcopy(response)
            wrong["source_requests"][0].update(updates)
            with self.assertRaises(ValueError):
                review._validate_verdict(wrong, job, self.segments)
        values = {j["job_id"]: {"status": "passed", "findings": []} for j in plan["jobs"]}
        values[job["job_id"]] = response
        with self.assertRaisesRegex(ValueError, "awaiting original-source"):
            review._aggregate(plan, values, ["Summary", "knowledge"])

    def test_missing_verdict_fields_are_reported_without_synthesizing_a_judgment(self):
        _, job = self.deficient_plan()
        response = self.request_source(job)
        del response["reason"]
        original = copy.deepcopy(response)
        with self.assertRaisesRegex(ValueError, "missing=reason"):
            review._validate_verdict(response, job, self.segments)
        self.assertEqual(response, original)

    def test_lookup_is_reaudited_and_its_exact_scope_resumes_without_new_calls(self):
        self.setup_review()
        plan, job = self.deficient_plan()
        job_id = job["job_id"]
        asked = False

        def respond(*args):
            nonlocal asked
            value, record = self.respond(*args)
            data = json.loads(args[2].split("\nINPUT DATA:\n", 1)[1].split("\n\n协议纠错", 1)[0])
            if data["job_kind"] == "grounding" and data["artifact_shard"]["kind"] == "body" and not asked:
                asked = True
                value = self.request_source(job)
            elif asked and data["job_kind"] == "grounding" and data["artifact_shard"]["kind"] == "body":
                self.assertIn("s1", {s["id"] for s in data["retrieved_source"]})
            return value, record

        with patch.object(review, "plan_review", return_value=plan):
            audit = self.run_review(Mock(side_effect=respond))
        self.assertEqual(audit["status"], "passed")
        self.assertEqual(len(self.calls), len(plan["jobs"]) + 1)
        self.assertFalse((self.root / "semantic-repair.json").exists())
        progress = [read(p) for p in (self.root / "semantic-review-progress").glob("*.json")]
        saved = next(value for value in progress if value["job_id"] == job_id)
        self.assertEqual(len(saved["recovery_rounds"]), 1)
        self.task["attempts"] = 2
        no_call = Mock(side_effect=AssertionError("Validated expanded review must be reused"))
        with patch.object(review, "plan_review", return_value=plan):
            self.assertEqual(self.run_review(no_call)["status"], "passed")
        no_call.assert_not_called()

    def test_lookup_that_cannot_add_context_preserves_drafts_without_fishing(self):
        self.setup_review()
        plan = self.plan()
        job = next(j for j in plan["jobs"] if j["kind"] == "grounding" and j["shard"]["kind"] == "body")

        def respond(*args):
            value, record = self.respond(*args)
            if len(self.calls) == 2:
                value = self.request_source(job)
            return value, record

        with self.assertRaisesRegex(OSError, "cannot add relevant original context"):
            self.run_review(Mock(side_effect=respond))
        self.assertEqual(read(self.root / "semantic-review.json")["status"], "needs_source")
        self.assertTrue((self.part(self.requests[0]) / "validated.json").exists())
        self.assertFalse((self.root / "semantic-repair.json").exists())
        self.task["attempts"] = 2
        no_call = Mock(side_effect=AssertionError("Known blocked lookup cannot repeat model calls"))
        with self.assertRaises(OSError):
            self.run_review(no_call)
        no_call.assert_not_called()

    def test_protocol_retry_receives_error_without_changing_the_base_budget(self):
        self.setup_review()
        plan = self.plan()
        job = next(j for j in plan["jobs"] if j["kind"] == "grounding" and j["shard"]["kind"] == "body")
        first = True

        def respond(*args):
            nonlocal first
            value, record = self.respond(*args)
            if '"job_kind":"grounding"' in args[2] and first:
                first = False
                atom = next(iter(job["artifact_addresses"].values()))
                value.update(status="failed", reason="Unsupported synthetic claim requires repair",
                             findings=[{"part": atom["part_names"][0], "source_id": "s0",
                                        "issue_type": "missing_content", "artifact_id": atom["artifact_id"],
                                        "artifact_quote": atom["text"], "reason": "Wrong protocol type supplied for this review"}])
            return value, record

        audit = self.run_review(Mock(side_effect=respond))
        self.assertEqual(audit["status"], "passed")
        corrected = [prompt for prompt in self.calls if "协议纠错" in prompt]
        self.assertEqual(len(corrected), 1)
        self.assertIn("only valid for source coverage", corrected[0])
        self.assertLessEqual(len(corrected[0].encode()), 23000)
        ledger = [read(p) for p in (self.root / "review-retries").rglob("*.json")]
        self.assertTrue(any([r["status"] for r in value["attempts"]] == ["failed", "passed"] for value in ledger))

    def test_context_only_unit_cannot_be_reported_as_a_primary_omission(self):
        plan = self.plan()
        job = next(j for j in plan["jobs"] if j["kind"] == "coverage")
        address = next(key for key in job["source_addresses"] if key == "s1")
        job["primary_source_ids"] = [plan["units"][0]["source_id"]]
        response = {"status": "failed", "reason": "Synthetic omission outside the primary source",
                    "findings": [{"part": "Summary", "source_id": address, "issue_type": "missing_content",
                                  "artifact_id": "", "artifact_quote": "", "reason": "Context must not count as primary omission"}],
                    "source_requests": [], "approved_name_translations": []}
        with self.assertRaisesRegex(ValueError, "context-only"):
            review._resolve_source_addresses(response, job)

    def test_exhausted_protocol_resume_keeps_evidence_pending_not_factually_failed(self):
        self.setup_review()
        self.segments.append({"start": 10, "end": 12, "text": "Additional original role context."})
        write(self.payload["result"]["segments_path"], self.segments)
        plan, job = self.deficient_plan()

        def more_context(current, units, *unused):
            address = next(f"s{i}" for i in range(len(units)) if f"s{i}" not in current["source_addresses"])
            unit = units[int(address[1:])]
            return {**current, "prompt": current["prompt"] + "\nSynthetic bounded lookup round",
                    "source_addresses": {**current["source_addresses"], address: unit},
                    "grounding_source": current["grounding_source"] + [unit]}

        def respond(*args):
            value, record = self.respond(*args)
            if len(self.calls) == 2:
                return {"status": "invalid protocol"}, record
            if len(self.calls) > 2:
                return self.request_source(job), record
            return value, record

        with patch.object(review, "plan_review", return_value=plan), patch.object(retrieval, "expand_job", side_effect=more_context):
            with self.assertRaisesRegex(ValueError, "retry limit exhausted"):
                self.run_review(Mock(side_effect=respond))
            self.assertEqual(read(self.root / "semantic-review.json")["status"], "needs_source")
            self.task["attempts"] = 2
            no_call = Mock(side_effect=AssertionError("Exhausted protocol cannot make another call"))
            with self.assertRaisesRegex(ValueError, "retry limit exhausted"):
                self.run_review(no_call)
            no_call.assert_not_called()
        self.assertEqual(read(self.root / "semantic-review.json")["status"], "needs_source")
        self.assertFalse((self.root / "semantic-repair.json").exists())

    def test_input_migration_preserves_expanded_scope_and_remaining_budget(self):
        self.setup_review()
        plan, job = self.deficient_plan()

        def interrupted(*args):
            value, record = self.respond(*args)
            if len(self.calls) == 2:
                return self.request_source(job), record
            if len(self.calls) == 3:
                raise OSError("Synthetic network interruption after source lookup")
            return value, record

        with patch.object(review, "plan_review", return_value=plan), self.assertRaises(OSError):
            self.run_review(Mock(side_effect=interrupted))
        old_recovery = next((self.root / "review-recovery").glob("*.json"))
        original = old_recovery.read_bytes()
        old_hash = self.task["input_hash"]
        self.task["input_hash"] = "migrated-synthetic-input"
        self.payload["migrated_from_input_hashes"] = [old_hash]
        write(self.task["input"], self.payload)
        write(self.task["output"], {"input_hash": self.task["input_hash"]})
        self.task["attempts"] = 2

        def finish(*args):
            if '"kind":"body"' in args[2]:
                self.assertIn('"id":"s1"', args[2])
            return self.respond(*args)

        with patch.object(review, "plan_review", return_value=plan):
            self.assertEqual(self.run_review(Mock(side_effect=finish))["status"], "passed")
        self.assertEqual(old_recovery.read_bytes(), original)
        checkpoints = [read(p) for p in (self.root / "review-recovery").glob("*.json")]
        self.assertTrue(any(value.get("migrated_from") == str(old_recovery) for value in checkpoints))
        ledgers = [read(p) for p in (self.root / "review-retries").rglob("*.json")]
        self.assertTrue(any(len(value["attempts"]) == 3 and value["attempts"][-1]["status"] == "passed"
                            for value in ledgers))

    def test_storage_failure_after_valid_rejection_cannot_request_a_new_vote(self):
        self.setup_review()
        plan = self.plan()
        job = next(j for j in plan["jobs"] if j["kind"] == "coverage")

        def reject(*args):
            value, record = self.respond(*args)
            value.update(status="failed", reason="An explicit source qualification was omitted",
                         findings=[{"part": "Summary", "issue_type": "missing_content", "source_id": "s1",
                                    "artifact_id": "", "artifact_quote": "",
                                    "reason": "The original feedback qualification is missing"}])
            return value, record

        actual_write = review.write
        def storage_error(path, value):
            if "semantic-review-progress" in str(path):
                raise OSError("Synthetic cache write interruption")
            return actual_write(path, value)

        with patch.object(review, "write", side_effect=storage_error):
            with self.assertRaisesRegex(OSError, "could not be persisted"):
                self.run_review(Mock(side_effect=reject))
        self.assertEqual(len(self.calls), 1)
        self.task["attempts"] = 2
        def finish(*args):
            self.assertNotIn('"job_kind":"coverage"', args[2], "Known rejection cannot be re-evaluated")
            return self.respond(*args)
        with self.assertRaisesRegex(ValueError, "Semantic review failed"):
            self.run_review(Mock(side_effect=finish))
        self.assertTrue((self.root / "semantic-repair.json").exists())
        self.assertNotEqual(read(self.root / "semantic-review.json")["status"], "passed")

    def test_legacy_valid_rejection_receipt_also_survives_cache_write_failure(self):
        self.payload["semantic_review_contract"] = "linear_source_grounding_v4"
        self.payload.pop("part_retry_contract")
        self.test_storage_failure_after_valid_rejection_cannot_request_a_new_vote()

    def test_failed_receipt_and_cache_write_blocks_before_any_new_vote(self):
        self.setup_review()
        actual_write = review.write
        def storage_error(path, value):
            if "review-receipts" in str(path):
                raise OSError("Synthetic receipt storage interruption")
            return actual_write(path, value)
        with patch.object(review, "write", side_effect=storage_error), self.assertRaises(OSError):
            self.run_review(Mock(side_effect=self.respond))
        self.task["attempts"] = 2
        no_vote = Mock(side_effect=AssertionError("Cannot replace an unpersisted valid judgment"))
        with self.assertRaisesRegex(OSError, "never request a different verdict"):
            self.run_review(no_vote)
        no_vote.assert_not_called()

    def test_legacy_storage_block_survives_every_remaining_parent_attempt(self):
        self.payload["semantic_review_contract"] = "linear_source_grounding_v4"
        self.payload.pop("part_retry_contract")
        self.setup_review()
        actual_write = review.write

        def storage_error(path, value):
            if "review-receipts" in str(path):
                raise OSError("Synthetic missing durable receipt")
            return actual_write(path, value)

        with patch.object(review, "write", side_effect=storage_error), self.assertRaises(OSError):
            self.run_review(Mock(side_effect=self.respond))
        blocked_key = read(self.root / "semantic-review.json")["storage_failure_key"]
        for attempt in (2, 3):
            self.task["attempts"] = attempt
            no_vote = Mock(side_effect=AssertionError("A missing judgment cannot become a fresh vote"))
            with self.assertRaisesRegex(OSError, "never request a different verdict"):
                self.run_review(no_vote)
            no_vote.assert_not_called()
            self.assertEqual(read(self.root / "semantic-review.json")["storage_failure_key"], blocked_key)

    def test_reordered_json_fields_are_not_new_original_context(self):
        self.segments[0]["speaker"] = "Alice"
        write(self.payload["result"]["segments_path"], self.segments)
        self.setup_review()
        plan = self.plan()
        job = next(j for j in plan["jobs"] if j["kind"] == "grounding" and j["shard"]["kind"] == "body")
        expanded = retrieval.expand_job(job, plan["units"], self.request_source(job)["source_requests"])
        self.assertEqual(set(job["source_addresses"]), set(expanded["source_addresses"]))
        self.assertEqual(self.data(job), self.data(expanded))

        def respond(*args):
            value, record = self.respond(*args)
            if '"job_kind":"grounding"' in args[2]:
                value = self.request_source(job)
            return value, record

        with self.assertRaisesRegex(OSError, "cannot add relevant original context"):
            self.run_review(Mock(side_effect=respond))
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(read(self.root / "semantic-review.json")["status"], "needs_source")
        self.assertFalse((self.root / "semantic-repair.json").exists())
