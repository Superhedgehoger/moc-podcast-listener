#!/usr/bin/env python3
"""Export the podcast knowledge library for common PKM tools."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from knowledge_export import EXPORT_FORMATS, export_library


def main() -> None:
    parser = argparse.ArgumentParser(description="导出本地播客知识库")
    parser.add_argument(
        "--output-dir", default=str(Path.home() / "Documents" / "播客总结")
    )
    parser.add_argument("--export-dir")
    parser.add_argument(
        "--format",
        action="append",
        choices=["all", *sorted(EXPORT_FORMATS)],
        default=[],
        help="可重复指定；默认导出全部格式",
    )
    args = parser.parse_args()
    requested = set(args.format)
    formats = None if not requested or "all" in requested else requested
    result = export_library(
        Path(args.output_dir),
        formats=formats,
        export_dir=Path(args.export_dir).expanduser() if args.export_dir else None,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
