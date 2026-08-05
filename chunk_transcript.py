#!/usr/bin/env python3
"""
转录稿分块辅助工具

用于长播客的 evidence-map/reduce 流程：
- 将超长转录稿按目标字数切成多个块
- 优先在段落/换行/句末边界切分，避免硬切句子
- 输出每个块文件和一份证据提取执行清单
"""

from __future__ import annotations

import argparse
import math
import re
from pathlib import Path


SENTENCE_END_RE = re.compile(r"[。！？!?]\s*")


def split_paragraphs(text: str) -> list[str]:
    paragraphs = [item.strip() for item in re.split(r"\n\s*\n", text) if item.strip()]
    if len(paragraphs) > 1:
        return paragraphs
    lines = [item.strip() for item in text.splitlines() if item.strip()]
    return lines or [text.strip()]


def split_long_unit(text: str, target_chars: int) -> list[str]:
    if len(text) <= target_chars:
        return [text]

    parts: list[str] = []
    start = 0
    while start < len(text):
        limit = min(start + target_chars, len(text))
        if limit == len(text):
            parts.append(text[start:].strip())
            break

        window = text[start:limit]
        sentence_matches = list(SENTENCE_END_RE.finditer(window))
        if sentence_matches:
            cut = start + sentence_matches[-1].end()
        else:
            cut = start + window.rfind(" ")
            if cut <= start:
                cut = limit

        parts.append(text[start:cut].strip())
        start = cut

    return [part for part in parts if part]


def chunk_transcript(text: str, target_chars: int, overlap_chars: int) -> list[str]:
    units: list[str] = []
    for paragraph in split_paragraphs(text):
        units.extend(split_long_unit(paragraph, target_chars))

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for unit in units:
        extra = len(unit) + (2 if current else 0)
        if current and current_len + extra > target_chars:
            chunks.append("\n\n".join(current).strip())
            current = []
            current_len = 0

        current.append(unit)
        current_len += extra

    if current:
        chunks.append("\n\n".join(current).strip())

    if overlap_chars <= 0 or len(chunks) <= 1:
        return chunks

    overlapped = [chunks[0]]
    for index in range(1, len(chunks)):
        prev_tail = chunks[index - 1][-overlap_chars:].strip()
        overlapped.append(f"【上一块结尾上下文】\n{prev_tail}\n\n【当前块正文】\n{chunks[index]}")
    return overlapped


def write_manifest(
    manifest_path: Path,
    transcript_path: Path,
    chunk_paths: list[Path],
    target_chars: int,
    overlap_chars: int,
) -> None:
    total_chars = sum(path.read_text(encoding="utf-8").__len__() for path in chunk_paths)
    content = [
        "# 长转录稿证据提取清单",
        "",
        f"- 原始转录稿：{transcript_path}",
        f"- 分块数量：{len(chunk_paths)}",
        f"- 目标块大小：{target_chars} 字",
        f"- 上下文重叠：{overlap_chars} 字",
        f"- 分块总字符数（含重叠上下文）：{total_chars}",
        "",
    ]

    content.extend(["## 第一阶段：逐块独立提取", ""])
    for index, path in enumerate(chunk_paths, start=1):
        chars = len(path.read_text(encoding="utf-8"))
        content.append(
            f"{index}. `{path}`（{chars} 字）→ `evidence_{index:02d}.md`"
        )

    content.extend(
        [
            "",
            "每块只提取：主题与主张、论据和案例、数字与实体、原文引述、资源、歧义点。",
            "不要读取上一块摘要，不要在每一块重写完整报告。",
            "",
            "## 第二阶段：合并与整合",
            "",
            "1. 合并所有 evidence 文件，并按实体、主张和引述去重。",
            "2. 对照时间戳分段核验引述；没有说话人证据时标记“说话人未确认”。",
            "3. 按 `references/report-workflow.md` 只生成一次正式报告。",
            "4. 仅在音频超过 60 分钟、高风险主题或用户明确要求时执行独立质检。",
        ]
    )
    manifest_path.write_text("\n".join(content), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="将播客转录稿切分为适合逐块证据提取的块。")
    parser.add_argument("transcript", help="转录稿 txt 文件路径")
    parser.add_argument(
        "--target-chars",
        type=int,
        default=8000,
        help="每块目标字符数，默认 8000",
    )
    parser.add_argument(
        "--overlap-chars",
        type=int,
        default=400,
        help="从第二块开始附带上一块结尾上下文字符数，默认 400",
    )
    parser.add_argument(
        "--output-dir",
        help="分块输出目录，默认在转录稿同目录创建 <文件名>_chunks",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    transcript_path = Path(args.transcript).expanduser().resolve()
    if not transcript_path.exists():
        raise SystemExit(f"转录稿不存在: {transcript_path}")

    text = transcript_path.read_text(encoding="utf-8").strip()
    if not text:
        raise SystemExit("转录稿为空")

    output_dir = (
        Path(args.output_dir).expanduser().resolve()
        if args.output_dir
        else transcript_path.with_name(f"{transcript_path.stem}_chunks")
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    chunks = chunk_transcript(
        text=text,
        target_chars=args.target_chars,
        overlap_chars=args.overlap_chars,
    )
    width = max(2, int(math.log10(len(chunks))) + 1)

    chunk_paths: list[Path] = []
    for index, chunk in enumerate(chunks, start=1):
        chunk_path = output_dir / f"chunk_{index:0{width}d}.txt"
        chunk_path.write_text(chunk, encoding="utf-8")
        chunk_paths.append(chunk_path)

    manifest_path = output_dir / "REFINE_MANIFEST.md"
    write_manifest(
        manifest_path=manifest_path,
        transcript_path=transcript_path,
        chunk_paths=chunk_paths,
        target_chars=args.target_chars,
        overlap_chars=args.overlap_chars,
    )

    print(f"已生成 {len(chunk_paths)} 个分块")
    print(f"分块目录: {output_dir}")
    print(f"执行清单: {manifest_path}")


if __name__ == "__main__":
    main()
