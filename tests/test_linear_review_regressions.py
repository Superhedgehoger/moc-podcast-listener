"""Synthetic linear-review regressions; mocked verdicts do not prove model quality."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from scripts import linear_review as linear, semantic_review as review
from summary_workflow import digest, read, write


class LinearReviewRegressions(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="linear-review-regressions-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.segments = [{"start": 0, "end": 10,
                          "text": "Alice is the researcher. The mechanism requires feedback."}]
        self.payload = {"semantic_review_contract": linear.CONTRACT,
                        "result": {"segments_path": str(self.root / "segments.json")}}
        self.body = "## Summary\n\nThe researcher describes a mechanism."
        self.knowledge = {"insights": []}
        write(self.root / "segments.json", self.segments)

    def plan(self, segments=None, body=None):
        if segments is not None:
            write(self.root / "segments.json", segments)
        return review.plan_review(self.payload, self.body if body is None else body,
                                  self.knowledge, ["Summary", "knowledge"])

    def data(self, job):
        return json.loads(job["prompt"].split("\nINPUT DATA:\n", 1)[1])

    def assert_bounds_and_coverage(self, plan, segments, body):
        self.assertEqual(plan["mode"], "linear")
        self.assertEqual("".join(unit["text"] for unit in plan["units"]),
                         "".join(segment["text"] for segment in segments))
        self.assertEqual("".join(atom["text"] for atom in plan["artifacts"] if atom["kind"] == "body"), body)
        self.assertCountEqual([key for job in plan["jobs"] for key in job.get("primary_source_ids", [])],
                              [unit["source_id"] for unit in plan["units"]])
        self.assertCountEqual([key for job in plan["jobs"] for key in job.get("artifact_ids", [])],
                              [atom["artifact_id"] for atom in plan["artifacts"]])
        self.assertEqual(len({atom["artifact_id"] for atom in plan["artifacts"]}), len(plan["artifacts"]))
        self.assertEqual(len({job["job_id"] for job in plan["jobs"]}), len(plan["jobs"]))
        self.assertEqual([unit for batch in plan["batches"] for unit in batch], plan["units"])
        self.assertEqual([atom for shard in plan["shards"] for atom in shard["items"]], plan["artifacts"])
        reconstructed = {}
        for atom in plan["artifacts"]:
            if atom["kind"] == "knowledge":
                if "index" in atom:
                    values = reconstructed.setdefault(atom["field"], [])
                    self.assertEqual(atom["index"], len(values))
                    values.append(atom["value"])
                else:
                    self.assertNotIn(atom["field"], reconstructed)
                    reconstructed[atom["field"]] = atom["value"]
        self.assertEqual(reconstructed, self.knowledge)
        for job in plan["jobs"]:
            self.assertLessEqual(len(job["prompt"].encode("utf-8")), linear.BUDGET)
            self.assertLessEqual(len("\n".join(unit["text"] for unit in job["grounding_source"]).encode("utf-8")),
                                 linear.SOURCE_BUDGET)
            data = self.data(job)
            supplied = data.get("primary_source", []) + data.get("context_source", []) + data.get("retrieved_source", [])
            self.assertEqual(len({unit["id"] for unit in supplied}), len(supplied))
            self.assertEqual([unit["text"] for unit in supplied],
                             [unit["text"] for unit in job["grounding_source"]])
            artifacts = data.get("retrieved_body", []) if job["kind"] == "coverage" else data["artifact_shard"]["items"]
            self.assertEqual(len({atom["artifact_id"] for atom in artifacts}), len(artifacts))

    def test_source_units_preserve_speakers_without_inventing_labels(self):
        segments = [{"id": "named", "start": 0.5, "end": 5.5, "speaker": "Alice",
                     "text": "I support the proposal. It needs feedback."},
                    {"id": "anonymous", "start": 6, "end": 10, "speaker": "SPEAKER_00",
                     "text": "I disagree. Consent is required."},
                    {"id": "unlabelled", "start": 10, "end": 12, "text": "Thank you."}]
        original = copy.deepcopy(segments)
        units = review.source_units(segments)
        self.assertEqual(segments, original)
        self.assertEqual("".join(unit["text"] for unit in units), "".join(s["text"] for s in segments))
        for unit in units:
            parent = next(s for s in segments if s["id"] == unit["parent_id"])
            self.assertEqual((unit["start"], unit["end"]), (parent["start"], parent["end"]))
            if "speaker" in parent:
                self.assertEqual(unit["speaker"], parent["speaker"])
            else:
                self.assertNotIn("speaker", unit)

    def test_review_prompts_preserve_source_speaker_attribution(self):
        segments = [{"start": 0, "end": 5, "speaker": "Alice", "text": "I support the proposal."},
                    {"start": 5, "end": 10, "speaker": "Bob", "text": "I oppose the proposal."}]
        body = "## Summary\n\nAlice supports the proposal; Bob opposes it."
        plan = self.plan(segments, body)
        speakers = {segment["text"]: segment["speaker"] for segment in segments}
        for job in plan["jobs"]:
            data = self.data(job)
            supplied = data.get("primary_source", []) + data.get("context_source", []) + data.get("retrieved_source", [])
            self.assertTrue(supplied)
            for unit in supplied:
                self.assertEqual(unit.get("speaker"), speakers[unit["text"]])
        swapped = copy.deepcopy(segments)
        swapped[0]["speaker"], swapped[1]["speaker"] = "Bob", "Alice"
        changed = self.plan(swapped, body)
        self.assertNotEqual([job["prompt"] for job in plan["jobs"]],
                            [job["prompt"] for job in changed["jobs"]])

    def test_source_addresses_restore_exact_original_without_inferencing_verdict(self):
        job = self.plan()["jobs"][0]
        address, source = next(iter(job["source_addresses"].items()))
        verdict = {"status": "failed", "reason": "Missing important source condition",
                   "findings": [{"part": "Summary", "reason": "The source condition was omitted",
                                 "issue_type": "missing_content", "artifact_id": "", "artifact_quote": "",
                                 "source_id": address}], "approved_name_translations": []}
        resolved = review._resolve_source_addresses(verdict, job)
        self.assertEqual(resolved["status"], "failed")
        self.assertEqual(resolved["findings"][0]["source_quote"], source["text"])
        self.assertNotIn("source_quote", verdict["findings"][0])
        review._validate_verdict(resolved, job, self.segments)
        for bad_address in ("a0", "s999999", None, [address]):
            bad = copy.deepcopy(verdict)
            bad["findings"][0]["source_id"] = bad_address
            with self.assertRaisesRegex(ValueError, "unknown original source address"):
                review._resolve_source_addresses(bad, job)
        forged = copy.deepcopy(verdict)
        forged["findings"][0]["source_quote"] = "Draft text presented as source"
        with self.assertRaisesRegex(ValueError, "replacement quotation"):
            review._resolve_source_addresses(forged, job)
        legacy = {"status": "passed", "findings": []}
        self.assertIs(review._resolve_source_addresses(legacy, {}), legacy)

    def test_cached_address_verdict_is_validated_without_resolving_again(self):
        self.setup_review()

        def reject(*args):
            value, record = self.respond(*args)
            if len(self.calls) == 1:
                value.update(status="failed", reason="Missing important feedback condition",
                             findings=[{"part": "Summary", "reason": "The feedback requirement was omitted",
                                        "issue_type": "missing_content", "artifact_id": "", "artifact_quote": "",
                                        "source_id": "s1"}])
            return value, record

        with self.assertRaisesRegex(ValueError, "Semantic review failed"):
            self.run_review(Mock(side_effect=reject))
        self.task["attempts"] = 2
        no_call = Mock(side_effect=AssertionError("Unchanged review must use cached normalized verdicts"))
        with self.assertRaisesRegex(ValueError, "Semantic review failed"):
            self.run_review(no_call)
        no_call.assert_not_called()

    def test_critique_must_quote_actual_artifact_in_its_producer_part(self):
        job = next(j for j in self.plan()["jobs"] if j["kind"] == "grounding" and j["shard"]["kind"] == "body")
        atom = job["shard"]["items"][0]
        finding = {"part": "Summary", "reason": "The original feedback condition is absent",
                   "issue_type": "incorrect_claim", "artifact_id": atom["artifact_id"],
                   "artifact_quote": atom["text"], "source_id": next(iter(job["source_addresses"]))}
        verdict = {"status": "failed", "reason": finding["reason"], "findings": [finding],
                   "approved_name_translations": []}
        resolved = review._resolve_source_addresses(verdict, job)
        review._validate_verdict(resolved, job, self.segments)
        for updates, diagnostic in (({"artifact_quote": "She imagined an unsupported city."}, "text absent"),
                                    ({"artifact_id": "a999999"}, "supplied artifact"),
                                    ({"part": "knowledge"}, "producer part"),
                                    ({"issue_type": "missing_content", "artifact_id": "", "artifact_quote": ""}, "only valid")):
            bad = copy.deepcopy(verdict)
            bad["findings"][0].update(updates)
            with self.assertRaisesRegex(ValueError, diagnostic):
                review._resolve_source_addresses(bad, job)
        tampered = copy.deepcopy(resolved)
        tampered["findings"][0]["artifact_quote"] = "Absent draft quotation"
        with self.assertRaisesRegex(ValueError, "text absent"):
            review._validate_verdict(tampered, job, self.segments)

    def test_grounding_splits_artifacts_to_fit_all_required_source(self):
        texts = [f"Example {i} requires " + chr(97 + i) * 2965 + "." for i in range(3)]
        segments = [{"start": i * 10, "end": i * 10 + 10, "text": text} for i, text in enumerate(texts)]
        # Separate paragraphs exercise shard packing; one paragraph exercises recursive admission.
        for separator in ("\n\n", " "):
            with self.subTest(separator=repr(separator)):
                body = "## Summary\n\n" + separator.join(texts)
                plan = self.plan(segments, body)
                self.assert_bounds_and_coverage(plan, segments, body)
                jobs = [job for job in plan["jobs"] if job["kind"] == "grounding" and job["shard"]["kind"] == "body"]
                checked = []
                for job in jobs:
                    shard_text = "".join(atom["text"] for atom in job["shard"]["items"])
                    supplied = [unit["text"] for unit in self.data(job)["retrieved_source"]]
                    for text in texts:
                        if text in shard_text:
                            self.assertIn(text, supplied, "A grounding job must receive evidence for every included claim")
                            checked.append(text)
                self.assertCountEqual(checked, texts)
                self.assertGreater(len(jobs), 1)

    def test_coverage_splits_source_batches_to_reserve_each_best_body_paragraph(self):
        segments = [{"start": i, "end": i + 1, "text": f"Topic{i} requires condition{i}."}
                    for i in range(4)]
        paragraphs = [segment["text"] + " " + chr(97 + i) * 6500 + "."
                      for i, segment in enumerate(segments)]
        body = "## Summary\n\n" + "\n\n".join(paragraphs)
        self.assertLess(len("\n".join(s["text"] for s in segments).encode("utf-8")), linear.SOURCE_BUDGET)
        self.assertGreater(len(body.encode("utf-8")), linear.BUDGET)
        plan = self.plan(segments, body)
        self.assert_bounds_and_coverage(plan, segments, body)
        jobs = [job for job in plan["jobs"] if job["kind"] == "coverage"]
        self.assertGreater(len(jobs), 1, "Complete source fits alone, but its required body passages do not")
        by_source = {segment["text"]: paragraph for segment, paragraph in zip(segments, paragraphs)}
        checked = []
        for job in jobs:
            data = self.data(job)
            passages = [atom["text"] for atom in data["retrieved_body"]]
            for unit in data["primary_source"]:
                self.assertTrue(any(by_source[unit["text"]] in text for text in passages),
                                "Every primary sentence needs its complete best body paragraph")
                checked.append(unit["text"])
        self.assertCountEqual(checked, list(by_source))

    def test_divisible_atom_accounts_for_generated_id_overhead(self):
        prefix = "## Summary\nFact. "
        sample = review._body_atoms(prefix, ["Summary", "knowledge"], {})[0]
        remaining = 9490 - linear._size(sample)
        body = prefix + "Fact. " * (remaining // 6) + "x" * (remaining % 6)
        atom = review._body_atoms(body, ["Summary", "knowledge"], {})[0]
        self.assertEqual(linear._size(atom), 9490)
        self.assertGreater(linear._size({**atom, "artifact_id": "a0"}), linear.ARTIFACT_BUDGET)
        segments = [{"start": 0, "end": 1, "text": "Fact. xxx"}]
        plan = self.plan(segments, body)
        self.assert_bounds_and_coverage(plan, segments, body)
        self.assertTrue(all(linear._size(item) <= linear.ARTIFACT_BUDGET for item in plan["artifacts"]))
        self.assertGreater(len([item for item in plan["artifacts"] if item["kind"] == "body"]), 1)

    def setup_review(self):
        self.task = {"kind": "write", "input_hash": "synthetic-input", "attempts": 1,
                     "input": str(self.root / "input.json"), "output": str(self.root / "output.json")}
        write(self.task["input"], self.payload)
        write(self.task["output"], {"input_hash": self.task["input_hash"]})
        write(self.root / "body.md", self.body)
        write(self.root / "knowledge.draft.json", self.knowledge)
        write(self.root / "coverage.json", {"items": []})
        self.config = self.root / "private.json"
        write(self.config, {"tools": {"deny": ["*"]}})
        self.requests = [{"kind": "section", "name": "Summary"}, {"kind": "knowledge", "name": "knowledge"}]
        for index, request in enumerate(self.requests):
            write(self.part(request) / "validated.json", {"value": self.body if index == 0 else self.knowledge})
            write(self.part(request) / "attempt-1/worker-run.json", {"sessionId": f"producer-{index}"})
        self.calls = []

    def part(self, request):
        return self.root / "parts" / digest([request["kind"], request["name"]])[:16]

    def respond(self, *args):
        self.calls.append(args[2])
        record = {"sessionId": f"review-{len(self.calls)}", "provider": "provider", "model": "current"}
        write(args[-1] / "worker-run.json", record)
        return {"status": "passed", "reason": "Synthetic protocol-only successful verdict",
                "findings": [], "approved_name_translations": []}, record

    def run_review(self, invoke):
        return review.run_review(self.task, self.payload, "provider/current", str(self.config), 600,
                                 invoke, self.requests)

    def test_linear_progress_resumes_after_interruption(self):
        self.setup_review()

        def interrupted(*args):
            result = self.respond(*args)
            if len(self.calls) == 2:
                raise ValueError("Synthetic interrupted reviewer")
            return result

        with self.assertRaisesRegex(ValueError, "Synthetic interrupted"):
            self.run_review(Mock(side_effect=interrupted))
        self.assertEqual(len(list((self.root / "semantic-review-progress").glob("*.json"))), 1)
        self.assertEqual(read(self.root / "semantic-review.json")["status"], "failed")
        self.task["attempts"] = 2
        audit = self.run_review(Mock(side_effect=self.respond))
        self.assertEqual(audit["status"], "passed")
        self.assertTrue(audit["reviewer"]["sessions"][0]["reused"])
        self.assertEqual(len(self.calls), len(audit["planned_pairs"]) + 1)
        sessions = [row["sessionId"] for row in audit["reviewer"]["sessions"]]
        self.assertEqual(len(set(sessions)), len(sessions))
        self.assertTrue(set(sessions).isdisjoint(audit["reviewer"]["producer_session_ids"]))
        self.task["attempts"] = 3
        no_call = Mock(side_effect=AssertionError("Unchanged validated progress must be reused"))
        self.assertEqual(self.run_review(no_call)["status"], "passed")
        no_call.assert_not_called()

    def test_source_change_invalidates_linear_progress(self):
        self.setup_review()
        self.run_review(Mock(side_effect=self.respond))
        before = len(self.calls)
        self.segments[0]["text"] += " Consent is required."
        write(self.root / "segments.json", self.segments)
        self.task["attempts"] = 2
        audit = self.run_review(Mock(side_effect=self.respond))
        self.assertEqual(audit["status"], "passed")
        self.assertEqual(len(self.calls) - before, len(audit["planned_pairs"]))
        self.assertTrue(all(not row["reused"] for row in audit["reviewer"]["sessions"]))

    def test_linear_rejection_invalidates_only_impacted_part(self):
        self.setup_review()
        finding = {"part": "Summary", "reason": "The feedback requirement was omitted",
                   "issue_type": "missing_content", "artifact_id": "", "artifact_quote": "",
                   "source_id": "s1"}

        def reject(*args):
            value, record = self.respond(*args)
            if len(self.calls) == 1:
                value.update(status="failed", reason=finding["reason"], findings=[finding])
            return value, record

        with self.assertRaisesRegex(ValueError, "Semantic review failed"):
            self.run_review(Mock(side_effect=reject))
        self.assertFalse((self.part(self.requests[0]) / "validated.json").exists())
        self.assertTrue((self.part(self.requests[1]) / "validated.json").exists())
        self.assertEqual(read(self.root / "semantic-repair.json")["sections"],
                         {"Summary": [{**finding, "source_quote": " The mechanism requires feedback."}]})
        audit = read(self.root / "semantic-review.json")
        self.assertEqual(audit["status"], "failed")
        self.assertCountEqual(audit["planned_pairs"], audit["audited_pairs"])

    def test_unicode_partition_and_prompts_remain_lossless_and_bounded(self):
        self.knowledge = {"insights": [{"claim": "\u8bc1\u636e", "details": ["same", "same"]},
                                      {"claim": "\u9650\u5b9a", "details": []}],
                          "tags": ["repeated", "repeated"], "empty": [], "metadata": {"version": 1}}
        segments = [{"start": i + 0.25, "end": i + 1.25,
                     "text": "\u8bc1\u636e\u4e0e\u9650\u5b9a\u6761\u4ef6\u3002" * 12} for i in range(50)]
        body = "## Summary\n\n" + "\n\n".join(segment["text"] for segment in segments[::4])
        plan = self.plan(segments, body)
        self.assert_bounds_and_coverage(plan, segments, body)
        for unit in plan["units"]:
            self.assertIn((unit["start"], unit["end"]), [(s["start"], s["end"]) for s in segments])


if __name__ == "__main__":
    unittest.main()
