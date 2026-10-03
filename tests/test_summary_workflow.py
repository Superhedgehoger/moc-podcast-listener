"""Synthetic workflow regression tests, not actual-model or summary-quality checks.

Mocked writer/assembly checks are explicitly labeled unit tests only.
"""

import copy
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import summary_workflow as workflow


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


class WorkflowFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="summary-tests-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.result_path = self.root / "result.json"
        self.result = {
            "episode_dir": str(self.root / "episode"),
            "transcript_path": str(self.root / "transcripts" / "episode.txt"),
            "segments_path": str(self.root / "episode" / "segments.json"),
            "metadata_path": str(self.root / "episode" / "metadata.json"),
            "report_path": str(self.root / "reports" / "episode.md"),
            "knowledge_path": str(self.root / "episode" / "knowledge.json"),
        }
        save(self.result_path, self.result)
        save(self.result["metadata_path"], {
            "episode": {"title": "Fixture episode", "show_title": "Fixture show",
                        "duration_minutes": 1, "url": "https://example.com/episode"},
            "transcription": {"model": "fixture"},
        })
        self.set_source(["Primary source evidence."])

    def set_source(self, texts):
        save(self.result["transcript_path"], "\n\n".join(texts))
        save(self.result["segments_path"], [
            {"start": index * 2, "end": index * 2 + 2, "text": text}
            for index, text in enumerate(texts)
        ])

    def state(self):
        return read(workflow.workflow_dir(self.result) / "state.json")

    def extracts(self):
        state = self.state()
        return [state["tasks"][key] for key in state["levels"][0]]

    def evidence(self, task, detail_size=0):
        payload = read(task["input"])
        return {
            "chunk_id": payload["id"], "source_hash": payload["source_hash"],
            "covered_segment_ids": [s["id"] for s in payload["segments"]],
            "items": [{
                "id": f"item-{index}", "claim": "Source-backed claim " + "x" * detail_size,
                "quote": segment["text"], "segment_ids": [segment["id"]],
                "topics": ["Testing"], "examples": [], "numbers": [],
                "limitations": [], "ambiguities": [],
            } for index, segment in enumerate(payload["segments"])],
        }

    def complete_extracts(self, detail_size=0):
        for task in self.extracts():
            save(task["output"], self.evidence(task, detail_size))
            workflow.validate_task(task)
        return workflow.status(self.result_path)


