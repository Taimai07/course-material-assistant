#!/usr/bin/env python3
"""Print a host-appropriate local MCP configuration without modifying the host."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

entry = str(Path(__file__).resolve().parent / "server/app.py")
configuration = {"mcpServers": {"course-material-bridge": {
    "command": sys.executable, "args": [entry, "mcp"]
}}}
print("MCP 配置（使用本次 Python 的真实路径；保留现有其他服务器）：")
print(json.dumps(configuration, ensure_ascii=False, indent=2))
command = ["codex", "mcp", "add", "course-material-bridge", "--", sys.executable, entry, "mcp"]
print("\n若本机已安装 Codex CLI，也可以在本机终端运行：")
print(subprocess.list2cmdline(command) if os.name == "nt" else shlex.join(command))
print("\n本脚本只生成配置，没有替你安装或修改聊天工具。")
