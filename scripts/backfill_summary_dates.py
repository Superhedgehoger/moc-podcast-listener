#!/usr/bin/env python3
"""Backfill a summary date near the top of existing podcast reports."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any


DATE_LINE_PATTERN = re.compile(
    r"(?m)^>\s*转录总结日期[：:]\s*(\d{4}-\d{2}-\d{2})\s*$"
)


def read_json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def parse_timestamp(value: Any) -> float:
    raw = str(value or "").strip()
    if not raw:
        return 0.0
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def verified_report_dates(output_dir: Path) -> dict[str, tuple[float, str]]:
    dates: dict[str, tuple[float, str]] = {}
    jobs_dir = output_dir / ".jobs"
    for result_path in jobs_dir.glob("*/result.json") if jobs_dir.is_dir() else []:
        try:
            result = read_json_object(result_path)
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        report_path = result.get("report_path")
        timestamp = parse_timestamp(result.get("report_verified_at"))
        if not report_path or timestamp <= 0:
            continue
        key = str(Path(str(report_path)).expanduser().resolve())
        previous = dates.get(key)
        if previous and previous[0] >= timestamp:
            continue
        local_date = datetime.fromtimestamp(timestamp).astimezone().date().isoformat()
        dates[key] = (timestamp, local_date)
    return dates


def insert_summary_date(text: str, summary_date: str) -> str:
    if DATE_LINE_PATTERN.search(text):
        return text
    lines = text.splitlines()
    heading_index = next(
        (index for index, line in enumerate(lines) if line.lstrip().startswith("# ")),
        None,
    )
    if heading_index is None:
        prefix = f"> 转录总结日期：{summary_date}\n\n"
        return prefix + text
    insert_at = heading_index + 1
    while insert_at < len(lines) and not lines[insert_at].strip():
        insert_at += 1
    lines[insert_at:insert_at] = [f"> 转录总结日期：{summary_date}", ""]
    trailing_newline = "\n" if text.endswith("\n") else ""
    return "\n".join(lines) + trailing_newline


def plan_backfill(output_dir: Path) -> list[dict[str, Any]]:
    verified_dates = verified_report_dates(output_dir)
    summary_dir = output_dir / "总结稿"
    plan: list[dict[str, Any]] = []
    for report_path in sorted(summary_dir.glob("*.md")) if summary_dir.is_dir() else []:
        text = report_path.read_text(encoding="utf-8")
        if DATE_LINE_PATTERN.search(text):
            continue
        resolved = str(report_path.resolve())
        verified = verified_dates.get(resolved)
        if verified:
            summary_date = verified[1]
            source = "report_verified_at"
        else:
            summary_date = datetime.fromtimestamp(report_path.stat().st_mtime).astimezone().date().isoformat()
            source = "report_mtime"
        plan.append(
            {
                "path": str(report_path),
                "summary_date": summary_date,
                "date_source": source,
            }
        )
    return plan


def apply_backfill(
    output_dir: Path,
    plan: list[dict[str, Any]],
    *,
    backup_dir: Path | None = None,
) -> dict[str, Any]:
    if not plan:
        return {"updated": 0, "backup_dir": None}
    backup_root = backup_dir or (
        output_dir / ".backup" / f"summary-date-{time.strftime('%Y%m%d-%H%M%S')}"
    )
    for item in plan:
        report_path = Path(item["path"])
        stat = report_path.stat()
        relative = report_path.relative_to(output_dir)
        backup_path = backup_root / relative
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(report_path, backup_path)
        updated = insert_summary_date(
            report_path.read_text(encoding="utf-8"),
            str(item["summary_date"]),
        )
        temporary = report_path.with_name(f".{report_path.name}.{os.getpid()}.tmp")
        temporary.write_text(updated, encoding="utf-8")
        os.replace(temporary, report_path)
        os.utime(report_path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    return {"updated": len(plan), "backup_dir": str(backup_root)}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="为已有总结稿补录转录总结日期；默认仅预览"
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
        "verified_date_count": sum(
            item["date_source"] == "report_verified_at" for item in plan
        ),
        "mtime_fallback_count": sum(
            item["date_source"] == "report_mtime" for item in plan
        ),
        "items": plan,
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
    sys.exit(main())
