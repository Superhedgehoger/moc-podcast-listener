"""Planning and failure-gate tests; mocked verdicts are not live quality evidence."""
import json
from pathlib import Path
import tempfile
import unittest

from scripts import linear_review, semantic_review
from summary_workflow import write


class LinearReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "segments.json"
        self.segments = [{"start": i * 2, "end": i * 2 + 2,
                          "text": f"Research example {i} requires consent and feedback."}
                         for i in range(800)]
        write(self.path, self.segments)
        self.payload = {"semantic_review_contract": linear_review.CONTRACT,
                        "result": {"segments_path": str(self.path)}}
        self.body = "## Summary\n\n" + "\n\n".join(s["text"] for s in self.segments[::20])
        self.knowledge = {"insights": [{"claim": "Consent and feedback are required."}]}

    def plan(self):
        return semantic_review.plan_review(self.payload, self.body, self.knowledge, ["Summary", "knowledge"])

    def test_linear_calls_preserve_all_original_text_and_artifact_items(self):
        plan = self.plan()
        self.assertEqual(plan["mode"], "linear")
        self.assertLess(len(plan["jobs"]), 30)
        self.assertEqual(len(plan["jobs"]), len(plan["batches"]) + len(plan["shards"]))
        self.assertEqual("".join(u["text"] for b in plan["batches"] for u in b),
                         "".join(s["text"] for s in self.segments))
        original_ids = [u["source_id"] for u in plan["units"]]
        self.assertEqual([i for j in plan["jobs"] for i in j.get("primary_source_ids", [])], original_ids)
        self.assertEqual([i for j in plan["jobs"] for i in j.get("artifact_ids", [])],
                         [a["artifact_id"] for a in plan["artifacts"]])
        for job in plan["jobs"]:
            self.assertLessEqual(len(job["prompt"].encode()), 23000)
            data = json.loads(job["prompt"].split("\nINPUT DATA:\n")[1])
            self.assertEqual(data["review_contract"], linear_review.CONTRACT)
            if job["kind"] == "coverage":
                self.assertNotIn("knowledge", [a["kind"] for a in data["retrieved_body"]])

    def test_missing_source_or_artifact_job_cannot_pass_aggregate(self):
        plan = self.plan()
        verdict = {"status": "passed", "reason": "All supplied content audited", "findings": [],
                   "approved_name_translations": []}
        for key in ("primary_source_ids", "artifact_ids"):
            job = next(j for j in plan["jobs"] if j.get(key))
            saved = job[key]
            job[key] = saved[1:]
            with self.assertRaisesRegex(ValueError, "coverage lost"):
                semantic_review._aggregate(plan, {j["job_id"]: verdict for j in plan["jobs"]}, ["Summary", "knowledge"])
            job[key] = saved

    def test_rejection_requires_real_supplied_source_and_impacted_part(self):
        plan = self.plan()
        job = plan["jobs"][-1]
        verdict = {"status": "failed", "reason": "Unsupported factual assertion found", "findings": [],
                   "approved_name_translations": []}
        with self.assertRaisesRegex(ValueError, "actionable"):
            semantic_review._validate_verdict(verdict, job, self.segments)
        verdict["findings"] = [{"part": job["parts"][0], "reason": "The claim omitted the explicit consent condition",
                                "source_id": next(iter(job["source_addresses"])),
                                "issue_type": "incorrect_claim", "artifact_id": job["shard"]["items"][0]["artifact_id"],
                                "artifact_quote": "Consent and feedback are required.",
                                "source_quote": job["grounding_source"][0]["text"]}]
        semantic_review._validate_verdict(verdict, job, self.segments)
        verdict["findings"][0]["source_quote"] = "Invented source quote"
        with self.assertRaisesRegex(ValueError, "source-supported"):
            semantic_review._validate_verdict(verdict, job, self.segments)


if __name__ == "__main__":
    unittest.main()
