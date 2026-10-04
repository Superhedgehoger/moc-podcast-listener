"""Real local workflow/JobTracker integration, not live model-quality validation.

All artifacts are synthetic and isolated in temporary directories. No validators
or state transitions are mocked, and no installed skill or library is touched.
"""

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import summary_workflow as workflow
from knowledge_base import knowledge_template


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


class SummaryJobCompletionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="summary-job-completion-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.listener = workflow.listener()
        self.tracker = self.listener.JobTracker.create(
            self.root, "https://example.com/synthetic-episode",
            SimpleNamespace(job_id="summary-integration", keep_audio=False),
        )
        self.result_path = self.tracker.result_path
        self.texts = [
            f"{index}: " + "\u5408\u6210\u8bc1\u636e\u7528\u4e8e\u9a8c\u8bc1\u6587\u4ef6\u5951\u7ea6\u548c\u65f6\u95f4\u5b9a\u4f4d\u3002" * 25
            for index in range(3)
        ]
        package = self.root / "episode"
        self.result = {
            "mode": "transcribe", "summary_workflow_version": 1,
            "episode_dir": str(package),
            "transcript_path": str(self.root / "transcripts" / "episode.txt"),
            "segments_path": str(package / "segments.json"),
            "metadata_path": str(package / "metadata.json"),
            "srt_path": str(package / "transcript.srt"),
            "vtt_path": str(package / "transcript.vtt"),
            "instruction_path": str(package / "instruction.txt"),
            "report_path": str(self.root / "reports" / "episode.md"),
            "knowledge_path": str(package / "knowledge.json"),
            "personal_notes_path": str(package / "personal-notes.md"),
        }
        save(self.result["transcript_path"], "\n\n".join(self.texts))
        save(self.result["segments_path"], [
            {"start": index * 2, "end": index * 2 + 2, "text": text}
            for index, text in enumerate(self.texts)
        ])
        save(self.result["metadata_path"], {
            "episode": {"title": "Synthetic episode", "show_title": "Synthetic show",
                        "duration_minutes": 1, "url": "https://example.com/synthetic-episode"},
            "transcription": {"model": "synthetic-fixture"},
        })
        save(self.result["knowledge_path"], knowledge_template(
            read(self.result["metadata_path"])["episode"],
            transcript_path=Path(self.result["transcript_path"]),
            report_path=Path(self.result["report_path"]),
        ))
        for key, content in (
            ("srt_path", ""), ("vtt_path", "WEBVTT\n"),
            ("instruction_path", "Synthetic contract validation only."),
            ("personal_notes_path", "Personal notes must remain untouched.\n"),
        ):
            save(self.result[key], content)
        self.result = self.tracker.finish(self.result, awaiting_report=True)
        self.assertEqual(self.result["job_id"], self.tracker.job_id)
        self.assert_pending()

    def state(self):
        return read(workflow.workflow_dir(self.result) / "state.json")

    def assert_pending(self):
        for path in (self.tracker.job_path, self.tracker.status_path):
            state = read(path)
            self.assertEqual(state["job_id"], self.tracker.job_id)
            self.assertEqual(state["status"], "awaiting_report")
            self.assertEqual(state["report_status"], "pending")
            self.assertNotIn("completed_at", state)
        result = read(self.result_path)
        self.assertEqual(result["job_status"], "awaiting_report")
        self.assertNotIn("report_verified_at", result)

    def verify(self, *, require_report=True, target=None):
        output = StringIO()
        with redirect_stdout(output):
            code = self.listener.run_verify(
                self.root, target or self.tracker.job_id, require_report=require_report,
            )
        payload = json.loads(output.getvalue())
        self.assertEqual(read(self.result_path.with_name("verification.json"))["ok"], payload["ok"])
        return code, payload

    def complete_and_assemble(self):
        workflow.prepare(self.result_path)
        state = self.state()
        for key in state["levels"][0]:
            task = state["tasks"][key]
            payload = read(task["input"])
            save(task["output"], {
                "chunk_id": payload["id"], "source_hash": payload["source_hash"],
                "covered_segment_ids": [segment["id"] for segment in payload["segments"]],
                "items": [{
                    "id": f"item-{index}", "claim": "Synthetic source-backed claim",
                    "quote": segment["text"], "segment_ids": [segment["id"]],
                    "topics": ["Validation"], "examples": [], "numbers": [],
                    "limitations": [], "ambiguities": [],
                } for index, segment in enumerate(payload["segments"])],
            })
            workflow.validate_task(task)
        workflow.status(self.result_path)
        writer = self.state()["tasks"]["write"]
        payload = read(writer["input"])
        directory = Path(writer["output"]).parent
        body = []
        for heading, minimum in payload["section_minimums"].items():
            content = "\u5408\u6210\u6d4b\u8bd5\u5360\u4f4d\u5185\u5bb9\uff0c\u4ec5\u9a8c\u8bc1\u7a0b\u5e8f\u5951\u7ea6\u3002" * (minimum // 10 + 1)
            if heading == "\u5173\u952e\u5f15\u8ff0":
                content = "\n".join(
                    f"- [00:00:{index * 2:02d}]: {text[:100]}"
                    for index, text in enumerate(self.texts)
                )
            body.append(f"## {heading}\n\n{content}\n")
        save(directory / "body.md", "\n".join(body))
        save(directory / "coverage.json", {"items": [
            {"evidence_id": key, "section": "\u8be6\u7ec6\u603b\u7ed3"}
            for key in payload["leaf_ids"]
        ]})
        save(directory / "knowledge.draft.json", {
            "schema_version": 1, "status": "complete", "topics": [],
            "entities": [], "ai_tags": [], "insights": [{
                "id": "synthetic", "claim": "Synthetic source-backed claim",
                "evidence": [{"kind": "quote", "quote": self.texts[0][:100],
                              "start": 0, "end": 2, "confidence": "high",
                              "speaker": "unconfirmed"}],
            }],
        })
        save(writer["output"], {"input_hash": writer["input_hash"]})
        workflow.validate_task(writer)
        self.assertEqual(workflow.status(self.result_path)["status"], "ready_to_assemble")
        self.assert_pending()
        self.assertEqual(workflow.assemble(self.result_path)["status"], "assembled")
        self.assertTrue(all(task["status"] == "complete" for task in self.state()["tasks"].values()))
        validation = workflow.validate_workflow(self.result)
        self.assertTrue(validation["ok"], validation["errors"])
        self.assert_pending()

    def assert_successfully_completed(self, *, target=None):
        code, payload = self.verify(target=target)
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["ok"], payload["errors"])
        self.assertIn({"name": "summary_workflow", "ok": True}, payload["checks"])
        for path in (self.tracker.job_path, self.tracker.status_path):
            state = read(path)
            self.assertEqual(state["status"], "completed")
            self.assertEqual(state["report_status"], "verified")
            self.assertEqual(state["progress"], 100)
        result = read(self.result_path)
        self.assertEqual(result["job_status"], "completed")
        self.assertEqual(result["report_verified_at"], read(self.tracker.job_path)["completed_at"])
        self.assertEqual(Path(self.result["personal_notes_path"]).read_text(encoding="utf-8"),
                         "Personal notes must remain untouched.\n")

    def test_tracked_job_completes_only_after_final_verification(self):
        code, payload = self.verify(require_report=False)
        self.assertEqual(code, 0, payload)
        self.assert_pending()
        code, payload = self.verify()
        self.assertEqual(code, 1, payload)
        self.assert_pending()
        self.complete_and_assemble()
        self.assert_successfully_completed()

    def test_invalid_report_keeps_tracked_job_pending_until_repaired(self):
        self.complete_and_assemble()
        save(self.result["report_path"], "# Invalid report\n")
        code, payload = self.verify()
        self.assertEqual(code, 1, payload)
        self.assertFalse(payload["ok"])
        self.assertTrue(any("report" in error.lower() for error in payload["errors"]), payload)
        self.assert_pending()
        workflow.assemble(self.result_path)
        self.assert_successfully_completed()

    def test_invalid_knowledge_keeps_tracked_job_pending_until_repaired(self):
        self.complete_and_assemble()
        knowledge = read(self.result["knowledge_path"])
        knowledge["insights"][0]["evidence"][0]["quote"] = "Invented evidence absent from source."
        save(self.result["knowledge_path"], knowledge)
        code, payload = self.verify(target=str(self.result_path))
        self.assertEqual(code, 1, payload)
        self.assertFalse(payload["ok"])
        self.assertTrue(any("knowledge" in error.lower() for error in payload["errors"]), payload)
        self.assert_pending()
        workflow.assemble(self.result_path)
        self.assert_successfully_completed(target=str(self.result_path))

    def test_artifact_only_verification_cannot_complete_assembled_job(self):
        self.complete_and_assemble()
        code, payload = self.verify(require_report=False)
        self.assertEqual(code, 0, payload)
        self.assert_pending()
        self.assert_successfully_completed()


if __name__ == "__main__":
    unittest.main()
