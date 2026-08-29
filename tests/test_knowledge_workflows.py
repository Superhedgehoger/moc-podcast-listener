#!/usr/bin/env python3
"""Evidence, subscription, search, and export regression tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from knowledge_base import (
    ensure_episode_knowledge_files,
    load_knowledge_index,
    rebuild_knowledge_index,
    search_knowledge,
    validate_knowledge,
)
from knowledge_export import EXPORT_FORMATS, export_library
from subscription_manager import initialize_subscriptions, scan_subscriptions


def create_package(output: Path) -> tuple[Path, Path, Path, Path]:
    name = "Fixture_Show_Evidence_Episode_20260829"
    package = output / "资料" / name
    transcript = output / "转录稿" / f"{name}_转录稿.txt"
    report = output / "总结稿" / f"{name}_详细总结.md"
    segments = package / "转录数据" / "segments.json"
    package.mkdir(parents=True)
    transcript.parent.mkdir()
    report.parent.mkdir()
    segments.parent.mkdir()
    transcript.write_text("主持人说证据必须回到原始转录稿核对。", encoding="utf-8")
    report.write_text("# 总结\n", encoding="utf-8")
    segments.write_text(
        json.dumps(
            [
                {
                    "start": 10.0,
                    "end": 18.0,
                    "text": "主持人说证据必须回到原始转录稿核对。",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    metadata = {
        "episode": {
            "show_title": "Fixture Show",
            "title": "Evidence Episode",
            "url": "https://example.com/episode",
            "pub_date": "20260829",
            "duration_minutes": 1,
            "guests": ["主持人"],
        },
        "transcription": {"processed_at": "2026-08-29 12:00:00"},
    }
    (package / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False), encoding="utf-8"
    )
    knowledge, notes = ensure_episode_knowledge_files(
        package,
        metadata["episode"],
        transcript_path=transcript,
        report_path=report,
    )
    return knowledge, notes, transcript, segments


class EvidenceTests(unittest.TestCase):
    def test_exact_evidence_and_timestamp_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            knowledge, notes, transcript, segments = create_package(output)
            payload = json.loads(knowledge.read_text(encoding="utf-8"))
            payload.update(
                {
                    "status": "complete",
                    "topics": ["知识管理"],
                    "ai_tags": ["证据"],
                    "insights": [
                        {
                            "id": "insight-01",
                            "claim": "结论必须可回到原文核验。",
                            "evidence": [
                                {
                                    "kind": "quote",
                                    "quote": "证据必须回到原始转录稿核对",
                                    "start": 10,
                                    "end": 18,
                                    "speaker": "主持人",
                                    "confidence": "high",
                                }
                            ],
                        }
                    ],
                }
            )
            knowledge.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            verification = validate_knowledge(
                knowledge,
                transcript_path=transcript,
                segments_path=segments,
                duration_minutes=1,
            )
            notes_text = notes.read_text(encoding="utf-8")

        self.assertTrue(verification["ok"], verification["errors"])
        self.assertIn("不得覆盖", notes_text)

    def test_wrong_quote_and_timestamp_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            knowledge, _, transcript, segments = create_package(output)
            payload = json.loads(knowledge.read_text(encoding="utf-8"))
            payload.update(
                {
                    "status": "complete",
                    "insights": [
                        {
                            "id": "x",
                            "claim": "错误示例",
                            "evidence": [
                                {
                                    "kind": "quote",
                                    "quote": "这句话根本不在转录稿里面",
                                    "start": 40,
                                    "end": 45,
                                    "speaker": None,
                                    "confidence": "high",
                                }
                            ],
                        }
                    ],
                }
            )
            knowledge.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            verification = validate_knowledge(
                knowledge,
                transcript_path=transcript,
                segments_path=segments,
                duration_minutes=1,
            )

        self.assertFalse(verification["ok"])
        self.assertTrue(any("not present" in error for error in verification["errors"]))


class IndexAndExportTests(unittest.TestCase):
    def test_rebuild_search_and_all_exports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            knowledge, notes, _, _ = create_package(output)
            payload = json.loads(knowledge.read_text(encoding="utf-8"))
            payload.update(
                {
                    "status": "complete",
                    "topics": ["Agent"],
                    "ai_tags": ["知识库"],
                    "insights": [{"id": "i", "claim": "Agent 需要证据。", "evidence": []}],
                }
            )
            knowledge.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            notes.write_text(notes.read_text(encoding="utf-8").replace("- 用户标签：", "- 用户标签：#重要"), encoding="utf-8")

            rebuilt = rebuild_knowledge_index(output, bootstrap=False)
            records = load_knowledge_index(Path(rebuilt["index_path"]))
            matches = search_knowledge(records, query="Agent", tag="重要")
            exported = export_library(output)

            self.assertEqual(len(matches), 1)
            self.assertEqual(set(exported["formats"]), EXPORT_FORMATS)
            for files in exported["formats"].values():
                self.assertTrue(files)
                self.assertTrue(all(Path(path).is_file() for path in files))


class SubscriptionTests(unittest.TestCase):
    def test_scan_deduplicates_and_never_transcribes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            initialized = initialize_subscriptions(output)
            config_path = Path(initialized["config_path"])
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["settings"]["preferred_keywords"] = ["Agent"]
            config["subscriptions"] = [
                {
                    "name": "Fixture",
                    "feed_url": "https://example.com/feed.xml",
                    "enabled": True,
                    "priority": 2,
                    "keywords": [],
                }
            ]
            config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

            calls: list[str] = []

            def fetch_entries(url: str):
                calls.append(url)
                return [
                    {
                        "id": "episode-1",
                        "title": "Agent 实践",
                        "show_title": "Fixture",
                        "url": "https://example.com/episode-1",
                        "pub_date": "20260829",
                        "show_notes": "Evidence",
                        "transcripts": [{"url": "https://example.com/t.txt"}],
                    }
                ]

            first = scan_subscriptions(output, fetch_entries, scan_date="2026-08-29")
            same_day = scan_subscriptions(output, fetch_entries, scan_date="2026-08-29")
            second = scan_subscriptions(output, fetch_entries, scan_date="2026-08-30")

            self.assertEqual(calls, ["https://example.com/feed.xml"] * 3)
            self.assertEqual(first["candidate_count"], 1)
            self.assertEqual(same_day["candidate_count"], 1)
            self.assertEqual(second["candidate_count"], 0)
            self.assertIn("没有自动下载音频", Path(first["brief_path"]).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
