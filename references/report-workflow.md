# Report Workflow

Use this reference when generating the final podcast or course-lesson report.

## Evidence extraction

Use [low-context-workflow.md](low-context-workflow.md) for all new summaries, regardless of transcript length. The main session coordinates disk-backed tasks, never the whole transcript. The host starts independent workers with `sessions_spawn` or an equivalent mechanism (current model, concurrency 2). Extraction workers read only their assigned inputs; the writer receives bounded evidence and may retrieve specific source chunks when needed.

For every chunk, independently extract:

- Topics and claims
- Supporting examples, numbers, named entities, and limitations
- Quotations copied exactly with verified timestamps; untimed sources need alignment before workflow v1 can continue
- Books, articles, music, films, podcasts, tools, products, people, and concepts
- Open questions or ambiguous passages

Do not repeatedly rewrite a growing full summary for every chunk. Merge and deduplicate evidence through bounded reduction tasks, then perform one writing task. Keep original evidence IDs, cases, numbers, disagreements and limitations; record a report section or explicit omission reason for every leaf evidence ID in `coverage.json`. Run an independent review only for transcripts longer than 60 minutes, high-stakes subject matter, or when the user explicitly requests deep review. Such review is separate from the mandatory deterministic final verification.

## Quality rules

- Prefer verifiable facts, examples, mechanisms, and numbers over generic observations.
- Attribute a quotation to a named person only when speaker evidence exists. Otherwise write `说话人未确认`.
- Do not fabricate missing background facts. Clearly distinguish transcript content from outside context.
- Treat minimum length as a coverage check, not a target to pad.
- Use original segment start/end boundaries for report and knowledge timestamps; round to whole seconds when displaying them. Disclose coarse segmentation rather than inventing finer sentence locations.
- A body resource URL must occur in the transcript segments. Archive-only URLs are inserted by the assembler in Show Notes.
- Synthesize `内容摘要`, `内容大纲`, `核心观点`, `详细总结`, `关键洞察与证据`, `关键引述`, `背景与术语`, `实用资源`, and `延伸思考与局限` only from the transcript and timestamp segments. Do not use Show Notes as a substitute for listening evidence. Preserve Show Notes only in their archival section.
- If a term, resource, person, timeline item, or claim exists only in Show Notes and cannot be located in the transcript, omit it from the synthesis sections or explicitly list it inside `Show Notes`; do not silently promote it into the summary.

The single source of section requirements is `report_section_minimums(duration)` in `podcast-listener.py`. The coordinator inserts that dictionary as `section_minimums` in the writer input; generation and validation use the same values. Meet each section requirement, not merely an aggregate word count. Preserve source detail without inventing examples to reach a minimum.

## Output

Write the final report to the target path in the generated Agent instruction:

In workflow v1, the independent writer writes only the nine synthesis sections to `body.md`, plus `knowledge.draft.json`, `coverage.json`, and `output.json` beside its task output. Follow `section_minimums` in the task input. `内容摘要` is the quick overview, not a replacement for `详细总结` or the other sections. `assemble` owns the title, local completion date, basic information, archived Show Notes, relative-link rebasing and transcript footer. It publishes the final report and knowledge file; do not edit those published files directly. The core blocks untimed transcripts: preserve official text, obtain reliable timestamp alignment, then prepare again. Never invent timestamps or present this blocked path as validated.

For `local_media` or a user-identified course lesson, use a course-oriented
title and organize the body around learning objectives, concepts, procedures,
demonstrations, exercises, and open questions. Keep the same evidence,
quotation, timestamp, artifact-link, and verification requirements.

```markdown
# 播客代听报告 / 课程转录总结

> 转录总结日期：YYYY-MM-DD

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

Build a chronological outline from transcript/segments. Do not copy the Show Notes timeline.

## 内容摘要

Summarize the episode's actual argument, examples, and progression from the transcript.

## 核心观点

Each major point must include its evidence, example, or reasoning and any relevant limitation.

## 详细总结

Develop the main argument in depth from transcript evidence. Cover mechanisms,
examples, disagreements, transitions, and qualifications rather than repeating
the title or Show Notes description.

## 关键洞察与证据

List the most reusable claims. For each one, include an
explicitly labeled paraphrase, its timestamp range, speaker status, and
confidence. Keep this section synchronized with `knowledge.json`.

## 关键引述

At least three lines, strictly `- [HH:MM:SS]：verbatim text`, checked against original timestamped segments. No commentary or subheadings inside this section; use paraphrases in other body sections. Do not guess speakers or fabricate timestamps.

## 背景与术语

## 实用资源

## 延伸思考与局限

## Show Notes

Insert the complete archived Show Notes Markdown. Preserve its text and online
links, but rebase local relative image or snapshot paths from the archived
`shownotes.md` directory to the final report directory. Do not copy a path
verbatim when that would make the report link resolve to a different file.
Keep the managed `链接归档` section intact: every link must retain its original
online URL, and a local snapshot link may be added only when the manifest marks
that snapshot complete.

## 转录稿

Briefly describe the transcript format and known ASR limitations. Link to the
independent transcript, timestamp segments, SRT, WebVTT, and Podcasting 2.0
chapters when available, using paths relative to the report file. Do not copy
the full transcript into the report. When metadata says the source is
`publisher_transcript`, use `source_kind` to distinguish publisher text,
platform manual captions, and platform automatic captions rather than describing it as ASR.

Example:

- [独立转录稿](<../转录稿/{节目名称}_{播客标题}_{发布日期}_转录稿.txt>)
- [时间戳分段](<../资料/{节目名称}_{播客标题}_{发布日期}/转录数据/segments.json>)
- [SRT 字幕](<../资料/{节目名称}_{播客标题}_{发布日期}/转录数据/transcript.srt>)
- [WebVTT 字幕](<../资料/{节目名称}_{播客标题}_{发布日期}/转录数据/transcript.vtt>)
- [章节数据](<../资料/{节目名称}_{播客标题}_{发布日期}/转录数据/chapters.json>)（存在时）
```

Use the local calendar date on which the transcript-based report was completed,
not the episode publication date. Place `转录总结日期` immediately after the
top-level title. Before finalizing, complete `knowledge.json` according to
`references/knowledge-workflow.md`, leave `我的笔记.md` untouched, verify all
quotations, rebase and preserve image-relative paths, list
failed media downloads from the manifest, and confirm every transcript link
resolves from the report directory. Then run the exact `--verify JOB_ID
--require-report` command from `_Agent任务指令.txt`. Do not report the podcast
task as complete until `.jobs/<job-id>/status.json` says `completed`; a status
of `awaiting_report` means only the transcription phase is complete.
