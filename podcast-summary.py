#!/usr/bin/env python3
"""Compact coordinator commands; never starts inference in the current session."""
import argparse
import json
import sys

from summary_workflow import prepare, status, assemble, task_event, locate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "status", "assemble", "start", "fail", "locate"))
    parser.add_argument("result", help="Absolute path to result.json")
    parser.add_argument("--task")
    parser.add_argument("--evidence")
    parser.add_argument("--reason", default="")
    parser.add_argument("--target-tokens", type=int, default=8000)
    parser.add_argument("--synthesis-tokens", type=int, default=24000)
    parser.add_argument("--model")
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            value = prepare(args.result, args.target_tokens, args.synthesis_tokens, args.model)
        elif args.command == "locate":
            if not args.evidence:
                parser.error("--evidence required")
            value = locate(args.result, args.evidence)
        elif args.command in {"start", "fail"}:
            if not args.task:
                parser.error("--task required")
            value = task_event(args.result, args.task, args.command, args.reason)
        else:
            value = {"status": status, "assemble": assemble}[args.command](args.result)
        print(json.dumps(value, ensure_ascii=False, indent=2))
    except (ValueError, KeyError, TypeError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
