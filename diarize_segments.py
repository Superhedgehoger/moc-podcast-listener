#!/usr/bin/env python3
"""Optional pyannote speaker diarization and timestamp overlap alignment."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable


def assign_speakers(
    segments: list[dict[str, Any]],
    turns: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    normalized_turns = list(turns)
    assigned = []
    for segment in segments:
        item = dict(segment)
        start = float(item.get("start") or 0.0)
        end = max(start, float(item.get("end") or start))
        best_speaker = None
        best_overlap = 0.0
        for turn in normalized_turns:
            overlap = max(
                0.0,
                min(end, float(turn["end"])) - max(start, float(turn["start"])),
            )
            if overlap > best_overlap:
                best_overlap = overlap
                best_speaker = str(turn["speaker"])
        if best_speaker:
            item["speaker"] = best_speaker
        assigned.append(item)
    return assigned


def _annotation_turns(annotation: Any) -> list[dict[str, Any]]:
    turns = []
    try:
        iterator = annotation.itertracks(yield_label=True)
        for turn, _, speaker in iterator:
            turns.append(
                {"start": float(turn.start), "end": float(turn.end), "speaker": str(speaker)}
            )
    except AttributeError:
        for turn, speaker in annotation:
            turns.append(
                {"start": float(turn.start), "end": float(turn.end), "speaker": str(speaker)}
            )
    return turns


def diarize(
    audio_path: Path,
    segments: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if os.environ.get("DIARIZATION", "0") != "1":
        return segments, {"status": "disabled"}
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_HUB_TOKEN")
    if not token:
        return segments, {"status": "skipped", "reason": "missing Hugging Face token"}

    model = os.environ.get(
        "DIARIZATION_MODEL", "pyannote/speaker-diarization-community-1"
    )
    try:
        from pyannote.audio import Pipeline

        pipeline = Pipeline.from_pretrained(model, token=token)
        options: dict[str, int] = {}
        if os.environ.get("DIARIZATION_MIN_SPEAKERS"):
            options["min_speakers"] = int(os.environ["DIARIZATION_MIN_SPEAKERS"])
        if os.environ.get("DIARIZATION_MAX_SPEAKERS"):
            options["max_speakers"] = int(os.environ["DIARIZATION_MAX_SPEAKERS"])
        output = pipeline(str(audio_path), **options)
        annotation = getattr(output, "exclusive_speaker_diarization", None)
        if annotation is None:
            annotation = getattr(output, "speaker_diarization", output)
        turns = _annotation_turns(annotation)
        return assign_speakers(segments, turns), {
            "status": "complete",
            "model": model,
            "speaker_count": len({turn["speaker"] for turn in turns}),
            "turn_count": len(turns),
        }
    except Exception as exc:
        return segments, {
            "status": "failed",
            "model": model,
            "reason": f"{type(exc).__name__}: {exc}",
        }
