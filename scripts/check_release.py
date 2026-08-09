#!/usr/bin/env python3
"""Validate release metadata before creating a tag or GitHub release."""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from version import __version__  # noqa: E402


def main() -> int:
    expected_tag = f"v{__version__}"
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    readme_zh = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")

    errors: list[str] = []
    if not re.search(rf"^## {re.escape(expected_tag)}(?:\s|$)", changelog, re.MULTILINE):
        errors.append(f"CHANGELOG.md 缺少 {expected_tag} 条目")
    if f"版本：{expected_tag}" not in readme_zh:
        errors.append(f"README.zh-CN.md 未声明 {expected_tag}")

    supplied_tag = sys.argv[1] if len(sys.argv) > 1 else None
    if supplied_tag and supplied_tag != expected_tag:
        errors.append(f"标签 {supplied_tag} 与项目版本 {expected_tag} 不一致")

    if errors:
        for item in errors:
            print(f"[ERROR] {item}", file=sys.stderr)
        return 1

    print(f"Release metadata OK: {expected_tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
