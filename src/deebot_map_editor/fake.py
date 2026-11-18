"""模拟设备：演示模式和测试用。按实测到的固件规则做了简化模拟：

- divide：端点要落在该房间组 6 边界 10 cm 内、不能和组 6 的边共线，否则返回 20004；
  成功后切开组 6，新房间 mssid = 最大值 + 1，两个房间名字清空，全图组 1 重新生成；
- merge：保留最小 mssid，名字清空，全图组 1 重新生成；
- setMapInfo_V2：单个角点移动超过 limit_mm，或一次改了多个房间，就「掉线」并丢弃写入；
- 掉线后所有命令都不回包，直到调用 reboot()。
"""
from __future__ import annotations

import base64
import copy
import json

from . import geometry as G
from .cloud import DeviceOffline
from .mapdata import MapState, format_seg, parse_seg
from .plan import check_divide


def _enc(obj) -> str:
    return base64.b64encode(json.dumps(obj, ensure_ascii=False).encode()).decode()


class FakeCloud:
    def __init__(self, snapshot: dict, limit_mm: float = 450, drop_after_divide: bool = False) -> None:
        self.snap = copy.deepcopy(snapshot)
        self.limit_mm = limit_mm
        self.drop_after_divide = drop_after_divide
        self.offline = False
        self.sent: list[tuple[str, dict]] = []
        self.backups = 0

    # 上下文协议：和 CloudSession 用法一致
    async def __aenter__(self) -> "FakeCloud":
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    def reboot(self) -> None:
        self.offline = False

    # ------------------------------------------------------------------ #
    @property
    def state(self) -> MapState:
        return MapState.from_snapshot(self.snap)

    def _g6(self) -> dict[str, list]:
        return self.state.g6

    def _set_info0(self, g1: list[str], g6: dict[str, list]) -> None:
        seg6 = ["6"] + [format_seg(m, pts) for m, pts in g6.items()]
        seg2 = ["2"] + [format_seg(m, pts) for m, pts in g6.items()]
        self.snap["info0"] = [g1, seg2, seg6]

    def _regen_g1(self, g6: dict[str, list]) -> None:
        """固件重新生成组 1：这里简单地用组 6 外框当墙，全部标成外墙。"""
        order = [str(r[0]) for r in self.snap["rooms"]]
        g1 = ["1"]
        for i, m in enumerate(order):
            ring = G.convex_hull(g6[m]) if len(g6[m]) > 2 else g6[m]
            g1.append(format_seg(str(i), [[int(p[0]), int(p[1]), "1"] for p in ring]))
        self._set_info0(g1, g6)

    def _room(self, mssid: str) -> list | None:
        return next((r for r in self.snap["rooms"] if str(r[0]) == str(mssid)), None)

    async def send(self, name: str, args: dict, timeout: float | None = None) -> dict:
        if self.offline:
            raise DeviceOffline(f"{name}：设备未回包（模拟掉线）")
        self.sent.append((name, copy.deepcopy(args)))
        ok = {"code": 0, "msg": "ok"}
        if name == "getWorkState":
            return {**ok, "data": {"robotState": {"state": "idle"}}}
        if name == "getCachedMapInfo":
            meta = self.snap.get("meta") or {}
            return {**ok, "data": {"info": [{**meta, "mid": self.snap["mid"], "built": 1, "using": 1}]}}
        if name == "getMapSet_V2":
            t = args.get("type")
            items = self.snap["rooms"] if t == "ar" else (self.snap.get("sets") or {}).get(t) or []
            return {**ok, "data": {"mid": self.snap["mid"], "type": t, "subsets": _enc(items)}}
        if name == "getMapInfo_V2":
            t = str(args.get("type"))
            key = {"0": "info0", "5": "info5", "7": "info7"}.get(t)
            val = self.snap.get(key) if key else None
            return {**ok, "data": {"info": _enc(val)} if val else {}}
        if name in ("getPos_V2",):
            return {**ok, "data": self.snap.get("pos") or {}}
        if name == "getMapPoint":
            return {**ok, "data": {"pointValue": self.snap.get("patrol") or []}}
        if name == "getFurnitureInfo_V2":
            return {**ok, "data": {"info": self.snap.get("furniture") or []}}
        if name == "getMapInfo":
            return {**ok, "data": {}}
        if name == "setCachedMapInfo" and args.get("act") == "backup":
            self.backups += 1
            self.snap.setdefault("meta", {})["backupId"] = str(1000 + self.backups)
            return ok
        if name == "setMapSet_V2":
            return self._map_set(args)
        if name == "setMapInfo_V2":
            return self._map_info(args)
        return {"code": 20003, "msg": "rcp not support"}

    # ------------------------------------------------------------------ #
    def _map_set(self, args: dict) -> dict:
        subsets = args.get("subsets") or []
        if not subsets:
            return {"code": 20011, "msg": "format error"}
        act = subsets[0].get("act")
        g6 = self._g6()
        if act == "rename":
            for s in subsets:
                r = self._room(s["mssid"])
                if r is None:
                    return {"code": 20004, "msg": "room not found"}
                r[1], r[2] = s.get("name", ""), str(s.get("subtype", "0"))
            return {"code": 0, "msg": "ok"}
        if act == "divide":
            s = subsets[0]
            m = str(s["mssid"])
            if m not in g6:
                return {"code": 20004, "msg": "split fail"}
            (x1, y1), (x2, y2) = [[int(v) for v in p.split(",")] for p in s["value"].split(";")]
            ra, rb, errs, notes = check_divide(g6[m], (x1, y1), (x2, y2), tol=100, extend=0)
            if errs or notes and any("移动" in n for n in notes):
                return {"code": 20004, "msg": "split fail"}
            halves = G.split_ring([(p[0], p[1]) for p in g6[m]], ra, rb)
            if not halves:
                return {"code": 20004, "msg": "split fail"}
            new = str(max(int(r[0]) for r in self.snap["rooms"]) + 1)
            g6[m] = [[int(p[0]), int(p[1]), ""] for p in halves[0]]
            g6[new] = [[int(p[0]), int(p[1]), ""] for p in halves[1]]
            self._room(m)[1] = ""
            cx = sum(p[0] for p in halves[1]) / len(halves[1])
            cy = sum(p[1] for p in halves[1]) / len(halves[1])
            self.snap["rooms"].append([new, "", "0", m, "0", str(int(cx)), str(int(cy)), "1-0-2", " ", "0", "0"])
            self.snap["rooms"].sort(key=lambda r: int(r[0]))
            g6 = {str(r[0]): g6[str(r[0])] for r in self.snap["rooms"]}
            self._regen_g1(g6)
            if self.drop_after_divide:
                self.offline = True
                raise DeviceOffline("divide：设备未回包（模拟切割后掉线，切割已生效）")
            return {"code": 0, "msg": "ok"}
        if act == "merge":
            ids = [str(s["mssid"]) for s in subsets]
            if len(ids) < 2 or any(i not in g6 for i in ids):
                return {"code": 20004, "msg": "merge fail"}
            keep = min(ids, key=int)
            g6[keep] = [[int(p[0]), int(p[1]), ""] for p in G.convex_hull([p for i in ids for p in g6[i]])]
            for i in ids:
                if i != keep:
                    g6.pop(i)
            self.snap["rooms"] = [r for r in self.snap["rooms"] if str(r[0]) == keep or str(r[0]) not in ids]
            self._room(keep)[1] = ""
            self._regen_g1(g6)
            return {"code": 0, "msg": "ok"}
        return {"code": 0, "msg": "ok"}

    def _map_info(self, args: dict) -> dict:
        info = args.get("info")
        if args.get("type") != "1" or not isinstance(info, list) or not info or info[0] != "1":
            # 二维数组之类的错误格式：真机上会清空组 1，这里直接拒绝
            return {"code": 20011, "msg": "format error"}
        st = self.state
        old = {s.split(";", 1)[0]: parse_seg(s)[1] for s in st.g1[1:]}
        new = {s.split(";", 1)[0]: parse_seg(s)[1] for s in info[1:]}
        if set(old) != set(new):
            self.offline = True
            raise DeviceOffline("setMapInfo_V2：设备未回包（模拟：房间段对不上）")
        changed = [k for k in old if old[k] != new[k]]
        too_far = False
        for k in changed:
            a, b = old[k], new[k]
            ring = [(q[0], q[1]) for q in a]
            for p in b:
                if G.nearest_on_ring(p, ring)[0] > self.limit_mm:
                    too_far = True
        if len(changed) > 1 or too_far:
            self.offline = True
            raise DeviceOffline("setMapInfo_V2：设备未回包（模拟：改动太大，写入被丢弃）")
        self.snap["info0"] = [list(info)] + [e for e in self.snap["info0"] if e and e[0] != "1"]
        return {"code": 0, "msg": "ok"}