class SummaryWorkflowTests(WorkflowFixture):
    def test_source_lookup_returns_only_one_verified_chunk_path(self):
        workflow.prepare(self.result_path)
        self.complete_extracts()
        task = self.extracts()[0]
        located = workflow.locate(self.result_path, task["id"] + ":item-0")
        self.assertEqual(located["source_chunk"], task["input"])
        self.assertNotIn("text", located)
        with self.assertRaisesRegex(ValueError, "Unknown"):
            workflow.locate(self.result_path, task["id"] + ":missing")

    def test_old_workflow_version_does_not_reuse_evidence(self):
        workflow.prepare(self.result_path)
        self.complete_extracts()
        state = self.state()
        base = workflow.workflow_dir(self.result)
        old_directory = base / state["generation"]
        legacy_directory = base / "previous-version"
        old_directory.rename(legacy_directory)
        for task in state["tasks"].values():
            for key in ("input", "output", "instruction"):
                task[key] = task[key].replace(str(old_directory), str(legacy_directory))
        state.update(version=-1, generation="previous-version")
        workflow.persist(base, state)
        workflow.prepare(self.result_path)
        self.assertEqual(self.extracts()[0]["status"], "pending")

    def test_final_verification_only_requires_workflow_for_opted_in_results(self):
        listener = workflow.listener()
        result = dict(self.result, mode="transcribe")
        with patch.object(workflow, "validate_workflow", return_value={"ok": False, "errors": ["workflow pending"]}) as verifier:
            listener.verify_result_artifacts(result, require_report=True)
            verifier.assert_not_called()
            result["summary_workflow_version"] = 1
            listener.verify_result_artifacts(result, require_report=False)
            verifier.assert_not_called()
            final = listener.verify_result_artifacts(result, require_report=True)
            verifier.assert_called_once()
            self.assertIn("workflow pending", final["errors"])

    def test_report_links_support_parentheses_spaces_query_and_fragment(self):
        listener = workflow.listener()
        document = self.root / "documents" / "source (1).txt"
        save(document, "source")
        save(self.result["report_path"], '[Source](<../documents/source%20(1).txt?download=1#start>)')
        final = listener.verify_result_artifacts(dict(self.result, mode="transcribe"))
        self.assertFalse(any("report link is broken" in e for e in final["errors"]))
        document.unlink()
        final = listener.verify_result_artifacts(dict(self.result, mode="transcribe"))
        self.assertTrue(any("report link is broken" in e for e in final["errors"]))

    def test_quote_validation_is_verbatim_and_supports_repeated_occurrences(self):
        segments = [dict(start=0, end=1, text="Growth was 1.5 percent."),
                    dict(start=100, end=101, text="Growth was 1.5 percent.")]
        self.assertTrue(workflow.quote_matches("Growth was 1.5 percent.", 100, 101, segments))
        self.assertFalse(workflow.quote_matches("Growth was 15 percent.", 100, 101, segments))
        self.assertFalse(workflow.quote_matches("Growth was 1.5 percent.", 50, 51, segments))
        self.assertFalse(workflow.quote_matches("Growth was 1.5 percent.", 100, 101,
                         [segments[0], dict(start=100, end=101, text="Unrelated content")]))
        from knowledge_base import evidence_matches_segments
        self.assertTrue(evidence_matches_segments("Growth was 1.5 percent.", 100, 101, segments))

    def test_source_rejects_invalid_timestamp_ranges(self):
        for start, end in ((-0.5, 1), (2, 1), (float("nan"), 1), (0, float("inf")), (None, None)):
            with self.subTest(start=start, end=end):
                save(self.result["segments_path"], [dict(start=start, end=end, text="Source")])
                with self.assertRaises(ValueError):
                    workflow.prepare(self.result_path)

    def test_prepare_is_idempotent_and_preserves_running_state_and_artifacts(self):
        first = workflow.prepare(self.result_path)
        task = self.extracts()[0]
        workflow.task_event(self.result_path, task["id"], "start")
        before = self.state()
        artifacts = {key: Path(task[key]).read_bytes() for key in ("input", "instruction")}
        again = workflow.prepare(self.result_path)
        self.assertEqual(first["generation"], again["generation"])
        self.assertEqual(before, self.state())
        for key, content in artifacts.items():
            self.assertEqual(Path(task[key]).read_bytes(), content)
        self.assertEqual(len(list(workflow.workflow_dir(self.result).glob("*/result.json"))), 1)

    def test_concurrency_two_and_retries_stop_after_three_attempts(self):
        self.set_source([str(i) + "x" * 199 for i in range(4)])
        progress = workflow.prepare(self.result_path, target_tokens=800)
        tasks = self.extracts()
        self.assertEqual(len(tasks), 4)
        self.assertEqual(len(progress["next_tasks"]), 2)
        for task in tasks[:2]:
            workflow.task_event(self.result_path, task["id"], "start")
        self.assertEqual(workflow.status(self.result_path)["next_tasks"], [])
        with self.assertRaisesRegex(ValueError, "Concurrency limit is 2"):
            workflow.task_event(self.result_path, tasks[2]["id"], "start")
        self.assertEqual(self.state()["tasks"][tasks[2]["id"]]["attempts"], 0)
        key = tasks[0]["id"]
        for attempt in range(1, 4):
            failed = workflow.task_event(self.result_path, key, "fail", "worker failed")
            self.assertEqual(failed["attempts"], attempt)
            self.assertEqual(failed["status"], "blocked" if attempt == 3 else "pending")
            if attempt < 3:
                workflow.task_event(self.result_path, key, "start")
        with self.assertRaisesRegex(ValueError, "retries exhausted"):
            workflow.task_event(self.result_path, key, "start")
        progress = workflow.status(self.result_path)
        self.assertNotIn(key, [task["id"] for task in progress["next_tasks"]])
        self.assertIn({"id": key, "status": "blocked", "error": "worker failed"}, progress["issues"])
        with self.assertRaisesRegex(ValueError, "Only running"):
            workflow.task_event(self.result_path, key, "fail")

    def test_invalid_outputs_are_archived_on_retry_and_eventually_block(self):
        workflow.prepare(self.result_path)
        task = self.extracts()[0]
        for attempt in range(1, 4):
            workflow.task_event(self.result_path, task["id"], "start")
            invalid = self.evidence(task)
            invalid["items"][0]["quote"] = "Invented quotation"
            save(task["output"], invalid)
            progress = workflow.status(self.result_path)
            self.assertIn("Quote", progress["issues"][0]["error"])
            self.assertEqual(self.state()["tasks"][task["id"]]["status"],
                             "blocked" if attempt == 3 else "pending")
        for attempt in (1, 2):
            archived = Path(task["output"]).parent / f"attempt-{attempt}" / "output.json"
            self.assertEqual(read(archived), invalid)
        with self.assertRaisesRegex(ValueError, "retries exhausted"):
            workflow.task_event(self.result_path, task["id"], "start")

    def test_extract_rejects_invalid_quote_source_and_primary_coverage(self):
        self.set_source(["First distinct evidence.", "Second distinct evidence."])
        workflow.prepare(self.result_path)
        task = self.extracts()[0]
        valid = self.evidence(task)
        cases = []
        for field in ("source_hash", "chunk_id"):
            bad = copy.deepcopy(valid)
            bad[field] = "another-source"
            cases.append((field, bad, "different chunk"))
        bad = copy.deepcopy(valid)
        bad["items"][0]["quote"] = "Invented quotation"
        cases.append(("fabricated quote", bad, "Quote is absent"))
        bad = copy.deepcopy(valid)
        bad["items"][0]["quote"] = valid["items"][1]["quote"]
        cases.append(("quote from wrong segment", bad, "Quote is absent"))
        bad = copy.deepcopy(valid)
        bad["items"][0]["segment_ids"] = ["unknown"]
        cases.append(("unknown segment", bad, "Unknown evidence segment"))
        for coverage in ([], valid["covered_segment_ids"][:1],
                         [valid["covered_segment_ids"][0]] * 2,
                         valid["covered_segment_ids"] + ["unknown"]):
            bad = copy.deepcopy(valid)
            bad["covered_segment_ids"] = coverage
            cases.append((f"coverage {coverage}", bad, "coverage incomplete or duplicated"))
        for label, bad, message in cases:
            with self.subTest(label=label):
                save(task["output"], bad)
                with self.assertRaisesRegex(ValueError, message):
                    workflow.validate_task(task)
        save(task["output"], valid)
        self.assertEqual(workflow.validate_task(task), valid)

    def test_source_change_requires_prepare_and_retains_only_unchanged_chunks(self):
        texts = [str(i) + "x" * 199 for i in range(3)]
        self.set_source(texts)
        original = workflow.prepare(self.result_path, target_tokens=800)
        self.complete_extracts()
        old_tasks = self.extracts()
        texts[-1] = "Changed " + "y" * 192
        self.set_source(texts)
        with self.assertRaisesRegex(ValueError, "Source changed"):
            workflow.status(self.result_path)
        new = workflow.prepare(self.result_path, target_tokens=800)
        self.assertNotEqual(original["generation"], new["generation"])
        tasks = self.extracts()
        for previous, current in zip(old_tasks[:2], tasks[:2]):
            self.assertEqual(current["input_hash"], previous["input_hash"])
            self.assertEqual(current["status"], "complete")
            self.assertEqual(read(current["output"]), read(previous["output"]))
            self.assertEqual(Path(current["output"]).read_bytes(), Path(previous["output"]).read_bytes())
        self.assertNotEqual(tasks[-1]["input_hash"], old_tasks[-1]["input_hash"])
        self.assertEqual(tasks[-1]["status"], "pending")
        self.assertFalse(Path(tasks[-1]["output"]).exists())
        self.assertNotIn("write", self.state()["tasks"])

    def test_upstream_change_rebuilds_writer_dependency_and_missing_output_removes_writer(self):
        workflow.prepare(self.result_path)
        self.complete_extracts()
        old_writer = self.state()["tasks"]["write"]
        task = self.extracts()[0]
        revised = read(task["output"])
        revised["items"][0]["claim"] = "Revised source-backed interpretation"
        save(task["output"], revised)
        workflow.status(self.result_path)
        state = self.state()
        writer = state["tasks"]["write"]
        self.assertNotEqual(writer["input_hash"], old_writer["input_hash"])
        self.assertEqual(writer["status"], "pending")
        self.assertEqual(read(writer["input"])["dependencies"],
                         {task["id"]: state["tasks"][task["id"]]["output_hash"]})
        Path(task["output"]).unlink()
        workflow.status(self.result_path)
        self.assertNotIn("write", self.state()["tasks"])
        self.assertEqual(len(self.state()["levels"]), 1)

    def test_metadata_is_budgeted_in_chunk_inputs_without_omissions(self):
        self.set_source([str(i) + "x" * 199 for i in range(4)])
        original = read(self.result["segments_path"])
        workflow.prepare(self.result_path, target_tokens=800)
        self.assertEqual(self.state()["settings"]["chunk_format"], "segment_json_short_ids_v1")
        tasks = self.extracts()
        self.assertEqual(len(tasks), 4)
        primary = []
        for task in tasks:
            payload = read(task["input"])
            primary.extend(payload["segments"])
            self.assertEqual(len(payload["segments"]), 1)
            self.assertGreater(payload["estimated_tokens"], len(payload["segments"][0]["text"]))
            self.assertLessEqual(payload["estimated_tokens"], 400)
            self.assertLessEqual(workflow.tokens(payload), 800)
            self.assertEqual(task["estimated_input_tokens"], workflow.tokens(payload))
        self.assertEqual([{key: value for key, value in item.items() if key != "id"}
                          for item in primary], original)
        self.assertEqual(len({item["id"] for item in primary}), len(original))
        self.assertEqual(read(self.result["segments_path"]), original)

    def test_prepare_rejects_invalid_and_duplicate_supplied_segment_ids(self):
        self.set_source(["First source segment.", "Second source segment."])
        original = read(self.result["segments_path"])
        for identifiers in (("duplicate", "duplicate"), ("", "valid"),
                            (None, "valid"), (123, "valid")):
            with self.subTest(identifiers=identifiers):
                segments = copy.deepcopy(original)
                for segment, identifier in zip(segments, identifiers):
                    segment["id"] = identifier
                save(self.result["segments_path"], segments)
                with self.assertRaisesRegex(ValueError, "invalid or duplicate ID"):
                    workflow.prepare(self.result_path)
                self.assertFalse((workflow.workflow_dir(self.result) / "state.json").exists())
                self.assertEqual(read(self.result["segments_path"]), segments)

    def test_untimed_source_is_explicitly_blocked_without_workflow_state(self):
        segments = read(self.result["segments_path"])
        segments[0].update(start=None, end=None)
        save(self.result["segments_path"], segments)
        with self.assertRaisesRegex(ValueError, "Timestamped segments required"):
            workflow.prepare(self.result_path)
        self.assertFalse((workflow.workflow_dir(self.result) / "state.json").exists())

    def test_modified_task_input_is_not_trusted(self):
        workflow.prepare(self.result_path)
        task = self.extracts()[0]
        save(task["output"], self.evidence(task))
        payload = read(task["input"])
        payload["segments"][0]["text"] = "Changed input"
        save(task["input"], payload)
        with self.assertRaisesRegex(ValueError, "Task input changed"):
            workflow.validate_task(task)

    def test_default_24k_budget_triggers_hierarchy_with_enough_evidence(self):
        self.set_source([f"Source {i}: " + "x" * 180 for i in range(8)])
        workflow.prepare(self.result_path, target_tokens=800)
        self.assertEqual(self.state()["settings"]["synthesis_tokens"], 24000)
        self.complete_extracts(detail_size=2800)
        state = self.state()
        self.assertEqual(len(state["levels"]), 2)
        self.assertNotIn("write", state["tasks"])
        reducers = [state["tasks"][key] for key in state["levels"][1]]
        self.assertGreater(len(reducers), 1)
        all_ids = []
        for task in reducers:
            self.assertEqual(task["kind"], "reduce")
            payload = read(task["input"])
            self.assertLessEqual(workflow.tokens(payload), 24000)
            all_ids.extend(payload["leaf_ids"])
            output = {"input_hash": task["input_hash"], "items": [{
                "claim": "Grouped topic", "details": "Preserved source-backed details",
                "evidence_ids": payload["leaf_ids"],
            }], "omitted": []}
            bad = copy.deepcopy(output)
            bad["items"][0]["evidence_ids"] = payload["leaf_ids"][:-1]
            save(task["output"], bad)
            with self.assertRaises(ValueError):
                workflow.validate_task(task)
            bad["omitted"] = [{"evidence_id": payload["leaf_ids"][-1], "reason": ""}]
            save(task["output"], bad)
            with self.assertRaisesRegex(ValueError, "Invalid omission"):
                workflow.validate_task(task)
            bad["omitted"][0]["reason"] = "Repeated example covered by the grouped topic"
            save(task["output"], bad)
            workflow.validate_task(task)
            save(task["output"], output)
        expected = [f"{task['id']}:item-0" for task in self.extracts()]
        self.assertCountEqual(all_ids, expected)
        self.assertEqual(len(all_ids), len(set(all_ids)))
        workflow.status(self.result_path)
        writer = self.state()["tasks"]["write"]
        payload = read(writer["input"])
        self.assertCountEqual(payload["leaf_ids"], expected)
        self.assertLessEqual(workflow.tokens(payload), 24000)
        self.assertEqual(set(payload["dependencies"]), {task["id"] for task in reducers})

    def test_markdown_rebases_relative_links_images_and_reference_definitions(self):
        source = self.root / "episode" / "notes"
        report = self.root / "reports"
        original = ('[Document](docs/readme.md?download=1#part "Read me")\n'
                    '![Image](<images/cover photo.png> "Cover")\n'
                    '[Encoded](images/cover%20photo.png)\n'
                    '[ref]: docs/reference.pdf "Reference"\n'
                    '![Reference image][ref]\n')
        rendered = workflow.rebase_markdown(original, source, report)
        self.assertEqual(rendered,
                         '[Document](<../episode/notes/docs/readme.md?download=1#part> "Read me")\n'
                         '![Image](<../episode/notes/images/cover photo.png> "Cover")\n'
                         '[Encoded](<../episode/notes/images/cover photo.png>)\n'
                         '[ref]: <../episode/notes/docs/reference.pdf> "Reference"\n'
                         '![Reference image][ref]\n')
        for destination in ("https://example.com/a?q=1#part", "//cdn.example.com/image.png",
                            "mailto:reader@example.com", "#section"):
            with self.subTest(destination=destination):
                self.assertEqual(workflow.rebase_markdown(f"[Link](<{destination}>)", source, report),
                                 f"[Link](<{destination}>)")


