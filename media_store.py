#!/usr/bin/env python3
"""Sync archived Show Notes and images to a low-cost storage backend."""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import posixpath
import shutil
import ssl
import time
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.parse import quote, unquote, urlparse, urlunsplit
from urllib.request import Request, urlopen


SUPPORTED_BACKENDS = {"local", "webdav", "s3"}


def _archive_id(manifest_path: Path) -> str:
    suffix = "_media-manifest.json"
    return manifest_path.name[: -len(suffix)] if manifest_path.name.endswith(suffix) else manifest_path.stem


def _public_url(base_url: str, key: str) -> str:
    encoded = "/".join(quote(part) for part in key.split("/"))
    return f"{base_url.rstrip('/')}/{encoded}"


def _load_archive(archive: dict[str, Any] | str | Path) -> tuple[Path, dict[str, Any]]:
    manifest_path = (
        Path(archive["manifest_path"]) if isinstance(archive, dict) else Path(archive)
    ).expanduser().resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return manifest_path, manifest


def _collect_files(
    manifest_path: Path,
    manifest: dict[str, Any],
    public_base_url: str | None,
) -> tuple[list[dict[str, Any]], Path | None]:
    archive_id = _archive_id(manifest_path)
    markdown_path = Path(manifest["markdown_path"]).expanduser().resolve()
    raw_html_path = Path(manifest["raw_html_path"]).expanduser().resolve()
    files = [
        {"path": raw_html_path, "key": f"{archive_id}/shownotes.raw.html"},
    ]
    replacements: dict[str, str] = {}
    seen_paths: set[Path] = set()

    for image in manifest.get("images") or []:
        if not image.get("ok") or not image.get("path"):
            continue
        image_path = Path(image["path"]).expanduser().resolve()
        if not image_path.is_file() or image_path in seen_paths:
            continue
        seen_paths.add(image_path)
        key = f"{archive_id}/assets/{image_path.name}"
        files.append({"path": image_path, "key": key})
        target_url = f"assets/{image_path.name}"
        if public_base_url:
            published_url = _public_url(public_base_url, key)
            image["published_url"] = published_url
            target_url = published_url
        local_url = image.get("markdown_url")
        if local_url:
            replacements[str(local_url)] = target_url

    synced_markdown_path = None
    markdown_source = markdown_path
    if replacements:
        variant = "published" if public_base_url else "synced"
        synced_markdown_path = markdown_path.with_name(
            markdown_path.name.removesuffix(".md") + f"_{variant}.md"
        )
        published = markdown_path.read_text(encoding="utf-8")
        for local_url, published_url in sorted(
            replacements.items(), key=lambda item: len(item[0]), reverse=True
        ):
            published = published.replace(f"]({local_url})", f"]({published_url})")
        synced_markdown_path.write_text(published, encoding="utf-8")
        markdown_source = synced_markdown_path

    files.insert(0, {"path": markdown_source, "key": f"{archive_id}/shownotes.md"})
    return files, synced_markdown_path


def _sync_local(files: list[dict[str, Any]], destination: str) -> list[dict[str, Any]]:
    root = Path(destination).expanduser().resolve()
    uploaded = []
    for item in files:
        target = root / item["key"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item["path"], target)
        uploaded.append({**item, "destination": str(target)})
    return uploaded


def _webdav_request(
    url: str,
    method: str,
    username: str,
    password: str,
    data: bytes | None = None,
) -> None:
    credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
    request = Request(
        url,
        data=data,
        method=method,
        headers={"Authorization": f"Basic {credentials}"},
    )
    try:
        with urlopen(request, timeout=60, context=ssl.create_default_context()):
            return
    except HTTPError as exc:
        if method == "MKCOL" and exc.code in {301, 405}:
            return
        raise


def _sync_webdav(files: list[dict[str, Any]], destination: str) -> list[dict[str, Any]]:
    parsed = urlparse(destination)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("WebDAV destination must be an http(s) URL")
    if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1"}:
        raise ValueError("WebDAV requires HTTPS except for localhost")
    if parsed.username or parsed.password:
        raise ValueError("WebDAV credentials must use environment variables, not URL userinfo")
    username = os.environ.get("WEBDAV_USERNAME", "")
    password = os.environ.get("WEBDAV_PASSWORD", "")
    if not username or not password:
        raise ValueError("WEBDAV_USERNAME and WEBDAV_PASSWORD are required")

    hostname = parsed.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    netloc = f"{hostname}:{parsed.port}" if parsed.port else hostname
    encoded_path = quote(unquote(parsed.path), safe="/%:@")
    base = urlunsplit((parsed.scheme, netloc, encoded_path.rstrip("/"), "", ""))
    created: set[str] = set()
    uploaded = []
    for item in files:
        parts = item["key"].split("/")
        for index in range(1, len(parts)):
            directory = "/".join(parts[:index])
            if directory not in created:
                _webdav_request(
                    f"{base}/{_public_url('', directory).lstrip('/')}",
                    "MKCOL",
                    username,
                    password,
                )
                created.add(directory)
        target_url = f"{base}/{_public_url('', item['key']).lstrip('/')}"
        _webdav_request(
            target_url,
            "PUT",
            username,
            password,
            Path(item["path"]).read_bytes(),
        )
        uploaded.append({**item, "destination": target_url})
    return uploaded


