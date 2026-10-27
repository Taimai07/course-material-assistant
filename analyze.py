#!/usr/bin/env python3
"""Optional separate AI phase for an already exported local bundle."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "server"))
from automation import analyze_bundle

parser = argparse.ArgumentParser(description="可选：用自己的本机 Codex 分析已有资料包；使用 AI 账号额度")
parser.add_argument("bundle", type=Path)
parser.add_argument("--max-chars", type=int, default=120000)
args = parser.parse_args()
try:
    print(analyze_bundle(args.bundle, max_chars=args.max_chars))
except (ValueError, OSError) as error:
    print(str(error), file=sys.stderr)
    raise SystemExit(1)
