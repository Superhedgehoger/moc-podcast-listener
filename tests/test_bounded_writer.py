"""Offline contracts for bounded planning; no CLI or provider calls."""
import copy
import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("bounded_writer", ROOT / "scripts/bounded_writer.py")
writer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(writer)


def fixture():
    leaves = [f"extract-0000:item-{i}" for i in range(3)]
    payload = {"duration_minutes": 12, "leaf_ids": leaves,
               "section_minimums": writer.listener().report_section_minimums(12),
               "result": {"segments_path": "DO_NOT_READ"},
               "source_lookup": {"result": "DO_NOT_READ"},
               "entries": [{"evidence": {"items": [
                   {"claim": f"Claim {i}", "evidence_ids": [key],
                    "details": "Mechanism and qualification", "examples": ["Case"],
                    "numbers": ["42"], "limitations": ["Limited sample"],
                    "ambiguities": ["Uncertain name"], "topics": ["Term"]}
                   for i, key in enumerate(leaves)]}}]}
    sources = {"boundaries": [{"evidence_id": key, "start": i * 90 + 0.6,
                               "end": i * 90 + 80.6} for i, key in enumerate(leaves)],
               "verified_quotations": [{"evidence_id": key, "start": i * 90 + 2.6,
                                        "end": i * 90 + 80.6,
                                        "quote": f"Source {i}: " + "original evidence " * 8}
                                       for i, key in enumerate(leaves)]}
    return payload, sources


def valid_sections(payload):
    return {name: "Supported analysis " + "x" * minimum
            for name, minimum in payload["section_minimums"].items()
            if name != writer.QUOTE_SECTION}


