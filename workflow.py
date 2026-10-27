#!/usr/bin/env python3
"""Local collection wizard. Manual AI handoff is the default; AI is optional."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "server"))
from browser_rpc import BrowserClient
from bundle import export_task
from store import Store


def choose_tab(client):
    tabs = client.call("tabs", {}).get("tabs", [])
    print("\n当前已打开的课程/网盘页面：")
    for index, tab in enumerate(tabs, 1):
        print(f"  {index}. {tab.get('title', '未命名')}\n     {tab['url']}")
    value = input("输入上面的编号，或粘贴课程/文件夹链接：").strip()
    if value.startswith("https://"):
        return client.call("open", {"url": value})["tab_id"]
    try:
        index = int(value)
        if not 1 <= index <= len(tabs):
            raise ValueError()
    except ValueError:
        raise ValueError("请选择有效编号，或提供 Google Classroom / Drive / Docs 的 HTTPS 链接") from None
    return tabs[index - 1]["tab_id"]


def run_workflow(client, store, tab_id, max_pages=10, max_files=30, max_scrolls=20,
                 output=None, part_chars=22000, question="", poll=time.sleep):
    started = client.post("/collection/start", {"tab_id": tab_id, "max_pages": max_pages, "max_files": max_files, "max_scrolls": max_scrolls})
    task_id = started["task_id"]
    print("\n本地采集已启动，不调用 AI：" + task_id, flush=True)
    previous = None
    while True:
        task = client.post("/collection/status", {"task_id": task_id})
        progress = (task["status"], len(task["pages"]), len(task["downloads"]), len(task["errors"]))
        if progress != previous:
            print(f"{progress[0]} · 页面 {progress[1]} · 附件 {progress[2]} · 异常 {progress[3]}", flush=True)
            previous = progress
        if task["status"] != "running":
            destination = output or store.root / "exports" / task_id
            result = export_task(store, task, destination, part_chars=part_chars, question=question)
            print("\n资料已保存：" + result["directory"])
            print(f"原附件 {result['files']} 份，可读资料 {result['sources']} 份，AI 文字分块 {result['parts']} 份。")
            print("朋友手动使用：打开 START-HERE.txt，按提示把资料交给任意 AI；也可以先保留资料，不使用 AI。")
            print("已发现的读取/下载缺失会保留在 manifest.json 中。", flush=True)
            return result
        poll(2)


def show_folder(path):
    if os.name == "nt":
        os.startfile(str(path))
    else:
        command = "open" if sys.platform == "darwin" else "xdg-open"
        try:
            subprocess.Popen([command, str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass


def main():
    parser = argparse.ArgumentParser(description="课题资料采集向导；默认不需要 AI、GPT 会员或 API Key")
    parser.add_argument("--url")
    parser.add_argument("--tab", type=int)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--max-pages", type=int, default=10)
    parser.add_argument("--max-files", type=int, default=30)
    parser.add_argument("--max-scrolls", type=int, default=20)
    parser.add_argument("--part-chars", type=int, default=22000)
    parser.add_argument("--question", default="")
    parser.add_argument("--analyze", choices=("none", "codex"), default="none")
    parser.add_argument("--max-analysis-chars", type=int, default=120000)
    parser.add_argument("--show", action="store_true")
    args = parser.parse_args()
    store = Store()
    client = BrowserClient(store.control_root)
    if not client.status().get("connected"):
        raise ValueError("Chrome 尚未连接。请运行 start-bridge，填入配对码，并保持扩展连接页打开；不需要 MCP 或 AI。")
    tab_id = client.call("open", {"url": args.url})["tab_id"] if args.url else args.tab
    if tab_id is None:
        tab_id = choose_tab(client)
    result = run_workflow(client, store, tab_id, args.max_pages, args.max_files, args.max_scrolls,
                          args.output, args.part_chars, args.question)
    if args.show:
        show_folder(result["directory"])
    if args.analyze == "codex":
        from automation import analyze_bundle
        report = analyze_bundle(result["directory"], max_chars=args.max_analysis_chars)
        print("AI 分析完成，报告保存在：" + str(report))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, EOFError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        print("已停止等待。服务中已启动的采集可通过断开 Chrome 连接终止。", file=sys.stderr)
        raise SystemExit(130)
