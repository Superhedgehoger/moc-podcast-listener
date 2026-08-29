#!/usr/bin/env python3
"""
播客代听助手 - 转录阶段实现

职责边界：
1. 解析播客链接或名称，提取音频、Show Notes、嘉宾信息
2. 下载音频并预处理为单声道 WAV
3. 使用 Whisper 转录，注入 initial_prompt 提升专有名词准确率
4. 保存带来源信息和时间戳的独立转录稿、元数据和 Agent 后续总结指令

深度总结由 Agent 按 SKILL.md 的证据提取流程直接完成，本脚本不调用外部 LLM。
总结稿只链接转录稿，不重复嵌入完整转录正文。
"""

from __future__ import annotations

import argparse
import html
from html.parser import HTMLParser
import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
import xml.etree.ElementTree as ET

from knowledge_base import (
    PERSONAL_NOTES_FILENAME,
    ensure_episode_knowledge_files,
    rebuild_knowledge_index,
    validate_knowledge,
)
from shownotes_links import (
    LINK_ARCHIVE_START,
    canonicalize_links,
    extract_plain_urls,
    render_link_archive,
    update_link_archive,
)
from version import __version__


DEFAULT_OUTPUT_DIR = Path.home() / "Documents" / "播客总结"
HUMAN_INDEX_FILENAME = "播客索引.md"
DEFAULT_MODEL = "large-v3"
FALLBACK_MODELS = ["large-v3", "small", "base"]
ITUNES_LOOKUP_URL = "https://itunes.apple.com/lookup"
ITUNES_SEARCH_URL = "https://itunes.apple.com/search"
SHOWNOTES_ASSET_MODES = {"off", "online", "local", "hybrid"}
SHOWNOTES_LINK_SNAPSHOT_MODES = {"none", "singlefile", "archivebox"}
SHOWNOTES_DEFAULT_MAX_IMAGES = 40
SHOWNOTES_DEFAULT_MAX_IMAGE_BYTES = 15 * 1024 * 1024
SHOWNOTES_DEFAULT_MAX_LINK_SNAPSHOTS = 10
IMAGE_CONTENT_TYPE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/avif": ".avif",
}
HTTP_REDIRECT_CODES = {301, 302, 303, 307, 308}
MAX_HTTP_REDIRECTS = 8
MAX_TEXT_RESPONSE_BYTES = 20 * 1024 * 1024
PROXY_FAKE_IP_NETWORKS = (
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("fdfe:dcba:9876::/48"),
)
LOCAL_AUDIO_EXTENSIONS = {".aac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav"}
LOCAL_VIDEO_EXTENSIONS = {".avi", ".m4v", ".mkv", ".mov", ".mp4", ".ts", ".webm"}
LOCAL_MEDIA_EXTENSIONS = LOCAL_AUDIO_EXTENSIONS | LOCAL_VIDEO_EXTENSIONS


def log(msg: str) -> None:
    print(f"[INFO] {msg}")


def warn(msg: str) -> None:
    print(f"[WARN] {msg}")


def error(msg: str) -> None:
    print(f"[ERROR] {msg}", file=sys.stderr)


def run_command(
    args: list[str],
    timeout: int | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(cwd) if cwd else None,
    )


def is_url(value: str) -> bool:
    parsed = urlparse(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def iso_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def is_safe_remote_url(url: str) -> tuple[bool, str | None]:
    parsed = urlparse(url)
    hostname = parsed.hostname
    if parsed.scheme not in {"http", "https"} or not hostname:
        return False, "unsupported URL"
    if parsed.username or parsed.password:
        return False, "URL credentials are not allowed"
    if hostname.lower() == "localhost" or hostname.lower().endswith(".localhost"):
        return False, "local address blocked"

    try:
        literal_ip = ipaddress.ip_address(hostname)
    except ValueError:
        literal_ip = None
    if literal_ip is not None and not literal_ip.is_global:
        return False, f"non-public address blocked: {literal_ip}"

    try:
        default_port = 443 if parsed.scheme == "https" else 80
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(hostname, parsed.port or default_port)
        }
    except OSError as exc:
        return False, f"DNS lookup failed: {exc}"

    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return False, f"invalid resolved address: {address}"
        fake_ip_allowed = (
            literal_ip is None
            and any(ip in network for network in PROXY_FAKE_IP_NETWORKS)
            and os.environ.get("ALLOW_PROXY_FAKE_IP", "1") != "0"
        )
        if not ip.is_global and not fake_ip_allowed:
            return False, f"non-public address blocked: {address}"
    return True, None


class NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


def open_safe_http(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    timeout: int = 40,
):
    """Open an HTTP URL after validating every redirect target."""
    current_url = url
    opener = build_opener(NoRedirectHandler())
    request_headers = {
        "User-Agent": "Mozilla/5.0 podcast-listener",
        "Accept": "*/*",
        **(headers or {}),
    }

    for _ in range(MAX_HTTP_REDIRECTS + 1):
        safe, reason = is_safe_remote_url(current_url)
        if not safe:
            raise ValueError(reason or "unsafe URL")
        request = Request(current_url, headers=request_headers, method=method)
        try:
            response = opener.open(request, timeout=timeout)
        except HTTPError as exc:
            if exc.code not in HTTP_REDIRECT_CODES:
                raise
            location = exc.headers.get("Location")
            exc.close()
            if not location:
                raise ValueError(f"redirect without Location header: HTTP {exc.code}")
            current_url = urljoin(current_url, location)
            continue
        return response, current_url
    raise ValueError(f"too many redirects (>{MAX_HTTP_REDIRECTS})")


def fetch_text(url: str, timeout: int = 40) -> str | None:
    try:
        response, _ = open_safe_http(url, timeout=timeout)
        with response:
            payload = response.read(MAX_TEXT_RESPONSE_BYTES + 1)
            if len(payload) > MAX_TEXT_RESPONSE_BYTES:
                warn(f"获取失败: {url} response exceeds {MAX_TEXT_RESPONSE_BYTES} bytes")
                return None
            charset = response.headers.get_content_charset() or "utf-8"
        decoded = payload.decode(charset, errors="replace")
        return decoded if decoded.strip() else None
    except Exception as exc:
        warn(f"获取异常: {url} {exc}")
        return None


def download_http_resource(
    url: str,
    output_path: Path,
    *,
    max_bytes: int | None = None,
    timeout: int = 90,
    referer: str | None = None,
    resume: bool = False,
) -> dict[str, Any]:
    """Download one HTTP resource with redirect validation and bounded streaming."""
    fetched_at = iso_timestamp()
    existing_bytes = output_path.stat().st_size if resume and output_path.exists() else 0
    headers: dict[str, str] = {}
    if referer:
        headers["Referer"] = referer
    if existing_bytes:
        headers["Range"] = f"bytes={existing_bytes}-"

    try:
        response, final_url = open_safe_http(
            url,
            headers=headers,
            timeout=timeout,
        )
        with response:
            status = int(getattr(response, "status", response.getcode()))
            append = bool(existing_bytes and status == 206)
            total_before = existing_bytes if append else 0
            raw_length = response.headers.get("Content-Length")
            if max_bytes and raw_length and total_before + int(raw_length) > max_bytes:
                raise ValueError(f"response exceeds {max_bytes} bytes")
            output_path.parent.mkdir(parents=True, exist_ok=True)
            mode = "ab" if append else "wb"
            written = total_before
            with output_path.open(mode) as handle:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if max_bytes and written > max_bytes:
                        raise ValueError(f"response exceeds {max_bytes} bytes")
                    handle.write(chunk)
            content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].strip()
        return {
            "ok": True,
            "source_url": url,
            "final_url": final_url,
            "http_status": status,
            "fetched_at": fetched_at,
            "content_type": content_type or None,
            "bytes": output_path.stat().st_size,
            "path": str(output_path),
        }
    except (HTTPError, URLError, OSError, ValueError) as exc:
        status = exc.code if isinstance(exc, HTTPError) else None
        return {
            "ok": False,
            "source_url": url,
            "final_url": getattr(exc, "url", None) or url,
            "http_status": status,
            "fetched_at": fetched_at,
            "content_type": None,
            "bytes": output_path.stat().st_size if output_path.exists() else 0,
            "error": str(exc),
            "failure_reason": str(exc),
        }


def fetch_json(url: str, timeout: int = 40) -> dict[str, Any] | None:
    text = fetch_text(url, timeout=timeout)
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        warn(f"JSON 解析失败: {url} {exc}")
        return None


def parse_xiaoyuzhou_url(url: str) -> str | None:
    """解析小宇宙 URL，提取 episode id。"""
    patterns = [
        r"/episode/([a-f0-9]{24})",
        r"id=([a-f0-9]{24})",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def parse_apple_podcasts_url(url: str) -> dict[str, str | None]:
    """解析 Apple Podcasts URL，提取 podcast id 和 episode id(i 参数)。"""
    parsed = urlparse(url)
    if "podcasts.apple.com" not in parsed.netloc:
        return {"podcast_id": None, "episode_id": None}

    podcast_id = None
    match = re.search(r"/id(\d+)", parsed.path)
    if match:
        podcast_id = match.group(1)

    query = parse_qs(parsed.query)
    episode_id = (query.get("i") or [None])[0]
    return {"podcast_id": podcast_id, "episode_id": episode_id}


def apple_slug_query(url: str) -> str:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    for index, part in enumerate(parts):
        if re.fullmatch(r"id\d+", part) and index > 0:
            slug = parts[index - 1]
            return clean_text(slug.replace("-", " "))
    return ""


def extract_next_data(html_text: str) -> dict[str, Any] | None:
    """提取 Next.js 页面中的 __NEXT_DATA__ JSON。"""
    match = re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
        html_text,
        flags=re.DOTALL,
    )
    if not match:
        return None

    raw_json = html.unescape(match.group(1))
    try:
        return json.loads(raw_json)
    except json.JSONDecodeError:
        return None


def extract_podcast_schema(html_text: str) -> dict[str, Any] | None:
    for match in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html_text,
        flags=re.DOTALL | re.IGNORECASE,
    ):
        try:
            value = json.loads(html.unescape(match.group(1)))
        except json.JSONDecodeError:
            continue
        candidates = value if isinstance(value, list) else [value]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            schema_type = candidate.get("@type")
            types = schema_type if isinstance(schema_type, list) else [schema_type]
            if "PodcastEpisode" in types:
                return candidate
    return None


def walk_json(value: Any):
    """深度遍历 JSON 值，产出所有 dict/list/scalar。"""
    yield value
    if isinstance(value, dict):
        for item in value.values():
            yield from walk_json(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk_json(item)


def first_string_by_keys(data: Any, keys: set[str]) -> str | None:
    for item in walk_json(data):
        if not isinstance(item, dict):
            continue
        for key, value in item.items():
            if key in keys and isinstance(value, str) and value.strip():
                return value.strip()
    return None


def find_audio_url(html_text: str, data: dict[str, Any] | None) -> str | None:
    if data:
        for item in walk_json(data):
            if not isinstance(item, dict):
                continue
            for key, value in item.items():
                key_lower = key.lower()
                if "audio" in key_lower and isinstance(value, str):
                    if re.search(r"\.(m4a|mp3|aac|wav|m3u8)(\?|$)", value):
                        return value.replace("\\u002F", "/")

    patterns = [
        r'https://[^"\'\s]+?\.(?:m4a|mp3|aac|wav|m3u8)[^"\'\s<]*',
        r'"audioUrl"\s*:\s*"([^"]+)"',
        r'<audio[^>]+src=["\']([^"\']+)["\']',
        r'<source[^>]+src=["\']([^"\']+)["\']',
    ]
    for pattern in patterns:
        match = re.search(pattern, html_text)
        if match:
            url = match.group(1) if match.lastindex else match.group(0)
            return html.unescape(url).replace("\\u002F", "/")
    return None


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    value = html.unescape(value)
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    value = re.sub(r"</p\s*>", "\n", value, flags=re.IGNORECASE)
    value = re.sub(r"<[^>]+>", "", value)
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def xml_inner_html(element: ET.Element) -> str:
    """Return child text with embedded XML/HTML preserved where ElementTree keeps it."""
    pieces: list[str] = []
    if element.text:
        pieces.append(element.text)
    for child in list(element):
        pieces.append(ET.tostring(child, encoding="unicode", method="html"))
    return "".join(pieces).strip()


def first_child_raw_text(element: ET.Element, names: set[str]) -> str:
    for child in list(element):
        if xml_local_name(child.tag) in names:
            raw = xml_inner_html(child)
            if raw:
                return html.unescape(raw).strip()
    return ""


def normalize_match_text(value: str | None) -> str:
    value = clean_text(value).lower()
    value = re.sub(r"[\W_]+", "", value, flags=re.UNICODE)
    return value


def text_matches(candidate: str | None, target: str | None) -> bool:
    candidate_norm = normalize_match_text(candidate)
    target_norm = normalize_match_text(target)
    if not candidate_norm or not target_norm:
        return False
    return candidate_norm in target_norm or target_norm in candidate_norm


def query_match_score(query: str, candidate: str | None) -> float:
    candidate_norm = normalize_match_text(candidate)
    if not candidate_norm:
        return 0.0
    query_norm = normalize_match_text(query)
    if query_norm and query_norm in candidate_norm:
        return 1.0

    terms = query_terms(query)
    if not terms:
        return 0.0
    hits = sum(1 for term in terms if term in candidate_norm)
    return hits / len(terms)


def query_terms(query: str) -> list[str]:
    stopwords = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "podcast", "episode"}
    terms = []
    for term in re.split(r"\s+", query):
        normalized = normalize_match_text(term)
        if len(normalized) >= 3 and normalized not in stopwords:
            terms.append(normalized)
    return terms


def xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def first_child_text(element: ET.Element, names: set[str]) -> str:
    for child in list(element):
        if xml_local_name(child.tag) in names and child.text:
            return clean_text(child.text)
    return ""


def find_rss_feed_url(html_text: str, data: dict[str, Any] | None = None) -> str | None:
    if data:
        value = first_string_by_keys(data, {"feedUrl", "feedURL", "rssUrl", "rssURL", "xmlUrl"})
        if value and value.startswith("http"):
            return value

    patterns = [
        r'<link[^>]+type=["\']application/rss\+xml["\'][^>]+href=["\']([^"\']+)["\']',
        r'<link[^>]+href=["\']([^"\']+)["\'][^>]+type=["\']application/rss\+xml["\']',
        r'https?://[^"\'\s<>]+(?:rss|feed|xml)[^"\'\s<>]*',
    ]
    for pattern in patterns:
        match = re.search(pattern, html_text, flags=re.IGNORECASE)
        if match:
            return html.unescape(match.group(1) if match.lastindex else match.group(0))
    return None


def extract_meta_content(html_text: str, property_name: str) -> str | None:
    patterns = [
        rf'<meta[^>]+(?:property|name)=["\']{re.escape(property_name)}["\'][^>]+content=["\']([^"\']+)["\']',
        rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']{re.escape(property_name)}["\']',
    ]
    for pattern in patterns:
        match = re.search(pattern, html_text, flags=re.IGNORECASE)
        if match:
            return clean_text(match.group(1))
    return None


def extract_page_title(html_text: str, suffixes: list[str] | None = None) -> str:
    title = extract_meta_content(html_text, "og:title")
    if not title:
        title_match = re.search(r"<title>(.*?)</title>", html_text, flags=re.DOTALL | re.IGNORECASE)
        title = clean_text(title_match.group(1)) if title_match else ""
    for suffix in suffixes or []:
        title = title.replace(suffix, "").strip()
    return title or "Unknown"


def rss_item_audio_url(item: ET.Element) -> str | None:
    for child in list(item):
        name = xml_local_name(child.tag)
        if name == "enclosure":
            url = child.attrib.get("url")
            media_type = child.attrib.get("type", "")
            if url and ("audio" in media_type or re.search(r"\.(m4a|mp3|aac|wav|m3u8)(\?|$)", url)):
                return html.unescape(url)
        if name in {"content", "player"}:
            url = child.attrib.get("url")
            medium = child.attrib.get("medium", "")
            media_type = child.attrib.get("type", "")
            if url and ("audio" in medium or "audio" in media_type or re.search(r"\.(m4a|mp3|aac|wav|m3u8)(\?|$)", url)):
                return html.unescape(url)
    return None


def parse_duration_minutes(value: str | None) -> float:
    raw = clean_text(value)
    if not raw:
        return 0.0
    try:
        if ":" not in raw:
            return max(0.0, float(raw) / 60.0)
        parts = [float(part) for part in raw.split(":")]
        if len(parts) == 2:
            seconds = parts[0] * 60 + parts[1]
        elif len(parts) == 3:
            seconds = parts[0] * 3600 + parts[1] * 60 + parts[2]
        else:
            return 0.0
        return max(0.0, seconds / 60.0)
    except ValueError:
        return 0.0


def parse_iso8601_duration_minutes(value: str | None) -> float:
    raw = clean_text(value)
    match = re.fullmatch(
        r"P(?:(?P<days>\d+(?:\.\d+)?)D)?(?:T(?:(?P<hours>\d+(?:\.\d+)?)H)?(?:(?P<minutes>\d+(?:\.\d+)?)M)?(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?",
        raw,
        flags=re.IGNORECASE,
    )
    if not match:
        return 0.0
    days = float(match.group("days") or 0)
    hours = float(match.group("hours") or 0)
    minutes = float(match.group("minutes") or 0)
    seconds = float(match.group("seconds") or 0)
    return days * 1440 + hours * 60 + minutes + seconds / 60


def rss_people(element: ET.Element | None) -> list[dict[str, str]]:
    if element is None:
        return []
    people: list[dict[str, str]] = []
    for child in list(element):
        if xml_local_name(child.tag) != "person":
            continue
        name = clean_text(child.text)
        if not name:
            continue
        person = {
            "name": name,
            "role": clean_text(child.attrib.get("role")) or "host",
            "group": clean_text(child.attrib.get("group")) or "cast",
        }
        for key in ("img", "href"):
            value = child.attrib.get(key)
            if value:
                person[key] = html.unescape(value)
        people.append(person)
    return people


def rss_transcripts(element: ET.Element) -> list[dict[str, str]]:
    transcripts: list[dict[str, str]] = []
    for child in list(element):
        if xml_local_name(child.tag) != "transcript":
            continue
        url = child.attrib.get("url")
        media_type = child.attrib.get("type")
        if not url or not media_type:
            continue
        entry = {
            "url": html.unescape(url),
            "type": media_type.strip().lower(),
        }
        for key in ("language", "rel"):
            value = child.attrib.get(key)
            if value:
                entry[key] = value.strip()
        transcripts.append(entry)
    return transcripts


def rss_chapters(element: ET.Element) -> dict[str, str] | None:
    for child in list(element):
        if xml_local_name(child.tag) != "chapters":
            continue
        url = child.attrib.get("url")
        if url:
            return {
                "url": html.unescape(url),
                "type": (child.attrib.get("type") or "application/json+chapters").strip().lower(),
            }
    return None


def rss_images(element: ET.Element | None) -> list[dict[str, Any]]:
    if element is None:
        return []
    images: list[dict[str, Any]] = []
    for child in list(element):
        if xml_local_name(child.tag) != "image":
            continue
        href = child.attrib.get("href") or child.attrib.get("url")
        if not href:
            nested_url = first_child_text(child, {"url"})
            href = nested_url or None
        if not href:
            continue
        entry: dict[str, Any] = {"href": html.unescape(href)}
        for key in ("alt", "aspect-ratio", "width", "height", "type", "purpose"):
            value = child.attrib.get(key)
            if value:
                entry[key] = value
        images.append(entry)
    return images


def rss_items(feed_xml: str) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(feed_xml)
    except ET.ParseError as exc:
        warn(f"RSS 解析失败: {exc}")
        return []

    show_title = first_child_text(root, {"title"})
    channel = next((item for item in root.iter() if xml_local_name(item.tag) == "channel"), None)
    if channel is not None:
        show_title = first_child_text(channel, {"title"}) or show_title
    channel_people = rss_people(channel)
    channel_images = rss_images(channel)
    feed_language = first_child_text(channel, {"language"}) if channel is not None else ""

    items: list[dict[str, Any]] = []
    for item in root.iter():
        if xml_local_name(item.tag) != "item":
            continue
        title = first_child_text(item, {"title"})
        description = first_child_raw_text(item, {"description", "summary", "encoded"})
        guid = first_child_text(item, {"guid"})
        link = first_child_text(item, {"link"})
        author = first_child_text(item, {"author", "creator"})
        audio_url = rss_item_audio_url(item)
        item_people = rss_people(item)
        people = item_people or channel_people
        speaker_people = [
            person["name"]
            for person in people
            if person.get("group", "cast").lower() == "cast"
            or person.get("role", "host").lower() in {"host", "co-host", "guest"}
        ]
        if author and author not in speaker_people:
            speaker_people.append(author)
        item_images = rss_images(item)
        images = item_images or channel_images
        transcripts = rss_transcripts(item)
        chapters = rss_chapters(item)
        duration = parse_duration_minutes(first_child_text(item, {"duration"}))
        
        pub_date_raw = first_child_text(item, {"pubdate", "pubDate", "date"})
        pub_date = None
        if pub_date_raw:
            import email.utils
            try:
                parsed_date = email.utils.parsedate_to_datetime(pub_date_raw)
                pub_date = parsed_date.strftime("%Y%m%d")
            except Exception:
                match = re.search(r"(\d{4})[-/]?(\d{2})[-/]?(\d{2})", pub_date_raw)
                if match:
                    pub_date = "".join(match.groups())
        if not pub_date:
            pub_date = time.strftime("%Y%m%d")

        if audio_url:
            items.append(
                {
                    "id": guid or link or title,
                    "title": title or "Unknown",
                    "url": link,
                    "audio_url": audio_url,
                    "cover_url": images[0]["href"] if images else "",
                    "show_notes": description,
                    "guests": speaker_people,
                    "people": people,
                    "transcripts": transcripts,
                    "chapters": chapters,
                    "images": images,
                    "language": feed_language or None,
                    "duration_minutes": duration,
                    "show_title": show_title,
                    "pub_date": pub_date,
                    "guid": guid,
                    "source": "rss",
                }
            )
    return items


def subscription_feed_entries(feed_url: str) -> list[dict[str, Any]]:
    """Fetch RSS metadata for Brief generation without downloading episode audio."""
    feed_xml = fetch_text(feed_url, timeout=50)
    if not feed_xml:
        raise RuntimeError("RSS feed could not be fetched")
    items = rss_items(feed_xml)
    for item in items:
        if item.get("url"):
            item["url"] = urljoin(feed_url, str(item["url"]))
        for transcript in item.get("transcripts") or []:
            if isinstance(transcript, dict) and transcript.get("url"):
                transcript["url"] = urljoin(feed_url, str(transcript["url"]))
    return items


