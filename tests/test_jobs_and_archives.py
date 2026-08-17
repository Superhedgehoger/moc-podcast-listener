#!/usr/bin/env python3
"""作业状态、核验与补充归档产物测试。"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("podcast_listener_jobs", ROOT / "podcast-listener.py")
PODCAST = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(PODCAST)


def job_args(**overrides):
    values = {
        "archive_only": False,
        "engine": "whisper",
        "model": "small",
        "keep_audio": False,
        "force_transcribe": False,
        "shownotes_assets": "hybrid",
        "link_snapshot": "none",
        "sync_backend": None,
        "sync_destination": None,
        "public_base_url": None,
        "sync_required": False,
        "diarize": False,
        "job_id": None,
    }
    values.update(overrides)
    return types.SimpleNamespace(**values)


class JobTrackerTests(unittest.TestCase):
    def test_job_state_is_atomic_resumable_and_awaits_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            tracker = PODCAST.JobTracker.create(
                output,
                "https://example.com/episode",
                job_args(job_id="fixture-job"),
            )
            tracker.phase("resolving")
            tracker.checkpoint("episode", {"id": "episode-1", "title": "Fixture"})
            result = tracker.finish(
                {"mode": "transcribe", "report_path": str(output / "总结稿" / "report.md")},
                awaiting_report=True,
            )

            status = json.loads(tracker.status_path.read_text(encoding="utf-8"))
            resumed = PODCAST.JobTracker.resume(output, "fixture-job")

        self.assertEqual(result["job_status"], "awaiting_report")
        self.assertEqual(status["status"], "awaiting_report")
        self.assertEqual(resumed.state["checkpoints"]["episode"]["id"], "episode-1")
        self.assertTrue(resumed.resumed)

    def test_verify_marks_job_complete_only_after_report_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            transcript_dir = output / "转录稿"
            summary_dir = output / "总结稿"
            transcript_dir.mkdir()
            summary_dir.mkdir()

            transcript = transcript_dir / "episode_转录稿.txt"
            segments = transcript_dir / "episode_segments.json"
            srt = transcript_dir / "episode.srt"
            vtt = transcript_dir / "episode.vtt"
            metadata = output / "episode_metadata.json"
            instruction = output / "episode_Agent任务指令.txt"
            report = summary_dir / "episode_详细总结.md"
            transcript.write_text("# 播客转录稿\n\n正文\n", encoding="utf-8")
            segments.write_text("[]\n", encoding="utf-8")
            srt.write_text("", encoding="utf-8")
            vtt.write_text("WEBVTT\n", encoding="utf-8")
            metadata.write_text('{"episode": {}}\n', encoding="utf-8")
            instruction.write_text("fixture\n", encoding="utf-8")

            tracker = PODCAST.JobTracker.create(
                output,
                "https://example.com/episode",
                job_args(job_id="verify-job"),
            )
            tracker.finish(
                {
                    "mode": "transcribe",
                    "transcript_path": str(transcript),
                    "segments_path": str(segments),
                    "srt_path": str(srt),
                    "vtt_path": str(vtt),
                    "metadata_path": str(metadata),
                    "instruction_path": str(instruction),
                    "report_path": str(report),
                    "shownotes_archive": None,
                    "chapters_path": None,
                },
                awaiting_report=True,
            )

            with redirect_stdout(StringIO()):
                before = PODCAST.run_verify(output, "verify-job", require_report=False)
            pending = json.loads(tracker.status_path.read_text(encoding="utf-8"))

            report.write_text(
                "# 详细总结\n\n"
                "[转录稿](<../转录稿/episode_转录稿.txt>)\n\n"
                "[segments](<../转录稿/episode_segments.json>)\n\n"
                "[SRT](<../转录稿/episode.srt>)\n\n"
                "[VTT](<../转录稿/episode.vtt>)\n",
                encoding="utf-8",
            )
            with redirect_stdout(StringIO()):
                after = PODCAST.run_verify(output, "verify-job", require_report=True)
            completed = json.loads(tracker.status_path.read_text(encoding="utf-8"))
            result = json.loads(tracker.result_path.read_text(encoding="utf-8"))

        self.assertEqual(before, 0)
        self.assertEqual(pending["status"], "awaiting_report")
        self.assertEqual(after, 0)
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(result["job_status"], "completed")


class ArchiveTests(unittest.TestCase):
    def test_coverless_checkpoint_is_rebuilt_when_episode_has_cover(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "shownotes.raw.html"
            markdown = root / "shownotes.md"
            manifest = root / "media-manifest.json"
            raw.write_text("", encoding="utf-8")
            markdown.write_text("fixture", encoding="utf-8")
            manifest.write_text('{"images": []}', encoding="utf-8")
            archive = {
                "raw_html_path": str(raw),
                "markdown_path": str(markdown),
                "manifest_path": str(manifest),
            }
            usable = PODCAST.archive_checkpoint_is_usable(
                archive, "https://cdn.example/cover.jpg"
            )
        self.assertFalse(usable)

    def test_podcasting2_chapters_are_downloaded_and_normalized(self) -> None:
        payload = {
            "version": "1.2.0",
            "chapters": [
                {"startTime": 42, "title": "第二章", "url": "https://example.com/two"},
                {"startTime": 0, "title": "开场"},
            ],
        }

        def fake_download(url, output_path, **kwargs):
            output_path.write_text(json.dumps(payload), encoding="utf-8")
            return {
                "ok": True,
                "source_url": url,
                "final_url": url,
                "http_status": 200,
                "fetched_at": "2026-08-13T00:00:00Z",
                "content_type": "application/json+chapters",
                "bytes": output_path.stat().st_size,
                "path": str(output_path),
            }

        info = {
            "chapters": {
                "url": "https://cdn.example.com/chapters.json",
                "type": "application/json+chapters",
            }
        }
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            PODCAST, "download_http_resource", side_effect=fake_download
        ):
            archive = PODCAST.archive_episode_chapters(info, Path(tmp), "fixture")
            normalized = json.loads(Path(archive["path"]).read_text(encoding="utf-8"))

        self.assertTrue(archive["ok"])
        self.assertEqual(archive["chapter_count"], 2)
        self.assertEqual(normalized["chapters"][0]["title"], "开场")
        self.assertEqual(normalized["source_url"], info["chapters"]["url"])

    def test_singlefile_snapshot_is_bounded_and_recorded(self) -> None:
        links = [
            {"text": "one", "url": "https://example.com/one"},
            {"text": "two", "url": "https://example.com/two"},
        ]

        def fake_run(args, timeout=None, cwd=None):
            Path(args[2]).write_text("<html>fixture</html>", encoding="utf-8")
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            PODCAST.os.environ, {"SHOWNOTES_MAX_LINK_SNAPSHOTS": "1"}
        ), patch.object(PODCAST.shutil, "which", return_value="/usr/bin/single-file"), patch.object(
            PODCAST, "is_safe_remote_url", return_value=(True, None)
        ), patch.object(PODCAST, "run_command", side_effect=fake_run) as command:
            summary = PODCAST.snapshot_shownotes_links(
                links, Path(tmp), "fixture", "singlefile"
            )

        self.assertEqual(summary["requested"], 1)
        self.assertEqual(summary["completed"], 1)
        self.assertEqual(links[0]["snapshot"]["status"], "complete")
        self.assertNotIn("snapshot", links[1])
        command.assert_called_once()


if __name__ == "__main__":
    unittest.main()
