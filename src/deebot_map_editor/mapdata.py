"""地图数据：解码、快照格式、组 1 房间段的编号映射、编辑器数据。

坐标约定：设备地图坐标单位 mm，y 轴向上为负。

几个关键事实（实测）：
- getMapInfo_V2 {type:"0"} 返回二维数组，里面有组 1（房间墙）、组 2（美化轮廓）、组 6（清扫边界）。
- 组 2、组 6 的房间段以 mssid 开头；组 1 和 type 7 的房间段开头是**顺序序号**（0,1,2…），
  按房间列表（getMapSet_V2 ar）的顺序对应 mssid。mssid 不连续时（如 0–8、10、11、14）一定要换算。
- 墙点格式 "x,y,T"：T 表示「以这个点结尾的那段墙」的类型，"1" 外墙、"2" 内墙、其它（"3-1-0"、"0"）门类段。
"""
from __future__ import annotations

import base64
import json
import time
from typing import Any

SNAPSHOT_FORMAT = "deebot-map-snapshot"


# --------------------------------------------------------------------------- #
# 编解码
# --------------------------------------------------------------------------- #
def decompress(value: str) -> Any:
    """base64 + (zstd | LZMA) → JSON 对象。"""
    return json.loads(decompress_bytes(value).decode())


def decompress_bytes(value: str) -> bytes:
    raw = base64.b64decode(value)
    if raw[:1] in (b"[", b"{"):
        return raw  # 未压缩（模拟设备用）
    if raw[:4] == b"\x28\xb5\x2f\xfd":
        try:
            from compression import zstd  # Python 3.14+

            return zstd.decompress(raw)
        except ImportError:
            pass
    # LZMA（7z 头缺 4 字节 size）或旧版 Python：交给 deebot-client 的实现
    from deebot_client.rs.util import decompress_base64_data

    return decompress_base64_data(value)


def parse_seg(seg: str) -> tuple[str, list[list]]:
    """'房间号;x,y,T;x,y,T;...' → (房间号, [[x, y, T], ...])。"""
    parts = seg.split(";")
    key = parts[0]
    pts: list[list] = []
    for p in parts[1:]:
        p = p.strip()
        if not p:
            continue
        c = p.split(",")
        if len(c) >= 2:
            pts.append([int(float(c[0])), int(float(c[1])), c[2] if len(c) > 2 else ""])
    return key, pts


def format_seg(key: str, pts: list[list]) -> str:
    return key + "".join(f";{int(round(p[0]))},{int(round(p[1]))},{p[2]}" for p in pts)


def body_of(resp: dict | None) -> dict:
    return ((resp or {}).get("resp") or {}).get("body") or {}


def data_of(resp: dict | None) -> dict:
    d = body_of(resp).get("data")
    return d if isinstance(d, dict) else {}


# --------------------------------------------------------------------------- #
# 上传用的核心状态
# --------------------------------------------------------------------------- #
class MapState:
    """一次读取得到的、上传需要的核心数据：房间列表 + 组 1 + 组 6。"""

    def __init__(self, mid: str, rooms: list[list], info0: list) -> None:
        self.mid = str(mid)
        self.rooms_raw = rooms
        self.info0 = info0
        self.rooms = [
            {"mssid": str(r[0]), "name": str(r[1]), "subtype": str(r[2]), "raw": r}
            for r in rooms
            if isinstance(r, list) and r
        ]
        self.g1: list[str] = []
        self.g6: dict[str, list[list]] = {}
        self.g2: dict[str, list[list]] = {}
        for entry in info0 or []:
            if not isinstance(entry, list) or not entry:
                continue
            gid = str(entry[0])
            if gid == "1":
                self.g1 = [str(x) for x in entry]
            elif gid in ("2", "6"):
                tgt = self.g6 if gid == "6" else self.g2
                for seg in entry[1:]:
                    if isinstance(seg, str):
                        k, pts = parse_seg(seg)
                        if pts:
                            tgt[k] = pts

    # 组 1 的序号 ↔ mssid
    @property
    def order(self) -> list[str]:
        return [r["mssid"] for r in self.rooms]

    def g1_keys(self) -> list[str]:
        return [s.split(";", 1)[0] for s in self.g1[1:]]

    def index_map(self) -> dict[str, str]:
        """组 1 序号 → mssid。数量对不上时报错（不能乱猜）。"""
        keys = self.g1_keys()
        if len(keys) != len(self.rooms):
            raise ValueError(f"组 1 有 {len(keys)} 段，房间列表有 {len(self.rooms)} 个，无法对应")
        return {k: self.rooms[i]["mssid"] for i, k in enumerate(keys)}

    def walls(self) -> dict[str, list[list]]:
        """mssid → 组 1 墙点。"""
        im = self.index_map()
        out = {}
        for seg in self.g1[1:]:
            k, pts = parse_seg(seg)
            out[im[k]] = pts
        return out

    def g1_with(self, replace: dict[str, list[list]]) -> list[str]:
        """把若干房间（按 mssid）的墙换掉，生成完整的一维组 1（写入格式）。"""
        im = self.index_map()
        out = ["1"]
        for seg in self.g1[1:]:
            k = seg.split(";", 1)[0]
            m = im[k]
            out.append(format_seg(k, replace[m]) if m in replace else seg)
        return out

    def room(self, mssid: str) -> dict | None:
        return next((r for r in self.rooms if r["mssid"] == str(mssid)), None)

    def signature(self) -> str:
        """房间列表 + 组 1 的指纹，用来判断机器人上的地图在编辑之后有没有变过。"""
        import hashlib

        key = json.dumps([[r["mssid"], r["name"], r["subtype"]] for r in self.rooms] + [self.g1], ensure_ascii=False)
        return hashlib.sha1(key.encode()).hexdigest()[:12]

    @classmethod
    def from_snapshot(cls, snap: dict) -> "MapState":
        return cls(snap["mid"], snap.get("rooms") or [], snap.get("info0") or [])


