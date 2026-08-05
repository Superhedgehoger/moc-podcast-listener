# 播客代听助手

**简体中文** | [English](README.en.md) | [多语言首页](README.md)

> 版本：v4.4  
> 核心流程：IM 输入 → 平台解析 → 音频下载 → SenseVoice/Whisper 转录 → Show Notes 归档 → Agent 直接总结

很多播客值得反复查阅，但音频不方便搜索，Show Notes 中的图片和链接也可能失效。
这个 Skill 的实际作用，是让你只提供一个单集链接，就自动得到一套可保存、可检索、
可引用的播客资料：带来源和时间戳的完整转录稿、SRT 字幕、结构化分段、Show Notes
图文归档，以及供 Agent 生成总结的可靠输入。无论是个人知识管理、内容研究、
访谈引用还是长期收藏，都不必再手工下载音频、复制链接和整理附件。

项目先生成可独立阅读和追溯的转录稿。深度总结由 Agent 按 `SKILL.md` 和
`references/report-workflow.md` 读取证据后完成；总结稿只链接转录稿，不再复制
整篇转录正文。这样既减少重复文件，也让原始材料与观点整理保持清晰边界。


---

## 文件说明

| 文件/目录 | 用途 |
|------|------|
| `SKILL.md` | 精简的 Agent 执行入口 |
| `references/report-workflow.md` | 按需加载的总结结构和证据规则 |
| `podcast-listener.py` | 解析播客链接/名称、下载音频、预处理、转录、归档 Show Notes、输出 Agent 指令 |
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
./listen-and-summarize.sh "https://music.163.com/#/program?id=xxxxxxxx"
./listen-and-summarize.sh "https://www.ximalaya.com/sound/xxxxxxxx"
./listen-and-summarize.sh "https://www.lizhi.fm/xxxxxxxx/xxxxxxxx"
./listen-and-summarize.sh "节目名 单集标题关键词"
```

支持输入：

- 小宇宙、Apple Podcasts、Overcast、Podwise.ai、Spotify、Pocket Casts、Castro、Castbox、YouTube、Bilibili、网易云音乐、喜马拉雅、荔枝 FM、Listen Notes、Podbean、iHeart 链接
- RSS/XML/feed 链接
- 节目名 + 单集标题关键词搜索

快速检查或只归档 Show Notes：

```bash
python3 podcast-listener.py --resolve-only "单集链接"
python3 podcast-listener.py --archive-only "单集链接"
python3 podcast-listener.py --force-transcribe "单集链接"
python3 podcast-listener.py --archive-only \
  --sync-backend local --sync-destination "$HOME/Nutstore Files/播客归档" "单集链接"
```

完整运行默认复用 URL 或单集 ID 匹配的已有转录稿；使用 `--force-transcribe` 强制重转。

处理完成后输出目录结构如下（`总结稿/` 中的最终报告由 Agent 后续写入）：

```text
/Users/djy/Documents/播客总结/
├── {节目名称}_{播客标题}_{发布日期}_metadata.json
├── {节目名称}_{播客标题}_{发布日期}_Agent任务指令.txt
├── 音频/
│   ├── {节目名称}_{播客标题}_{发布日期}.m4a (或 .mp3)
│   └── {节目名称}_{播客标题}_{发布日期}.wav
├── 转录稿/
│   ├── {节目名称}_{播客标题}_{发布日期}_转录稿.txt
│   ├── {节目名称}_{播客标题}_{发布日期}_segments.json
│   └── {节目名称}_{播客标题}_{发布日期}.srt
├── 总结稿/
│   └── {节目名称}_{播客标题}_{发布日期}_详细总结.md
├── Show Notes/
│   ├── {节目名称}_{播客标题}_{发布日期}_shownotes.md
│   ├── {节目名称}_{播客标题}_{发布日期}_shownotes.raw.html
│   └── {节目名称}_{播客标题}_{发布日期}_media-manifest.json
└── 图片/
    └── {节目名称}_{播客标题}_{发布日期}_assets/
        └── image-01-{hash}.jpg
