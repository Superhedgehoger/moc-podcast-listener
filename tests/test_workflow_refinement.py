"""Synthetic disk-only refinement checks; no model or network calls."""
import copy
import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import refine_workflow as refinement
import summary_workflow as workflow
from reference_registry import expand_refs, expand_reduction, load_registry


class RefinementTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="refinement-synthetic-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.result_path = self.root / "result.json"
        self.result = {"episode_dir": str(self.root / "episode"),
                       "transcript_path": str(self.root / "transcript.txt"),
                       "segments_path": str(self.root / "segments.json"),
                       "metadata_path": str(self.root / "metadata.json"),
                       "report_path": str(self.root / "report.md")}
        workflow.write(self.result_path, self.result)
        workflow.write(self.result["transcript_path"], "Synthetic source only.\n")
        self.segments = [{"id": f"supplied/source-{i}", "start": i * 2, "end": i * 2 + 2,
                          "text": f"Synthetic primary {i}.", "speaker": "unverified",
                          "extra": {"source_index": i}} for i in range(48)]
        workflow.write(self.result["segments_path"], self.segments)
        workflow.write(self.result["metadata_path"], {"episode": {"duration_minutes": 1, "source": "synthetic"}})
        workflow.write(self.result["report_path"], "Untouched synthetic report.\n")
        self.base = workflow.workflow_dir(self.result)
        self.settings = {"target_tokens": 8000, "synthesis_tokens": 24000}
        fingerprint, _ = workflow.source(self.result)
        generation_id = workflow.digest([fingerprint, self.settings, workflow.PIPELINE_VERSION])[:20]
        self.generation = self.base / generation_id
        self.initial = {"version": workflow.PIPELINE_VERSION, "source_hash": fingerprint,
                        "generation": generation_id, "settings": self.settings,
                        "tasks": {}, "levels": [[]], "created_at": 123, "assembled": None}
        workflow.write(self.generation / "result.json", self.result)
        self.protected = {path: Path(path).read_bytes() for path in
                          (str(self.result_path), self.result["transcript_path"],
                           self.result["segments_path"], self.result["metadata_path"],
                           self.result["report_path"])}

    def state(self):
        return workflow.read(self.base / "state.json")

    def commit(self):
        workflow.write(self.generation / "chunks.json", [
            {"task_id": key, "input": self.initial["tasks"][key]["input"],
             "input_hash": self.initial["tasks"][key]["input_hash"]}
            for key in self.initial["levels"][0]])
        workflow.persist(self.base, self.initial)

    def extract(self, key, segments, *, context=None, complete=False, attempts=0):
        source_hash = hashlib.sha256("\n".join(s["text"] for s in segments).encode()).hexdigest()
        payload = {"id": f"chunk-{key}", "source_hash": source_hash,
                   "segments": copy.deepcopy(segments), "context_segments": context or [],
                   "estimated_tokens": 500, "output_budget": 8000,
                   "evidence_content_budget": 6000, "worker_contract": "source_selection_v2"}
        task = workflow.add_task(self.initial, self.generation, key, "extract", payload)
        self.initial["levels"][0].append(key)
        task["attempts"] = attempts
        if complete:
            self.finish_extract(task)
        return task

    def finish_extract(self, task):
        payload = workflow.read(task["input"])
        output = {"chunk_id": payload["id"], "source_hash": payload["source_hash"],
                  "covered_segment_ids": [s["id"] for s in payload["segments"]],
                  "items": [{"id": f"item-{i}", "claim": f"Synthetic claim {i}",
                             "quote": s["text"], "segment_ids": [s["id"]],
                             "topics": ["Test"], "examples": [], "numbers": [],
                             "limitations": [], "ambiguities": []}
                            for i, s in enumerate(payload["segments"])]}
        workflow.write(task["output"], output)
        workflow.validate_task(task)
        task.update(status="complete", output_hash=workflow.output_hash(task), error=None)

    def exhaust(self, task):
        task.update(status="blocked", attempts=3, error="Synthetic exhausted task",
                    started_at=10, finished_at=20)
        directory = Path(task["input"]).parent
        for i in (1, 2):
            workflow.write(directory / f"attempt-{i}" / "worker-run.json", {"attempt": i, "error": "fixture"})
            workflow.write(directory / f"attempt-{i}" / "output.json", {"incomplete": i})
        workflow.write(directory / "worker-run.json", {"attempt": 3, "error": "fixture"})
        workflow.write(task["output"], {"invalid": True})
        workflow.write(directory / "task.json", task)

    def canonical(self, task):
        output = workflow.validate_task(task)
        evidence = output
        if task["kind"] == "extract":
            evidence = {"items": [{**{k: v for k, v in item.items() if k not in {"id", "segment_ids"}},
                                   "evidence_ids": [f"{task['id']}:{item['id']}"]} for item in output["items"]]}
        return {"task_id": task["id"], "leaf_ids": workflow.leaves(task, output), "evidence": evidence}

    def reduce(self, key, tasks, *, complete=False, grouped=False):
        entries = [self.canonical(t) for t in tasks]
        payload = {**workflow.registered_payload(entries, self.generation, key), "output_budget": 6000,
                   "worker_reduce_contract": "topic_jsonl_records_v1",
                   "dependencies": {t["id"]: t["output_hash"] for t in tasks}}
        task = workflow.add_task(self.initial, self.generation, key, "reduce", payload)
        if complete:
            nodes = payload["leaf_ids"]
            output = {"input_hash": task["input_hash"], "items": [], "omitted": []}
            if grouped:
                output["items"] = [{"claim": "Grouped synthetic evidence", "details": "All original details",
                                    "evidence_ids": nodes[:-1]}]
                output["omitted"] = [{"evidence_id": nodes[-1], "reason": "Synthetic explicit omission"}]
            else:
                output["items"] = [{"claim": f"Synthetic reduction {i}", "details": "Synthetic details",
                                    "evidence_ids": [node]} for i, node in enumerate(nodes)]
            workflow.write(task["output"], output)
            workflow.validate_task(task)
            task.update(status="complete", attempts=2, output_hash=workflow.output_hash(task))
        return task

    def assert_protected(self):
        for path, content in self.protected.items():
            self.assertEqual(Path(path).read_bytes(), content)

    def files(self, directory):
        return {str(p.relative_to(directory)): p.read_bytes() for p in directory.rglob("*") if p.is_file()}

    def assert_archive(self, old, receipt, artifacts):
        self.assertEqual(workflow.read(receipt["snapshot"]), old)
        self.assertEqual(workflow.read(self.generation / "state-snapshot.json"), old)
        ledger = workflow.read(receipt["retry_ledger"])
        self.assertEqual(ledger["tasks"], old["tasks"])
        for key, previous in old["tasks"].items():
            directory = Path(previous["input"]).parent
            self.assertEqual(self.files(directory), artifacts[key])

    def assert_clone(self, old, new, key):
        previous, task = old["tasks"][key], new["tasks"][key]
        self.assertEqual(workflow.read(previous["input"]), workflow.read(task["input"]))
        self.assertEqual(Path(previous["input"]).read_bytes(), Path(task["input"]).read_bytes())
        for field in ("status", "attempts", "input_hash", "output_hash", "error"):
            self.assertEqual(task.get(field), previous.get(field))
        self.assertNotEqual(task["input"], previous["input"])
        self.assertEqual(task["workspace"], str(self.base / new["generation"]))
        instruction = Path(task["instruction"]).read_text()
        self.assertIn(new["generation"], instruction)
        self.assertNotIn(old["generation"], instruction)
        self.assertEqual(workflow.read(Path(task["input"]).with_name("task.json")), task)
        if task["status"] == "complete":
            self.assertEqual(Path(task["output"]).read_bytes(), Path(previous["output"]).read_bytes())
            workflow.validate_task(task)

    def test_extract_exact_partition_context_reuse_ledger_and_idempotence(self):
        before = self.extract("extract-0000", self.segments[:2], complete=True, attempts=2)
        context = copy.deepcopy(self.segments[1:2])
        parent = self.extract("extract-0001", self.segments[2:10], context=context)
        after = self.extract("extract-0002", self.segments[10:12], complete=True, attempts=1)
        blocked = self.extract("extract-0003", self.segments[12:14])
        pending = self.extract("extract-0004", self.segments[14:16], attempts=2)
        self.exhaust(parent)
        self.exhaust(blocked)
        self.commit()
        old = self.state()
        artifacts = {key: self.files(Path(t["input"]).parent) for key, t in old["tasks"].items()}
        with patch.object(workflow, "status", side_effect=AssertionError("No implicit scheduling")):
            receipt = refinement.refine(self.result_path, parent["id"])
        new = self.state()
        self.assertNotEqual(old["generation"], new["generation"])
        self.assertNotIn(parent["id"], new["tasks"])
        self.assertEqual(new["superseded_tasks"][parent["id"]], old["tasks"][parent["id"]])
        self.assertEqual(new["levels"][0], [before["id"], *receipt["children"], after["id"], blocked["id"], pending["id"]])
        parts = [workflow.read(new["tasks"][key]["input"]) for key in receipt["children"]]
        self.assertEqual([s for part in parts for s in part["segments"]], self.segments[2:10])
        ids = [s["id"] for part in parts for s in part["segments"]]
        self.assertEqual(len(ids), len(set(ids)))
        for i, part in enumerate(parts):
            self.assertEqual(part["context_segments"][:len(context)], context)
            if i:
                self.assertIn(parts[i - 1]["segments"][-1], part["context_segments"])
            self.assertLess(workflow.tokens(part), workflow.tokens(workflow.read(parent["input"])))
            self.assertLessEqual(workflow.tokens(part), 8000)
        for key in (before["id"], after["id"], blocked["id"], pending["id"]):
            self.assert_clone(old, new, key)
        self.assertFalse(Path(new["tasks"][blocked["id"]]["output"]).exists())
        manifest = workflow.read(self.base / new["generation"] / "chunks.json")
        self.assertEqual([row["task_id"] for row in manifest], new["levels"][0])
        for row in manifest:
            self.assertEqual(row["input"], new["tasks"][row["task_id"]]["input"])
            self.assertEqual(row["input_hash"], workflow.digest(workflow.read(row["input"])))
        self.assert_archive(old, receipt, artifacts)
        disk = self.files(self.base)
        self.assertEqual(refinement.refine(self.result_path, parent["id"]), receipt)
        self.assertEqual(self.files(self.base), disk)
        for key in receipt["children"]:
            self.assertEqual(new["tasks"][key]["attempts"], 0)
            self.finish_extract(new["tasks"][key])
            workflow.validate_task(new["tasks"][key])
        self.assert_protected()

    def test_reduce_contiguous_entry_and_leaf_partition_with_validated_children(self):
        tasks = [self.extract(f"extract-{i:04d}", self.segments[i * 2:i * 2 + 2],
                              complete=True, attempts=1) for i in range(16)]
        sibling = self.reduce("reduce-01-0000", tasks[:2], complete=True)
        parent = self.reduce("reduce-01-0001", tasks[2:])
        self.exhaust(parent)
        self.initial["levels"].append([sibling["id"], parent["id"]])
        self.commit()
        old = self.state()
        artifacts = {key: self.files(Path(t["input"]).parent) for key, t in old["tasks"].items()}
        original = workflow.read(parent["input"])
        receipt = refinement.refine(self.result_path, parent["id"])
        new = self.state()
        self.assertEqual(new["levels"][1], [sibling["id"], *receipt["children"]])
        children = [workflow.read(new["tasks"][key]["input"]) for key in receipt["children"]]
        self.assertEqual([e["task_id"] for child in children for e in child["entries"]],
                         [e["task_id"] for e in original["entries"]])
        expected = expand_refs(original, original["leaf_ids"])
        actual = [ref for child in children for ref in expand_refs(child, child["leaf_ids"])]
        self.assertEqual(actual, expected)
        self.assertEqual(len(actual), len(set(actual)))
        for key, payload in zip(receipt["children"], children):
            self.assertLessEqual(len(payload["leaf_ids"]), 12)
            self.assertLessEqual(workflow.tokens(payload), 12000)
            self.assertLessEqual(Path(new["tasks"][key]["input"]).stat().st_size, 12000)
            self.assertLess(workflow.tokens(payload), workflow.tokens(original))
            self.assertIn(new["generation"], payload["reference_registry"]["path"])
            load_registry(payload)
            child = new["tasks"][key]
            workflow.write(child["output"], {"input_hash": child["input_hash"], "items": [
                {"claim": "Synthetic child", "details": "All details retained", "evidence_ids": payload["leaf_ids"]}],
                "omitted": []})
            validated = workflow.validate_task(child)
            self.assertEqual(validated["items"][0]["evidence_ids"], expand_refs(payload, payload["leaf_ids"]))
        for task in tasks + [sibling]:
            self.assert_clone(old, new, task["id"])
        self.assertEqual(workflow.read(new["tasks"][sibling["id"]]["input"])["reference_registry"],
                         workflow.read(sibling["input"])["reference_registry"])
        self.assert_archive(old, receipt, artifacts)
        self.assert_protected()

    def test_nested_packed_reductions_expand_then_repack_without_false_provenance(self):
        extracts = [self.extract(f"extract-{i:04d}", self.segments[i * 3:i * 3 + 3],
                                 complete=True) for i in range(8)]
        reductions = [self.reduce(f"reduce-01-{i:04d}", extracts[i * 2:i * 2 + 2],
                                  complete=True, grouped=True) for i in range(4)]
        self.initial["levels"].append([t["id"] for t in reductions])
        parent = self.reduce("reduce-02-0000", reductions)
        self.exhaust(parent)
        self.initial["levels"].append([parent["id"]])
        self.commit()
        original = workflow.read(parent["input"])
        receipt = refinement.refine(self.result_path, parent["id"])
        new = self.state()
        all_leaves, expanded_entries = [], []
        for key in receipt["children"]:
            payload = workflow.read(new["tasks"][key]["input"])
            all_leaves.extend(expand_refs(payload, payload["leaf_ids"]))
            for entry in payload["entries"]:
                expanded_entries.append(expand_reduction(payload, entry["evidence"]))
        self.assertEqual(expanded_entries, [expand_reduction(original, e["evidence"]) for e in original["entries"]])
        self.assertEqual(all_leaves, expand_refs(original, original["leaf_ids"]))
        self.assertEqual(len(all_leaves), len(set(all_leaves)))
        self.assertTrue(all(leaf.startswith("extract-") and ":item-" in leaf for leaf in all_leaves))
        self.assertEqual([len(entry["items"][0]["evidence_ids"]) for entry in expanded_entries], [5] * 4)
        self.assertEqual([len(entry["omitted"]) for entry in expanded_entries], [1] * 4)
        for task in reductions:
            self.assertEqual(new["tasks"][task["id"]]["attempts"], 2)
        self.assert_protected()

    def test_legacy_writer_settings_and_31_long_sources_survive_reduce_refinement(self):
        legacy_writer = "verbatim_name_evidence_pieces_v4"
        self.settings["writer_format"] = legacy_writer
        for segment in self.segments[:31]:
            segment["text"] += " Synthetic long source. " + "x" * 6000
        workflow.write(self.result["segments_path"], self.segments)
        workflow.write(self.result["transcript_path"], "\n".join(s["text"] for s in self.segments))
        self.protected = {path: Path(path).read_bytes() for path in self.protected}
        fingerprint, _ = workflow.source(self.result)
        generation_id = workflow.digest([fingerprint, self.settings, workflow.PIPELINE_VERSION])[:20]
        self.generation = self.base / generation_id
        self.initial.update(source_hash=fingerprint, generation=generation_id)
        workflow.write(self.generation / "result.json", self.result)
        sources = [self.extract(f"extract-{i:04d}", self.segments[i:i + 1],
                                context=self.segments[i - 1:i] if i else [],
                                complete=True, attempts=1 + i % 3) for i in range(31)]
        self.assertTrue(all(Path(t["input"]).stat().st_size > refinement.MAX_INPUT_BYTES
                            for t in sources[1:]))
        sibling = self.reduce("reduce-01-0000", sources[:2], complete=True)
        parent = self.reduce("reduce-01-0001", sources[2:])
        self.exhaust(parent)
        self.initial["levels"].append([sibling["id"], parent["id"]])
        self.commit()
        old = self.state()
        receipt = refinement.refine(self.result_path, parent["id"])
        current = self.state()
        for task in sources + [sibling]:
            self.assert_clone(old, current, task["id"])
        original = workflow.read(parent["input"])
        leaves = []
        for key in receipt["children"]:
            task = current["tasks"][key]
            payload = workflow.read(task["input"])
            self.assertLessEqual(Path(task["input"]).stat().st_size, refinement.MAX_INPUT_BYTES)
            self.assertLessEqual(len(payload["leaf_ids"]), refinement.MAX_REFERENCE_NODES)
            leaves.extend(expand_refs(payload, payload["leaf_ids"]))
        self.assertEqual(leaves, expand_refs(original, original["leaf_ids"]))
        self.assertEqual(current["settings"]["writer_format"], "chinese_markdown_independent_parts_v6")
        self.assertEqual({k: v for k, v in current["settings"].items() if k != "refinement_plan"},
                         {**old["settings"], "writer_format": "chinese_markdown_independent_parts_v6"})
        v5_settings = {**self.settings, "writer_format": "chinese_markdown_independent_parts_v6"}
        with self.assertRaisesRegex(ValueError, "settings changed"):
            refinement.retain_refinement_plan(current, fingerprint, self.settings)
        retained = refinement.retain_refinement_plan(current, fingerprint, v5_settings)
        self.assertEqual(retained, current["settings"])
        self.assertEqual(workflow.digest([fingerprint, retained, workflow.PIPELINE_VERSION])[:20], current["generation"])
        disk = self.files(self.base)
        self.assertEqual(refinement.refine(self.result_path, parent["id"]), receipt)
        self.assertEqual(self.files(self.base), disk)
        self.assert_protected()

    def test_prepare_compatibility_hook_keeps_deterministic_generation(self):
        parent = self.extract("extract-0000", self.segments[:8])
        self.exhaust(parent)
        self.commit()
        refinement.refine(self.result_path, parent["id"])
        old = self.state()
        settings = refinement.retain_refinement_plan(old, old["source_hash"], self.settings)
        self.assertEqual(settings, old["settings"])
        self.assertNotIn("refinement_plan", self.settings)
        self.assertEqual(workflow.digest([old["source_hash"], settings, workflow.PIPELINE_VERSION])[:20], old["generation"])
        with self.assertRaisesRegex(ValueError, "settings changed"):
            refinement.retain_refinement_plan(old, old["source_hash"], {**self.settings, "target_tokens": 9999})
        self.assertEqual(refinement.retain_refinement_plan(old, "changed-source", self.settings), self.settings)

    def test_oversized_completed_entry_splits_only_whole_topics_and_revalidates(self):
        first = self.extract("extract-0000", self.segments[:2], complete=True)
        second = self.extract("extract-0001", self.segments[2:18], complete=True)
        reduced = self.reduce("reduce-01-0000", [second], complete=True)
        parent = self.reduce("reduce-02-0000", [first, reduced])
        self.exhaust(parent)
        self.initial["levels"].append([reduced["id"]])
        self.initial["levels"].append([parent["id"]])
        self.commit()
        original = workflow.read(parent["input"])
        expected = refinement._canonical_entries(original, self.state())
        receipt = refinement.refine(self.result_path, parent["id"])
        current = self.state()
        actual, leaves = [], []
        for key in receipt["children"]:
            payload = workflow.read(current["tasks"][key]["input"])
            entries = refinement._canonical_entries(payload, current)
            actual.extend(item for entry in entries for item in entry["evidence"]["items"])
            leaves.extend(expand_refs(payload, payload["leaf_ids"]))
            self.assertLessEqual(len(payload["leaf_ids"]), refinement.MAX_REFERENCE_NODES)
            self.assertEqual(len({e["task_id"] for e in entries}), len(entries))
        self.assertEqual(actual, [item for entry in expected for item in entry["evidence"]["items"]])
        self.assertEqual(leaves, expand_refs(original, original["leaf_ids"]))
        self.assertEqual(len(leaves), len(set(leaves)))
        payload["entries"][0]["evidence"]["items"][0]["claim"] = "Invented replacement"
        with self.assertRaisesRegex(ValueError, "canonical evidence"):
            refinement._canonical_entries(payload, current)
        self.assert_protected()

    def test_overlapping_topic_members_cannot_be_split_across_children(self):
        source = self.extract("extract-0000", self.segments[:16], complete=True)
        reduced = self.reduce("reduce-01-0000", [source], complete=True)
        output = workflow.read(reduced["output"])
        output["items"][0]["evidence_ids"] = workflow.read(reduced["input"])["leaf_ids"]
        workflow.write(reduced["output"], output)
        workflow.validate_task(reduced)
        reduced["output_hash"] = workflow.output_hash(reduced)
        parent = self.reduce("reduce-02-0000", [reduced])
        self.exhaust(parent)
        self.initial["levels"].append([reduced["id"]])
        self.initial["levels"].append([parent["id"]])
        self.commit()
        self.assert_refusal("overlapping canonical topics", parent["id"])

    def test_two_whole_segments_can_be_refined_with_full_context(self):
        parent = self.extract("extract-0000", self.segments[1:3], context=self.segments[:1])
        self.exhaust(parent)
        self.commit()
        receipt = refinement.refine(self.result_path, parent["id"])
        current = self.state()
        self.assertEqual(len(receipt["children"]), 2)
        for i, key in enumerate(receipt["children"]):
            payload = workflow.read(current["tasks"][key]["input"])
            self.assertEqual(payload["segments"], self.segments[i + 1:i + 2])
            self.assertEqual(payload["context_segments"][:1], self.segments[:1])
            self.assertLessEqual(Path(current["tasks"][key]["input"]).stat().st_size, 8000)

    def test_real_prepared_workflow_advances_with_refined_children_and_lookup(self):
        workflow.prepare(self.result_path, target_tokens=4000)
        original = self.state()
        parent = original["tasks"][original["levels"][0][0]]
        self.exhaust(parent)
        sibling = original["tasks"][original["levels"][0][1]]
        sibling["attempts"] = 2
        self.finish_extract(sibling)
        workflow.persist(self.base, original)
        receipt = refinement.refine(self.result_path, parent["id"])
        current = self.state()
        retained = refinement.retain_refinement_plan(current, current["source_hash"], original["settings"])
        resumed = workflow.prepare(self.result_path, target_tokens=4000)
        self.assertEqual(resumed["generation"], current["generation"])
        self.assertEqual(self.state()["superseded_tasks"][parent["id"]]["attempts"], 3)
        self.assertEqual(workflow.digest([current["source_hash"], retained, workflow.PIPELINE_VERSION])[:20], receipt["generation"])
        for key in current["levels"][0]:
            task = current["tasks"][key]
            if task["status"] != "complete":
                self.finish_extract(task)
        workflow.persist(self.base, current)
        progress = workflow.status(self.result_path)
        self.assertEqual(progress["generation"], receipt["generation"])
        current = self.state()
        self.assertEqual(current["levels"][-1], ["write"])
        writer = workflow.read(current["tasks"]["write"]["input"])
        original_refs = expand_refs(writer, writer["leaf_ids"])
        self.assertFalse(any(ref.startswith(parent["id"] + ":") for ref in original_refs))
        child_id = receipt["children"][0]
        located = workflow.locate(self.result_path, child_id + ":item-0")
        self.assertEqual(located["source_chunk"], current["tasks"][child_id]["input"])
        self.assertEqual(current["tasks"][sibling["id"]]["attempts"], 2)
        self.assert_protected()

    def test_reduce_serialized_byte_bound_splits_large_contiguous_entries(self):
        tasks = [self.extract(f"extract-{i:04d}", self.segments[i:i + 1], complete=True) for i in range(10)]
        for task in tasks:
            output = workflow.read(task["output"])
            output["items"][0]["claim"] = "Synthetic evidence detail " + "x" * 3500
            workflow.write(task["output"], output)
            workflow.validate_task(task)
            task["output_hash"] = workflow.output_hash(task)
        parent = self.reduce("reduce-01-0000", tasks)
        self.exhaust(parent)
        self.initial["levels"].append([parent["id"]])
        self.commit()
        receipt = refinement.refine(self.result_path, parent["id"])
        current = self.state()
        self.assertGreater(len(receipt["children"]), 2)
        flattened = []
        for key in receipt["children"]:
            task = current["tasks"][key]
            self.assertLessEqual(Path(task["input"]).stat().st_size, 12000)
            payload = workflow.read(task["input"])
            self.assertLessEqual(len(payload["leaf_ids"]), 12)
            flattened.extend(expand_refs(payload, payload["leaf_ids"]))
        original = workflow.read(parent["input"])
        self.assertEqual(flattened, expand_refs(original, original["leaf_ids"]))

    def test_successive_refinements_keep_all_superseded_attempts(self):
        parent = self.extract("extract-0000", self.segments[:8])
        self.exhaust(parent)
        self.commit()
        first = refinement.refine(self.result_path, parent["id"])
        state = self.state()
        child = state["tasks"][first["children"][0]]
        self.exhaust(child)
        workflow.persist(self.base, state)
        second = refinement.refine(self.result_path, child["id"])
        current = self.state()
        self.assertNotEqual(first["generation"], second["generation"])
        self.assertEqual(len(current["settings"]["refinement_plan"]), 2)
        for key in (parent["id"], child["id"]):
            self.assertNotIn(key, current["tasks"])
            self.assertEqual(current["superseded_tasks"][key]["attempts"], 3)
        self.assertEqual(workflow.read(second["snapshot"])["superseded_tasks"][parent["id"]]["attempts"], 3)
        self.assertEqual(refinement.refine(self.result_path, parent["id"])["children"], first["children"])

    def assert_refusal(self, message, task_id):
        old = self.state()
        generations = {p.name for p in self.base.iterdir() if p.is_dir()}
        with self.assertRaisesRegex((ValueError, OSError), message):
            refinement.refine(self.result_path, task_id)
        self.assertEqual(self.state(), old)
        self.assertEqual({p.name for p in self.base.iterdir() if p.is_dir()}, generations)

    def test_refuses_wrong_kind_status_attempts_running_and_inactive_tasks(self):
        parent = self.extract("extract-0000", self.segments[:8])
        self.exhaust(parent)
        self.commit()
        valid = copy.deepcopy(self.initial)
        for changes in ({"kind": "write"}, {"status": "pending"}, {"status": "complete"},
                        {"attempts": 0}, {"attempts": 1}, {"attempts": 2}, {"attempts": 4}, {"attempts": True}):
            with self.subTest(changes=changes):
                state = copy.deepcopy(valid)
                state["tasks"][parent["id"]].update(changes)
                workflow.persist(self.base, state)
                self.assert_refusal("blocked extract/reduce", parent["id"])
        state = copy.deepcopy(valid)
        state["tasks"][parent["id"]]["status"] = "running"
        workflow.persist(self.base, state)
        self.assert_refusal("running tasks", parent["id"])
        state = copy.deepcopy(valid)
        state["tasks"]["other"] = {**copy.deepcopy(parent), "id": "other", "status": "pending"}
        state["levels"] = [["other"], [parent["id"]]]
        workflow.persist(self.base, state)
        self.assert_refusal("active level", parent["id"])
        workflow.persist(self.base, valid)
        self.assert_refusal("blocked extract/reduce", "unknown")

    def test_source_input_and_completed_output_tampering_refused(self):
        sibling = self.extract("extract-0000", self.segments[:2], complete=True)
        parent = self.extract("extract-0001", self.segments[2:10])
        self.exhaust(parent)
        self.commit()
        for field in ("transcript_path", "segments_path"):
            path = Path(self.result[field])
            original = path.read_text()
            workflow.write(path, "changed" if field == "transcript_path" else self.segments[:-1])
            self.assert_refusal("Source changed", parent["id"])
            workflow.write(path, original)
        payload = workflow.read(parent["input"])
        workflow.write(parent["input"], {**payload, "output_budget": 9999})
        self.assert_refusal("Task input changed", parent["id"])
        workflow.write(parent["input"], payload)
        output = workflow.read(sibling["output"])
        output["items"][0]["claim"] = "Different but syntactically valid claim"
        workflow.write(sibling["output"], output)
        self.assert_refusal("output fingerprint changed", parent["id"])

    def test_indivisible_extract_or_full_context_refused(self):
        parent = self.extract("extract-0000", self.segments[:1])
        self.exhaust(parent)
        self.commit()
        self.assert_refusal("indivisible", parent["id"])
        payload = workflow.read(parent["input"])
        payload["segments"] = self.segments[:2]
        payload["context_segments"] = [{**self.segments[5], "text": "x" * 12000}]
        workflow.write(parent["input"], payload)
        self.initial["tasks"][parent["id"]]["input_hash"] = workflow.digest(payload)
        self.commit()
        self.assert_refusal("full context exceeds", parent["id"])

    def test_indivisible_reduce_and_oversized_entry_refused(self):
        first = self.extract("extract-0000", self.segments[:1], complete=True)
        second = self.extract("extract-0001", self.segments[1:3], complete=True)
        parent = self.reduce("reduce-01-0000", [first])
        self.exhaust(parent)
        self.initial["levels"].append([parent["id"]])
        self.commit()
        self.assert_refusal("[Ii]ndivisible canonical", parent["id"])
        replacement = {**workflow.registered_payload([self.canonical(first), self.canonical(second)], self.generation, "replacement"),
                       "output_budget": 6000, "dependencies": {first["id"]: first["output_hash"], second["id"]: second["output_hash"]}}
        workflow.write(parent["input"], replacement)
        parent["input_hash"] = workflow.digest(replacement)
        self.commit()
        with patch.object(refinement, "MAX_INPUT_BYTES", 250):
            self.assert_refusal("Indivisible canonical source entry/topic exceeds", parent["id"])

    def test_registry_and_canonical_provenance_tampering_refused(self):
        tasks = [self.extract(f"extract-{i:04d}", self.segments[i * 2:i * 2 + 2], complete=True) for i in range(4)]
        parent = self.reduce("reduce-01-0000", tasks)
        self.exhaust(parent)
        self.initial["levels"].append([parent["id"]])
        self.commit()
        payload = workflow.read(parent["input"])
        original_payload = copy.deepcopy(payload)
        pointer = payload["reference_registry"]
        registry = workflow.read(pointer["path"])
        changed = copy.deepcopy(registry)
        changed["r0"] = ["invented:leaf"]
        workflow.write(pointer["path"], changed)
        self.assert_refusal("registry changed", parent["id"])
        workflow.write(pointer["path"], registry)
        payload["entries"][0]["evidence"]["items"][0]["claim"] = "Fabricated source entry"
        workflow.write(parent["input"], payload)
        parent["input_hash"] = workflow.digest(payload)
        self.commit()
        self.assert_refusal("differs from validated canonical", parent["id"])
        payload = original_payload
        swapped = copy.deepcopy(registry)
        swapped["r0"], swapped["r1"] = swapped["r1"], swapped["r0"]
        workflow.write(pointer["path"], swapped)
        payload["reference_registry"]["sha256"] = workflow.digest(swapped)
        workflow.write(parent["input"], payload)
        parent["input_hash"] = workflow.digest(payload)
        self.commit()
        self.assert_refusal("differs from validated canonical", parent["id"])

    def test_unrelated_running_task_refuses_even_idempotent_request(self):
        parent = self.extract("extract-0000", self.segments[:8])
        sibling = self.extract("extract-0001", self.segments[8:10])
        self.exhaust(parent)
        sibling["status"] = "running"
        self.commit()
        self.assert_refusal("running tasks", parent["id"])
        sibling["status"] = "pending"
        self.commit()
        refinement.refine(self.result_path, parent["id"])
        state = self.state()
        state["tasks"][sibling["id"]]["status"] = "running"
        workflow.persist(self.base, state)
        self.assert_refusal("running tasks", parent["id"])

    def test_clone_failure_rolls_back_unpublished_generation(self):
        self.extract("extract-0000", self.segments[:2], complete=True)
        parent = self.extract("extract-0001", self.segments[2:10])
        self.exhaust(parent)
        self.commit()
        with patch.object(refinement.shutil, "copy2", side_effect=OSError("Synthetic disk failure")):
            self.assert_refusal("Synthetic disk failure", parent["id"])
        receipt = refinement.refine(self.result_path, parent["id"])
        self.assertNotEqual(receipt["generation"], self.initial["generation"])
        self.assert_protected()

    def test_existing_downstream_and_concurrent_changes_refused(self):
        parent = self.extract("extract-0000", self.segments[:8])
        self.exhaust(parent)
        downstream = workflow.add_task(self.initial, self.generation, "write", "write",
                                       {"dependencies": {parent["id"]: "old"}})
        self.initial["levels"].append([downstream["id"]])
        self.commit()
        self.assert_refusal("downstream tasks", parent["id"])
        del self.initial["tasks"]["write"]
        self.initial["levels"].pop()
        self.commit()
        real_load = workflow.load
        count = 0

        def changing_load(path):
            nonlocal count
            result, base, state = real_load(path)
            count += 1
            if count == 2:
                state["concurrent_change"] = True
            return result, base, state

        with patch.object(workflow, "load", side_effect=changing_load):
            self.assert_refusal("changed during refinement", parent["id"])

    def test_cli_refine_and_required_task(self):
        parent = self.extract("extract-0000", self.segments[:8])
        self.exhaust(parent)
        self.commit()
        cli = Path(workflow.__file__).with_name("podcast-summary.py")
        command = [sys.executable, str(cli), "refine", str(self.result_path)]
        missing = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(missing.returncode, 2)
        self.assertIn("--task required", missing.stderr)
        run = subprocess.run(command + ["--task", parent["id"]], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn('"refined_task": "extract-0000"', run.stdout)
        second = subprocess.run(command + ["--task", parent["id"]], capture_output=True, text=True)
        self.assertEqual(second.stdout, run.stdout)
        self.assert_protected()


if __name__ == "__main__":
    unittest.main()