class BoundedWriterTests(unittest.TestCase):
    def setUp(self):
        self.payload, self.sources = fixture()

    def test_independent_requests_preserve_nine_sections_and_budget(self):
        before = copy.deepcopy((self.payload, self.sources))
        requests = writer.plan(self.payload, self.sources)
        sections = {r.get("section", r["name"]) for r in requests if r["kind"] in {"section", "section_piece"}}
        self.assertEqual(sections, set(self.payload["section_minimums"]))
        self.assertEqual(requests[-1]["kind"], "knowledge")
        self.assertEqual((self.payload, self.sources), before)
        for request in requests:
            if request["prompt"] is None:
                self.assertEqual(request["name"], writer.QUOTE_SECTION)
                continue
            self.assertEqual(request["estimated_bytes"], len(request["prompt"].encode("utf-8")))
            self.assertLessEqual(request["estimated_bytes"], 24000)
            self.assertIn("untrusted source data", request["prompt"])
            self.assertNotIn("DO_NOT_READ", request["prompt"])
            self.assertNotIn("original evidence", request["prompt"])

    def test_relevant_details_and_strict_minima_not_weakened(self):
        requests = {r["name"]: r for r in writer.plan(self.payload, self.sources)}
        overview = requests["内容摘要"]["prompt"]
        pieces = writer.detail_requests(self.payload)
        detail = pieces[0]["prompt"]
        self.assertNotIn('"examples"', overview)
        for field in ("examples", "numbers", "limitations", "ambiguities"):
            self.assertIn(f'"{field}"', detail)
        data = json.loads(detail.split("\nINPUT DATA:\n")[1])
        self.assertGreaterEqual(sum(piece["minimum_visible_chars"] for piece in pieces), 700)
        self.assertGreater(data["target_visible_chars"], data["minimum_visible_chars"])

    def test_detail_pieces_cover_all_claims_without_duplication_and_keep_final_floor(self):
        pieces = writer.detail_requests(self.payload)
        claims = [claim for piece in pieces for claim in json.loads(piece["prompt"].split("\nINPUT DATA:\n")[1])["claims"]]
        self.assertEqual(claims, writer._evidence(self.payload, writer.FIELDS["详细总结"])["claims"])
        text = "x" * pieces[0]["minimum_visible_chars"]
        self.assertEqual(writer.validate_piece(pieces[0], text, self.payload, self.sources), text)
        with self.assertRaisesRegex(ValueError, "Short section"):
            writer.validate_piece(pieces[0], "short", self.payload, self.sources)
        with self.assertRaisesRegex(ValueError, "Short section"):
            writer.validate_section("详细总结", text, self.payload, self.sources)
        altered = {**pieces[0], "prompt": "changed"}
        with self.assertRaises(ValueError):
            writer.validate_piece(altered, text, self.payload, self.sources)
        with self.assertRaisesRegex(ValueError, "URL is absent"):
            writer.validate_piece(pieces[0], text + " https://invented.example/", self.payload, self.sources)

    def test_coverage_retains_all_leaf_ids_and_semantic_claims(self):
        request = writer.coverage_request(self.payload, self.sources)
        data = json.loads(request["prompt"].split("\nINPUT DATA:\n")[1])
        self.assertEqual(data["leaf_ids"], self.payload["leaf_ids"])
        self.assertEqual(len(data["claims"]), 3)
        self.assertIn("not merely mentions its ID or topic", request["prompt"])
        self.assertIn("provisional allocation", request["prompt"])

    def test_coverage_can_check_actual_sections(self):
        sections = valid_sections(self.payload)
        request = writer.coverage_request(self.payload, self.sources, sections)
        data = json.loads(request["prompt"].split("\nINPUT DATA:\n")[1])
        self.assertEqual(set(data["sections"]), set(self.payload["section_minimums"]))
        self.assertIn("Supported analysis", data["sections"]["详细总结"])

    def test_upstream_omissions_are_explicit_not_erased(self):
        evidence = self.payload["entries"][0]["evidence"]
        evidence["items"].pop()
        evidence["omitted"] = [{"evidence_id": self.payload["leaf_ids"][-1], "reason": "Duplicate case"}]
        request = writer.coverage_request(self.payload, self.sources)
        self.assertIn("Duplicate case", request["prompt"])
        self.assertIn(self.payload["leaf_ids"][-1], request["prompt"])

    def test_over_budget_unicode_fails_without_truncation(self):
        self.payload["entries"][0]["evidence"]["items"][0]["details"] = "证" * 8000
        before = copy.deepcopy(self.payload)
        with self.assertRaisesRegex(ValueError, "exceeds 23000 UTF-8 bytes.*no evidence was truncated"):
            writer.plan(self.payload, self.sources)
        self.assertEqual(self.payload, before)

    def test_coverage_large_leaf_set_fails_not_silently_dropped(self):
        self.payload["leaf_ids"] = ["leaf-" + str(i) + "x" * 40 for i in range(1000)]
        self.sources["boundaries"] = [{"evidence_id": key, "start": 0, "end": 1}
                                      for key in self.payload["leaf_ids"]]
        self.payload["entries"] = [{"evidence": {"items": [{"claim": "One claim",
                                  "evidence_ids": self.payload["leaf_ids"], "details": "Detail"}]}}]
        with self.assertRaisesRegex(ValueError, "request 'coverage' exceeds"):
            writer.coverage_request(self.payload, self.sources)

    def test_mechanical_quotes_use_verified_original_rounding(self):
        text = writer.validate_section(writer.QUOTE_SECTION, "invented model quote", self.payload, self.sources)
        self.assertIn("[00:00:03]", text)
        self.assertIn("[00:01:33]", text)
        self.assertNotIn("invented model quote", text)
        self.assertEqual(len(text.splitlines()), 3)
        for quote in self.sources["verified_quotations"]:
            self.assertIn(quote["quote"].strip(), text)

    def test_missing_short_or_duplicate_quotes_fail_without_padding(self):
        for kind in ("missing", "short", "duplicate"):
            sources = copy.deepcopy(self.sources)
            if kind == "missing":
                sources["verified_quotations"].pop()
            elif kind == "short":
                for quote in sources["verified_quotations"]:
                    quote["quote"] = "short"
            else:
                sources["verified_quotations"] = [sources["verified_quotations"][0]] * 3
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                writer.quotation_section(self.payload, sources)

    def test_invalid_quote_timestamp_rejected_not_clamped_to_zero(self):
        for start in (None, -1, float("nan"), float("inf"), True):
            sources = copy.deepcopy(self.sources)
            sources["verified_quotations"][0]["start"] = start
            with self.subTest(start=start), self.assertRaisesRegex(ValueError, "original timestamp"):
                writer.quotation_section(self.payload, sources)

    def test_quote_timestamp_must_stay_in_its_original_leaf(self):
        self.sources["verified_quotations"][0]["start"] = 9000
        with self.assertRaisesRegex(ValueError, "outside its original leaf"):
            writer.quotation_section(self.payload, self.sources)

    def test_actual_coverage_over_budget_fails_without_dropping_sections(self):
        sections = valid_sections(self.payload)
        sections["详细总结"] = "evidence " * 4000
        with self.assertRaisesRegex(ValueError, "request 'coverage' exceeds"):
            writer.coverage_request(self.payload, self.sources, sections)

    def test_coverage_batches_all_leaves_once_and_keeps_complete_body(self):
        leaves = [f"leaf-{i}" for i in range(100)]
        self.payload["leaf_ids"] = leaves
        self.sources["boundaries"] = [{"evidence_id": key, "start": 0, "end": 1000}
                                      for key in leaves]
        for quote, key in zip(self.sources["verified_quotations"], leaves):
            quote["evidence_id"] = key
        self.payload["entries"] = [{"evidence": {"items": [
            {"claim": f"Claim {i}", "details": "Full semantic detail " * 30,
             "evidence_ids": [key]} for i, key in enumerate(leaves)]}}]
        sections = valid_sections(self.payload)
        requests = writer.coverage_requests(self.payload, self.sources, sections)
        self.assertGreater(len(requests), 1)
        self.assertEqual([key for request in requests for key in request["leaf_ids"]], leaves)
        again = writer.coverage_requests(self.payload, self.sources, sections)
        self.assertEqual(requests, again)
        for request in requests:
            data = json.loads(request["prompt"].split("\nINPUT DATA:\n")[1])
            self.assertLessEqual(request["estimated_bytes"], 24000)
            self.assertEqual(set(data["sections"]), set(self.payload["section_minimums"]))
            self.assertEqual(data["sections"]["详细总结"], sections["详细总结"])
            self.assertEqual([key for item in data["claims"] for key in item["evidence_ids"]], request["leaf_ids"])
            self.assertTrue(all(item["details"] == "Full semantic detail " * 30 for item in data["claims"]))

    def test_coverage_single_claim_and_body_too_big_is_blocking(self):
        self.payload["entries"][0]["evidence"]["items"][0]["details"] = "evidence " * 4000
        with self.assertRaisesRegex(ValueError, "Coverage blocked: single leaf.*all body sections"):
            writer.coverage_requests(self.payload, self.sources, valid_sections(self.payload))

    def test_knowledge_provenance_over_budget_fails_without_dropping_ids(self):
        leaves = [f"leaf-{i}" for i in range(500)]
        self.payload["leaf_ids"] = leaves
        self.payload["entries"] = [{"evidence": {"items": [{"claim": "Common mechanism",
                                  "details": "Supported detail", "evidence_ids": leaves}]}}]
        self.sources["boundaries"] = [{"evidence_id": key, "start": 0, "end": 1000}
                                      for key in leaves]
        for quote, key in zip(self.sources["verified_quotations"], leaves):
            quote["evidence_id"] = key
        self.assertGreater(len(json.dumps(self.sources["boundaries"]).encode()), 24000)
        before = copy.deepcopy((self.payload, self.sources))
        with self.assertRaisesRegex(ValueError, "request 'knowledge' exceeds 23000 UTF-8 bytes"):
            writer.plan(self.payload, self.sources)
        self.assertEqual((self.payload, self.sources), before)

    def test_knowledge_multileaf_claim_keeps_all_ids_and_matching_boundaries(self):
        claim = self.payload["entries"][0]["evidence"]["items"][0]
        claim["evidence_ids"] = list(self.payload["leaf_ids"])
        claim["details"] = "Combined mechanism supported collectively across three leaves"
        requests = writer.plan(self.payload, self.sources)
        for request in requests[:9]:
            if request["prompt"]:
                self.assertNotIn('"boundaries"', request["prompt"])
        data = json.loads(requests[-1]["prompt"].split("\nINPUT DATA:\n")[1])
        self.assertEqual(data["claims"][0], claim)
        self.assertEqual(data["boundaries"], self.sources["boundaries"])
        self.assertIn("without source proof", requests[-1]["prompt"])
        self.assertIn("collective support", requests[-1]["prompt"])
        self.assertIn("report failure rather than returning fewer insights", requests[-1]["prompt"])

    def test_visible_minimum_uses_listener_utility(self):
        name = "实用资源"
        hidden = "[" + "x" * 500 + "](https://example.com)"
        with self.assertRaisesRegex(ValueError, "Short section"):
            writer.validate_section(name, hidden, self.payload, self.sources)
        text = "x" * self.payload["section_minimums"][name]
        self.assertEqual(writer.validate_section(name, text, self.payload, self.sources), text)

    def test_heading_injection_rejected(self):
        for heading in ("# Title", "## 内容摘要", "### Subheading", "  ## 内容摘要", "Injected\n==="):
            with self.subTest(heading=heading), self.assertRaisesRegex(ValueError, "heading injection"):
                writer.validate_section("实用资源", "x" * 100 + "\n" + heading, self.payload, self.sources)

    def test_fake_timestamps_urls_and_shownotes_rejected(self):
        for addition in ("00:00:42", "00:00:00", "https://invented.invalid", "Show Notes"):
            with self.subTest(addition=addition), self.assertRaises(ValueError):
                writer.validate_section("实用资源", "x" * 100 + addition, self.payload, self.sources)
        writer.validate_section("实用资源", "x" * 100 + "00:01:21", self.payload, self.sources)

    def test_missing_semantic_claim_boundaries_or_weakened_sections_fail(self):
        for kind in ("claim", "boundary", "minimum", "section"):
            payload, sources = fixture()
            if kind == "claim":
                payload["entries"][0]["evidence"]["items"].pop()
            elif kind == "boundary":
                sources["boundaries"].pop()
            elif kind == "minimum":
                payload["section_minimums"]["详细总结"] = 1
            else:
                payload["section_minimums"].pop("内容摘要")
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                writer.plan(payload, sources)

    def test_assembly_is_nine_sections_and_ignores_model_quotes(self):
        sections = valid_sections(self.payload)
        sections[writer.QUOTE_SECTION] = "FAKE QUOTATION"
        body = writer.assemble_sections(sections, self.payload, self.sources)
        self.assertEqual(list(writer.listener().parse_report_sections(body)), list(self.payload["section_minimums"]))
        self.assertNotIn("FAKE QUOTATION", body)
        for bad in ({**sections, "基本信息": "bad"}, {"内容摘要": "short"}):
            with self.assertRaises(ValueError):
                writer.assemble_sections(bad, self.payload, self.sources)


if __name__ == "__main__":
    unittest.main()
