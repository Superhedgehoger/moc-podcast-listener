"""Select complete body passages locally; fidelity is checked independently."""
import re

from scripts.linear_review import _rank, _strings, _terms
from scripts.semantic_review import _sentences
from summary_workflow import digest

BODY_RESERVE = 8000


def passages(sections):
    result = []
    for heading, text in sections.items():
        if heading == "关键引述":
            continue
        for paragraph in re.split(r"\n\s*\n", text):
            if not paragraph.strip():
                continue
            units = [paragraph]
            if len(paragraph.encode("utf-8")) > BODY_RESERVE:
                # Preserve complete sentences when an entire paragraph is oversized.
                units = _sentences(paragraph)
                if "".join(units) != paragraph:
                    raise ValueError("Coverage blocked: body sentence splitting lost content")
            for unit in units:
                if len(unit.encode("utf-8")) > BODY_RESERVE:
                    raise ValueError("Coverage blocked: indivisible body passage exceeds 8000 UTF-8 bytes")
                result.append({"id": f"p{len(result):04d}", "section": heading, "text": unit})
    return result


def coverage_requests(payload, evidence, actual, build_request):
    fragments = passages(actual)
    fragment_terms = [_terms(item["text"]) for item in fragments]
    body_hash = digest(actual)
    leaves = payload["leaf_ids"]

    def request(batch):
        wanted = set(batch)
        claims = [{**item, "evidence_ids": [key for key in item["evidence_ids"] if key in wanted]}
                  for item in evidence["claims"] if wanted.intersection(item["evidence_ids"])]
        filtered = {"claims": claims, "upstream_omissions": [item for item in evidence["upstream_omissions"]
                                                             if item["evidence_id"] in wanted]}
        queries = [_terms("\n".join(_strings({key: value for key, value in item.items()
                                               if key != "evidence_ids"}))) for item in claims]
        required = set()
        for query in queries:
            scores = [sum((query & candidate).values()) for candidate in fragment_terms]
            if scores and max(scores) > 0:
                required.add(max(range(len(scores)), key=lambda i: scores[i]))

        def build(selected):
            grouped = {}
            for i in sorted(selected):
                item = fragments[i]
                grouped.setdefault(item["section"], []).append(item["text"])
            sections = {name: "\n\n".join(parts) for name, parts in grouped.items()}
            scope = {"kind": "complete_body_excerpts", "whole_body_hash": body_hash,
                     "passage_ids": [fragments[i]["id"] for i in sorted(selected)]}
            result = build_request(payload, filtered, batch, sections, scope=scope)
            result["section_scope"] = list(sections)
            result["coverage_scope"] = scope
            return result

        selected = set(required)
        try:
            candidate = build(selected)
        except ValueError as exc:
            raise ValueError("Coverage blocked: complete claims and their reserved best body passages exceed budget") from exc
        if queries:
            body_bytes = sum(len(fragments[i]["text"].encode("utf-8")) for i in selected)
            for i in _rank(queries, fragment_terms):
                if i in selected or body_bytes + len(fragments[i]["text"].encode("utf-8")) > BODY_RESERVE:
                    continue
                try:
                    enlarged = build(selected | {i})
                except ValueError:
                    continue
                selected.add(i)
                body_bytes += len(fragments[i]["text"].encode("utf-8"))
                candidate = enlarged
        return candidate

    requests, batch, previous = [], [], None
    for leaf in leaves:
        try:
            candidate = request(batch + [leaf])
        except ValueError:
            if not batch:
                raise
            requests.append(previous)
            batch = []
            candidate = request([leaf])
        batch.append(leaf)
        previous = candidate
    if batch:
        requests.append(previous)
    for i, item in enumerate(requests):
        item["name"] = f"coverage-scoped-{i:04d}"
    return requests
