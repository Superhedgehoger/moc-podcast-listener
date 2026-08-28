# 更新日志 (Changelog)

## v4.10.0 (2026-08-28)
- 根目录新增自动维护的 `播客索引.md`，集中链接每期总结稿、转录稿、资料包和原始页面
- 索引按发布日期排序，并区分 `已完成`、`待总结`、`仅归档` 和 `资料不完整`，便于人类阅读与补做任务
- 新增离线 `--rebuild-index` 命令；归档、转录和最终核验后也会自动刷新索引

## v4.9.0 (2026-08-28)
- 顶层只保留面向阅读的 `总结稿/` 和 `转录稿/`；转录目录只写一个人类可读 `.txt`
- 每期技术附件统一归入 `资料/<节目_标题_日期>/`，集中保存字幕、segments、元数据、任务指令、Show Notes、图片和可选链接快照
- Show Notes 使用自包含子目录：`shownotes.md`、`source.raw.html`、`media-manifest.json` 和 `图片/`
- 继续兼容旧版平铺转录附件、Show Notes manifest、图片缓存与历史作业验证

## v4.8.0 (2026-08-23)
- YouTube 与 Bilibili 统一只选择 `yt-dlp` 的纯音频格式，不再回退到可能包含视频的默认直链
- 支持直接输入本地课程音频或视频文件；视频使用 `ffmpeg -vn` 仅提取第一条音轨后转录
- 支持按课时生成独立转录、字幕、总结和可恢复任务，默认完成后删除中间音频

## v4.7.1 (2026-08-17)
- 单集封面现在作为 `cover` 资源写入 Show Notes manifest，并在 `hybrid` / `local` 模式下落盘
- 即使发布方没有 Show Notes 正文，只要存在封面，也会生成可核验的 Markdown、manifest 和图片归档
- Overcast 页面缺少 Show Notes 时通过页面 RSS 或播客目录补全节目名、发布日期和正文，同时保留页面直链音频
- 恢复任务时拒绝复用缺少封面的旧 manifest；核验阶段检查封面资源是否进入 manifest
- 兼容 Clash/Mihomo 默认的 IPv4 与 IPv6 Fake-IP 网段；例外仅用于域名解析结果

## v4.7.0 (2026-08-13)
- 新增 WebVTT 字幕输出，并保留匿名或发布方说话人标签
- 支持下载、校验和标准化 Podcasting 2.0 章节 JSON
- Show Notes manifest 升级为 schema v2，记录最终 URL、HTTP 状态、抓取时间、MIME、字节数与失败原因
- 新增默认关闭的 `singlefile` / `archivebox` 一级外链快照，支持数量上限且始终保留在线 URL
- 总结稿工作流和产物核验覆盖 VTT、章节及链接快照

## v4.6 (2026-08-13)
- 每次处理创建 `.jobs/<job-id>/job.json`、`status.json` 和 `result.json`
- 新增阶段进度、检查点和 `--resume JOB_ID|latest`，避免中断后重复解析或归档
- 明确区分 `awaiting_report` 与 `completed`，不再把“转录完成”误报为“整集完成”
- 新增 `--verify JOB_ID_OR_RESULT --require-report`，核验转录、字幕、元数据、Show Notes、总结稿及相对链接
- 状态与结果文件使用原子写入，失败任务会记录原因

## v4.5 (2026-08-13)
- 所有内置 HTTP 下载统一校验 URL 与每次重定向，阻止本地/私网目标并限制响应体大小
- 兼容 Clash/Surge 类透明代理的域名 Fake-IP，同时继续拒绝非公网 IP 直连
- RSS 新增 Podcasting 2.0 transcript、chapters、person、image 和 language 元数据解析
- 优先使用发布方 VTT/SRT/JSON/文本转录，不可用时才回退缓存或本地 ASR
- 修复小宇宙当前页面结构解析，准确提取节目名、时长、日期、富文本 Show Notes、图片和嘉宾候选
- 新增目标小宇宙页面、Podcasting 2.0 feed 与发布方 WebVTT 的离线回归 fixture

## v4.5.0 (2026-08-09)
- 采用 MIT License，明确项目的使用、修改与分发权限
- 新增 Python 3.10–3.13 GitHub Actions 离线回归测试矩阵
- 新增基于版本标签的 GitHub Release 工作流
- 新增统一版本来源、`--version` 命令和发布元数据一致性检查

## v4.4 (2026-08-05)
- 转录稿新增节目、单集、原始链接、发布日期、时长、引擎、语言和生成时间头部
- 转录正文按 `[HH:MM:SS - HH:MM:SS]` 展示分段时间戳，并保留可选匿名说话人标签
- 转录稿尾部新增原始页面、segments、SRT、元数据、Show Notes、媒体清单和结束标记
- 总结稿不再嵌入完整转录正文，改为介绍并相对链接独立转录稿、segments 和 SRT
- 缓存复用优先从 segments 重建纯正文，兼容旧版纯文本和新版自描述转录稿
- 长音频新增字数与时间戳覆盖完整性门槛；残缺缓存会被拒绝，残缺 ASR 输出会自动降级重试

## v4.2 (2026-08-04)
- 将 Skill 标准化并精简，新增 `agents/openai.yaml` 和渐进加载的报告工作流
- 修复 Show Notes 富文本被提前清洗的问题，扩展图片、链接和 manifest 归档
- 增加图片私网拦截、类型/大小校验、附件缓存和哈希去重
- 新增 timestamp segments、SRT、`--resolve-only` 和 `--archive-only`
- 完整运行默认复用单集 ID/URL 匹配的已有转录；支持 `--force-transcribe`
- 长播客改用逐块证据提取和一次整合，减少重复重写

