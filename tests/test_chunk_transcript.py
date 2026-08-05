#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("chunk_transcript", ROOT / "chunk_transcript.py")
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class ChunkTranscriptTests(unittest.TestCase):
    def test_chunks_respect_sentence_boundaries_and_overlap(self) -> None:
        text = "第一句内容。第二句内容很长。第三句内容。第四句内容。"
        chunks = MODULE.chunk_transcript(text, target_chars=12, overlap_chars=4)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(chunks[0].endswith("。"))
        self.assertIn("【上一块结尾上下文】", chunks[1])

    def test_empty_overlap_returns_plain_chunks(self) -> None:
        chunks = MODULE.chunk_transcript("甲。" * 20, target_chars=10, overlap_chars=0)
        self.assertTrue(all("当前块正文" not in chunk for chunk in chunks))


if __name__ == "__main__":
    unittest.main()
