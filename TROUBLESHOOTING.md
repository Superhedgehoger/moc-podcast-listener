# 故障排查手册 (Troubleshooting)

## ❌ 问题：无法提取音频地址

```bash
curl -s "EPISODE_URL" | grep -o '"audioUrl":"[^"]*"'
```

解决：
1. 优先用 Lightpanda 打开页面并保存渲染后 HTML：
   ```bash
   lightpanda "EPISODE_URL" --dump > /tmp/xiaoyuzhou_episode.html
   ```
2. 在页面 JSON 中查找这些字段：
   - `audioUrl`
   - `episode.audio.url`
   - `episode.enclosure.url`
   - `episode.media.url`
   - 任意包含 `audio/enclosure/media` 的 URL 字段
3. 在页面 HTML 中查找音频扩展名：
   ```bash
   grep -Eo 'https?://[^"'\'' <>]+?\.(m4a|mp3|aac|wav|m3u8)(\?[^"'\'' <>]*)?' /tmp/xiaoyuzhou_episode.html
   ```
4. 查找 RSS feed，并从 `<enclosure url="...">` 获取音频。
5. 浏览器或 Lightpanda 无法拿到时，再试：
   ```bash
   yt-dlp --get-url "EPISODE_URL"
   ```

---

## ❌ 问题：小宇宙链接失效，但仍想找音频

> [!TIP]
> **富文本与标题恢复机制**：
> 如果你向 Agent 粘贴的是带标题的富文本/Markdown 链接（例如 `[标题](失效的URL)`），脚本和 Agent 会自动从你的输入中解析出标题，并在 URL 解析或下载失败时自动在 iTunes/RSS 中以该标题进行检索与恢复。

处理顺序：
1. 从原链接提取 episode id，拼回 `https://www.xiaoyuzhoufm.com/episode/EPISODE_ID`
2. 用 Lightpanda 打开 canonical URL，保存最终 HTML。
3. 如果页面仍打不开，搜索播客标题、单集标题、Show Notes 关键词，优先找官方 RSS。
4. 在 RSS 中匹配单集 `<item>`，读取 `<enclosure url="...">`。
5. 若 RSS 被转成 JSON，重点查 `episode.enclosure.url`。
6. 如果拿到 `.m3u8` 而不是 `.m4a/.mp3`，用 ffmpeg 下载：
   ```bash
   ffmpeg -i "M3U8_URL" -c copy /tmp/podcast_audio.m4a
   ```

只要能定位到同一标题、同一发布时间或同一 Show Notes 的 RSS item，就可以使用其 enclosure 音频作为替代来源。

---

## ❌ 问题：ffmpeg 转换 WAV 失败

```bash
brew install ffmpeg
ffmpeg -version   # 验证
# 如果 M4A 本身损坏，重新下载
curl -L -C - -o /tmp/podcast_audio.m4a "AUDIO_URL"   # 断点续传
```

---

## ❌ 问题：Whisper 转录失败或太慢

```bash
# 降级1：换 small 模型
whisper /tmp/podcast_audio.wav --model small --language zh

# 降级2：跳过WAV转换，直接用 m4a
whisper /tmp/podcast_audio.m4a --model small --language zh

# 降级3：检查安装
pip3 show openai-whisper
pip3 install openai-whisper --break-system-packages
```

---

## ❌ 问题：SenseVoice 转录太慢（M1 Pro CPU RTF > 1.0）

```bash
# 方案1：切换到 faster-whisper 链路
ASR_ENGINE=whisper WHISPER_MODEL=small python3 podcast-listener.py "https://..."

# 方案2：使用模式 B 分段自动拼接
ASR_ENGINE=stitch WHISPER_MODEL=small python3 podcast-listener.py "https://..."

# 方案3：检查 funasr 版本
pip3 show funasr   # 推荐 1.3.14+
pip3 install funasr --upgrade
```

---

## ❌ 问题：转录稿存在，但总结失败

总结由 Agent 直接执行，不依赖外部脚本或 API。脚本生成转录后，作业会停在
`awaiting_report`，这不是故障，而是明确表示总结尚未写入。

如果 Agent 反馈转录稿过长，运行项目自带的分块工具：

```bash
python3 chunk_transcript.py "transcript.txt"
```

让 Agent 按 `REFINE_MANIFEST.md` 对每块独立提取证据，合并去重后只生成一次正式报告。

报告生成后执行任务指令末尾的核验命令：

```bash
python3 podcast-listener.py --output-dir "$HOME/Documents/播客总结" \
  --verify "JOB_ID" --require-report
```

检查当前状态或结果：

```bash
cat "$HOME/Documents/播客总结/.jobs/JOB_ID/status.json"
cat "$HOME/Documents/播客总结/.jobs/JOB_ID/result.json"
```

若进程中断，不要启动重复转录，优先恢复检查点：

```bash
python3 podcast-listener.py --output-dir "$HOME/Documents/播客总结" --resume "JOB_ID"
python3 podcast-listener.py --output-dir "$HOME/Documents/播客总结" --resume latest
```

---

## ❌ 问题：发布方转录或章节文件不可用

RSS 提供的 `<podcast:transcript>` 和 `<podcast:chapters>` 仍可能失效或格式不规范。
发布方转录失败时会自动尝试下一种格式，最后回退到本地 ASR；章节失败只记录原因，
不会中断转录。可在 metadata、`result.json` 和章节归档字段中查看源 URL、HTTP 状态和
失败原因。需要强制本地转录时：

```bash
PREFER_PUBLISHER_TRANSCRIPT=0 python3 podcast-listener.py --force-transcribe "EPISODE_URL"
```

---

## ❌ 问题：Show Notes 外链没有保存网页副本

外链快照默认关闭。安装 SingleFile CLI 或 ArchiveBox 后显式启用：

```bash
python3 podcast-listener.py --archive-only --link-snapshot singlefile "EPISODE_URL"
```

快照失败不会删除在线链接。具体结果和失败原因位于
`Show Notes/*_media-manifest.json` 的 `links[].snapshot` 中。

---

## ❌ 问题：总结字数不达标

Agent 必须自查并补充，检查清单：

- [ ] 每条核心观点是否有 200+ 字且包含具体人名/数字/案例？
- [ ] 关键引述是否有 8+ 条且每条有重要性说明？
- [ ] 是否存在"泛泛而谈"的描述？找出并替换为具体内容
- [ ] 背景知识是否真实补充了外部信息（非转录稿内容）？

---

## ❌ 问题：文件写入失败

```bash
mkdir -p "$HOME/Documents/播客总结"/{转录稿,音频,总结稿}
df -h "$HOME/Documents"    # 检查磁盘空间
```
