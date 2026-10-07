"""Mocked offline gate contracts only, NOT live semantic-quality evaluation."""
import copy
import json
import re
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts import semantic_review as review
from summary_workflow import digest, output_hash, read, write


class SemanticReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.source = [{"start": 0, "end": 10,
                        "text": "Alice is the researcher. Only 42 observations; the mechanism requires feedback."}]
        write(self.path / "segments.json", self.source)
        self.payload = {"result": {"segments_path": str(self.path / "segments.json")}}
        self.task = {"kind": "write", "input_hash": "input", "attempts": 1,
                     "input": str(self.path / "input.json"), "output": str(self.path / "output.json")}
        write(self.task["output"], {"input_hash": "input"})
        write(self.path / "body.md", "## Summary\nAlice, researcher, reports 42 observations.")
        write(self.path / "knowledge.draft.json", {"insights": []})
        write(self.path / "coverage.json", {"items": []})
        self.config = self.path / "private.json"
        write(self.config, {"tools": {"deny": ["*"]}})
        self.requests = [{"kind": "section", "name": "Summary", "prompt": "producer"},
                         {"kind": "knowledge", "name": "knowledge", "prompt": "producer"}]
        for request in self.requests:
            part = self.part(request)
            write(part / "validated.json", {"value": "cached"})
            write(part / "attempt-1/worker-run.json", {"sessionId": "producer"})
        self.record = {"sessionId": "fresh-review", "provider": "provider", "model": "current"}
        self.verdict = {"status": "passed", "reason": "All source fidelity checks completed",
                        "findings": [], "approved_name_translations": []}
        self.invoke = Mock(side_effect=lambda *args: (copy.deepcopy(self.verdict), copy.deepcopy(self.record)))

    def part(self, request):
        return self.path / "parts" / digest([request["kind"], request["name"]])[:16]

    def run_review(self):
        return review.run_review(self.task, self.payload, "provider/current", str(self.config),
                                 600, self.invoke, self.requests)

    def long_fixture(self, count=4):
        """3K complete original units and multi-shard artifacts, not quality evidence."""
        self.source = []
        paragraphs, insights = [], []
        for index in range(count):
            clause = f"Alice researcher explains mechanism-{index:04d}: feedback required."
            text = clause[:-1] + " " + "s" * (3000 - len(clause) - 1) + "."
            self.source.append({"id": f"original-{index}", "start": index * 10 + 0.5,
                                "end": index * 10 + 9.5, "text": text})
            paragraphs.append(clause + " " + "b" * 2950 + "\n\n")
            insights.append({"id": str(index), "claim": "Synthetic independent knowledge item " + str(index),
                             "evidence": [{"start": index * 10 + 0.5, "end": index * 10 + 9.5,
                                           "quote": "Synthetic evidence paraphrase"}]})
        self.source.append({"id": "minor", "start": count * 10, "end": count * 10 + 1,
                            "text": "A repeated greeting, a minor non-mechanism."})
        self.body = "## Summary\n\n" + "".join(paragraphs)
        self.knowledge = {"schema_version": 1, "status": "complete", "topics": ["Synthetic topic"],
                          "entities": [], "ai_tags": ["Synthetic tag"], "insights": insights}
        write(self.path / "segments.json", self.source)
        write(self.path / "body.md", self.body)
        write(self.path / "knowledge.draft.json", self.knowledge)
        self.calls = []

        def respond(*args):
            data = json.loads(args[2].split("\nINPUT DATA:\n", 1)[1])
            self.calls.append(data)
            self.session_sequence = getattr(self, "session_sequence", 0) + 1
            record = {"sessionId": f"independent-{self.session_sequence}", "provider": "provider", "model": "current"}
            write(args[-1] / "worker-run.json", record)
            return self.scoped_response(data), record
        self.respond = respond
        self.invoke = Mock(side_effect=respond)

    def scoped_response(self, data):
        if "source_batch" not in data:
            return {**copy.deepcopy(self.verdict), "units": []}
        shard = data["artifact_shard"]
        strings = [text for atom in shard["items"] for text in (
            [atom["text"]] if atom["kind"] == "body" else review._string_values(atom["value"]))]
        rows = []
        for unit in data["source_batch"]["units"]:
            marker = re.search(r"mechanism-\d{4}", unit["text"])
            clause = f"Alice researcher explains {marker.group()}: feedback required." if marker else ""
            covered = bool(clause) and any(clause in text for text in strings)
            rows.append({"source_id": unit["source_id"], "major": bool(marker), "covered": covered,
                         "body_quote": clause if covered else "",
                         "reason": "Mechanism is covered by this literal artifact passage" if covered else (
                             "Major feedback mechanism needs coverage in another shard" if marker else
                             "Approved minor omission: repeated greeting has no substantive mechanism")})
        return {"status": "passed", "reason": "Scoped original-source fidelity checks completed",
                "findings": [], "approved_name_translations": [], "units": rows}

    def assert_lossless_plan(self, plan):
        self.assertEqual(plan["mode"], "bounded")
        self.assertEqual("".join(unit["text"] for unit in plan["units"]), "".join(s["text"] for s in self.source))
        primary = [unit for batch in plan["batches"] for unit in batch["units"]]
        self.assertEqual(primary, plan["units"])
        for batch in plan["batches"]:
            self.assertLessEqual(review._source_bytes(batch["units"] + batch["context_units"]), 8000)
            self.assertLessEqual(len(batch["units"]), review.MAX_PRIMARY_UNITS)
        for unit in plan["units"]:
            parent = next(s for s in self.source if s["id"] == unit["parent_id"])
            self.assertEqual((unit["start"], unit["end"]), (parent["start"], parent["end"]))
        text = "".join(atom["text"] for shard in plan["shards"] for atom in shard["items"] if atom["kind"] == "body")
        self.assertEqual(text, self.body)
        reconstructed = {}
        for shard in plan["shards"]:
            if shard["kind"] == "knowledge":
                for atom in shard["items"]:
                    if "index" in atom:
                        reconstructed.setdefault(atom["field"], []).append(atom["value"])
                    else:
                        reconstructed[atom["field"]] = atom["value"]
        self.assertEqual(reconstructed, self.knowledge)
        self.assertEqual(len(plan["jobs"]), (len(plan["batches"]) + 1) * len(plan["shards"]))
        self.assertEqual(len({job["job_id"] for job in plan["jobs"]}), len(plan["jobs"]))
        for job in plan["jobs"]:
            self.assertLessEqual(len(job["prompt"].encode("utf-8")), 23000)
            if "batch" in job:
                self.assertIn("other UNKNOWN source batches", job["prompt"])
                self.assertIn("not sentence times", job["prompt"])
            self.assertIn("untrusted data", job["prompt"])

    def test_many_short_sentences_bound_verdict_rows_without_dropping_text(self):
        source = [{"start": 0, "end": 100, "text": "Important qualification. " * 41}]
        units = review.source_units(source)
        batches = review.source_batches(units)
        self.assertEqual([unit for batch in batches for unit in batch["units"]], units)
        self.assertTrue(all(len(batch["units"]) <= 8 for batch in batches))
        self.assertGreater(len(batches), 1)

    def test_scoped_prompt_provides_valid_json_template_with_all_primary_ids(self):
        batch = review.source_batches(review.source_units(self.source))[0]
        prompt = review._scoped_prompt(batch, {"kind": "body", "items": []})
        template = json.loads(prompt.split("IDs: ", 1)[1].split("\nINPUT DATA:\n", 1)[0])
        self.assertEqual([row["source_id"] for row in template["units"]],
                         [unit["source_id"] for unit in batch["units"]])
        self.assertIn("No YAML", prompt)
        self.assertIn("paraphrase need not match source wording", prompt)

    def test_knowledge_only_coverage_cannot_hide_missing_report_content(self):
        self.long_fixture(count=4)
        plan = review.plan_review(self.payload, self.body, self.knowledge, ["Summary", "knowledge"])
        verdicts = {job["job_id"]: self.scoped_response(json.loads(job["prompt"].split("\nINPUT DATA:\n")[1]))
                    for job in plan["jobs"]}
        for job in plan["jobs"]:
            for row in verdicts[job["job_id"]]["units"]:
                if row["major"]:
                    row["covered"] = job.get("shard", {}).get("kind") == "knowledge"
        _, failures, coverage = review._aggregate(plan, verdicts, ["Summary", "knowledge"])
        self.assertTrue(failures)
        self.assertTrue(all(not row["covered"] for row in coverage.values() if row["major"]))

    def test_long_source_lossless_bounded_product_and_supported_aggregate(self):
        self.long_fixture(count=6)
        plan = review.plan_review(self.payload, self.body, self.knowledge, ["Summary", "knowledge"])
        self.assert_lossless_plan(plan)
        self.assertGreater(len(plan["shards"]), 2)
        audit = self.run_review()
        self.assertEqual(audit["status"], "passed")
        self.assertEqual(set(audit["planned_pairs"]), set(audit["audited_pairs"]))
        self.assertEqual(len(self.calls), len(plan["jobs"]))
        for unit in plan["units"]:
            result = audit["source_coverage"][unit["source_id"]]
            if unit["parent_id"] == "minor":
                self.assertFalse(result["major"])
                self.assertFalse(result["covered"])
                self.assertTrue(all("minor omission" in reason for reason in result["reasons"]))
            else:
                self.assertTrue(result["major"])
                self.assertTrue(result["covered"])
        # Local missing coverage is allowed only because a different shard covers it.
        self.assertTrue(any(not row["covered"] and row["major"] for data in self.calls
                            for row in self.scoped_response(data)["units"]))

    def test_regrouping_source_batches_and_artifact_shards_preserves_coverage(self):
        self.long_fixture(count=6)
        counts = []
        for source_budget, prompt_budget in ((8000, 23000), (6500, 19000), (5000, 16000)):
            with patch.object(review, "SOURCE_BUDGET", source_budget), patch.object(review, "BUDGET", prompt_budget):
                plan = review.plan_review(self.payload, self.body, self.knowledge, ["Summary", "knowledge"])
                self.assert_lossless_plan(plan)
                verdicts = {job["job_id"]: self.scoped_response(json.loads(job["prompt"].split("\nINPUT DATA:\n")[1]))
                            for job in plan["jobs"]}
                for job in plan["jobs"]:
                    review._validate_verdict(verdicts[job["job_id"]], job, self.source)
                    self.assertLessEqual(len(job["prompt"].encode("utf-8")), prompt_budget)
                findings, failures, covered = review._aggregate(plan, verdicts, ["Summary", "knowledge"])
                self.assertEqual((findings, failures), ([], []))
                self.assertTrue(all(result["covered"] for key, result in covered.items() if result["major"]))
                counts.append((len(plan["batches"]), len(plan["shards"])))
        self.assertGreater(len(set(counts)), 1)

    def test_major_omission_across_all_shards_rejects_after_complete_audit(self):
        self.long_fixture()
        clause = "Alice researcher explains mechanism-0002: feedback required."
        write(self.path / "body.md", self.body.replace(clause, "Unrelated synthetic paragraph."))
        plan = review.plan_review(self.payload, (self.path / "body.md").read_text(), self.knowledge, ["Summary", "knowledge"])
        with self.assertRaisesRegex(ValueError, "Major original source unit absent across all artifact shards: s000002.sentence000000"):
            self.run_review()
        self.assertEqual(len(self.calls), len(plan["jobs"]))
        self.assertFalse((self.part(self.requests[0]) / "validated.json").exists())
        self.assertTrue((self.part(self.requests[1]) / "validated.json").exists())
        finding = read(self.path / "semantic-repair.json")["findings"][0]
        self.assertEqual(finding["source_quote"], self.source[2]["text"])
        self.assertEqual(finding["part"], "Summary")

    def test_one_major_vote_overrides_other_minor_votes(self):
        self.long_fixture()
        def respond(*args):
            response, record = self.respond(*args)
            for row in response["units"]:
                if row["source_id"] == "s000000.sentence000000":
                    row.update(major=len(self.calls) == 1, covered=False, body_quote="",
                               reason="Major essential mechanism absent" if len(self.calls) == 1 else
                               "Approved minor omission in this synthetic alternative vote")
            return response, record
        self.invoke.side_effect = respond
        with self.assertRaisesRegex(ValueError, "absent across all artifact shards: s000000"):
            self.run_review()

    def test_invented_artifact_cannot_pass_by_deferral_to_unknown_batches(self):
        self.long_fixture()
        self.body += "\nInvented researcher achieved 900 percent efficiency.\n"
        write(self.path / "body.md", self.body)
        def reject_grounding(*args):
            response, record = self.respond(*args)
            data = self.calls[-1]
            if "source_batch" not in data and any("900 percent" in atom.get("text", "")
                                                   for atom in data["artifact_shard"]["items"]):
                response.update(status="failed", reason="Invented 900 percent claim lacks original support",
                                findings=[{"part": "Summary", "reason": "Original describes feedback, not a 900 percent result",
                                           "source_quote": "Alice researcher"}])
            return response, record
        self.invoke.side_effect = reject_grounding
        with self.assertRaisesRegex(ValueError, "Invented 900 percent"):
            self.run_review()
        self.assertEqual(read(self.path / "semantic-review.json")["status"], "failed")

    def test_missing_duplicate_unknown_unit_ids_and_false_snippet_fail_closed(self):
        for corruption in ("missing", "duplicate", "unknown", "snippet", "minor_reason", "bool"):
            with self.subTest(corruption=corruption):
                self.long_fixture()
                # Remove prior progress, if any, before each distinct mock corruption.
                for file in (self.path / "semantic-review-progress").glob("*.json"):
                    file.unlink()
                self.calls.clear()
                def respond(*args):
                    response, record = self.respond(*args)
                    if corruption == "missing":
                        response["units"].pop()
                    elif corruption == "duplicate":
                        response["units"].append(response["units"][0])
                    elif corruption == "unknown":
                        response["units"][0]["source_id"] = "unknown"
                    elif corruption == "snippet":
                        response["units"][0].update(covered=True, body_quote="fabricated snippet not in artifact")
                    elif corruption == "minor_reason":
                        response["units"][0].update(major=False, covered=False, body_quote="", reason="")
                    else:
                        response["units"][0]["major"] = 1
                    record["sessionId"] += "-" + corruption
                    return response, record
                self.invoke.side_effect = respond
                with self.assertRaises(ValueError):
                    self.run_review()
                self.assertEqual(read(self.path / "semantic-review.json")["status"], "failed")
                self.assertEqual(list((self.path / "semantic-review-progress").glob("*.json")), [])

    def test_progress_resumes_after_interruption_and_third_start_reuses_all(self):
        self.long_fixture()
        plan = review.plan_review(self.payload, self.body, self.knowledge, ["Summary", "knowledge"])
        def interrupted(*args):
            value = self.respond(*args)
            if len(self.calls) == 2:
                raise ValueError("Synthetic interrupted reviewer call")
            return value
        self.invoke.side_effect = interrupted
        with self.assertRaisesRegex(ValueError, "interrupted"):
            self.run_review()
        self.assertEqual(len(list((self.path / "semantic-review-progress").glob("*.json"))), 1)
        self.task["attempts"] = 2
        self.invoke.side_effect = self.respond
        audit = self.run_review()
        self.assertEqual(audit["status"], "passed")
        self.assertTrue(audit["reviewer"]["sessions"][0]["reused"])
        self.assertEqual(len(self.calls), len(plan["jobs"]) + 1)
        before = len(self.calls)
        self.task["attempts"] = 3
        self.assertEqual(self.run_review()["status"], "passed")
        self.assertEqual(len(self.calls), before)
        self.task["attempts"] = 4
        with self.assertRaisesRegex(ValueError, "between one and three"):
            self.run_review()
        self.assertEqual(len(self.calls), before)

    def test_source_artifact_input_config_and_model_changes_cannot_reuse_old_progress(self):
        self.long_fixture()
        self.run_review()
        for changed in ("source", "artifact", "input", "config", "model"):
            before = len(self.calls)
            if changed == "source":
                self.source[0]["text"] += " Updated qualification."
                write(self.path / "segments.json", self.source)
            elif changed == "artifact":
                self.body += "\nSynthetic added paragraph.\n"
                write(self.path / "body.md", self.body)
            elif changed == "input":
                self.task["input_hash"] = "new-input"
            elif changed == "config":
                write(self.config, {"tools": {"deny": ["*"]}, "new_config": True})
            else:
                original = self.respond
                def new_model(*args):
                    value, record = original(*args)
                    record["model"] = "new-current"
                    return value, record
                self.invoke.side_effect = new_model
            if changed == "model":
                audit = review.run_review(self.task, self.payload, "provider/new-current", str(self.config),
                                          600, self.invoke, self.requests)
            else:
                audit = self.run_review()
            self.assertEqual(audit["status"], "passed")
            self.assertGreater(len(self.calls), before)
            if changed == "artifact":
                self.assertTrue(any(row["reused"] for row in audit["reviewer"]["sessions"]))
                self.assertTrue(any(not row["reused"] for row in audit["reviewer"]["sessions"]))
            else:
                self.assertTrue(all(not row["reused"] for row in audit["reviewer"]["sessions"]))

    def test_each_new_pair_requires_distinct_reviewer_session_and_exact_model(self):
        for changed in ("session", "model"):
            self.long_fixture()
            def bad_record(*args):
                value, record = self.respond(*args)
                if changed == "session":
                    record["sessionId"] = "one-session-for-every-pair"
                elif len(self.calls) == 2:
                    record["model"] = "fallback"
                return value, record
            self.invoke.side_effect = bad_record
            expected = "fresh non-producer" if changed == "session" else "exact current model"
            with self.assertRaisesRegex(ValueError, expected):
                self.run_review()
            self.assertEqual(self.invoke.call_count, 2)
            self.assertEqual(read(self.path / "semantic-review.json")["status"], "failed")
            for file in (self.path / "semantic-review-progress").glob("*.json"):
                file.unlink()

    def test_rejection_requires_exact_current_source_and_current_shard_part(self):
        self.long_fixture()
        plan = review.plan_review(self.payload, self.body, self.knowledge, ["Summary", "knowledge"])
        job = plan["jobs"][0]
        response = self.scoped_response(json.loads(job["prompt"].split("\nINPUT DATA:\n")[1]))
        response.update(status="failed", reason="Wrong researcher role explicitly pertaining to this batch")
        quote = job["batch"]["units"][0]["text"].split(".")[0]
        response["findings"] = [{"part": "Summary", "reason": "Source says researcher, not financier", "source_quote": quote}]
        review._validate_verdict(response, job, self.source)
        for finding in ({**response["findings"][0], "source_quote": self.source[-1]["text"]},
                        {**response["findings"][0], "part": "knowledge"},
                        {**response["findings"][0], "source_quote": quote.replace("researcher", "financier")}):
            response["findings"] = [finding]
            with self.assertRaisesRegex(ValueError, "source-supported"):
                review._validate_verdict(response, job, self.source)

    def test_complete_source_sentences_keep_parent_timestamps_and_all_characters(self):
        self.long_fixture()
        self.source = [{"id": "long-parent", "start": 7.5, "end": 90.5,
                        "text": "Alice is researcher. " + ("x" * 2980 + ". ") * 4}]
        write(self.path / "segments.json", self.source)
        plan = review.plan_review(self.payload, self.body, self.knowledge, ["Summary", "knowledge"])
        self.assert_lossless_plan(plan)
        self.assertTrue(all(".sentence" in unit["source_id"] for unit in plan["units"]))
        self.assertTrue(all(unit["start"] == 7.5 and unit["end"] == 90.5 for unit in plan["units"]))

    def test_large_sentence_delimited_body_shards_without_truncation(self):
        self.long_fixture()
        self.body = "## Summary\n" + ("Alice reports original observations " + "x" * 2900 + ". ") * 10
        write(self.path / "body.md", self.body)
        plan = review.plan_review(self.payload, self.body, self.knowledge, ["Summary", "knowledge"])
        self.assert_lossless_plan(plan)
        self.assertGreater(len([s for s in plan["shards"] if s["kind"] == "body"]), 1)

    def test_unfit_indivisible_knowledge_item_stops_before_review(self):
        self.long_fixture()
        self.knowledge["insights"][0]["claim"] = "z" * 23000
        write(self.path / "knowledge.draft.json", self.knowledge)
        with self.assertRaisesRegex(ValueError, "unfit indivisible source/artifact pair"):
            self.run_review()
        self.invoke.assert_not_called()

    def test_no_aggregate_pass_without_every_planned_pair(self):
        self.long_fixture()
        plan = review.plan_review(self.payload, self.body, self.knowledge, ["Summary", "knowledge"])
        verdicts = {job["job_id"]: self.scoped_response(json.loads(job["prompt"].split("\nINPUT DATA:\n")[1]))
                    for job in plan["jobs"]}
        verdicts.pop(next(iter(verdicts)))
        with self.assertRaisesRegex(ValueError, "not every planned unit/shard pair"):
            review._aggregate(plan, verdicts, ["Summary", "knowledge"])

    def test_failed_pair_progress_survives_impacted_producer_cache_invalidation(self):
        self.long_fixture()
        write(self.part(self.requests[0]) / "validated.json", {"value": self.body.split("\n\n", 1)[1]})
        def reject(*args):
            response, record = self.respond(*args)
            if len(self.calls) == 1:
                response.update(status="failed", reason="Explicitly pertaining researcher role rejected",
                                findings=[{"part": "Summary", "reason": "Source role must remain researcher",
                                           "source_quote": "Alice researcher"}])
            return response, record
        self.invoke.side_effect = reject
        with self.assertRaisesRegex(ValueError, "role rejected"):
            self.run_review()
        self.assertFalse((self.part(self.requests[0]) / "validated.json").exists())
        before = len(self.calls)
        self.task["attempts"] = 2
        with self.assertRaisesRegex(ValueError, "role rejected"):
            self.run_review()
        self.assertEqual(len(self.calls), before)
        self.assertTrue(all(r["reused"] for r in read(self.path / "semantic-review.json")["reviewer"]["sessions"]))

    def test_scoped_role_rejection_invalidates_only_impacted_role_part(self):
        self.long_fixture()
        role_request = {"kind": "section", "name": "Roles", "prompt": "producer"}
        self.requests.append(role_request)
        write(self.part(role_request) / "validated.json", {"value": "Alice financier directs the trial."})
        write(self.part(role_request) / "attempt-1/worker-run.json", {"sessionId": "role-producer"})
        self.body += "## Roles\n\nAlice financier directs the trial.\n"
        write(self.path / "body.md", self.body)
        def reject(*args):
            response, record = self.respond(*args)
            data = self.calls[-1]
            applicable = any("Roles" in item["part_names"] and "financier" in item.get("text", "")
                             for item in data["artifact_shard"]["items"])
            source = (data["source_batch"]["units"] + data["source_batch"]["context_units"]
                      if "source_batch" in data else data["original_source"])
            grounded = any("mechanism-0000" in unit["text"] for unit in source)
            if applicable and grounded:
                response.update(status="failed", reason="Wrong role explicitly pertains to source researcher",
                                findings=[{"part": "Roles", "reason": "Financier role contradicts original researcher role",
                                           "source_quote": "Alice researcher"}])
            return response, record
        self.invoke.side_effect = reject
        with self.assertRaisesRegex(ValueError, "Wrong role explicitly"):
            self.run_review()
        self.assertFalse((self.part(role_request) / "validated.json").exists())
        self.assertTrue((self.part(self.requests[0]) / "validated.json").exists())
        self.assertTrue((self.part(self.requests[1]) / "validated.json").exists())
        findings = read(self.path / "semantic-repair.json")["findings"]
        self.assertTrue(findings)
        self.assertTrue(all(f["part"] == "Roles" for f in findings))

    def test_unicode_three_k_sentences_retain_full_original_parent_and_byte_bounds(self):
        self.long_fixture()
        sentence = "原始角色与机制" + "证" * 992 + "。\n"
        self.assertGreaterEqual(len(sentence.encode("utf-8")), 3000)
        self.source = [{"id": "unicode-parent", "start": 3.2, "end": 45.8, "text": sentence * 5}]
        write(self.path / "segments.json", self.source)
        plan = review.plan_review(self.payload, self.body, self.knowledge, ["Summary", "knowledge"])
        self.assert_lossless_plan(plan)
        self.assertGreater(len(plan["batches"]), 1)

    def test_short_source_large_report_uses_bounded_shards_not_a_size_rejection(self):
        self.long_fixture()
        self.source = [self.source[0]]
        write(self.path / "segments.json", self.source)
        self.body = "## Summary\n" + ("Original short-source report paragraph " + "z" * 2950 + ".\n\n") * 12
        write(self.path / "body.md", self.body)
        plan = review.plan_review(self.payload, self.body, self.knowledge, ["Summary", "knowledge"])
        self.assert_lossless_plan(plan)
        self.assertEqual(len(plan["batches"]), 1)
        self.assertGreater(len(plan["shards"]), 2)

    def test_neighboring_role_context_is_complete_and_explicit_when_unavailable(self):
        self.long_fixture(count=6)
        units = review.source_units(self.source)
        batches = review.source_batches(units)
        by_id = {unit["source_id"]: unit for unit in units}
        for batch in batches[1:]:
            first = units.index(batch["units"][0])
            preceding = units[first - 1]
            self.assertTrue(batch["preceding_context_available"])
            self.assertIn(preceding, batch["context_units"])
            self.assertEqual(by_id[preceding["source_id"]]["text"], preceding["text"])
        # Complete 7K segments cannot be paired under the source cap. Their
        # context gap is explicit, never a silently cut invented role anchor.
        big = [{"source_id": f"large-{i}", "parent_id": str(i), "start": i, "end": i + 1,
                "text": "x" * 7000} for i in range(2)]
        batches = review.source_batches(big)
        self.assertFalse(batches[1]["preceding_context_available"])
        self.assertEqual(batches[1]["units"], [big[1]])
        self.assertIn("needs unavailable original context, fail explicitly", review.SCOPED_INSTRUCTION)

    def test_corrupt_validated_progress_cannot_be_reused_or_silently_passed(self):
        self.long_fixture()
        self.run_review()
        path = next((self.path / "semantic-review-progress").glob("*.json"))
        saved = read(path)
        saved["response"]["units"][0]["body_quote"] = "corrupted cached snippet"
        write(path, saved)
        with self.assertRaisesRegex(ValueError, "progress is corrupt or stale"):
            self.run_review()
        self.assertEqual(read(self.path / "semantic-review.json")["status"], "failed")

    def test_exact_eight_k_segment_in_long_source_remains_complete(self):
        self.long_fixture()
        self.source = [{"id": "exact-8k", "start": 0, "end": 5, "text": "a" * 8000},
                       {"id": "another", "start": 5, "end": 10, "text": "Another complete source statement."}]
        write(self.path / "segments.json", self.source)
        plan = review.plan_review(self.payload, self.body, self.knowledge, ["Summary", "knowledge"])
        self.assert_lossless_plan(plan)
        self.assertEqual(plan["units"][0]["source_id"], "s000000.sentence000000")
        self.assertEqual(plan["units"][0]["text"], "a" * 8000)

    def test_small_segment_distinct_qualifier_is_not_covered_by_same_topic_line(self):
        self.long_fixture()
        main = "Alice researcher explains mechanism-0000: feedback required."
        qualification = "Consent is required before mechanism-0000 feedback can be used."
        self.source[0]["text"] = main + " " + qualification + " " + "s" * 2800 + "."
        write(self.path / "segments.json", self.source)
        self.body += "\nFeedback is a recurring topic; feedback matters.\n"
        write(self.path / "body.md", self.body)

        def sentence_grounded(*args):
            response, record = self.respond(*args)
            data = self.calls[-1]
            strings = [text for atom in data["artifact_shard"]["items"] for text in (
                [atom["text"]] if atom["kind"] == "body" else review._string_values(atom["value"]))]
            for row in response["units"]:
                if row["source_id"] == "s000000.sentence000001":
                    present = any(qualification in text for text in strings)
                    row.update(major=True, covered=present, body_quote=qualification if present else "",
                               reason="Explicit consent condition is fully covered" if present else
                               "Major consent qualification is missing; a feedback topic mention does not cover this condition")
            return response, record
        self.invoke.side_effect = sentence_grounded
        plan = review.plan_review(self.payload, self.body, self.knowledge, ["Summary", "knowledge"])
        first = [unit for unit in plan["units"] if unit["parent_id"] == "original-0"]
        self.assertEqual(len(first), 3)
        self.assertEqual("".join(unit["text"] for unit in first), self.source[0]["text"])
        self.assertTrue(all((unit["start"], unit["end"]) == (0.5, 9.5) for unit in first))
        with self.assertRaisesRegex(ValueError, "absent across all artifact shards: s000000.sentence000001"):
            self.run_review()
        audit = read(self.path / "semantic-review.json")
        self.assertTrue(audit["source_coverage"][first[0]["source_id"]]["covered"])
        self.assertFalse(audit["source_coverage"][first[1]["source_id"]]["covered"])
        self.assertTrue(audit["source_coverage"][first[1]["source_id"]]["major"])
        self.assertTrue(all("ALL conditions/qualifications" in call.args[2] for call in self.invoke.call_args_list))
        # A real artifact repair changes its hash and the qualification is then
        # independently covered; no source unit is merged with the main claim.
        self.body += "\n" + qualification + "\n"
        write(self.path / "body.md", self.body)
        self.task["attempts"] = 2
        self.assertEqual(self.run_review()["status"], "passed")

    def test_divisible_source_batch_is_repacked_for_large_indivisible_artifact(self):
        self.long_fixture(count=6)
        self.body = "## Summary\n" + "x" * 15500 + "."
        write(self.path / "body.md", self.body)
        plan = review.plan_review(self.payload, self.body, self.knowledge, ["Summary", "knowledge"])
        self.assert_lossless_plan(plan)
        self.assertGreater(len(plan["batches"]), 3)
        self.assertIn("x" * 15500 + ".", "".join(atom["text"] for shard in plan["shards"]
                                                 for atom in shard["items"] if atom["kind"] == "body"))

    def test_many_small_sentence_ids_account_for_metadata_in_prompt_budget(self):
        self.long_fixture()
        self.source = [{"id": "many", "start": 1, "end": 50, "text": "Alpha. " * 220}]
        self.body = "## Summary\n" + ("b" * 3000 + ".\n\n") * 9
        write(self.path / "segments.json", self.source)
        write(self.path / "body.md", self.body)
        plan = review.plan_review(self.payload, self.body, self.knowledge, ["Summary", "knowledge"])
        self.assert_lossless_plan(plan)
        self.assertGreater(len(plan["batches"]), 1)

    def test_pass_is_hash_bound_complete_source_and_same_private_config(self):
        audit = self.run_review()
        self.assertEqual(audit["status"], "passed")
        self.assertEqual(audit["artifact_hash"], output_hash(self.task))
        self.assertEqual(audit["input_hash"], "input")
        self.assertEqual(audit["reviewer"]["config_hash"], digest(read(self.config)))
        args = self.invoke.call_args.args
        self.assertEqual(args[3:6], ("provider/current", str(self.config), 600))
        self.assertLessEqual(len(args[2].encode("utf-8")), 23000)
        data = json.loads(args[2].split("\nINPUT DATA:\n")[1])
        self.assertEqual(data["original_source"], self.source)
        self.assertEqual(data["knowledge"], {"insights": []})
        self.assertIn("untrusted data", args[2])

    def test_rejection_only_invalidates_named_part_and_appends_supported_findings(self):
        finding = {"part": "Summary", "reason": "Missing essential feedback mechanism",
                   "source_quote": "the mechanism requires feedback"}
        self.verdict.update(status="failed", reason="Material feedback mechanism missing", findings=[finding])
        write(self.path / "semantic-repair.json", {"findings": [finding]})
        with self.assertRaisesRegex(ValueError, "Semantic review failed"):
            self.run_review()
        self.assertFalse((self.part(self.requests[0]) / "validated.json").exists())
        self.assertTrue((self.part(self.requests[1]) / "validated.json").exists())
        repair = read(self.path / "semantic-repair.json")
        self.assertEqual(repair["findings"], [finding, finding])
        self.assertEqual(read(self.path / "semantic-review.json")["status"], "failed")

    def test_unsupported_or_unknown_part_findings_do_not_delete_caches(self):
        for part, quote in (("Summary", "invented source quotation"), ("Other", "42 observations")):
            self.verdict.update(status="failed", reason="Unsupported finding rejected",
                                findings=[{"part": part, "reason": "Correct this unsupported claim", "source_quote": quote}])
            with self.assertRaisesRegex(ValueError, "source-supported"):
                self.run_review()
            self.assertTrue(all((self.part(r) / "validated.json").exists() for r in self.requests))
        self.assertFalse((self.path / "semantic-repair.json").exists())

    def test_same_missing_and_previous_reviewer_sessions_fail_closed(self):
        write(self.path / "reviews/attempt-0/worker-run.json", {"sessionId": "previous-review"})
        for session in ("producer", None, "previous-review"):
            self.record["sessionId"] = session
            with self.subTest(session=session), self.assertRaisesRegex(ValueError, "fresh non-producer"):
                self.run_review()
            self.assertEqual(read(self.path / "semantic-review.json")["status"], "failed")

    def test_wrong_model_rejected(self):
        self.record["model"] = "fallback"
        with self.assertRaisesRegex(ValueError, "exact current model"):
            self.run_review()

    def test_config_without_denied_tools_stops_before_call(self):
        write(self.config, {"tools": {"deny": []}})
        with self.assertRaisesRegex(ValueError, "tools.deny"):
            self.run_review()
        self.invoke.assert_not_called()

    def test_changed_artifact_and_changed_config_during_review_rejected(self):
        for target, value in ((self.path / "body.md", "changed body"),
                              (self.config, {"tools": {"deny": ["*"]}, "changed": True}),
                              (self.path / "segments.json", [{"start": 0, "end": 10, "text": "changed source"}])):
            def mutate(*args):
                write(target, value)
                return copy.deepcopy(self.verdict), copy.deepcopy(self.record)
            self.invoke.side_effect = mutate
            with self.assertRaisesRegex(ValueError, "changed during audit"):
                self.run_review()

    def test_indivisible_source_and_artifact_block_without_any_call(self):
        write(self.path / "segments.json", [{"start": 0, "end": 10, "text": "source" * 1500}])
        with self.assertRaisesRegex(ValueError, "unfit indivisible source sentence"):
            self.run_review()
        write(self.path / "segments.json", self.source)
        write(self.path / "body.md", "证" * 8000)
        with self.assertRaisesRegex(ValueError, "23000 UTF-8 bytes"):
            self.run_review()
        self.invoke.assert_not_called()

    def test_exact_eight_k_source_is_reviewed_whole_without_cutting(self):
        source = [{"start": 0, "end": 10, "text": "a" * 8000}]
        write(self.path / "segments.json", source)
        prompt, segments = review.review_prompt(self.payload, "body", {"insights": []}, ["Summary"])
        self.assertEqual(segments, source)
        self.assertEqual(json.loads(prompt.split("\nINPUT DATA:\n")[1])["original_source"], source)

    def test_incomplete_or_contradictory_verdict_never_passes(self):
        for update in ({"findings": None}, {"reason": "OK"}, {"approved_name_translations": ["Alice"]},
                       {"findings": [{"part": "Summary", "reason": "Mechanism omitted from summary",
                                      "source_quote": "requires feedback"}]}):
            original = copy.deepcopy(self.verdict)
            self.verdict.update(update)
            with self.assertRaises(ValueError):
                self.run_review()
            self.verdict = original
            self.assertEqual(read(self.path / "semantic-review.json")["status"], "failed")


if __name__ == "__main__":
    unittest.main()
