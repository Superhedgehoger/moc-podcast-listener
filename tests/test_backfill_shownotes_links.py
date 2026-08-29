#!/usr/bin/env python3
"""Historical Show Notes link-archive backfill tests."""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "backfill_shownotes_links", ROOT / "scripts" / "backfill_shownotes_links.py"
)
BACKFILL = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(BACKFILL)


class ShowNotesLinkBackfillTests(unittest.TestCase):
    def test_cleans_urls_backs_up_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            shownotes = output / "资料" / "fixture" / "Show Notes"
            shownotes.mkdir(parents=True)
            markdown = shownotes / "shownotes.md"
            raw_html = shownotes / "source.raw.html"
            manifest = shownotes / "media-manifest.json"
            markdown.write_text("# Show Notes\n\n正文\n", encoding="utf-8")
            raw_html.write_text(
                '<p>申请地址 https://example.com/apply），后续说明文字；</p>'
                '<p><a href="https://example.com/article">文章</a></p>',
                encoding="utf-8",
            )
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "episode_url": "https://episode.example/item",
                        "created_at": "2026-08-29T00:00:00Z",
                        "images": [],
                        "links": [
                            {
                                "text": "https://example.com/apply）；",
                                "url": "https://example.com/apply），后续说明文字；",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            original_mtime = 1_700_000_000
            os.utime(markdown, (original_mtime, original_mtime))
            backup = output / ".backup" / "fixture"

            plan = BACKFILL.plan_backfill(output)
            applied = BACKFILL.apply_backfill(output, plan, backup_dir=backup)
            updated_markdown = markdown.read_text(encoding="utf-8")
            updated_manifest = json.loads(manifest.read_text(encoding="utf-8"))
            second_plan = BACKFILL.plan_backfill(output)
            preserved_mtime = int(markdown.stat().st_mtime)
            backup_exists = (
                backup / "资料" / "fixture" / "Show Notes" / "shownotes.md"
            ).is_file()

        urls = [item["url"] for item in updated_manifest["links"]]
        self.assertEqual(
            urls,
            ["https://example.com/apply", "https://example.com/article"],
        )
        self.assertIn("## 链接归档", updated_markdown)
        self.assertNotIn("后续说明文字", updated_markdown)
        self.assertEqual(updated_markdown.count("shownotes-links:start"), 1)
        self.assertEqual(preserved_mtime, original_mtime)
        self.assertTrue(backup_exists)
        self.assertEqual(applied["updated"], 1)
        self.assertEqual(second_plan, [])


if __name__ == "__main__":
    unittest.main()
