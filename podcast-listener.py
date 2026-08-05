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
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urljoin, urlparse
import xml.etree.ElementTree as ET


DEFAULT_OUTPUT_DIR = Path.home() / "Documents" / "播客总结"
DEFAULT_MODEL = "large-v3"
FALLBACK_MODELS = ["large-v3", "small", "base"]
ITUNES_LOOKUP_URL = "https://itunes.apple.com/lookup"
ITUNES_SEARCH_URL = "https://itunes.apple.com/search"
SHOWNOTES_ASSET_MODES = {"off", "online", "local", "hybrid"}
SHOWNOTES_DEFAULT_MAX_IMAGES = 40
SHOWNOTES_DEFAULT_MAX_IMAGE_BYTES = 15 * 1024 * 1024
IMAGE_CONTENT_TYPE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/avif": ".avif",
}


def log(msg: str) -> None:
    print(f"[INFO] {msg}")


def warn(msg: str) -> None:
    print(f"[WARN] {msg}")


def error(msg: str) -> None:
    print(f"[ERROR] {msg}", file=sys.stderr)


def run_command(args: list[str], timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def is_url(value: str) -> bool:
    parsed = urlparse(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def fetch_text(url: str, timeout: int = 40) -> str | None:
    try:
        result = run_command(
            [
                "curl",
                "-s",
                "-L",
                "--max-time",
                str(max(timeout - 5, 5)),
                "-A",
                "Mozilla/5.0 podcast-listener",
                url,
            ],
            timeout=timeout,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout
        warn(f"获取失败: {url} {result.stderr.strip()}")
        return None
    except Exception as exc:
        warn(f"获取异常: {url} {exc}")
        return None


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
                    "cover_url": "",
                    "show_notes": description,
                    "guests": [author] if author else [],
                    "duration_minutes": 0.0,
                    "show_title": show_title,
                    "pub_date": pub_date,
                    "guid": guid,
                    "source": "rss",
                }
            )
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
        for key_set in [
            {"shownotes", "showNotes", "description", "content", "brief"},
            {"podcastDescription", "episodeDescription"},
        ]:
            value = first_string_by_keys(data, key_set)
            if value:
                candidates.append(value)

    meta_match = re.search(
        r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)["\']',
        html_text,
        flags=re.IGNORECASE,
    )
    if meta_match:
        candidates.append(meta_match.group(1))

    usable = [(item.strip(), clean_text(item)) for item in candidates if clean_text(item)]
    return max(usable, key=lambda item: len(item[1]))[0] if usable else ""


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

    if data:
        for item in walk_json(data):
            if not isinstance(item, dict):
                continue
            for key, value in item.items():
                if key.lower() in {"nickname", "name", "username", "author", "speaker"} and isinstance(value, str):
                    add_name(value)

    text = "\n".join([title, show_notes])
    guest_patterns = [
        r"(?:本期嘉宾|嘉宾|主播|主持人|对谈人|采访者|受访者)[:：]\s*([^\n]+)",
        r"(?:和|与)([\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z·.\s]{1,20})(?:聊|谈|对话)",
    ]
    for pattern in guest_patterns:
        for match in re.finditer(pattern, text):
            add_name(match.group(1))

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


def is_safe_remote_url(url: str) -> tuple[bool, str | None]:
    parsed = urlparse(url)
    hostname = parsed.hostname
    if parsed.scheme not in {"http", "https"} or not hostname:
        return False, "unsupported URL"
    if hostname.lower() == "localhost" or hostname.lower().endswith(".localhost"):
        return False, "local address blocked"

    try:
        default_port = 443 if parsed.scheme == "https" else 80
        addresses = {item[4][0] for item in socket.getaddrinfo(hostname, parsed.port or default_port)}
    except OSError as exc:
        return False, f"DNS lookup failed: {exc}"

    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return False, f"invalid resolved address: {address}"
        if not ip.is_global:
            return False, f"non-public address blocked: {address}"
    return True, None


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
    safe, safety_error = is_safe_remote_url(url)
    if not safe:
        return {"ok": False, "error": safety_error or "unsafe URL"}
    try:
        command = [
            "curl",
            "-L",
            "-f",
            "-sS",
            "--connect-timeout",
            "15",
            "--max-time",
            "90",
            "--max-filesize",
            str(max_bytes),
            "-A",
            "Mozilla/5.0 podcast-listener",
        ]
        if referer:
            command.extend(["-e", referer])
        command.extend(
            [
                "-w",
                "%{content_type}",
                "-o",
                str(temp_path),
                url,
            ]
        )
        result = run_command(
            command,
            timeout=100,
        )
        if (
            result.returncode != 0
            or not temp_path.exists()
            or temp_path.stat().st_size == 0
            or temp_path.stat().st_size > max_bytes
        ):
            if temp_path.exists():
                temp_path.unlink()
            return {"ok": False, "error": result.stderr.strip() or "download failed"}

        content_type = result.stdout.strip()
        ext = detect_image_extension(temp_path, content_type, url)
        if not ext:
            temp_path.unlink()
            return {"ok": False, "error": f"response is not a supported image: {content_type or 'unknown type'}"}

        file_hash = hashlib.sha256(temp_path.read_bytes()).hexdigest()
        final_path = assets_dir / f"image-{index:02d}-{file_hash[:10]}{ext}"
        if not final_path.exists():
            temp_path.replace(final_path)
        else:
            temp_path.unlink()
        return {
            "ok": True,
            "path": str(final_path),
            "sha256": file_hash,
            "bytes": final_path.stat().st_size,
            "content_type": content_type or None,
        }
    except Exception as exc:
        try:
            if temp_path.exists():
                temp_path.unlink()
        except OSError:
            pass
        return {"ok": False, "error": str(exc)}


def archive_show_notes(info: dict[str, Any], output_dir: Path, combined_name: str) -> dict[str, Any] | None:
    raw_show_notes = info.get("show_notes") or ""
    if not raw_show_notes.strip():
        return None

    mode = os.environ.get("SHOWNOTES_ASSETS", "hybrid").lower().strip()
    if mode not in SHOWNOTES_ASSET_MODES:
        warn(f"SHOWNOTES_ASSETS 值 '{mode}' 无效，将使用 hybrid")
        mode = "hybrid"
    if mode == "off":
        return None

    shownotes_dir = output_dir / "Show Notes"
    assets_dir = output_dir / "图片" / f"{combined_name}_assets"
    shownotes_dir.mkdir(parents=True, exist_ok=True)
    if mode in {"local", "hybrid"}:
        assets_dir.mkdir(parents=True, exist_ok=True)

    html_path = shownotes_dir / f"{combined_name}_shownotes.raw.html"
    markdown_path = shownotes_dir / f"{combined_name}_shownotes.md"
    manifest_path = shownotes_dir / f"{combined_name}_media-manifest.json"

    images: list[dict[str, Any]] = []
    seen_images: dict[str, str] = {}
    seen_hashes: dict[str, str] = {}
    cached_images: dict[str, dict[str, Any]] = {}
    if manifest_path.exists():
        try:
            previous_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            for entry in previous_manifest.get("images") or []:
                source_url = entry.get("source_url")
                previous_path = entry.get("path")
                if not source_url or not previous_path or not entry.get("ok"):
                    continue
                candidate = assets_dir / Path(previous_path).name
                if candidate.exists() and candidate.is_file():
                    cached_entry = dict(entry)
                    cached_entry["path"] = str(candidate)
                    cached_images[source_url] = cached_entry
        except (OSError, json.JSONDecodeError, AttributeError):
            warn(f"已有 Show Notes manifest 不可读，将重新归档: {manifest_path}")
    max_images = max(0, int(os.environ.get("SHOWNOTES_MAX_IMAGES", str(SHOWNOTES_DEFAULT_MAX_IMAGES))))
    max_image_bytes = max(
        1,
        int(os.environ.get("SHOWNOTES_MAX_IMAGE_BYTES", str(SHOWNOTES_DEFAULT_MAX_IMAGE_BYTES))),
    )

    def resolve_image(src: str, alt: str) -> str:
        image_entry: dict[str, Any] = {"source_url": src, "alt": alt}
        if src in seen_images:
            image_entry["markdown_url"] = seen_images[src]
            image_entry["duplicate"] = True
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
                    warn(f"Show Notes 图片下载失败，保留在线链接: {src}")
        image_entry["markdown_url"] = markdown_url
        seen_images[src] = markdown_url
        images.append(image_entry)
        return markdown_url

    parser = ShowNotesMarkdownParser(info.get("url") or "", resolve_image)
    parser.feed(raw_show_notes)
    parser.close()

    markdown = parser.markdown() or clean_text(raw_show_notes)
    source_url = info.get("url") or ""
    if source_url:
        markdown = f"[原始单集链接]({markdown_escape_url(source_url)})\n\n{markdown}".strip()

    html_path.write_text(raw_show_notes, encoding="utf-8")
    markdown_path.write_text(markdown + "\n", encoding="utf-8")

    image_urls = {item["source_url"] for item in images if item.get("source_url")}
    links = list(parser.links)
    seen_links = {item["url"] for item in links}
    for candidate in re.findall(r"https?://[^\s<>'\"\])]+", raw_show_notes):
        normalized = normalize_shownotes_url(candidate, info.get("url") or "")
        if normalized and normalized not in image_urls and normalized not in seen_links:
            links.append({"text": normalized, "url": normalized})
            seen_links.add(normalized)

    manifest = {
        "mode": mode,
        "episode_url": info.get("url"),
        "title": info.get("title"),
        "show_title": info.get("show_title"),
        "raw_html_path": str(html_path),
        "markdown_path": str(markdown_path),
        "assets_dir": str(assets_dir) if mode in {"local", "hybrid"} else None,
        "images": images,
        "links": links,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "raw_html_path": str(html_path),
        "markdown_path": str(markdown_path),
        "manifest_path": str(manifest_path),
        "assets_dir": str(assets_dir) if mode in {"local", "hybrid"} else None,
        "image_count": len(images),
        "link_count": len(links),
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

        title = first_string_by_keys(data, {"title"}) if data else None
        if not title:
            title_match = re.search(r"<title>(.*?)</title>", page_html, flags=re.DOTALL)
            title = title_match.group(1) if title_match else "Unknown"
        title = clean_text(title).replace(" - 小宇宙", "").strip() or "Unknown"

        show_notes = extract_show_notes(page_html, data)
        audio_url = find_audio_url(page_html, data)
        guests = extract_guests(title, show_notes, data)

        cover_url = first_string_by_keys(data, {"image", "cover", "coverUrl"}) if data else None

        show_title = None
        pub_date = None
        if data:
            show_title = find_key_path(data, ["episode", "podcast", "title"]) or find_key_path(data, ["podcast", "title"])
            raw_date = (
                find_key_path(data, ["episode", "datePublished"])
                or find_key_path(data, ["episode", "pubDate"])
                or find_key_path(data, ["datePublished"])
                or find_key_path(data, ["pubDate"])
                or find_key_path(data, ["episode", "publishTime"])
            )
            if raw_date and isinstance(raw_date, str):
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

        return {
            "id": episode_id,
            "title": title,
            "url": url,
            "audio_url": audio_url,
            "cover_url": cover_url,
            "show_notes": show_notes,
            "guests": guests,
            "duration_minutes": 0.0,
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
        return {
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
            audio_url = None
            
            formats = info_json.get("formats", [])
            audio_formats = [f for f in formats if f.get("vcodec") == "none" and f.get("acodec") != "none"]
            if audio_formats:
                audio_formats.sort(key=lambda x: x.get("abr") or x.get("tbr") or 0, reverse=True)
                audio_url = audio_formats[0].get("url")
            
            if not audio_url:
                g_res = run_command(["yt-dlp", "-g", "-f", "bestaudio", url], timeout=30)
                if g_res.returncode == 0 and g_res.stdout.strip():
                    audio_url = g_res.stdout.strip()
                    
            if not audio_url:
                g_res = run_command(["yt-dlp", "-g", url], timeout=30)
                if g_res.returncode == 0 and g_res.stdout.strip():
                    audio_url = g_res.stdout.strip()

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
        formats = info_json.get("formats", [])
        audio_formats = [
            item for item in formats
            if item.get("vcodec") == "none" and item.get("acodec") not in (None, "none") and item.get("url")
        ]
        audio_formats.sort(key=lambda item: item.get("abr") or item.get("tbr") or 0, reverse=True)
        audio_url = (audio_formats[0].get("url") if audio_formats else None) or info_json.get("url")
        if not audio_url:
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


def resolve_episode_info(input_value: str) -> dict[str, Any] | None:
    value = input_value.strip()
    if not value:
        return None

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
        if ".m3u8" in audio_url.lower():
            result = run_command(
                ["ffmpeg", "-y", "-i", audio_url, "-c", "copy", str(output_path)],
                timeout=dl_timeout,
            )
            if result.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0:
                size_mb = output_path.stat().st_size / 1024 / 1024
                log(f"HLS 下载完成: {size_mb:.1f} MB")
                return True
            error(f"HLS 下载失败: {result.stderr.strip()[-800:]}")
            return False

        result = run_command(
            ["curl", "-L", "-C", "-", "--max-time", str(dl_timeout), "-o", str(output_path), audio_url],
            timeout=dl_timeout + 60,
        )
        if result.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0:
            size_mb = output_path.stat().st_size / 1024 / 1024
            log(f"下载完成: {size_mb:.1f} MB")
            return True
        error(f"下载失败: {result.stderr.strip()}")
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


def build_agent_instruction(
    transcript_path: Path,
    segments_path: Path,
    srt_path: Path,
    metadata_path: Path,
    chunk_command: str,
    info: dict[str, Any],
    transcript_chars: int,
    output_dir: Path,
    combined_name: str,
) -> str:
    duration = info.get("duration_minutes", 0.0)
    target_words = target_summary_words(duration)
    guests_text = ", ".join(info.get("guests") or []) or "未自动识别"
    report_path = output_dir / "总结稿" / f"{combined_name}_详细总结.md"
    workflow_path = Path(__file__).with_name("references") / "report-workflow.md"
    return f"""请按 SKILL.md 和报告工作流继续完成播客总结。

输入文件：
- 转录稿：{transcript_path}
- 时间戳分段：{segments_path}
- SRT 字幕：{srt_path}
- 元数据：{metadata_path}
- 报告工作流：{workflow_path}
{f"- Show Notes Markdown：{info.get('shownotes_archive', {}).get('markdown_path')}" if info.get('shownotes_archive') else ""}
{f"- Show Notes 图片/链接 Manifest：{info.get('shownotes_archive', {}).get('manifest_path')}" if info.get('shownotes_archive') else ""}

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
6. 仅对 60 分钟以上、高风险主题或用户明确要求的深度版执行独立质检。
7. 提交前确认：
   - [ ] 包含「📋 基本信息」表格，所有字段已填写
   - [ ] 核心观点包含具体证据、案例、数字或机制
   - [ ] 引述已核对原文，且未猜测说话人
   - [ ] 包含背景与术语、实用资源、延伸思考与局限
   - [ ] 正文字数 ≥ {target_words} 字（不含 Show Notes）
   - [ ] 包含原始 Show Notes
   - [ ] 包含独立转录稿、segments、SRT 的相对链接，且未嵌入完整转录正文

   写入最终目标文件：
   {report_path}
"""


def format_srt_timestamp(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


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
        "> 本文件由自动语音识别生成，可能包含人名、专有名词和断句错误。"
        "时间戳来自 ASR/VAD 分段；没有可靠说话人证据时不推断姓名。",
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
            f"- 元数据：{relative_markdown_link('打开 JSON', metadata_path, transcript_dir)}",
            f"- Show Notes：{relative_markdown_link('打开 Markdown', archive.get('markdown_path'), transcript_dir)}",
            f"- 媒体清单：{relative_markdown_link('打开 JSON', archive.get('manifest_path'), transcript_dir)}",
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
    segments: list[dict[str, Any]],
) -> None:
    normalized: list[dict[str, Any]] = []
    srt_blocks: list[str] = []
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
    segments_path.write_text(
        json.dumps(normalized, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    srt_path.write_text("\n\n".join(srt_blocks) + ("\n" if srt_blocks else ""), encoding="utf-8")


def write_metadata(
    path: Path,
    info: dict[str, Any],
    transcription: dict[str, Any],
    transcript_path: Path,
    segments_path: Path,
    srt_path: Path,
) -> None:
    metadata = {
        "episode": info,
        "transcription": {
            "model": transcription.get("model"),
            "language": transcription.get("language"),
            "initial_prompt": transcription.get("initial_prompt"),
            "transcript_path": str(transcript_path),
            "segments_path": str(segments_path),
            "srt_path": str(srt_path),
            "segment_count": len(transcription.get("segments") or []),
            "transcript_chars": len(transcription.get("text", "")),
            "diarization": transcription.get("diarization"),
            "processed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        },
    }
    path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_cli_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="解析、归档并转录播客链接、RSS、媒体页面或节目搜索词。"
    )
    parser.add_argument("input", nargs="+", help="单集链接、带标题链接或搜索关键词")
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
    parser.add_argument(
        "--force-transcribe",
        action="store_true",
        help="忽略已有转录产物，重新下载和转录",
    )
    parser.add_argument("--output-dir", help="覆盖输出目录")
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
    return parser.parse_args()


def emit_result(payload: dict[str, Any], *, print_json: bool = False) -> None:
    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    result_json_path = os.environ.get("RESULT_JSON")
    if result_json_path:
        Path(result_json_path).expanduser().write_text(serialized, encoding="utf-8")
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
    show_name = safe_filename(info.get("show_title") or "未知节目", "未知节目")
    episode_title = safe_filename(info.get("title") or "未知单集", "未知单集")
    raw_pub_date = info.get("pub_date") or time.strftime("%Y%m%d")
    pub_date = re.sub(r"[^0-9]", "", str(raw_pub_date))[:8]
    if len(pub_date) < 8:
        pub_date = time.strftime("%Y%m%d")

    combined = f"{show_name}_{episode_title}_{pub_date}"
    while len(combined.encode("utf-8")) > 200 and len(episode_title) > 5:
        episode_title = episode_title[:-1]
        combined = f"{show_name}_{episode_title}_{pub_date}"
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
            "initial_prompt": transcription_metadata.get("initial_prompt") or "",
            "diarization": transcription_metadata.get("diarization"),
        },
        cached_metadata,
    )


def main() -> None:
    args = parse_cli_args()
    user_input = " ".join(args.input).strip()
    asr_engine = (args.engine or os.environ.get("ASR_ENGINE", "sensevoice")).lower().strip()
    if asr_engine not in ("sensevoice", "whisper", "stitch"):
        warn(f"ASR_ENGINE 值 '{asr_engine}' 无效，将使用默认值 'sensevoice'")
        asr_engine = "sensevoice"
    model = args.model or os.environ.get("WHISPER_MODEL", DEFAULT_MODEL)
    output_dir = Path(
        args.output_dir or os.environ.get("OUTPUT_DIR", str(DEFAULT_OUTPUT_DIR))
    ).expanduser()
    keep_audio = args.keep_audio or os.environ.get("KEEP_AUDIO", "0") == "1"
    force_transcribe = (
        args.force_transcribe or os.environ.get("FORCE_TRANSCRIBE", "0") == "1"
    )
    if args.shownotes_assets:
        os.environ["SHOWNOTES_ASSETS"] = args.shownotes_assets
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
    info = resolve_episode_info(user_input)
    if not info:
        error("无法解析播客输入；请换用单集链接、RSS 链接，或补充节目名 + 单集标题关键词")
        sys.exit(1)

    log(f"标题: {info['title']}")
    log(f"来源: {info.get('source', 'unknown')}")
    if info.get("guests"):
        log(f"嘉宾/说话人候选: {', '.join(info['guests'])}")

    if args.resolve_only:
        emit_result({"mode": "resolve-only", "episode": info}, print_json=True)
        return

    output_dir.mkdir(parents=True, exist_ok=True)
    transcript_dir = output_dir / "转录稿"
    summary_dir = output_dir / "总结稿"
    audio_dir = output_dir / "音频"

    transcript_dir.mkdir(parents=True, exist_ok=True)
    summary_dir.mkdir(parents=True, exist_ok=True)
    audio_dir.mkdir(parents=True, exist_ok=True)

    combined_name = build_combined_name(info)

    transcript_path = transcript_dir / f"{combined_name}_转录稿.txt"
    segments_path = transcript_dir / f"{combined_name}_segments.json"
    srt_path = transcript_dir / f"{combined_name}.srt"
    metadata_path = output_dir / f"{combined_name}_metadata.json"
    instruction_path = output_dir / f"{combined_name}_Agent任务指令.txt"

    shownotes_archive = archive_show_notes(info, output_dir, combined_name)
    if shownotes_archive:
        info["shownotes_archive"] = shownotes_archive
        log(f"   Show Notes: {shownotes_archive['markdown_path']}")
        log(
            f"   Show Notes 图片: {shownotes_archive['image_count']}，链接: {shownotes_archive['link_count']}，模式: {shownotes_archive['mode']}"
        )
        sync_result = sync_shownotes_if_configured(shownotes_archive)
        if sync_result:
            shownotes_archive["sync"] = sync_result
            if sync_result.get("status") != "failed":
                log(
                    f"   Show Notes 已同步: {sync_result['backend']}，{sync_result['file_count']} 个文件"
                )

    if args.archive_only:
        metadata_path.write_text(
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
            encoding="utf-8",
        )
        payload = {
            "mode": "archive-only",
            "metadata_path": str(metadata_path),
            "shownotes_archive": shownotes_archive,
        }
        emit_result(payload, print_json=True)
        return

    if not info.get("audio_url"):
        error("未能提取音频地址，可能网页结构已变更或搜索结果不够精确")
        print("\n调试信息 - 网页片段:")
        print((info.get("html_sample") or "N/A")[:500])
        sys.exit(1)

    parsed_url = urlparse(info["audio_url"])
    ext = os.path.splitext(parsed_url.path)[1]
    if not ext or len(ext) > 5 or ext.lower() in {".m3u8", ".m3u"}:
        ext = ".m4a"

    audio_path = audio_dir / f"{combined_name}{ext}"
    wav_path = audio_dir / f"{combined_name}.wav"
    used_cached_transcript = False
    audio_created_this_run = False

    try:
        cached = None if force_transcribe else load_cached_transcription(
            transcript_path, segments_path, metadata_path, info
        )
        if cached:
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
                if download_audio(info["audio_url"], audio_path):
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
            if not download_audio(info["audio_url"], audio_path):
                title = info.get("title")
                if title and title != "Unknown" and info.get("source") != "itunes_episode_search":
                    log(f"音频下载失败，尝试使用标题搜索替代音频源: {title}")
                    fallback_info = search_episode_info(title)
                    fallback_audio_url = (fallback_info or {}).get("audio_url")
                    if fallback_audio_url and fallback_audio_url != info["audio_url"]:
                        log(f"成功找到替代音频地址: {fallback_audio_url}")
                        info["audio_url"] = fallback_audio_url
                        audio_path.unlink(missing_ok=True)
                        if not download_audio(info["audio_url"], audio_path):
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

        transcript = transcription["text"]
        write_transcript_segments(
            segments_path,
            srt_path,
            list(transcription.get("segments") or []),
        )
        transcript_document = render_transcript_document(
            info,
            transcription,
            transcript_path,
            segments_path,
            srt_path,
            metadata_path,
        )
        transcript_path.write_text(transcript_document, encoding="utf-8")
        write_metadata(
            metadata_path,
            info,
            transcription,
            transcript_path,
            segments_path,
            srt_path,
        )

        chunk_script = Path(__file__).with_name("chunk_transcript.py")
        chunk_command = f'"{sys.executable}" "{chunk_script}" "{transcript_path}"'
        instruction = build_agent_instruction(
            transcript_path=transcript_path,
            segments_path=segments_path,
            srt_path=srt_path,
            metadata_path=metadata_path,
            chunk_command=chunk_command,
            info=info,
            transcript_chars=len(transcript),
            output_dir=output_dir,
            combined_name=combined_name,
        )
        instruction_path.write_text(instruction, encoding="utf-8")

        result_payload = {
            "mode": "transcribe",
            "reused_transcript": used_cached_transcript,
            "transcript_path": str(transcript_path),
            "segments_path": str(segments_path),
            "srt_path": str(srt_path),
            "metadata_path": str(metadata_path),
            "instruction_path": str(instruction_path),
            "report_path": str(summary_dir / f"{combined_name}_详细总结.md"),
            "shownotes_archive": shownotes_archive,
        }
        emit_result(result_payload)

        log("✅ 转录阶段完成")
        if not used_cached_transcript:
            log(f"   音频文件: {audio_path}")
        log(f"   转录稿: {transcript_path}")
        log(f"   时间戳: {segments_path}")
        log(f"   SRT: {srt_path}")
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


if __name__ == "__main__":
    main()