```

把终端打印出的 Agent 任务指令交给 Agent 继续执行即可。最终报告由 Agent 写入：

```text
/Users/djy/Documents/播客总结/总结稿/
└── {节目名称}_{播客标题}_{发布日期}_详细总结.md
```

总结稿中的「转录稿」章节只介绍独立转录文件，并使用相对路径链接
`_转录稿.txt`、`_segments.json` 和 `.srt`。完整转录正文不会再次嵌入总结稿。

---

## 转录流程

```text
解析链接或名称
  ↓
提取音频 URL、标题、Show Notes、嘉宾/说话人候选
  ↓
下载音频
  ↓
ffmpeg 转换为单声道 WAV；VAD 需要时内部生成 16kHz 临时副本
  ↓
SenseVoice-Small 转录（默认，模型单例缓存）→ faster-whisper/openai-whisper 备用
  ↓
Show Notes 转 Markdown，图片下载到同级「图片」目录，链接保留原 URL
  ↓
保存转录稿、时间戳、SRT、元数据、Agent 指令
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
- 时间戳分段：同目录 segments.json
- SRT 字幕：同目录 .srt
- 元数据：上级目录 metadata.json
- Show Notes：上级目录 Show Notes/...

--- 转录稿结束 ---
```

时间戳正文便于阅读和引用；机器处理仍以 `_segments.json` 为准。启用说话人分离后，
正文只展示匿名标签（例如 `SPEAKER_00`），不会自动猜测真实姓名。

---

## 超长转录稿分块

转录稿超过 30000 字时，按 `SKILL.md` 推荐使用证据提取流程。可先运行：

```bash
python3 chunk_transcript.py "/Users/djy/Documents/播客总结/转录稿/{节目名称}_{播客标题}_{发布日期}_转录稿.txt"
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
| `OUTPUT_DIR` | `/Users/djy/Documents/播客总结` | 输出目录 |
| `KEEP_AUDIO` | `0` | 设为 `1` 时保留临时音频和 WAV |
| `FORCE_TRANSCRIBE` | `0` | 设为 `1` 时忽略匹配缓存并重新转录 |
| `ASR_ENGINE` | `sensevoice` | `sensevoice`、`whisper` 或 `stitch` |
| `DOWNLOAD_TIMEOUT` | `1800` | 下载超时秒数 |
| `PREPROCESS_TIMEOUT` | `1800` | 音频预处理超时秒数 |
| `SHOWNOTES_ASSETS` | `hybrid` | `hybrid` 下载图片并保留在线兜底；`online` 只保留在线链接；`local` 强制本地优先；`off` 关闭归档 |
| `SHOWNOTES_MAX_IMAGES` | `40` | 每期最多下载的 Show Notes 图片数 |
| `SHOWNOTES_MAX_IMAGE_BYTES` | `15728640` | 单张图片最大字节数 |
| `SHOWNOTES_SYNC_BACKEND` | `none` | `local`、`webdav` 或 `s3`；未配置时不执行同步 |
| `SHOWNOTES_SYNC_DESTINATION` | 无 | 本地同步目录、WebDAV URL 或 `s3://bucket/prefix` |
| `SHOWNOTES_PUBLIC_BASE_URL` | 无 | 生成公开图片链接版 `_published.md` 的 URL 基址 |
| `SHOWNOTES_SYNC_REQUIRED` | `0` | 设为 `1` 时同步失败即退出，适合自动化 |
| `WEBDAV_USERNAME` / `WEBDAV_PASSWORD` | 无 | WebDAV 账号和第三方应用密码，不写入清单 |
| `S3_ENDPOINT_URL` | AWS 默认 | R2 等 S3 兼容服务的 endpoint |
| `DIARIZATION` | `0` | 设为 `1` 时用 pyannote 添加匿名说话人标签 |
| `HF_TOKEN` | 无 | pyannote 模型访问令牌 |
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