def select_rss_episode(
    feed_url: str,
    *,
    episode_title: str | None = None,
    episode_id: str | None = None,
    source_url: str | None = None,
) -> dict[str, Any] | None:
    target_title = episode_title if episode_title and episode_title.strip().lower() != "unknown" else None
    feed_xml = fetch_text(feed_url, timeout=50)
    if not feed_xml:
        return None

    items = rss_items(feed_xml)
    if not items:
        return None
    for item in items:
        for transcript in item.get("transcripts") or []:
            if transcript.get("url"):
                transcript["url"] = urljoin(feed_url, transcript["url"])
        chapters = item.get("chapters")
        if isinstance(chapters, dict) and chapters.get("url"):
            chapters["url"] = urljoin(feed_url, chapters["url"])
        for image in item.get("images") or []:
            if image.get("href"):
                image["href"] = urljoin(feed_url, image["href"])
        if item.get("images"):
            item["cover_url"] = item["images"][0]["href"]

    if episode_id:
        for item in items:
            haystack = "\n".join([item.get("id", ""), item.get("guid", ""), item.get("url", ""), item.get("audio_url", "")])
            if episode_id in haystack:
                item["url"] = source_url or item.get("url") or feed_url
                return item

    if target_title:
        for item in items:
            if text_matches(item.get("title"), target_title):
                item["url"] = source_url or item.get("url") or feed_url
                return item

    if target_title or episode_id:
        warn("RSS 中未找到匹配的单集，拒绝静默使用最新一集")
        return None

    # 只有调用方明确表示“节目/Feed本身”时，才允许取最新一集。
    first_item = items[0]
    first_item["url"] = source_url or first_item.get("url") or feed_url
    return first_item


def extract_show_notes(html_text: str, data: dict[str, Any] | None) -> str:
    candidates: list[str] = []
    if data:
        episode = find_key_path(data, ["props", "pageProps", "episode"])
        if isinstance(episode, dict):
            for key in ("shownotes", "showNotes", "description", "content", "brief"):
                value = episode.get(key)
                if isinstance(value, str) and value.strip():
                    candidates.append(value)
        for key_set in [
            {"shownotes", "showNotes"},
            {"episodeDescription", "description", "content", "brief"},
        ]:
            value = first_string_by_keys(data, key_set)
            if value and value not in candidates:
                candidates.append(value)

    schema = extract_podcast_schema(html_text)
    if schema and isinstance(schema.get("description"), str):
        candidates.append(str(schema["description"]))

    meta_match = re.search(
        r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)["\']',
        html_text,
        flags=re.IGNORECASE,
    )
    if meta_match:
        candidates.append(meta_match.group(1))

    usable = [(item.strip(), clean_text(item)) for item in candidates if clean_text(item)]
    if not usable:
        return ""

    def richness(item: tuple[str, str]) -> tuple[int, int]:
        raw, plain = item
        rich_tags = len(
            re.findall(r"<(?:img|a|picture|source)\b", raw, flags=re.IGNORECASE)
        )
        return rich_tags, len(plain)

    return max(usable, key=richness)[0]


def extract_guests(title: str, show_notes: str, data: dict[str, Any] | None) -> list[str]:
    """从结构化数据、标题和 Show Notes 中尽力抽取主播/嘉宾名。"""
    names: list[str] = []

    def add_name(value: str | None) -> None:
        value = clean_text(value)
        if not value:
            return
        value = re.sub(r"^(主播|主持人|嘉宾|对谈人|本期嘉宾|Guest|Host)[:：]\s*", "", value, flags=re.IGNORECASE)
        for piece in re.split(r"[,，、/｜|&和与]\s*", value):
            piece = piece.strip(" -—:：[]【】()（）\t\r\n")
            if 1 < len(piece) <= 24 and not re.search(r"https?://|\d{3,}", piece):
                names.append(piece)

    text = "\n".join([title, clean_text(show_notes)])
    guest_patterns = [
        r"(?:本期嘉宾|嘉宾|主播|主持人|对谈人|采访者|受访者)[:：]\s*([^\n]+)",
        r"(?:和|与)([\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z·.\s]{1,20})(?:聊|谈|对话)",
    ]
    for pattern in guest_patterns:
        for match in re.finditer(pattern, text):
            add_name(match.group(1))

    # Restrict structured candidates to explicit episode fields. A full JSON walk
    # also reaches listener comments and permission names such as SHARE/COMMENT.
    if data and not names:
        episode = find_key_path(data, ["props", "pageProps", "episode"])
        if not isinstance(episode, dict):
            episode = find_key_path(data, ["episode"])
        if isinstance(episode, dict):
            for key in ("hosts", "guests", "speakers", "people"):
                value = episode.get(key)
                values = value if isinstance(value, list) else [value]
                for candidate in values:
                    if isinstance(candidate, str):
                        add_name(candidate)
                    elif isinstance(candidate, dict):
                        add_name(
                            candidate.get("name")
                            or candidate.get("nickname")
                            or candidate.get("username")
                        )

    seen: set[str] = set()
    unique: list[str] = []
    for name in names:
        if name not in seen:
            seen.add(name)
            unique.append(name)
    return unique[:12]


def normalize_shownotes_url(raw_url: str | None, base_url: str | None = None) -> str:
    if not raw_url:
        return ""
    value = html.unescape(raw_url).strip()
    if not value or value.startswith(("data:", "javascript:", "mailto:", "tel:")):
        return ""
    resolved = urljoin(base_url or "", value)
    parsed = urlparse(resolved)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return resolved


def markdown_escape_text(value: str) -> str:
    return value.replace("[", "\\[").replace("]", "\\]").strip()


def markdown_escape_url(value: str) -> str:
    return value.replace(")", "%29").replace("(", "%28").strip()


class ShowNotesMarkdownParser(HTMLParser):
    BLOCK_TAGS = {
        "address", "article", "aside", "blockquote", "div", "dl", "fieldset", "figcaption",
        "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr",
        "li", "main", "nav", "ol", "p", "pre", "section", "table", "tr", "ul",
    }

    def __init__(self, base_url: str, image_resolver):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.image_resolver = image_resolver
        self.parts: list[str] = []
        self.links: list[dict[str, str]] = []
        self.anchor_stack: list[dict[str, Any]] = []
        self.picture_sources: list[list[str]] = []
        self.skip_depth = 0

    def best_srcset_url(self, value: str | None) -> str:
        candidates: list[tuple[float, str]] = []
        for index, item in enumerate((value or "").split(",")):
            parts = item.strip().split()
            if not parts:
                continue
            url = normalize_shownotes_url(parts[0], self.base_url)
            if not url:
                continue
            score = float(index)
            if len(parts) > 1:
                descriptor = parts[1].lower()
                try:
                    score = float(descriptor[:-1]) if descriptor.endswith(("w", "x")) else score
                except ValueError:
                    pass
            candidates.append((score, url))
        return max(candidates, default=(0.0, ""), key=lambda item: item[0])[1]

    def append_newline(self) -> None:
        if not self.parts:
            return
        if not self.parts[-1].endswith("\n"):
            self.parts.append("\n")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attrs_map = {name.lower(): value for name, value in attrs if name}
        if tag in {"script", "style", "noscript"}:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag in self.BLOCK_TAGS:
            self.append_newline()
        if tag == "br":
            self.append_newline()
        elif tag == "li":
            self.parts.append("- ")
        elif tag == "a":
            href = normalize_shownotes_url(attrs_map.get("href"), self.base_url)
            self.anchor_stack.append({"href": href, "start": len(self.parts), "contains_image": False})
        elif tag == "picture":
            self.picture_sources.append([])
        elif tag == "source" and self.picture_sources:
            src = self.best_srcset_url(attrs_map.get("srcset"))
            if src:
                self.picture_sources[-1].append(src)
        elif tag == "img":
            src = self.best_srcset_url(attrs_map.get("srcset"))
            if not src and self.picture_sources and self.picture_sources[-1]:
                src = self.picture_sources[-1][-1]
            if not src:
                for attr_name in ("src", "data-src", "data-original", "data-lazy-src"):
                    src = normalize_shownotes_url(attrs_map.get(attr_name), self.base_url)
                    if src:
                        break
            if src:
                alt = clean_text(attrs_map.get("alt") or attrs_map.get("title") or "shownotes image")
                markdown_url = self.image_resolver(src, alt)
                if markdown_url:
                    if self.anchor_stack:
                        self.anchor_stack[-1]["contains_image"] = True
                    self.append_newline()
                    self.parts.append(f"![{markdown_escape_text(alt)}]({markdown_escape_url(markdown_url)})")
                    self.append_newline()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript"} and self.skip_depth:
            self.skip_depth -= 1
            return
        if self.skip_depth:
            return
        if tag == "a" and self.anchor_stack:
            anchor = self.anchor_stack.pop()
            href = anchor.get("href") or ""
            start = int(anchor.get("start", len(self.parts)))
            content = "".join(self.parts[start:]).strip()
            text = clean_text(content) or href
            if href:
                del self.parts[start:]
                if anchor.get("contains_image") and content.startswith("!["):
                    self.parts.append(f"[{content}]({markdown_escape_url(href)})")
                else:
                    self.parts.append(f"[{markdown_escape_text(text)}]({markdown_escape_url(href)})")
                self.links.append({"text": text, "url": href})
        if tag == "picture" and self.picture_sources:
            self.picture_sources.pop()
        if tag in self.BLOCK_TAGS:
            self.append_newline()

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        if data:
            self.parts.append(data)

    def markdown(self) -> str:
        text = "".join(self.parts)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def detect_image_extension(path: Path, content_type: str, url: str) -> str | None:
    normalized_type = content_type.split(";", 1)[0].strip().lower()
    if normalized_type in IMAGE_CONTENT_TYPE_EXTENSIONS:
        return IMAGE_CONTENT_TYPE_EXTENSIONS[normalized_type]

    signature = path.read_bytes()[:16]
    if signature.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if signature.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if signature.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if len(signature) >= 12 and signature[8:12] == b"WEBP":
        return ".webp"
    if len(signature) >= 12 and signature[4:12] in {b"ftypavif", b"ftypavis"}:
        return ".avif"

    suffix = Path(urlparse(url).path).suffix.lower()
    if normalized_type.startswith("image/") and suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif"}:
        return ".jpg" if suffix == ".jpeg" else suffix
    return None


def download_shownotes_image(
    url: str,
    assets_dir: Path,
    index: int,
    *,
    max_bytes: int,
    referer: str | None = None,
) -> dict[str, Any]:
    url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()[:10]
    temp_path = assets_dir / f".image-{index:02d}-{url_hash}.download"
    try:
        downloaded = download_http_resource(
            url,
            temp_path,
            max_bytes=max_bytes,
            timeout=90,
            referer=referer,
        )
        if (
            not downloaded.get("ok")
            or not temp_path.exists()
            or temp_path.stat().st_size == 0
            or temp_path.stat().st_size > max_bytes
        ):
            if temp_path.exists():
                temp_path.unlink()
            return downloaded

        content_type = str(downloaded.get("content_type") or "")
        ext = detect_image_extension(
            temp_path,
            content_type,
            str(downloaded.get("final_url") or url),
        )
        if not ext:
            temp_path.unlink()
            downloaded.update(
                {
                    "ok": False,
                    "error": f"response is not a supported image: {content_type or 'unknown type'}",
                    "failure_reason": f"response is not a supported image: {content_type or 'unknown type'}",
                }
            )
            return downloaded

        file_hash = hashlib.sha256(temp_path.read_bytes()).hexdigest()
        final_path = assets_dir / f"image-{index:02d}-{file_hash[:10]}{ext}"
        if not final_path.exists():
            temp_path.replace(final_path)
        else:
            temp_path.unlink()
        downloaded.update(
            {
                "ok": True,
                "path": str(final_path),
                "sha256": file_hash,
                "bytes": final_path.stat().st_size,
                "content_type": content_type or None,
            }
        )
        return downloaded
    except Exception as exc:
        try:
            if temp_path.exists():
                temp_path.unlink()
        except OSError:
            pass
        return {
            "ok": False,
            "source_url": url,
            "final_url": url,
            "http_status": None,
            "fetched_at": iso_timestamp(),
            "content_type": None,
            "bytes": 0,
            "error": str(exc),
            "failure_reason": str(exc),
        }


def snapshot_shownotes_links(
    links: list[dict[str, Any]],
    archive_dir: Path,
    mode: str,
) -> dict[str, Any]:
    """Optionally archive a bounded number of first-level Show Notes links."""
    if mode not in SHOWNOTES_LINK_SNAPSHOT_MODES:
        warn(f"SHOWNOTES_LINK_SNAPSHOT 值 '{mode}' 无效，将使用 none")
        mode = "none"
    summary: dict[str, Any] = {
        "mode": mode,
        "requested": 0,
        "completed": 0,
        "failed": 0,
        "directory": None,
    }
    if mode == "none" or not links:
        return summary

    try:
        max_links = max(
            0,
            int(
                os.environ.get(
                    "SHOWNOTES_MAX_LINK_SNAPSHOTS",
                    str(SHOWNOTES_DEFAULT_MAX_LINK_SNAPSHOTS),
                )
            ),
        )
    except ValueError:
        max_links = SHOWNOTES_DEFAULT_MAX_LINK_SNAPSHOTS
        warn("SHOWNOTES_MAX_LINK_SNAPSHOTS 无效，将使用默认值 10")

    snapshots_dir = archive_dir / "链接快照"
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    summary["directory"] = str(snapshots_dir)
    binary_name = (
        os.environ.get("SINGLEFILE_BIN", "single-file")
        if mode == "singlefile"
        else os.environ.get("ARCHIVEBOX_BIN", "archivebox")
    )
    binary = shutil.which(binary_name)
    if not binary:
        reason = f"command not found: {binary_name}"
        for link in links[:max_links]:
            link["snapshot"] = {"status": "failed", "reason": reason}
        summary["requested"] = min(len(links), max_links)
        summary["failed"] = summary["requested"]
        summary["reason"] = reason
        warn(f"Show Notes 链接快照未执行: {reason}")
        return summary

    archivebox_ready = False
    archivebox_dir = snapshots_dir / "archivebox"
    for index, link in enumerate(links[:max_links], start=1):
        url = str(link.get("url") or "")
        summary["requested"] += 1
        safe, reason = is_safe_remote_url(url)
        if not safe:
            link["snapshot"] = {
                "status": "failed",
                "reason": reason or "unsafe URL",
            }
            summary["failed"] += 1
            continue
        try:
            if mode == "singlefile":
                url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()[:10]
                target = snapshots_dir / f"link-{index:02d}-{url_hash}.html"
                completed = run_command([binary, url, str(target)], timeout=300)
                if completed.returncode != 0 or not target.is_file():
                    raise RuntimeError(
                        completed.stderr.strip()
                        or completed.stdout.strip()
                        or f"SingleFile exited with {completed.returncode}"
                    )
                link["snapshot"] = {
                    "status": "complete",
                    "mode": mode,
                    "path": str(target),
                    "bytes": target.stat().st_size,
                    "captured_at": iso_timestamp(),
                }
            else:
                archivebox_dir.mkdir(parents=True, exist_ok=True)
                if not archivebox_ready:
                    if not (archivebox_dir / "index.sqlite3").exists():
                        initialized = run_command([binary, "init"], timeout=300, cwd=archivebox_dir)
                        if initialized.returncode != 0:
                            raise RuntimeError(
                                initialized.stderr.strip()
                                or initialized.stdout.strip()
                                or f"ArchiveBox init exited with {initialized.returncode}"
                            )
                    archivebox_ready = True
                completed = run_command(
                    [binary, "add", url],
                    timeout=600,
                    cwd=archivebox_dir,
                )
                if completed.returncode != 0:
                    raise RuntimeError(
                        completed.stderr.strip()
                        or completed.stdout.strip()
                        or f"ArchiveBox exited with {completed.returncode}"
                    )
                link["snapshot"] = {
                    "status": "complete",
                    "mode": mode,
                    "depth": 0,
                    "path": str(archivebox_dir),
                    "captured_at": iso_timestamp(),
                }
            summary["completed"] += 1
        except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
            link["snapshot"] = {
                "status": "failed",
                "mode": mode,
                "reason": str(exc),
            }
            summary["failed"] += 1
            warn(f"Show Notes 链接快照失败，仍保留在线链接: {url}: {exc}")
    return summary


def archive_show_notes(info: dict[str, Any], output_dir: Path, combined_name: str) -> dict[str, Any] | None:
    raw_show_notes = info.get("show_notes") or ""
    cover_url = normalize_shownotes_url(info.get("cover_url"), info.get("url") or "")
    if not raw_show_notes.strip() and not cover_url:
        return None

    mode = os.environ.get("SHOWNOTES_ASSETS", "hybrid").lower().strip()
    if mode not in SHOWNOTES_ASSET_MODES:
        warn(f"SHOWNOTES_ASSETS 值 '{mode}' 无效，将使用 hybrid")
        mode = "hybrid"
    if mode == "off":
        return None

    episode_dir = output_dir / "资料" / combined_name
    shownotes_dir = episode_dir / "Show Notes"
    assets_dir = shownotes_dir / "图片"
    shownotes_dir.mkdir(parents=True, exist_ok=True)
    if mode in {"local", "hybrid"}:
        assets_dir.mkdir(parents=True, exist_ok=True)

    html_path = shownotes_dir / "source.raw.html"
    markdown_path = shownotes_dir / "shownotes.md"
    manifest_path = shownotes_dir / "media-manifest.json"
    legacy_manifest_path = output_dir / "Show Notes" / f"{combined_name}_media-manifest.json"

    images: list[dict[str, Any]] = []
    seen_images: dict[str, str] = {}
    seen_hashes: dict[str, str] = {}
    cached_images: dict[str, dict[str, Any]] = {}
    cache_manifest_path = manifest_path if manifest_path.exists() else legacy_manifest_path
    if cache_manifest_path.exists():
        try:
            previous_manifest = json.loads(cache_manifest_path.read_text(encoding="utf-8"))
            for entry in previous_manifest.get("images") or []:
                source_url = entry.get("source_url")
                previous_path = entry.get("path")
                if not source_url or not previous_path or not entry.get("ok"):
                    continue
                candidate = assets_dir / Path(previous_path).name
                previous_file = Path(previous_path).expanduser()
                if not candidate.exists() and previous_file.is_file():
                    candidate.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(previous_file, candidate)
                if candidate.exists() and candidate.is_file():
                    cached_entry = dict(entry)
                    cached_entry["path"] = str(candidate)
                    cached_images[source_url] = cached_entry
        except (OSError, json.JSONDecodeError, AttributeError):
            warn(f"已有 Show Notes manifest 不可读，将重新归档: {cache_manifest_path}")
    max_images = max(0, int(os.environ.get("SHOWNOTES_MAX_IMAGES", str(SHOWNOTES_DEFAULT_MAX_IMAGES))))
    max_image_bytes = max(
        1,
        int(os.environ.get("SHOWNOTES_MAX_IMAGE_BYTES", str(SHOWNOTES_DEFAULT_MAX_IMAGE_BYTES))),
    )

    def resolve_image(src: str, alt: str, role: str = "shownotes_image") -> str:
        image_entry: dict[str, Any] = {"source_url": src, "alt": alt, "role": role}
        if src in seen_images:
            image_entry.update(
                {
                    "ok": None,
                    "final_url": src,
                    "http_status": None,
                    "fetched_at": None,
                    "content_type": None,
                    "bytes": None,
                    "status": "duplicate",
                    "markdown_url": seen_images[src],
                    "duplicate": True,
                }
            )
            images.append(image_entry)
            return seen_images[src]

        markdown_url = src
        if mode in {"local", "hybrid"}:
            cached_entry = cached_images.get(src)
            if cached_entry:
                image_entry.update(cached_entry)
                image_entry["reused"] = True
                path = Path(cached_entry["path"])
                file_hash = str(cached_entry.get("sha256") or "")
                if file_hash:
                    seen_hashes[file_hash] = str(path)
                markdown_url = os.path.relpath(path, shownotes_dir)
            elif len(images) >= max_images:
                downloaded = {"ok": False, "error": f"image limit reached ({max_images})"}
            else:
                downloaded = download_shownotes_image(
                    src,
                    assets_dir,
                    len(images) + 1,
                    max_bytes=max_image_bytes,
                    referer=info.get("url"),
                )
            if not cached_entry:
                image_entry.update(downloaded)
                if downloaded.get("ok") and downloaded.get("path"):
                    path = Path(downloaded["path"])
                    file_hash = str(downloaded.get("sha256") or "")
                    if file_hash in seen_hashes:
                        path.unlink(missing_ok=True)
                        path = Path(seen_hashes[file_hash])
                        image_entry["path"] = str(path)
                        image_entry["duplicate_content"] = True
                    elif file_hash:
                        seen_hashes[file_hash] = str(path)
                    markdown_url = os.path.relpath(path, shownotes_dir)
                else:
                    image_entry.setdefault("failure_reason", downloaded.get("error"))
                    image_entry["status"] = "failed"
                    warn(f"Show Notes 图片下载失败，保留在线链接: {src}")
        else:
            image_entry.update(
                {
                    "ok": None,
                    "status": "online_only",
                    "final_url": src,
                    "http_status": None,
                    "fetched_at": None,
                    "content_type": None,
                    "bytes": None,
                }
            )
        if image_entry.get("ok"):
            image_entry["status"] = "archived"
        image_entry.setdefault("ok", None)
        image_entry.setdefault("final_url", src)
        image_entry.setdefault("http_status", None)
        image_entry.setdefault("fetched_at", None)
        image_entry.setdefault("content_type", None)
        image_entry.setdefault("bytes", None)
        if image_entry.get("error"):
            image_entry.setdefault("failure_reason", image_entry["error"])
        image_entry["markdown_url"] = markdown_url
        seen_images[src] = markdown_url
        images.append(image_entry)
        return markdown_url

    cover_markdown_url = None
    if cover_url:
        cover_alt = clean_text(f"{info.get('show_title') or info.get('title') or '播客'} 封面")
        cover_markdown_url = resolve_image(cover_url, cover_alt, "cover")

    parser = ShowNotesMarkdownParser(info.get("url") or "", resolve_image)
    parser.feed(raw_show_notes)
    parser.close()

    markdown_parts = []
    if cover_markdown_url:
        markdown_parts.append(f"![{cover_alt}]({markdown_escape_url(cover_markdown_url)})")
    body_markdown = parser.markdown() or clean_text(raw_show_notes)
    if body_markdown:
        markdown_parts.append(body_markdown)
    else:
        markdown_parts.append("> 本集未提供 Show Notes 正文。")
    markdown = "\n\n".join(markdown_parts)
    source_url = info.get("url") or ""
    if source_url:
        markdown = f"[原始单集链接]({markdown_escape_url(source_url)})\n\n{markdown}".strip()

    atomic_write_text(html_path, raw_show_notes)

    image_urls = {item["source_url"] for item in images if item.get("source_url")}
    links = list(parser.links)
    seen_links = {item["url"] for item in links}
    for candidate in extract_plain_urls(raw_show_notes):
        normalized = normalize_shownotes_url(candidate, info.get("url") or "")
        if normalized and normalized not in image_urls and normalized not in seen_links:
            links.append({"text": normalized, "url": normalized})
            seen_links.add(normalized)

    archive_created_at = iso_timestamp()
    links = canonicalize_links(
        links,
        base_url=info.get("url") or "",
        saved_at=archive_created_at,
    )
    snapshot_mode = os.environ.get("SHOWNOTES_LINK_SNAPSHOT", "none").lower().strip()
    snapshot_summary = snapshot_shownotes_links(
        links,
        shownotes_dir,
        snapshot_mode,
    )
    links = canonicalize_links(
        links,
        base_url=info.get("url") or "",
        saved_at=archive_created_at,
    )
    markdown = update_link_archive(
        markdown,
        render_link_archive(links, shownotes_dir),
    )
    atomic_write_text(markdown_path, markdown)

    manifest = {
        "schema_version": 2,
        "layout": "episode_directory",
        "mode": mode,
        "episode_url": info.get("url"),
        "title": info.get("title"),
        "show_title": info.get("show_title"),
        "raw_html_path": str(html_path),
        "markdown_path": str(markdown_path),
        "assets_dir": str(assets_dir) if mode in {"local", "hybrid"} else None,
        "cover_url": cover_url or None,
        "images": images,
        "links": links,
        "link_snapshots": snapshot_summary,
        "created_at": archive_created_at,
    }
    atomic_write_text(
        manifest_path,
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    )
    return {
        "archive_dir": str(shownotes_dir),
        "raw_html_path": str(html_path),
        "markdown_path": str(markdown_path),
        "manifest_path": str(manifest_path),
        "assets_dir": str(assets_dir) if mode in {"local", "hybrid"} else None,
        "image_count": len(images),
        "link_count": len(links),
        "link_snapshots": snapshot_summary,
        "mode": mode,
    }


