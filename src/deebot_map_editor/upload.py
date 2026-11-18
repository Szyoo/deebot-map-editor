"""一键上传执行器：备份 → 切割/合并 → 改名 →（可选）其它命令 → 逐间分步写墙。

每完成一步都写进断点日志（journal）。设备掉线时抛 NeedReboot：
提示用户重启机器人，等它重新上线后用同一个设计文件再跑一次，会从断点继续。
"""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .cloud import CommandError, DeviceOffline
from .mapdata import MapState, fetch_core
from .plan import DEFAULTS, Plan, Step, build_plan, design_hash, normalize_design, short_args


class NeedReboot(Exception):
    """设备不回包了。请物理重启机器人，等它上线后继续。"""


class UploadStopped(Exception):
    """计划校验失败、回读不一致或用户中止。"""


class Journal:
    def __init__(self, path: Path, design: dict) -> None:
        self.path = Path(path)
        self.hash = design_hash(design)
        self.data: dict[str, Any] = {}
        if self.path.is_file():
            try:
                old = json.loads(self.path.read_text())
                if old.get("design") == self.hash and not old.get("finished"):
                    self.data = old
            except (OSError, ValueError):
                pass
        if not self.data:
            self.data = {"design": self.hash, "mid": design["mid"], "started": int(time.time()),
                         "backup": False, "topology": [], "renamed": False, "extra": False}

    @property
    def resumed(self) -> bool:
        return bool(self.data.get("backup"))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=1))

    def topo(self, i: int) -> dict:
        t = self.data["topology"]
        while len(t) <= i:
            t.append({"status": "todo"})
        return t[i]