# --------------------------------------------------------------------------- #
# 读取
# --------------------------------------------------------------------------- #
async def fetch_core(session, mid: str) -> MapState:
    """读房间列表 + type 0（组 1/2/6）。设备不回包时抛 DeviceOffline。"""
    ar = await session.send("getMapSet_V2", {"mid": mid, "type": "ar"})
    sub = (ar.get("data") or {}).get("subsets")
    rooms = decompress(sub) if sub else []
    info = await session.send("getMapInfo_V2", {"mid": mid, "type": "0"})
    raw = (info.get("data") or {}).get("info")
    info0 = decompress(raw) if raw else []
    return MapState(mid, rooms, info0)


async def current_mid(session) -> tuple[str, dict]:
    body = await session.send("getCachedMapInfo", {})
    for entry in (body.get("data") or {}).get("info") or []:
        if entry.get("built") == 1 and entry.get("mid") not in (None, "", "0"):
            return str(entry["mid"]), entry
    raise RuntimeError("机器人上没有已建好的地图")


async def fetch_snapshot(session, log=print) -> dict:
    """完整只读拉取，生成快照（编辑器用）。每条命令之间留 1 秒，避免压垮设备。"""
    import asyncio

    mid, meta = await current_mid(session)
    snap: dict[str, Any] = {
        "format": SNAPSHOT_FORMAT,
        "version": 1,
        "fetchedAt": int(time.time()),
        "mid": mid,
        "meta": {k: meta.get(k) for k in ("name", "mid", "backupId", "mts", "angle")},
    }

    async def get(name: str, args: dict, optional: bool = False) -> dict:
        try:
            body = await session.send(name, args)
        except Exception:
            if optional:
                return {}
            raise
        await asyncio.sleep(1)
        d = body.get("data")
        return d if isinstance(d, dict) else {}

    def unpack(d: dict, key: str):
        v = d.get(key)
        if not v:
            return None
        try:
            return decompress(v)
        except Exception:
            return None

    log("读取房间列表…")
    snap["rooms"] = unpack(await get("getMapSet_V2", {"mid": mid, "type": "ar"}), "subsets") or []
    log("读取房间墙和清扫边界…")
    snap["info0"] = unpack(await get("getMapInfo_V2", {"mid": mid, "type": "0"}), "info") or []
    snap["info5"] = unpack(await get("getMapInfo_V2", {"mid": mid, "type": "5"}, True), "info")
    snap["info7"] = unpack(await get("getMapInfo_V2", {"mid": mid, "type": "7"}, True), "info")
    log("读取虚拟墙、地毯、门槛条等…")
    snap["sets"] = {}
    for t in ("vw", "mw", "ct", "ds", "na"):
        snap["sets"][t] = unpack(await get("getMapSet_V2", {"mid": mid, "type": t}, True), "subsets") or []
    log("读取位置、巡逻点、家具…")
    snap["pos"] = await get("getPos_V2", {"mid": mid, "type": ["chargePos", "deebotPos"]}, True)
    snap["patrol"] = (await get("getMapPoint", {"mid": mid}, True)).get("pointValue") or []
    snap["furniture"] = (await get("getFurnitureInfo_V2", {"mid": mid}, True)).get("info") or []
    snap["raster"] = None
    ol = await get("getMapInfo", {"mid": mid, "type": "ol"}, True)
    if ol.get("value"):
        try:
            cells = decompress_bytes(ol["value"])
            w = int(ol["totalWidth"])
            snap["raster"] = {
                "w": w,
                "h": int(ol["totalHeight"]),
                "pixel": ol.get("pixel", 50),
                "cells": [[i // w, i % w, v] for i, v in enumerate(cells) if v],
            }
        except Exception:
            pass
    return snap


# --------------------------------------------------------------------------- #
# 编辑器数据
# --------------------------------------------------------------------------- #
def _parse_items(items: list, skip: int) -> list[dict]:
    out = []
    for item in items or []:
        if not isinstance(item, list) or len(item) <= skip + 1:
            continue
        coords = item[skip:]
        pts = []
        for i in range(0, len(coords) - 1, 2):
            try:
                pts.append([int(float(coords[i])), int(float(coords[i + 1]))])
            except (TypeError, ValueError):
                pass
        if pts:
            out.append({"mssid": str(item[0]), "raw": item, "points": pts, "points_mm": pts})
    return out


def _group(entry: list, rekey: dict[str, str] | None = None) -> dict:
    g = {"id": str(entry[0]), "rooms": {}}
    for seg in entry[1:]:
        if isinstance(seg, str):
            k, pts = parse_seg(seg)
            if pts:
                g["rooms"][rekey.get(k, k) if rekey else k] = pts
    return g


def editor_data(snap: dict) -> dict:
    """快照 → 编辑器页面里的 D。组 1 和 type 7 的房间号换算成 mssid，和组 2/6、房间列表一致。"""
    state = MapState.from_snapshot(snap)
    try:
        im = state.index_map()
    except ValueError:
        im = {}
    groups: dict[str, dict] = {}
    for entry in snap.get("info0") or []:
        if isinstance(entry, list) and entry:
            g = _group(entry, im if str(entry[0]) == "1" else None)
            groups[g["id"]] = g
    i7 = snap.get("info7")
    if isinstance(i7, list) and i7 and isinstance(i7[0], str):
        keys7 = [s.split(";", 1)[0] for s in i7[1:] if isinstance(s, str)]
        g = _group(i7, im if len(keys7) == len(im) else None)
        g["id"] = "7"
        groups["7"] = g
    g5raw: list[str] = []
    i5 = snap.get("info5")
    if isinstance(i5, list) and i5:
        first = i5[0] if isinstance(i5[0], list) else i5
        g5raw = [str(x) for x in first[1:] if isinstance(x, str)]

    rooms = []
    for r in snap.get("rooms") or []:
        if not isinstance(r, list) or len(r) < 7:
            continue
        rooms.append({
            "mssid": str(r[0]), "name": r[1], "icon": r[2], "neighbors": r[3],
            "center_x_mm": int(float(r[5])), "center_y_mm": int(float(r[6])),
            "clean_config": r[7] if len(r) > 7 else "", "floor_type": r[9] if len(r) > 9 else "",
            "raw": r,
        })

    sets = snap.get("sets") or {}

    def vw_items(items):
        out = []
        for it in _parse_items(items, 1):
            raw = it["raw"]
            skip = 2 if str(raw[1]) in ("0", "1") else 1
            it["points"] = it["points_mm"] = _parse_items([raw], skip)[0]["points"] if len(raw) > skip + 1 else []
            out.append(it)
        return out

    furniture = []
    for f in snap.get("furniture") or []:
        pts = []
        for seg in (f.get("content") or "").split(";"):
            c = seg.split(",")
            if len(c) == 2:
                try:
                    pts.append([int(float(c[0])), int(float(c[1]))])
                except ValueError:
                    pass
        if pts:
            furniture.append({"fid": f.get("fid"), "type": f.get("type"), "points": pts, "edited": f.get("edited")})

    pos = snap.get("pos") or {}
    return {
        "meta": snap.get("meta") or {"mid": snap.get("mid")},
        "base": {"signature": state.signature(), "fetchedAt": snap.get("fetchedAt")},
        "groups": [groups[k] for k in ("7", "1", "2", "5", "6") if k in groups and groups[k]["rooms"]],
        "rooms": rooms,
        "vw": vw_items(sets.get("vw")),
        "mw": vw_items(sets.get("mw")),
        "ct": _parse_items(sets.get("ct"), 3),
        "ds": _parse_items(sets.get("ds"), 4),
        "na": _parse_items(sets.get("na"), 2),
        "furniture": furniture,
        "pos": {"charge": (pos.get("chargePos") or [{}])[0], "deebot": pos.get("deebotPos") or {}},
        "patrol": snap.get("patrol") or [],
        "block": {},
        "g5raw": g5raw,
        "raster": snap.get("raster") or {"w": 0, "h": 0, "pixel": 50, "cells": []},
    }
