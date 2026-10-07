#!/usr/bin/env python3

import copy
import hashlib
import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


SPEC = importlib.util.spec_from_file_location(
    "chunk_transcript", Path(__file__).resolve().parents[1] / "chunk_transcript.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def segments(*texts):
    return [dict(start=i, end=i + 1, text=text, speaker="A") for i, text in enumerate(texts)]


class SegmentChunkingTests(unittest.TestCase):
    def test_segment_count_cap_keeps_every_primary_and_complete_overlap(self):
        source = segments(*[str(i) for i in range(75)])
        result = MODULE.budget_chunks(source, 8000, max_segments=32)
        self.assertEqual([len(c["segments"]) for c in result["chunks"]], [32, 32, 11])
        self.assertEqual([s["text"] for c in result["chunks"] for s in c["segments"]], [s["text"] for s in source])
        self.assertEqual(result["chunks"][1]["context_segments"], [result["chunks"][0]["segments"][-1]])
        for cap in (0, -1, True, "32"):
            with self.assertRaises(ValueError):
                MODULE.budget_chunks(source, 8000, max_segments=cap)

    def test_exact_boundary_and_newline_cost(self):
        source = segments("abc", "de", "f")
        result = MODULE.budget_chunks(source, 6)
        self.assertEqual([len(c["segments"]) for c in result["chunks"]], [2, 1])
        self.assertEqual(result["chunks"][0]["estimated_tokens"], 6)
        self.assertEqual(result["estimator"], "utf8_bytes_upper_bound")

    def test_context_is_complete_previous_segment_and_within_budget(self):
        result = MODULE.budget_chunks(segments("aaaa", "bb", "ccccc", "ddddd"), 8)
        chunks = result["chunks"]
        self.assertEqual(chunks[0]["context_segments"], [])
        self.assertEqual(chunks[1]["context_segments"], [chunks[0]["segments"][-1]])
        self.assertEqual(chunks[1]["estimated_tokens"], 8)
        self.assertEqual(chunks[2]["context_segments"], [])
        for chunk in chunks:
            text = "\n".join(s["text"] for s in chunk["context_segments"] + chunk["segments"])
            self.assertEqual(chunk["estimated_tokens"], len(text.encode("utf-8")))
            self.assertLessEqual(chunk["estimated_tokens"], 8)

    def test_stability_no_omissions_metadata_and_original_hash(self):
        source = segments(" abc ", "same", "same", "\nlast\n", "")
        original = copy.deepcopy(source)
        result = MODULE.budget_chunks(source, 10)
        self.assertEqual(result, MODULE.budget_chunks(iter(source), 10))
        self.assertEqual(source, original)
        primary = [s for c in result["chunks"] for s in c["segments"]]
        self.assertEqual(len({s["id"] for s in primary}), len(source))
        for item, expected in zip(primary, source):
            self.assertEqual({k: v for k, v in item.items() if k != "id"}, expected)
        self.assertEqual(len(primary), len(source))
        for chunk in result["chunks"]:
            raw = "\n".join(s["text"] for s in chunk["segments"])
            self.assertEqual(chunk["source_hash"], hashlib.sha256(raw.encode("utf-8")).hexdigest())
        other_budget = MODULE.budget_chunks(source, 20)
        self.assertEqual([s["id"] for s in primary], [s["id"] for c in other_budget["chunks"] for s in c["segments"]])
        changed = MODULE.budget_chunks(segments("different"), 10)
        self.assertNotEqual(primary[0]["id"], changed["chunks"][0]["segments"][0]["id"])

    def test_utf8_budget_and_oversized_segment(self):
        source = segments("\u4e2d\u6587")
        self.assertEqual(MODULE.budget_chunks(source, 6)["chunks"][0]["estimated_tokens"], 6)
        with self.assertRaisesRegex(ValueError, "Segment 0 .*requires 6.*cannot be split"):
            MODULE.budget_chunks(source, 5)

    def test_empty_input_and_empty_text(self):
        self.assertEqual(MODULE.budget_chunks([])["chunks"], [])
        chunk = MODULE.budget_chunks(segments(""), 1)["chunks"][0]
        self.assertEqual(len(chunk["segments"]), 1)
        self.assertEqual(chunk["estimated_tokens"], 0)

    def test_invalid_budget_and_segment(self):
        for budget in (0, -1, True, 1.5, "8"):
            with self.subTest(budget=budget), self.assertRaises(ValueError):
                MODULE.budget_chunks([], budget)
        for item in ({"text": "a"}, {"start": 0, "end": 1, "text": None}, "a"):
            with self.subTest(item=item), self.assertRaisesRegex(ValueError, "Segment 0"):
                MODULE.budget_chunks([item])

    def test_existing_id_and_extra_fields_are_preserved(self):
        source = segments("a")
        source[0].update(id="existing", confidence=0.9)
        self.assertEqual(MODULE.budget_chunks(source)["chunks"][0]["segments"], source)

    def test_missing_tokenizer_and_unknown_model_fall_back(self):
        with patch.dict("sys.modules", {"tiktoken": None}):
            self.assertEqual(MODULE.budget_chunks(segments("abc"), model="known")["estimator"], "utf8_bytes_upper_bound")
        lookup = Mock(side_effect=KeyError("unknown"))
        with patch.dict("sys.modules", {"tiktoken": SimpleNamespace(encoding_for_model=lookup)}):
            result = MODULE.budget_chunks(segments("abc"), model="unknown")
            self.assertEqual(result["estimator"], "utf8_bytes_upper_bound")
            self.assertEqual(result["chunks"][0]["estimated_tokens"], 3)
            lookup.assert_called_once_with("unknown")

    def test_known_tokenizer_counts_context_and_literal_special_tokens(self):
        encode = Mock(side_effect=lambda text, **kwargs: list(text))
        encoding = SimpleNamespace(name="test_encoding", encode=encode)
        lookup = Mock(return_value=encoding)
        with patch.dict("sys.modules", {"tiktoken": SimpleNamespace(encoding_for_model=lookup)}):
            result = MODULE.budget_chunks(segments("aaaa", "bb", "ccccc"), 8, "known")
            self.assertEqual(result["estimator"], "tiktoken:test_encoding")
            self.assertEqual(result["chunks"][1]["estimated_tokens"], 8)
            self.assertEqual(len(result["chunks"][1]["context_segments"]), 1)
            lookup.assert_called_once_with("known")
            MODULE.budget_chunks(segments("<|endoftext|>"), model="known")
            self.assertTrue(all(c.kwargs == {"disallowed_special": ()} for c in encode.call_args_list))
            lookup.reset_mock()
            MODULE.budget_chunks(segments("a"))
            lookup.assert_not_called()


if __name__ == "__main__":
    unittest.main()
