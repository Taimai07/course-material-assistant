#!/bin/sh
cd "$(dirname "$0")" || exit 1
if [ -x .venv/bin/python ]; then
  exec .venv/bin/python workflow.py --analyze codex --show "$@"
fi
exec python3 workflow.py --analyze codex --show "$@"
