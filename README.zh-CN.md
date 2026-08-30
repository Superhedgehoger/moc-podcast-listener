# 播客代听助手

**简体中文** | [English](README.en.md) | [多语言首页](README.md)

> 版本：v4.16.0
> 核心流程：输入单集或课时 → 优先复用转录/仅提取音轨 → 来源归档 → 证据化总结 → 个人笔记 → 检索与导出

很多播客值得反复查阅，但音频不方便搜索，Show Notes 中的图片和链接也可能失效。
这个 Skill 的实际作用，是让你只提供一个单集链接，就自动得到一套可保存、可检索、
可引用的播客资料：带来源和时间戳的完整转录稿、SRT/WebVTT 字幕、结构化分段、Show Notes
图文归档、可核验的关键洞察、不会被 Agent 覆盖的个人笔记，以及可供检索和导出的本地知识索引。无论是个人知识管理、内容研究、
访谈引用还是长期收藏，都不必再手工下载音频、复制链接和整理附件。

项目先生成可独立阅读和追溯的转录稿。深度总结由 Agent 按 `SKILL.md` 和
`references/report-workflow.md` 读取证据后完成；总结稿只链接转录稿，不再复制
整篇转录正文。这样既减少重复文件，也让原始材料与观点整理保持清晰边界。

输出目录以人类阅读为先：`转录稿/` 只放可直接阅读的 `.txt`，`总结稿/` 只放
最终总结；字幕、JSON、图片和任务状态统一收进每期 `资料/` 包。根目录的
`播客索引.md` 是统一阅读入口，按总结完成时间列出节目、单集和处理状态，并直接链接总结稿、
转录稿、知识、个人笔记、资料包和原始页面。


---

## 文件说明

| 文件/目录 | 用途 |
|------|------|
| `SKILL.md` | 精简的 Agent 执行入口 |
| `references/report-workflow.md` | 按需加载的总结结构和证据规则 |
| `references/knowledge-workflow.md` | `knowledge.json` 结构、引述和时间戳核验规则 |
| `references/library-workflow.md` | 订阅、搜索、个人笔记和导出工作流 |
| `podcast-listener.py` | 解析播客链接/名称、下载音频、预处理、转录、归档 Show Notes、输出 Agent 指令 |
| `podcast-search.py` | 搜索本地知识索引，不依赖向量数据库 |
| `podcast-export.py` | 导出 Obsidian、Notion、Zotero、NotebookLM 和 MCP 文件 |
| `knowledge_base.py` | 证据校验、个人笔记模板、知识索引和搜索核心 |
| `subscription_manager.py` | RSS 订阅去重、评分和低成本 Brief |
| `scripts/backfill_summary_dates.py` | 为历史总结补录转录总结日期，默认预览、写入前备份 |
| `scripts/backfill_shownotes_links.py` | 为历史 Show Notes 清洗并补齐人类可读链接归档 |
| `listen-and-summarize.sh` | 一键转录入口，结束后打印 Agent 后续任务指令 |
| `chunk_transcript.py` | 超长转录稿分块工具，用于逐块证据提取 |
| `quick-listen.py` | 快速入口，复用主流程并默认使用 Whisper small |
| `media_store.py` | 将 Show Notes 和图片同步到本地同步盘、WebDAV 或 S3/R2 |
| `diarize_segments.py` | 可选的说话人分离与时间戳对齐 |
| `requirements.txt` | SenseVoice、Whisper、VAD 和 yt-dlp 依赖 |
| `requirements-storage.txt` | 可选的 S3/R2 同步依赖 |
| `requirements-diarization.txt` | 可选的 pyannote 说话人识别依赖 |
| `tests/` | 不联网的平台解析 fixture 测试 |

---

## 使用方法

克隆仓库：

```bash
git clone https://github.com/Superhedgehoger/moc-podcast-listener.git
cd moc-podcast-listener
```

处理单集：

```bash
./listen-and-summarize.sh "https://www.xiaoyuzhoufm.com/episode/xxxxxxxxxxxxxxxxxxxxxxxx"
./listen-and-summarize.sh "https://open.spotify.com/episode/xxxxxxxxxxxxxxxxxxxxxxxx"
./listen-and-summarize.sh "https://www.youtube.com/watch?v=xxxxxxxxxxx"
./listen-and-summarize.sh "https://overcast.fm/+abcdef"
./listen-and-summarize.sh "https://www.bilibili.com/video/BVxxxxxxxxx"
./listen-and-summarize.sh "$HOME/Courses/课程一/第01课.mp4"
./listen-and-summarize.sh "https://music.163.com/#/program?id=xxxxxxxx"
./listen-and-summarize.sh "https://www.ximalaya.com/sound/xxxxxxxx"
./listen-and-summarize.sh "https://www.lizhi.fm/xxxxxxxx/xxxxxxxx"
./listen-and-summarize.sh "节目名 单集标题关键词"
```

