---
name: moc-podcast-listener
description: Download and transcribe podcast episodes or supported video/audio pages, archive Show Notes with images and links, and create evidence-backed Chinese summaries. Use when the user provides a podcast episode URL, RSS feed, YouTube or supported Chinese media URL, podcast title/search terms, or asks to listen to, transcribe, archive, or summarize a podcast episode.
---

# Podcast Listener

Use the bundled scripts for deterministic retrieval, transcription, Show Notes archiving, and transcript chunking. Perform synthesis in the current agent after the scripts finish.

## Run

1. Resolve the absolute path of this skill directory.
2. Pass the user's complete rich-text input to `podcast-listener.py`; do not strip a supplied title from its URL.
3. Run:

```bash
python3 "<skill-directory>/podcast-listener.py" "EPISODE_URL_OR_SEARCH_TERMS"
```

The script first uses a publisher-provided Podcasting 2.0 transcript when one is available, then falls back to local ASR. It writes a transcript, timestamp segments, SRT and WebVTT subtitles, metadata, optional Podcasting 2.0 chapters, archived Show Notes, a media manifest, persistent job state, and an `_Agent任务指令.txt` file. Follow the generated instruction file to finish the report.

Use fast modes when a full ASR run is unnecessary:

```bash
python3 "<skill-directory>/podcast-listener.py" --resolve-only "EPISODE_INPUT"
python3 "<skill-directory>/podcast-listener.py" --archive-only "EPISODE_INPUT"
python3 "<skill-directory>/podcast-listener.py" --force-transcribe "EPISODE_INPUT"
python3 "<skill-directory>/podcast-listener.py" --resume latest
```

Full runs reuse a matching existing transcript by default.
Every non-resolve run creates `.jobs/<job-id>/job.json`, `status.json`, and `result.json` under the output directory. A finished transcript remains `awaiting_report`; it is not a completed podcast task until the report is written and verified.

Use these environment variables only when needed:

- `OUTPUT_DIR`: Override the default `~/Documents/播客总结`.
- `ASR_ENGINE`: Choose `sensevoice`, `whisper`, or `stitch`.
- `WHISPER_MODEL`: Choose the Whisper model; default `large-v3`.
- `KEEP_AUDIO=1`: Preserve downloaded audio and WAV files.
- `FORCE_TRANSCRIBE=1`: Ignore a matching cached transcript.
- `SHOWNOTES_ASSETS`: Choose `hybrid`, `online`, `local`, or `off`; default `hybrid`.
- `SHOWNOTES_MAX_IMAGES`: Maximum images downloaded per episode; default `40`.
- `SHOWNOTES_MAX_IMAGE_BYTES`: Maximum bytes per image; default `15728640`.
- `SHOWNOTES_LINK_SNAPSHOT`: Choose `none`, `singlefile`, or `archivebox`; default `none`.
- `SHOWNOTES_MAX_LINK_SNAPSHOTS`: Maximum first-level links archived per episode; default `10`.
- `SHOWNOTES_SYNC_BACKEND`: Optional `local`, `webdav`, or `s3` storage.
- `SHOWNOTES_SYNC_DESTINATION`: Sync folder, WebDAV URL, or `s3://bucket/prefix`.
- `SHOWNOTES_PUBLIC_BASE_URL`: Optional public base URL used in a generated `_published.md`.
- `SHOWNOTES_SYNC_REQUIRED=1`: Exit with failure when configured storage sync fails.
- `DIARIZATION=1`: Add anonymous speaker labels using optional pyannote dependencies.
- `PREFER_PUBLISHER_TRANSCRIPT=0`: Disable publisher transcript preference and use cached/local ASR instead.
- `ALLOW_PROXY_FAKE_IP=0`: Disable transparent-proxy domain mappings in `198.18.0.0/15`; literal non-public IP URLs are always blocked.

Read [references/storage-and-speakers.md](references/storage-and-speakers.md) when configuring storage or speaker diarization. Prefer a local sync folder for private, low-cost archives; use S3/R2 only when stable public image URLs are needed.

## Summarize

Read [references/report-workflow.md](references/report-workflow.md) before producing a report.

- For transcripts up to 30,000 Chinese characters, synthesize directly from the transcript and timestamp segments.
- For longer transcripts, run `chunk_transcript.py` and perform independent evidence extraction per chunk, followed by one reduce/synthesis pass.
- Never infer a real speaker name from an unlabelled transcript or an anonymous `SPEAKER_00` label. Use `说话人未确认` when identity is not supported.
- Attach timestamps to quotations whenever segment data is available.
- Preserve the archived Show Notes Markdown verbatim in the final report so relative image paths remain valid.
- Keep the transcript as a separate source document. In the report, describe it and link the transcript, segments JSON, SRT, WebVTT, and archived chapter JSON when present, using paths relative to the report; never embed the complete transcript.
- Name artifacts from the podcast show, episode title, and publication date. Do not add distribution platform names such as Overcast or 小宇宙 to filenames.
- Do not claim that a URL or image was archived unless its manifest entry reports success.

## Verify

Before returning:

1. Confirm the report file exists at `report_path` from `result.json`.
2. Confirm required sections from the report workflow are present.
3. Check quotations against transcript text and timestamp segments.
4. Count summary body characters without Show Notes.
5. Confirm the report's transcript, segments, SRT, WebVTT, and optional chapter links resolve.
6. Report any unavailable images, links, speaker identities, or transcription gaps explicitly.
7. Run the verification command written in `_Agent任务指令.txt`, normally:

```bash
python3 "<skill-directory>/podcast-listener.py" \
  --output-dir "OUTPUT_DIR" --verify "JOB_ID" --require-report
```

Return success only after `status.json` reports `completed`. A status of `awaiting_report` means transcription is done but the requested report is not.

## Transcript format

The standalone `_转录稿.txt` is the durable reading and citation source:

- Header: show, episode title, original URL, publication date, duration, ASR engine, language, and generation time.
- Body: timestamped segments in `[HH:MM:SS - HH:MM:SS]` form; anonymous speaker labels appear only when diarization produced them.
- Footer: original episode URL plus relative paths to segments JSON, SRT, WebVTT, metadata, Show Notes, media manifest, and optional chapters, followed by an explicit end marker.

Do not remove the header or footer when reusing a transcript. For evidence extraction, use the segments JSON for exact timestamps and treat the transcript's ASR warning as part of the source limitations.

Temporary audio cleanup is handled by `podcast-listener.py` using exact paths. Do not run wildcard deletion commands.
