# Knowledge and Evidence Workflow

Read this file when generating or revising a final report. The human report and
`knowledge.json` are two views of the same synthesis: Markdown is for reading;
JSON is for verification, search, and export.

## Required outcome

After writing the report, replace the episode package's draft `knowledge.json`
with schema version 1 and set `status` to `complete`. Keep `我的笔记.md` untouched.

```json
{
  "schema_version": 1,
  "status": "complete",
  "episode": {
    "show": "Show name",
    "title": "Episode title",
    "url": "https://example.com/episode",
    "publication_date": "20260829"
  },
  "source": {
    "transcript_path": "/absolute/path/to/transcript.txt",
    "report_path": "/absolute/path/to/report.md"
  },
  "topics": ["topic"],
  "entities": [{"name": "Name", "type": "person"}],
  "ai_tags": ["tag"],
  "insights": [
    {
      "id": "insight-01",
      "claim": "A concise evidence-backed claim.",
      "tags": ["topic"],
      "evidence": [
        {
          "kind": "quote",
          "quote": "Exact words from the transcript",
          "start": 123.4,
          "end": 141.8,
          "speaker": null,
          "confidence": "high"
        }
      ]
    }
  ],
  "generated_at": "2026-08-29T12:00:00+08:00"
}
```

## Evidence rules

- Every core insight needs at least one evidence item.
- Use `kind: quote` only for words that occur in the transcript. Preserve the
  wording; normalize only whitespace and obvious line breaks.
- Use `kind: paraphrase` when the report restates a mechanism or combines
  several passages. It still needs a valid timestamp range.
- Use seconds from the beginning of the episode for `start` and `end`.
- A direct quote's timestamp must overlap the segment containing that quote.
- Set `speaker` to `null` unless the transcript or source metadata supports the
  identity. Anonymous diarization labels are allowed but are not real names.
- Confidence describes evidence quality: `high`, `medium`, or `low`.
- AI-generated topics and tags may be regenerated. Never edit or replace
  `我的笔记.md`; user notes and user tags belong only to the user.

## Human report

Include a compact `关键洞察与证据` table or equivalent section with each claim,
its timestamp, and a short exact quote or explicit paraphrase label. Keep the
full transcript separate and retain the existing transcript/source links.
