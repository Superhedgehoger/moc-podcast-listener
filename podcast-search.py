#!/usr/bin/env python3
"""Search the local podcast knowledge index without a vector database."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from knowledge_base import (
    KNOWLEDGE_INDEX_FILENAME,
    load_knowledge_index,
    rebuild_knowledge_index,
    render_search_markdown,
    search_knowledge,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="搜索本地播客知识索引")
    parser.add_argument("query", nargs="?", default="")
    parser.add_argument("--output-dir", default=str(Path.home() / "Documents" / "播客总结"))
    parser.add_argument("--person", default="")
    parser.add_argument("--tag", default="")
    parser.add_argument("--since", default="")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--json", action="store_true", help="输出 JSON 而非 Markdown")
    parser.add_argument("--rebuild", action="store_true", help="搜索前重建知识索引")
    args = parser.parse_args()

    output_dir = Path(args.output_dir).expanduser()
    if args.rebuild:
        rebuild_knowledge_index(output_dir)
    index_path = output_dir / "资料" / KNOWLEDGE_INDEX_FILENAME
    records = load_knowledge_index(index_path)
    results = search_knowledge(
        records,
        query=args.query,
        person=args.person,
        tag=args.tag,
        since=args.since,
        limit=args.limit,
    )
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        print(render_search_markdown(results))


if __name__ == "__main__":
    main()