支持输入：

- 小宇宙、Apple Podcasts、Overcast、Podwise.ai、Spotify、Pocket Casts、Castro、Castbox、YouTube、Bilibili、网易云音乐、喜马拉雅、荔枝 FM、Listen Notes、Podbean、iHeart 链接
- RSS/XML/feed 链接
- 本地课程音频与视频：MP3、M4A、WAV、FLAC、OGG、Opus、MP4、MKV、MOV、WebM、M4V、AVI、TS
- 节目名 + 单集标题关键词搜索

YouTube 和 Bilibili 通过 `yt-dlp` 只选择独立音频流，不下载视频画面。输入本地课程视频时，
脚本使用 `ffmpeg -vn` 只提取第一条音轨；中间音频在完成后默认删除。多课时课程建议每节课
分别运行一次，这样每节都有独立转录稿、字幕、总结、状态和恢复点。需要保留提取出的音频时
再加 `--keep-audio`。

快速检查或只归档 Show Notes：

```bash
python3 podcast-listener.py --resolve-only "单集链接"
python3 podcast-listener.py --archive-only "单集链接"
python3 podcast-listener.py --force-transcribe "单集链接"
python3 podcast-listener.py --resume latest
python3 podcast-listener.py --rebuild-index
python3 podcast-listener.py --rebuild-knowledge-index
python3 podcast-listener.py --archive-only \
  --sync-backend local --sync-destination "$HOME/Nutstore Files/播客归档" "单集链接"
```

完整运行默认复用 URL 或单集 ID 匹配的已有转录稿；使用 `--force-transcribe` 强制重转。
拿到链接后会先查找官方 Transcript。优先级依次为：发布方 RSS / Podcasting 2.0
Transcript、YouTube/Bilibili 人工字幕、平台自动字幕、本地 SenseVoice/Whisper。
官方文本通过完整性检查后会保存原文件并直接使用，不再下载音频；只有缺失、不完整，
或明确需要说话人识别时才获取音频。

每次非 `--resolve-only` 运行都会创建持久化作业。转录产物写完后状态是
`awaiting_report`，表示“转录完成、总结未完成”；按任务指令生成总结并核验后才是
`completed`：

```bash
python3 podcast-listener.py --output-dir "$HOME/Documents/播客总结" \
  --verify "JOB_ID" --require-report
```

每次归档、转录或核验都会自动更新 `播客索引.md`。手动移动或补齐历史文件后，可运行
`--rebuild-index` 根据 `资料/` 中的元数据重新生成索引；它不会联网，也不会重新转录。

本地知识库、订阅和导出：

```bash
# 为历史资料补齐 knowledge.json / 我的笔记.md，并重建 JSONL 索引
python3 podcast-listener.py --rebuild-knowledge-index

# 搜索主题、人物或标签
python3 podcast-search.py "AI Agent" --person "Sam Altman" --since 2026-01-01
python3 podcast-search.py --tag "待复习" --json

# 初始化订阅配置；编辑 subscriptions.json 后只扫描 RSS，不自动转录
python3 podcast-listener.py --init-subscriptions
python3 podcast-listener.py --scan-subscriptions

# 导出全部或单一格式
python3 podcast-listener.py --export all
python3 podcast-listener.py --export obsidian --export-dir "$HOME/Obsidian/Podcast"
```

`我的笔记.md` 中 `- 用户标签：` 一行可写 `#待复习 #研究素材`。AI 标签保存在
`knowledge.json`，可重新生成；用户标签和正文笔记只属于用户，任何自动流程都不得覆盖。

处理完成后输出目录结构如下（`总结稿/` 中的最终报告由 Agent 后续写入）：

```text
~/Documents/播客总结/
├── 播客索引.md
├── .jobs/{job-id}/
│   ├── job.json
│   ├── status.json
│   ├── result.json
│   └── verification.json
├── 转录稿/
│   └── {节目名称}_{播客标题}_{发布日期}_转录稿.txt
├── 总结稿/
│   └── {节目名称}_{播客标题}_{发布日期}_详细总结.md
└── 资料/
    ├── knowledge-index.jsonl
    ├── 订阅/
    │   ├── subscriptions.json
    │   └── state.json
    ├── Brief/
    ├── 导出/
    └── {节目名称}_{播客标题}_{发布日期}/
        ├── metadata.json
        ├── knowledge.json
        ├── 我的笔记.md
        ├── Agent任务指令.txt
        ├── 转录数据/
        │   ├── segments.json
        │   ├── transcript.srt
        │   ├── transcript.vtt
        │   ├── chapters.json（RSS 提供时）
        │   └── 分块/（超长转录需要时）
        └── Show Notes/
            ├── shownotes.md
            ├── source.raw.html
            ├── media-manifest.json
            ├── 图片/
            └── 链接快照/（显式启用时）
```

