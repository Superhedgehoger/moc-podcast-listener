import tempfile
import unittest
from pathlib import Path

from scripts.bounded_retries import bounded_part_attempts, inherited_part_attempts
from summary_workflow import write


class RetryMigrationTests(unittest.TestCase):
    def test_completed_and_failed_prior_attempts_are_not_reset_or_double_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = [{"attempt": i + 1, "status": "passed" if i == 2 else "failed",
                     "started_at": i + 100, "error": "fixture"} for i in range(3)]
            for binding in ("old", "copied"):
                write(Path(tmp) / "dispatch-ledgers" / f"{binding}.json", {"binding": binding, "attempts": rows})
            inherited = inherited_part_attempts(tmp, "new", ["old", "copied"])
            self.assertEqual(len(inherited), 3)
            with self.assertRaisesRegex(ValueError, "retry limit exhausted"):
                bounded_part_attempts(tmp, "new", lambda _: self.fail("No fourth dispatch"), inherited)

    def test_current_binding_and_corrupt_history_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(Path(tmp) / "dispatch-ledgers/current.json", {"binding": "current", "attempts": []})
            self.assertEqual(inherited_part_attempts(tmp, "current", ["old"]), [])
            write(Path(tmp) / "dispatch-ledgers/old.json", {"binding": "wrong", "attempts": []})
            with self.assertRaisesRegex(ValueError, "Inherited part retry ledger is corrupt"):
                inherited_part_attempts(tmp, "new", ["old"])
