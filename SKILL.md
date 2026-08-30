---
name: moc-podcast-listener
description: Download and transcribe podcast episodes, course lessons, local media, or supported video/audio pages; archive source media and create evidence-backed Chinese summaries. Use when the user provides a podcast/RSS URL, YouTube or Bilibili link, local audio/video course file, podcast title/search terms, or asks to transcribe or summarize spoken long-form content.
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

After resolving a link, the script must look for an official transcript before downloading audio. The priority is publisher/Podcasting 2.0 transcript, platform manual captions, platform automatic captions, then local ASR. A usable official source is saved under the episode package and used directly; audio is downloaded only when official text is unavailable, incomplete, or explicitly needed for diarization. The script writes a readable transcript, timestamp segments, SRT and WebVTT subtitles, metadata, optional Podcasting 2.0 chapters, archived Show Notes, a media manifest, persistent job state, a draft `knowledge.json`, a protected `我的笔记.md`, and an `Agent任务指令.txt` file. Follow the generated instruction file to finish both the report and structured knowledge.

Human-facing folders stay minimal: `转录稿/` contains only readable transcript
text and `总结稿/` contains reports. Treat files under
`资料/<show>_<episode>_<date>/` as the episode's machine-readable package.
Use the automatically maintained `播客索引.md` as the human-facing catalog.
It links each episode's report, transcript, knowledge, personal notes, source page, and metadata, and marks
items as `待总结`, `已完成`, `仅归档`, or `资料不完整`. Completed items are
ordered by report verification time; pending items are ordered by transcription
time. Do not use the episode publication date as the catalog sort key.

YouTube and Bilibili resolution must select an audio-only `yt-dlp` format. Do not fall back to a combined video format. Local course files may be audio or video; video input is converted with `ffmpeg -vn` so only its first audio track enters ASR. Process a multi-lesson course as one task per lesson so retries and reports remain independent. Intermediate audio is removed unless the user requests `--keep-audio`.

Use fast modes when a full ASR run is unnecessary:

```bash
python3 "<skill-directory>/podcast-listener.py" --resolve-only "EPISODE_INPUT"
python3 "<skill-directory>/podcast-listener.py" --archive-only "EPISODE_INPUT"
python3 "<skill-directory>/podcast-listener.py" --force-transcribe "EPISODE_INPUT"
python3 "<skill-directory>/podcast-listener.py" --resume latest
python3 "<skill-directory>/podcast-listener.py" --rebuild-index
python3 "<skill-directory>/podcast-listener.py" --rebuild-knowledge-index
```

Full runs reuse a matching existing transcript by default.
Every non-resolve run creates `.jobs/<job-id>/job.json`, `status.json`, and `result.json` under the output directory. A finished transcript remains `awaiting_report`; it is not a completed podcast task until the report is written and verified.

Use these environment variables only when needed:

- `OUTPUT_DIR`: Override the default `~/Documents/播客总结`.
- `ASR_ENGINE`: Choose `sensevoice`, `whisper`, or `stitch`.
- `WHISPER_MODEL`: Choose the Whisper model; default `large-v3`.
- `COURSE_AUDIO_BITRATE`: AAC bitrate used when extracting local course video audio; default `128k`.
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
- `PREFER_PUBLISHER_TRANSCRIPT=0`: Disable official transcript preference and use cached/local ASR instead.
- `ALLOW_PROXY_FAKE_IP=0`: Disable transparent-proxy domain mappings in `198.18.0.0/15`; literal non-public IP URLs are always blocked.

Read [references/storage-and-speakers.md](references/storage-and-speakers.md) when configuring storage or speaker diarization. Prefer a local sync folder for private, low-cost archives; use S3/R2 only when stable public image URLs are needed.

## Summarize

Read [references/report-workflow.md](references/report-workflow.md) and [references/knowledge-workflow.md](references/knowledge-workflow.md) before producing a report.

- For a course lesson, preserve the same evidence and citation requirements, but organize the report around learning objectives, concepts, demonstrations, procedures, assignments, and unresolved questions rather than pretending it is a podcast interview.

