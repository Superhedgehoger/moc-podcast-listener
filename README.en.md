# MOC Podcast Listener

[Home](README.md) | [简体中文](README.zh-CN.md) | **English**

## Overview

MOC Podcast Listener is a practical, local-first skill for turning podcast
episodes into durable research material. It resolves an episode, prefers an
official publisher transcript when available, falls back to local speech
recognition, archives rich Show Notes, and prepares traceable evidence for an
agent-generated summary.

Podcast audio is difficult to search and quote, while images and links in Show
Notes can disappear over time. Give this skill an episode URL and it produces a
durable, reusable research package: a source-linked timestamped transcript,
SRT and WebVTT subtitles, structured segments, archived chapters, Show Notes and media, and clean
evidence for agent-assisted synthesis. It is useful for personal knowledge
management, editorial research, interview citation, accessibility workflows,
and anyone who wants the value of a long-form episode without repeatedly
scrubbing through the audio or organizing files by hand.

The scripts perform deterministic operations:

1. Resolve an episode URL, RSS item, or search query.
2. Extract episode metadata, audio, Show Notes, and speaker candidates.
3. Prefer a Podcasting 2.0 publisher transcript; otherwise download and preprocess audio.
4. Transcribe locally with SenseVoice-Small or Whisper when needed.
5. Write a self-contained transcript, segments JSON, SRT, and WebVTT.
6. Archive chapters, Show Notes, episode artwork, inline images, links, and an auditable media manifest.
7. Persist resumable job state and produce an agent instruction for synthesis.
8. Verify all artifacts after the agent writes the final report.

The final summary links to the independent transcript instead of embedding a
second copy of the complete text.

## Supported Sources

- Xiaoyuzhou
- Apple Podcasts
- Overcast
- Spotify
- Pocket Casts, Castro, and Castbox
- YouTube and Bilibili
- NetEase Cloud Music
- Ximalaya and Lizhi FM
- Listen Notes, Podbean, and iHeart
- RSS/XML feeds
- Show name and episode-title search terms

## Requirements

- Python 3.10+
- `ffmpeg` and `ffprobe`
- SenseVoice, faster-whisper, or openai-whisper
- `yt-dlp` for supported video platforms

Install the complete dependency set:

```bash
python3 -m pip install -r requirements.txt
```

Smaller dependency groups are available in:

- `requirements-base.txt`
- `requirements-sensevoice.txt`
- `requirements-whisper.txt`
- `requirements-storage.txt`
- `requirements-diarization.txt`

## Usage

Clone the repository:

```bash
git clone https://github.com/Superhedgehoger/moc-podcast-listener.git
cd moc-podcast-listener
```

Run a full episode:

```bash
python3 podcast-listener.py "EPISODE_URL"
```

Fast Whisper mode:

```bash
python3 quick-listen.py --model small "EPISODE_URL"
```

Utility modes:

```bash
python3 podcast-listener.py --resolve-only "EPISODE_URL"
python3 podcast-listener.py --archive-only "EPISODE_URL"
python3 podcast-listener.py --force-transcribe "EPISODE_URL"
python3 podcast-listener.py --resume latest
```

Full runs reuse a transcript only when the episode identity matches and the
content passes completeness checks.

Every non-resolve run creates `.jobs/<job-id>/job.json`, `status.json`, and
`result.json`. A full run stops at `awaiting_report` after transcription. Once
the agent has written `report_path`, verify the package and mark it complete:

```bash
python3 podcast-listener.py --output-dir "$HOME/Documents/播客总结" \
  --verify "JOB_ID" --require-report
```

## Transcript Format

The `_转录稿.txt` file is designed to remain useful outside the original
workflow:

```text
# Podcast transcript

- Show
- Episode
- Original URL
- Publication date
- Duration
- ASR engine
- Language
- Generated time

## Transcript

[00:00:03 - 00:00:16]
Recognized text...

## Attachments and source

- Original episode URL
- Segments JSON
- SRT
- WebVTT
- Podcasting 2.0 chapters, when supplied by the feed
- Metadata
- Show Notes
- Media manifest

--- End of transcript ---
```

Use `_segments.json` as the machine-readable source for precise timestamp
verification.

## Show Notes Storage

The default `hybrid` mode downloads images while retaining their source URLs.

```bash
SHOWNOTES_ASSETS=hybrid python3 podcast-listener.py "EPISODE_URL"
```

Low-cost private storage can use a local sync folder:

```bash
SHOWNOTES_SYNC_BACKEND=local \
SHOWNOTES_SYNC_DESTINATION="$HOME/Nutstore Files/Podcast Archive" \
python3 podcast-listener.py "EPISODE_URL"
```

WebDAV and S3/R2 are also supported. Credentials are read only from environment
variables. See [references/storage-and-speakers.md](references/storage-and-speakers.md).

External Show Notes pages remain online links by default. To create bounded,
first-level page snapshots, install SingleFile CLI or ArchiveBox and opt in:

```bash
python3 podcast-listener.py --archive-only --link-snapshot singlefile "EPISODE_URL"
```

Snapshots are disabled by default to keep storage and execution costs low.

## Optional Speaker Diarization

Install the optional dependencies and enable diarization:

```bash
python3 -m pip install -r requirements-diarization.txt
DIARIZATION=1 python3 podcast-listener.py "EPISODE_URL"
```

The output uses anonymous labels such as `SPEAKER_00`. A real person's identity
must not be inferred without independent evidence.

## Install as an Agent Skill

Codex:

```bash
cp -R . "$HOME/.codex/skills/moc-podcast-listener"
```

OpenClaw:

```bash
cp -R . "$HOME/.openclaw/workspace/skills/moc-podcast-listener"
```

Reload the agent after installation.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The regression suite is fully offline; run it to see the current test count.

## Security

- Do not commit access tokens, WebDAV passwords, cloud credentials, models,
  audio, or generated podcast artifacts.
- HTTP downloads validate redirect targets, block private-network addresses,
  and enforce configured size limits. Show Notes images also require a supported type.
- Audio cleanup uses exact paths.
- Cached transcripts are matched by episode ID or URL and checked for
  completeness.
- Domain names mapped by a transparent proxy into `198.18.0.0/15` are supported;
  literal non-public IP URLs remain blocked. Set `ALLOW_PROXY_FAKE_IP=0` for strict mode.

## License

Licensed under the [MIT License](LICENSE).
