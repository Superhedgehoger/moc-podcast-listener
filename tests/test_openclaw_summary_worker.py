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
            worker.write(path / "input.json", {"segments": [{"text": "source"}]})
            worker.write(path / "task.md", "# extract\n\nExtract source.\n\nInput: private-source-path\n")
            task = {"kind": "extract", "input": str(path / "input.json"),
                    "instruction": str(path / "task.md"), "output": str(path / "output.json"), "input_hash": "hash"}
            prompt = worker.build_prompt(task)
            self.assertIn('"text": "source"', prompt)
            self.assertNotIn("private-source-path", prompt)
            self.assertIn("Do not call tools", prompt)

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