def find_key_path(obj: Any, path_segments: list[str]) -> Any:
    if not path_segments:
        return obj
    if isinstance(obj, dict):
        key = path_segments[0]
        if key in obj:
            return find_key_path(obj[key], path_segments[1:])
        for k, v in obj.items():
            if k.lower() == key.lower():
                return find_key_path(v, path_segments[1:])
    elif isinstance(obj, list):
        for item in obj:
            res = find_key_path(item, path_segments)
            if res is not None:
                return res
    return None


def get_episode_info(episode_id: str) -> dict[str, Any] | None:
    url = f"https://www.xiaoyuzhoufm.com/episode/{episode_id}"
    try:
        page_html = fetch_text(url)
        if not page_html:
            error("获取页面失败")
            return None

        data = extract_next_data(page_html)
        schema = extract_podcast_schema(page_html) or {}
        episode_data = (
            find_key_path(data, ["props", "pageProps", "episode"])
            if data
            else None
        )
        if not isinstance(episode_data, dict):
            episode_data = {}

        title = episode_data.get("title") or schema.get("name")
        if not title:
            title = extract_meta_content(page_html, "og:title")
        if not title:
            title_match = re.search(r"<title>(.*?)</title>", page_html, flags=re.DOTALL)
            title = title_match.group(1) if title_match else "Unknown"
        title = re.sub(r"\s*[-|]\s*小宇宙.*$", "", clean_text(str(title))).strip() or "Unknown"

        show_notes = extract_show_notes(page_html, data)
        enclosure = episode_data.get("enclosure")
        audio_url = enclosure.get("url") if isinstance(enclosure, dict) else None
        associated_media = schema.get("associatedMedia")
        if not audio_url and isinstance(associated_media, dict):
            audio_url = associated_media.get("contentUrl")
        audio_url = audio_url or extract_meta_content(page_html, "og:audio") or find_audio_url(page_html, data)
        guests = extract_guests(title, show_notes, data)

        raw_image = episode_data.get("image")
        cover_url = raw_image.get("picUrl") if isinstance(raw_image, dict) else None
        cover_url = cover_url or extract_meta_content(page_html, "og:image")

        show_title = None
        pub_date = None
        podcast_data = episode_data.get("podcast")
        if isinstance(podcast_data, dict):
            show_title = podcast_data.get("title")
        series = schema.get("partOfSeries")
        if not show_title and isinstance(series, dict):
            show_title = series.get("name")
        raw_date = (
            episode_data.get("pubDate")
            or episode_data.get("datePublished")
            or schema.get("datePublished")
        )
        if isinstance(raw_date, str):
            match = re.match(r"(\d{4})[-/]?(\d{2})[-/]?(\d{2})", raw_date)
            if match:
                pub_date = "".join(match.groups())

        if not show_title:
            meta_podcast = extract_meta_content(page_html, "og:podcast:name") or extract_meta_content(page_html, "music:album")
            if meta_podcast:
                show_title = meta_podcast
            else:
                show_title = "未知节目"

        if not pub_date:
            date_match = re.search(r"20\d{2}[-/年]\d{1,2}[-/月]\d{1,2}", page_html)
            if date_match:
                date_str = date_match.group(0)
                parts = re.split(r"[-/预年月日\s]", date_str)
                parts = [p for p in parts if p]
                if len(parts) >= 3:
                    try:
                        pub_date = f"{parts[0]}{int(parts[1]):02d}{int(parts[2]):02d}"
                    except ValueError:
                        pass
        if not pub_date:
            pub_date = time.strftime("%Y%m%d")

        duration_minutes = 0.0
        raw_duration = episode_data.get("duration")
        if isinstance(raw_duration, (int, float)):
            duration_minutes = max(0.0, float(raw_duration) / 60.0)
        elif isinstance(raw_duration, str):
            duration_minutes = parse_duration_minutes(raw_duration)
        if not duration_minutes and isinstance(schema.get("timeRequired"), str):
            duration_minutes = parse_iso8601_duration_minutes(schema.get("timeRequired"))

        return {
            "id": episode_id,
            "title": title,
            "url": url,
            "audio_url": audio_url,
            "cover_url": cover_url,
            "show_notes": show_notes,
            "guests": guests,
            "duration_minutes": duration_minutes,
            "show_title": show_title,
            "pub_date": pub_date,
            "html_sample": page_html[:2000] if not audio_url else None,
        }
    except Exception as exc:
        error(f"获取节目信息失败: {exc}")
        return None


def itunes_lookup(lookup_id: str, entity: str | None = None) -> list[dict[str, Any]]:
    params = f"id={quote(lookup_id)}&country=US"
    if entity:
        params += f"&entity={quote(entity)}"
    data = fetch_json(f"{ITUNES_LOOKUP_URL}?{params}")
    results = data.get("results", []) if data else []
    return [item for item in results if isinstance(item, dict)]


def itunes_search(term: str, entity: str, limit: int = 5) -> list[dict[str, Any]]:
    params = f"media=podcast&entity={quote(entity)}&term={quote(term)}&limit={limit}&country=US"
    data = fetch_json(f"{ITUNES_SEARCH_URL}?{params}")
    results = data.get("results", []) if data else []
    return [item for item in results if isinstance(item, dict)]


def feed_url_from_itunes_result(result: dict[str, Any]) -> str | None:
    feed_url = result.get("feedUrl")
    if isinstance(feed_url, str) and feed_url.startswith("http"):
        return feed_url

    collection_id = result.get("collectionId")
    if collection_id:
        for item in itunes_lookup(str(collection_id)):
            feed_url = item.get("feedUrl")
            if isinstance(feed_url, str) and feed_url.startswith("http"):
                return feed_url
    return None


def episode_info_from_itunes_result(result: dict[str, Any], source_url: str | None = None) -> dict[str, Any] | None:
    audio_url = result.get("episodeUrl") or result.get("previewUrl")
    title = result.get("trackName") or result.get("trackCensoredName")
    if not isinstance(audio_url, str) or not audio_url.startswith("http") or not title:
        return None

    author = result.get("artistName") or result.get("collectionName")
    
    release_date = result.get("releaseDate")
    pub_date = None
    if isinstance(release_date, str):
        match = re.match(r"(\d{4})[-/]?(\d{2})[-/]?(\d{2})", release_date)
        if match:
            pub_date = "".join(match.groups())
    if not pub_date:
        pub_date = time.strftime("%Y%m%d")

    return {
        "id": str(result.get("trackId") or safe_filename(title, "itunes")),
        "title": title,
        "url": source_url or result.get("trackViewUrl") or result.get("collectionViewUrl") or "",
        "audio_url": audio_url,
        "cover_url": result.get("artworkUrl600") or result.get("artworkUrl160") or "",
        "show_notes": (result.get("description") or result.get("shortDescription") or "").strip(),
        "guests": [author] if isinstance(author, str) and author else [],
        "duration_minutes": 0.0,
        "show_title": result.get("collectionName") or "",
        "pub_date": pub_date,
        "source": "itunes_episode",
    }


def get_apple_episode_info(url: str) -> dict[str, Any] | None:
    ids = parse_apple_podcasts_url(url)
    episode_id = ids.get("episode_id")
    podcast_id = ids.get("podcast_id")
    episode_title = None
    feed_url = None

    if episode_id:
        for item in itunes_lookup(episode_id, entity="podcastEpisode"):
            if item.get("wrapperType") == "podcastEpisode" or item.get("kind") == "podcast-episode":
                direct_info = episode_info_from_itunes_result(item, source_url=url)
                if direct_info:
                    direct_info["source"] = "apple_podcasts"
                    return direct_info
                episode_title = item.get("trackName") or item.get("trackCensoredName")
                feed_url = feed_url_from_itunes_result(item)
                break

    if episode_id and not episode_title:
        slug_query = apple_slug_query(url)
        if slug_query:
            for item in itunes_search(slug_query, "podcastEpisode", limit=10):
                if str(item.get("trackId") or "") == episode_id or f"i={episode_id}" in str(item.get("trackViewUrl") or ""):
                    direct_info = episode_info_from_itunes_result(item, source_url=url)
                    if direct_info:
                        direct_info["source"] = "apple_podcasts"
                        return direct_info
                    episode_title = item.get("trackName") or item.get("trackCensoredName")
                    feed_url = feed_url_from_itunes_result(item)
                    break

    if not feed_url and podcast_id:
        for item in itunes_lookup(podcast_id):
            feed_url = feed_url_from_itunes_result(item)
            if feed_url:
                break

    if not feed_url:
        page_html = fetch_text(url)
        if page_html:
            data = extract_next_data(page_html)
            feed_url = find_rss_feed_url(page_html, data)
            episode_title = episode_title or extract_page_title(page_html, [" - Apple Podcasts", " on Apple Podcasts"])

    if not feed_url:
        error("未能从 Apple Podcasts 链接找到 RSS feed")
        return None

    info = select_rss_episode(feed_url, episode_title=episode_title, episode_id=episode_id, source_url=url)
    if info:
        info["source"] = "apple_podcasts"
        info["id"] = episode_id or info.get("id") or podcast_id or safe_filename(info["title"], "apple")
    return info


def get_overcast_episode_info(url: str) -> dict[str, Any] | None:
    page_html = fetch_text(url)
    if not page_html:
        return None

    data = extract_next_data(page_html)
    title = extract_page_title(page_html, [" - Overcast", " | Overcast"])
    audio_url = find_audio_url(page_html, data)
    show_notes = extract_show_notes(page_html, data)
    feed_url = find_rss_feed_url(page_html, data)

    if audio_url:
        page_info = {
            "id": safe_filename(title, "overcast"),
            "title": title,
            "url": url,
            "audio_url": audio_url,
            "cover_url": extract_meta_content(page_html, "og:image") or "",
            "show_notes": show_notes,
            "guests": extract_guests(title, show_notes, data),
            "duration_minutes": 0.0,
            "show_title": extract_meta_content(page_html, "og:site_name") or "Overcast",
            "pub_date": time.strftime("%Y%m%d"),
            "source": "overcast",
        }
        fallback_info = None
        fallback_source = None
        if feed_url:
            fallback_info = select_rss_episode(feed_url, episode_title=title, source_url=url)
            fallback_source = "overcast_rss"
        if not fallback_info and not show_notes and normalize_match_text(title) not in {"", "overcast"}:
            warn("Overcast 页面没有 Show Notes，尝试通过播客目录补全")
            fallback_info = search_episode_info(title)
            fallback_source = "overcast_catalog"
        if fallback_info:
            merged = dict(fallback_info)
            merged["url"] = url
            merged["audio_url"] = audio_url or merged.get("audio_url")
            merged["cover_url"] = merged.get("cover_url") or page_info.get("cover_url") or ""
            merged["show_notes"] = show_notes or merged.get("show_notes") or ""
            merged["guests"] = page_info.get("guests") or merged.get("guests") or []
            merged["source"] = fallback_source
            return merged
        return page_info

    if feed_url:
        info = select_rss_episode(feed_url, episode_title=title, source_url=url)
        if info:
            info["source"] = "overcast_rss"
            return info

    if normalize_match_text(title) and normalize_match_text(title) != "overcast":
        warn("Overcast 页面未暴露音频，尝试用页面标题搜索播客目录")
        return search_episode_info(title)

    error("Overcast 页面未暴露音频或 RSS；请改用单集分享链接、Apple Podcasts 链接、RSS 链接或节目名搜索")
    return None


def get_rss_episode_info(url: str) -> dict[str, Any] | None:
    info = select_rss_episode(url)
    if info:
        info["source"] = "rss_url"
    return info


def get_podwise_episode_info(url: str) -> dict[str, Any] | None:
    page_html = fetch_text(url)
    if not page_html:
        return None
    data = extract_next_data(page_html)
    title = extract_page_title(page_html, [" - Podwise", " | Podwise"])
    audio_url = find_audio_url(page_html, data)
    show_notes = extract_show_notes(page_html, data)
    show_title = extract_meta_content(page_html, "og:site_name") or "Podwise"
    
    if not audio_url:
        warn("Podwise 页面没有直接暴露音频 URL，正在使用标题在 iTunes 搜索...")
        resolved = search_episode_info(title)
        if resolved:
            resolved["url"] = url
            resolved["source"] = "podwise"
            return resolved
            
    return {
        "id": safe_filename(title, "podwise"),
        "title": title,
        "url": url,
        "audio_url": audio_url,
        "cover_url": extract_meta_content(page_html, "og:image") or "",
        "show_notes": show_notes,
        "guests": extract_guests(title, show_notes, data),
        "duration_minutes": 0.0,
        "show_title": show_title,
        "pub_date": time.strftime("%Y%m%d"),
        "source": "podwise",
    }


def get_spotify_episode_info(url: str) -> dict[str, Any] | None:
    page_html = fetch_text(url)
    if not page_html:
        return None
    
    title = extract_meta_content(page_html, "og:title")
    if not title:
        title_match = re.search(r"<title>(.*?)</title>", page_html, flags=re.DOTALL | re.IGNORECASE)
        title = clean_text(title_match.group(1)) if title_match else ""
    # Clean Spotify title suffixes
    title = re.sub(r"\s*\|\s*Podcast on Spotify", "", title)
    title = re.sub(r"\s*-\s*episode on Spotify", "", title)
    title = re.sub(r"\s*-\s*Spotify", "", title)

    show_title = extract_meta_content(page_html, "og:audio:artist") or extract_meta_content(page_html, "music:creator")
    if not show_title:
        desc = extract_meta_content(page_html, "og:description") or ""
        desc_match = re.search(r"Listen to this episode from (.*?) on Spotify", desc)
        if desc_match:
            show_title = desc_match.group(1).strip()
            
    search_query = f"{show_title} {title}" if show_title else title
    log(f"Spotify 页面提取的标题: {title}, 节目: {show_title or '未知'}")
    warn("Spotify 页面没有直接暴露音频 URL，正在通过 iTunes 检索对应的 RSS...")
    
    resolved = search_episode_info(search_query)
    if resolved:
        resolved["url"] = url
        resolved["source"] = "spotify"
        if show_title:
            resolved["show_title"] = show_title
        return resolved
        
    data = extract_next_data(page_html)
    feed_url = find_rss_feed_url(page_html, data)
    if feed_url:
        resolved = select_rss_episode(feed_url, episode_title=title, source_url=url)
        if resolved:
            resolved["source"] = "spotify"
            return resolved
            
    if show_title:
        resolved = search_episode_info(title)
        if resolved:
            resolved["url"] = url
            resolved["source"] = "spotify"
            return resolved
            
    return None


def get_pocketcasts_episode_info(url: str) -> dict[str, Any] | None:
    page_html = fetch_text(url)
    if not page_html:
        return None
    data = extract_next_data(page_html)
    title = extract_page_title(page_html, [" - Pocket Casts", " | Pocket Casts"])
    audio_url = find_audio_url(page_html, data)
    show_notes = extract_show_notes(page_html, data)
    show_title = extract_meta_content(page_html, "og:site_name") or "Pocket Casts"
    
    if audio_url:
        return {
            "id": safe_filename(title, "pocketcasts"),
            "title": title,
            "url": url,
            "audio_url": audio_url,
            "cover_url": extract_meta_content(page_html, "og:image") or "",
            "show_notes": show_notes,
            "guests": extract_guests(title, show_notes, data),
            "duration_minutes": 0.0,
            "show_title": show_title,
            "pub_date": time.strftime("%Y%m%d"),
            "source": "pocketcasts",
        }
        
    feed_url = find_rss_feed_url(page_html, data)
    if feed_url:
        resolved = select_rss_episode(feed_url, episode_title=title, source_url=url)
        if resolved:
            resolved["source"] = "pocketcasts"
            return resolved
            
    log(f"Pocket Casts 无法直取音频，尝试使用标题在 iTunes 搜索: {title}")
    resolved = search_episode_info(title)
    if resolved:
        resolved["url"] = url
        resolved["source"] = "pocketcasts"
        return resolved
    return None


def get_castro_episode_info(url: str) -> dict[str, Any] | None:
    page_html = fetch_text(url)
    if not page_html:
        return None
    data = extract_next_data(page_html)
    title = extract_page_title(page_html, [" - Castro", " | Castro"])
    audio_url = find_audio_url(page_html, data)
    show_notes = extract_show_notes(page_html, data)
    show_title = extract_meta_content(page_html, "og:site_name") or "Castro"
    
    if audio_url:
        return {
            "id": safe_filename(title, "castro"),
            "title": title,
            "url": url,
            "audio_url": audio_url,
            "cover_url": extract_meta_content(page_html, "og:image") or "",
            "show_notes": show_notes,
            "guests": extract_guests(title, show_notes, data),
            "duration_minutes": 0.0,
            "show_title": show_title,
            "pub_date": time.strftime("%Y%m%d"),
            "source": "castro",
        }
        
    feed_url = find_rss_feed_url(page_html, data)
    if feed_url:
        resolved = select_rss_episode(feed_url, episode_title=title, source_url=url)
        if resolved:
            resolved["source"] = "castro"
            return resolved
            
    log(f"Castro 无法直取音频，尝试使用标题在 iTunes 搜索: {title}")
    resolved = search_episode_info(title)
    if resolved:
        resolved["url"] = url
        resolved["source"] = "castro"
        return resolved
    return None


def get_castbox_episode_info(url: str) -> dict[str, Any] | None:
    page_html = fetch_text(url)
    if not page_html:
        return None
    data = extract_next_data(page_html)
    title = extract_page_title(page_html, [" - Castbox", " | Castbox"])
    audio_url = find_audio_url(page_html, data)
    show_notes = extract_show_notes(page_html, data)
    show_title = extract_meta_content(page_html, "og:site_name") or "Castbox"
    
    if not audio_url:
        audio_url_match = re.search(r'https?://[^"\']+\.(mp3|m4a|aac)[^"\']+', page_html)
        if audio_url_match:
            audio_url = audio_url_match.group(0)
            
    if audio_url:
        return {
            "id": safe_filename(title, "castbox"),
            "title": title,
            "url": url,
            "audio_url": audio_url,
            "cover_url": extract_meta_content(page_html, "og:image") or "",
            "show_notes": show_notes,
            "guests": extract_guests(title, show_notes, data),
            "duration_minutes": 0.0,
            "show_title": show_title,
            "pub_date": time.strftime("%Y%m%d"),
            "source": "castbox",
        }
        
    feed_url = find_rss_feed_url(page_html, data)
    if feed_url:
        resolved = select_rss_episode(feed_url, episode_title=title, source_url=url)
        if resolved:
            resolved["source"] = "castbox"
            return resolved
            
    log(f"Castbox 无法直取音频，尝试使用标题在 iTunes 搜索: {title}")
    resolved = search_episode_info(title)
    if resolved:
        resolved["url"] = url
        resolved["source"] = "castbox"
        return resolved
    return None


