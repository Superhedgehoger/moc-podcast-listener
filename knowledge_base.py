#!/usr/bin/env python3
"""Evidence, personal notes, indexing, and local search for podcast packages."""

from __future__ import annotations

import json
import os
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


KNOWLEDGE_SCHEMA_VERSION = 1
KNOWLEDGE_INDEX_FILENAME = "knowledge-index.jsonl"
PERSONAL_NOTES_FILENAME = "我的笔记.md"


def atomic_write_text(path: Path, content: str) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def read_json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def knowledge_template(
    episode: dict[str, Any],
    *,
    transcript_path: Path,
    report_path: Path,
) -> dict[str, Any]:
    return {
        "schema_version": KNOWLEDGE_SCHEMA_VERSION,
        "status": "awaiting_synthesis",
        "episode": {
            "show": episode.get("show_title") or "未知节目",
            "title": episode.get("title") or "未知单集",
            "url": episode.get("url"),
            "publication_date": episode.get("pub_date"),
        },
        "source": {
            "transcript_path": str(transcript_path),
            "report_path": str(report_path),
        },
        "topics": [],
        "entities": [],
        "ai_tags": [],
        "insights": [],
        "generated_at": None,
    }


def personal_notes_template(episode: dict[str, Any]) -> str:
    show = episode.get("show_title") or "未知节目"
    title = episode.get("title") or "未知单集"
    url = episode.get("url") or "未获取"
    return f"""# 我的笔记

- 节目：{show}
- 单集：{title}
- 原始链接：{url}
- 用户标签：

> 本文件只属于用户。重新转录、重新总结、迁移和导出都不得覆盖已有内容。

## 我的评论


## 我认同的观点


## 我不同意的观点


## 待验证问题


## 值得重听的片段


## 关联项目

"""


