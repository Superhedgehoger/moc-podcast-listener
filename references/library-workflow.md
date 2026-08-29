# Local Knowledge Library Workflow

Read this reference when the user asks to search past episodes, manage personal
notes, monitor RSS subscriptions, or export the library.

## Source of truth

The transcript, final report, and `资料/<episode>/` package are authoritative.
`knowledge-index.jsonl`, Briefs, and all exports are derived and may be rebuilt.
Never edit or overwrite `我的笔记.md` during re-transcription, re-summarization,
index rebuilds, subscription scans, or exports.

## Rebuild and search

```bash
python3 "<skill-directory>/podcast-listener.py" --rebuild-knowledge-index
python3 "<skill-directory>/podcast-search.py" "QUERY" \
  --person "PERSON" --tag "TAG" --since YYYY-MM-DD
```

Search is local string matching over show, title, people, topics, AI tags, user
tags, and insight claims. Use `--json` when another agent will consume results.

## RSS Brief

```bash
python3 "<skill-directory>/podcast-listener.py" --init-subscriptions
python3 "<skill-directory>/podcast-listener.py" --scan-subscriptions
```

After initialization, edit `资料/订阅/subscriptions.json`. A scan fetches feed
metadata only, records seen episode IDs, scores configured keywords and feed
priority, rewards publisher-provided transcripts, and writes `资料/Brief/DATE.md`.
It must not download episode audio or start ASR. The user or agent chooses which
candidates become normal transcription jobs.

## Export

```bash
python3 "<skill-directory>/podcast-listener.py" --export all
python3 "<skill-directory>/podcast-listener.py" --export obsidian \
  --export-dir "TARGET"
```

- Obsidian: Markdown notes with frontmatter and links to source artifacts.
- Notion: Markdown pages plus a CSV import index.
- Zotero: CSL-JSON items with source URLs, tags, and evidence in the abstract field.
- NotebookLM: one Markdown source bundle per episode, including the transcript.
- MCP: JSON catalog and JSONL records suitable for a resource server or agent.

Exports are intentionally credential-free. Authentication and upload to an
external service remain separate, explicit actions.