def select_ytdlp_audio_url(
    info_json: dict[str, Any],
    page_url: str,
    *,
    timeout: int = 45,
) -> str | None:
    """Return an audio-only stream URL and never fall back to a video format."""
    audio_formats = [
        item
        for item in info_json.get("formats") or []
        if item.get("vcodec") == "none"
        and item.get("acodec") not in (None, "none")
        and item.get("url")
    ]
    audio_formats.sort(
        key=lambda item: item.get("abr") or item.get("tbr") or 0,
        reverse=True,
    )
    if audio_formats:
        return str(audio_formats[0]["url"])

    result = run_command(
        ["yt-dlp", "-g", "-f", "bestaudio", "--no-playlist", page_url],
        timeout=timeout,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip().splitlines()[0]
    return None


def get_youtube_episode_info(url: str) -> dict[str, Any] | None:
    log(f"解析 YouTube 链接: {url}")
    try:
        result = run_command(
            ["yt-dlp", "--dump-json", "--no-playlist", url],
            timeout=60
        )
        if result.returncode == 0 and result.stdout:
            info_json = json.loads(result.stdout)
            title = info_json.get("title") or "YouTube Video"
            audio_url = select_ytdlp_audio_url(info_json, url, timeout=30)

            show_notes = info_json.get("description") or ""
            show_title = info_json.get("uploader") or "YouTube Channel"
            
            upload_date = info_json.get("upload_date")
            if not upload_date:
                upload_date = time.strftime("%Y%m%d")
                
            if audio_url:
                return {
                    "id": info_json.get("id") or safe_filename(title, "youtube"),
                    "title": title,
                    "url": url,
                    "audio_url": audio_url,
                    "cover_url": info_json.get("thumbnail") or "",
                    "show_notes": show_notes,
                    "guests": extract_guests(title, show_notes, None),
                    "duration_minutes": (info_json.get("duration") or 0) / 60.0,
                    "show_title": show_title,
                    "pub_date": upload_date,
                    "source": "youtube",
                }
    except Exception as exc:
        warn(f"YouTube/yt-dlp 解析异常: {exc}")
    return None


def get_ytdlp_media_info(url: str, source: str) -> dict[str, Any] | None:
    """解析可被 yt-dlp 支持的国内视频/音频页面。"""
    log(f"解析 {source} 链接: {url}")
    try:
        result = run_command(
            ["yt-dlp", "--dump-single-json", "--no-playlist", "--skip-download", url],
            timeout=90,
        )
        if result.returncode != 0 or not result.stdout.strip():
            warn(f"{source} 的 yt-dlp 解析失败: {result.stderr.strip()[-500:]}")
            return None

        info_json = json.loads(result.stdout)
        title = info_json.get("title") or f"{source} 音频"
        audio_url = select_ytdlp_audio_url(info_json, url)
        if not audio_url:
            warn(f"{source} 没有可用的纯音频格式，拒绝下载视频格式")
            return None

        upload_date = info_json.get("upload_date") or time.strftime("%Y%m%d")
        return {
            "id": info_json.get("id") or safe_filename(title, source),
            "title": title,
            "url": url,
            "audio_url": audio_url,
            "cover_url": info_json.get("thumbnail") or "",
            "show_notes": info_json.get("description") or "",
            "guests": extract_guests(title, info_json.get("description") or "", None),
            "duration_minutes": (info_json.get("duration") or 0) / 60.0,
            "show_title": info_json.get("channel") or info_json.get("uploader") or source,
            "pub_date": upload_date,
            "source": source,
        }
    except FileNotFoundError:
        warn("未安装 yt-dlp，跳过该平台的直接解析")
    except Exception as exc:
        warn(f"{source}/yt-dlp 解析异常: {exc}")
    return None


def get_chinese_media_episode_info(url: str, source: str) -> dict[str, Any] | None:
    """国内平台统一回退：yt-dlp → 页面音频/RSS → 标题搜索。"""
    direct = get_ytdlp_media_info(url, source)
    if direct:
        return direct

    page_html = fetch_text(url)
    if not page_html:
        return None
    data = extract_next_data(page_html)
    title = extract_page_title(page_html, [f" - {source}", f" | {source}"])
    audio_url = find_audio_url(page_html, data)
    show_notes = extract_show_notes(page_html, data)
    feed_url = find_rss_feed_url(page_html, data)
    if audio_url:
        return {
            "id": safe_filename(title, source),
            "title": title,
            "url": url,
            "audio_url": audio_url,
            "cover_url": extract_meta_content(page_html, "og:image") or "",
            "show_notes": show_notes,
            "guests": extract_guests(title, show_notes, data),
            "duration_minutes": 0.0,
            "show_title": extract_meta_content(page_html, "og:site_name") or source,
            "pub_date": time.strftime("%Y%m%d"),
            "source": source,
        }
    if feed_url:
        info = select_rss_episode(feed_url, episode_title=title, source_url=url)
        if info:
            info["source"] = source
            return info

    if title and title != "Unknown":
        resolved = search_episode_info(title)
        if resolved:
            resolved["url"] = url
            resolved["source"] = source
            return resolved
    return None


def get_listennotes_episode_info(url: str) -> dict[str, Any] | None:
    page_html = fetch_text(url)
    if not page_html:
        return None
    data = extract_next_data(page_html)
    title = extract_page_title(page_html, [" - Listen Notes", " | Listen Notes"])
    feed_url = find_rss_feed_url(page_html, data)
    audio_url = find_audio_url(page_html, data)
    show_notes = extract_show_notes(page_html, data)
    show_title = extract_meta_content(page_html, "og:site_name") or "Listen Notes"
    
    if audio_url:
        return {
            "id": safe_filename(title, "listennotes"),
            "title": title,
            "url": url,
            "audio_url": audio_url,
            "cover_url": extract_meta_content(page_html, "og:image") or "",
            "show_notes": show_notes,
            "guests": extract_guests(title, show_notes, data),
            "duration_minutes": 0.0,
            "show_title": show_title,
            "pub_date": time.strftime("%Y%m%d"),
            "source": "listennotes",
        }
        
    if feed_url:
        resolved = select_rss_episode(feed_url, episode_title=title, source_url=url)
        if resolved:
            resolved["source"] = "listennotes"
            return resolved
            
    log(f"Listen Notes 无法直取音频，尝试使用标题在 iTunes 搜索: {title}")
    resolved = search_episode_info(title)
    if resolved:
        resolved["url"] = url
        resolved["source"] = "listennotes"
        return resolved
    return None


def get_podbean_episode_info(url: str) -> dict[str, Any] | None:
    page_html = fetch_text(url)
    if not page_html:
        return None
    data = extract_next_data(page_html)
    title = extract_page_title(page_html, [" - Podbean", " | Podbean"])
    audio_url = find_audio_url(page_html, data)
    show_notes = extract_show_notes(page_html, data)
    show_title = extract_meta_content(page_html, "og:site_name") or "Podbean"
    
    if audio_url:
        return {
            "id": safe_filename(title, "podbean"),
            "title": title,
            "url": url,
            "audio_url": audio_url,
            "cover_url": extract_meta_content(page_html, "og:image") or "",
            "show_notes": show_notes,
            "guests": extract_guests(title, show_notes, data),
            "duration_minutes": 0.0,
            "show_title": show_title,
            "pub_date": time.strftime("%Y%m%d"),
            "source": "podbean",
        }
        
    feed_url = find_rss_feed_url(page_html, data)
    if feed_url:
        resolved = select_rss_episode(feed_url, episode_title=title, source_url=url)
        if resolved:
            resolved["source"] = "podbean"
            return resolved
            
    log(f"Podbean 无法直取音频，尝试使用标题在 iTunes 搜索: {title}")
    resolved = search_episode_info(title)
    if resolved:
        resolved["url"] = url
        resolved["source"] = "podbean"
        return resolved
    return None


def get_iheart_episode_info(url: str) -> dict[str, Any] | None:
    page_html = fetch_text(url)
    if not page_html:
        return None
    data = extract_next_data(page_html)
    title = extract_page_title(page_html, [" - iHeart", " | iHeart"])
    audio_url = find_audio_url(page_html, data)
    show_notes = extract_show_notes(page_html, data)
    show_title = extract_meta_content(page_html, "og:site_name") or "iHeart"
    
    if audio_url:
        return {
            "id": safe_filename(title, "iheart"),
            "title": title,
            "url": url,
            "audio_url": audio_url,
            "cover_url": extract_meta_content(page_html, "og:image") or "",
            "show_notes": show_notes,
            "guests": extract_guests(title, show_notes, data),
            "duration_minutes": 0.0,
            "show_title": show_title,
            "pub_date": time.strftime("%Y%m%d"),
            "source": "iheart",
        }
        
    feed_url = find_rss_feed_url(page_html, data)
    if feed_url:
        resolved = select_rss_episode(feed_url, episode_title=title, source_url=url)
        if resolved:
            resolved["source"] = "iheart"
            return resolved
            
    log(f"iHeart 无法直取音频，尝试使用标题在 iTunes 搜索: {title}")
    resolved = search_episode_info(title)
    if resolved:
        resolved["url"] = url
        resolved["source"] = "iheart"
        return resolved
    return None



def search_episode_info(query: str) -> dict[str, Any] | None:
    log(f"按名称搜索: {query}")

    episode_results = itunes_search(query, "podcastEpisode", limit=8)
    for result in episode_results:
        title = result.get("trackName") or result.get("trackCensoredName")
        collection = result.get("collectionName")
        title_score = query_match_score(query, title)
        combined_score = query_match_score(query, f"{collection or ''} {title or ''}")
        term_count = len(query_terms(query))
        if title_score < 0.35 and not (term_count >= 3 and combined_score >= 0.6):
            continue
        direct_info = episode_info_from_itunes_result(result, source_url=result.get("trackViewUrl"))
        if direct_info:
            direct_info["source"] = "itunes_episode_search"
            log(f"搜索命中单集: {direct_info['title']}")
            return direct_info
        feed_url = feed_url_from_itunes_result(result)
        if not feed_url:
            continue
        info = select_rss_episode(feed_url, episode_title=title, episode_id=str(result.get("trackId") or ""), source_url=result.get("trackViewUrl"))
        if info and info.get("audio_url"):
            info["source"] = "itunes_episode_search"
            info["id"] = str(result.get("trackId") or info.get("id") or safe_filename(info["title"], "search"))
            log(f"搜索命中单集: {info['title']}")
            return info

    podcast_results = itunes_search(query, "podcast", limit=5)
    for result in podcast_results:
        feed_url = feed_url_from_itunes_result(result)
        if not feed_url:
            continue
        info = select_rss_episode(feed_url, source_url=result.get("collectionViewUrl"))
        if info and info.get("audio_url"):
            info["source"] = "itunes_podcast_search_latest"
            info["id"] = str(result.get("collectionId") or info.get("id") or safe_filename(info["title"], "search"))
            warn(f"只匹配到播客节目，默认使用最新一集: {info['title']}")
            return info

    return None


def extract_rich_text_link(input_value: str) -> tuple[str | None, str | None]:
    """
    尝试从富文本/Markdown/混合输入中提取标题和 URL。
    返回 (extracted_title, extracted_url)。
    如果输入没有任何 URL，则返回 (None, None)。
    """
    input_value = input_value.strip()

    # 1. 尝试匹配 Markdown 链接格式: [标题](URL)
    m = re.search(r'\[(.*?)\]\((https?://[^\s)]+)\)', input_value)
    if m:
        title = m.group(1).strip()
        url = m.group(2).strip()
        return title if title else None, url

    # 2. 尝试匹配 HTML 链接格式: <a href="URL">标题</a>
    m = re.search(r'<a\s+[^>]*href=["\'](https?://[^"\']+)["\'][^>]*>(.*?)</a>', input_value, re.IGNORECASE)
    if m:
        url = m.group(1).strip()
        title = clean_text(m.group(2))
        return title if title else None, url

    # 3. 尝试直接寻找 URL 并提取它之前的文字作为标题
    m = re.search(r'(https?://[^\s<>"]+)', input_value)
    if m:
        url = m.group(1).strip()
        # 将 URL 移除后的部分清理作为标题
        remaining = input_value.replace(url, "")
        # 清理可能存在的 markdown 语法、冒号、中括号等符号
        remaining = re.sub(r'[\[\]\(\)<>#\-—_~*`:：|]+', ' ', remaining)
        title = re.sub(r'\s+', ' ', remaining).strip()
        return title if len(title) >= 2 else None, url

    return None, None


def resolve_url_info(url: str) -> dict[str, Any] | None:
    """仅解析纯 URL，提取播客单集元数据。"""
    parsed = urlparse(url)
    host = parsed.netloc.lower()

    if "xiaoyuzhoufm.com" in host:
        episode_id = parse_xiaoyuzhou_url(url)
        if not episode_id:
            error("无法解析小宇宙链接，请确认链接中包含 /episode/ 后的 24 位 id")
            return None
        info = get_episode_info(episode_id)
        if info:
            info["source"] = "xiaoyuzhou"
        return info

    if "podcasts.apple.com" in host:
        return get_apple_episode_info(url)

    if "overcast.fm" in host:
        return get_overcast_episode_info(url)

    if "podwise.ai" in host:
        return get_podwise_episode_info(url)

    if "spotify.com" in host:
        return get_spotify_episode_info(url)

    if "pca.st" in host:
        return get_pocketcasts_episode_info(url)

    if "castro.fm" in host:
        return get_castro_episode_info(url)

    if "castbox.fm" in host:
        return get_castbox_episode_info(url)

    if "youtube.com" in host or "youtu.be" in host:
        return get_youtube_episode_info(url)

    if "bilibili.com" in host or host.endswith("b23.tv"):
        return get_chinese_media_episode_info(url, "bilibili")

    if "music.163.com" in host or "163.com" in host:
        return get_chinese_media_episode_info(url, "netease_cloud_music")

    if "ximalaya.com" in host:
        return get_chinese_media_episode_info(url, "ximalaya")

    if "lizhi.fm" in host or "lizhiweike.com" in host:
        return get_chinese_media_episode_info(url, "lizhi_fm")

    if "listennotes.com" in host:
        return get_listennotes_episode_info(url)

    if "podbean.com" in host:
        return get_podbean_episode_info(url)

    if "iheart.com" in host:
        return get_iheart_episode_info(url)

    if parsed.path.lower().endswith((".xml", ".rss")) or "rss" in parsed.path.lower() or "feed" in parsed.path.lower():
        return get_rss_episode_info(url)

    page_html = fetch_text(url)
    if not page_html:
        return None
    data = extract_next_data(page_html)
    audio_url = find_audio_url(page_html, data)
    title = extract_page_title(page_html)
    show_notes = extract_show_notes(page_html, data)
    if audio_url:
        return {
            "id": safe_filename(title, "web"),
            "title": title,
            "url": url,
            "audio_url": audio_url,
            "cover_url": extract_meta_content(page_html, "og:image") or "",
            "show_notes": show_notes,
            "guests": extract_guests(title, show_notes, data),
            "duration_minutes": 0.0,
            "show_title": extract_meta_content(page_html, "og:site_name") or "网页",
            "pub_date": time.strftime("%Y%m%d"),
            "source": "web_page",
        }

    feed_url = find_rss_feed_url(page_html, data)
    if feed_url:
        return select_rss_episode(feed_url, episode_title=title, source_url=url)

    warn("网页未暴露音频或 RSS，尝试用页面标题搜索")
    return search_episode_info(title)


def resolve_local_media_info(input_value: str) -> dict[str, Any] | None:
    candidate = Path(input_value).expanduser()
    if not candidate.is_file() or candidate.suffix.lower() not in LOCAL_MEDIA_EXTENSIONS:
        return None
    path = candidate.resolve()
    modified = time.localtime(path.stat().st_mtime)
    media_kind = "audio" if path.suffix.lower() in LOCAL_AUDIO_EXTENSIONS else "video"
    title = clean_text(path.stem.replace("_", " ").replace("-", " ")) or path.stem
    return {
        "id": safe_filename(path.stem, "local-media"),
        "title": title,
        "url": path.as_uri(),
        "audio_url": str(path),
        "cover_url": "",
        "show_notes": f"本地课程{'视频' if media_kind == 'video' else '音频'}：{path.name}",
        "guests": [],
        "duration_minutes": 0.0,
        "show_title": path.parent.name or "本地课程",
        "pub_date": time.strftime("%Y%m%d", modified),
        "source": "local_media",
        "media_kind": media_kind,
        "local_media_path": str(path),
    }


def resolve_episode_info(input_value: str) -> dict[str, Any] | None:
    value = input_value.strip()
    if not value:
        return None

    local_info = resolve_local_media_info(value)
    if local_info:
        media_label = "视频" if local_info["media_kind"] == "video" else "音频"
        log(f"识别本地课程{media_label}: {local_info['local_media_path']}")
        return local_info

    # 提取可能的标题与 URL
    extracted_title, extracted_url = extract_rich_text_link(value)

    if extracted_url:
        log(f"提取出 URL: {extracted_url}")
        if extracted_title:
            log(f"提取出标题: {extracted_title}")

        # 尝试使用 URL 解析
        info = resolve_url_info(extracted_url)

        # 成功拿到音频，直接返回
        if info and info.get("audio_url"):
            if extracted_title and (not info.get("title") or info.get("title") == "Unknown"):
                info["title"] = extracted_title
            return info

        # URL 解析失败或未提取到音频，如果输入中带有标题，启动搜索恢复流程
        if extracted_title:
            warn(f"链接解析或音频提取失败，尝试使用提取的标题进行搜索恢复: {extracted_title}")
            search_info = search_episode_info(extracted_title)
            if search_info and search_info.get("audio_url"):
                log(f"搜索恢复成功，匹配到单集: {search_info['title']}")
                return search_info

        # 如果没有提取到标题，或搜索也失败了，但 info 含有页面其他元数据（如 show_notes），则尝试返回
        if info:
            return info
        return None

    else:
        # 没有 URL 时，直接当作搜索词搜索
        return search_episode_info(value)


def download_audio(audio_url: str, output_path: Path) -> bool:
    log(f"下载音频: {audio_url[:80]}...")
    dl_timeout = int(os.environ.get("DOWNLOAD_TIMEOUT", "1800"))
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        parsed_audio = urlparse(audio_url)
        local_path = Path(audio_url).expanduser() if not parsed_audio.scheme else None
        if local_path and local_path.is_file():
            if local_path.suffix.lower() in LOCAL_AUDIO_EXTENSIONS:
                shutil.copyfile(local_path, output_path)
                log(f"读取本地音频: {local_path.name}")
                return output_path.is_file() and output_path.stat().st_size > 0
            result = run_command(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(local_path),
                    "-map",
                    "0:a:0",
                    "-vn",
                    "-c:a",
                    "aac",
                    "-b:a",
                    os.environ.get("COURSE_AUDIO_BITRATE", "128k"),
                    str(output_path),
                ],
                timeout=dl_timeout,
            )
            if result.returncode == 0 and output_path.is_file() and output_path.stat().st_size > 0:
                size_mb = output_path.stat().st_size / 1024 / 1024
                log(f"本地视频音轨提取完成: {size_mb:.1f} MB")
                return True
            error(f"本地视频音轨提取失败: {result.stderr.strip()[-800:]}")
            return False

        if ".m3u8" in audio_url.lower():
            response, final_url = open_safe_http(
                audio_url,
                headers={"Range": "bytes=0-0"},
                timeout=min(dl_timeout, 60),
            )
            response.close()
            result = run_command(
                [
                    "ffmpeg",
                    "-y",
                    "-protocol_whitelist",
                    "file,http,https,tcp,tls,crypto",
                    "-i",
                    final_url,
                    "-c",
                    "copy",
                    str(output_path),
                ],
                timeout=dl_timeout,
            )
            if result.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0:
                size_mb = output_path.stat().st_size / 1024 / 1024
                log(f"HLS 下载完成: {size_mb:.1f} MB")
                return True
            error(f"HLS 下载失败: {result.stderr.strip()[-800:]}")
            return False

        max_audio_bytes = int(
            os.environ.get("MAX_AUDIO_BYTES", str(2 * 1024 * 1024 * 1024))
        )
        downloaded = download_http_resource(
            audio_url,
            output_path,
            max_bytes=max_audio_bytes,
            timeout=dl_timeout,
            resume=True,
        )
        if downloaded.get("ok") and output_path.exists() and output_path.stat().st_size > 0:
            size_mb = output_path.stat().st_size / 1024 / 1024
            log(f"下载完成: {size_mb:.1f} MB")
            return True
        error(f"下载失败: {downloaded.get('error') or 'unknown error'}")
        return False
    except Exception as exc:
        error(f"下载异常: {exc}")
        return False


def preprocess_audio(input_path: Path, wav_path: Path) -> bool:
    """转换为 mono WAV（保持原始采样率，避免 ffmpeg 重采样 bug）。

    历史 bug：强制 16kHz 重采样会触发 ffmpeg 异常，导致部分 m4a 音频被错误地静音。
    faster-whisper 支持任意采样率，所以直接转 mono 即可，保留原始采样率（44.1k/48k）。
    """
    log("预处理音频: 转换为 mono WAV（保持原始采样率）...")
    prep_timeout = int(os.environ.get("PREPROCESS_TIMEOUT", "1800"))
    try:
        result = run_command(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(input_path),
                "-ac",
                "1",
                str(wav_path),
            ],
            timeout=prep_timeout,
        )
        if result.returncode == 0 and wav_path.exists() and wav_path.stat().st_size > 0:
            log("音频预处理完成")
            return True
        error(f"ffmpeg 预处理失败: {result.stderr.strip()[-800:]}")
        return False
    except FileNotFoundError:
        error("ffmpeg 未安装，请先安装 ffmpeg")
        return False
    except Exception as exc:
        error(f"音频预处理异常: {exc}")
        return False


def get_audio_duration(audio_path: Path) -> float:
    try:
        result = run_command(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(audio_path),
            ],
            timeout=30,
        )
        return float(result.stdout.strip()) / 60
    except Exception:
        return 0.0


def build_initial_prompt(info: dict[str, Any]) -> str:
    names = "、".join(info.get("guests") or [])
    if names:
        return f"以下是中文播客对话，说话人可能包括：{names}。请特别保留人名、书名、产品名和专业术语。"
    return f"以下是中文播客《{info['title']}》的对话。请特别保留人名、书名、产品名和专业术语。"


# ─────────────────────────────────────────────────────────────────────────────
# SenseVoice-Small (FunASR) 主力转录引擎
# ─────────────────────────────────────────────────────────────────────────────

# SenseVoice 模型 ID（ModelScope 上的标准路径）
SENSEVOICE_MODEL_ID = "iic/SenseVoiceSmall"
_sensevoice_model: Any | None = None
_sensevoice_lock = threading.Lock()

# VAD 分段参数（防 OOM）
VAD_MAX_SEGMENT_SEC = 90      # 每段最长秒数，M1 Pro 32GB 推荐 60–90s
VAD_SILENCE_SEC = 0.5         # 静音检测阈值（秒），短于此的静音不切


def clean_transcript_text(text: str) -> str:
    """统一清洗转录输出文本，兼容 SenseVoice / Whisper 两种来源。

    SenseVoice 原始输出包含情感与语种标签，例如：
        <|zh|><|HAPPY|><|Speech|><|withitn|>这里是实际文字
    这些标签对下游 LLM 处理无用，须全部剥离。
    同时统一全角/半角混用的标点，去除多余空白行。
    """
    # 剥离所有 <|...|> 风格标签（SenseVoice 特有）
    text = re.sub(r"<\|[^|]*\|>", "", text)
    # 合并连续空白（保留换行结构）
    text = re.sub(r"[ \t]+", " ", text)
    # 去除行首/行尾多余空白
    lines = [line.strip() for line in text.splitlines()]
    # 去除连续空行（保留最多一个空行）
    cleaned_lines: list[str] = []
    prev_empty = False
    for line in lines:
        if not line:
            if not prev_empty:
                cleaned_lines.append("")
            prev_empty = True
        else:
            cleaned_lines.append(line)
            prev_empty = False
    return "\n".join(cleaned_lines).strip()


def parse_caption_timestamp(value: Any, key: str = "") -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
        if "millisecond" in key.lower() or key.lower().endswith("ms"):
            seconds /= 1000
        return max(0.0, seconds)
    raw = str(value).strip().replace(",", ".")
    if not raw:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", raw):
        return max(0.0, float(raw))
    parts = raw.split(":")
    try:
        if len(parts) == 2:
            return max(0.0, float(parts[0]) * 60 + float(parts[1]))
        if len(parts) == 3:
            return max(
                0.0,
                float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2]),
            )
    except ValueError:
        return None
    return None


def clean_caption_payload(lines: list[str]) -> tuple[str, str | None]:
    raw = "\n".join(lines).strip()
    voice = re.search(r"<v(?:\.[^ >]+)*(?:\s+([^>]+))?>", raw, flags=re.IGNORECASE)
    speaker = clean_text(voice.group(1)) if voice and voice.group(1) else None
    raw = re.sub(r"</?v(?:\.[^ >]+)*(?:\s+[^>]+)?>", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"<\d{2}:\d{2}(?::\d{2})?\.\d{3}>", "", raw)
    raw = re.sub(r"<[^>]+>", "", raw)
    return clean_transcript_text(html.unescape(raw)), speaker


def parse_caption_document(document: str, *, webvtt: bool) -> list[dict[str, Any]]:
    lines = document.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    segments: list[dict[str, Any]] = []
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if not line or (webvtt and line.upper().startswith(("WEBVTT", "NOTE", "STYLE", "REGION"))):
            index += 1
            continue
        timing_line = line
        if "-->" not in timing_line and index + 1 < len(lines):
            timing_line = lines[index + 1].strip()
            if "-->" in timing_line:
                index += 1
        if "-->" not in timing_line:
            index += 1
            continue
        start_raw, end_part = [part.strip() for part in timing_line.split("-->", 1)]
        end_raw = end_part.split()[0] if end_part else ""
        start = parse_caption_timestamp(start_raw)
        end = parse_caption_timestamp(end_raw)
        index += 1
        payload: list[str] = []
        while index < len(lines) and lines[index].strip():
            payload.append(lines[index])
            index += 1
        text, speaker = clean_caption_payload(payload)
        if text and start is not None:
            segment: dict[str, Any] = {
                "start": start,
                "end": max(start, end if end is not None else start),
                "text": text,
            }
            if speaker:
                segment["speaker"] = speaker
            segments.append(segment)
    return segments


