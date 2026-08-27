# Report Workflow

Use this reference when generating the final podcast or course-lesson report.

## Evidence extraction

For a transcript longer than 30,000 Chinese characters, run:

```bash
python3 "<skill-directory>/chunk_transcript.py" "TRANSCRIPT_PATH"
```

For every chunk, independently extract:

- Topics and claims
- Supporting examples, numbers, named entities, and limitations
- Quotations copied exactly, with timestamps when available
- Books, articles, music, films, podcasts, tools, products, people, and concepts
- Open questions or ambiguous passages

Do not repeatedly rewrite a growing full summary for every chunk. Merge the chunk evidence, deduplicate it, then perform one synthesis pass. Run an independent review only for transcripts longer than 60 minutes, high-stakes subject matter, or when the user explicitly requests deep review.

## Quality rules

- Prefer verifiable facts, examples, mechanisms, and numbers over generic observations.
- Attribute a quotation to a named person only when speaker evidence exists. Otherwise write `说话人未确认`.
- Do not fabricate missing background facts. Clearly distinguish transcript content from outside context.
- Treat minimum length as a coverage check, not a target to pad.

Suggested minimum summary-body length:

| Duration | Minimum |
| --- | ---: |
| Under 30 minutes | 1,200 Chinese characters |
| 30-60 minutes | 2,000 Chinese characters |
| 60-90 minutes | 3,000 Chinese characters |
| Over 90 minutes | 4,000 Chinese characters |

## Output

Write the final report to the target path in the generated Agent instruction:

For `local_media` or a user-identified course lesson, use a course-oriented
title and organize the body around learning objectives, concepts, procedures,
demonstrations, exercises, and open questions. Keep the same evidence,
quotation, timestamp, artifact-link, and verification requirements.

```markdown
# 播客代听报告 / 课程转录总结

## 基本信息

| 字段 | 内容 |
| --- | --- |
| 节目 | ... |
| 标题 | ... |
| 链接 | ... |
| 发布日期 | ... |
| 音频时长 | ... |
| 转录引擎 | ... |

## 内容大纲

## 核心观点

Each major point must include its evidence, example, or reasoning and any relevant limitation.

## 关键引述

Use exact transcript wording. Add `[HH:MM:SS]` when available and do not guess the speaker.

## 背景与术语

## 实用资源

## 延伸思考与局限

## Show Notes

Insert the complete archived Show Notes Markdown. Preserve its text and online
links, but rebase local relative image or snapshot paths from the archived
`shownotes.md` directory to the final report directory. Do not copy a path
verbatim when that would make the report link resolve to a different file.

## 转录稿

Briefly describe the transcript format and known ASR limitations. Link to the
independent transcript, timestamp segments, SRT, WebVTT, and Podcasting 2.0
chapters when available, using paths relative to the report file. Do not copy
the full transcript into the report. When metadata says the source is
`publisher_transcript`, describe it as publisher-provided rather than ASR.

Example:

- [独立转录稿](<../转录稿/{节目名称}_{播客标题}_{发布日期}_转录稿.txt>)
- [时间戳分段](<../资料/{节目名称}_{播客标题}_{发布日期}/转录数据/segments.json>)
- [SRT 字幕](<../资料/{节目名称}_{播客标题}_{发布日期}/转录数据/transcript.srt>)
- [WebVTT 字幕](<../资料/{节目名称}_{播客标题}_{发布日期}/转录数据/transcript.vtt>)
- [章节数据](<../资料/{节目名称}_{播客标题}_{发布日期}/转录数据/chapters.json>)（存在时）
```

Before finalizing, verify all quotations, rebase and preserve image-relative paths, list
failed media downloads from the manifest, and confirm every transcript link
resolves from the report directory. Then run the exact `--verify JOB_ID
--require-report` command from `_Agent任务指令.txt`. Do not report the podcast
task as complete until `.jobs/<job-id>/status.json` says `completed`; a status
of `awaiting_report` means only the transcription phase is complete.
