# MOC Podcast Listener

[简体中文](README.zh-CN.md) | [English](README.en.md)

A local-first podcast workflow for episode resolution, audio transcription,
timestamped transcripts, and durable Show Notes archiving.

一个本地优先的播客处理 Skill：解析单集、转录音频、生成时间戳转录稿，并完整归档
Show Notes 的文字、链接与图片。

Give it an episode link and it turns a piece of audio into a reusable research
package: a source-linked transcript, timestamp data, subtitles, archived Show
Notes, and clean inputs for an evidence-backed summary. It is practical for
people who listen to long-form podcasts but need to search, quote, review, or
preserve what they heard without repeatedly scrubbing through the audio.

只需提供一个播客链接，它就能把音频整理成可长期使用的资料包：包含原始来源、
带时间戳的转录稿、字幕、Show Notes 图文归档，以及可供 Agent 总结的可靠输入。
它适合经常听长播客、需要检索观点、核对引述、整理知识或保存节目资料的人，
能明显减少下载、转录、复制链接和手工归档的重复劳动。

## 中文简介

MOC Podcast Listener 面向 Codex、OpenClaw 及其他支持 `SKILL.md` 的 Agent。
脚本负责可验证的机械流程，Agent 负责基于证据生成总结：

- 支持小宇宙、Apple Podcasts、Spotify、Overcast、YouTube、Bilibili、
  网易云音乐、喜马拉雅、荔枝 FM、RSS 等来源。
- 默认使用 SenseVoice-Small，失败时降级至 faster-whisper 或
  openai-whisper。
- 生成带节目名称、标题、原始链接、日期、引擎和时间戳的独立转录稿。
- 同时输出 segments JSON 与 SRT，方便引用、检索和二次处理。
- 将 Show Notes 转为 Markdown，保留在线链接并可下载图片。
- 支持本地同步目录、坚果云 WebDAV、S3/R2。
- 长音频会检查字数与时间戳覆盖，拒绝静默复用残缺转录。
- 总结稿只链接独立转录稿，不重复嵌入整篇正文。

完整中文文档见 [README.zh-CN.md](README.zh-CN.md)。

## English Overview

MOC Podcast Listener is designed for Codex, OpenClaw, and other agents that
support `SKILL.md`. Deterministic scripts handle retrieval and transcription;
the agent performs evidence-backed synthesis.

- Resolves episodes from Xiaoyuzhou, Apple Podcasts, Spotify, Overcast,
  YouTube, Bilibili, NetEase Cloud Music, Ximalaya, Lizhi FM, and RSS.
- Uses SenseVoice-Small by default, with faster-whisper and openai-whisper
  fallbacks.
- Produces a self-contained transcript with source metadata and timestamps.
- Exports timestamp segments as JSON and subtitles as SRT.
- Archives Show Notes as Markdown while preserving links and images.
- Supports local sync folders, Nutstore WebDAV, and S3/R2.
- Rejects suspiciously short long-form transcripts and incomplete timestamp
  coverage.
- Keeps transcripts separate from summaries to avoid duplicated artifacts.

See [README.en.md](README.en.md) for the complete English guide.

## Quick Start / 快速开始

Requirements:

- macOS or Linux
- Python 3.10+
- `ffmpeg` and `ffprobe`
- One ASR backend from the provided requirements files

```bash
git clone https://github.com/Superhedgehoger/moc-podcast-listener.git
cd moc-podcast-listener

python3 -m pip install -r requirements.txt
python3 podcast-listener.py "EPISODE_URL"
```

Fast Whisper entry:

```bash
python3 quick-listen.py --model small "EPISODE_URL"
```

Resolve or archive without transcription:

```bash
python3 podcast-listener.py --resolve-only "EPISODE_URL"
python3 podcast-listener.py --archive-only "EPISODE_URL"
```

## Install as a Skill / 安装为 Skill

Codex:

```bash
cp -R . "$HOME/.codex/skills/moc-podcast-listener"
```

OpenClaw:

```bash
cp -R . "$HOME/.openclaw/workspace/skills/moc-podcast-listener"
```

Restart or reload the agent after installation. Do not commit model files,
downloaded audio, generated transcripts, or storage credentials.

## Output / 输出

```text
~/Documents/播客总结/
├── *_metadata.json
├── *_Agent任务指令.txt
├── 转录稿/
│   ├── *_转录稿.txt
│   ├── *_segments.json
│   └── *.srt
├── 总结稿/
│   └── *_详细总结.md
├── Show Notes/
│   ├── *_shownotes.md
│   ├── *_shownotes.raw.html
│   └── *_media-manifest.json
└── 图片/
    └── *_assets/
```

## Validation / 验证

```bash
python3 -m unittest discover -s tests -v
```

Current regression suite: 34 tests.

## Security / 安全

- Credentials are read from environment variables and must not be committed.
- Remote images are checked for private-network targets, content type, and size.
- Temporary audio cleanup uses exact paths rather than wildcard deletion.
- Speaker names are never inferred from anonymous diarization labels.

## License

Licensed under the [MIT License](LICENSE).