## v4.0 (2026-07-25)
- **架构优化**：SKILL.md 从 900+ 行精简至 ~350 行，拆分出 CHANGELOG.md、TROUBLESHOOTING.md
- **命名修复**：pub_date 格式统一为 YYYYMMDD，特殊字符过滤增强
- **格式强化**：build_agent_instruction() 增加格式自检清单
- **文档拆分**：版本历史、故障排查、配置说明分离至独立文件

## v3.8 (2026-07-18)
- **新增**：支持 OpenClaw IM 将标题和链接拆成多个参数后再合并解析
- **完成**：`quick-listen.py` 改为主流程包装入口，默认 Whisper small，支持 `--engine`、`--model`、`--output-dir`、`--keep-audio`
- **修复**：SenseVoice 模型进程内缓存，VAD 分段不再重复加载模型
- **新增**：`ASR_ENGINE=stitch` 可执行分段拼接模式，自动校准第二段时间戳
- **修复**：RSS 指定标题/id 匹配失败时禁止静默使用第一集
- **新增**：Bilibili、网易云音乐、喜马拉雅、荔枝 FM，优先使用 yt-dlp 并支持页面回退
- **新增**：`tests/fixtures` 平台解析回归测试

## v3.7 (2026-07-09)
- **架构升级**：转录引擎从 `openai-whisper` 单一链路升级为 **SenseVoice 主力 + Whisper 备用** 三层自动降级 + 模式 B 分段手动拼接
- **新增**：第 2.3 节"模式 B — 分段手动拼接"详细步骤
- **新增**：第 2.3 节"转录引擎决策矩阵"
- **修正**：M1 Pro 32GB CPU 实测性能数据——SenseVoice RTF 1.0–1.7
- **新增**：基本信息表新增"转录引擎"字段
- **新增**：环境变量 `ASR_ENGINE`（sensevoice / whisper / stitch）

## v3.6
- **重大质量升维**：将字数达标降级为兜底项
- **新增**：基于 CoD 论文的"3轮实体密度循环"取代空洞文字
- **新增**：基于 Self-Refine 盲区研究的"跨会话隔离质检"克服自我偏好偏差
- **新增**：针对超长播客（>5分块）引入"分组 Refine + Reduce"架构

## v3.5 (2026-06-09)
- **新增**：支持 Podwise.ai、Spotify、Pocket Casts、Castro、Castbox、YouTube、Listen Notes、Podbean 和 iHeart 链接解析与音频发现

## v3.4
- **重命名**：技能重命名为 `moc-pocast-listener`
- **强化**：字数标准明确为硬性下限（只能高不能低）
- **统一**：存放路径统一
- **新增**：`__pycache__` 文件夹说明与 Agent 严格执行限制

## v3.3 (2026-06-01)
- **新增**：支持 Apple Podcasts 单集链接，通过 Apple/iTunes API 与 RSS enclosure 定位音频
- **新增**：支持 Overcast 分享链接，页面直取失败时回退到标题搜索
- **新增**：支持直接输入 RSS/XML/feed 链接
- **新增**：支持根据节目名和单集关键词搜索

## v3.2 (2026-05-13)
- **新增**：解析链接时优先使用 Lightpanda 获取渲染后页面和最终 URL
- **新增**：音频发现顺序覆盖 audioUrl、enclosure、RSS、audio/src、.m3u8
- **新增**：小宇宙分享链接失效时通过 episode id、RSS feed、标题匹配恢复音频来源

## v3.1 (2026-05-10)
- **新增**：音频预处理——下载后先转 16kHz 单声道 WAV 再送 Whisper
- **新增**：Whisper `initial_prompt` 注入嘉宾姓名，减少专有名词识别错误
- **优化**：Whisper 默认模型改为 `large-v3`
- **架构**：分块策略从 Map-Reduce 改为 **Refine 迭代精炼**
- **优化**：Prompt 升级为三层分层设计
- **新增**：Prompt 强制要求引用具体人名、数字、案例
- **新增**：`faster-whisper` 可选加速方案

## v3.0 (2026-05-10)
- **架构变更**：总结步骤改为 Agent 直接执行，彻底解决外部 API 总结失败问题
- **新增**：Agent 字数自查机制
- **新增**：完整故障排查手册

## v2.0 (2026-05-03)
- 新增：自动生成 AI 详细总结（调用外部 API）
- 已知问题：外部 API 失败时总结步骤中断，无 fallback

## v1.0
- 基础转录功能，总结模板需手动调用 AI
# v4.3

- 新增本地同步目录、WebDAV（含坚果云）和 S3/R2 三种 Show Notes 存储后端。
- 可生成使用公开图片 URL 的 `_published.md`，同时保留原始本地 Markdown 和源链接。
- 新增独立同步清单，凭据只从环境变量读取。
- 新增可选 pyannote 说话人分离，将匿名标签写入分段 JSON 与 SRT。
- shell 入口支持 `PYTHON_BIN`，便于用 Python 3.12 运行可选说话人识别。
- 本地同步 Markdown 自动改写为可迁移的 `assets/` 相对路径。
- WebDAV 自动编码中文目录并拒绝 URL 内嵌凭据；自动化可启用同步失败即退出。
- 缓存转录可单独补做说话人识别，无需重新运行 ASR。
