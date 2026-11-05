"""Offline regression for a live 23030-byte writer pre-dispatch failure."""
import json
import unittest

from scripts import bounded_writer as writer


class WriterRetryBudgetTests(unittest.TestCase):
    def test_planner_reserves_feedback_inside_dispatch_ceiling(self):
        self.assertEqual(writer.BUDGET, 23000)
        self.assertEqual(writer.REQUEST_BUDGET, 22000)
        for diagnostic in ("wrong fields", "缺少证据" * 200, '\\"\n\x00' * 300):
            prompt = "原" * 7000 + "x" * 1000
            retried = writer.retry_prompt(prompt, diagnostic)
            self.assertTrue(retried.startswith(prompt))
            self.assertLessEqual(len(retried.encode("utf-8")), writer.BUDGET)
            feedback = json.loads(retried[len(prompt):].split("\n", 2)[-1])
            self.assertTrue(feedback["error"])
            self.assertTrue(diagnostic.startswith(feedback["error"]))

    def test_legacy_near_ceiling_prompt_never_repeats_23030_failure(self):
        prompt = "e" * 22950
        self.assertGreater(len((prompt + "\nPrevious part rejection; repair this issue using source evidence, not filler:\n" + "bad").encode()), 23000)
        self.assertEqual(writer.retry_prompt(prompt, "bad"), prompt)

    def test_feedback_shortens_without_truncating_legacy_source(self):
        prompt = "e" * 22880
        value = writer.retry_prompt(prompt, "诊断" * 100)
        self.assertTrue(value.startswith(prompt))
        self.assertGreater(len(value), len(prompt))
        self.assertLessEqual(len(value.encode("utf-8")), writer.BUDGET)
        json.loads(value[len(prompt):].split("\n", 2)[-1])

    def test_over_budget_base_is_rejected_not_truncated(self):
        with self.assertRaisesRegex(ValueError, "no model call made"):
            writer.retry_prompt("x" * 23001, "error")

    def test_empty_diagnostic_keeps_prompt_exact(self):
        self.assertEqual(writer.retry_prompt("source", ""), "source")


if __name__ == "__main__":
    unittest.main()