def parse_json_transcript(document: str) -> tuple[str, list[dict[str, Any]]]:
    value = json.loads(document)
    segments: list[dict[str, Any]] = []
    text_keys = ("text", "body", "content", "transcript")
    start_keys = ("start", "startTime", "start_time", "offset", "from", "startMs")
    end_keys = ("end", "endTime", "end_time", "to", "endMs")

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            text_key = next(
                (key for key in text_keys if isinstance(node.get(key), str)),
                None,
            )
            start_key = next((key for key in start_keys if key in node), None)
            if text_key and start_key:
                start = parse_caption_timestamp(node.get(start_key), start_key)
                end_key = next((key for key in end_keys if key in node), None)
                end = parse_caption_timestamp(node.get(end_key), end_key or "")
                text = clean_transcript_text(str(node.get(text_key) or ""))
                if text and start is not None:
                    segment: dict[str, Any] = {
                        "start": start,
                        "end": max(start, end if end is not None else start),
                        "text": text,
                    }
                    speaker = node.get("speaker") or node.get("speakerName") or node.get("speaker_name")
                    if isinstance(speaker, str) and speaker.strip():
                        segment["speaker"] = clean_text(speaker)
                    segments.append(segment)
                    return
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)
    if segments:
        segments.sort(key=lambda item: (float(item["start"]), float(item["end"])))
        for index, segment in enumerate(segments[:-1]):
            if float(segment["end"]) <= float(segment["start"]):
                segment["end"] = max(float(segment["start"]), float(segments[index + 1]["start"]))
        text = "\n".join(str(segment["text"]) for segment in segments)
        return clean_transcript_text(text), segments

    if isinstance(value, dict):
        for key in text_keys:
            if isinstance(value.get(key), str):
                return clean_transcript_text(value[key]), []
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return clean_transcript_text("\n".join(value)), []
    return "", []


def publisher_transcript_extension(media_type: str, url: str) -> str:
    normalized = media_type.lower().split(";", 1)[0].strip()
    mapping = {
        "text/vtt": ".vtt",
        "application/x-subrip": ".srt",
        "text/srt": ".srt",
        "application/json": ".json",
        "application/json+transcript": ".json",
        "text/html": ".html",
        "text/plain": ".txt",
    }
    if normalized in mapping:
        return mapping[normalized]
    suffix = Path(urlparse(url).path).suffix.lower()
    return suffix if suffix in {".vtt", ".srt", ".json", ".html", ".htm", ".txt"} else ".txt"


def parse_publisher_transcript(path: Path, media_type: str) -> tuple[str, list[dict[str, Any]]]:
    document = path.read_text(encoding="utf-8-sig", errors="replace")
    normalized = media_type.lower().split(";", 1)[0].strip()
    suffix = path.suffix.lower()
    if normalized == "text/vtt" or suffix == ".vtt":
        segments = parse_caption_document(document, webvtt=True)
        return clean_transcript_text("\n".join(item["text"] for item in segments)), segments
    if normalized in {"application/x-subrip", "text/srt"} or suffix == ".srt":
        segments = parse_caption_document(document, webvtt=False)
        return clean_transcript_text("\n".join(item["text"] for item in segments)), segments
    if "json" in normalized or suffix == ".json":
        return parse_json_transcript(document)
    if normalized == "text/html" or suffix in {".html", ".htm"}:
        return clean_transcript_text(clean_text(document)), []
    return clean_transcript_text(document), []


def load_publisher_transcript(
    info: dict[str, Any],
    transcript_dir: Path,
    combined_name: str,
) -> dict[str, Any] | None:
    candidates = [
        dict(item)
        for item in info.get("transcripts") or []
        if isinstance(item, dict) and item.get("url") and item.get("type")
    ]
    if not candidates:
        return None

    preferred_language = str(info.get("language") or "").lower()
    type_scores = {
        "text/vtt": 50,
        "application/x-subrip": 45,
        "text/srt": 45,
        "application/json": 40,
        "application/json+transcript": 40,
        "text/plain": 25,
        "text/html": 20,
    }

    def candidate_score(candidate: dict[str, Any]) -> int:
        score = type_scores.get(str(candidate.get("type") or "").lower(), 0)
        if str(candidate.get("rel") or "").lower() == "captions":
            score += 10
        language = str(candidate.get("language") or "").lower()
        if preferred_language and language.startswith(preferred_language.split("-", 1)[0]):
            score += 5
        return score

    max_bytes = int(os.environ.get("PUBLISHER_TRANSCRIPT_MAX_BYTES", str(100 * 1024 * 1024)))
    for index, candidate in enumerate(sorted(candidates, key=candidate_score, reverse=True), start=1):
        source_url = str(candidate["url"])
        media_type = str(candidate["type"])
        extension = publisher_transcript_extension(media_type, source_url)
        source_path = transcript_dir / f"publisher-{index:02d}{extension}"
        downloaded = download_http_resource(
            source_url,
            source_path,
            max_bytes=max_bytes,
            timeout=120,
        )
        if not downloaded.get("ok"):
            warn(f"发布方转录获取失败，将尝试下一格式: {source_url} ({downloaded.get('error')})")
            source_path.unlink(missing_ok=True)
            continue
        try:
            text, segments = parse_publisher_transcript(source_path, media_type)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            warn(f"发布方转录解析失败，将尝试下一格式: {source_url} ({exc})")
            source_path.unlink(missing_ok=True)
            continue
        if len(text) < 20:
            warn(f"发布方转录内容过短，将尝试下一格式: {source_url}")
            source_path.unlink(missing_ok=True)
            continue
        quality_issue = transcription_quality_issue(
            {"text": text, "segments": segments},
            float(info.get("duration_minutes") or 0.0),
        )
        if quality_issue:
            warn(f"发布方转录完整性检查未通过，将尝试下一格式: {quality_issue}")
            source_path.unlink(missing_ok=True)
            continue

        source = {
            **candidate,
            **downloaded,
            "path": str(source_path),
            "segment_count": len(segments),
        }
        info["publisher_transcript"] = source
        return {
            "text": text,
            "segments": segments,
            "language": candidate.get("language") or info.get("language") or "unknown",
            "model": f"publisher-transcript:{media_type}",
            "source": "publisher_transcript",
            "source_url": source_url,
            "source_path": str(source_path),
            "initial_prompt": "",
        }
    return None


def archive_episode_chapters(
    info: dict[str, Any],
    transcript_dir: Path,
    combined_name: str,
) -> dict[str, Any] | None:
    chapter_source = info.get("chapters")
    if not isinstance(chapter_source, dict) or not chapter_source.get("url"):
        return None

    source_url = str(chapter_source["url"])
    source_path = transcript_dir / "chapters.source.json"
    chapter_path = transcript_dir / "chapters.json"
    downloaded = download_http_resource(
        source_url,
        source_path,
        max_bytes=int(os.environ.get("PODCAST_CHAPTERS_MAX_BYTES", str(10 * 1024 * 1024))),
        timeout=90,
    )
    archive: dict[str, Any] = {
        **downloaded,
        "source_url": source_url,
        "source_path": str(source_path),
        "path": None,
        "chapter_count": 0,
    }
    if not downloaded.get("ok"):
        archive["failure_reason"] = downloaded.get("error") or "download failed"
        info["chapters_archive"] = archive
        warn(f"章节文件下载失败，继续处理转录: {source_url}")
        return archive

    try:
        source_payload = json.loads(source_path.read_text(encoding="utf-8-sig"))
        if not isinstance(source_payload, dict):
            raise ValueError("chapter document is not a JSON object")
        raw_chapters = source_payload.get("chapters")
        if not isinstance(raw_chapters, list):
            raise ValueError("chapter document has no chapters array")
        chapters: list[dict[str, Any]] = []
        for raw_chapter in raw_chapters:
            if not isinstance(raw_chapter, dict):
                continue
            try:
                start_time = max(0.0, float(raw_chapter.get("startTime")))
            except (TypeError, ValueError):
                continue
            title = clean_text(str(raw_chapter.get("title") or ""))
            chapter = {**raw_chapter, "startTime": start_time}
            if title:
                chapter["title"] = title
            chapters.append(chapter)
        chapters.sort(key=lambda item: float(item["startTime"]))
        if not chapters:
            raise ValueError("chapter document contains no valid chapters")
        normalized = {
            "version": source_payload.get("version") or "1.2.0",
            "chapters": chapters,
            "source_url": source_url,
            "archived_at": iso_timestamp(),
        }
        atomic_write_text(
            chapter_path,
            json.dumps(normalized, ensure_ascii=False, indent=2) + "\n",
        )
        archive.update(
            {
                "ok": True,
                "path": str(chapter_path),
                "chapter_count": len(chapters),
                "version": normalized["version"],
            }
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        archive.update(
            {
                "ok": False,
                "error": str(exc),
                "failure_reason": str(exc),
            }
        )
        warn(f"章节文件解析失败，继续处理转录: {source_url}: {exc}")
    info["chapters_archive"] = archive
    return archive


def vad_segment_audio(wav_path: Path) -> list[tuple[float, float]]:
    """使用 Silero VAD 将音频分成静音边界对齐的片段。

    返回 [(start_sec, end_sec), ...] 列表，每段不超过 VAD_MAX_SEGMENT_SEC 秒。
    如果 silero-vad 未安装或失败，返回空列表（由调用方降级为整段处理）。

    M1 Pro 32GB 内存安全边界：
    - SenseVoice-Small 模型本身约 300MB
    - 90s@44.1kHz mono float32 约 16MB
    - 推理峰值约 1–2GB，远低于 32GB 统一内存上限
    """
    try:
        import torch
        from silero_vad import load_silero_vad, read_audio, get_speech_timestamps

        log("加载 Silero VAD 模型...")
        vad_model = load_silero_vad()

        # silero-vad 要求 16kHz 输入
        wav_resampled_path = wav_path.with_suffix(".vad16k.wav")
        resample_result = run_command(
            [
                "ffmpeg", "-y", "-i", str(wav_path),
                "-ar", "16000", "-ac", "1",
                str(wav_resampled_path),
            ],
            timeout=300,
        )
        if resample_result.returncode != 0 or not wav_resampled_path.exists():
            warn("VAD 重采样失败，将对整段音频转录")
            return []

        wav_tensor = read_audio(str(wav_resampled_path))
        speech_timestamps = get_speech_timestamps(
            wav_tensor,
            vad_model,
            return_seconds=True,
            min_silence_duration_ms=int(VAD_SILENCE_SEC * 1000),
        )

        # 清理 VAD 临时文件
        try:
            wav_resampled_path.unlink()
        except OSError:
            pass

        if not speech_timestamps:
            log("VAD 未检测到有效语音片段，将对整段音频转录")
            return []

        # 合并过短片段，使每段不超过 VAD_MAX_SEGMENT_SEC 秒
        merged: list[tuple[float, float]] = []
        seg_start = speech_timestamps[0]["start"]
        seg_end = speech_timestamps[0]["end"]
        for ts in speech_timestamps[1:]:
            if ts["end"] - seg_start > VAD_MAX_SEGMENT_SEC:
                merged.append((seg_start, seg_end))
                seg_start = ts["start"]
            seg_end = ts["end"]
        merged.append((seg_start, seg_end))

        log(f"VAD 分段完成：{len(merged)} 段（每段 ≤ {VAD_MAX_SEGMENT_SEC}s）")
        return merged

    except ImportError:
        warn("silero-vad 未安装（pip install silero-vad），跳过 VAD 分段")
        return []
    except Exception as exc:
        warn(f"VAD 分段异常: {exc}，跳过分段")
        return []


def get_sensevoice_model() -> Any:
    """在一次进程内缓存 SenseVoice，避免 VAD 分段时重复加载模型。"""
    global _sensevoice_model
    if _sensevoice_model is not None:
        return _sensevoice_model

    with _sensevoice_lock:
        if _sensevoice_model is not None:
            return _sensevoice_model
        from funasr import AutoModel

        log(f"[SenseVoice] 加载模型 {SENSEVOICE_MODEL_ID}（首次运行将自动下载，约 1.2GB）...")
        _sensevoice_model = AutoModel(
            model=SENSEVOICE_MODEL_ID,
            trust_remote_code=True,
            vad_model="fsmn-vad",
            vad_kwargs={"max_single_segment_time": VAD_MAX_SEGMENT_SEC * 1000},
            device="cpu",
        )
        return _sensevoice_model


def transcribe_with_sensevoice(audio_path: Path) -> dict[str, Any] | None:
    """使用 FunASR SenseVoice-Small 转录单段音频。

    SenseVoice-Small 特点：
    - 非自回归架构，推理速度约为 Whisper large-v3 的 15–50 倍
    - 支持中文、粤语、英语、日语、韩语
    - 输出含情感与事件标签（由 clean_transcript_text 剥离）
    - 模型首次运行时通过 modelscope 自动下载到 ~/.cache/modelscope/

    返回格式与 transcribe_with_whisper 一致，供 transcribe_with_fallback 统一处理。
    """
    log(f"[SenseVoice] 开始转录: {audio_path.name}")
    try:
        model = get_sensevoice_model()

        log("[SenseVoice] 转录中...")
        result = model.generate(
            input=str(audio_path),
            cache={},
            language="auto",               # 自动检测语种（支持中/粤/英/日/韩）
            use_itn=True,                  # 启用逆文本规范化（数字/日期格式化）
            batch_size_s=60,              # 批推理窗口（秒）
        )

        if not result or not isinstance(result, list):
            warn("[SenseVoice] 返回结果为空")
            return None

        # FunASR 返回 [{"text": "...", "timestamp": [...]}] 格式
        raw_text = result[0].get("text", "")
        if not raw_text.strip():
            warn("[SenseVoice] 转录文本为空")
            return None

        clean_text = clean_transcript_text(raw_text)
        log(f"[SenseVoice] 转录完成，字符数: {len(clean_text)}")

        return {
            "text": clean_text,
            "segments": [],                 # SenseVoice 段落时间戳可从 result[0]["timestamp"] 读取
            "language": result[0].get("language", "zh"),
            "model": f"sensevoice-{SENSEVOICE_MODEL_ID}",
            "initial_prompt": "",           # SenseVoice 不支持 initial_prompt
        }

    except ImportError:
        warn("[SenseVoice] funasr 未安装（pip install funasr modelscope），将降级至 Whisper")
        return None
    except Exception as exc:
        warn(f"[SenseVoice] 转录失败: {exc}，将降级至 Whisper")
        return None


def transcribe_sensevoice_with_vad(wav_path: Path) -> dict[str, Any] | None:
    """SenseVoice 长音频防 OOM 总入口：VAD 分段 → 逐段转录 → 拼接。

    流程：
    1. 调用 vad_segment_audio() 获取片段时间列表
    2. 若分段成功：用 ffmpeg 逐段切片 → 逐段 transcribe_with_sensevoice() → 拼接
    3. 若分段失败（silero-vad 未安装/异常）：直接整段送 SenseVoice 转录
    """
    segments = vad_segment_audio(wav_path)

    if not segments:
        # 无法分段，整段处理（短音频 / VAD 不可用时）
        log("[SenseVoice] 整段转录模式")
        return transcribe_with_sensevoice(wav_path)

    log(f"[SenseVoice] 分段转录模式：{len(segments)} 段")
    all_texts: list[str] = []
    transcript_segments: list[dict[str, Any]] = []
    tmp_dir = wav_path.parent / f"_vad_tmp_{wav_path.stem}"
    tmp_dir.mkdir(exist_ok=True)

    try:
        for idx, (start_sec, end_sec) in enumerate(segments, start=1):
            duration_sec = end_sec - start_sec
            seg_path = tmp_dir / f"seg_{idx:04d}.wav"
            # 用 ffmpeg 精准切片（-ss/-t 比 -to 兼容性更好）
            cut_result = run_command(
                [
                    "ffmpeg", "-y",
                    "-i", str(wav_path),
                    "-ss", str(start_sec),
                    "-t", str(duration_sec),
                    "-ac", "1",
                    str(seg_path),
                ],
                timeout=60,
            )
            if cut_result.returncode != 0 or not seg_path.exists():
                warn(f"[SenseVoice] 片段 {idx} 切割失败，跳过")
                continue

            seg_result = transcribe_with_sensevoice(seg_path)
            if seg_result and seg_result.get("text"):
                segment_text = seg_result["text"].strip()
                all_texts.append(segment_text)
                transcript_segments.append(
                    {"start": start_sec, "end": end_sec, "text": segment_text}
                )
            else:
                warn(f"[SenseVoice] 片段 {idx}/{len(segments)} 转录为空，已跳过")

            # 立即释放片段文件，减小内存压力
            try:
                seg_path.unlink()
            except OSError:
                pass

    finally:
        # 清理临时目录
        try:
            if tmp_dir.exists():
                import shutil
                shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass

    if not all_texts:
        warn("[SenseVoice] 所有片段转录均为空")
        return None

    full_text = "\n".join(all_texts).strip()
    log(f"[SenseVoice] 分段拼接完成，总字符数: {len(full_text)}")
    return {
        "text": full_text,
        "segments": transcript_segments,
        "language": "zh",
        "model": f"sensevoice-{SENSEVOICE_MODEL_ID}",
        "initial_prompt": "",
    }

def transcribe_with_whisper(audio_path: Path, model_name: str, initial_prompt: str) -> dict[str, Any] | None:
    log(f"开始 Whisper 转录 (模型: {model_name})...")
    # 优先使用 faster-whisper（4-8x 加速，避免 CPU large-v3 崩溃）
    try:
        from faster_whisper import WhisperModel
        log("使用 faster-whisper（CPU 8 线程 int8）...")
        model = WhisperModel(model_name, device="cpu", compute_type="int8", cpu_threads=8)
        log("转录中，请耐心等待...")
        segments_iter, info = model.transcribe(
            str(audio_path),
            language="zh",
            initial_prompt=initial_prompt,
            beam_size=5,
            vad_filter=False,  # 关闭 VAD 避免误切
        )
        # 收集 segments
        segments_list = []
        full_text = []
        for seg in segments_iter:
            segments_list.append({"start": seg.start, "end": seg.end, "text": seg.text})
            full_text.append(seg.text.strip())
        return {
            "text": "\n".join(full_text).strip(),
            "segments": segments_list,
            "language": info.language,
            "model": f"faster-whisper-{model_name}",
            "initial_prompt": initial_prompt,
        }
    except ImportError:
        log("faster-whisper 未安装，降级到 openai-whisper...")
    except Exception as exc:
        log(f"faster-whisper 失败: {exc}，降级到 openai-whisper...")

    # 降级方案：openai-whisper
    try:
        import whisper
        log("加载 openai-whisper 模型...")
        model = whisper.load_model(model_name)
        log("转录中，请耐心等待...")
        result = model.transcribe(
            str(audio_path),
            language="zh",
            initial_prompt=initial_prompt,
            verbose=True,
        )
        return {
            "text": result.get("text", "").strip(),
            "segments": result.get("segments", []),
            "language": result.get("language", "zh"),
            "model": model_name,
            "initial_prompt": initial_prompt,
        }
    except ImportError:
        error("Whisper 未安装，请运行: pip3 install openai-whisper")
        return None
    except Exception as exc:
        error(f"模型 {model_name} 转录失败: {exc}")
        return None


def transcribe_with_stitch(
    audio_path: Path,
    preferred_model: str,
    initial_prompt: str,
) -> dict[str, Any] | None:
    """将长音频切成两半，分别用 SenseVoice 与 Whisper 转录后拼接。

    这是一个可执行的保守版 stitch：先用 SenseVoice 处理前半段，
    再用 faster-whisper/openai-whisper 处理后半段。任一引擎不可用时，
    该半段会尝试另一条链路；不依赖手工 CLI，也不会把两段结果静默丢失。
    """
    duration_seconds = get_audio_duration(audio_path) * 60
    if duration_seconds <= 0:
        warn("stitch 无法读取音频时长，回退到 Whisper 链路")
        return transcribe_with_whisper(audio_path, preferred_model, initial_prompt)

    # 短音频不需要跨引擎拼接，避免切分边界反而损失上下文。
    if duration_seconds < 20 * 60:
        log("stitch 音频短于 20 分钟，直接使用 Whisper 链路")
        return transcribe_with_whisper(audio_path, preferred_model, initial_prompt)

    midpoint = duration_seconds / 2
    tmp_dir = Path(tempfile.mkdtemp(prefix=f"_stitch_{audio_path.stem}_", dir=str(audio_path.parent)))
    first_path = tmp_dir / "part_1.wav"
    second_path = tmp_dir / "part_2.wav"

    try:
        for start, length, output in [
            (0, midpoint, first_path),
            (midpoint, duration_seconds - midpoint, second_path),
        ]:
            result = run_command(
                [
                    "ffmpeg", "-y", "-i", str(audio_path),
                    "-ss", str(start), "-t", str(length),
                    "-ar", "16000", "-ac", "1", str(output),
                ],
                timeout=int(os.environ.get("PREPROCESS_TIMEOUT", "1800")),
            )
            if result.returncode != 0 or not output.exists() or output.stat().st_size == 0:
                error(f"stitch 音频切分失败: {result.stderr.strip()[-500:]}")
                return None

        first = transcribe_with_sensevoice(first_path)
        if not first or not first.get("text"):
            warn("stitch 前半段 SenseVoice 失败，改用 Whisper")
            first = transcribe_with_whisper(first_path, preferred_model, initial_prompt)

        second = transcribe_with_whisper(second_path, preferred_model, initial_prompt)
        if not second or not second.get("text"):
            warn("stitch 后半段 Whisper 失败，改用 SenseVoice")
            second = transcribe_with_sensevoice(second_path)

        if not first or not first.get("text") or not second or not second.get("text"):
            return None

        segments = list(first.get("segments") or [])
        offset_segments = []
        for segment in second.get("segments") or []:
            adjusted = dict(segment)
            adjusted["start"] = float(adjusted.get("start", 0)) + midpoint
            adjusted["end"] = float(adjusted.get("end", 0)) + midpoint
            offset_segments.append(adjusted)
        segments.extend(offset_segments)

        return {
            "text": f"{first['text'].strip()}\n{second['text'].strip()}".strip(),
            "segments": segments,
            "language": second.get("language") or first.get("language") or "zh",
            "model": f"stitch({first.get('model', 'sensevoice')} + {second.get('model', 'whisper')})",
            "initial_prompt": initial_prompt,
        }
    finally:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)


def transcription_quality_issue(
    transcription: dict[str, Any],
    duration_minutes: float,
) -> str | None:
    text = clean_transcript_text(str(transcription.get("text") or ""))
    chars = len(text)
    if not text:
        return "转录正文为空"
    if duration_minutes >= 30:
        minimum_chars = round(duration_minutes * 15)
        if chars < minimum_chars:
            return (
                f"{duration_minutes:.1f} 分钟音频仅得到 {chars} 字，"
                f"低于完整性下限 {minimum_chars} 字"
            )
        segments = list(transcription.get("segments") or [])
        if segments:
            last_end = max(float(segment.get("end") or 0.0) for segment in segments)
            expected_seconds = duration_minutes * 60
            if last_end < expected_seconds * 0.5:
                return (
                    f"时间戳只覆盖到 {last_end:.1f} 秒，"
                    f"明显短于音频总长 {expected_seconds:.1f} 秒"
                )
    return None


