"""本地网页服务：只监听 127.0.0.1。

GET  /                 编辑器页面（地图数据内嵌）
GET  /api/status       当前模式、快照、上传任务状态
POST /api/refresh      从机器人重新读取地图（只读）
POST /api/plan         dry-run：{design, options} → 将要发送的命令序列
POST /api/upload       {design, options, confirm:<plan hash>} → 开始上传（后台执行）
POST /api/resume       机器人重启后继续（先等它上线）
POST /api/stop         在两步之间停止
POST /api/demo/reboot  演示模式：模拟「重启机器人」
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any

from .cloud import CloudSession, CommandError, has_credentials
from .fake import FakeCloud
from .mapdata import MapState, editor_data, fetch_snapshot
from .plan import build_plan, normalize_design
from .upload import NeedReboot, Uploader, UploadStopped, wait_online


def load_demo_snapshot() -> dict:
    return json.loads(resources.files(__package__).joinpath("demo-snapshot.json").read_text())


class Job:
    def __init__(self) -> None:
        self.state = "idle"  # idle / running / need_reboot / waiting_online / stopped / failed / done
        self.message = ""
        self.log: list[dict] = []
        self.uploader: Uploader | None = None
        self.design: dict | None = None
        self.options: dict = {}
        self.lock = threading.Lock()

    def add(self, msg: str) -> None:
        with self.lock:
            self.log.append({"t": time.time(), "m": msg})
            if len(self.log) > 2000:
                self.log = self.log[-2000:]

    def to_json(self) -> dict:
        with self.lock:
            return {"state": self.state, "message": self.message, "log": list(self.log)}


class App:
    def __init__(self, data_dir: Path, demo: bool = False) -> None:
        self.data_dir = Path(data_dir)
        self.demo = demo
        self.fake: FakeCloud | None = None
        self.snapshot: dict | None = None
        self.job = Job()
        self.busy = threading.Lock()
        if demo:
            self.snapshot = load_demo_snapshot()
            self.fake = FakeCloud(self.snapshot)
        else:
            latest = self.data_dir / "snapshot-latest.json"
            if latest.is_file():
                self.snapshot = json.loads(latest.read_text())

    # ---------------------------------------------------------------- #
    def session(self):
        return self.fake if self.demo else CloudSession()

    def journal_path(self, mid: str) -> Path:
        return self.data_dir / ("demo-journal.json" if self.demo else f"journal-{mid}.json")

    def save_snapshot(self, snap: dict, tag: str = "") -> None:
        self.snapshot = snap
        if self.demo:
            return
        d = self.data_dir / "snapshots"
        d.mkdir(parents=True, exist_ok=True)
        name = f"{snap['mid']}-{time.strftime('%Y%m%d-%H%M%S')}{'-' + tag if tag else ''}.json"
        text = json.dumps(snap, ensure_ascii=False)
        (d / name).write_text(text)
        (self.data_dir / "snapshot-latest.json").write_text(text)

    def refresh(self, log=lambda m: None) -> dict:
        async def go():
            async with self.session() as s:
                return await fetch_snapshot(s, log)
        snap = asyncio.run(go())
        self.save_snapshot(snap)
        return snap

    def status(self) -> dict:
        snap = self.snapshot or {}
        return {
            "mode": "demo" if self.demo else "device",
            "credentials": self.demo or has_credentials(),
            "snapshot": {"mid": snap.get("mid"), "fetchedAt": snap.get("fetchedAt"), "name": (snap.get("meta") or {}).get("name")} if snap else None,
            "job": self.job.to_json(),
        }

    def plan(self, design: dict, options: dict) -> dict:
        if not self.snapshot:
            raise ValueError("还没有读取过地图")
        p = build_plan(design, MapState.from_snapshot(self.snapshot), options)
        return p.to_json()

    # ---------------------------------------------------------------- #
    def start_upload(self, design: dict, options: dict, confirm: str) -> None:
        if self.job.state in ("running", "waiting_online"):
            raise ValueError("已经有上传在进行")
        p = self.plan(design, options)
        if p["errors"]:
            raise ValueError("计划校验没通过：" + "；".join(p["errors"]))
        if confirm != p["hash"]:
            raise ValueError("确认码和当前计划不一致（计划可能变了），请重新预演")
        self.job = Job()
        self.job.design = normalize_design(design)
        self.job.options = options
        self._spawn(wait_first=False)

    def resume(self) -> None:
        if self.job.state not in ("need_reboot", "stopped", "failed") or not self.job.design:
            raise ValueError("当前没有可以继续的上传")
        self._spawn(wait_first=self.job.state == "need_reboot")

    def stop(self) -> None:
        if self.job.uploader:
            self.job.uploader.stop_requested = True

    def _spawn(self, wait_first: bool) -> None:
        job = self.job

        def run() -> None:
            async def go():
                if wait_first:
                    job.state = "waiting_online"
                    job.message = "等待机器人重新上线…"
                    ok = await wait_online(self.session, timeout=1200, interval=5 if self.demo else 15, log=job.add)
                    if not ok:
                        job.state = "need_reboot"
                        job.message = "等了 20 分钟还没上线。确认机器人已开机联网后，再点「继续」"
                        return
                job.state = "running"
                job.message = "正在上传…"
                if not self.demo:
                    job.add("写入前先保存一份完整快照")
                    try:
                        await asyncio.to_thread(self.refresh, job.add)
                    except Exception as exc:  # 快照失败不阻塞（执行器自己会读核心数据）
                        job.add(f"快照没保存成功：{exc}")
                # 演示模式下模拟设备不需要等待，把每次等待缩短到 0.2 秒
                sleep = (lambda sec: asyncio.sleep(min(sec, 0.2))) if self.demo else asyncio.sleep
                up = Uploader(job.design, self.session, self.journal_path(job.design["mid"]), job.options, log=job.add, sleep=sleep)
                job.uploader = up
                await up.run()
                job.add("重新读取完整地图…")
                if self.demo:
                    await self._demo_refresh()
                else:
                    await asyncio.to_thread(self.refresh, job.add)
                job.state = "done"
                job.message = "上传完成，地图已重新读取。刷新页面查看结果"

            try:
                asyncio.run(go())
            except NeedReboot as exc:
                job.state = "need_reboot"
                job.message = f"机器人没有回应：{exc}。请物理重启机器人（长按电源键关机再开机），开机后点「继续」"
                job.add(job.message)
            except UploadStopped as exc:
                job.state = "stopped"
                job.message = str(exc)
                job.add(job.message)
            except CommandError as exc:
                job.state = "failed"
                job.message = f"命令被拒绝：{exc}"
                job.add(job.message)
            except Exception as exc:  # noqa: BLE001
                job.state = "failed"
                job.message = f"{type(exc).__name__}: {exc}"
                job.add(traceback.format_exc(limit=3))

        threading.Thread(target=run, daemon=True).start()

    async def _demo_refresh(self) -> None:
        async with self.session() as s:
            snap = await fetch_snapshot(s)
        snap["raster"] = None
        self.snapshot = snap


def render_editor(app: App) -> bytes:
    tpl = resources.files(__package__).joinpath("web/editor.html").read_text()
    if app.snapshot:
        data = editor_data(app.snapshot)
    else:
        data = None
    page = tpl.replace("/*__DATA__*/null", json.dumps(data, ensure_ascii=False))
    page = page.replace("/*__APP__*/null", json.dumps(app.status(), ensure_ascii=False))
    return page.encode()


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # 不打印访问日志
            return

        def _send(self, code: int, body: Any, ctype: str = "application/json; charset=utf-8") -> None:
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _json(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def _host_ok(self) -> bool:
            # 只接受本机地址访问，防止 DNS 重绑定：恶意域名解析到 127.0.0.1 时，
            # 它的 Origin 和 Host 一致，光比对两者拦不住
            bind, port = self.server.server_address[:2]
            allowed = {"127.0.0.1", "localhost", "[::1]", str(bind)}
            return self.headers.get("Host", "").lower() in {f"{h}:{port}" for h in allowed}

        def _same_origin(self) -> bool:
            # 只接受本页发起的写请求，防止别的网页跨站调用本地接口
            origin = self.headers.get("Origin")
            host = self.headers.get("Host", "")
            return origin is None or origin.split("://", 1)[-1] == host

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, {"error": "只接受本机地址访问"})
            if self.path in ("/", "/index.html"):
                return self._send(200, render_editor(app), "text/html; charset=utf-8")
            if self.path == "/api/status":
                return self._send(200, app.status())
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._host_ok():
                return self._send(403, {"error": "只接受本机地址访问"})
            if not self._same_origin():
                return self._send(403, {"error": "跨站请求被拒绝"})
            try:
                body = self._json()
                if self.path == "/api/plan":
                    return self._send(200, app.plan(body["design"], body.get("options") or {}))
                if self.path == "/api/refresh":
                    if not app.busy.acquire(blocking=False):
                        return self._send(409, {"error": "正在读取中"})
                    try:
                        if app.job.state in ("running", "waiting_online"):
                            return self._send(409, {"error": "上传进行中，不能读取"})
                        app.refresh() if not app.demo else None
                        return self._send(200, app.status())
                    finally:
                        app.busy.release()
                if self.path == "/api/upload":
                    app.start_upload(body["design"], body.get("options") or {}, body.get("confirm", ""))
                    return self._send(200, app.status())
                if self.path == "/api/resume":
                    app.resume()
                    return self._send(200, app.status())
                if self.path == "/api/stop":
                    app.stop()
                    return self._send(200, app.status())
                if self.path == "/api/demo/reboot" and app.demo and app.fake:
                    app.fake.reboot()
                    return self._send(200, app.status())
                return self._send(404, {"error": "not found"})
            except (ValueError, KeyError) as exc:
                return self._send(400, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                return self._send(500, {"error": f"{type(exc).__name__}: {exc}"})

    return Handler


def serve(app: App, host: str = "127.0.0.1", port: int = 8790, open_browser: bool = True) -> None:
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    url = f"http://{host}:{port}/"
    print(f"编辑器已启动：{url}（{'演示模式，不连机器人' if app.demo else '真机模式'}）  Ctrl+C 退出")
    if open_browser:
        import webbrowser

        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