class WriterAssemblyUnitTests(WorkflowFixture):
    """Unit tests only: listener and knowledge validation are mocked boundaries."""

    def setUp(self):
        super().setUp()
        self.set_source(["First source quotation.", "Second source quotation.", "Third source quotation."])

        def sections(body):
            parts = re.split(r"(?m)^## (.+)\n", body)
            return dict(zip(parts[1::2], parts[2::2]))

        listener = SimpleNamespace(
            report_section_minimums=Mock(return_value={"Overview": 8}),
            parse_report_sections=Mock(side_effect=sections),
            visible_report_chars=Mock(side_effect=lambda value: len(value.strip())),
            escape_markdown_table_cell=Mock(side_effect=lambda value: str(value or "").replace("|", "\\|")),
        )
        listener_patch = patch.object(workflow, "listener", return_value=listener)
        listener_patch.start()
        self.addCleanup(listener_patch.stop)
        knowledge_patch = patch("knowledge_base.validate_knowledge", return_value={"ok": True, "errors": []})
        self.knowledge_validator = knowledge_patch.start()
        self.addCleanup(knowledge_patch.stop)
        self.notes = self.root / "episode" / "personal-notes.md"
        save(self.notes, "Personal notes must not change.\n")
        self.archive = self.root / "episode" / "notes" / "shownotes.md"
        save(self.archive, '![Cover](images/cover.png)\n[Source](docs/source.html)\n')
        self.result["shownotes_archive"] = {"markdown_path": str(self.archive)}
        save(self.result_path, self.result)
        workflow.prepare(self.result_path)
        self.complete_extracts()
        self.writer = self.state()["tasks"]["write"]
        self.directory = Path(self.writer["output"]).parent
        self.payload = read(self.writer["input"])
        self.body = ("## Overview\n\nA detailed source-backed summary.\n\n"
                     "## \u5173\u952e\u5f15\u8ff0\n\n"
                     "- [00:00:00]\uff1aFirst source quotation.\n"
                     "- [00:00:02]\uff1aSecond source quotation.\n"
                     "- [00:00:04]\uff1aThird source quotation.\n")
        self.coverage = {"items": [{"evidence_id": key, "section": "Overview", "reason": ""}
                                   for key in self.payload["leaf_ids"]]}

    def write_draft(self):
        save(self.writer["output"], {"input_hash": self.writer["input_hash"]})
        save(self.directory / "body.md", self.body)
        save(self.directory / "coverage.json", self.coverage)
        save(self.directory / "knowledge.draft.json", {"status": "complete", "unit_fixture": True})

    def test_unit_assembly_requires_verified_writer_and_preserves_personal_notes(self):
        with self.assertRaisesRegex(ValueError, "incomplete"):
            workflow.assemble(self.result_path)
        self.assertFalse(Path(self.result["report_path"]).exists())
        self.write_draft()
        self.assertEqual(workflow.status(self.result_path)["status"], "ready_to_assemble")
        result = workflow.assemble(self.result_path)
        self.assertEqual(result["status"], "assembled")
        report = Path(self.result["report_path"]).read_text(encoding="utf-8")
        self.assertIn(self.body.strip(), report)
        self.assertIn("![Cover](<../episode/notes/images/cover.png>)", report)
        self.assertIn("[Source](<../episode/notes/docs/source.html>)", report)
        relative_transcript = os.path.relpath(self.result["transcript_path"], Path(self.result["report_path"]).parent)
        self.assertIn(f"](<{relative_transcript}>)", report)
        self.assertEqual(read(self.result["knowledge_path"]), read(self.directory / "knowledge.draft.json"))
        self.assertEqual(self.notes.read_text(encoding="utf-8"), "Personal notes must not change.\n")
        self.knowledge_validator.assert_called_with(
            self.directory / "knowledge.draft.json",
            transcript_path=Path(self.result["transcript_path"]),
            segments_path=Path(self.result["segments_path"]),
            duration_minutes=1.0, require_complete=True,
        )
        self.assertEqual(workflow.validate_workflow(self.result), {"ok": True, "errors": []})
        save(self.result["report_path"], report + "Unverified edit")
        self.assertFalse(workflow.validate_workflow(self.result)["ok"])
        workflow.assemble(self.result_path)
        save(self.result["knowledge_path"], {"tampered": True})
        self.assertFalse(workflow.validate_workflow(self.result)["ok"])

    def test_unit_writer_coverage_rejects_missing_duplicate_unknown_and_unexplained(self):
        key = self.payload["leaf_ids"][0]
        cases = [([], "coverage incomplete"),
                 (self.coverage["items"] * 2, "duplicate coverage ID"),
                 ([{"evidence_id": "unknown", "section": "Overview"}], "Unknown"),
                 ([{"evidence_id": key, "section": "Missing"}], "missing section"),
                 ([{"evidence_id": key, "section": "", "reason": " "}], "needs a reason")]
        self.write_draft()
        for items, message in cases:
            with self.subTest(message=message):
                save(self.directory / "coverage.json", {"items": items})
                with self.assertRaisesRegex(ValueError, message):
                    workflow.validate_task(self.writer)
                with self.assertRaisesRegex(ValueError, "incomplete"):
                    workflow.assemble(self.result_path)
        save(self.directory / "coverage.json", {"items": [
            {"evidence_id": key, "section": "", "reason": "Duplicate background detail"}
        ] + self.coverage["items"][1:]})
        workflow.validate_task(self.writer)

    def test_unit_writer_requires_three_correctly_formatted_source_matched_quotes(self):
        self.write_draft()
        cases = [
            (self.body.replace("- [00:00:04]\uff1aThird source quotation.\n", ""), "At least three"),
            (self.body.replace("[00:00:04]", "[00:60:04]"), "Quote format"),
            (self.body.replace("[00:00:04]", "[01:00:04]"), "does not match"),
            (self.body.replace("Third source quotation.", "Invented quotation."), "does not match"),
        ]
        for body, message in cases:
            with self.subTest(message=message):
                save(self.directory / "body.md", body)
                with self.assertRaisesRegex(ValueError, message):
                    workflow.validate_task(self.writer)
                with self.assertRaisesRegex(ValueError, "incomplete"):
                    workflow.assemble(self.result_path)
        save(self.directory / "body.md", self.body)
        workflow.validate_task(self.writer)

    def test_unit_invalid_knowledge_short_body_and_wrong_input_hash_block_assembly(self):
        self.write_draft()
        self.knowledge_validator.return_value = {"ok": False, "errors": ["invalid source quote"]}
        with self.assertRaisesRegex(ValueError, "Knowledge: invalid source quote"):
            workflow.validate_task(self.writer)
        with self.assertRaisesRegex(ValueError, "incomplete"):
            workflow.assemble(self.result_path)
        self.knowledge_validator.return_value = {"ok": True, "errors": []}
        save(self.directory / "body.md", "## Overview\n\nShort")
        with self.assertRaisesRegex(ValueError, "short/missing section"):
            workflow.validate_task(self.writer)
        save(self.directory / "body.md", self.body)
        save(self.writer["output"], {"input_hash": "stale"})
        with self.assertRaisesRegex(ValueError, "input_hash mismatch"):
            workflow.validate_task(self.writer)
        self.assertFalse(Path(self.result["report_path"]).exists())

    def test_unit_changed_evidence_invalidates_assembled_report_and_writer(self):
        self.write_draft()
        workflow.assemble(self.result_path)
        task = self.extracts()[0]
        changed = read(task["output"])
        changed["items"][0]["claim"] = "Updated interpretation"
        save(task["output"], changed)
        self.assertFalse(workflow.validate_workflow(self.result)["ok"])
        progress = workflow.status(self.result_path)
        self.assertEqual(progress["status"], "awaiting_workers")
        self.assertIsNone(self.state()["assembled"])
        writer = self.state()["tasks"]["write"]
        self.assertNotEqual(writer["input_hash"], self.writer["input_hash"])
        self.assertEqual(writer["status"], "pending")
        self.assertFalse(Path(writer["output"]).exists())
        with self.assertRaisesRegex(ValueError, "incomplete"):
            workflow.assemble(self.result_path)


