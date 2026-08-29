#!/usr/bin/env python3
"""Low-cost RSS subscription discovery without automatic transcription."""

from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

from knowledge_base import atomic_write_text, read_json_object


SUBSCRIPTION_SCHEMA_VERSION = 1


def subscription_paths(output_dir: Path) -> dict[str, Path]:
    root = output_dir.expanduser() / "资料" / "订阅"
    return {
        "root": root,
        "config": root / "subscriptions.json",
        "state": root / "state.json",
        "brief_dir": output_dir.expanduser() / "资料" / "Brief",
    }


def subscription_template() -> dict[str, Any]:
    return {
        "schema_version": SUBSCRIPTION_SCHEMA_VERSION,
        "settings": {
            "max_items_per_feed": 20,
            "brief_limit": 20,
            "minimum_score": 0,
            "preferred_keywords": [],
            "excluded_keywords": [],
        },
        "subscriptions": [
            {
                "name": "示例节目（请替换或删除）",
                "feed_url": "https://example.com/feed.xml",
                "enabled": False,
                "priority": 1,
                "keywords": [],
            }
        ],
    }


def initialize_subscriptions(output_dir: Path) -> dict[str, Any]:
    paths = subscription_paths(output_dir)
    paths["root"].mkdir(parents=True, exist_ok=True)
    created = not paths["config"].exists()
    if created:
        atomic_write_text(
            paths["config"],
            json.dumps(subscription_template(), ensure_ascii=False, indent=2) + "\n",
        )
    if not paths["state"].exists():
        atomic_write_text(
            paths["state"],
            json.dumps(
                {"schema_version": 1, "seen": {}, "last_scan_at": None},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )
    return {
        "config_path": str(paths["config"]),
        "state_path": str(paths["state"]),
        "created": created,
    }


def _plain_text(value: Any) -> str:
    text = re.sub(r"<[^>]+>", " ", html.unescape(str(value or "")))
    return re.sub(r"\s+", " ", text).strip()


def _episode_key(feed_url: str, item: dict[str, Any]) -> str:
    identity = item.get("guid") or item.get("id") or item.get("url") or item.get("title")
    return hashlib.sha256(f"{feed_url}\n{identity}".encode("utf-8")).hexdigest()


def _score_item(
    item: dict[str, Any],
    subscription: dict[str, Any],
    settings: dict[str, Any],
) -> tuple[int, list[str]]:
    score = int(subscription.get("priority") or 1) * 10
    reasons = [f"订阅优先级 {int(subscription.get('priority') or 1)}"]
    transcripts = item.get("transcripts") or []
    if isinstance(transcripts, list) and transcripts:
        score += 25
        reasons.append("发布方提供转录稿")
    raw_guests = item.get("guests") or []
    guests = [raw_guests] if isinstance(raw_guests, str) else raw_guests
    haystack = _plain_text(
        " ".join(
            [
                str(item.get("title") or ""),
                str(item.get("show_notes") or ""),
                " ".join(str(value) for value in guests),
            ]
        )
    ).lower()
    preferred = list(settings.get("preferred_keywords") or []) + list(
        subscription.get("keywords") or []
    )
    matched = [str(value) for value in preferred if str(value).lower() in haystack]
    if matched:
        score += min(30, len(matched) * 8)
        reasons.append("命中关键词：" + "、".join(matched))
    excluded = [
        str(value)
        for value in settings.get("excluded_keywords") or []
        if str(value).lower() in haystack
    ]
    if excluded:
        score -= 50
        reasons.append("命中排除词：" + "、".join(excluded))
    return score, reasons


def _render_brief(scan_date: str, candidates: list[dict[str, Any]], failures: list[dict[str, str]]) -> str:
    lines = [
        f"# 播客订阅 Brief｜{scan_date}",
        "",
        "> 本文件只做低成本发现和筛选，没有自动下载音频或启动转录。",
        "",
    ]
    if not candidates:
        lines.extend(["今天没有新的候选单集。", ""])
    for number, candidate in enumerate(candidates, start=1):
        transcript_hint = "可直接使用发布方转录稿" if candidate["has_transcript"] else "如需全文，需另行转录"
        lines.extend(
            [
                f"## {number}. {candidate['show']}｜{candidate['title']}",
                "",
                f"- 评分：{candidate['score']}",
                f"- 发布日期：{candidate.get('publication_date') or '未获取'}",
                f"- 转录可用性：{transcript_hint}",
                f"- 推荐理由：{'；'.join(candidate['reasons'])}",
                f"- 原始链接：{candidate.get('url') or candidate.get('feed_url')}",
                "",
            ]
        )
    if failures:
        lines.extend(["## 扫描失败", ""])
        for failure in failures:
            lines.append(f"- {failure['name']}：{failure['error']}")
        lines.append("")
    return "\n".join(lines)


def scan_subscriptions(
    output_dir: Path,
    fetch_entries: Callable[[str], list[dict[str, Any]]],
    *,
    scan_date: str | None = None,
) -> dict[str, Any]:
    paths = subscription_paths(output_dir)
    if not paths["config"].is_file():
        raise FileNotFoundError(
            f"subscription config not found: {paths['config']}; run --init-subscriptions first"
        )
    config = read_json_object(paths["config"])
    if config.get("schema_version") != SUBSCRIPTION_SCHEMA_VERSION:
        raise ValueError("unsupported subscription schema")
    settings = config.get("settings") if isinstance(config.get("settings"), dict) else {}
    subscriptions = (
        config.get("subscriptions") if isinstance(config.get("subscriptions"), list) else []
    )
    try:
        state = read_json_object(paths["state"])
    except (OSError, json.JSONDecodeError, ValueError):
        state = {"schema_version": 1, "seen": {}, "last_scan_at": None}
    seen = state.get("seen") if isinstance(state.get("seen"), dict) else {}
    candidates: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    discovered = 0
    max_items = max(1, int(settings.get("max_items_per_feed") or 20))
    day = scan_date or date.today().isoformat()

    for subscription in subscriptions:
        if not isinstance(subscription, dict) or not subscription.get("enabled", True):
            continue
        feed_url = str(subscription.get("feed_url") or "").strip()
        name = str(subscription.get("name") or feed_url or "未命名订阅")
        if not feed_url:
            failures.append({"name": name, "error": "缺少 feed_url"})
            continue
        try:
            entries = fetch_entries(feed_url)
        except Exception as exc:
            failures.append({"name": name, "error": f"{type(exc).__name__}: {exc}"})
            continue
        for item in entries[:max_items]:
            key = _episode_key(feed_url, item)
            if key in seen:
                continue
            discovered += 1
            score, reasons = _score_item(item, subscription, settings)
            seen_entry: dict[str, Any] = {
                "feed_url": feed_url,
                "title": item.get("title"),
                "first_seen_at": datetime.now(timezone.utc).isoformat(),
            }
            if score < int(settings.get("minimum_score") or 0):
                seen[key] = seen_entry
                continue
            transcripts = item.get("transcripts") or []
            candidate = {
                "id": item.get("id") or item.get("guid") or key,
                "show": item.get("show_title") or name,
                "title": item.get("title") or "未知单集",
                "url": item.get("url"),
                "feed_url": feed_url,
                "publication_date": item.get("pub_date"),
                "has_transcript": bool(transcripts),
                "transcripts": transcripts if isinstance(transcripts, list) else [],
                "score": score,
                "reasons": reasons,
            }
            seen_entry.update({"brief_date": day, "candidate": candidate})
            seen[key] = seen_entry

    candidates = [
        entry["candidate"]
        for entry in seen.values()
        if isinstance(entry, dict)
        and entry.get("brief_date") == day
        and isinstance(entry.get("candidate"), dict)
    ]

    candidates.sort(
        key=lambda item: (item["score"], str(item.get("publication_date") or "")),
        reverse=True,
    )
    candidates = candidates[: max(1, int(settings.get("brief_limit") or 20))]
    now = datetime.now(timezone.utc).isoformat()
    state.update({"schema_version": 1, "seen": seen, "last_scan_at": now})
    brief_path = paths["brief_dir"] / f"{day}.md"
    atomic_write_text(brief_path, _render_brief(day, candidates, failures) + "\n")
    atomic_write_text(paths["state"], json.dumps(state, ensure_ascii=False, indent=2) + "\n")
    return {
        "brief_path": str(brief_path),
        "candidate_count": len(candidates),
        "new_episode_count": discovered,
        "failure_count": len(failures),
        "candidates": candidates,
        "failures": failures,
        "state_path": str(paths["state"]),
    }