def transcribe_with_fallback(
    audio_path: Path,
    preferred_model: str,
    initial_prompt: str,
    engine: str = "sensevoice",
    duration_minutes: float = 0.0,
) -> dict[str, Any] | None:
    """三层引擎路由：SenseVoice (主力) → faster-whisper → openai-whisper。

    engine 参数：
    - "sensevoice"（默认）：先尝试 SenseVoice-Small，失败后自动降级 Whisper
    - "whisper"：跳过 SenseVoice，直接走 Whisper 链路（纯英文/生僻技术内容推荐）
    - "stitch"：长音频前半段 SenseVoice、后半段 Whisper，失败时逐段互相回退

    通过环境变量 ASR_ENGINE=whisper 可在外部强制指定引擎。
    """
    if engine == "stitch":
        log("转录引擎：stitch（SenseVoice + Whisper 分段拼接）")
        stitched = transcribe_with_stitch(audio_path, preferred_model, initial_prompt)
        if stitched and stitched.get("text"):
            stitched["text"] = clean_transcript_text(stitched["text"])
            issue = transcription_quality_issue(stitched, duration_minutes)
            if not issue:
                return stitched
            warn(f"stitch 输出完整性检查未通过：{issue}")
        warn("stitch 未能产生有效输出，自动降级至 Whisper 备用链路")

    # ── 第一层：SenseVoice-Small（主力） ─────────────────────────────────────
    if engine == "sensevoice":
        log("转录引擎：SenseVoice-Small（主力）")
        sv_result = transcribe_sensevoice_with_vad(audio_path)
        if sv_result and sv_result.get("text"):
            # 对 SenseVoice 输出执行二次清洗，确保格式统一
            sv_result["text"] = clean_transcript_text(sv_result["text"])
            issue = transcription_quality_issue(sv_result, duration_minutes)
            if not issue:
                return sv_result
            warn(f"SenseVoice 输出完整性检查未通过：{issue}；自动降级 Whisper")
        warn("SenseVoice 未能产生有效输出，自动降级至 Whisper 备用链路")

    # ── 第二层 & 第三层：Whisper 备用链路（faster-whisper → openai-whisper） ─
    log("转录引擎：Whisper 备用链路")
    models = [preferred_model]
    for fallback in FALLBACK_MODELS:
        if fallback not in models:
            models.append(fallback)

    for model_name in models:
        result = transcribe_with_whisper(audio_path, model_name, initial_prompt)
        if result and result.get("text"):
            # 对 Whisper 输出也执行统一清洗（去多余空白、统一换行）
            result["text"] = clean_transcript_text(result["text"])
            issue = transcription_quality_issue(result, duration_minutes)
            if not issue:
                return result
            warn(f"Whisper {model_name} 输出完整性检查未通过：{issue}")
        if model_name != models[-1]:
            warn("将降级 Whisper 模型后重试")

    return None



def target_summary_words(duration_minutes: float) -> int:
    if duration_minutes < 30:
        return 1200
    if duration_minutes < 60:
        return 2000
    if duration_minutes < 90:
        return 3000
    return 4000


def safe_filename(title: str, fallback: str) -> str:
    safe = re.sub(r'[/\\:*?"<>|#&%()\[\]{}+=@!~`;,\']', "", title).strip()
    safe = re.sub(r"\s+", " ", safe)
    safe = safe[:80].strip()
    return safe or fallback


PLATFORM_FILENAME_LABELS = {
    "apple podcasts",
    "castbox",
    "castro",
    "iheartradio",
    "listennotes",
    "overcast",
    "pocket casts",
    "pocketcasts",
    "podbean",
    "podwise",
    "spotify",
    "xiaoyuzhou",
    "小宇宙",
}


def filename_episode_parts(info: dict[str, Any]) -> tuple[str, str]:
    """Return content names for filenames without a podcast platform label."""
    raw_show = str(info.get("show_title") or "").strip()
    raw_title = str(info.get("title") or "").strip()
    normalized_show = normalize_match_text(raw_show)
    platform_names = {
        normalize_match_text(label) for label in PLATFORM_FILENAME_LABELS
    }

    raw_title = re.sub(
        r"\s*(?:[-—–|]\s*)?小宇宙(?:\s*[-—–|]\s*听播客上小宇宙)?\s*$",
        "",
        raw_title,
        flags=re.IGNORECASE,
    ).strip()
    if normalized_show in platform_names:
        raw_show = ""
        for separator in (" — ", " – ", " - "):
            if separator not in raw_title:
                continue
            episode_candidate, show_candidate = raw_title.rsplit(separator, 1)
            if episode_candidate.strip() and show_candidate.strip():
                raw_title = episode_candidate.strip()
                raw_show = show_candidate.strip()
                break

    platform_prefix = "|".join(
        re.escape(label) for label in sorted(PLATFORM_FILENAME_LABELS, key=len, reverse=True)
    )
    raw_title = re.sub(
        rf"^(?:{platform_prefix})[\s_：:\-—–|]+",
        "",
        raw_title,
        flags=re.IGNORECASE,
    ).strip()
    return raw_show, raw_title


def build_agent_instruction(
    transcript_path: Path,
    segments_path: Path,
    srt_path: Path,
    vtt_path: Path,
    metadata_path: Path,
    knowledge_path: Path,
    personal_notes_path: Path,
    chunk_command: str,
    info: dict[str, Any],
    transcript_chars: int,
    output_dir: Path,
    combined_name: str,
    job_id: str | None = None,
    job_status_path: Path | None = None,
    job_result_path: Path | None = None,
) -> str:
    duration = info.get("duration_minutes", 0.0)
    target_words = target_summary_words(duration)
    guests_text = ", ".join(info.get("guests") or []) or "未自动识别"
    report_path = output_dir / "总结稿" / f"{combined_name}_详细总结.md"
    workflow_path = Path(__file__).with_name("references") / "report-workflow.md"
    knowledge_workflow_path = Path(__file__).with_name("references") / "knowledge-workflow.md"
    job_lines = ""
    verification_step = ""
    if job_id:
        job_lines = (
            f"- 作业 ID：{job_id}\n"
            f"- 作业状态：{job_status_path}\n"
            f"- 作业结果：{job_result_path}\n"
        )
        verification_step = (
            "9. 总结稿写入后执行完整性核验；只有该命令成功，作业才从 "
            "`awaiting_report` 变为 `completed`：\n"
            f'   "{sys.executable}" "{Path(__file__).resolve()}" '
            f'--output-dir "{output_dir}" --verify "{job_id}" --require-report\n'
        )
    return f"""请按 SKILL.md 和报告工作流继续完成播客总结。

输入文件：
- 转录稿：{transcript_path}
- 时间戳分段：{segments_path}
- SRT 字幕：{srt_path}
- WebVTT 字幕：{vtt_path}
- 元数据：{metadata_path}
- 报告工作流：{workflow_path}
- 知识与证据工作流：{knowledge_workflow_path}
- 结构化知识：{knowledge_path}
- 我的笔记（只读，不得覆盖）：{personal_notes_path}
{f"- Show Notes Markdown：{info.get('shownotes_archive', {}).get('markdown_path')}" if info.get('shownotes_archive') else ""}
{f"- Show Notes 图片/链接 Manifest：{info.get('shownotes_archive', {}).get('manifest_path')}" if info.get('shownotes_archive') else ""}
{f"- Podcasting 2.0 章节：{info.get('chapters_archive', {}).get('path')}" if info.get('chapters_archive', {}).get('ok') else ""}
{job_lines}

播客信息：
- 节目名称：{info.get('show_title', '未知节目')}
- 标题：{info['title']}
- 链接：{info['url']}
- 来源：{info.get('source', 'unknown')}
- 音频时长：{duration:.1f} 分钟
- 转录字数：{transcript_chars}
- 兜底总结字数：{target_words} 字以上（此为防敷衍兜底线，只能多不能少；仅算正文，不含 Show Notes）
- 嘉宾/说话人候选：{guests_text}

执行要求：
1. 读取转录稿和元数据。
2. 若转录稿超过 30000 字，先运行分块辅助工具：
   {chunk_command}
3. 长转录稿逐块独立提取证据、实体、引述和时间戳；合并去重后只做一次正式整合，禁止每块重写整篇总结。
4. 引述必须与转录稿一致，并尽量附时间戳。没有说话人证据时写“说话人未确认”，禁止猜测姓名。
5. 按“报告工作流”文件生成报告；保留 Show Notes，并在「转录稿」章节介绍独立转录文件及其链接，禁止把完整转录正文复制进总结稿。
6. 按“知识与证据工作流”把 `{knowledge_path}` 从草稿更新为 `complete`；每条关键洞察必须有可在 transcript/segments 中核验的引述或明确标注的转述。不得修改或覆盖 `{personal_notes_path}`。
7. 仅对 60 分钟以上、高风险主题或用户明确要求的深度版执行独立质检。
8. 提交前确认：
   - [ ] 包含「📋 基本信息」表格，所有字段已填写
   - [ ] 标题后写有「转录总结日期：YYYY-MM-DD」，使用完成总结的本地日期而非节目发布日期
   - [ ] 核心观点包含具体证据、案例、数字或机制
   - [ ] 引述已核对原文，且未猜测说话人
   - [ ] 包含背景与术语、实用资源、延伸思考与局限
   - [ ] 正文字数 ≥ {target_words} 字（不含 Show Notes）
   - [ ] 包含原始 Show Notes
   - [ ] 包含「关键洞察与证据」，且与 knowledge.json 一致
   - [ ] knowledge.json 状态为 complete，并通过引述与时间戳核验
   - [ ] 我的笔记.md 未被覆盖
   - [ ] 包含独立转录稿、segments、SRT、WebVTT 的相对链接，且未嵌入完整转录正文

   写入最终目标文件：
   {report_path}
{verification_step}
"""


