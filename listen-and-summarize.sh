#!/bin/bash
#
# 播客代听助手 - 转录入口
# 使用: ./listen-and-summarize.sh <播客链接或名称>
#
# 转录引擎：SenseVoice-Small（主力）→ faster-whisper → openai-whisper（备用）
# ASR_ENGINE=stitch 时，长音频前半段使用 SenseVoice、后半段使用 Whisper
# 深度总结由 Agent 读取输出的任务指令后按 SKILL.md 完成。

set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'

log() { echo -e "${GREEN}[INFO]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_SCRIPT="$SCRIPT_DIR/podcast-listener.py"
OUTPUT_DIR="${OUTPUT_DIR:-$HOME/Documents/播客总结}"
ASR_ENGINE="${ASR_ENGINE:-sensevoice}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

if [ $# -lt 1 ]; then
    echo "🎧 播客代听助手"
    echo ""
    echo "用法:"
    echo "  $0 <播客链接或名称>"
    echo ""
    echo "示例:"
    echo "  $0 'https://www.xiaoyuzhoufm.com/episode/xxxxxxxxxxxxxxxxxxxxxxxx'"
    echo "  $0 'https://podcasts.apple.com/us/podcast/xxx/id123456789?i=1000123456789'"
    echo "  $0 'https://overcast.fm/+abcdef'"
    echo "  $0 'https://www.bilibili.com/video/BVxxxxxxxxx'"
    echo "  $0 'https://music.163.com/#/program?id=xxxxxx'"
    echo "  $0 'https://www.ximalaya.com/sound/xxxxxx'"
    echo "  $0 'https://www.lizhi.fm/xxxxxx/xxxxxx'"
    echo "  $0 '节目名 单集标题关键词'"
    echo ""
    echo "环境变量:"
    echo "  ASR_ENGINE=sensevoice  - 主力引擎 SenseVoice-Small（默认，中文播客推荐）"
    echo "  ASR_ENGINE=whisper     - 强制使用 Whisper（纯英文/专业技术播客）"
    echo "  ASR_ENGINE=stitch      - 长音频分段拼接 SenseVoice + Whisper"
    echo "  WHISPER_MODEL=large-v3 - Whisper 备用模型，默认 large-v3"
    echo ""
    echo "流程:"
    echo "  1. 解析播客链接或名称，提取音频、Show Notes、嘉宾信息"
    echo "  2. 下载音频并预处理为单声道 WAV"
    echo "  3. SenseVoice-Small 转录（VAD 分段防 OOM），失败自动降级 Whisper"
    echo "  4. 输出转录稿、元数据和 Agent 任务指令"
    echo "  5. Agent 按 SKILL.md 继续完成 Refine 总结和最终报告"
    exit 1
fi

# 允许 OpenClaw/IM 将“标题 URL”拆成多个参数，Python 入口会再次解析富文本。
URL="$*"

log "检查环境..."

if ! "$PYTHON_BIN" --version &>/dev/null; then
    error "Python3 未安装"
    exit 1
fi

if ! command -v ffmpeg &>/dev/null; then
    warn "ffmpeg 未安装，音频预处理会失败；建议先安装 ffmpeg"
fi

if ! command -v ffprobe &>/dev/null; then
    warn "ffprobe 未安装，将无法读取准确音频时长；建议先安装 ffmpeg"
fi

if ! command -v yt-dlp &>/dev/null; then
    warn "yt-dlp 未安装，YouTube/Bilibili 视频链接无法解析；建议: pip3 install yt-dlp"
fi

# 检测主力引擎依赖
if ! "$PYTHON_BIN" -c "import funasr" 2>/dev/null; then
    warn "FunASR 未安装（主力引擎 SenseVoice-Small 不可用）"
    warn "建议安装: pip3 install funasr modelscope"
    warn "将自动降级到 Whisper 备用引擎"
fi

if ! "$PYTHON_BIN" -c "import silero_vad" 2>/dev/null; then
    warn "silero-vad 未安装（VAD 分段防 OOM 功能不可用）"
    warn "建议安装: pip3 install silero-vad"
fi

# 检测备用引擎依赖（至少一个需要可用）
HAS_FASTER_WHISPER=$("$PYTHON_BIN" -c "import faster_whisper; print('yes')" 2>/dev/null || echo "no")
HAS_WHISPER=$("$PYTHON_BIN" -c "import whisper; print('yes')" 2>/dev/null || echo "no")

if [ "$HAS_FASTER_WHISPER" = "no" ] && [ "$HAS_WHISPER" = "no" ]; then
    warn "Whisper 备用引擎均未安装，若 SenseVoice 失败将无法转录"
    warn "建议安装: pip3 install faster-whisper openai-whisper"
fi

mkdir -p "$OUTPUT_DIR"

log "=================================="
log "开始处理播客"
log "链接: $URL"
log "输出目录: $OUTPUT_DIR"
log "=================================="

RESULT_JSON_FILE="$(mktemp -t moc-podcast-result.XXXXXX)"
trap 'rm -f "$RESULT_JSON_FILE"' EXIT
RESULT_JSON="$RESULT_JSON_FILE" "$PYTHON_BIN" "$PYTHON_SCRIPT" "$URL"

read_result() {
    "$PYTHON_BIN" -c 'import json, sys; print(json.load(open(sys.argv[1], encoding="utf-8")).get(sys.argv[2], ""))' \
        "$RESULT_JSON_FILE" "$1"
}

LATEST_INSTRUCTION="$(read_result instruction_path)"
LATEST_TRANSCRIPT="$(read_result transcript_path)"
LATEST_METADATA="$(read_result metadata_path)"
LATEST_JOB_ID="$(read_result job_id)"
LATEST_JOB_STATUS="$(read_result job_status)"
LATEST_STATUS_PATH="$(read_result status_path)"
LATEST_RESULT_PATH="$(read_result result_path)"

echo ""
log "转录阶段完成"

if [ -n "$LATEST_JOB_ID" ]; then
    echo "   作业 ID: $LATEST_JOB_ID"
    echo "   当前状态: ${LATEST_JOB_STATUS:-awaiting_report}"
    echo "   状态文件: $LATEST_STATUS_PATH"
    echo "   结果文件: $LATEST_RESULT_PATH"
fi

if [ -n "$LATEST_TRANSCRIPT" ] && [ -f "$LATEST_TRANSCRIPT" ]; then
    CHAR_COUNT=$(wc -m < "$LATEST_TRANSCRIPT" 2>/dev/null || echo "0")
    echo "   转录稿: $LATEST_TRANSCRIPT"
    echo "   转录字符: $CHAR_COUNT"
fi

if [ -n "$LATEST_METADATA" ] && [ -f "$LATEST_METADATA" ]; then
    echo "   元数据: $LATEST_METADATA"
fi

if [ -n "$LATEST_INSTRUCTION" ] && [ -f "$LATEST_INSTRUCTION" ]; then
    echo "   Agent任务指令: $LATEST_INSTRUCTION"
    echo ""
    echo "=================================="
    echo "请把下面这段交给 Agent 继续执行"
    echo "=================================="
    cat "$LATEST_INSTRUCTION"
else
    warn "未找到 Agent 任务指令文件，请检查 Python 脚本输出"
fi

log "=================================="
log "脚本结束：作业仍需由 Agent 生成总结并执行产物核验"
log "=================================="
