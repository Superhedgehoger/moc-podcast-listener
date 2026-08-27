import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "migrate_output_layout.py"
SPEC = importlib.util.spec_from_file_location("migrate_output_layout", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class MigrateOutputLayoutTests(unittest.TestCase):
    def test_migrates_technical_files_and_rewrites_links(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            name = "节目_单集_20260828"
            transcript_dir = root / "转录稿"
            summary_dir = root / "总结稿"
            notes_dir = root / "Show Notes"
            assets_dir = root / "图片" / f"{name}_assets"
            for directory in (transcript_dir, summary_dir, notes_dir, assets_dir):
                directory.mkdir(parents=True, exist_ok=True)

            transcript = transcript_dir / f"{name}_转录稿.txt"
            transcript.write_text(
                f"[segments](<{name}_segments.json>)\n"
                f"[notes](<../Show Notes/{name}_shownotes.md>)\n",
                encoding="utf-8",
            )
            (transcript_dir / f"{name}_segments.json").write_text("[]\n")
            (transcript_dir / f"{name}.srt").write_text("subtitle\n")
            (transcript_dir / f"{name}.vtt").write_text("WEBVTT\n")
            chunks = transcript_dir / f"{name}_转录稿_chunks"
            chunks.mkdir()
            (chunks / "chunk_01.txt").write_text(
                f"[notes](<../Show Notes/{name}_shownotes.md>)\n", encoding="utf-8"
            )
            (root / f"{name}_metadata.json").write_text("{}\n")
            (root / f"{name}_Agent任务指令.txt").write_text(
                str(transcript_dir / f"{name}_segments.json"), encoding="utf-8"
            )
            image = assets_dir / "cover.jpg"
            image.write_bytes(b"image")
            (notes_dir / f"{name}_shownotes.md").write_text(
                f"![cover](../图片/{name}_assets/cover.jpg)\n", encoding="utf-8"
            )
            (notes_dir / f"{name}_shownotes.raw.html").write_text("<p>notes</p>")
            (notes_dir / f"{name}_media-manifest.json").write_text(
                json.dumps(
                    {
                        "images": [
                            {
                                "path": str(image),
                                "markdown_url": f"../图片/{name}_assets/cover.jpg",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            report = summary_dir / f"{name}_详细总结.md"
            report.write_text(
                f"![cover](../图片/{name}_assets/cover.jpg)\n"
                f"[srt](../转录稿/{name}.srt)\n",
                encoding="utf-8",
            )

            moves = MODULE.plan_moves(root)
            self.assertEqual(MODULE.validate_moves(moves), [])
            result = MODULE.apply_migration(root, moves)

            package = root / "资料" / name
            self.assertEqual(result["moved"], 10)
            self.assertTrue(transcript.exists())
            self.assertTrue((package / "转录数据" / "segments.json").exists())
            self.assertTrue((package / "转录数据" / "transcript.srt").exists())
            self.assertTrue((package / "Show Notes" / "shownotes.md").exists())
            self.assertTrue((package / "Show Notes" / "图片" / "cover.jpg").exists())
            self.assertIn(
                "../../Show Notes/shownotes.md",
                (package / "转录数据" / "分块" / "chunk_01.txt").read_text(
                    encoding="utf-8"
                ),
            )
            self.assertIn(
                f"../资料/{name}/转录数据/segments.json",
                transcript.read_text(encoding="utf-8"),
            )
            self.assertIn(
                f"../资料/{name}/Show Notes/图片/cover.jpg",
                report.read_text(encoding="utf-8"),
            )
            shownotes = (package / "Show Notes" / "shownotes.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("图片/cover.jpg", shownotes)
            manifest = json.loads(
                (package / "Show Notes" / "media-manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(manifest["layout"], "episode_directory")
            self.assertEqual(manifest["images"][0]["markdown_url"], "图片/cover.jpg")


if __name__ == "__main__":
    unittest.main()
