"""命令行入口：deebot-map <子命令>

  serve        启动本地网页编辑器（默认 http://127.0.0.1:8790/）
  fetch        从机器人读取地图，保存快照（只读）
  plan         dry-run：列出上传某个设计文件将要发送的命令序列
  upload       上传设计文件（默认只 dry-run；加 --yes 并在终端确认后才真正写入）
  wait-online  等机器人重新上线
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from .cloud import CloudSession, load_env_file
from .plan import DEFAULTS, build_plan, plan_summary

DEFAULT_ENV = "~/.config/deebot-map-editor/credentials.env"
DEFAULT_DATA = "~/.local/share/deebot-map-editor"


def _data_dir(args) -> Path:
    return Path(args.data_dir or os.environ.get("DEEBOT_MAP_DATA") or DEFAULT_DATA).expanduser()


def _load_snapshot(args) -> dict:
    if getattr(args, "demo", False):
        from .server import load_demo_snapshot

        return load_demo_snapshot()
    path = Path(args.snapshot).expanduser() if getattr(args, "snapshot", None) else _data_dir(args) / "snapshot-latest.json"
    if not path.is_file():
        raise SystemExit(f"找不到地图快照 {path}，请先运行 deebot-map fetch")
    return json.loads(path.read_text())


def _opts(args) -> dict:
    o = {}
    if getattr(args, "max_move", None):
        o["max_move_mm"] = args.max_move
    if getattr(args, "include_untested", False):
        o["include_untested"] = True
    return o


def cmd_serve(args) -> None:
    from .server import App, serve

    app = App(_data_dir(args), demo=args.demo)
    serve(app, args.host, args.port, open_browser=not args.no_browser)


def cmd_fetch(args) -> None:
    from .server import App

    app = App(_data_dir(args))
    snap = app.refresh(log=print)
    print(f"已保存：{_data_dir(args) / 'snapshot-latest.json'}（房间 {len(snap.get('rooms') or [])} 个）")


def cmd_plan(args) -> None:
    from .mapdata import MapState

    design = json.loads(Path(args.design).read_text())
    plan = build_plan(design, MapState.from_snapshot(_load_snapshot(args)), _opts(args))
    if args.json:
        print(json.dumps(plan.to_json(), ensure_ascii=False, indent=1))
    else:
        print(plan_summary(plan))
        print(f"\n共 {len(plan.steps)} 步；计划指纹 {plan.hash()}")
    if plan.errors:
        sys.exit(2)


def cmd_upload(args) -> None:
    from .mapdata import MapState
    from .upload import NeedReboot, Uploader, UploadStopped

    design = json.loads(Path(args.design).read_text())
    snap = _load_snapshot(args)
    plan = build_plan(design, MapState.from_snapshot(snap), _opts(args))
    print(plan_summary(plan))
    if plan.errors:
        raise SystemExit("\n计划校验没通过，不会发送任何命令")
    if not args.yes:
        print("\n以上是 dry-run，没有发送任何命令。确认无误后加 --yes 真正上传。")
        return
    if args.demo:
        from .fake import FakeCloud

        fake = FakeCloud(snap)
        factory = lambda: fake  # noqa: E731
    else:
        factory = CloudSession
        ans = input(f"\n即将向机器人写入 {len(plan.steps)} 步。输入 yes 继续：").strip()
        if ans != "yes":
            raise SystemExit("已取消")
    journal = _data_dir(args) / (f"journal-{design['mid']}.json" if not args.demo else "demo-journal.json")
    # 演示模式下模拟设备不需要等待
    sleep = (lambda sec: asyncio.sleep(min(sec, 0.2))) if args.demo else asyncio.sleep
    if args.demo:
        import tempfile

        journal = Path(tempfile.mkdtemp()) / "demo-journal.json"
    up = Uploader(design, factory, journal, _opts(args), log=print, sleep=sleep)
    try:
        asyncio.run(up.run())
    except NeedReboot as exc:
        raise SystemExit(f"\n机器人没有回应：{exc}\n请物理重启机器人，上线后（可用 deebot-map wait-online 等待）用同一条命令继续，会从断点接着做。")
    except UploadStopped as exc:
        raise SystemExit(f"\n已停止：{exc}")


def cmd_wait_online(args) -> None:
    from .upload import wait_online

    ok = asyncio.run(wait_online(CloudSession, timeout=args.timeout, interval=args.interval))
    if not ok:
        raise SystemExit("等待超时，设备仍未上线")


def main() -> None:
    p = argparse.ArgumentParser(prog="deebot-map", description="科沃斯扫地机地图编辑器")
    p.add_argument("--env-file", help=f"凭据文件（KEY=VALUE），默认 {DEFAULT_ENV}")
    p.add_argument("--data-dir", help=f"快照和断点日志目录，默认 {DEFAULT_DATA}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="启动本地网页编辑器")
    s.add_argument("--demo", action="store_true", help="演示模式：用虚构地图和模拟设备，不连机器人")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8790)
    s.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("fetch", help="从机器人读取地图（只读）")
    s.set_defaults(func=cmd_fetch)

    for name, fn, h in (("plan", cmd_plan, "dry-run：列出将要发送的命令"), ("upload", cmd_upload, "上传设计文件")):
        s = sub.add_parser(name, help=h)
        s.add_argument("design", help="编辑器导出的设计文件（.json）")
        s.add_argument("--snapshot", help="用指定的地图快照（默认用最近一次读取的）")
        s.add_argument("--demo", action="store_true", help="对演示地图和模拟设备执行")
        s.add_argument("--max-move", type=int, help=f"写墙时每步单个角点最多移动多少 mm（默认 {DEFAULTS['max_move_mm']}）")
        s.add_argument("--include-untested", action="store_true", help="同时发送编辑器里「未实测」的其它命令")
        if name == "plan":
            s.add_argument("--json", action="store_true", help="输出 JSON")
        else:
            s.add_argument("--yes", action="store_true", help="真正写入（仍会在终端再确认一次）")
        s.set_defaults(func=fn)

    s = sub.add_parser("wait-online", help="等机器人重新上线")
    s.add_argument("--timeout", type=int, default=900)
    s.add_argument("--interval", type=int, default=15)
    s.set_defaults(func=cmd_wait_online)

    args = p.parse_args()
    env = args.env_file or os.environ.get("DEEBOT_MAP_ENV_FILE") or DEFAULT_ENV
    if args.env_file and not load_env_file(env):
        raise SystemExit(f"凭据文件不存在：{env}")
    load_env_file(env)
    args.func(args)


if __name__ == "__main__":
    main()
