#!/usr/bin/env python3
"""Normalize and render durable human-facing Show Notes link archives."""

from __future__ import annotations

import html
import os
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse


LINK_ARCHIVE_START = "<!-- shownotes-links:start -->"
LINK_ARCHIVE_END = "<!-- shownotes-links:end -->"
PLAIN_URL_PATTERN = re.compile(r"https?://[^\s<>'\"\]\)]+")
TRAILING_PUNCTUATION = "，。；：！？、）】》」』〉”’"


def clean_plain_url_candidate(value: str) -> str:
    cleaned = html.unescape(value or "").strip()
    boundaries = [
        cleaned.find(character)
        for character in TRAILING_PUNCTUATION
        if character in cleaned
    ]
    if boundaries:
        cleaned = cleaned[: min(boundaries)]
    while cleaned and cleaned[-1] in TRAILING_PUNCTUATION:
        cleaned = cleaned[:-1]
    return cleaned


def normalize_http_url(value: Any, base_url: str = "") -> str:
    cleaned = clean_plain_url_candidate(str(value or ""))
    if not cleaned:
        return ""
    resolved = urljoin(base_url, cleaned)
    parsed = urlparse(resolved)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return resolved


def extract_plain_urls(value: str) -> list[str]:
    urls: list[str] = []
    for match in PLAIN_URL_PATTERN.findall(value or ""):
        cleaned = normalize_http_url(match)
        if cleaned and cleaned not in urls:
            urls.append(cleaned)
    return urls


class _ShowNotesLinkExtractor(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.links: list[dict[str, str]] = []
        self.anchor_stack: list[dict[str, Any]] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript"}:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag == "a":
            attrs_map = {name.lower(): value for name, value in attrs if name}
            self.anchor_stack.append(
                {
                    "url": normalize_http_url(attrs_map.get("href"), self.base_url),
                    "text": [],
                }
            )

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript"} and self.skip_depth:
            self.skip_depth -= 1
            return
        if self.skip_depth:
            return
        if tag == "a" and self.anchor_stack:
            anchor = self.anchor_stack.pop()
            if anchor["url"]:
                text = re.sub(r"\s+", " ", "".join(anchor["text"])).strip()
                self.links.append({"text": text or anchor["url"], "url": anchor["url"]})

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        if self.anchor_stack:
            self.anchor_stack[-1]["text"].append(data)
            return
        for url in extract_plain_urls(data):
            self.links.append({"text": url, "url": url})


def extract_shownotes_links(raw_html: str, base_url: str = "") -> list[dict[str, str]]:
    parser = _ShowNotesLinkExtractor(base_url)
    parser.feed(raw_html or "")
    parser.close()
    return parser.links


def _display_text(value: Any, url: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    parsed = urlparse(url)
    url_like_with_suffix = text.startswith(parsed.netloc) and any(
        character in text for character in TRAILING_PUNCTUATION
    )
    if (
        not text
        or text.startswith("http://")
        or text.startswith("https://")
        or url_like_with_suffix
    ):
        return (parsed.netloc + parsed.path).rstrip("/") or url
    return text


def canonicalize_links(
    links: list[dict[str, Any]],
    *,
    base_url: str = "",
    saved_at: str | None = None,
) -> list[dict[str, Any]]:
    canonical: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in links:
        if not isinstance(item, dict):
            continue
        url = normalize_http_url(
            item.get("url") or item.get("normalized_url") or item.get("source_url"),
            base_url,
        )
        if not url or url in seen:
            continue
        seen.add(url)
        normalized = dict(item)
        normalized.update(
            {
                "text": _display_text(item.get("text"), url),
                "url": url,
                "source_url": url,
                "normalized_url": url,
            }
        )
        snapshot = normalized.get("snapshot")
        normalized["preservation"] = (
            "local_snapshot"
            if isinstance(snapshot, dict) and snapshot.get("status") == "complete"
            else "online_url"
        )
        if saved_at and not normalized.get("saved_at"):
            normalized["saved_at"] = saved_at
        canonical.append(normalized)
    return canonical


def _markdown_label(value: str) -> str:
    return value.replace("[", "\\[").replace("]", "\\]")


def _markdown_url(value: str) -> str:
    return value.replace("(", "%28").replace(")", "%29").replace(">", "%3E")


def render_link_archive(links: list[dict[str, Any]], shownotes_dir: Path) -> str:
    if not links:
        return ""
    lines = [
        LINK_ARCHIVE_START,
        "## 链接归档",
        "",
        "> 以下链接的原始在线地址已保存在本文件和 media-manifest.json；网页内容仅在明确启用链接快照时保存到本地。",
        "",
    ]
    for item in links:
        url = str(item.get("url") or "")
        if not url:
            continue
        label = _markdown_label(str(item.get("text") or url))
        parts = [f"[{label}](<{_markdown_url(url)}>)", "保存：在线 URL"]
        snapshot = item.get("snapshot") or {}
        if isinstance(snapshot, dict) and snapshot.get("status") == "complete" and snapshot.get("path"):
            snapshot_path = Path(str(snapshot["path"])).expanduser()
            try:
                relative = os.path.relpath(snapshot_path, shownotes_dir).replace(os.sep, "/")
            except ValueError:
                relative = str(snapshot_path)
            parts.append(f"[本地快照](<{_markdown_url(relative)}>)")
        elif isinstance(snapshot, dict) and snapshot.get("status") == "failed":
            parts.append("本地快照失败")
        lines.append("- " + " · ".join(parts))
    lines.extend(["", LINK_ARCHIVE_END])
    return "\n".join(lines)


def update_link_archive(markdown: str, archive: str) -> str:
    pattern = re.compile(
        re.escape(LINK_ARCHIVE_START) + r".*?" + re.escape(LINK_ARCHIVE_END),
        re.DOTALL,
    )
    without_existing = pattern.sub("", markdown or "").rstrip()
    if not archive:
        return without_existing + ("\n" if without_existing else "")
    return without_existing + "\n\n" + archive.strip() + "\n"