把终端打印出的 Agent 任务指令交给 Agent 继续执行即可。最终报告由 Agent 写入：

```text
~/Documents/播客总结/总结稿/
└── {节目名称}_{播客标题}_{发布日期}_详细总结.md
```

总结稿中的「转录稿」章节只介绍独立转录文件，并使用相对路径链接人类可读的
`_转录稿.txt`，以及 `资料/` 中的 segments、SRT、VTT 和可选章节 JSON。
完整转录正文不会再次嵌入总结稿。

新作业的总结完成条件更严格：Agent 还必须同步完成 `knowledge.json`，每条核心洞察至少
提供一条引述或转述证据；直接引述需要同时匹配转录稿正文和对应时间戳 segments。报告中
必须保留「关键洞察与证据」章节。`--verify --require-report` 全部通过后，作业才是完成状态。

总结稿标题下一行还会记录 `转录总结日期`。该日期表示基于转录稿完成总结的本地日期，
不是节目发布日期。历史总结和 Show Notes 链接可以先预览、再备份写入：

```bash
python3 scripts/backfill_summary_dates.py "$HOME/Documents/播客总结"
python3 scripts/backfill_summary_dates.py "$HOME/Documents/播客总结" --apply
python3 scripts/backfill_shownotes_links.py "$HOME/Documents/播客总结"
python3 scripts/backfill_shownotes_links.py "$HOME/Documents/播客总结" --apply
```

旧版本生成的平铺目录可先预览、再迁移。正式迁移默认会把受影响文件备份到
`.backup/`，并同步修正转录稿、总结稿、Manifest 与作业状态中的本地路径：

```bash
python3 scripts/migrate_output_layout.py "$HOME/Documents/播客总结"
python3 scripts/migrate_output_layout.py "$HOME/Documents/播客总结" --apply
```

---

## 转录流程

```text
解析链接或名称
  ↓
提取官方 Transcript、音频 URL、章节、Show Notes、嘉宾/说话人候选
  ↓
优先保存并使用发布方或平台 VTT/SRT/JSON 转录；不可用时下载音频
  ↓
ffmpeg 转换为单声道 WAV；VAD 需要时内部生成 16kHz 临时副本
  ↓
SenseVoice-Small 转录（默认，模型单例缓存）→ faster-whisper/openai-whisper 备用
  ↓
Show Notes 转 Markdown；单集封面始终进入 manifest，封面和正文图片进入每期资料包，链接保留原 URL
  ↓
保存转录稿、时间戳、SRT、WebVTT、章节、元数据、作业状态和 Agent 指令
```

Whisper 会使用 `initial_prompt` 注入节目标题和嘉宾/说话人候选，减少人名、书名、产品名和专业术语的识别错误。

### 转录稿结构

`_转录稿.txt` 本身包含完整上下文，不依赖文件名猜测来源：

```text
# 播客转录稿

- 节目：...
- 单集：...
- 原始链接：https://...
- 发布日期：...
- 音频时长：...
- 转录引擎：...
- 语言：...
- 生成时间：...

## 转录正文

[00:00:03 - 00:00:16]
转录文本……

## 附件与来源

- 原始页面：https://...
- 时间戳分段：`../资料/{节目名称}_{播客标题}_{发布日期}/转录数据/segments.json`
- SRT 字幕：`../资料/{节目名称}_{播客标题}_{发布日期}/转录数据/transcript.srt`
- WebVTT 字幕：`../资料/{节目名称}_{播客标题}_{发布日期}/转录数据/transcript.vtt`
- Podcasting 2.0 章节：同一资料包中的 `chapters.json`（存在时）
- 元数据：`../资料/{节目名称}_{播客标题}_{发布日期}/metadata.json`
- Show Notes：`../资料/{节目名称}_{播客标题}_{发布日期}/Show Notes/shownotes.md`

--- 转录稿结束 ---
```

时间戳正文便于阅读和引用；机器处理仍以资料包中的 `segments.json` 为准。启用说话人分离后，
正文只展示匿名标签（例如 `SPEAKER_00`），不会自动猜测真实姓名。

---

## 超长转录稿分块

转录稿超过 30000 字时，按 `SKILL.md` 推荐使用证据提取流程。可先运行：

```bash
python3 chunk_transcript.py "$HOME/Documents/播客总结/转录稿/{节目名称}_{播客标题}_{发布日期}_转录稿.txt"
```

默认每块约 8000 字，并从第二块开始附带上一块结尾 400 字上下文。工具会生成：

