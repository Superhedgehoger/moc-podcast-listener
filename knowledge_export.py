#!/usr/bin/env python3
"""Rebuildable exports for common PKM and agent workflows."""

from __future__ import annotations

import csv
import io
import json
import os
import re
from pathlib import Path
from typing import Any

from knowledge_base import (
    KNOWLEDGE_INDEX_FILENAME,
    atomic_write_text,
    load_knowledge_index,
    rebuild_knowledge_index,
)


EXPORT_FORMATS = {"obsidian", "notion", "zotero", "notebooklm", "mcp"}


def _safe_name(value: Any) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", str(value or "未命名")).strip(" .")
    return cleaned[:180] or "未命名"


def _source_path(output_dir: Path, raw_path: Any) -> Path | None:
    if not raw_path:
        return None
    path = Path(str(raw_path)).expanduser()
    return path if path.is_absolute() else output_dir / path


def _read_source(output_dir: Path, raw_path: Any) -> str:
    path = _source_path(output_dir, raw_path)
    if not path or not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def _markdown_link(export_file: Path, output_dir: Path, raw_path: Any, label: str) -> str:
    source = _source_path(output_dir, raw_path)
    if not source:
        return ""
    relative = os.path.relpath(source, export_file.parent).replace(os.sep, "/")
    return f"[{label}](<{relative}>)"


def _insights_markdown(record: dict[str, Any]) -> str:
    lines: list[str] = []
    for insight in record.get("insights") or []:
        if not isinstance(insight, dict):
            continue
        lines.append(f"- {insight.get('claim') or ''}")
        for evidence in insight.get("evidence") or []:
            if not isinstance(evidence, dict):
                continue
            try:
                start = float(evidence.get("start") or 0.0)
                end = float(evidence.get("end") or start)
            except (TypeError, ValueError):
                start = 0.0
                end = 0.0
            kind = "引述" if evidence.get("kind") == "quote" else "转述"
            lines.append(
                f"  - {kind} [{start:.1f}s-{end:.1f}s]：{evidence.get('quote') or ''}"
            )
    return "\n".join(lines) or "- 尚未生成结构化洞察"


def _episode_markdown(record: dict[str, Any], export_file: Path, output_dir: Path) -> str:
    paths = record.get("paths") or {}
    links = [
        _markdown_link(export_file, output_dir, paths.get("report"), "总结稿"),
        _markdown_link(export_file, output_dir, paths.get("transcript"), "转录稿"),
        _markdown_link(export_file, output_dir, paths.get("personal_notes"), "我的笔记"),
        _markdown_link(export_file, output_dir, paths.get("knowledge"), "knowledge.json"),
    ]
    return "\n".join(
        [
            f"# {record.get('show')}｜{record.get('title')}",
            "",
            f"- 原始链接：{record.get('url') or '未获取'}",
            f"- 发布日期：{record.get('publication_date') or '未获取'}",
            f"- 主题：{', '.join(record.get('topics') or []) or '未生成'}",
            f"- 标签：{', '.join((record.get('ai_tags') or []) + (record.get('user_tags') or [])) or '无'}",
            f"- 本地资料：{' · '.join(link for link in links if link) or '未生成'}",
            "",
            "## 关键洞察与证据",
            "",
            _insights_markdown(record),
            "",
        ]
    )


def _export_obsidian(records: list[dict[str, Any]], output_dir: Path, root: Path) -> list[str]:
    written: list[str] = []
    for record in records:
        path = root / f"{_safe_name(record.get('id'))}.md"
        tags = (record.get("ai_tags") or []) + (record.get("user_tags") or [])
        frontmatter = [
            "---",
            f"title: {json.dumps(str(record.get('title') or ''), ensure_ascii=False)}",
            f"show: {json.dumps(str(record.get('show') or ''), ensure_ascii=False)}",
            f"source: {json.dumps(str(record.get('url') or ''), ensure_ascii=False)}",
            "tags:" if tags else "tags: []",
            *[f"  - {json.dumps(str(tag), ensure_ascii=False)}" for tag in tags],
            "---",
            "",
        ]
        atomic_write_text(path, "\n".join(frontmatter) + _episode_markdown(record, path, output_dir))
        written.append(str(path))
    return written


