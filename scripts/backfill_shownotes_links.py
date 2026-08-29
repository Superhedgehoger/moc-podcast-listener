#!/usr/bin/env python3
"""Backfill a human-readable link archive into existing Show Notes packages."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from shownotes_links import (  # noqa: E402
    canonicalize_links,
    extract_shownotes_links,
    render_link_archive,
    update_link_archive,
)


def read_json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def build_update(manifest_path: Path) -> dict[str, Any] | None:
    shownotes_dir = manifest_path.parent
    markdown_path = shownotes_dir / "shownotes.md"
    raw_html_path = shownotes_dir / "source.raw.html"
    if not markdown_path.is_file():
        return None
    manifest = read_json_object(manifest_path)
    existing_links = manifest.get("links") if isinstance(manifest.get("links"), list) else []
    discovered: list[dict[str, Any]] = list(existing_links)
    if raw_html_path.is_file():
        discovered.extend(
            extract_shownotes_links(
                raw_html_path.read_text(encoding="utf-8"),
                str(manifest.get("episode_url") or ""),
            )
        )
    image_urls = {
        str(item.get("source_url"))
        for item in manifest.get("images") or []
        if isinstance(item, dict) and item.get("source_url")
    }
    links = [
        item
        for item in canonicalize_links(
            discovered,
            base_url=str(manifest.get("episode_url") or ""),
            saved_at=str(manifest.get("created_at") or "") or None,
        )
        if item.get("url") not in image_urls
    ]
    markdown = markdown_path.read_text(encoding="utf-8")
    updated_markdown = update_link_archive(
        markdown,
        render_link_archive(links, shownotes_dir),
    )
    updated_manifest = dict(manifest)
    updated_manifest["links"] = links
    manifest_changed = links != existing_links
    markdown_changed = updated_markdown != markdown
    if manifest_changed:
        updated_manifest["links_updated_at"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
        )
    return {
        "manifest_path": manifest_path,
        "markdown_path": markdown_path,
        "manifest": updated_manifest,
        "markdown": updated_markdown,
        "manifest_changed": manifest_changed,
        "markdown_changed": markdown_changed,
        "link_count": len(links),
    }


def plan_backfill(output_dir: Path) -> list[dict[str, Any]]:
    package_root = output_dir / "资料"
    plan: list[dict[str, Any]] = []
    for manifest_path in sorted(package_root.glob("*/Show Notes/media-manifest.json")):
        try:
            update = build_update(manifest_path)
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if update and (update["manifest_changed"] or update["markdown_changed"]):
            plan.append(update)
    return plan


def _write_preserving_mtime(path: Path, content: str) -> None:
    stat = path.stat()
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))


def apply_backfill(
    output_dir: Path,
    plan: list[dict[str, Any]],
    *,
    backup_dir: Path | None = None,
) -> dict[str, Any]:
    if not plan:
        return {"updated": 0, "backup_dir": None}
    backup_root = backup_dir or (
        output_dir / ".backup" / f"shownotes-links-{time.strftime('%Y%m%d-%H%M%S')}"
    )
    for item in plan:
        for key in ("manifest_path", "markdown_path"):
            path = Path(item[key])
            backup_path = backup_root / path.relative_to(output_dir)
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, backup_path)
        if item["manifest_changed"]:
            _write_preserving_mtime(
                Path(item["manifest_path"]),
                json.dumps(item["manifest"], ensure_ascii=False, indent=2) + "\n",
            )
        if item["markdown_changed"]:
            _write_preserving_mtime(Path(item["markdown_path"]), item["markdown"])
    return {"updated": len(plan), "backup_dir": str(backup_root)}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="为已有 Show Notes 补录人类可读链接归档；默认仅预览"
    )
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--apply", action="store_true", help="备份后实际写入")
    parser.add_argument("--backup-dir", type=Path, help="覆盖默认备份目录")
    args = parser.parse_args()
    output_dir = args.output_dir.expanduser().resolve()
    plan = plan_backfill(output_dir)
    payload: dict[str, Any] = {
        "mode": "apply" if args.apply else "preview",
        "output_dir": str(output_dir),
        "planned": len(plan),
        "link_count": sum(int(item["link_count"]) for item in plan),
        "items": [
            {
                "manifest_path": str(item["manifest_path"]),
                "markdown_path": str(item["markdown_path"]),
                "link_count": item["link_count"],
                "manifest_changed": item["manifest_changed"],
                "markdown_changed": item["markdown_changed"],
            }
            for item in plan
        ],
    }
    if args.apply:
        payload.update(
            apply_backfill(
                output_dir,
                plan,
                backup_dir=args.backup_dir.expanduser().resolve()
                if args.backup_dir
                else None,
            )
        )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
