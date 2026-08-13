#!/usr/bin/env python3
"""Storage and optional speaker-diarization regression tests."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


MEDIA_STORE = load_module("media_store_test", "media_store.py")
DIARIZATION = load_module("diarization_test", "diarize_segments.py")
PODCAST = load_module("podcast_listener_storage_test", "podcast-listener.py")


class MediaStoreTests(unittest.TestCase):
    def test_local_sync_copies_archive_and_rewrites_public_image(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            destination = root / "sync"
            shownotes = source / "Show Notes"
            assets = source / "图片" / "fixture_assets"
            shownotes.mkdir(parents=True)
            assets.mkdir(parents=True)
            image = assets / "image-01.png"
            image.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
            markdown = shownotes / "fixture_shownotes.md"
            markdown.write_text("![图](../图片/fixture_assets/image-01.png)\n", encoding="utf-8")
            raw_html = shownotes / "fixture_shownotes.raw.html"
            raw_html.write_text("<img src='image.png'>", encoding="utf-8")
            manifest_path = shownotes / "fixture_media-manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "markdown_path": str(markdown),
                        "raw_html_path": str(raw_html),
                        "images": [
                            {
                                "ok": True,
                                "path": str(image),
                                "markdown_url": "../图片/fixture_assets/image-01.png",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result = MEDIA_STORE.sync_archive(
                manifest_path,
                backend="local",
                destination=str(destination),
                public_base_url="https://media.example/podcasts",
            )

            self.assertEqual(result["file_count"], 5)
            self.assertTrue((destination / "fixture/assets/image-01.png").is_file())
            self.assertTrue((destination / "fixture/media-manifest.json").is_file())
            self.assertTrue((destination / "fixture/sync-manifest.json").is_file())
            published = (destination / "fixture/shownotes.md").read_text(encoding="utf-8")
            self.assertIn(
                "https://media.example/podcasts/fixture/assets/image-01.png",
                published,
            )
            updated = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(updated["sync"]["backend"], "local")
            self.assertEqual(updated["sync"]["file_count"], 5)
            self.assertIn("published_url", updated["images"][0])

    def test_local_sync_rewrites_images_to_portable_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shownotes = root / "Show Notes"
            assets = root / "图片" / "fixture_assets"
            destination = root / "sync"
            shownotes.mkdir(parents=True)
            assets.mkdir(parents=True)
            image = assets / "image.png"
            image.write_bytes(b"fixture")
            markdown = shownotes / "fixture_shownotes.md"
            markdown.write_text(
                "![图](../图片/fixture_assets/image.png)\n", encoding="utf-8"
            )
            raw_html = shownotes / "fixture_shownotes.raw.html"
            raw_html.write_text("<p>fixture</p>", encoding="utf-8")
            manifest = shownotes / "fixture_media-manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "markdown_path": str(markdown),
                        "raw_html_path": str(raw_html),
                        "images": [
                            {
                                "ok": True,
                                "path": str(image),
                                "markdown_url": "../图片/fixture_assets/image.png",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result = MEDIA_STORE.sync_archive(
                manifest, backend="local", destination=str(destination)
            )

            synced_markdown = (destination / "fixture/shownotes.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("](assets/image.png)", synced_markdown)
            self.assertTrue((destination / "fixture/assets/image.png").is_file())
            self.assertTrue(Path(result["synced_markdown_path"]).is_file())

    def test_webdav_rejects_insecure_remote_url(self) -> None:
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            MEDIA_STORE._sync_webdav([], "http://dav.example/archive")

    def test_webdav_encodes_destination_and_rejects_url_credentials(self) -> None:
        with self.assertRaisesRegex(ValueError, "userinfo"):
            MEDIA_STORE._sync_webdav([], "https://user:secret@dav.example/archive")

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            MEDIA_STORE.os.environ,
            {"WEBDAV_USERNAME": "user", "WEBDAV_PASSWORD": "secret"},
        ), patch.object(MEDIA_STORE, "_webdav_request") as request:
            source = Path(tmp) / "shownotes.md"
            source.write_text("fixture", encoding="utf-8")
            MEDIA_STORE._sync_webdav(
                [{"path": source, "key": "fixture/shownotes.md"}],
                "https://dav.example/播客归档",
            )
        self.assertTrue(all("播客归档" not in call.args[0] for call in request.call_args_list))
        self.assertTrue(any("%E6%92%AD" in call.args[0] for call in request.call_args_list))


class DiarizationTests(unittest.TestCase):
    def test_assign_speakers_uses_largest_overlap(self) -> None:
        segments = [
            {"start": 0.0, "end": 4.0, "text": "第一段"},
            {"start": 4.0, "end": 8.0, "text": "第二段"},
        ]
        turns = [
            {"start": 0.0, "end": 3.5, "speaker": "SPEAKER_00"},
            {"start": 3.5, "end": 8.0, "speaker": "SPEAKER_01"},
        ]
        assigned = DIARIZATION.assign_speakers(segments, turns)
        self.assertEqual(assigned[0]["speaker"], "SPEAKER_00")
        self.assertEqual(assigned[1]["speaker"], "SPEAKER_01")

    def test_srt_includes_speaker_without_changing_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            segments_path = Path(tmp) / "segments.json"
            srt_path = Path(tmp) / "episode.srt"
            vtt_path = Path(tmp) / "episode.vtt"
            PODCAST.write_transcript_segments(
                segments_path,
                srt_path,
                vtt_path,
                [
                    {
                        "start": 1.0,
                        "end": 2.0,
                        "text": "原始文本",
                        "speaker": "SPEAKER_00",
                    }
                ],
            )
            data = json.loads(segments_path.read_text(encoding="utf-8"))
            srt = srt_path.read_text(encoding="utf-8")
            vtt = vtt_path.read_text(encoding="utf-8")
        self.assertEqual(data[0]["text"], "原始文本")
        self.assertEqual(data[0]["speaker"], "SPEAKER_00")
        self.assertIn("[SPEAKER_00] 原始文本", srt)
        self.assertIn("<v SPEAKER_00>原始文本", vtt)


if __name__ == "__main__":
    unittest.main()
