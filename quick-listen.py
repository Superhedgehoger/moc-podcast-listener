#!/usr/bin/env python3
"""播客快速入口。

快速入口与主流程共用同一套平台解析、下载、转录和输出逻辑，
默认选择 faster-whisper/openai-whisper 的 small 模型以减少等待时间。
它仍会生成标准的转录稿、metadata 和 Agent 任务指令，不再维护第二套流水线。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="快速转录播客链接或名称")
    parser.add_argument("input", nargs="+", help="播客链接、带标题的链接或搜索关键词")
    parser.add_argument(
        "--engine",
        choices=("sensevoice", "whisper", "stitch"),
        default="whisper",
        help="转录引擎，默认 whisper；长音频可选 stitch",
    )
    parser.add_argument(
        "--model",
        default="small",
        help="Whisper 备用/主用模型，默认 small",
    )
    parser.add_argument("--output-dir", help="覆盖标准输出目录")
    parser.add_argument("--keep-audio", action="store_true", help="保留下载音频和 WAV")
    parser.add_argument("--force-transcribe", action="store_true", help="忽略已有转录并重新处理")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    script_path = Path(__file__).with_name("podcast-listener.py")
    user_input = " ".join(args.input).strip()
    env = os.environ.copy()
    env["ASR_ENGINE"] = args.engine
    env["WHISPER_MODEL"] = args.model
    if args.output_dir:
        env["OUTPUT_DIR"] = str(Path(args.output_dir).expanduser())
    if args.keep_audio:
        env["KEEP_AUDIO"] = "1"
    if args.force_transcribe:
        env["FORCE_TRANSCRIBE"] = "1"

    return subprocess.run(
        [sys.executable, str(script_path), user_input],
        env=env,
        check=False,
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
