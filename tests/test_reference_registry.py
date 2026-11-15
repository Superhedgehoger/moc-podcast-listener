"""Local provenance and model-wire budget checks, not semantic-quality tests."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from reference_registry import pack_entries, load_registry, expand_refs, expand_reduction, expand_coverage, fingerprint


class ReferenceRegistryTests(unittest.TestCase):
    def payload(self, refs):
        entries = [{"task_id": "reduce-01", "leaf_ids": refs,
                    "evidence": {"items": [{"claim": "merged topic", "details": "preserved examples and qualifications",
                                              "evidence_ids": refs}], "omitted": []}}]
        packed, nodes, registry = pack_entries(entries)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "registry.json"
        path.write_text(json.dumps(registry))
        return {"entries": packed, "leaf_ids": nodes, "reference_registry": {
            "path": str(path), "sha256": fingerprint(registry)}}

    def test_thousands_of_original_ids_do_not_enter_model_wire(self):
        refs = [f"extract-{i // 10:04d}:item-{i % 10}" for i in range(4000)]
        payload = self.payload(refs)
        self.assertLess(len(json.dumps(payload).encode()), 2000)
        self.assertEqual(expand_refs(payload, payload["leaf_ids"]), refs)
        self.assertNotIn(refs[-1], json.dumps(payload))

    def test_nested_reduction_keeps_all_original_provenance(self):
        refs = [f"extract-0000:item-{i}" for i in range(1000)]
        payload = self.payload(refs)
        raw = {"input_hash": "h", "items": [{"claim": "topic", "details": "cases retained", "evidence_ids": payload["leaf_ids"]}], "omitted": []}
        expanded = expand_reduction(payload, raw)
        self.assertEqual(expanded["items"][0]["evidence_ids"], refs)
        self.assertEqual(raw["items"][0]["evidence_ids"], payload["leaf_ids"])
        next_entries = [{"task_id": "reduce-02", "leaf_ids": refs, "evidence": expanded}]
        _, _, registry = pack_entries(next_entries)
        self.assertEqual(list(registry.values()), [refs])

    def test_registry_tampering_and_unknown_nodes_fail(self):
        payload = self.payload(["extract-0000:a"])
        with self.assertRaises(ValueError):
            expand_refs(payload, ["unknown"])
        path = Path(payload["reference_registry"]["path"])
        path.write_text('{"r0":["invented"]}')
        with self.assertRaisesRegex(ValueError, "registry changed"):
            load_registry(payload)

    def test_coverage_and_omission_expand_without_losing_members(self):
        refs = ["extract-0000:a", "extract-0000:b"]
        payload = self.payload(refs)
        rows = expand_coverage(payload, [{"evidence_id": payload["leaf_ids"][0], "section": "详细总结", "reason": "actual support"}])
        self.assertEqual([row["evidence_id"] for row in rows], refs)
        output = expand_reduction(payload, {"items": [], "omitted": [{"evidence_id": payload["leaf_ids"][0], "reason": "duplicate examples"}]})
        self.assertEqual([row["evidence_id"] for row in output["omitted"]], refs)

    def test_missing_source_members_are_not_silently_packed(self):
        with self.assertRaisesRegex(ValueError, "lost original"):
            pack_entries([{"task_id": "extract", "leaf_ids": ["a", "b"], "evidence": {
                "items": [{"claim": "one", "evidence_ids": ["a"]}]}}])

    def test_cross_dependency_reference_swap_rejected(self):
        with self.assertRaisesRegex(ValueError, "provenance group"):
            pack_entries([{"task_id": "one", "leaf_ids": ["a"], "evidence": {
                "items": [{"claim": "one", "evidence_ids": ["b"]}]}},
                {"task_id": "two", "leaf_ids": ["b"], "evidence": {
                "items": [{"claim": "two", "evidence_ids": ["a"]}]}}])

    def test_overlapping_node_omissions_are_preserved(self):
        entries = [{"task_id": "one", "leaf_ids": ["a"], "evidence": {"items": [
            {"claim": "first claim", "evidence_ids": ["a"]},
            {"claim": "second claim", "evidence_ids": ["a"]}]}}]
        _, nodes, registry = pack_entries(entries)
        path = Path(self.tmpdir()) / "registry.json"
        path.write_text(json.dumps(registry))
        payload = {"leaf_ids": nodes, "reference_registry": {"path": str(path), "sha256": fingerprint(registry)}}
        result = expand_coverage(payload, [{"evidence_id": nodes[0], "section": "详细总结", "reason": "covered"},
            {"evidence_id": nodes[1], "section": "", "reason": "second claim explicitly omitted"}])
        self.assertEqual(result[0]["section"], "")
        self.assertEqual(len(result[0]["coverage_records"]), 2)

    def tmpdir(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return tmp.name


if __name__ == "__main__":
    unittest.main()
