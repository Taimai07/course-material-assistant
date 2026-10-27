#!/usr/bin/env python3
"""Run the same course collection scripts directly, without any model calls."""
import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "server"))
from browser_rpc import BrowserClient
from store import Store
from bundle import export_task


def main():
    parser = argparse.ArgumentParser(description="不消耗模型调用的本机课程采集脚本；先连接 Chrome")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("tabs")
    collect = sub.add_parser("collect")
    collect.add_argument("--tab", type=int)
    collect.add_argument("--url")
    collect.add_argument("--max-pages", type=int, default=10)
    collect.add_argument("--max-files", type=int, default=30)
    collect.add_argument("--max-scrolls", type=int, default=20)
    collect.add_argument("--output", type=Path)
    collect.add_argument("--part-chars", type=int, default=22000)
    status = sub.add_parser("status")
    status.add_argument("task_id")
    args = parser.parse_args()
    store = Store()
    client = BrowserClient(store.control_root)
    if args.command == "tabs":
        print(json.dumps(client.call("tabs", {}), ensure_ascii=False, indent=2))
        return
    if args.command == "status":
        print(json.dumps(client.post("/collection/status", {"task_id": args.task_id}), ensure_ascii=False, indent=2))
        return
    tab_id = args.tab
    if args.url:
        tab_id = client.call("open", {"url": args.url})["tab_id"]
    if tab_id is None:
        raise ValueError("请用 --tab 指定课程标签页，或用 --url 指定课程/网盘链接")
    started = client.post("/collection/start", {"tab_id": tab_id, "max_pages": args.max_pages, "max_files": args.max_files, "max_scrolls": args.max_scrolls})
    task_id = started["task_id"]
    print("脚本任务已启动：" + task_id, flush=True)
    previous = None
    while True:
        task = client.post("/collection/status", {"task_id": task_id})
        progress = (task["status"], len(task["pages"]), len(task["downloads"]), len(task["errors"]))
        if progress != previous:
            print(f"{progress[0]} · 页面 {progress[1]} · 附件 {progress[2]} · 异常 {progress[3]}", flush=True)
            previous = progress
        if task["status"] != "running":
            destination = args.output or store.root / "exports" / task_id
            export_task(store, task, destination, part_chars=args.part_chars)
            print("原附件、文字与来源清单已保存至：" + str(destination.resolve()))
            print("渲染页面遍历可能有缺失，请查看 manifest.json 中的 warnings 和 errors。")
            return
        time.sleep(2)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