def format_srt_timestamp(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def format_vtt_timestamp(seconds: float) -> str:
    return format_srt_timestamp(seconds).replace(",", ".")


def format_transcript_timestamp(seconds: float) -> str:
    total_seconds = max(0, round(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def relative_output_path(target: str | Path | None, start: Path) -> str:
    if not target:
        return "未生成"
    try:
        return os.path.relpath(Path(target), start=start)
    except (OSError, TypeError, ValueError):
        return str(target)


def relative_markdown_link(label: str, target: str | Path | None, start: Path) -> str:
    relative = relative_output_path(target, start)
    if relative == "未生成":
        return relative
    return f"[{label}](<{relative}>)"


def render_transcript_document(
    info: dict[str, Any],
    transcription: dict[str, Any],
    transcript_path: Path,
    segments_path: Path,
    srt_path: Path,
    vtt_path: Path,
    metadata_path: Path,
) -> str:
    segments = list(transcription.get("segments") or [])
    raw_text = clean_transcript_text(str(transcription.get("text") or ""))
    duration = float(info.get("duration_minutes") or 0.0)
    archive = info.get("shownotes_archive") or {}
    generated_at = time.strftime("%Y-%m-%d %H:%M:%S")
    transcript_dir = transcript_path.parent

    lines = [
        "# 播客转录稿",
        "",
        f"- 节目：{info.get('show_title') or '未知节目'}",
        f"- 单集：{info.get('title') or '未知单集'}",
        f"- 原始链接：{info.get('url') or '未获取'}",
        f"- 发布日期：{info.get('pub_date') or '未获取'}",
        f"- 音频时长：{duration:.1f} 分钟" if duration else "- 音频时长：未获取",
        f"- 转录引擎：{transcription.get('model') or '未知'}",
        f"- 语言：{transcription.get('language') or 'unknown'}",
        f"- 生成时间：{generated_at}",
        "",
        (
            "> 本文件优先采用播客发布方提供的转录；仍可能包含编辑、断句或时间戳误差。"
            "没有可靠说话人证据时不推断姓名。"
            if transcription.get("source") == "publisher_transcript"
            else "> 本文件由自动语音识别生成，可能包含人名、专有名词和断句错误。"
            "时间戳来自 ASR/VAD 分段；没有可靠说话人证据时不推断姓名。"
        ),
        "",
        "---",
        "",
        "## 转录正文",
        "",
    ]

    if segments:
        for segment in segments:
            text = clean_transcript_text(str(segment.get("text") or ""))
            if not text:
                continue
            start = format_transcript_timestamp(float(segment.get("start") or 0.0))
            end = format_transcript_timestamp(
                float(segment.get("end") or segment.get("start") or 0.0)
            )
            speaker = str(segment.get("speaker") or "").strip()
            label = f" [{speaker}]" if speaker else ""
            lines.extend([f"[{start} - {end}]{label}", text, ""])
    elif raw_text:
        lines.extend([raw_text, ""])
    else:
        lines.extend(["（未识别到转录正文）", ""])

    lines.extend(
        [
            "---",
            "",
            "## 附件与来源",
            "",
            f"- 原始页面：[打开单集]({info.get('url')})"
            if info.get("url")
            else "- 原始页面：未获取",
            f"- 时间戳分段：{relative_markdown_link('打开 JSON', segments_path, transcript_dir)}",
            f"- SRT 字幕：{relative_markdown_link('打开 SRT', srt_path, transcript_dir)}",
            f"- WebVTT 字幕：{relative_markdown_link('打开 VTT', vtt_path, transcript_dir)}",
            f"- 元数据：{relative_markdown_link('打开 JSON', metadata_path, transcript_dir)}",
            f"- Show Notes：{relative_markdown_link('打开 Markdown', archive.get('markdown_path'), transcript_dir)}",
            f"- 媒体清单：{relative_markdown_link('打开 JSON', archive.get('manifest_path'), transcript_dir)}",
            f"- Podcasting 2.0 章节：{relative_markdown_link('打开 JSON', (info.get('chapters_archive') or {}).get('path'), transcript_dir)}",
            "",
            "--- 转录稿结束 ---",
            "",
        ]
    )
    return "\n".join(lines)


def extract_transcript_body(document: str) -> str:
    start_marker = "## 转录正文"
    end_marker = "\n---\n\n## 附件与来源"
    if start_marker not in document:
        return document.strip()
    body = document.split(start_marker, 1)[1]
    if end_marker in body:
        body = body.split(end_marker, 1)[0]
    timestamp_line = re.compile(
        r"^\[\d{2}:\d{2}:\d{2} - \d{2}:\d{2}:\d{2}\](?: \[[^\]]+\])?$"
    )
    return "\n".join(
        line for line in body.strip().splitlines() if not timestamp_line.match(line.strip())
    ).strip()


def write_transcript_segments(
    segments_path: Path,
    srt_path: Path,
    vtt_path: Path,
    segments: list[dict[str, Any]],
) -> None:
    normalized: list[dict[str, Any]] = []
    srt_blocks: list[str] = []
    vtt_blocks: list[str] = []
    for index, segment in enumerate(segments, start=1):
        text = clean_transcript_text(str(segment.get("text") or ""))
        if not text:
            continue
        start = max(0.0, float(segment.get("start") or 0.0))
        end = max(start, float(segment.get("end") or start))
        normalized_segment = {"start": start, "end": end, "text": text}
        speaker = str(segment.get("speaker") or "").strip()
        if speaker:
            normalized_segment["speaker"] = speaker
        normalized.append(normalized_segment)
        srt_text = f"[{speaker}] {text}" if speaker else text
        srt_blocks.append(
            f"{len(normalized)}\n{format_srt_timestamp(start)} --> {format_srt_timestamp(end)}\n{srt_text}"
        )
        vtt_text = html.escape(text, quote=False)
        if speaker:
            voice = re.sub(r"[<>\r\n]+", " ", speaker).strip()
            vtt_text = f"<v {voice}>{vtt_text}"
        vtt_blocks.append(
            f"{format_vtt_timestamp(start)} --> {format_vtt_timestamp(end)}\n{vtt_text}"
        )
    atomic_write_text(
        segments_path,
        json.dumps(normalized, ensure_ascii=False, indent=2) + "\n",
    )
    atomic_write_text(srt_path, "\n\n".join(srt_blocks) + ("\n" if srt_blocks else ""))
    vtt_body = "\n\n".join(vtt_blocks)
    atomic_write_text(vtt_path, "WEBVTT\n\n" + vtt_body + ("\n" if vtt_body else ""))


def write_metadata(
    path: Path,
    info: dict[str, Any],
    transcription: dict[str, Any],
    transcript_path: Path,
    segments_path: Path,
    srt_path: Path,
    vtt_path: Path,
) -> None:
    metadata = {
        "episode": info,
        "transcription": {
            "model": transcription.get("model"),
            "source": transcription.get("source") or "asr",
            "source_url": transcription.get("source_url"),
            "source_path": transcription.get("source_path"),
            "language": transcription.get("language"),
            "initial_prompt": transcription.get("initial_prompt"),
            "transcript_path": str(transcript_path),
            "segments_path": str(segments_path),
            "srt_path": str(srt_path),
            "vtt_path": str(vtt_path),
            "chapters_path": (info.get("chapters_archive") or {}).get("path"),
            "segment_count": len(transcription.get("segments") or []),
            "transcript_chars": len(transcription.get("text", "")),
            "diarization": transcription.get("diarization"),
            "processed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        },
    }
    atomic_write_text(
        path,
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
    )


def atomic_write_text(path: Path, content: str) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp_path.write_text(content, encoding="utf-8")
    os.replace(temp_path, path)


def escape_markdown_table_cell(value: Any) -> str:
    return str(value or "未获取").replace("|", "\\|").replace("\r", " ").replace("\n", " ").strip()


def parse_index_timestamp(value: Any) -> float:
    raw = str(value or "").strip()
    if not raw:
        return 0.0
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def format_index_timestamp(timestamp: float) -> str:
    if timestamp <= 0:
        return "-"
    return datetime.fromtimestamp(timestamp).astimezone().strftime("%Y-%m-%d %H:%M")


def report_completion_times(output_dir: Path) -> dict[str, float]:
    completed: dict[str, float] = {}
    jobs_dir = output_dir / ".jobs"
    for result_path in jobs_dir.glob("*/result.json") if jobs_dir.is_dir() else []:
        try:
            result = read_json_object(result_path)
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        report_path = result.get("report_path")
        timestamp = parse_index_timestamp(result.get("report_verified_at"))
        if not report_path or timestamp <= 0:
            continue
        key = str(Path(str(report_path)).expanduser().resolve())
        completed[key] = max(timestamp, completed.get(key, 0.0))
    return completed


def human_index_status(transcript_path: Path, report_path: Path, mode: str) -> str:
    transcript_exists = transcript_path.is_file()
    report_exists = report_path.is_file()
    if transcript_exists and report_exists:
        return "已完成"
    if transcript_exists:
        return "待总结"
    if mode == "archive-only":
        return "仅归档"
    return "资料不完整"


def rebuild_human_index(output_dir: Path) -> Path:
    """Build the single human-facing catalog from per-episode metadata packages."""
    output_dir = output_dir.expanduser()
    package_root = output_dir / "资料"
    completion_times = report_completion_times(output_dir)
    rows: list[dict[str, str]] = []
    for metadata_path in package_root.glob("*/metadata.json") if package_root.is_dir() else []:
        try:
            payload = read_json_object(metadata_path)
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        episode = payload.get("episode") or {}
        if not isinstance(episode, dict):
            continue
        package_name = metadata_path.parent.name
        transcript_path = output_dir / "转录稿" / f"{package_name}_转录稿.txt"
        report_path = output_dir / "总结稿" / f"{package_name}_详细总结.md"
        knowledge_path = metadata_path.parent / "knowledge.json"
        personal_notes_path = metadata_path.parent / PERSONAL_NOTES_FILENAME
        mode = str(payload.get("mode") or "transcribe")
        transcription = payload.get("transcription") or {}
        transcript_time = parse_index_timestamp(
            transcription.get("processed_at") if isinstance(transcription, dict) else None
        )
        if transcript_time <= 0 and transcript_path.is_file():
            transcript_time = transcript_path.stat().st_mtime
        report_time = completion_times.get(str(report_path.resolve()), 0.0)
        if report_time <= 0 and report_path.is_file():
            report_time = report_path.stat().st_mtime
        rows.append(
            {
                "sort_group": "1" if report_time > 0 else "0",
                "sort_time": str(report_time or transcript_time or metadata_path.stat().st_mtime),
                "report_date": format_index_timestamp(report_time),
                "transcript_date": format_index_timestamp(transcript_time),
                "show": escape_markdown_table_cell(episode.get("show_title") or "未知节目"),
                "title": escape_markdown_table_cell(episode.get("title") or package_name),
                "status": human_index_status(transcript_path, report_path, mode),
                "url": str(episode.get("url") or "").strip(),
                "transcript": relative_output_path(transcript_path, output_dir)
                if transcript_path.is_file()
                else "",
                "report": relative_output_path(report_path, output_dir)
                if report_path.is_file()
                else "",
                "metadata": relative_output_path(metadata_path, output_dir),
                "knowledge": relative_output_path(knowledge_path, output_dir)
                if knowledge_path.is_file()
                else "",
                "personal_notes": relative_output_path(personal_notes_path, output_dir)
                if personal_notes_path.is_file()
                else "",
            }
        )

    rows.sort(
        key=lambda row: (
            int(row["sort_group"]),
            float(row["sort_time"]),
            row["show"],
            row["title"],
        ),
        reverse=True,
    )
    completed = sum(row["status"] == "已完成" for row in rows)
    pending = sum(row["status"] == "待总结" for row in rows)
    archived = sum(row["status"] == "仅归档" for row in rows)
    incomplete = sum(row["status"] == "资料不完整" for row in rows)
    lines = [
        "# 播客资料索引",
        "",
        "> 这是自动生成的人类阅读入口。可直接打开总结稿或转录稿；字幕、图片、Show Notes 和机器数据统一收在每期资料包中。",
        "",
        f"共 {len(rows)} 期：已完成 {completed}，待总结 {pending}，仅归档 {archived}，资料不完整 {incomplete}。",
        "",
        "| 总结日期 | 转录日期 | 节目 | 单集 | 状态 | 阅读与资料 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        links: list[str] = []
        if row["report"]:
            links.append(f"[总结稿](<{row['report']}>)")
        if row["transcript"]:
            links.append(f"[转录稿](<{row['transcript']}>)")
        if row["knowledge"]:
            links.append(f"[知识](<{row['knowledge']}>)")
        if row["personal_notes"]:
            links.append(f"[我的笔记](<{row['personal_notes']}>)")
        links.append(f"[资料](<{row['metadata']}>)")
        if row["url"]:
            links.append(f"[原始页面]({row['url']})")
        lines.append(
            f"| {row['report_date']} | {row['transcript_date']} | {row['show']} | {row['title']} | {row['status']} | {' · '.join(links)} |"
        )
    if not rows:
        lines.append("| - | - | - | 暂无资料 | - | - |")

    index_path = output_dir / HUMAN_INDEX_FILENAME
    atomic_write_text(index_path, "\n".join(lines) + "\n")
    return index_path


def read_json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


JOB_PHASE_PROGRESS = {
    "created": 0,
    "resolving": 10,
    "archiving": 25,
    "acquiring_transcript": 40,
    "downloading_audio": 50,
    "transcribing": 70,
    "writing_outputs": 90,
    "awaiting_report": 95,
    "verifying": 98,
    "completed": 100,
    "failed": 100,
}


class JobTracker:
    def __init__(self, job_dir: Path, state: dict[str, Any], *, resumed: bool = False):
        self.job_dir = job_dir
        self.job_path = job_dir / "job.json"
        self.status_path = job_dir / "status.json"
        self.result_path = job_dir / "result.json"
        self.state = state
        self.resumed = resumed

    @classmethod
    def create(
        cls,
        output_dir: Path,
        user_input: str,
        args: argparse.Namespace,
    ) -> "JobTracker":
        jobs_dir = output_dir / ".jobs"
        jobs_dir.mkdir(parents=True, exist_ok=True)
        requested_id = str(getattr(args, "job_id", "") or "").strip()
        if requested_id and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", requested_id):
            raise ValueError("--job-id only accepts letters, digits, dot, underscore and hyphen")
        digest = hashlib.sha256(user_input.encode("utf-8")).hexdigest()[:8]
        job_id = requested_id or f"{time.strftime('%Y%m%d-%H%M%S')}-{digest}"
        job_dir = jobs_dir / job_id
        if job_dir.exists():
            raise FileExistsError(f"job already exists; use --resume {job_id}")
        job_dir.mkdir(parents=True)
        mode = "archive-only" if getattr(args, "archive_only", False) else "transcribe"
        now = iso_timestamp()
        state: dict[str, Any] = {
            "schema_version": 1,
            "job_id": job_id,
            "input": user_input,
            "mode": mode,
            "status": "running",
            "current_phase": "created",
            "progress": 0,
            "created_at": now,
            "updated_at": now,
            "report_status": "not_required" if mode == "archive-only" else "pending",
            "phases": [],
            "checkpoints": {},
            "options": {
                "engine": getattr(args, "engine", None),
                "model": getattr(args, "model", None),
                "keep_audio": bool(getattr(args, "keep_audio", False)),
                "force_transcribe": bool(getattr(args, "force_transcribe", False)),
                "shownotes_assets": getattr(args, "shownotes_assets", None),
                "link_snapshot": getattr(args, "link_snapshot", None),
                "sync_backend": getattr(args, "sync_backend", None),
                "sync_destination": getattr(args, "sync_destination", None),
                "public_base_url": getattr(args, "public_base_url", None),
                "sync_required": bool(getattr(args, "sync_required", False)),
                "diarize": bool(getattr(args, "diarize", False)),
            },
            "paths": {
                "job_dir": str(job_dir),
                "job_path": str(job_dir / "job.json"),
                "status_path": str(job_dir / "status.json"),
                "result_path": str(job_dir / "result.json"),
            },
            "error": None,
        }
        tracker = cls(job_dir, state)
        tracker.persist()
        return tracker

    @classmethod
    def resume(cls, output_dir: Path, job_id: str) -> "JobTracker":
        jobs_dir = output_dir / ".jobs"
        if job_id == "latest":
            candidates = sorted(
                jobs_dir.glob("*/job.json"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
            if not candidates:
                raise FileNotFoundError("no jobs found to resume")
            job_path = candidates[0]
        else:
            job_path = jobs_dir / job_id / "job.json"
        state = read_json_object(job_path)
        tracker = cls(job_path.parent, state, resumed=True)
        tracker.state["status"] = "running"
        tracker.state["error"] = None
        tracker.state["resumed_at"] = iso_timestamp()
        tracker.persist()
        return tracker

    @property
    def job_id(self) -> str:
        return str(self.state["job_id"])

    def status_summary(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": self.state.get("status"),
            "current_phase": self.state.get("current_phase"),
            "progress": self.state.get("progress", 0),
            "report_status": self.state.get("report_status"),
            "updated_at": self.state.get("updated_at"),
            "error": self.state.get("error"),
            "result_path": str(self.result_path) if self.result_path.exists() else None,
            "report_path": (self.state.get("paths") or {}).get("report_path"),
        }

    def persist(self) -> None:
        self.state["updated_at"] = iso_timestamp()
        atomic_write_text(
            self.job_path,
            json.dumps(self.state, ensure_ascii=False, indent=2) + "\n",
        )
        atomic_write_text(
            self.status_path,
            json.dumps(self.status_summary(), ensure_ascii=False, indent=2) + "\n",
        )

    def phase(self, name: str, detail: str | None = None) -> None:
        now = iso_timestamp()
        phases = self.state.setdefault("phases", [])
        for phase in reversed(phases):
            if phase.get("status") == "running":
                phase["status"] = "completed"
                phase["completed_at"] = now
                break
        phases.append(
            {
                "name": name,
                "status": "running",
                "started_at": now,
                **({"detail": detail} if detail else {}),
            }
        )
        self.state["status"] = "running"
        self.state["current_phase"] = name
        self.state["progress"] = JOB_PHASE_PROGRESS.get(name, self.state.get("progress", 0))
        self.persist()

    def checkpoint(self, name: str, value: Any) -> None:
        self.state.setdefault("checkpoints", {})[name] = value
        self.persist()

    def finish(self, payload: dict[str, Any], *, awaiting_report: bool) -> dict[str, Any]:
        now = iso_timestamp()
        for phase in reversed(self.state.setdefault("phases", [])):
            if phase.get("status") == "running":
                phase["status"] = "completed"
                phase["completed_at"] = now
                break
        status = "awaiting_report" if awaiting_report else "completed"
        enriched = {
            **payload,
            "job_id": self.job_id,
            "job_status": status,
            "job_path": str(self.job_path),
            "status_path": str(self.status_path),
            "result_path": str(self.result_path),
        }
        atomic_write_text(
            self.result_path,
            json.dumps(enriched, ensure_ascii=False, indent=2) + "\n",
        )
        self.state["status"] = status
        self.state["current_phase"] = status
        self.state["progress"] = JOB_PHASE_PROGRESS.get(status, 100)
        self.state["report_status"] = "pending" if awaiting_report else "not_required"
        self.state.setdefault("paths", {})["result_path"] = str(self.result_path)
        if enriched.get("report_path"):
            self.state["paths"]["report_path"] = enriched["report_path"]
        self.persist()
        return enriched

    def fail(self, reason: str) -> None:
        now = iso_timestamp()
        for phase in reversed(self.state.setdefault("phases", [])):
            if phase.get("status") == "running":
                phase["status"] = "failed"
                phase["failed_at"] = now
                phase["error"] = reason
                break
        self.state["status"] = "failed"
        self.state["current_phase"] = "failed"
        self.state["progress"] = 100
        self.state["error"] = reason
        self.persist()

    def mark_report_complete(self) -> None:
        self.state["status"] = "completed"
        self.state["current_phase"] = "completed"
        self.state["progress"] = 100
        self.state["report_status"] = "verified"
        self.state["error"] = None
        self.state["completed_at"] = iso_timestamp()
        if self.result_path.is_file():
            result = read_json_object(self.result_path)
            result["job_status"] = "completed"
            result["report_verified_at"] = self.state["completed_at"]
            atomic_write_text(
                self.result_path,
                json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            )
        self.persist()


ACTIVE_JOB: JobTracker | None = None


def archive_checkpoint_is_usable(archive: Any, cover_url: str | None = None) -> bool:
    if not isinstance(archive, dict):
        return False
    required = ("raw_html_path", "markdown_path", "manifest_path")
    if not all(archive.get(key) and Path(archive[key]).is_file() for key in required):
        return False
    if not cover_url:
        return True
    try:
        manifest = read_json_object(Path(str(archive["manifest_path"])))
    except (OSError, json.JSONDecodeError, ValueError):
        return False
    normalized_cover = normalize_shownotes_url(cover_url)
    return any(
        image.get("role") == "cover" and image.get("source_url") == normalized_cover
        for image in manifest.get("images") or []
    )


def resolve_verify_target(output_dir: Path, target: str) -> tuple[Path, Path | None]:
    candidate = Path(target).expanduser()
    if candidate.is_dir():
        result_path = candidate / "result.json"
        job_path = candidate / "job.json"
        return result_path, job_path if job_path.is_file() else None
    if candidate.is_file():
        job_path = candidate.parent / "job.json" if candidate.name == "result.json" else None
        return candidate, job_path if job_path and job_path.is_file() else None
    job_dir = output_dir / ".jobs" / target
    return job_dir / "result.json", job_dir / "job.json"


def verify_result_artifacts(
    result: dict[str, Any],
    *,
    require_report: bool = False,
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    checks: list[dict[str, Any]] = []

    def check_file(name: str, raw_path: Any, *, required: bool = True) -> Path | None:
        if not raw_path:
            if required:
                errors.append(f"missing path: {name}")
            return None
        path = Path(str(raw_path)).expanduser()
        ok = path.is_file()
        checks.append({"name": name, "path": str(path), "ok": ok})
        if required and not ok:
            errors.append(f"missing file: {name}: {path}")
        return path if ok else None

    mode = result.get("mode")
    metadata_path = check_file("metadata", result.get("metadata_path"))
    transcript_path = None
    if mode == "transcribe":
        transcript_path = check_file("transcript", result.get("transcript_path"))
        segments_path = check_file("segments", result.get("segments_path"))
        check_file("srt", result.get("srt_path"))
        vtt_path = check_file("vtt", result.get("vtt_path"))
        check_file("instruction", result.get("instruction_path"))
        if segments_path:
            try:
                if not isinstance(json.loads(segments_path.read_text(encoding="utf-8")), list):
                    errors.append(f"segments JSON is not an array: {segments_path}")
            except (OSError, json.JSONDecodeError) as exc:
                errors.append(f"segments JSON is invalid: {segments_path}: {exc}")
        if vtt_path and not vtt_path.read_text(encoding="utf-8").startswith("WEBVTT"):
            errors.append(f"WebVTT header is missing: {vtt_path}")

    metadata_payload: dict[str, Any] = {}
    if metadata_path:
        try:
            metadata_payload = read_json_object(metadata_path)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            errors.append(f"metadata JSON is invalid: {metadata_path}: {exc}")

    knowledge_path = None
    raw_knowledge_path = result.get("knowledge_path")
    if mode == "transcribe" and raw_knowledge_path:
        knowledge_path = check_file("knowledge", raw_knowledge_path)
        check_file("personal_notes", result.get("personal_notes_path"))
        if knowledge_path:
            episode = metadata_payload.get("episode") or {}
            knowledge_verification = validate_knowledge(
                knowledge_path,
                transcript_path=transcript_path,
                segments_path=segments_path,
                duration_minutes=float(episode.get("duration_minutes") or 0.0)
                if isinstance(episode, dict)
                else 0.0,
                require_complete=require_report,
            )
            errors.extend(knowledge_verification.get("errors") or [])
            warnings.extend(knowledge_verification.get("warnings") or [])

    archive = result.get("shownotes_archive") or {}
    shownotes_online_urls: list[str] = []
    if archive:
        markdown_path = check_file("shownotes_markdown", archive.get("markdown_path"))
        check_file("shownotes_raw_html", archive.get("raw_html_path"))
        manifest_path = check_file("shownotes_manifest", archive.get("manifest_path"))
        if manifest_path:
            try:
                manifest = read_json_object(manifest_path)
                shownotes_online_urls = [
                    str(link.get("url"))
                    for link in manifest.get("links") or []
                    if isinstance(link, dict) and link.get("url")
                ]
                expected_cover = normalize_shownotes_url(
                    (metadata_payload.get("episode") or {}).get("cover_url")
                )
                if expected_cover and not any(
                    image.get("role") == "cover" and image.get("source_url") == expected_cover
                    for image in manifest.get("images") or []
                ):
                    errors.append(f"Show Notes manifest is missing episode cover: {expected_cover}")
                for image in manifest.get("images") or []:
                    if image.get("ok") and image.get("path"):
                        image_path = Path(str(image["path"])).expanduser()
                        if not image_path.is_file():
                            errors.append(f"archived image is missing: {image_path}")
                    elif image.get("error"):
                        warnings.append(
                            f"image unavailable, online URL retained: {image.get('source_url')}: {image.get('error')}"
                        )
                for link in manifest.get("links") or []:
                    snapshot = link.get("snapshot") or {}
                    if snapshot.get("status") == "complete" and snapshot.get("path"):
                        snapshot_path = Path(str(snapshot["path"])).expanduser()
                        if not snapshot_path.exists():
                            errors.append(f"link snapshot is missing: {snapshot_path}")
                    elif snapshot.get("status") == "failed":
                        warnings.append(
                            f"link snapshot failed, online URL retained: {link.get('url')}: {snapshot.get('reason')}"
                        )
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                errors.append(f"Show Notes manifest is invalid: {manifest_path}: {exc}")
        if markdown_path:
            markdown = markdown_path.read_text(encoding="utf-8")
            if shownotes_online_urls and LINK_ARCHIVE_START not in markdown:
                errors.append("Show Notes is missing the human-readable link archive")
            for online_url in shownotes_online_urls:
                escaped_url = markdown_escape_url(online_url)
                if online_url not in markdown and escaped_url not in markdown:
                    errors.append(f"Show Notes online link is missing: {online_url}")
            for raw_link in re.findall(r"!\[[^\]]*\]\((?:<)?([^)>]+)(?:>)?\)", markdown):
                if urlparse(raw_link).scheme in {"http", "https"}:
                    continue
                image_path = (markdown_path.parent / raw_link).resolve()
                if not image_path.is_file():
                    errors.append(f"Show Notes image link is broken: {raw_link}")

    raw_chapters_path = result.get("chapters_path")
    chapters_path = check_file(
        "chapters",
        raw_chapters_path,
        required=bool(raw_chapters_path),
    )
    if chapters_path:
        try:
            chapters_payload = read_json_object(chapters_path)
            if not isinstance(chapters_payload.get("chapters"), list):
                errors.append(f"chapters JSON has no chapters array: {chapters_path}")
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            errors.append(f"chapters JSON is invalid: {chapters_path}: {exc}")

    report_path = None
    if mode == "transcribe":
        report_path = check_file("report", result.get("report_path"), required=False)
        if not report_path:
            message = f"report is not generated yet: {result.get('report_path') or 'unknown path'}"
            if require_report:
                errors.append(message)
            else:
                warnings.append(message)
        else:
            report = report_path.read_text(encoding="utf-8")
            if require_report and raw_knowledge_path and not re.search(
                r"(?m)^>\s*转录总结日期[：:]\s*\d{4}-\d{2}-\d{2}\s*$",
                report[:2000],
            ):
                errors.append("report is missing transcript summary date")
            if require_report and raw_knowledge_path and "关键洞察与证据" not in report:
                errors.append("report is missing section: 关键洞察与证据")
            if require_report:
                for online_url in shownotes_online_urls:
                    escaped_url = markdown_escape_url(online_url)
                    if online_url not in report and escaped_url not in report:
                        errors.append(f"report is missing Show Notes link: {online_url}")
            for raw_link in re.findall(r"\[[^\]]+\]\((?:<)?([^)>]+)(?:>)?\)", report):
                parsed = urlparse(raw_link)
                if parsed.scheme or raw_link.startswith("#"):
                    continue
                linked_path = (report_path.parent / raw_link).resolve()
                if not linked_path.exists():
                    errors.append(f"report link is broken: {raw_link}")

    return {
        "ok": not errors,
        "mode": mode,
        "job_id": result.get("job_id"),
        "report_present": bool(report_path),
        "checks": checks,
        "errors": errors,
        "warnings": warnings,
        "verified_at": iso_timestamp(),
    }


def run_verify(output_dir: Path, target: str, *, require_report: bool) -> int:
    result_path, job_path = resolve_verify_target(output_dir, target)
    if not result_path.is_file():
        payload = {
            "ok": False,
            "errors": [f"result.json not found: {result_path}"],
            "status_path": str(job_path.parent / "status.json") if job_path else None,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1
    try:
        result = read_json_object(result_path)
        verification = verify_result_artifacts(result, require_report=require_report)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(json.dumps({"ok": False, "errors": [str(exc)]}, ensure_ascii=False, indent=2))
        return 1

    verification_path = result_path.with_name("verification.json")
    atomic_write_text(
        verification_path,
        json.dumps(verification, ensure_ascii=False, indent=2) + "\n",
    )
    verification["verification_path"] = str(verification_path)
    if verification["ok"] and verification["report_present"] and job_path and job_path.is_file():
        tracker = JobTracker(job_path.parent, read_json_object(job_path), resumed=True)
        tracker.mark_report_complete()
    if verification["ok"]:
        verification["knowledge_index"] = rebuild_knowledge_index(output_dir)
        verification["index_path"] = str(rebuild_human_index(output_dir))
    print(json.dumps(verification, ensure_ascii=False, indent=2))
    return 0 if verification["ok"] else 1


def parse_cli_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="解析、归档并转录播客链接、课程音视频、RSS、媒体页面或节目搜索词。"
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "input",
        nargs="*",
        help="单集/课程链接、本地音视频文件、带标题链接或搜索关键词",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--resolve-only",
        action="store_true",
        help="只解析并输出单集元数据，不下载或写入归档",
    )
    mode.add_argument(
        "--archive-only",
        action="store_true",
        help="只解析和归档 Show Notes，不下载音频或转录",
    )
    mode.add_argument(
        "--rebuild-index",
        action="store_true",
        help="根据现有资料包重建根目录的播客索引，不解析或转录",
    )
    mode.add_argument(
        "--rebuild-knowledge-index",
        action="store_true",
        help="为现有资料包补齐知识/笔记模板并重建本地知识索引",
    )
    mode.add_argument(
        "--init-subscriptions",
        action="store_true",
        help="创建低成本 RSS 订阅发现配置，不下载音频",
    )
    mode.add_argument(
        "--scan-subscriptions",
        action="store_true",
        help="扫描订阅并生成去重评分 Brief，不启动转录",
    )
    mode.add_argument(
        "--export",
        choices=("all", "obsidian", "notion", "zotero", "notebooklm", "mcp"),
        help="从本地知识索引导出可重建的 PKM/Agent 文件",
    )
    parser.add_argument(
        "--force-transcribe",
        action="store_true",
        help="忽略已有转录产物，重新下载和转录",
    )
    parser.add_argument("--output-dir", help="覆盖输出目录")
    parser.add_argument("--export-dir", help="覆盖知识库导出目录")
    parser.add_argument(
        "--engine",
        choices=("sensevoice", "whisper", "stitch"),
        help="覆盖 ASR_ENGINE",
    )
    parser.add_argument("--model", help="覆盖 WHISPER_MODEL")
    parser.add_argument("--keep-audio", action="store_true", help="保留音频和 WAV")
    parser.add_argument(
        "--shownotes-assets",
        choices=tuple(sorted(SHOWNOTES_ASSET_MODES)),
        help="覆盖 SHOWNOTES_ASSETS",
    )
    parser.add_argument(
        "--link-snapshot",
        choices=tuple(sorted(SHOWNOTES_LINK_SNAPSHOT_MODES)),
        help="可选地用 SingleFile 或 ArchiveBox 保存 Show Notes 外链页面",
    )
    parser.add_argument(
        "--sync-backend",
        choices=("local", "webdav", "s3"),
        help="同步 Show Notes 到本地同步盘、WebDAV 或 S3/R2",
    )
    parser.add_argument("--sync-destination", help="同步目录、WebDAV URL 或 s3:// 地址")
    parser.add_argument("--public-base-url", help="图片可公开访问时的 URL 基址")
    parser.add_argument(
        "--sync-required",
        action="store_true",
        help="同步失败时以非零状态退出，适合自动化任务",
    )
    parser.add_argument(
        "--diarize",
        action="store_true",
        help="使用 pyannote 为时间戳分段添加说话人标签",
    )
    parser.add_argument("--job-id", help="为新任务指定便于识别的 ID")
    parser.add_argument(
        "--resume",
        metavar="JOB_ID",
        help="恢复 output-dir/.jobs 下的任务；使用 latest 恢复最近任务",
    )
    parser.add_argument(
        "--verify",
        metavar="JOB_ID_OR_RESULT",
        help="核验 result.json 及其产物，不执行解析或转录",
    )
    parser.add_argument(
        "--require-report",
        action="store_true",
        help="核验时要求正式总结稿已经生成",
    )
    args = parser.parse_args()
    standalone_mode = any(
        (
            args.rebuild_index,
            args.rebuild_knowledge_index,
            args.init_subscriptions,
            args.scan_subscriptions,
            bool(args.export),
        )
    )
    if not args.input and not args.resume and not args.verify and not standalone_mode:
        parser.error("请提供播客输入，或使用索引、订阅、导出或核验命令")
    if args.verify and (args.input or args.resume):
        parser.error("--verify 不能与播客输入或 --resume 同时使用")
    if args.verify and args.archive_only:
        parser.error("--verify 不能与 --archive-only 同时使用")
    if args.resume and args.input:
        parser.error("--resume 不能与新的播客输入同时使用")
    if args.resume and args.job_id:
        parser.error("--resume 不能与 --job-id 同时使用")
    if args.resolve_only and (args.resume or args.verify):
        parser.error("--resolve-only 不能与 --resume / --verify 同时使用")
    if args.rebuild_index and (args.input or args.resume or args.verify):
        parser.error("--rebuild-index 不能与播客输入、--resume 或 --verify 同时使用")
    if standalone_mode and (args.input or args.resume or args.verify):
        parser.error("索引、订阅和导出命令不能与播客输入、--resume 或 --verify 同时使用")
    if args.require_report and not args.verify:
        parser.error("--require-report 只能与 --verify 同时使用")
    return args


def emit_result(payload: dict[str, Any], *, print_json: bool = False) -> None:
    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    result_json_path = os.environ.get("RESULT_JSON")
    if result_json_path:
        atomic_write_text(Path(result_json_path), serialized)
    if print_json:
        print(serialized, end="")


def sync_shownotes_if_configured(
    shownotes_archive: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not shownotes_archive:
        return None
    try:
        from media_store import sync_archive

        return sync_archive(shownotes_archive)
    except Exception as exc:
        warn(f"Show Notes 同步失败，本地归档仍可用: {exc}")
        if os.environ.get("SHOWNOTES_SYNC_REQUIRED", "0") == "1":
            raise
        return {"status": "failed", "reason": f"{type(exc).__name__}: {exc}"}


def diarize_if_configured(
    audio_path: Path,
    transcription: dict[str, Any],
) -> None:
    if os.environ.get("DIARIZATION", "0") != "1":
        return
    from diarize_segments import diarize

    segments, metadata = diarize(
        audio_path, list(transcription.get("segments") or [])
    )
    transcription["segments"] = segments
    transcription["diarization"] = metadata
    if metadata.get("status") == "complete":
        log(f"说话人识别完成: {metadata.get('speaker_count', 0)} 位说话人")
    else:
        warn(f"说话人识别未完成: {metadata.get('reason', metadata.get('status'))}")


def build_combined_name(info: dict[str, Any]) -> str:
    raw_show, raw_title = filename_episode_parts(info)
    show_name = safe_filename(raw_show, "")
    episode_title = safe_filename(raw_title or "未知单集", "未知单集")
    raw_pub_date = info.get("pub_date") or time.strftime("%Y%m%d")
    pub_date = re.sub(r"[^0-9]", "", str(raw_pub_date))[:8]
    if len(pub_date) < 8:
        pub_date = time.strftime("%Y%m%d")

    combined = "_".join(part for part in (show_name, episode_title, pub_date) if part)
    while len(combined.encode("utf-8")) > 200 and len(episode_title) > 5:
        episode_title = episode_title[:-1]
        combined = "_".join(part for part in (show_name, episode_title, pub_date) if part)
    return combined


def load_cached_transcription(
    transcript_path: Path,
    segments_path: Path,
    metadata_path: Path,
    expected_episode: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None] | None:
    if not transcript_path.exists() or transcript_path.stat().st_size == 0:
        return None
    transcript_document = transcript_path.read_text(encoding="utf-8").strip()
    if not transcript_document:
        return None

    segments: list[dict[str, Any]] = []
    if segments_path.exists():
        try:
            loaded_segments = json.loads(segments_path.read_text(encoding="utf-8"))
            if isinstance(loaded_segments, list):
                segments = loaded_segments
        except (OSError, json.JSONDecodeError):
            warn(f"已有时间戳文件不可读，将继续复用纯文本转录: {segments_path}")

    cached_metadata: dict[str, Any] | None = None
    transcription_metadata: dict[str, Any] = {}
    if metadata_path.exists():
        try:
            loaded_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if isinstance(loaded_metadata, dict):
                cached_metadata = loaded_metadata
                transcription_metadata = loaded_metadata.get("transcription") or {}
        except (OSError, json.JSONDecodeError):
            warn(f"已有 metadata 不可读，将继续复用转录稿: {metadata_path}")

    if cached_metadata is None:
        warn("已有转录稿缺少可验证 metadata，将重新转录")
        return None

    cached_episode = (cached_metadata or {}).get("episode") or {}
    cached_id = str(cached_episode.get("id") or "")
    expected_id = str(expected_episode.get("id") or "")
    cached_url = str(cached_episode.get("url") or "")
    expected_url = str(expected_episode.get("url") or "")
    identity_matches = (
        bool(cached_id and expected_id and cached_id == expected_id)
        or bool(cached_url and expected_url and cached_url == expected_url)
    )
    if cached_metadata and not identity_matches:
        warn("已有转录 metadata 与当前单集不匹配，将重新转录")
        return None

    transcript = (
        "\n".join(
            clean_transcript_text(str(segment.get("text") or ""))
            for segment in segments
            if clean_transcript_text(str(segment.get("text") or ""))
        )
        if segments
        else extract_transcript_body(transcript_document)
    )
    if not transcript:
        return None
    quality_issue = transcription_quality_issue(
        {"text": transcript, "segments": segments},
        float(cached_episode.get("duration_minutes") or 0.0),
    )
    if quality_issue:
        warn(f"已有转录稿完整性检查未通过，将重新转录：{quality_issue}")
        return None

    return (
        {
            "text": transcript,
            "segments": segments,
            "language": transcription_metadata.get("language") or "unknown",
            "model": transcription_metadata.get("model") or "cached-unknown",
            "source": transcription_metadata.get("source") or "cached",
            "source_url": transcription_metadata.get("source_url"),
            "source_path": transcription_metadata.get("source_path"),
            "initial_prompt": transcription_metadata.get("initial_prompt") or "",
            "diarization": transcription_metadata.get("diarization"),
        },
        cached_metadata,
    )


def main() -> None:
    global ACTIVE_JOB
    ACTIVE_JOB = None
    args = parse_cli_args()
    output_dir = Path(
        getattr(args, "output_dir", None)
        or os.environ.get("OUTPUT_DIR", str(DEFAULT_OUTPUT_DIR))
    ).expanduser()
    if getattr(args, "rebuild_index", False):
        index_path = rebuild_human_index(output_dir)
        emit_result({"mode": "rebuild-index", "index_path": str(index_path)}, print_json=True)
        return
    if getattr(args, "rebuild_knowledge_index", False):
        payload = rebuild_knowledge_index(output_dir)
        payload["mode"] = "rebuild-knowledge-index"
        payload["human_index_path"] = str(rebuild_human_index(output_dir))
        emit_result(payload, print_json=True)
        return
    if getattr(args, "init_subscriptions", False):
        from subscription_manager import initialize_subscriptions

        payload = initialize_subscriptions(output_dir)
        payload["mode"] = "init-subscriptions"
        emit_result(payload, print_json=True)
        return
    if getattr(args, "scan_subscriptions", False):
        from subscription_manager import scan_subscriptions

        payload = scan_subscriptions(output_dir, subscription_feed_entries)
        payload["mode"] = "scan-subscriptions"
        emit_result(payload, print_json=True)
        return
    if getattr(args, "export", None):
        from knowledge_export import export_library

        formats = None if args.export == "all" else {str(args.export)}
        payload = export_library(
            output_dir,
            formats=formats,
            export_dir=Path(args.export_dir).expanduser() if args.export_dir else None,
        )
        payload["mode"] = "export"
        emit_result(payload, print_json=True)
        return
    if getattr(args, "verify", None):
        raise SystemExit(
            run_verify(
                output_dir,
                str(args.verify),
                require_report=bool(getattr(args, "require_report", False)),
            )
        )

    tracker: JobTracker | None = None
    if getattr(args, "resume", None):
        tracker = JobTracker.resume(output_dir, str(args.resume))
        options = tracker.state.get("options") or {}
        if getattr(args, "engine", None) is None:
            args.engine = options.get("engine")
        if getattr(args, "model", None) is None:
            args.model = options.get("model")
        for name in ("keep_audio", "force_transcribe", "sync_required", "diarize"):
            if not bool(getattr(args, name, False)):
                setattr(args, name, bool(options.get(name, False)))
        for name in (
            "shownotes_assets",
            "link_snapshot",
            "sync_backend",
            "sync_destination",
            "public_base_url",
        ):
            if getattr(args, name, None) is None:
                setattr(args, name, options.get(name))
        args.archive_only = tracker.state.get("mode") == "archive-only"
        args.resolve_only = False
        user_input = str(tracker.state.get("input") or "").strip()
        if not user_input:
            raise ValueError(f"作业 {tracker.job_id} 缺少原始输入")
        log(f"恢复作业: {tracker.job_id}")
    else:
        user_input = " ".join(getattr(args, "input", [])).strip()
        if not getattr(args, "resolve_only", False):
            output_dir.mkdir(parents=True, exist_ok=True)
            tracker = JobTracker.create(output_dir, user_input, args)
            log(f"创建作业: {tracker.job_id}")
    ACTIVE_JOB = tracker

    asr_engine = (
        getattr(args, "engine", None) or os.environ.get("ASR_ENGINE", "sensevoice")
    ).lower().strip()
    if asr_engine not in ("sensevoice", "whisper", "stitch"):
        warn(f"ASR_ENGINE 值 '{asr_engine}' 无效，将使用默认值 'sensevoice'")
        asr_engine = "sensevoice"
    model = getattr(args, "model", None) or os.environ.get("WHISPER_MODEL", DEFAULT_MODEL)
    keep_audio = bool(getattr(args, "keep_audio", False)) or os.environ.get("KEEP_AUDIO", "0") == "1"
    force_transcribe = (
        bool(getattr(args, "force_transcribe", False))
        or os.environ.get("FORCE_TRANSCRIBE", "0") == "1"
    )
    if getattr(args, "shownotes_assets", None):
        os.environ["SHOWNOTES_ASSETS"] = args.shownotes_assets
    if getattr(args, "link_snapshot", None):
        os.environ["SHOWNOTES_LINK_SNAPSHOT"] = args.link_snapshot
    if getattr(args, "sync_backend", None):
        os.environ["SHOWNOTES_SYNC_BACKEND"] = args.sync_backend
    if getattr(args, "sync_destination", None):
        os.environ["SHOWNOTES_SYNC_DESTINATION"] = args.sync_destination
    if getattr(args, "public_base_url", None):
        os.environ["SHOWNOTES_PUBLIC_BASE_URL"] = args.public_base_url
    if getattr(args, "sync_required", False):
        os.environ["SHOWNOTES_SYNC_REQUIRED"] = "1"
    if getattr(args, "diarize", False):
        os.environ["DIARIZATION"] = "1"
    log(f"转录引擎: {asr_engine.upper()}{'（Whisper 备用模型: ' + model + '）' if asr_engine == 'sensevoice' else '（模型: ' + model + '）'}")

    log(f"解析输入: {user_input}")
    if tracker:
        tracker.phase("resolving")
    episode_checkpoint = (tracker.state.get("checkpoints") or {}).get("episode") if tracker else None
    if isinstance(episode_checkpoint, dict):
        info = dict(episode_checkpoint)
        log("复用作业检查点中的单集元数据")
    else:
        info = resolve_episode_info(user_input)
    if not info:
        error("无法解析播客输入；请换用单集链接、RSS 链接，或补充节目名 + 单集标题关键词")
        sys.exit(1)
    if tracker and not isinstance(episode_checkpoint, dict):
        tracker.checkpoint("episode", info)

    log(f"标题: {info['title']}")
    log(f"来源: {info.get('source', 'unknown')}")
    if info.get("guests"):
        log(f"嘉宾/说话人候选: {', '.join(info['guests'])}")

    if getattr(args, "resolve_only", False):
        emit_result({"mode": "resolve-only", "episode": info}, print_json=True)
        return

    output_dir.mkdir(parents=True, exist_ok=True)
    transcript_dir = output_dir / "转录稿"
    summary_dir = output_dir / "总结稿"
    audio_dir = output_dir / "音频"

    transcript_dir.mkdir(parents=True, exist_ok=True)
    summary_dir.mkdir(parents=True, exist_ok=True)

    combined_name = build_combined_name(info)

    episode_dir = output_dir / "资料" / combined_name
    transcript_data_dir = episode_dir / "转录数据"
    episode_dir.mkdir(parents=True, exist_ok=True)
    transcript_data_dir.mkdir(parents=True, exist_ok=True)

    transcript_path = transcript_dir / f"{combined_name}_转录稿.txt"
    segments_path = transcript_data_dir / "segments.json"
    srt_path = transcript_data_dir / "transcript.srt"
    vtt_path = transcript_data_dir / "transcript.vtt"
    metadata_path = episode_dir / "metadata.json"
    instruction_path = episode_dir / "Agent任务指令.txt"
    legacy_segments_path = transcript_dir / f"{combined_name}_segments.json"
    legacy_metadata_path = output_dir / f"{combined_name}_metadata.json"

    if tracker:
        tracker.phase("archiving")
    archive_checkpoint = (
        (tracker.state.get("checkpoints") or {}).get("shownotes_archive")
        if tracker
        else None
    )
    if archive_checkpoint_is_usable(archive_checkpoint, info.get("cover_url")):
        shownotes_archive = dict(archive_checkpoint)
        log("复用作业检查点中的 Show Notes 归档")
    else:
        shownotes_archive = archive_show_notes(info, output_dir, combined_name)
    if shownotes_archive:
        info["shownotes_archive"] = shownotes_archive
        log(f"   Show Notes: {shownotes_archive['markdown_path']}")
        log(
            f"   Show Notes 图片: {shownotes_archive['image_count']}，链接: {shownotes_archive['link_count']}，模式: {shownotes_archive['mode']}"
        )
        sync_result = shownotes_archive.get("sync") or sync_shownotes_if_configured(
            shownotes_archive
        )
        if sync_result:
            shownotes_archive["sync"] = sync_result
            if sync_result.get("status") != "failed":
                log(
                    f"   Show Notes 已同步: {sync_result['backend']}，{sync_result['file_count']} 个文件"
                )
    if tracker and not archive_checkpoint_is_usable(archive_checkpoint, info.get("cover_url")):
        tracker.checkpoint("shownotes_archive", shownotes_archive)

    chapter_checkpoint = (
        (tracker.state.get("checkpoints") or {}).get("chapters_archive")
        if tracker
        else None
    )
    if (
        isinstance(chapter_checkpoint, dict)
        and chapter_checkpoint.get("ok")
        and chapter_checkpoint.get("path")
        and Path(str(chapter_checkpoint["path"])).is_file()
    ):
        chapters_archive = dict(chapter_checkpoint)
        info["chapters_archive"] = chapters_archive
        log("复用作业检查点中的章节归档")
    else:
        chapters_archive = archive_episode_chapters(info, transcript_data_dir, combined_name)
        if tracker:
            tracker.checkpoint("chapters_archive", chapters_archive)
    if chapters_archive and chapters_archive.get("ok"):
        log(
            f"   章节: {chapters_archive['chapter_count']} 个 ({chapters_archive['path']})"
        )

    if getattr(args, "archive_only", False):
        atomic_write_text(
            metadata_path,
            json.dumps(
                {
                    "episode": info,
                    "mode": "archive-only",
                    "processed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )
        payload = {
            "mode": "archive-only",
            "episode": info,
            "episode_dir": str(episode_dir),
            "metadata_path": str(metadata_path),
            "shownotes_archive": shownotes_archive,
            "chapters_archive": chapters_archive,
            "chapters_path": (
                chapters_archive.get("path") if chapters_archive and chapters_archive.get("ok") else None
            ),
        }
        payload["index_path"] = str(rebuild_human_index(output_dir))
        if tracker:
            payload = tracker.finish(payload, awaiting_report=False)
        emit_result(payload, print_json=True)
        return

    parsed_url = urlparse(str(info.get("audio_url") or ""))
    ext = os.path.splitext(parsed_url.path)[1]
    if info.get("source") == "local_media" and info.get("media_kind") == "video":
        ext = ".m4a"
    if not ext or len(ext) > 5 or ext.lower() in {".m3u8", ".m3u"}:
        ext = ".m4a"

    audio_path = audio_dir / f"{combined_name}{ext}"
    wav_path = audio_dir / f"{combined_name}.wav"
    used_cached_transcript = False
    audio_created_this_run = False

    try:
        if tracker:
            tracker.phase("acquiring_transcript")
        cache_segments_path = segments_path if segments_path.exists() else legacy_segments_path
        cache_metadata_path = metadata_path if metadata_path.exists() else legacy_metadata_path
        cached = None if force_transcribe else load_cached_transcription(
            transcript_path, cache_segments_path, cache_metadata_path, info
        )
        cached_model = str(cached[0].get("model") or "") if cached else ""
        publisher_transcription = None
        if os.environ.get("PREFER_PUBLISHER_TRANSCRIPT", "1") != "0" and not cached_model.startswith(
            "publisher-transcript:"
        ):
            publisher_transcription = load_publisher_transcript(
                info,
                transcript_data_dir,
                combined_name,
            )

        if publisher_transcription:
            transcription = publisher_transcription
            log(
                "使用发布方提供的转录，跳过音频下载与 ASR: "
                f"{publisher_transcription.get('source_url')}"
            )
            has_speaker_labels = any(
                segment.get("speaker") for segment in transcription.get("segments") or []
            )
            if (
                os.environ.get("DIARIZATION", "0") == "1"
                and not has_speaker_labels
                and info.get("audio_url")
            ):
                log("发布方转录没有说话人标签，仅下载音频执行说话人识别")
                if tracker:
                    tracker.phase("downloading_audio", "为发布方转录补充说话人标签")
                if download_audio(str(info["audio_url"]), audio_path):
                    audio_created_this_run = True
                    diarization_path = audio_path
                    if preprocess_audio(audio_path, wav_path):
                        diarization_path = wav_path
                    diarize_if_configured(diarization_path, transcription)
        elif cached:
            transcription, cached_metadata = cached
            used_cached_transcript = True
            cached_episode = (cached_metadata or {}).get("episode") or {}
            if cached_episode.get("duration_minutes"):
                info["duration_minutes"] = cached_episode["duration_minutes"]
            log(f"复用已有转录稿: {transcript_path}")
            has_speaker_labels = any(
                segment.get("speaker") for segment in transcription.get("segments") or []
            )
            if os.environ.get("DIARIZATION", "0") == "1" and not has_speaker_labels:
                log("复用转录文本，仅下载音频执行说话人识别")
                if tracker:
                    tracker.phase("downloading_audio", "为缓存转录补充说话人标签")
                if info.get("audio_url") and download_audio(str(info["audio_url"]), audio_path):
                    audio_created_this_run = True
                    diarization_path = audio_path
                    if preprocess_audio(audio_path, wav_path):
                        diarization_path = wav_path
                    diarize_if_configured(diarization_path, transcription)
                else:
                    transcription["diarization"] = {
                        "status": "failed",
                        "reason": "audio download failed",
                    }
                    warn("音频下载失败，无法为缓存转录添加说话人标签")
        else:
            if not info.get("audio_url"):
                error("既没有可用的发布方转录，也未能提取音频地址")
                print("\n调试信息 - 网页片段:")
                print((info.get("html_sample") or "N/A")[:500])
                sys.exit(1)
            if tracker:
                tracker.phase("downloading_audio")
            if not download_audio(str(info["audio_url"]), audio_path):
                title = info.get("title")
                if title and title != "Unknown" and info.get("source") != "itunes_episode_search":
                    log(f"音频下载失败，尝试使用标题搜索替代音频源: {title}")
                    fallback_info = search_episode_info(title)
                    fallback_audio_url = (fallback_info or {}).get("audio_url")
                    if fallback_audio_url and fallback_audio_url != info["audio_url"]:
                        log(f"成功找到替代音频地址: {fallback_audio_url}")
                        info["audio_url"] = fallback_audio_url
                        audio_path.unlink(missing_ok=True)
                        if not download_audio(str(info["audio_url"]), audio_path):
                            sys.exit(1)
                    else:
                        sys.exit(1)
                else:
                    sys.exit(1)
            audio_created_this_run = True

            if not preprocess_audio(audio_path, wav_path):
                warn("WAV 预处理失败，将直接使用原始音频转录")
                transcribe_path = audio_path
            else:
                transcribe_path = wav_path

            duration = get_audio_duration(transcribe_path)
            if duration > 0:
                info["duration_minutes"] = duration
                log(f"音频时长: {duration:.1f} 分钟")

            initial_prompt = build_initial_prompt(info)
            if asr_engine == "whisper":
                log(f"Whisper initial_prompt: {initial_prompt}")
            else:
                log(f"initial_prompt（备用 Whisper 时使用）: {initial_prompt}")
            if tracker:
                tracker.phase("transcribing", f"engine={asr_engine}, model={model}")
            transcription = transcribe_with_fallback(
                transcribe_path,
                model,
                initial_prompt,
                engine=asr_engine,
                duration_minutes=float(info.get("duration_minutes") or 0.0),
            )
            if not transcription:
                sys.exit(1)
            diarize_if_configured(transcribe_path, transcription)

        if tracker:
            tracker.phase("writing_outputs")
        transcript = transcription["text"]
        write_transcript_segments(
            segments_path,
            srt_path,
            vtt_path,
            list(transcription.get("segments") or []),
        )
        transcript_document = render_transcript_document(
            info,
            transcription,
            transcript_path,
            segments_path,
            srt_path,
            vtt_path,
            metadata_path,
        )
        atomic_write_text(transcript_path, transcript_document)
        write_metadata(
            metadata_path,
            info,
            transcription,
            transcript_path,
            segments_path,
            srt_path,
            vtt_path,
        )
        report_path = summary_dir / f"{combined_name}_详细总结.md"
        knowledge_path, personal_notes_path = ensure_episode_knowledge_files(
            episode_dir,
            info,
            transcript_path=transcript_path,
            report_path=report_path,
        )

        chunk_script = Path(__file__).with_name("chunk_transcript.py")
        chunk_dir = transcript_data_dir / "分块"
        chunk_command = (
            f'"{sys.executable}" "{chunk_script}" "{transcript_path}" '
            f'--output-dir "{chunk_dir}"'
        )
        instruction = build_agent_instruction(
            transcript_path=transcript_path,
            segments_path=segments_path,
            srt_path=srt_path,
            vtt_path=vtt_path,
            metadata_path=metadata_path,
            knowledge_path=knowledge_path,
            personal_notes_path=personal_notes_path,
            chunk_command=chunk_command,
            info=info,
            transcript_chars=len(transcript),
            output_dir=output_dir,
            combined_name=combined_name,
            job_id=tracker.job_id if tracker else None,
            job_status_path=tracker.status_path if tracker else None,
            job_result_path=tracker.result_path if tracker else None,
        )
        atomic_write_text(instruction_path, instruction)

        result_payload = {
            "mode": "transcribe",
            "reused_transcript": used_cached_transcript,
            "transcription_source": transcription.get("source") or "asr",
            "transcript_path": str(transcript_path),
            "episode_dir": str(episode_dir),
            "segments_path": str(segments_path),
            "srt_path": str(srt_path),
            "vtt_path": str(vtt_path),
            "metadata_path": str(metadata_path),
            "instruction_path": str(instruction_path),
            "report_path": str(report_path),
            "knowledge_path": str(knowledge_path),
            "personal_notes_path": str(personal_notes_path),
            "shownotes_archive": shownotes_archive,
            "chapters_archive": chapters_archive,
            "chapters_path": (
                chapters_archive.get("path")
                if chapters_archive and chapters_archive.get("ok")
                else None
            ),
        }
        result_payload["knowledge_index"] = rebuild_knowledge_index(output_dir)
        result_payload["index_path"] = str(rebuild_human_index(output_dir))
        if tracker:
            result_payload = tracker.finish(result_payload, awaiting_report=True)
        emit_result(result_payload)

        log("转录阶段完成；正式总结尚待 Agent 写入")
        if tracker:
            log(f"   作业状态: awaiting_report ({tracker.status_path})")
            log(f"   作业结果: {tracker.result_path}")
        if audio_path.exists():
            log(f"   音频文件: {audio_path}")
        log(f"   转录稿: {transcript_path}")
        log(f"   时间戳: {segments_path}")
        log(f"   SRT: {srt_path}")
        log(f"   WebVTT: {vtt_path}")
        log(f"   元数据: {metadata_path}")
        log(f"   Agent任务指令: {instruction_path}")
        log(f"   转录引擎: {transcription['model']}")
        log(f"   转录字符: {len(transcript)}")
        print("\n" + instruction)
    finally:
        if audio_created_this_run:
            if not keep_audio:
                for path in [audio_path, wav_path]:
                    try:
                        if path.exists():
                            path.unlink()
                    except OSError:
                        warn(f"临时文件删除失败: {path}")
            else:
                log(f"保留音频文件: {audio_path}")
                if wav_path.exists():
                    log(f"保留 WAV 文件: {wav_path}")
        if not keep_audio:
            try:
                audio_dir.rmdir()
            except OSError:
                pass


def cli_entrypoint() -> None:
    try:
        main()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        if ACTIVE_JOB and code and ACTIVE_JOB.state.get("status") == "running":
            ACTIVE_JOB.fail(f"process exited with status {code}")
        raise
    except KeyboardInterrupt:
        if ACTIVE_JOB and ACTIVE_JOB.state.get("status") == "running":
            ACTIVE_JOB.fail("interrupted by user")
        raise
    except Exception as exc:
        if ACTIVE_JOB and ACTIVE_JOB.state.get("status") == "running":
            ACTIVE_JOB.fail(f"{type(exc).__name__}: {exc}")
        raise


if __name__ == "__main__":
    cli_entrypoint()
