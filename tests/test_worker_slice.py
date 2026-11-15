import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from scripts.worker_slice import Slice, WorkerPaused, limit, checkpoint
from scripts.bounded_retries import bounded_part_attempts
from summary_workflow import read


class WorkerSliceTests(unittest.TestCase):
    def test_no_limit_does_not_pause(self):
        for _ in range(10):
            checkpoint()

    def test_limit_rejects_invalid_values(self):
        for value in (0, -1, True, 1.5):
            with self.assertRaises(ValueError):
                Slice(max_dispatches=value)
        for value in (0, -1, True, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                Slice(max_seconds=value)

    def test_expired_clock_does_not_allocate_dispatch(self):
        budget = Slice(max_seconds=5, started=10)
        with patch("scripts.worker_slice.time.monotonic", return_value=15):
            with self.assertRaises(WorkerPaused):
                budget.checkpoint()
        self.assertEqual(budget.dispatches, 0)

    def test_pause_between_retries_preserves_failure_and_does_not_add_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            def fail(_):
                raise ValueError("malformed fixture")
            with limit(max_dispatches=1):
                with self.assertRaises(WorkerPaused):
                    bounded_part_attempts(tmp, "binding", fail)
            ledger = Path(tmp) / "dispatch-ledgers/binding.json"
            self.assertEqual(len(read(ledger)["attempts"]), 1)
            self.assertEqual(read(ledger)["attempts"][0]["status"], "failed")
            with limit(max_dispatches=1):
                self.assertEqual(bounded_part_attempts(tmp, "binding", lambda _: "good"), "good")
            self.assertEqual([row["status"] for row in read(ledger)["attempts"]], ["failed", "passed"])

    def test_exhausted_ledger_cannot_be_reset_by_new_slice(self):
        with tempfile.TemporaryDirectory() as tmp:
            def fail(_):
                raise ValueError("bad")
            with self.assertRaises(ValueError):
                bounded_part_attempts(tmp, "same", fail)
            with limit(max_dispatches=1) as budget:
                with self.assertRaises(ValueError):
                    bounded_part_attempts(tmp, "same", lambda _: self.fail("fourth attempt"))
                self.assertEqual(budget.dispatches, 0)

    def test_context_is_restored_after_exception(self):
        with self.assertRaises(WorkerPaused):
            with limit(max_dispatches=1):
                checkpoint()
                checkpoint()
        checkpoint()
