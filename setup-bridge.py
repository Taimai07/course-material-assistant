#!/usr/bin/env python3
"""One-time local setup; installs PDF support in an isolated project venv."""
import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import venv


def main():
    parser = argparse.ArgumentParser(description="一次安装资料桥及 PDF 读取依赖")
    parser.add_argument("--register-codex", action="store_true", help="显式将此工具注册到本机 Codex CLI")
    parser.add_argument("--start", action="store_true", help="设置完成后启动本机服务")
    parser.add_argument("--show-mcp-config", action="store_true", help="可选：输出本机 AI 客户端的 MCP 配置")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    virtual = root / ".venv"
    python = virtual / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    if not python.exists():
        venv.EnvBuilder(with_pip=True).create(virtual)
    subprocess.run([str(python), "-m", "pip", "install", "-r", str(root / "server/requirements.txt")], check=True)
    if args.show_mcp_config or args.register_codex:
        subprocess.run([str(python), str(root / "show-mcp-config.py")], check=True)
    else:
        print("本地采集已准备好，不需要 GPT、MCP 或 API Key。连接 Chrome 后运行 collect-materials。", flush=True)
    if args.register_codex:
        codex = shutil.which("codex")
        if not codex:
            raise ValueError("未找到 Codex CLI；依赖已安装，请按上面的配置手动连接 MCP")
        subprocess.run([codex, "mcp", "add", "course-material-bridge", "--", str(python), str(root / "server/app.py"), "mcp"], check=True)
    if args.start:
        subprocess.run([str(python), str(root / "server/app.py"), "serve"], check=True)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