class Uploader:
    def __init__(
        self,
        design: dict,
        session_factory: Callable[[], Any],
        journal_path: Path,
        opts: dict | None = None,
        log: Callable[[str], None] = print,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self.design = normalize_design(design)
        self.session_factory = session_factory
        self.opts = {**DEFAULTS, **(opts or {})}
        self.log = log
        self.sleep = sleep
        self.journal = Journal(journal_path, self.design)
        self.stop_requested = False
        self._base_checked = False

    # ------------------------------------------------------------------ #
    def _check_stop(self) -> None:
        if self.stop_requested:
            raise UploadStopped("已按要求在两步之间停止，可以随时继续")

    async def _send(self, s, step: Step) -> dict:
        self._check_stop()
        self.log(f"→ {step.label}")
        self.log(f"   {step.cmd} {short_args(step.args)}")
        try:
            body = await s.send(step.cmd, step.args)
        except DeviceOffline as exc:
            raise NeedReboot(f"{step.label}：{exc}") from exc
        code = body.get("code")
        self.log(f"   ← code={code} msg={body.get('msg')!r}")
        if code != 0:
            raise CommandError(step.cmd, body)
        return body

    async def _read(self, s) -> MapState:
        try:
            return await fetch_core(s, self.design["mid"])
        except DeviceOffline as exc:
            raise NeedReboot(f"回读地图时设备没有回包：{exc}") from exc

    def _plan(self, state: MapState, done: int) -> Plan:
        check = not self.journal.resumed and not self._base_checked
        self._base_checked = True
        plan = build_plan(self.design, state, self.opts, topology_done=done, check_base=check)
        if plan.errors:
            raise UploadStopped("计划校验没通过：\n" + "\n".join(f"  ✖ {e}" for e in plan.errors))
        return plan

    # ------------------------------------------------------------------ #
    async def run(self) -> MapState:
        j = self.journal
        actions = self.design["actions"]
        wait = float(self.opts["readback_wait_s"])
        async with self.session_factory() as s:
            state = await self._read(s)
            if j.resumed:
                done = sum(1 for t in j.data["topology"] if t.get("status") == "done")
                parts = ["备份"] + ([f"{done} 个拓扑动作"] if j.data["topology"] else []) + (["改名"] if j.data.get("renamed") else [])
                self.log(f"从断点继续（已完成：{'、'.join(parts)}）")

            # ---------- 备份 ----------
            if not j.data["backup"]:
                plan = self._plan(state, 0)
                await self._send(s, plan.steps[0])
                j.data["backup"] = True
                j.save()
                await self.sleep(2)

            # ---------- 切割 / 合并 ----------
            for i, a in enumerate(actions):
                t = j.topo(i)
                if t["status"] == "done":
                    continue
                expect_delta = 1 if a.get("kind") in ("cut", "divide") else None
                if t["status"] == "sent":
                    # 上次发出后掉线了：看房间数判断有没有生效
                    if len(state.rooms) == t["pre"] + t["delta"]:
                        self.log(f"拓扑动作 {i + 1} 上次已生效（房间数 {t['pre']} → {len(state.rooms)}）")
                        t["status"] = "done"
                        j.save()
                        continue
                    self.log(f"拓扑动作 {i + 1} 上次没有生效，重新发送")
                plan = self._plan(state, i)
                step = next(st for st in plan.steps if st.phase == "topology")
                n_members = len(step.args["subsets"])
                delta = expect_delta if expect_delta is not None else -(n_members - 1)
                t.update(status="sent", pre=len(state.rooms), delta=delta)
                j.save()
                await self._send(s, step)
                await self.sleep(wait)
                state = await self._read(s)
                if len(state.rooms) != t["pre"] + delta:
                    raise UploadStopped(f"{step.label} 返回成功，但房间数 {t['pre']} → {len(state.rooms)}，和预期不符，已停止")
                t["status"] = "done"
                j.save()
                self.log(f"   ✓ 房间数 {t['pre']} → {len(state.rooms)}")

            # ---------- 改名 ----------
            plan = self._plan(state, len(actions))
            renames = [st for st in plan.steps if st.phase == "rename"]
            for st in renames:
                await self._send(s, st)
                await self.sleep(2)
            if renames:
                state = await self._read(s)
                bad = []
                for st in renames:
                    sub = st.args["subsets"][0]
                    r = state.room(sub["mssid"])
                    if not r or r["name"] != sub["name"]:
                        bad.append(sub["mssid"])
                if bad:
                    raise UploadStopped(f"改名后回读不一致：房间 {bad}")
                self.log(f"   ✓ {len(renames)} 个房间改名已核对")
            # 改名步骤按设备当前名字现算，续传时不会重发；这里只记录进度
            if not j.data.get("renamed"):
                j.data["renamed"] = True
                j.save()

            # ---------- 其它命令（未实测，默认不发） ----------
            extras = [st for st in plan.steps if st.phase == "extra"]
            if extras and not j.data.get("extra"):
                for st in extras:
                    await self._send(s, st)
                    await self.sleep(2)
                j.data["extra"] = True
                j.save()
                state = await self._read(s)

            # ---------- 写墙 ----------
            plan = self._plan(state, len(actions))
            walls = [st for st in plan.steps if st.phase == "walls"]
            if not walls:
                self.log("墙体已经和设计一致，不用写")
            for k, st in enumerate(walls, 1):
                await self._send(s, st)
                await self.sleep(wait)
                state = await self._read(s)
                if state.g1 != st.args["info"]:
                    raise UploadStopped(
                        f"{st.label}：回读的组 1 和写入的不一致（固件可能丢弃了这次写入）。"
                        "可以把「每步最大移动」调小后重新上传，会从当前状态继续"
                    )
                self.log(f"   ✓ 回读一致（{k}/{len(walls)}）")

            j.data["finished"] = int(time.time())
            j.save()
            self.log("全部完成")
            return state


async def wait_online(session_factory, timeout: float = 900, interval: float = 15, log=print, sleep=asyncio.sleep) -> bool:
    """轮询 getWorkState，直到设备回包。"""
    deadline = time.monotonic() + timeout
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        try:
            async with session_factory() as s:
                while time.monotonic() < deadline:
                    try:
                        body = await s.send("getWorkState", {}, timeout=20)
                        if body.get("code") == 0:
                            log(f"[{attempt}] 设备已在线")
                            return True
                    except DeviceOffline:
                        log(f"[{attempt}] 设备还没上线…")
                    attempt += 1
                    await sleep(interval)
        except Exception as exc:  # 登录失败等：重建会话再试
            log(f"[{attempt}] {type(exc).__name__}: {exc}")
            await sleep(interval)
    return False
