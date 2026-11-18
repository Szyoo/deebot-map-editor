"""生成演示用的虚构地图快照（examples/demo-snapshot.json）。

一套假想的五室户型，和任何真实住宅无关。特意让 mssid 不连续（0、1、2、4、5），
用来演示「组 1 房间段是顺序序号、不是 mssid」这条规则。

    python examples/make_demo.py
"""
import json
import time
from pathlib import Path

# 房间：mssid, 名字, 房间类型, 矩形 (x0, y0, x1, y1)，单位 mm
ROOMS = [
    ("0", "客厅", "1", (0, -6000, 5000, -2500)),
    ("1", "卧室", "3", (5000, -6000, 8000, -2500)),
    ("2", "厨房", "5", (0, -2500, 3000, 0)),
    ("4", "卫生间", "6", (3000, -2500, 5000, 0)),
    ("5", "书房", "4", (5000, -2500, 8000, 0)),
]
OUTER = (0, -6000, 8000, 0)
# 门：在共用墙上的一段 (x0, y0, x1, y1)
DOORS = [
    (5000, -4500, 5000, -3500),
    (1000, -2500, 2000, -2500),
    (3500, -2500, 4300, -2500),
    (5000, -1800, 5000, -1000),
]


def on_outer(a, b):
    x0, y0, x1, y1 = OUTER
    return (a[0] == b[0] and a[0] in (x0, x1)) or (a[1] == b[1] and a[1] in (y0, y1))


def ring(rect):
    """矩形四条边按顺时针（屏幕上 y 向下时）排列，并在门和相邻房间的角点处切开。"""
    x0, y0, x1, y1 = rect
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    # 所有可能的切分点：门端点 + 其它房间的角点
    cuts = set()
    for d in DOORS:
        cuts.add((d[0], d[1]))
        cuts.add((d[2], d[3]))
    for _, _, _, r in ROOMS:
        for c in [(r[0], r[1]), (r[2], r[1]), (r[2], r[3]), (r[0], r[3])]:
            cuts.add(c)
    pts = []
    for i in range(4):
        a, b = corners[i], corners[(i + 1) % 4]
        on = [c for c in cuts if c not in (a, b) and (
            (a[0] == b[0] == c[0] and min(a[1], b[1]) < c[1] < max(a[1], b[1])) or
            (a[1] == b[1] == c[1] and min(a[0], b[0]) < c[0] < max(a[0], b[0])))]
        on.sort(key=lambda c: abs(c[0] - a[0]) + abs(c[1] - a[1]))
        pts += on + [b]
    out = []
    for i, p in enumerate(pts):
        prev = pts[i - 1]
        if on_outer(prev, p):
            t = "1"
        elif any({prev, p} == {(d[0], d[1]), (d[2], d[3])} for d in DOORS):
            t = "3-1-0"
        else:
            t = "2"
        out.append([p[0], p[1], t])
    return out


def inset(rect, d=50):
    x0, y0, x1, y1 = rect
    return [[x0 + d, y0 + d, ""], [x1 - d, y0 + d, ""], [x1 - d, y1 - d, ""], [x0 + d, y1 - d, ""]]


def seg(key, pts):
    return key + "".join(f";{p[0]},{p[1]},{p[2]}" for p in pts)


def main():
    g1 = ["1"] + [seg(str(i), ring(r)) for i, (_, _, _, r) in enumerate(ROOMS)]
    g6 = ["6"] + [seg(m, inset(r)) for m, _, _, r in ROOMS]
    g2 = ["2"] + [seg(m, inset(r, 25)) for m, _, _, r in ROOMS]
    rooms = []
    for m, name, sub, r in ROOMS:
        cx, cy = (r[0] + r[2]) // 2, (r[1] + r[3]) // 2
        rooms.append([m, name, sub, "", "0", str(cx), str(cy), "1-0-2", " ", "0", "0"])
    snap = {
        "format": "deebot-map-snapshot",
        "version": 1,
        "fetchedAt": int(time.time()),
        "mid": "100000001",
        "meta": {"name": "演示地图", "mid": "100000001", "backupId": "1000", "mts": int(time.time()), "angle": 0},
        "rooms": rooms,
        "info0": [g1, g2, g6],
        "info5": None,
        "info7": ["7"] + g1[1:],
        "sets": {"vw": [["0", "1", "6500", "-500", "7500", "-500"]], "mw": [], "ct": [], "ds": [], "na": []},
        "pos": {"chargePos": [{"a": 90, "invalid": 0, "t": 3, "x": 2500, "y": -5800}], "deebotPos": {"a": 0, "invalid": 1, "x": 0, "y": 0}},
        "patrol": [],
        "furniture": [],
        "raster": None,
    }
    out = Path(__file__).resolve().parent.parent / "src" / "deebot_map_editor" / "demo-snapshot.json"
    out.write_text(json.dumps(snap, ensure_ascii=False, indent=1))
    print(out)


if __name__ == "__main__":
    main()