def _sync_s3(files: list[dict[str, Any]], destination: str) -> list[dict[str, Any]]:
    parsed = urlparse(destination)
    if parsed.scheme != "s3" or not parsed.netloc:
        raise ValueError("S3 destination must look like s3://bucket/optional-prefix")
    try:
        import boto3
    except ImportError as exc:
        raise RuntimeError("S3 sync requires: pip install boto3") from exc

    client_options: dict[str, Any] = {
        "region_name": os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "auto"
    }
    endpoint_url = os.environ.get("S3_ENDPOINT_URL")
    if endpoint_url:
        client_options["endpoint_url"] = endpoint_url
    client = boto3.client("s3", **client_options)
    prefix = parsed.path.strip("/")
    uploaded = []
    for item in files:
        key = posixpath.join(prefix, item["key"]) if prefix else item["key"]
        content_type = mimetypes.guess_type(str(item["path"]))[0] or "application/octet-stream"
        client.upload_file(
            str(item["path"]),
            parsed.netloc,
            key,
            ExtraArgs={"ContentType": content_type},
        )
        uploaded.append({**item, "destination": f"s3://{parsed.netloc}/{key}"})
    return uploaded


SYNC_HANDLERS: dict[str, Callable[[list[dict[str, Any]], str], list[dict[str, Any]]]] = {
    "local": _sync_local,
    "webdav": _sync_webdav,
    "s3": _sync_s3,
}


def sync_archive(
    archive: dict[str, Any] | str | Path,
    backend: str | None = None,
    destination: str | None = None,
    public_base_url: str | None = None,
) -> dict[str, Any] | None:
    backend = (backend or os.environ.get("SHOWNOTES_SYNC_BACKEND", "none")).lower().strip()
    if backend in {"", "none", "off"}:
        return None
    if backend not in SUPPORTED_BACKENDS:
        raise ValueError(f"Unsupported sync backend: {backend}")
    destination = destination or os.environ.get("SHOWNOTES_SYNC_DESTINATION")
    if not destination:
        raise ValueError("SHOWNOTES_SYNC_DESTINATION is required when sync is enabled")
    public_base_url = public_base_url or os.environ.get("SHOWNOTES_PUBLIC_BASE_URL")

    manifest_path, manifest = _load_archive(archive)
    files, synced_markdown_path = _collect_files(manifest_path, manifest, public_base_url)
    uploaded = SYNC_HANDLERS[backend](files, destination)

    sync_result = {
        "backend": backend,
        "destination": destination,
        "public_base_url": public_base_url,
        "synced_markdown_path": str(synced_markdown_path) if synced_markdown_path else None,
        "published_markdown_path": (
            str(synced_markdown_path) if synced_markdown_path and public_base_url else None
        ),
        "file_count": len(uploaded),
        "files": [
            {
                "source": str(item["path"]),
                "key": item["key"],
                "destination": item["destination"],
            }
            for item in uploaded
        ],
        "synced_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    sync_manifest_path = manifest_path.with_name(
        f"{_archive_id(manifest_path)}_sync-manifest.json"
    )
    sync_result["sync_manifest_path"] = str(sync_manifest_path)
    manifest["sync"] = sync_result
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    sync_manifest_path.write_text(
        json.dumps(sync_result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    manifest_files = [
        {
            "path": sync_manifest_path,
            "key": f"{_archive_id(manifest_path)}/sync-manifest.json",
        },
        {
            "path": manifest_path,
            "key": f"{_archive_id(manifest_path)}/media-manifest.json",
        },
    ]
    synced_manifests = SYNC_HANDLERS[backend](manifest_files, destination)
    sync_result["file_count"] += len(synced_manifests)
    sync_result["files"].extend(
        {
            "source": str(item["path"]),
            "key": item["key"],
            "destination": item["destination"],
        }
        for item in synced_manifests
    )
    sync_manifest_path.write_text(
        json.dumps(sync_result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    manifest["sync"] = sync_result
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    sync_manifest_path.write_text(
        json.dumps(sync_result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    SYNC_HANDLERS[backend](manifest_files, destination)
    return sync_result


def main() -> None:
    parser = argparse.ArgumentParser(description="同步已归档的 Show Notes 和图片。")
    parser.add_argument("manifest", help="*_media-manifest.json 路径")
    parser.add_argument("--backend", choices=tuple(sorted(SUPPORTED_BACKENDS)))
    parser.add_argument("--destination")
    parser.add_argument("--public-base-url")
    args = parser.parse_args()
    result = sync_archive(
        args.manifest,
        backend=args.backend,
        destination=args.destination,
        public_base_url=args.public_base_url,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
