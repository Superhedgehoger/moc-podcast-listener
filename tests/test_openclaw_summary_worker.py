"""Adapter contracts only; these tests do not invoke a model."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("openclaw_worker", ROOT / "scripts/openclaw_summary_worker.py")
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


class WorkerAdapterTests(unittest.TestCase):
    def test_preflight_accepts_legacy_workspace_without_launching_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            generation = Path(tmp) / "generation"
            directory = generation / "extract-0000"
            directory.mkdir(parents=True)
            task = {"kind": "extract", "input": str(directory / "input.json")}
            with patch.object(worker, "build_prompt", return_value="bounded prompt"):
                workspace, _, estimate = worker.preflight(task)
            self.assertEqual(Path(workspace), generation)
            self.assertEqual(estimate, len("bounded prompt"))
            self.assertNotIn("workspace", task)

    def test_preflight_rejects_over_budget_before_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = {"kind": "extract", "input": str(Path(tmp) / "task/input.json")}
            with patch.object(worker, "build_prompt", return_value="x" * 10001):
                with self.assertRaisesRegex(ValueError, "exceeds 10000"):
                    worker.preflight(task)

    def test_section_repair_preserves_other_sections_and_rejects_injection(self):
        body = "## A\n\nOld A\n\n## B\n\nPreserve B exactly.\n"
        changed = worker.replace_sections(body, {"A": "New A"}, {"A": 1, "B": 1})
        self.assertIn("New A", changed)
        self.assertTrue(changed.endswith("## B\n\nPreserve B exactly.\n"))
        for updates in ({"C": "new"}, {"A": "## B\nInjected"}, {}):
            with self.assertRaises(ValueError):
                worker.replace_sections(body, updates, {"A": 1, "B": 1})

    def test_accepts_json_and_complete_fence_but_not_commentary(self):
        self.assertEqual(worker.parse_response('{"items":[]}'), {"items": []})
        self.assertEqual(worker.parse_response('```json\n{"items":[]}\n```'), {"items": []})
        for text in ('[]', '```json\n{}', 'Here is the result: {}'):
            with self.assertRaises(ValueError):
                worker.parse_response(text)

    def test_extract_prompt_contains_only_own_input_not_source_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            worker.write(path / "input.json", {"segments": [{"text": "source", "start": 0, "end": 1}]})
            worker.write(path / "task.md", "# extract\n\nExtract source.\n\nInput: private-source-path\n")
            task = {"kind": "extract", "input": str(path / "input.json"),
                    "instruction": str(path / "task.md"), "output": str(path / "output.json"), "input_hash": "hash"}
            prompt = worker.build_prompt(task)
            self.assertIn('"0:0": "source"', prompt)
            self.assertNotIn("private-source-path", prompt)
            self.assertIn("Do not call tools", prompt)

    def test_compact_evidence_copies_exact_source_not_model_quotation(self):
        payload = {"id": "chunk", "source_hash": "hash", "segments": [
            {"id": "stable1", "text": "First. Qualification!", "start": 0, "end": 5},
            {"id": "stable2", "text": "Second。", "start": 5, "end": 9}]}
        response = {"covered_indices": [0, 1], "items": [{"id": "one", "claim": "qualified claim",
                    "quote_ref": "0:0", "segment_indices": [0], "quote": "invented"}]}
        expanded = worker.expand_extraction(payload, response)
        self.assertEqual(expanded["items"][0]["quote"], "First. Qualification!")
        self.assertEqual(expanded["covered_segment_ids"], ["stable1", "stable2"])
        literal = {**response, "items": [{**response["items"][0], "id": 1, "quote_ref": "Qualification!"}]}
        expanded_literal = worker.expand_extraction(payload, literal)
        self.assertEqual(expanded_literal["items"][0]["quote"], "Qualification!")
        self.assertEqual(expanded_literal["items"][0]["id"], "1")
        for covered in ([0], [0, 0], [True, 1]):
            with self.assertRaises(ValueError):
                worker.expand_extraction(payload, {**response, "covered_indices": covered})
        for indices, ref in (([1], "0:0"), ([0], "99:0"), ([False], "0:0"), ([[0]], "0:0")):
            bad = {**response, "items": [{**response["items"][0], "segment_indices": indices, "quote_ref": ref}]}
            with self.assertRaises(ValueError):
                worker.expand_extraction(payload, bad)

    def test_source_catalog_slices_are_contiguous_and_cover_all_text(self):
        source = " a。" + "long" * 40 + "！\n End?"
        values = list(worker.source_catalog({"segments": [{"text": source}]}).values())
        self.assertEqual("".join(values), source)
        self.assertTrue(all(len(value) <= 60 for value in values))

    def test_writer_saves_all_files_then_checks_without_claiming_validity(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            task = {"kind": "write", "output": str(path / "output.json"), "input_hash": "expected"}
            response = {"body": "## Body", "knowledge": {"insights": []}, "coverage": {"items": []}}
            with patch.object(worker, "validate_task", side_effect=ValueError("invalid evidence")) as check:
                with self.assertRaisesRegex(ValueError, "invalid evidence"):
                    worker.save_response(task, response)
            self.assertTrue(all((path / name).exists() for name in
                                ("body.md", "knowledge.draft.json", "coverage.json", "output.json")))
            self.assertEqual(worker.read(path / "output.json"), {"input_hash": "expected"})
            check.assert_called_once_with(task)

    def test_rejects_missing_writer_fields_before_writing_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = {"kind": "write", "output": str(Path(tmp) / "output.json"), "input_hash": "hash"}
            with self.assertRaises(ValueError):
                worker.save_response(task, {"body": "incomplete"})
            self.assertFalse(Path(task["output"]).exists())

    def test_unstarted_task_cannot_launch_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "task.json"
            worker.write(path, {"status": "complete"})
            with patch.object(worker.subprocess, "Popen") as launch:
                with self.assertRaisesRegex(ValueError, "Coordinator must start"):
                    worker.run(path, "provider/current")
                launch.assert_not_called()

    def test_writer_resume_reuses_valid_parts_without_model_quotation_calls(self):
        # Orchestration only: synthetic text and final validator mock are NOT quality evidence.
        helper_spec = importlib.util.spec_from_file_location("writer_fixture", ROOT / "tests/test_bounded_writer.py")
        helper = importlib.util.module_from_spec(helper_spec)
        helper_spec.loader.exec_module(helper)
        payload, sources = helper.fixture()
        calls = []
        def invoke(task, workspace, prompt, model, config, timeout, directory):
            data = json.loads(prompt.split("\nINPUT DATA:\n", 1)[1].split("\nPrevious part rejection", 1)[0])
            if "section" in data:
                name = data["section"]
                calls.append(name)
                text = "x" * data["target_visible_chars"]
                if name == "内容大纲" and calls.count(name) == 1:
                    text = "short"
                value = {"text": text}
            elif "leaf_ids" in data:
                calls.append("coverage")
                value = {"items": [{"evidence_id": key, "section": "详细总结", "reason": "synthetic"}
                                    for key in data["leaf_ids"]]}
            else:
                calls.append("knowledge")
                value = {"insights": [{"claim": "synthetic"} for _ in range(6)]}
            return value, {"elapsed_seconds": 1, "estimated_prompt_tokens": len(prompt.encode("utf-8"))}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            worker.write(path / "input.json", payload)
            task = {"id": "write", "kind": "write", "attempts": 1, "workspace": tmp,
                    "input": str(path / "input.json"), "input_hash": "synthetic", "output": str(path / "output.json")}
            with patch.object(worker, "writer_sources", return_value=sources), patch.object(worker, "invoke", side_effect=invoke), patch.object(worker, "validate_task") as final_check:
                with self.assertRaisesRegex(ValueError, "Short section"):
                    worker.run_writer(task, "provider/model", None, 600)
                task["attempts"] = 2
                final_check.side_effect = ValueError("Report evidence coverage incomplete")
                with self.assertRaisesRegex(ValueError, "coverage incomplete"):
                    worker.run_writer(task, "provider/model", None, 600)
                final_check.side_effect = None
                task["attempts"] = 3
                self.assertTrue(worker.run_writer(task, "provider/model", None, 600)["ok"])
            self.assertEqual(calls.count("内容摘要"), 1)
            self.assertEqual(calls.count("内容大纲"), 2)
            self.assertNotIn("关键引述", calls)
            self.assertEqual(calls.count("knowledge"), 1)
            self.assertEqual(calls.count("coverage"), 2)
            self.assertTrue((path / "writer-run-attempt-1.json").exists())
            self.assertTrue((path / "writer-run-attempt-2.json").exists())

    def test_repair_prompt_excludes_valid_sections_and_unchanged_knowledge(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            payload = {"entries": [], "leaf_ids": ["one"], "section_minimums": {"内容摘要": 20, "详细总结": 10}}
            worker.write(path / "input.json", payload)
            worker.write(path / "body.md", "## 内容摘要\n短\n## 详细总结\nALREADY_VALID_SECTION_CONTENT\n")
            worker.write(path / "repair.json", {"error": "Repair only short/missing section: 内容摘要"})
            worker.write(path / "knowledge.draft.json", {"private_marker": "UNCHANGED_KNOWLEDGE"})
            worker.write(path / "coverage.json", {"items": []})
            task = {"kind": "write", "input": str(path / "input.json"), "output": str(path / "output.json"), "input_hash": "hash"}
            with patch.object(worker, "writer_sources", return_value={"boundaries": [], "verified_quotations": []}):
                prompt = worker.build_prompt(task)
            self.assertIn("REPAIR MODE", prompt)
            self.assertIn('"actual": 1', prompt)
            self.assertNotIn("ALREADY_VALID_SECTION_CONTENT", prompt)
            self.assertNotIn("UNCHANGED_KNOWLEDGE", prompt)


if __name__ == "__main__":
    unittest.main()
