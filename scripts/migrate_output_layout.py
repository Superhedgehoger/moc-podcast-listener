#!/usr/bin/env python3
"""Migrate legacy flat podcast outputs into the v4.9 human-first layout."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class Move:
    source: Path
    destination: Path


def add_move(moves: list[Move], source: Path, destination: Path) -> None:
    if source.exists():
        moves.append(Move(source, destination))


def discover_episode_names(root: Path) -> set[str]:
    names: set[str] = set()
    patterns = (
        (root, "*_metadata.json", "_metadata.json"),
        (root, "*_Agent任务指令.txt", "_Agent任务指令.txt"),
        (root / "转录稿", "*_转录稿.txt", "_转录稿.txt"),
        (root / "转录稿", "*_segments.json", "_segments.json"),
        (root / "Show Notes", "*_shownotes.md", "_shownotes.md"),
        (root / "Show Notes", "*_media-manifest.json", "_media-manifest.json"),
        (root / "图片", "*_assets", "_assets"),
    )
    for directory, pattern, suffix in patterns:
        if not directory.is_dir():
            continue
        for path in directory.glob(pattern):
            names.add(path.name[: -len(suffix)])
    return names


def plan_moves(root: Path) -> list[Move]:
    moves: list[Move] = []
    for name in sorted(discover_episode_names(root)):
        episode = root / "资料" / name
        transcript_data = episode / "转录数据"
        shownotes = episode / "Show Notes"

        add_move(moves, root / f"{name}_metadata.json", episode / "metadata.json")
        add_move(
            moves,
            root / f"{name}_Agent任务指令.txt",
            episode / "Agent任务指令.txt",
        )
        add_move(
            moves,
            root / "转录稿" / f"{name}_segments.json",
            transcript_data / "segments.json",
        )
        add_move(
            moves,
            root / "转录稿" / f"{name}.srt",
            transcript_data / "transcript.srt",
        )
        add_move(
            moves,
            root / "转录稿" / f"{name}.vtt",
            transcript_data / "transcript.vtt",
        )
        add_move(
            moves,
            root / "转录稿" / f"{name}_转录稿_chunks",
            transcript_data / "分块",
        )
        add_move(
            moves,
            root / "Show Notes" / f"{name}_shownotes.md",
            shownotes / "shownotes.md",
        )
        add_move(
            moves,
            root / "Show Notes" / f"{name}_shownotes.raw.html",
            shownotes / "source.raw.html",
        )
        add_move(
            moves,
            root / "Show Notes" / f"{name}_media-manifest.json",
            shownotes / "media-manifest.json",
        )
        add_move(
            moves,
            root / "图片" / f"{name}_assets",
            shownotes / "图片",
        )
        add_move(
            moves,
            root / "Show Notes" / f"{name}_link-snapshots",
            shownotes / "链接快照",
        )
    return moves


def replacement_map(moves: list[Move]) -> dict[str, str]:
    replacements: dict[str, str] = {}
    for move in moves:
        replacements[str(move.source)] = str(move.destination)
    return replacements


def relative_replacements(root: Path, path: Path, names: set[str]) -> dict[str, str]:
    replacements: dict[str, str] = {}
    for name in names:
        package = f"../资料/{name}"
        if path.parent == root / "转录稿":
            replacements.update(
                {
                    f"{name}_segments.json": f"{package}/转录数据/segments.json",
                    f"{name}.srt": f"{package}/转录数据/transcript.srt",
                    f"{name}.vtt": f"{package}/转录数据/transcript.vtt",
                    f"../{name}_metadata.json": f"{package}/metadata.json",
                    f"../Show Notes/{name}_shownotes.md": f"{package}/Show Notes/shownotes.md",
                    f"../Show Notes/{name}_media-manifest.json": f"{package}/Show Notes/media-manifest.json",
                }
            )
        elif path.parent == root / "总结稿":
            replacements.update(
                {
                    f"../转录稿/{name}_segments.json": f"{package}/转录数据/segments.json",
                    f"../转录稿/{name}.srt": f"{package}/转录数据/transcript.srt",
                    f"../转录稿/{name}.vtt": f"{package}/转录数据/transcript.vtt",
                    f"../{name}_metadata.json": f"{package}/metadata.json",
                    f"../Show Notes/{name}_shownotes.md": f"{package}/Show Notes/shownotes.md",
                    f"../Show Notes/{name}_media-manifest.json": f"{package}/Show Notes/media-manifest.json",
                    f"../图片/{name}_assets/": f"{package}/Show Notes/图片/",
                }
            )
        elif path.name in {"shownotes.md", "media-manifest.json"}:
            replacements[f"../图片/{name}_assets/"] = "图片/"
        elif root / "资料" / name / "转录数据" / "分块" in path.parents:
            replacements.update(
                {
                    f"{name}_segments.json": "../segments.json",
                    f"{name}.srt": "../transcript.srt",
                    f"{name}.vtt": "../transcript.vtt",
                    f"../{name}_metadata.json": "../../metadata.json",
                    f"../Show Notes/{name}_shownotes.md": "../../Show Notes/shownotes.md",
                    f"../Show Notes/{name}_media-manifest.json": "../../Show Notes/media-manifest.json",
                }
            )
    return replacements


def rewrite_text(text: str, replacements: dict[str, str]) -> str:
    for old in sorted(replacements, key=len, reverse=True):
        text = text.replace(old, replacements[old])
    return text


def rewrite_json_values(value: object, replacements: dict[str, str]) -> object:
    if isinstance(value, str):
        return rewrite_text(value, replacements)
    if isinstance(value, list):
        return [rewrite_json_values(item, replacements) for item in value]
    if isinstance(value, dict):
        return {
            key: rewrite_json_values(item, replacements)
            for key, item in value.items()
        }
    return value


def candidate_text_files(root: Path) -> list[Path]:
    candidates: list[Path] = []
    excluded = {"旧内容", ".backup", "音频"}
    allowed = {".json", ".md", ".txt", ".html"}
    for directory, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in excluded]
        for filename in filenames:
            path = Path(directory) / filename
            if path.suffix.lower() in allowed:
                candidates.append(path)
    return candidates


def validate_moves(moves: list[Move]) -> list[str]:
    errors: list[str] = []
    destinations: set[Path] = set()
    for move in moves:
        if move.destination in destinations:
            errors.append(f"duplicate destination: {move.destination}")
        destinations.add(move.destination)
        if move.destination.exists():
            errors.append(f"destination already exists: {move.destination}")
    return errors


def moved_path_for(path: Path, moves: list[Move]) -> Optional[Path]:
    for move in moves:
        try:
            relative = path.relative_to(move.source)
        except ValueError:
            continue
        return move.destination / relative
    return None


def backup_paths(root: Path, paths: list[Path], backup_dir: Path) -> int:
    copied = 0
    covered_directories: list[Path] = []
    for path in paths:
        if not path.exists() or any(parent in path.parents for parent in covered_directories):
            continue
        destination = backup_dir / path.relative_to(root)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if path.is_dir():
            shutil.copytree(path, destination)
            covered_directories.append(path)
        else:
            shutil.copy2(path, destination)
        copied += 1
    return copied


def apply_migration(
    root: Path, moves: list[Move], backup_dir: Optional[Path] = None
) -> dict[str, object]:
    names = discover_episode_names(root)
    absolute_replacements = replacement_map(moves)
    rewrite_candidates = candidate_text_files(root)
    rewritten: list[str] = []
    backed_up = 0

    if backup_dir:
        backed_up = backup_paths(
            root,
            [move.source for move in moves] + rewrite_candidates,
            backup_dir,
        )

    for move in moves:
        move.destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(move.source), str(move.destination))

    for path in rewrite_candidates:
        if not path.exists():
            moved_path = moved_path_for(path, moves)
            if moved_path is None:
                continue
            path = moved_path
        try:
            original = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        updated = rewrite_text(original, absolute_replacements)
        updated = rewrite_text(updated, relative_replacements(root, path, names))
        if path.suffix.lower() == ".json":
            try:
                json_data = json.loads(updated)
            except json.JSONDecodeError:
                pass
            else:
                all_replacements = dict(absolute_replacements)
                all_replacements.update(relative_replacements(root, path, names))
                rewritten_data = rewrite_json_values(json_data, all_replacements)
                if rewritten_data != json_data:
                    updated = (
                        json.dumps(rewritten_data, ensure_ascii=False, indent=2) + "\n"
                    )
        if path.name == "media-manifest.json":
            try:
                manifest = json.loads(updated)
            except json.JSONDecodeError:
                pass
            else:
                manifest["layout"] = "episode_directory"
                manifest["archive_dir"] = str(path.parent)
                for image in manifest.get("images") or []:
                    image_path = image.get("path")
                    if image_path:
                        image["markdown_url"] = os.path.relpath(
                            image_path, path.parent
                        )
                updated = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
        if updated != original:
            path.write_text(updated, encoding="utf-8")
            rewritten.append(str(path))

    for legacy_dir in (root / "Show Notes", root / "图片"):
        try:
            legacy_dir.rmdir()
        except OSError:
            pass

    return {
        "root": str(root),
        "moved": len(moves),
        "rewritten": len(rewritten),
        "backup_dir": str(backup_dir) if backup_dir else None,
        "backed_up": backed_up,
        "moves": [
            {"source": str(move.source), "destination": str(move.destination)}
            for move in moves
        ],
        "rewritten_files": rewritten,
        "completed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_dir", type=Path, help="Podcast output root")
    parser.add_argument(
        "--apply", action="store_true", help="Apply moves; otherwise print a dry run"
    )
    parser.add_argument("--log", type=Path, help="Write the migration result as JSON")
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Do not copy affected files into .backup before applying",
    )
    args = parser.parse_args()

    root = args.output_dir.expanduser().resolve()
    if not root.is_dir():
        parser.error(f"output directory does not exist: {root}")

    moves = plan_moves(root)
    errors = validate_moves(moves)
    if errors:
        for message in errors:
            print(f"ERROR: {message}", file=sys.stderr)
        return 2

    if not args.apply:
        print(f"Dry run: {len(moves)} items would move")
        for move in moves:
            print(f"{move.source} -> {move.destination}")
        return 0

    timestamp = time.strftime("%Y%m%d-%H%M%S")
    backup_dir = None if args.no_backup else root / ".backup" / f"布局迁移-{timestamp}"
    result = apply_migration(root, moves, backup_dir=backup_dir)
    log_path = args.log or root / "工作日志" / (
        f"布局迁移-{timestamp}.json"
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"Migrated {result['moved']} items; rewrote {result['rewritten']} files"
    )
    print(f"Log: {log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