```text
{标题}_转录稿_chunks/
├── chunk_01.txt
├── chunk_02.txt
└── REFINE_MANIFEST.md
```

Agent 应对每块独立提取观点、证据、实体和引述，合并去重后只生成一次正式报告。

---

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `WHISPER_MODEL` | `large-v3` | 首选 Whisper 模型 |
| `OUTPUT_DIR` | `~/Documents/播客总结` | 输出目录 |
| `KEEP_AUDIO` | `0` | 设为 `1` 时保留临时音频和 WAV |
| `FORCE_TRANSCRIBE` | `0` | 设为 `1` 时忽略匹配缓存并重新转录 |
| `ASR_ENGINE` | `sensevoice` | `sensevoice`、`whisper` 或 `stitch` |
| `DOWNLOAD_TIMEOUT` | `1800` | 下载超时秒数 |
| `PREPROCESS_TIMEOUT` | `1800` | 音频预处理超时秒数 |
| `SHOWNOTES_ASSETS` | `hybrid` | `hybrid` 下载图片并保留在线兜底；`online` 只保留在线链接；`local` 强制本地优先；`off` 关闭归档 |
| `SHOWNOTES_MAX_IMAGES` | `40` | 每期最多下载的 Show Notes 图片数 |
| `SHOWNOTES_MAX_IMAGE_BYTES` | `15728640` | 单张图片最大字节数 |
| `SHOWNOTES_LINK_SNAPSHOT` | `none` | `none`、`singlefile` 或 `archivebox`；仅归档 Show Notes 中的一级外链 |
| `SHOWNOTES_MAX_LINK_SNAPSHOTS` | `10` | 每期最多保存的外链网页数 |
| `SHOWNOTES_SYNC_BACKEND` | `none` | `local`、`webdav` 或 `s3`；未配置时不执行同步 |
| `SHOWNOTES_SYNC_DESTINATION` | 无 | 本地同步目录、WebDAV URL 或 `s3://bucket/prefix` |
| `SHOWNOTES_PUBLIC_BASE_URL` | 无 | 生成公开图片链接版 `_published.md` 的 URL 基址 |
| `SHOWNOTES_SYNC_REQUIRED` | `0` | 设为 `1` 时同步失败即退出，适合自动化 |
| `WEBDAV_USERNAME` / `WEBDAV_PASSWORD` | 无 | WebDAV 账号和第三方应用密码，不写入清单 |
| `S3_ENDPOINT_URL` | AWS 默认 | R2 等 S3 兼容服务的 endpoint |
| `DIARIZATION` | `0` | 设为 `1` 时用 pyannote 添加匿名说话人标签 |
| `HF_TOKEN` | 无 | pyannote 模型访问令牌 |
| `PREFER_PUBLISHER_TRANSCRIPT` | `1` | 设为 `0` 时禁用官方 Transcript 优先策略 |
| `ALLOW_PROXY_FAKE_IP` | `1` | 兼容 Clash/Surge 类透明代理的 `198.18.0.0/15` 域名映射；设为 `0` 可严格禁用 |
| `PYTHON_BIN` | `python3` | shell 入口使用的 Python；说话人识别建议 `python3.12` |

存储选择与完整配置见 [`references/storage-and-speakers.md`](references/storage-and-speakers.md)。日常私有归档优先选择本地同步文件夹；只有需要长期可访问的在线图片 URL 时，再配置 S3/R2 和公开域名。

---

## 依赖

```bash
brew install ffmpeg
pip3 install -r requirements.txt
```

也可只安装需要的转录链路：

```bash
pip3 install -r requirements-base.txt -r requirements-sensevoice.txt
pip3 install -r requirements-base.txt -r requirements-whisper.txt
pip3 install -r requirements-storage.txt
python3.12 -m pip install -r requirements-diarization.txt
```

`quick-listen.py` 可直接使用较快配置：

```bash
python3 quick-listen.py "节目名 单集标题关键词"
python3 quick-listen.py --engine stitch --model small "长播客链接"
```

运行测试：

```bash
python3 -m unittest discover -s tests -v
```

---

## 设计原则

- 脚本只负责可验证的机械步骤：解析、下载、预处理、转录、归档和输出指令。
- 转录稿必须独立可读，头部保留来源信息，正文保留时间戳，尾部保留附件索引和结束标记。
- 总结由 Agent 直接读取转录稿完成，避免外部 API 失败导致流程中断。
- 总结稿不得嵌入完整转录正文，只能介绍并链接独立转录稿、segments 和 SRT。
- 超长文本逐块独立提取证据，合并后只生成一次报告，避免反复重写。
- 最终报告必须核对引述；没有说话人证据时不得猜测姓名。
- RSS 有明确目标单集但匹配失败时必须报错，禁止静默使用最新一集。
