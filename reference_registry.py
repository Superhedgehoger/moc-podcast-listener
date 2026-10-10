"""Bounded model references with complete, hash-checked provenance on disk."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def pack_entries(entries):
    packed, registry = [], {}
    expected = {ref for entry in entries for ref in entry["leaf_ids"]}
    for entry in entries:
        dependency_refs = set(entry["leaf_ids"])
        evidence = {"items": [], "omitted": []}
        for item in entry["evidence"].get("items", []):
            refs = list(dict.fromkeys(item["evidence_ids"]))
            if not refs or not set(refs) <= dependency_refs:
                raise ValueError("Unknown or empty provenance group")
            key = f"r{len(registry):x}"
            registry[key] = refs
            evidence["items"].append({**copy.deepcopy(item), "evidence_ids": [key]})
        for item in entry["evidence"].get("omitted", []):
            ref = item["evidence_id"]
            if ref not in dependency_refs or not str(item.get("reason", "")).strip():
                raise ValueError("Invalid provenance omission")
            key = f"r{len(registry):x}"
            registry[key] = [ref]
            evidence["omitted"].append({**copy.deepcopy(item), "evidence_id": key})
        packed.append({"task_id": entry["task_id"], "evidence": evidence})
    if {ref for refs in registry.values() for ref in refs} != expected:
        raise ValueError("Provenance packing lost original references")
    return packed, list(registry), registry


def load_registry(payload):
    pointer = payload.get("reference_registry")
    if pointer is None:
        return {key: [key] for key in payload["leaf_ids"]}
    registry = json.loads(Path(pointer["path"]).read_text(encoding="utf-8"))
    if not isinstance(registry, dict) or fingerprint(registry) != pointer["sha256"]:
        raise ValueError("Reference registry changed or invalid")
    if set(registry) != set(payload["leaf_ids"]):
        raise ValueError("Reference registry scope mismatch")
    for refs in registry.values():
        if (not isinstance(refs, list) or not refs or len(set(refs)) != len(refs)
                or any(not isinstance(ref, str) or not ref for ref in refs)):
            raise ValueError("Invalid original provenance members")
    return registry


def expand_refs(payload, refs):
    registry = load_registry(payload)
    result = []
    for ref in refs:
        if ref not in registry:
            raise ValueError(f"Unknown reference node: {ref}")
        result.extend(registry[ref])
    return list(dict.fromkeys(result))


def expand_reduction(payload, output):
    result = copy.deepcopy(output)
    for item in result.get("items", []):
        item["evidence_ids"] = expand_refs(payload, item["evidence_ids"])
    result["omitted"] = [
        {**item, "evidence_id": ref}
        for item in result.get("omitted", [])
        for ref in expand_refs(payload, [item["evidence_id"]])]
    return result


def expand_coverage(payload, rows):
    expanded = {}
    for item in rows:
        for ref in expand_refs(payload, [item["evidence_id"]]):
            record = {**item, "reference_node": item["evidence_id"]}
            if ref not in expanded:
                expanded[ref] = {**item, "evidence_id": ref, "coverage_records": []}
            expanded[ref]["coverage_records"].append(record)
            if not item.get("section"):
                expanded[ref]["section"] = ""
                expanded[ref]["reason"] = item.get("reason", "")
    return list(expanded.values())