def ensure_episode_knowledge_files(
    episode_dir: Path,
    episode: dict[str, Any],
    *,
    transcript_path: Path,
    report_path: Path,
) -> tuple[Path, Path]:
    knowledge_path = episode_dir / "knowledge.json"
    notes_path = episode_dir / PERSONAL_NOTES_FILENAME
    if not knowledge_path.exists():
        atomic_write_text(
            knowledge_path,
            json.dumps(
                knowledge_template(
                    episode,
                    transcript_path=transcript_path,
                    report_path=report_path,
                ),
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )
    if not notes_path.exists():
        atomic_write_text(notes_path, personal_notes_template(episode))
    return knowledge_path, notes_path


def normalize_evidence_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").lower()
    return "".join(char for char in normalized if char.isalnum() or "\u4e00" <= char <= "\u9fff")


def load_segments(path: Path | None) -> list[dict[str, Any]]:
    if not path or not path.is_file():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def evidence_matches_segments(
    quote: str,
    start: float,
    end: float,
    segments: list[dict[str, Any]],
) -> bool:
    target = normalize_evidence_text(quote)
    if not target:
        return False
    for index in range(len(segments)):
        combined = ""
        window_start = float(segments[index].get("start") or 0.0)
        window_end = window_start
        for window_size in range(1, 5):
            if index + window_size > len(segments):
                break
            segment = segments[index + window_size - 1]
            combined += str(segment.get("text") or "")
            window_end = float(segment.get("end") or segment.get("start") or window_end)
            if target in normalize_evidence_text(combined):
                return start <= window_end + 8.0 and end >= window_start - 8.0
    return False


def validate_knowledge(
    knowledge_path: Path,
    *,
    transcript_path: Path | None = None,
    segments_path: Path | None = None,
    duration_minutes: float = 0.0,
    require_complete: bool = True,
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    try:
        payload = read_json_object(knowledge_path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return {"ok": False, "errors": [f"knowledge JSON is invalid: {exc}"], "warnings": []}

    if payload.get("schema_version") != KNOWLEDGE_SCHEMA_VERSION:
        errors.append(f"unsupported knowledge schema: {payload.get('schema_version')}")
    if require_complete and payload.get("status") != "complete":
        errors.append("knowledge status must be complete")
    for field in ("topics", "entities", "ai_tags", "insights"):
        if not isinstance(payload.get(field), list):
            errors.append(f"knowledge field must be an array: {field}")

    insights = payload.get("insights") if isinstance(payload.get("insights"), list) else []
    if require_complete and not insights:
        errors.append("knowledge must contain at least one insight")
    transcript = ""
    if transcript_path and transcript_path.is_file():
        transcript = transcript_path.read_text(encoding="utf-8")
    normalized_transcript = normalize_evidence_text(transcript)
    segments = load_segments(segments_path)
    duration_seconds = max(0.0, duration_minutes * 60.0)
    insight_ids: set[str] = set()

    for number, insight in enumerate(insights, start=1):
        prefix = f"insight[{number}]"
        if not isinstance(insight, dict):
            errors.append(f"{prefix} must be an object")
            continue
        insight_id = str(insight.get("id") or "").strip()
        if not insight_id:
            errors.append(f"{prefix} is missing id")
        elif insight_id in insight_ids:
            errors.append(f"duplicate insight id: {insight_id}")
        insight_ids.add(insight_id)
        if not str(insight.get("claim") or "").strip():
            errors.append(f"{prefix} is missing claim")
        evidence_items = insight.get("evidence")
        if not isinstance(evidence_items, list) or not evidence_items:
            errors.append(f"{prefix} must contain evidence")
            continue
        for evidence_number, evidence in enumerate(evidence_items, start=1):
            evidence_prefix = f"{prefix}.evidence[{evidence_number}]"
            if not isinstance(evidence, dict):
                errors.append(f"{evidence_prefix} must be an object")
                continue
            kind = str(evidence.get("kind") or "quote").strip().lower()
            if kind not in {"quote", "paraphrase"}:
                errors.append(f"{evidence_prefix} has invalid kind: {kind}")
            quote = str(evidence.get("quote") or "").strip()
            if not quote:
                errors.append(f"{evidence_prefix} is missing evidence text")
            if kind == "quote" and len(normalize_evidence_text(quote)) < 8:
                errors.append(f"{evidence_prefix} quote is too short")
            try:
                start = float(evidence.get("start"))
                end = float(evidence.get("end"))
            except (TypeError, ValueError):
                errors.append(f"{evidence_prefix} has invalid timestamps")
                continue
            if start < 0 or end < start:
                errors.append(f"{evidence_prefix} has invalid timestamp range")
            if duration_seconds and end > duration_seconds + 10.0:
                errors.append(f"{evidence_prefix} exceeds episode duration")
            if kind == "quote":
                normalized_quote = normalize_evidence_text(quote)
                if normalized_transcript and normalized_quote not in normalized_transcript:
                    errors.append(f"{evidence_prefix} quote is not present in transcript")
                if segments and not evidence_matches_segments(quote, start, end, segments):
                    errors.append(f"{evidence_prefix} quote does not match its timestamp segments")
            confidence = str(evidence.get("confidence") or "").lower()
            if confidence not in {"high", "medium", "low"}:
                errors.append(f"{evidence_prefix} has invalid confidence")
            if not evidence.get("speaker"):
                warnings.append(f"{evidence_prefix} has no confirmed speaker")

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "insight_count": len(insights),
    }


def user_tags_from_notes(notes_path: Path) -> list[str]:
    if not notes_path.is_file():
        return []
    text = notes_path.read_text(encoding="utf-8")
    line = next((line for line in text.splitlines() if line.startswith("- 用户标签：")), "")
    tags = re.findall(r"#([\w\-\u4e00-\u9fff]+)", line)
    return list(dict.fromkeys(tags))


def _relative(path: Path, root: Path) -> str:
    try:
        return os.path.relpath(path, root)
    except ValueError:
        return str(path)


def package_record(metadata_path: Path, output_dir: Path) -> dict[str, Any] | None:
    try:
        metadata = read_json_object(metadata_path)
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    episode = metadata.get("episode") or {}
    if not isinstance(episode, dict):
        return None
    package_name = metadata_path.parent.name
    transcript_path = output_dir / "转录稿" / f"{package_name}_转录稿.txt"
    report_path = output_dir / "总结稿" / f"{package_name}_详细总结.md"
    knowledge_path = metadata_path.parent / "knowledge.json"
    notes_path = metadata_path.parent / PERSONAL_NOTES_FILENAME
    knowledge: dict[str, Any] = {}
    if knowledge_path.is_file():
        try:
            knowledge = read_json_object(knowledge_path)
        except (OSError, json.JSONDecodeError, ValueError):
            knowledge = {}
    transcription = metadata.get("transcription") or {}
    entities = knowledge.get("entities") if isinstance(knowledge.get("entities"), list) else []
    entity_names = [
        str(item.get("name"))
        for item in entities
        if isinstance(item, dict) and item.get("name")
    ]
    raw_guests = episode.get("guests") or []
    if isinstance(raw_guests, str):
        guests = [raw_guests]
    elif isinstance(raw_guests, list):
        guests = [str(item) for item in raw_guests if item]
    else:
        guests = []
    insights = knowledge.get("insights") if isinstance(knowledge.get("insights"), list) else []
    return {
        "id": package_name,
        "show": episode.get("show_title") or "未知节目",
        "title": episode.get("title") or package_name,
        "url": episode.get("url"),
        "publication_date": episode.get("pub_date"),
        "transcription_date": transcription.get("processed_at")
        if isinstance(transcription, dict)
        else None,
        "duration_minutes": episode.get("duration_minutes"),
        "people": list(dict.fromkeys(guests + entity_names)),
        "topics": knowledge.get("topics") if isinstance(knowledge.get("topics"), list) else [],
        "ai_tags": knowledge.get("ai_tags") if isinstance(knowledge.get("ai_tags"), list) else [],
        "user_tags": user_tags_from_notes(notes_path),
        "insights": insights,
        "knowledge_status": knowledge.get("status") or "missing",
        "paths": {
            "metadata": _relative(metadata_path, output_dir),
            "transcript": _relative(transcript_path, output_dir) if transcript_path.is_file() else None,
            "report": _relative(report_path, output_dir) if report_path.is_file() else None,
            "knowledge": _relative(knowledge_path, output_dir) if knowledge_path.is_file() else None,
            "personal_notes": _relative(notes_path, output_dir) if notes_path.is_file() else None,
        },
    }


def bootstrap_existing_packages(output_dir: Path) -> dict[str, int]:
    created_knowledge = 0
    created_notes = 0
    package_root = output_dir / "资料"
    for metadata_path in package_root.glob("*/metadata.json") if package_root.is_dir() else []:
        try:
            metadata = read_json_object(metadata_path)
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        episode = metadata.get("episode") or {}
        package_name = metadata_path.parent.name
        transcript_path = output_dir / "转录稿" / f"{package_name}_转录稿.txt"
        report_path = output_dir / "总结稿" / f"{package_name}_详细总结.md"
        knowledge_path = metadata_path.parent / "knowledge.json"
        notes_path = metadata_path.parent / PERSONAL_NOTES_FILENAME
        if not knowledge_path.exists():
            created_knowledge += 1
        if not notes_path.exists():
            created_notes += 1
        ensure_episode_knowledge_files(
            metadata_path.parent,
            episode if isinstance(episode, dict) else {},
            transcript_path=transcript_path,
            report_path=report_path,
        )
    return {"knowledge_created": created_knowledge, "notes_created": created_notes}


def rebuild_knowledge_index(output_dir: Path, *, bootstrap: bool = True) -> dict[str, Any]:
    output_dir = output_dir.expanduser()
    bootstrap_result = bootstrap_existing_packages(output_dir) if bootstrap else {}
    records: list[dict[str, Any]] = []
    package_root = output_dir / "资料"
    for metadata_path in package_root.glob("*/metadata.json") if package_root.is_dir() else []:
        record = package_record(metadata_path, output_dir)
        if record:
            records.append(record)
    records.sort(
        key=lambda item: str(item.get("transcription_date") or item.get("publication_date") or ""),
        reverse=True,
    )
    index_path = package_root / KNOWLEDGE_INDEX_FILENAME
    atomic_write_text(
        index_path,
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
    )
    return {
        "index_path": str(index_path),
        "record_count": len(records),
        **bootstrap_result,
    }


def load_knowledge_index(index_path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not index_path.is_file():
        return records
    for line in index_path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def _searchable_text(record: dict[str, Any]) -> str:
    insight_text = " ".join(
        str(item.get("claim") or "")
        for item in record.get("insights") or []
        if isinstance(item, dict)
    )
    values: Iterable[Any] = (
        record.get("show"),
        record.get("title"),
        " ".join(record.get("people") or []),
        " ".join(record.get("topics") or []),
        " ".join(record.get("ai_tags") or []),
        " ".join(record.get("user_tags") or []),
        insight_text,
    )
    return unicodedata.normalize("NFKC", " ".join(str(value or "") for value in values)).lower()


def search_knowledge(
    records: list[dict[str, Any]],
    *,
    query: str = "",
    person: str = "",
    tag: str = "",
    since: str = "",
    limit: int = 20,
) -> list[dict[str, Any]]:
    terms = [term.lower() for term in re.findall(r"[\w\u4e00-\u9fff]+", query)]
    matches: list[tuple[int, dict[str, Any]]] = []
    for record in records:
        searchable = _searchable_text(record)
        if person and person.lower() not in " ".join(record.get("people") or []).lower():
            continue
        tags = [str(value).lower() for value in (record.get("ai_tags") or []) + (record.get("user_tags") or [])]
        if tag and tag.lower() not in tags:
            continue
        record_date = str(record.get("transcription_date") or record.get("publication_date") or "")
        if since and record_date[:10].replace("-", "") < since.replace("-", "")[:8]:
            continue
        if terms and not all(term in searchable for term in terms):
            continue
        score = sum(searchable.count(term) for term in terms) if terms else 1
        matches.append((score, record))
    matches.sort(
        key=lambda item: (
            item[0],
            str(item[1].get("transcription_date") or item[1].get("publication_date") or ""),
        ),
        reverse=True,
    )
    return [record for _, record in matches[: max(1, limit)]]


def render_search_markdown(records: list[dict[str, Any]]) -> str:
    lines = ["# 播客知识库搜索结果", ""]
    if not records:
        return "\n".join(lines + ["没有匹配结果。", ""])
    for record in records:
        lines.extend(
            [
                f"## {record.get('show')}｜{record.get('title')}",
                "",
                f"- 原始链接：{record.get('url') or '未获取'}",
                f"- 主题：{', '.join(record.get('topics') or []) or '未生成'}",
                f"- 标签：{', '.join((record.get('ai_tags') or []) + (record.get('user_tags') or [])) or '无'}",
            ]
        )
        for insight in (record.get("insights") or [])[:5]:
            if not isinstance(insight, dict):
                continue
            lines.append(f"- 洞察：{insight.get('claim') or ''}")
            for evidence in (insight.get("evidence") or [])[:1]:
                if not isinstance(evidence, dict):
                    continue
                try:
                    start = float(evidence.get("start") or 0.0)
                    end = float(evidence.get("end") or start)
                except (TypeError, ValueError):
                    start = 0.0
                    end = 0.0
                lines.append(
                    f"  - 证据：{evidence.get('quote') or '转述'} "
                    f"[{start:.1f}s-{end:.1f}s]"
                )
        paths = record.get("paths") or {}
        lines.extend(
            [
                f"- 总结稿：{paths.get('report') or '未生成'}",
                f"- 转录稿：{paths.get('transcript') or '未生成'}",
                "",
            ]
        )
    return "\n".join(lines)
