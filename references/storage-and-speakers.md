# Show Notes Storage And Speaker Labels

## Recommended Storage Order

1. **Local sync folder** is the lowest-maintenance default. Point `SHOWNOTES_SYNC_DESTINATION` at an iCloud Drive, Nutstore/Jianguoyun, Dropbox, OneDrive, or Syncthing folder. Markdown and images stay together and continue to work offline.
2. **Nutstore/Jianguoyun WebDAV** is useful when a local sync client is unavailable. Create a dedicated third-party app password. Never use the account login password in configuration.
3. **S3-compatible storage or Cloudflare R2** is best when images need stable public URLs. Keep the bucket private unless public access is intentional, and set `SHOWNOTES_PUBLIC_BASE_URL` only after a public domain or bucket URL works.
4. **Online-only mode** has zero storage cost but is not archival: publishers can move or remove images and links.

The original source URL is always retained in the Show Notes and media manifest. In `hybrid` mode, failed image downloads fall back to their source URLs.
Manifest schema v2 records each image's source URL, final URL, HTTP status,
fetch time, MIME type, byte size, local path, and failure reason when relevant.

Each episode's machine-readable package is stored under
`资料/<show_title>_<episode_title>_<date>/`. Its `Show Notes/` folder contains
`shownotes.md`, `source.raw.html`, `media-manifest.json`, and `图片/`. The
Markdown uses paths relative to its own folder; when embedding it in a report,
rebase local paths to the report directory.

## Local Sync Folder

```bash
SHOWNOTES_SYNC_BACKEND=local \
SHOWNOTES_SYNC_DESTINATION="$HOME/Nutstore Files/播客归档" \
python3 podcast-listener.py --archive-only "EPISODE_URL"
```

This backend needs no extra Python package. It copies each episode into its own folder and includes the Markdown, raw HTML, image assets, media manifest, and sync manifest.

## Nutstore/Jianguoyun WebDAV

Create a third-party app password in Nutstore security settings, then use:

```bash
SHOWNOTES_SYNC_BACKEND=webdav \
SHOWNOTES_SYNC_DESTINATION="https://dav.jianguoyun.com/dav/播客归档" \
WEBDAV_USERNAME="YOUR_ACCOUNT_EMAIL" \
WEBDAV_PASSWORD="YOUR_APP_PASSWORD" \
python3 podcast-listener.py --archive-only "EPISODE_URL"
```

The destination root folder should already exist. Credentials are read only from environment variables and are never written to manifests.
Do not put credentials in the URL. Chinese folder names are encoded automatically.

## S3 And Cloudflare R2

Install the optional dependency:

```bash
python3 -m pip install -r requirements-storage.txt
```

For generic S3:

```bash
SHOWNOTES_SYNC_BACKEND=s3 \
SHOWNOTES_SYNC_DESTINATION="s3://BUCKET/podcasts" \
AWS_ACCESS_KEY_ID="..." \
AWS_SECRET_ACCESS_KEY="..." \
AWS_REGION="REGION" \
python3 podcast-listener.py --archive-only "EPISODE_URL"
```

For Cloudflare R2, additionally set its S3 endpoint and use region `auto`:

```bash
S3_ENDPOINT_URL="https://ACCOUNT_ID.r2.cloudflarestorage.com" \
AWS_REGION="auto" \
SHOWNOTES_PUBLIC_BASE_URL="https://media.example.com/podcasts" \
python3 podcast-listener.py --archive-only "EPISODE_URL"
```

When `SHOWNOTES_PUBLIC_BASE_URL` is present, the script creates a sibling `_published.md` whose locally downloaded image references use public URLs. The original local Markdown remains unchanged.

Set `SHOWNOTES_SYNC_REQUIRED=1` or pass `--sync-required` when an automation should fail if storage sync fails. The default keeps the local archive usable and reports the sync failure in JSON.

## Optional Link Snapshots

Show Notes hyperlinks remain online URLs by default. Every URL is written to
both the human-readable `链接归档` section in `shownotes.md` and the machine-readable
manifest. This is the lowest-cost
choice and avoids unexpectedly archiving entire websites. When a linked page
is important enough to preserve, install one of these tools and opt in:

```bash
# One self-contained HTML file per first-level link
python3 podcast-listener.py --archive-only \
  --link-snapshot singlefile "EPISODE_URL"

# ArchiveBox collection; default add mode archives only the supplied URL
python3 podcast-listener.py --archive-only \
  --link-snapshot archivebox "EPISODE_URL"
```

The default limit is 10 links per episode. Override it with
`SHOWNOTES_MAX_LINK_SNAPSHOTS`. Snapshot failures do not discard the original
URL and do not fail the podcast job. The manifest records success, output path,
capture time, or failure reason for every attempted link.

Enable snapshots only for links you trust. SingleFile and ArchiveBox run as
external browsers/archivers and may fetch redirects or embedded resources
beyond the built-in downloader's network checks.

Recommended cost policy: use `hybrid` for images, leave link snapshots at
`none`, and selectively rerun valuable episodes with `singlefile`. Sync the
resulting Markdown and sibling asset folders through a local Nutstore,
OneDrive, iCloud Drive, Dropbox, or Syncthing directory.

## Speaker Diarization

Speaker diarization is off by default because it downloads a separate model and needs more compute. `pyannote.audio` 4 requires Python 3.10 or newer:

```bash
python3.12 -m pip install -r requirements-diarization.txt
DIARIZATION=1 HF_TOKEN="..." PYTHON_BIN=python3.12 \
./listen-and-summarize.sh "EPISODE_URL"
```

Before first use, accept the access conditions for `pyannote/speaker-diarization-community-1` on Hugging Face. Optional bounds:

- `DIARIZATION_MIN_SPEAKERS`
- `DIARIZATION_MAX_SPEAKERS`
- `DIARIZATION_MODEL`

Output labels such as `SPEAKER_00` identify consistent voices, not real names. Map them to a guest or host only when the audio, transcript, or metadata provides explicit evidence.
When a matching transcript is cached, diarization reuses the text and downloads only the audio; it does not rerun ASR.