def _export_notion(records: list[dict[str, Any]], output_dir: Path, root: Path) -> list[str]:
    written: list[str] = []
    table = io.StringIO()
    writer = csv.writer(table)
    writer.writerow(["Title", "Show", "URL", "Published", "Topics", "Tags", "Markdown"])
    for record in records:
        path = root / f"{_safe_name(record.get('id'))}.md"
        report = _read_source(output_dir, (record.get("paths") or {}).get("report"))
        notes = _read_source(output_dir, (record.get("paths") or {}).get("personal_notes"))
        body = _episode_markdown(record, path, output_dir)
        if report:
            body += "\n## 正式总结\n\n" + report + "\n"
        if notes:
            body += "\n## 我的笔记\n\n" + notes + "\n"
        atomic_write_text(path, body)
        written.append(str(path))
        writer.writerow(
            [
                record.get("title"),
                record.get("show"),
                record.get("url"),
                record.get("publication_date"),
                ", ".join(record.get("topics") or []),
                ", ".join((record.get("ai_tags") or []) + (record.get("user_tags") or [])),
                path.name,
            ]
        )
    csv_path = root / "Notion导入索引.csv"
    atomic_write_text(csv_path, table.getvalue())
    written.append(str(csv_path))
    return written


def _export_zotero(records: list[dict[str, Any]], root: Path) -> list[str]:
    items: list[dict[str, Any]] = []
    for record in records:
        year = str(record.get("publication_date") or "")[:4]
        item = {
            "id": record.get("id"),
            "type": "broadcast",
            "title": record.get("title"),
            "container-title": record.get("show"),
            "URL": record.get("url"),
            "keyword": (record.get("ai_tags") or []) + (record.get("user_tags") or []),
            "abstract": _insights_markdown(record),
        }
        if year.isdigit():
            item["issued"] = {"date-parts": [[int(year)]]}
        items.append(item)
    path = root / "podcasts.csl.json"
    atomic_write_text(path, json.dumps(items, ensure_ascii=False, indent=2) + "\n")
    return [str(path)]


def _export_notebooklm(records: list[dict[str, Any]], output_dir: Path, root: Path) -> list[str]:
    written: list[str] = []
    for record in records:
        path = root / f"{_safe_name(record.get('id'))}.md"
        paths = record.get("paths") or {}
        sections = [_episode_markdown(record, path, output_dir)]
        for heading, key in (
            ("正式总结", "report"),
            ("完整转录稿", "transcript"),
            ("我的笔记", "personal_notes"),
        ):
            content = _read_source(output_dir, paths.get(key))
            if content:
                sections.extend([f"## {heading}", "", content, ""])
        atomic_write_text(path, "\n".join(sections))
        written.append(str(path))
    return written


def _export_mcp(records: list[dict[str, Any]], root: Path) -> list[str]:
    catalog_path = root / "catalog.json"
    jsonl_path = root / "knowledge.jsonl"
    atomic_write_text(
        catalog_path,
        json.dumps(
            {
                "schema_version": 1,
                "resource_type": "podcast_knowledge_library",
                "record_count": len(records),
                "records": records,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
    )
    atomic_write_text(
        jsonl_path,
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
    )
    return [str(catalog_path), str(jsonl_path)]


def export_library(
    output_dir: Path,
    *,
    formats: set[str] | None = None,
    export_dir: Path | None = None,
) -> dict[str, Any]:
    requested = formats or set(EXPORT_FORMATS)
    unknown = requested - EXPORT_FORMATS
    if unknown:
        raise ValueError(f"unsupported export formats: {', '.join(sorted(unknown))}")
    output_dir = output_dir.expanduser()
    index_path = output_dir / "资料" / KNOWLEDGE_INDEX_FILENAME
    if not index_path.is_file():
        rebuild_knowledge_index(output_dir)
    records = load_knowledge_index(index_path)
    root = (export_dir or output_dir / "资料" / "导出").expanduser()
    written: dict[str, list[str]] = {}
    for format_name in sorted(requested):
        format_root = root / format_name
        if format_name == "obsidian":
            written[format_name] = _export_obsidian(records, output_dir, format_root)
        elif format_name == "notion":
            written[format_name] = _export_notion(records, output_dir, format_root)
        elif format_name == "zotero":
            written[format_name] = _export_zotero(records, format_root)
        elif format_name == "notebooklm":
            written[format_name] = _export_notebooklm(records, output_dir, format_root)
        elif format_name == "mcp":
            written[format_name] = _export_mcp(records, format_root)
    manifest = {
        "schema_version": 1,
        "source_index": str(index_path),
        "record_count": len(records),
        "formats": written,
    }
    manifest_path = root / "manifest.json"
    atomic_write_text(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return {**manifest, "manifest_path": str(manifest_path), "export_dir": str(root)}