class NativeFinalVerificationTests(WorkflowFixture):
    def test_synthetic_pipeline_passes_real_final_verifier_without_model_mocks(self):
        # Repeated synthetic text tests contracts only, never summary quality.
        texts = [f"第{i}段用于验证文件契约和时间定位。" * 25 for i in range(3)]
        self.set_source(texts)
        self.result.update(mode="transcribe", summary_workflow_version=1,
                           srt_path=str(self.root / "episode/transcript.srt"),
                           vtt_path=str(self.root / "episode/transcript.vtt"),
                           instruction_path=str(self.root / "episode/instruction.txt"),
                           personal_notes_path=str(self.root / "episode/notes.md"))
        for key, content in (("srt_path", ""), ("vtt_path", "WEBVTT\n"),
                             ("instruction_path", "Synthetic contract test"),
                             ("personal_notes_path", "Keep this personal note")):
            save(self.result[key], content)
        save(self.result_path, self.result)
        workflow.prepare(self.result_path)
        self.complete_extracts()
        writer = self.state()["tasks"]["write"]
        payload = read(writer["input"])
        directory = Path(writer["output"]).parent
        body = []
        for heading, minimum in payload["section_minimums"].items():
            content = ("合成测试占位内容，仅验证程序契约。" * (minimum // 10 + 1))
            if heading == "关键引述":
                content = "\n".join(f"- [00:00:{i * 2:02d}]：{text[:100]}" for i, text in enumerate(texts))
            body.append(f"## {heading}\n\n{content}\n")
        save(directory / "body.md", "\n".join(body))
        save(directory / "coverage.json", {"items": [{"evidence_id": key, "section": "详细总结"} for key in payload["leaf_ids"]]})
        save(directory / "knowledge.draft.json", {"schema_version": 1, "status": "complete",
             "topics": [], "entities": [], "ai_tags": [], "insights": [{"id": "synthetic",
             "claim": "Synthetic source claim", "evidence": [{"kind": "quote", "quote": texts[0][:100],
             "start": 0, "end": 2, "confidence": "high", "speaker": "unconfirmed"}]}]})
        save(writer["output"], {"input_hash": writer["input_hash"]})
        workflow.assemble(self.result_path)
        final = workflow.listener().verify_result_artifacts(self.result, require_report=True)
        self.assertTrue(final["ok"], final["errors"])
        self.assertEqual(Path(self.result["personal_notes_path"]).read_text(), "Keep this personal note")


if __name__ == "__main__":
    unittest.main()
