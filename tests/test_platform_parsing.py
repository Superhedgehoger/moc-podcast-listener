#!/usr/bin/env python3
"""平台解析回归测试：不联网、不下载音频、不加载 ASR 模型。"""

from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
SPEC = importlib.util.spec_from_file_location("podcast_listener", ROOT / "podcast-listener.py")
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class PlatformParsingTests(unittest.TestCase):
    @staticmethod
    def cli_args(tmp: str, **overrides):
        values = {
            "input": ["fixture"],
            "resolve_only": False,
            "archive_only": False,
            "force_transcribe": False,
            "output_dir": tmp,
            "engine": "whisper",
            "model": "small",
            "keep_audio": False,
            "shownotes_assets": "online",
            "link_snapshot": "none",
            "resume": None,
            "job_id": None,
            "verify": None,
            "require_report": False,
        }
        values.update(overrides)
        return types.SimpleNamespace(**values)

    def test_extract_show_notes_preserves_rich_html(self) -> None:
        rich = '<p>正文 <a href="https://example.com/article">链接</a><img src="https://example.com/a.png"></p>'
        self.assertEqual(MODULE.extract_show_notes("", {"description": rich}), rich)

    def test_xiaoyuzhou_real_shape_prefers_rich_shownotes(self) -> None:
        with patch.object(
            MODULE, "fetch_text", return_value=fixture("xiaoyuzhou_episode.html")
        ):
            info = MODULE.get_episode_info("6a659e146356eb2d9be87c49")
        self.assertEqual(info["show_title"], "无聊斋")
        self.assertEqual(info["pub_date"], "20260726")
        self.assertAlmostEqual(info["duration_minutes"], 5402 / 60)
        self.assertEqual(info["show_notes"].count("<img"), 4)
        self.assertEqual(info["guests"], ["刘旸教主", "孟阳", "月明", "阿铖"])
        self.assertNotIn("SHARE", info["guests"])
        self.assertNotIn("听众昵称", info["guests"])

    def test_shownotes_parser_supports_picture_and_lazy_images(self) -> None:
        seen = []
        parser = MODULE.ShowNotesMarkdownParser(
            "https://episode.example/item",
            lambda url, alt: seen.append((url, alt)) or url,
        )
        parser.feed(
            '<picture><source srcset="https://cdn.example/low.webp 1x, '
            'https://cdn.example/high.webp 2x"><img data-src="https://cdn.example/lazy.jpg" alt="图"></picture>'
        )
        parser.close()
        self.assertEqual(seen, [("https://cdn.example/high.webp", "图")])
        self.assertIn("![图](https://cdn.example/high.webp)", parser.markdown())

    def test_shownotes_parser_preserves_linked_image(self) -> None:
        parser = MODULE.ShowNotesMarkdownParser(
            "https://episode.example/item",
            lambda url, alt: url,
        )
        parser.feed(
            '<a href="https://target.example"><img src="https://cdn.example/a.png" alt="图"></a>'
        )
        parser.close()
        self.assertEqual(
            parser.markdown(),
            "[![图](https://cdn.example/a.png)](https://target.example)",
        )

    def test_private_shownotes_image_url_is_blocked(self) -> None:
        safe, reason = MODULE.is_safe_remote_url("http://127.0.0.1/private.png")
        self.assertFalse(safe)
        self.assertIn("blocked", reason)
        safe, reason = MODULE.is_safe_remote_url("http://198.18.0.162/private.png")
        self.assertFalse(safe)
        self.assertIn("blocked", reason)

    def test_proxy_fake_ip_is_allowed_only_for_domain_resolution(self) -> None:
        fake_result = [
            (MODULE.socket.AF_INET, MODULE.socket.SOCK_STREAM, 6, "", ("198.18.0.162", 443))
        ]
        with patch.object(MODULE.socket, "getaddrinfo", return_value=fake_result), patch.dict(
            MODULE.os.environ, {"ALLOW_PROXY_FAKE_IP": "1"}
        ):
            safe, reason = MODULE.is_safe_remote_url("https://www.example.com/page")
        self.assertTrue(safe)
        self.assertIsNone(reason)

    def test_safe_http_revalidates_redirect_targets(self) -> None:
        redirect = MODULE.HTTPError(
            "https://example.com/start",
            302,
            "Found",
            {"Location": "http://127.0.0.1/private"},
            io.BytesIO(),
        )
        opener = types.SimpleNamespace(open=lambda *args, **kwargs: (_ for _ in ()).throw(redirect))
        with patch.object(MODULE, "build_opener", return_value=opener), patch.object(
            MODULE,
            "is_safe_remote_url",
            side_effect=[(True, None), (False, "non-public address blocked")],
        ):
            with self.assertRaisesRegex(ValueError, "non-public"):
                MODULE.open_safe_http("https://example.com/start")

    def test_shownotes_manifest_collects_plain_and_markdown_links(self) -> None:
        info = {
            "url": "https://episode.example/item",
            "title": "Episode",
            "show_title": "Show",
            "show_notes": "官网 https://example.com/site\n[文章](https://example.com/article)",
        }
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            MODULE.os.environ, {"SHOWNOTES_ASSETS": "online"}
        ):
            archived = MODULE.archive_show_notes(info, Path(tmp), "fixture")
            manifest = json.loads(Path(archived["manifest_path"]).read_text(encoding="utf-8"))
        urls = {item["url"] for item in manifest["links"]}
        self.assertIn("https://example.com/site", urls)
        self.assertIn("https://example.com/article", urls)

    def test_online_image_manifest_keeps_auditable_source_metadata(self) -> None:
        info = {
            "url": "https://episode.example/item",
            "title": "Episode",
            "show_title": "Show",
            "show_notes": '<img src="https://cdn.example/image.png" alt="图">',
        }
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            MODULE.os.environ,
            {"SHOWNOTES_ASSETS": "online", "SHOWNOTES_LINK_SNAPSHOT": "none"},
        ):
            archived = MODULE.archive_show_notes(info, Path(tmp), "fixture")
            manifest = json.loads(Path(archived["manifest_path"]).read_text(encoding="utf-8"))
        image = manifest["images"][0]
        self.assertEqual(manifest["schema_version"], 2)
        self.assertEqual(image["status"], "online_only")
        self.assertEqual(image["source_url"], "https://cdn.example/image.png")
        self.assertEqual(image["final_url"], image["source_url"])

    def test_shownotes_archive_reuses_manifest_image(self) -> None:
        info = {
            "url": "https://episode.example/item",
            "title": "Episode",
            "show_title": "Show",
            "show_notes": '<img src="https://cdn.example/image.png" alt="图">',
        }
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            assets = output / "图片" / "fixture_assets"
            shownotes = output / "Show Notes"
            assets.mkdir(parents=True)
            shownotes.mkdir(parents=True)
            image_path = assets / "image-01-hash.png"
            image_path.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
            manifest_path = shownotes / "fixture_media-manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "images": [
                            {
                                "source_url": "https://cdn.example/image.png",
                                "path": str(image_path),
                                "sha256": "hash",
                                "ok": True,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(
                MODULE.os.environ, {"SHOWNOTES_ASSETS": "hybrid"}
            ), patch.object(MODULE, "download_shownotes_image") as download:
                archived = MODULE.archive_show_notes(info, output, "fixture")
            manifest = json.loads(Path(archived["manifest_path"]).read_text(encoding="utf-8"))
        download.assert_not_called()
        self.assertTrue(manifest["images"][0]["reused"])

    def test_transcript_segments_write_json_and_srt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            segments_path = Path(tmp) / "segments.json"
            srt_path = Path(tmp) / "episode.srt"
            vtt_path = Path(tmp) / "episode.vtt"
            MODULE.write_transcript_segments(
                segments_path,
                srt_path,
                vtt_path,
                [{"start": 1.25, "end": 3.5, "text": "测试文本"}],
            )
            segments = json.loads(segments_path.read_text(encoding="utf-8"))
            srt = srt_path.read_text(encoding="utf-8")
            vtt = vtt_path.read_text(encoding="utf-8")
        self.assertEqual(segments[0]["text"], "测试文本")
        self.assertIn("00:00:01,250 --> 00:00:03,500", srt)
        self.assertTrue(vtt.startswith("WEBVTT"))
        self.assertIn("00:00:01.250 --> 00:00:03.500", vtt)

    def test_render_transcript_document_has_header_timestamps_and_footer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            transcript_dir = output / "转录稿"
            transcript_dir.mkdir()
            transcript_path = transcript_dir / "episode_转录稿.txt"
            segments_path = transcript_dir / "episode_segments.json"
            srt_path = transcript_dir / "episode.srt"
            vtt_path = transcript_dir / "episode.vtt"
            metadata_path = output / "episode_metadata.json"
            document = MODULE.render_transcript_document(
                {
                    "show_title": "测试节目",
                    "title": "测试单集",
                    "url": "https://example.com/episode",
                    "pub_date": "20260805",
                    "duration_minutes": 1.5,
                },
                {
                    "model": "test-model",
                    "language": "zh",
                    "text": "测试文本",
                    "segments": [{"start": 1.2, "end": 3.6, "text": "测试文本"}],
                },
                transcript_path,
                segments_path,
                srt_path,
                vtt_path,
                metadata_path,
            )
        self.assertIn("# 播客转录稿", document)
        self.assertIn("- 原始链接：https://example.com/episode", document)
        self.assertIn("[00:00:01 - 00:00:04]", document)
        self.assertIn("## 附件与来源", document)
        self.assertIn("--- 转录稿结束 ---", document)

    def test_extract_transcript_body_ignores_document_chrome(self) -> None:
        document = """# 播客转录稿

## 转录正文

[00:00:01 - 00:00:03]
第一段

[00:00:03 - 00:00:05] [SPEAKER_00]
第二段

---

## 附件与来源

- 原始页面：https://example.com
"""
        self.assertEqual(MODULE.extract_transcript_body(document), "第一段\n\n第二段")

    def test_archive_only_skips_audio_and_asr(self) -> None:
        info = {
            "id": "fixture",
            "title": "Episode",
            "url": "https://episode.example/item",
            "audio_url": "https://cdn.example/audio.mp3",
            "show_notes": '<p><a href="https://example.com">链接</a></p>',
            "guests": [],
            "duration_minutes": 0.0,
            "show_title": "Show",
            "pub_date": "20260804",
            "source": "fixture",
        }
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            MODULE, "parse_cli_args", return_value=self.cli_args(tmp, archive_only=True)
        ), patch.object(MODULE, "resolve_episode_info", return_value=info.copy()), patch.object(
            MODULE, "download_audio"
        ) as download, patch.object(MODULE, "transcribe_with_fallback") as transcribe:
            MODULE.main()
            metadata_files = list(Path(tmp).glob("*_metadata.json"))
        download.assert_not_called()
        transcribe.assert_not_called()
        self.assertEqual(len(metadata_files), 1)

    def test_existing_transcript_is_reused(self) -> None:
        info = {
            "id": "fixture",
            "title": "Episode",
            "url": "https://episode.example/item",
            "audio_url": "https://cdn.example/audio.mp3",
            "show_notes": "",
            "guests": [],
            "duration_minutes": 12.0,
            "show_title": "Show",
            "pub_date": "20260804",
            "source": "fixture",
        }
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            transcript_dir = output / "转录稿"
            transcript_dir.mkdir()
            combined = MODULE.build_combined_name(info)
            transcript_path = transcript_dir / f"{combined}_转录稿.txt"
            segments_path = transcript_dir / f"{combined}_segments.json"
            metadata_path = output / f"{combined}_metadata.json"
            transcript_path.write_text("已有转录文本", encoding="utf-8")
            segments_path.write_text(
                json.dumps([{"start": 0, "end": 2, "text": "已有转录文本"}]),
                encoding="utf-8",
            )
            metadata_path.write_text(
                json.dumps(
                    {
                        "episode": info,
                        "transcription": {
                            "model": "cached-model",
                            "language": "zh",
                            "initial_prompt": "",
                        },
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(
                MODULE, "parse_cli_args", return_value=self.cli_args(tmp)
            ), patch.object(
                MODULE, "resolve_episode_info", return_value=info.copy()
            ), patch.object(
                MODULE, "download_audio"
            ) as download, patch.object(
                MODULE, "transcribe_with_fallback"
            ) as transcribe:
                MODULE.main()
                instruction_files = list(output.glob("*_Agent任务指令.txt"))
                rendered_transcript = transcript_path.read_text(encoding="utf-8")
        download.assert_not_called()
        transcribe.assert_not_called()
        self.assertEqual(len(instruction_files), 1)
        self.assertEqual(rendered_transcript.count("# 播客转录稿"), 1)
        self.assertIn("- 原始链接：https://episode.example/item", rendered_transcript)
        self.assertIn("[00:00:00 - 00:00:02]", rendered_transcript)
        self.assertTrue(rendered_transcript.endswith("--- 转录稿结束 ---\n"))

    def test_cached_transcript_rejects_mismatched_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript = root / "transcript.txt"
            segments = root / "segments.json"
            metadata = root / "metadata.json"
            transcript.write_text("旧文本", encoding="utf-8")
            segments.write_text("[]", encoding="utf-8")
            metadata.write_text(
                json.dumps({"episode": {"id": "other", "url": "https://example.com/other"}}),
                encoding="utf-8",
            )
            cached = MODULE.load_cached_transcription(
                transcript,
                segments,
                metadata,
                {"id": "current", "url": "https://example.com/current"},
            )
        self.assertIsNone(cached)

    def test_cached_transcript_rejects_implausibly_short_long_episode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript = root / "transcript.txt"
            segments = root / "segments.json"
            metadata = root / "metadata.json"
            transcript.write_text("只有开头一小段", encoding="utf-8")
            segments.write_text("[]", encoding="utf-8")
            episode = {
                "id": "episode",
                "url": "https://example.com/episode",
                "duration_minutes": 90,
            }
            metadata.write_text(
                json.dumps({"episode": episode, "transcription": {"model": "old"}}),
                encoding="utf-8",
            )
            cached = MODULE.load_cached_transcription(
                transcript, segments, metadata, episode
            )
        self.assertIsNone(cached)

    def test_transcription_quality_rejects_partial_timestamp_coverage(self) -> None:
        issue = MODULE.transcription_quality_issue(
            {
                "text": "正文" * 1000,
                "segments": [{"start": 0, "end": 60, "text": "正文"}],
            },
            60,
        )
        self.assertIn("时间戳只覆盖", issue)

    def test_cached_transcript_can_add_diarization_without_rerunning_asr(self) -> None:
        info = {
            "id": "fixture",
            "title": "Episode",
            "url": "https://episode.example/item",
            "audio_url": "https://cdn.example/audio.mp3",
            "show_notes": "",
            "guests": [],
            "duration_minutes": 12.0,
            "show_title": "Show",
            "pub_date": "20260804",
            "source": "fixture",
        }
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            transcript_dir = output / "转录稿"
            transcript_dir.mkdir()
            combined = MODULE.build_combined_name(info)
            (transcript_dir / f"{combined}_转录稿.txt").write_text(
                "已有转录文本", encoding="utf-8"
            )
            (transcript_dir / f"{combined}_segments.json").write_text(
                json.dumps([{"start": 0, "end": 2, "text": "已有转录文本"}]),
                encoding="utf-8",
            )
            (output / f"{combined}_metadata.json").write_text(
                json.dumps(
                    {
                        "episode": info,
                        "transcription": {"model": "cached-model", "language": "zh"},
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(
                MODULE.os.environ, {"DIARIZATION": "0"}
            ), patch.object(
                MODULE,
                "parse_cli_args",
                return_value=self.cli_args(tmp, diarize=True),
            ), patch.object(
                MODULE, "resolve_episode_info", return_value=info.copy()
            ), patch.object(
                MODULE, "download_audio", return_value=True
            ) as download, patch.object(
                MODULE, "preprocess_audio", return_value=True
            ), patch.object(
                MODULE, "diarize_if_configured"
            ) as diarize, patch.object(
                MODULE, "transcribe_with_fallback"
            ) as transcribe:
                MODULE.main()
        download.assert_called_once()
        diarize.assert_called_once()
        transcribe.assert_not_called()

    def test_apple_single_episode(self) -> None:
        result = json.loads(fixture("apple_episode.json"))["results"][0]
        with patch.object(MODULE, "itunes_lookup", return_value=[]), patch.object(
            MODULE, "itunes_search", return_value=[result]
        ):
            info = MODULE.get_apple_episode_info(
                "https://podcasts.apple.com/us/podcast/apple-fixture-episode/id123456789?i=100000000001"
            )
        self.assertEqual(info["title"], "Apple Fixture Episode")
        self.assertEqual(info["audio_url"], "https://cdn.example.com/apple-fixture.mp3")

    def test_overcast_page(self) -> None:
        with patch.object(MODULE, "fetch_text", return_value=fixture("overcast.html")):
            info = MODULE.get_overcast_episode_info("https://overcast.fm/+fixture")
        self.assertEqual(info["source"], "overcast")
        self.assertTrue(info["audio_url"].endswith("overcast-fixture.mp3"))

    def test_spotify_search_fallback(self) -> None:
        resolved = {"title": "Spotify Fixture Episode", "audio_url": "https://cdn.example.com/spotify.mp3"}
        with patch.object(MODULE, "fetch_text", return_value=fixture("spotify.html")), patch.object(
            MODULE, "search_episode_info", return_value=resolved.copy()
        ):
            info = MODULE.get_spotify_episode_info("https://open.spotify.com/episode/fixture")
        self.assertEqual(info["source"], "spotify")
        self.assertEqual(info["url"], "https://open.spotify.com/episode/fixture")

    def test_rss_requires_match_for_target_episode(self) -> None:
        with patch.object(MODULE, "fetch_text", return_value=fixture("rss.xml")):
            self.assertIsNone(MODULE.select_rss_episode("https://example.com/feed.xml", episode_title="Missing"))
            info = MODULE.select_rss_episode("https://example.com/feed.xml", episode_title="Fixture Episode Two")
        self.assertEqual(info["id"], "fixture-2")

    def test_podcasting2_metadata_is_preserved_and_urls_are_resolved(self) -> None:
        with patch.object(MODULE, "fetch_text", return_value=fixture("podcasting2.xml")):
            info = MODULE.select_rss_episode("https://feeds.example.com/show/feed.xml")
        self.assertEqual(info["guests"], ["主持人甲", "嘉宾乙"])
        self.assertEqual(info["people"][1]["role"], "guest")
        self.assertEqual(info["language"], "zh-CN")
        self.assertAlmostEqual(info["duration_minutes"], 90 + 2 / 60)
        self.assertEqual(
            info["transcripts"][0]["url"],
            "https://feeds.example.com/show/transcript.vtt",
        )
        self.assertEqual(
            info["chapters"]["url"],
            "https://feeds.example.com/show/chapters.json",
        )
        self.assertEqual(info["cover_url"], "https://cdn.example.com/episode-square.jpg")

    def test_publisher_vtt_is_preferred_and_parsed_with_speakers(self) -> None:
        info = {
            "language": "zh-CN",
            "transcripts": [
                {
                    "url": "https://cdn.example.com/transcript.txt",
                    "type": "text/plain",
                    "language": "zh-CN",
                },
                {
                    "url": "https://cdn.example.com/transcript.vtt",
                    "type": "text/vtt",
                    "language": "zh-CN",
                    "rel": "captions",
                },
            ],
        }

        def fake_download(url, output_path, **kwargs):
            self.assertTrue(url.endswith("transcript.vtt"))
            output_path.write_text(fixture("publisher_transcript.vtt"), encoding="utf-8")
            return {
                "ok": True,
                "source_url": url,
                "final_url": url,
                "http_status": 200,
                "fetched_at": "2026-08-13T00:00:00Z",
                "content_type": "text/vtt",
                "bytes": output_path.stat().st_size,
                "path": str(output_path),
            }

        with tempfile.TemporaryDirectory() as tmp, patch.object(
            MODULE, "download_http_resource", side_effect=fake_download
        ):
            transcription = MODULE.load_publisher_transcript(
                info, Path(tmp), "fixture"
            )
        self.assertEqual(transcription["source"], "publisher_transcript")
        self.assertEqual(len(transcription["segments"]), 2)
        self.assertEqual(transcription["segments"][0]["speaker"], "主持人甲")
        self.assertIn("长期归档", transcription["text"])

    def test_partial_publisher_transcript_is_rejected_for_long_episode(self) -> None:
        info = {
            "duration_minutes": 90,
            "transcripts": [
                {"url": "https://cdn.example.com/transcript.txt", "type": "text/plain"}
            ],
        }

        def fake_download(url, output_path, **kwargs):
            output_path.write_text("这只是很短的一小段发布方转录内容，不能代表完整节目。", encoding="utf-8")
            return {
                "ok": True,
                "source_url": url,
                "final_url": url,
                "http_status": 200,
                "fetched_at": "2026-08-13T00:00:00Z",
                "content_type": "text/plain",
                "bytes": output_path.stat().st_size,
                "path": str(output_path),
            }

        with tempfile.TemporaryDirectory() as tmp, patch.object(
            MODULE, "download_http_resource", side_effect=fake_download
        ):
            transcription = MODULE.load_publisher_transcript(info, Path(tmp), "fixture")
        self.assertIsNone(transcription)

    def test_youtube_yt_dlp_result(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["yt-dlp"], returncode=0, stdout=fixture("youtube.json"), stderr=""
        )
        with patch.object(MODULE, "run_command", return_value=completed):
            info = MODULE.get_youtube_episode_info("https://www.youtube.com/watch?v=fixture")
        self.assertEqual(info["title"], "YouTube Fixture Episode")
        self.assertTrue(info["audio_url"].endswith("youtube-fixture.m4a"))

    def test_domestic_platform_page_audio_fallbacks(self) -> None:
        cases = [
            ("bilibili", "bilibili.html", "bilibili-fixture.mp3"),
            ("netease_cloud_music", "netease.html", "netease-fixture.mp3"),
            ("ximalaya", "ximalaya.html", "ximalaya-fixture.mp3"),
            ("lizhi_fm", "lizhi.html", "lizhi-fixture.mp3"),
        ]
        for source, filename, audio_name in cases:
            with self.subTest(source=source), patch.object(MODULE, "get_ytdlp_media_info", return_value=None), patch.object(
                MODULE, "fetch_text", return_value=fixture(filename)
            ):
                info = MODULE.get_chinese_media_episode_info(f"https://example.com/{source}/fixture", source)
            self.assertEqual(info["source"], source)
            self.assertTrue(info["audio_url"].endswith(audio_name))

    def test_domestic_platform_dispatch(self) -> None:
        cases = [
            ("https://www.bilibili.com/video/BVfixture", "bilibili"),
            ("https://music.163.com/#/program?id=fixture", "netease_cloud_music"),
            ("https://www.ximalaya.com/sound/fixture", "ximalaya"),
            ("https://www.lizhi.fm/fixture/episode", "lizhi_fm"),
        ]
        for url, source in cases:
            with self.subTest(source=source), patch.object(
                MODULE, "get_chinese_media_episode_info", return_value={"source": source}
            ) as handler:
                info = MODULE.resolve_url_info(url)
            self.assertEqual(info["source"], source)
            handler.assert_called_once_with(url, source)

    def test_sensevoice_model_is_cached(self) -> None:
        calls = []

        class FakeModel:
            pass

        def fake_auto_model(**kwargs):
            calls.append(kwargs)
            return FakeModel()

        old_model = MODULE._sensevoice_model
        try:
            MODULE._sensevoice_model = None
            fake_funasr = types.SimpleNamespace(AutoModel=fake_auto_model)
            with patch.dict(sys.modules, {"funasr": fake_funasr}):
                first = MODULE.get_sensevoice_model()
                second = MODULE.get_sensevoice_model()
            self.assertIs(first, second)
            self.assertEqual(len(calls), 1)
        finally:
            MODULE._sensevoice_model = old_model

    def test_stitch_short_audio_uses_whisper_without_cutting(self) -> None:
        expected = {"text": "short transcript", "segments": [], "language": "zh", "model": "fake-whisper"}
        with patch.object(MODULE, "get_audio_duration", return_value=5), patch.object(
            MODULE, "transcribe_with_whisper", return_value=expected
        ) as transcribe:
            info = MODULE.transcribe_with_stitch(Path("/tmp/not-created.wav"), "small", "prompt")
        self.assertEqual(info["text"], "short transcript")
        transcribe.assert_called_once()


if __name__ == "__main__":
    unittest.main()
