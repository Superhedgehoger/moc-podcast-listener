#!/usr/bin/env python3
"""Summary-date backfill regression tests."""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "backfill_summary_dates", ROOT / "scripts" / "backfill_summary_dates.py"
)
BACKFILL = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(BACKFILL)


class SummaryDateBackfillTests(unittest.TestCase):
    def test_prefers_verified_date_backs_up_and_preserves_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            report = output / "总结稿" / "fixture_详细总结.md"
            result = output / ".jobs" / "job-1" / "result.json"
            report.parent.mkdir(parents=True)
            result.parent.mkdir(parents=True)
            report.write_text("# 播客代听报告\n\n## 基本信息\n", encoding="utf-8")
            original_mtime = 1_700_000_000
            os.utime(report, (original_mtime, original_mtime))
            result.write_text(
                json.dumps(
                    {
                        "report_path": str(report),
                        "report_verified_at": "2026-08-29T17:30:00Z",
                    }
                ),
                encoding="utf-8",
            )

            plan = BACKFILL.plan_backfill(output)
            backup = output / ".backup" / "fixture"
            applied = BACKFILL.apply_backfill(output, plan, backup_dir=backup)
            text = report.read_text(encoding="utf-8")
            second_plan = BACKFILL.plan_backfill(output)

            self.assertEqual(plan[0]["date_source"], "report_verified_at")
            self.assertIn("> 转录总结日期：2026-08-30", text)
            self.assertLess(text.index("转录总结日期"), text.index("## 基本信息"))
            self.assertEqual(int(report.stat().st_mtime), original_mtime)
            self.assertTrue((backup / "总结稿" / report.name).is_file())
            self.assertEqual(applied["updated"], 1)
            self.assertEqual(second_plan, [])

    def test_uses_report_mtime_without_job_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            report = output / "总结稿" / "fixture_详细总结.md"
            report.parent.mkdir(parents=True)
            report.write_text("# Report\n\nBody\n", encoding="utf-8")
            timestamp = 1_700_000_000
            os.utime(report, (timestamp, timestamp))

            plan = BACKFILL.plan_backfill(output)

        expected = BACKFILL.datetime.fromtimestamp(timestamp).astimezone().date().isoformat()
        self.assertEqual(plan[0]["summary_date"], expected)
        self.assertEqual(plan[0]["date_source"], "report_mtime")


if __name__ == "__main__":
    unittest.main()