- For transcripts up to 30,000 Chinese characters, synthesize directly from the transcript and timestamp segments.
- For longer transcripts, run `chunk_transcript.py` and perform independent evidence extraction per chunk, followed by one reduce/synthesis pass.
- Never infer a real speaker name from an unlabelled transcript or an anonymous `SPEAKER_00` label. Use `说话人未确认` when identity is not supported.
- Attach timestamps to quotations whenever segment data is available.
- Preserve the archived Show Notes content and online links in the final report. Rebase only local relative asset paths from the Show Notes file location to the report location so archived images remain valid in both files.
- Keep every Show Notes hyperlink in the managed `链接归档` section of `shownotes.md` and in `media-manifest.json`. Preserve the original online URL even when a local snapshot succeeds or fails; do not imply that an online URL alone is a saved webpage.
- Keep the transcript as a separate source document. In the report, describe it and link the transcript, segments JSON, SRT, WebVTT, and archived chapter JSON when present, using paths relative to the report; never embed the complete transcript.
- Name artifacts from the podcast show, episode title, and publication date. Do not add distribution platform names such as Overcast or 小宇宙 to filenames.
- Do not claim that a URL or image was archived unless its manifest entry reports success.
- Complete the episode package's `knowledge.json` together with the report. Every core insight needs evidence and direct quotations must match transcript text and timestamp segments.
- Put `> 转录总结日期：YYYY-MM-DD` immediately after the report title, using the local date when the transcript-based summary is completed, not the episode publication date.
- Never overwrite `我的笔记.md`. AI topics/tags belong in `knowledge.json`; user comments and user tags belong only in the personal notes file.

## Knowledge Library

Read [references/library-workflow.md](references/library-workflow.md) when the user asks to search, monitor subscriptions, or export.

```bash
python3 "<skill-directory>/podcast-search.py" "QUERY" --person "PERSON" --tag "TAG"
python3 "<skill-directory>/podcast-listener.py" --init-subscriptions
python3 "<skill-directory>/podcast-listener.py" --scan-subscriptions
python3 "<skill-directory>/podcast-listener.py" --export all
```

Subscription scans are discovery-only: they may fetch RSS metadata but must not download episode audio or start ASR. Treat `knowledge-index.jsonl`, Briefs, and exports as rebuildable derivatives; the transcript, report, episode package, and personal notes are the source of truth.

## Verify

Before returning:

1. Confirm the report file exists at `report_path` from `result.json`.
2. Confirm required sections from the report workflow are present.
3. Confirm the report begins with a valid `转录总结日期`, contains `关键洞察与证据`, completes `knowledge.json`, and check quotations against transcript text and timestamp segments.
4. Count summary body characters without Show Notes.
5. Confirm the report's transcript, segments, SRT, WebVTT, optional chapter links, and every Show Notes online link are preserved.
6. Report any unavailable images, links, speaker identities, or transcription gaps explicitly.
7. Run the verification command written in `_Agent任务指令.txt`, normally:

```bash
python3 "<skill-directory>/podcast-listener.py" \
  --output-dir "OUTPUT_DIR" --verify "JOB_ID" --require-report
```

Return success only after `status.json` reports `completed`. A status of `awaiting_report` means transcription is done but the requested report or structured evidence is not complete.

## Transcript format

The standalone `_转录稿.txt` is the durable reading and citation source:

- Header: show, episode title, original URL, publication date, duration, ASR engine, language, and generation time.
- Body: timestamped segments in `[HH:MM:SS - HH:MM:SS]` form; anonymous speaker labels appear only when diarization produced them.
- Footer: original episode URL plus relative paths to segments JSON, SRT, WebVTT, metadata, Show Notes, media manifest, and optional chapters, followed by an explicit end marker.

Do not remove the header or footer when reusing a transcript. For evidence extraction, use the segments JSON for exact timestamps and treat the transcript's ASR warning as part of the source limitations.

Temporary audio cleanup is handled by `podcast-listener.py` using exact paths. Do not run wildcard deletion commands.
