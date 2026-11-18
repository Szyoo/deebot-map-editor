"""上传规划：把编辑器导出的「设计」变成一串可以安全发送的命令。

固件的真实规则（实测）决定了整个流程的顺序：

1. 备份：setCachedMapInfo {act:"backup"}。
2. 拓扑：先把全部 divide / merge 做完。它们会让固件重新生成全图组 1，并清空房间名，
   所以墙和名字都必须放在后面。
   - divide 的两个端点必须落在被切房间的组 6（清扫边界）上，容差约 10 cm；
   - 切线和组 6 的某条边共线会失败（20004）；
   - value 是设备坐标，y 不取反。
3. 改名：按几何位置把设计里的房间对应到设备上的 mssid，再逐个 rename。
4. 写墙：setMapInfo_V2 {type:"1"} 一次改动太大，设备会掉线并丢弃写入，
   所以逐间写，大改动再拆成小步；每步等约 6 秒回读核对。

本模块全是纯函数，不连设备，便于测试和 dry-run。
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field

from . import geometry as G
from .mapdata import MapState, format_seg

DESIGN_KIND = "deebot-map-design"

# 默认参数（都可以在调用时改）
DEFAULTS = {
    "divide_tol_mm": 100,       # 端点离组 6 边界的容差
    "divide_extend_mm": 600,    # 端点不在边界上时，沿切线方向最多延长/缩短多少去找边界
    "max_move_mm": 300,         # 写墙时每一步单个角点最多移动多少
    "readback_wait_s": 6,       # 写入后等多久回读
    "include_untested": False,  # 是否发送编辑器里「未实测」的其它命令
}


@dataclass
class Step:
    phase: str                  # backup / topology / rename / extra / walls
    label: str
    cmd: str
    args: dict
    note: str = ""
    provisional: bool = False   # dry-run 时还不能确定的步骤（要等前面的切割结果）
    room: str = ""              # 写墙步骤对应的设计房间 id
    level: str = "ok"           # ok / warn


@dataclass
class Plan:
    steps: list[Step] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    info: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "steps": [asdict(s) for s in self.steps],
            "errors": self.errors,
            "warnings": self.warnings,
            "info": self.info,
            "hash": self.hash(),
        }

    def hash(self) -> str:
        key = json.dumps([[s.cmd, s.args] for s in self.steps], ensure_ascii=False, sort_keys=True)
        return hashlib.sha1(key.encode()).hexdigest()[:12]


# --------------------------------------------------------------------------- #
# 设计文件
# --------------------------------------------------------------------------- #
def normalize_design(design: dict) -> dict:
    if design.get("kind") != DESIGN_KIND:
        raise ValueError("不是地图设计文件（kind 不对）")
    d = dict(design)
    d["mid"] = str(d["mid"])
    d["rooms"] = {str(k): v for k, v in (d.get("rooms") or {}).items()}
    d["walls"] = {str(k): [[int(round(p[0])), int(round(p[1])), str(p[2])] for p in v] for k, v in (d.get("walls") or {}).items()}
    d["actions"] = list(d.get("actions") or [])
    d["extra"] = list(d.get("extra") or [])
    return d


def design_hash(design: dict) -> str:
    key = json.dumps({k: design.get(k) for k in ("mid", "rooms", "walls", "actions", "extra")}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(key.encode()).hexdigest()[:12]


def xy(pts) -> list[tuple[float, float]]:
    return [(p[0], p[1]) for p in pts]


# --------------------------------------------------------------------------- #
# 切割（divide）端点校验
# --------------------------------------------------------------------------- #
def check_divide(ring6: list, p1, p2, tol: float = 100, extend: float = 600) -> tuple[list[int] | None, list[int] | None, list[str], list[str]]:
    """校验并修正一条切线。返回 (端点1, 端点2, 错误, 提示)。端点为 None 表示无法修正。"""
    ring = xy(ring6)
    errors: list[str] = []
    notes: list[str] = []
    a, b = (float(p1[0]), float(p1[1])), (float(p2[0]), float(p2[1]))
    if G.dist(a, b) < 100:
        return None, None, ["切线太短（< 10 cm）"], notes

    def fix(p, other, name):
        d, _, _, q = G.nearest_on_ring(p, ring)
        if d <= tol:
            if d > 1:
                notes.append(f"{name}离清扫边界 {d:.0f} mm，已吸附到边界上")
            return q
        # 沿切线方向找和边界的交点
        best = None
        for _, e0, e1 in G.edges(ring):
            hit = G.line_intersection(other, p, e0, e1)
            if hit is None:
                continue
            s, u, pt = hit
            if -1e-9 <= u <= 1 + 1e-9 and s > 0.05:
                moved = G.dist(pt, p)
                if best is None or moved < best[0]:
                    best = (moved, pt)
        if best and best[0] <= extend:
            notes.append(f"{name}不在清扫边界上（差 {d:.0f} mm），已沿切线方向移动 {best[0]:.0f} mm 到边界")
            return best[1]
        errors.append(f"{name}离清扫边界 {d:.0f} mm，超过 {tol:.0f} mm 容差，沿切线方向也找不到边界")
        return None

    qa = fix(a, b, "起点")
    qb = fix(b, a, "终点")
    if qa is None or qb is None:
        return None, None, errors, notes
    ra = [int(round(qa[0])), int(round(qa[1]))]
    rb = [int(round(qb[0])), int(round(qb[1]))]
    mid = ((ra[0] + rb[0]) / 2, (ra[1] + rb[1]) / 2)
    if not G.point_in_poly(mid, ring):
        errors.append("切线中点不在房间的清扫边界内")
    for _, e0, e1 in G.edges(ring):
        # 实测：和边平行但相距 25 mm（组 6 的一格）可以切；真正重合才失败
        if G.collinear_overlap(ra, rb, e0, e1, tol=10):
            errors.append("切线和清扫边界的一条边共线，固件会返回 20004")
            break
    for _, e0, e1 in G.edges(ring):
        if G.seg_cross(ra, rb, e0, e1):
            errors.append("切线中途穿出了房间的清扫边界")
            break
    return ra, rb, errors, notes


# --------------------------------------------------------------------------- #
# 设计房间 → 设备房间（按几何位置）
# --------------------------------------------------------------------------- #
def resolve_rooms(design_walls: dict[str, list], device_g6: dict[str, list]) -> tuple[dict[str, str], dict[str, float]]:
    """每个设计房间在内部取样，看样点落在哪个设备房间的组 6 里最多。返回 (映射, 一致比例)。

    组 6 只覆盖能扫到的区域，往往比墙围出来的范围小，所以只统计落进某个房间的样点。"""
    out: dict[str, str] = {}
    score: dict[str, float] = {}
    rings = {m: xy(r) for m, r in device_g6.items() if len(r) >= 3}
    for rid, pts in design_walls.items():
        if len(pts) < 3:
            continue
        samples = G.sample_points(xy(pts))
        votes: dict[str, int] = {}
        for s in samples:
            for m, ring in rings.items():
                if G.point_in_poly(s, ring):
                    votes[m] = votes.get(m, 0) + 1
                    break
        if votes:
            m, v = max(votes.items(), key=lambda kv: kv[1])
            out[rid] = m
            score[rid] = v / sum(votes.values())  # 落在某个房间里的样点中，归属这个房间的比例
    return out, score


def room_at(point, device_g6: dict[str, list]) -> str | None:
    for m, r in device_g6.items():
        if len(r) >= 3 and G.point_in_poly(point, xy(r)):
            return m
    return None


# --------------------------------------------------------------------------- #
# 写墙拆步
# --------------------------------------------------------------------------- #
def _align(cur: list, tgt: list, gap: float = 1500.0) -> list[tuple]:
    """环形序列对齐（带插入/删除）。返回按当前环顺序的操作：('m',i,j) / ('d',i) / ('i',j)。"""
    n, m = len(cur), len(tgt)
    best = None
    for rot in range(m):
        t = [tgt[(rot + k) % m] for k in range(m)]
        idx = [(rot + k) % m for k in range(m)]
        # 让当前第 0 点和目标第 0 点（旋转后）对齐的 Needleman–Wunsch
        INF = float("inf")
        dp = [[INF] * (m + 1) for _ in range(n + 1)]
        bt = [[None] * (m + 1) for _ in range(n + 1)]
        dp[0][0] = 0.0
        for i in range(n + 1):
            for j in range(m + 1):
                if i == 0 and j == 0:
                    continue
                cands = []
                if i > 0 and j > 0:
                    cost = G.dist(cur[i - 1], t[j - 1])
                    if cur[i - 1][2] != t[j - 1][2]:
                        cost += 50
                    cands.append((dp[i - 1][j - 1] + cost, "m"))
                if i > 0:
                    cands.append((dp[i - 1][j] + gap, "d"))
                if j > 0:
                    cands.append((dp[i][j - 1] + gap, "i"))
                dp[i][j], bt[i][j] = min(cands, key=lambda c: c[0])
        if best is None or dp[n][m] < best[0]:
            ops = []
            i, j = n, m
            while i > 0 or j > 0:
                op = bt[i][j]
                if op == "m":
                    ops.append(("m", i - 1, idx[j - 1]))
                    i, j = i - 1, j - 1
                elif op == "d":
                    ops.append(("d", i - 1))
                    i -= 1
                else:
                    ops.append(("i", idx[j - 1]))
                    j -= 1
            best = (dp[n][m], ops[::-1])
    assert best is not None
    return best[1]


def _place_on(a, b, k: int, total: int):
    """线段 a→b 上第 k 个（共 total 个）均匀分布的点，留在线段内部。"""
    f = (k + 1) / (total + 1)
    return (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f)


def wall_steps(cur: list, tgt: list, max_move: float = 300) -> list[tuple[str, list]]:
    """把一个房间的墙从 cur 改成 tgt，拆成多步。每步返回 (说明, 完整墙点)。最后一步等于 tgt。

    拆法：
      1. 插点：目标里多出来的角点，先插在当前墙上（几何不变）；
      2. 移动：所有角点从起点线性移到终点，每步最多移动 max_move；
      3. 改类型：墙段类型一次改到位（几何不变）；
      4. 删点：多余的角点此时已落在目标墙线上，删掉（几何不变）。
    """
    cur = [[int(p[0]), int(p[1]), str(p[2])] for p in cur]
    tgt = [[int(p[0]), int(p[1]), str(p[2])] for p in tgt]
    if cur == tgt:
        return []
    if len(cur) < 3 or len(tgt) < 3:
        return [("整间替换", tgt)]
    ops = _align(cur, tgt)
    # 把每个空隙里的操作排成「先删后插」
    anchors = [k for k, o in enumerate(ops) if o[0] == "m"]
    if not anchors:
        return [("整间替换（新旧墙对不上，无法拆步）", tgt)]
    # 旋转使第一个元素是锚点
    ops = ops[anchors[0]:] + ops[:anchors[0]]
    seq: list[tuple] = []
    k = 0
    while k < len(ops):
        seq.append(ops[k])
        k += 1
        gap = []
        while k < len(ops) and ops[k][0] != "m":
            gap.append(ops[k])
            k += 1
        seq += [o for o in gap if o[0] == "d"] + [o for o in gap if o[0] == "i"]

    # 每个元素：起点、终点、起始类型、目标类型、是否保留到最后
    items = []
    for o in seq:
        if o[0] == "m":
            c, t = cur[o[1]], tgt[o[2]]
            items.append({"s": (c[0], c[1]), "e": (t[0], t[1]), "ts": c[2], "te": t[2], "keep": True})
        elif o[0] == "d":
            c = cur[o[1]]
            items.append({"s": (c[0], c[1]), "e": None, "ts": c[2], "te": c[2], "keep": False})
        else:
            t = tgt[o[1]]
            items.append({"s": None, "e": (t[0], t[1]), "ts": None, "te": t[2], "keep": True})
    n = len(items)
    # 插入点的起点：落在当前墙上（前一个已有点 → 后一个已有点之间），类型取后一段的类型
    k = 0
    while k < n:
        if items[k]["s"] is None:
            j = k
            while items[j % n]["s"] is None:
                j += 1
            run = list(range(k, j))
            prev = items[(k - 1) % n]["s"]
            nxt = items[j % n]
            for idx, r in enumerate(run):
                items[r]["s"] = _place_on(prev, nxt["s"], idx, len(run))
                items[r]["ts"] = nxt["ts"]
            k = j
        else:
            k += 1
    # 删除点的终点：落在目标墙上（前一个保留点 → 后一个保留点之间）
    k = 0
    while k < n:
        if items[k]["e"] is None:
            j = k
            while items[j % n]["e"] is None:
                j += 1
            run = list(range(k, j))
            prev = items[(k - 1) % n]["e"]
            nxt = items[j % n]["e"]
            for idx, r in enumerate(run):
                items[r]["e"] = _place_on(prev, nxt, idx, len(run))
            k = j
        else:
            k += 1

    def ring(f: float, types: str, keep_only: bool) -> list:
        out = []
        for it in items:
            if keep_only and not it["keep"]:
                continue
            x = it["s"][0] + (it["e"][0] - it["s"][0]) * f
            y = it["s"][1] + (it["e"][1] - it["s"][1]) * f
            out.append([int(round(x)), int(round(y)), it[types]])
        return out

    steps: list[tuple[str, list]] = []

    def push(label: str, pts: list) -> None:
        last = steps[-1][1] if steps else cur
        if pts != last:
            steps.append((label, pts))

    if any(o[0] == "i" for o in seq):
        push(f"插入 {sum(1 for o in seq if o[0] == 'i')} 个角点（几何不变）", ring(0.0, "ts", False))
    move = max(G.dist(it["s"], it["e"]) for it in items)
    parts = max(1, math.ceil(move / max_move)) if move > 0.5 else 0
    for p in range(1, parts + 1):
        push(f"移动角点 {p}/{parts}（单点最多 {move / parts:.0f} mm）", ring(p / parts, "ts", False))
    push("改墙段类型", ring(1.0, "te", False))
    if any(o[0] == "d" for o in seq):
        push(f"删除 {sum(1 for o in seq if o[0] == 'd')} 个角点", ring(1.0, "te", True))
    # 保证最后一步与目标完全一致（包括点的起始位置）
    if not steps or not _same_ring(steps[-1][1], tgt):
        push("对齐到目标", tgt)
    elif steps[-1][1] != tgt:
        steps[-1] = (steps[-1][0], tgt)
    return steps


def _same_ring(a: list, b: list) -> bool:
    if len(a) != len(b):
        return False
    n = len(a)
    for r in range(n):
        if all(a[(r + k) % n] == b[k] for k in range(n)):
            return True
    return False


# --------------------------------------------------------------------------- #
# 生成计划
# --------------------------------------------------------------------------- #
def build_plan(design: dict, state: MapState, opts: dict | None = None, *, topology_done: int = 0, check_base: bool = True) -> Plan:
    """按当前设备状态生成计划。topology_done：已经完成的拓扑动作个数（续传时用）。

    有拓扑动作时，后面的改名和写墙要等切割结果出来才能确定，这里按「模拟切割后的结果」估算，
    并标记为 provisional；真正执行时每个阶段都会重新读取设备状态后再算一遍。
    """
    o = {**DEFAULTS, **(opts or {})}
    d = normalize_design(design)
    plan = Plan()
    if d["mid"] != state.mid:
        plan.errors.append("设计文件的地图 mid 和机器人当前地图不一致")
        return plan
    try:
        state.index_map()
    except ValueError as exc:
        plan.errors.append(str(exc))
        return plan
    base_sig = (d.get("base") or {}).get("signature")
    if check_base and topology_done == 0 and base_sig and base_sig != state.signature():
        plan.errors.append("机器人上的地图在你打开编辑器之后变过（房间或墙不一致）。请重新读取地图后再编辑")

    mid = state.mid
    plan.steps.append(Step("backup", "备份当前地图", "setCachedMapInfo", {"act": "backup", "mid": mid}, "写入前先备份，出问题可以 restore"))

    # ---------------- 拓扑：切割 / 合并 ----------------
    g6 = {m: [list(p) for p in r] for m, r in state.g6.items()}
    sim_names: dict[str, str] = {}  # 模拟出来的新房间 → 占位名
    touched: set[str] = set()       # 会被切割/合并清空名字的房间
    actions = d["actions"]
    provisional = False
    for idx, a in enumerate(actions):
        kind = a.get("kind")
        if idx < topology_done:
            continue
        if kind in ("cut", "divide"):
            p1, p2 = a["p1"], a["p2"]
            mid_pt = ((p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2)
            target = room_at(mid_pt, g6)
            name = (d["rooms"].get(str(a.get("mssid"))) or {}).get("name") or str(a.get("mssid"))
            if target is None:
                plan.errors.append(f"切割「{name}」：切线中点不在任何房间的清扫边界内")
                continue
            ra, rb, errs, notes = check_divide(g6[target], p1, p2, o["divide_tol_mm"], o["divide_extend_mm"])
            for e in errs:
                plan.errors.append(f"切割「{name}」：{e}")
            if ra is None:
                continue
            label_target = sim_names.get(target, target)
            plan.steps.append(Step(
                "topology", f"切割房间 {label_target}（{name}）", "setMapSet_V2",
                {"mid": mid, "subsets": [{"type": "ar", "act": "divide", "mssid": target, "value": f"{ra[0]},{ra[1]};{rb[0]},{rb[1]}"}]},
                "；".join(notes), provisional=provisional or target in sim_names,
            ))
            halves = G.split_ring(xy(g6[target]), ra, rb)
            if halves:
                h1, h2 = halves
                big, small = (h1, h2) if abs(G.area(h1)) >= abs(G.area(h2)) else (h2, h1)
                new_id = f"新{idx + 1}"
                g6[target] = [list(p) for p in big]
                g6[new_id] = [list(p) for p in small]
                sim_names[new_id] = f"（切割 {idx + 1} 产生的新房间）"
                touched.update((target, new_id))
            provisional = True
        elif kind == "merge":
            ids = [str(x) for x in a.get("ids") or []]
            members = []
            for rid in ids:
                pts = d["walls"].get(rid)
                if not pts:
                    plan.errors.append(f"合并：设计里找不到房间 {rid} 的墙")
                    continue
                res, _ = resolve_rooms({rid: pts}, g6)
                if rid not in res:
                    plan.errors.append(f"合并：房间 {rid} 对应不到机器人上的房间")
                    continue
                members.append(res[rid])
            members = list(dict.fromkeys(members))
            if len(members) < 2:
                plan.errors.append(f"合并 {ids}：至少要两个不同的房间")
                continue
            plan.steps.append(Step(
                "topology", f"合并房间 {' + '.join(sim_names.get(m, m) for m in members)}", "setMapSet_V2",
                {"mid": mid, "subsets": [{"act": "merge", "mssid": m, "type": "ar"} for m in members]},
                "合并后保留最小的 mssid，名字被清空", provisional=provisional or any(m in sim_names for m in members),
            ))
            keep = min(members, key=lambda m: (m in sim_names, int(m) if m.isdigit() else 1e9))
            g6[keep] = [list(p) for p in G.convex_hull([p for m in members for p in g6[m]])]
            for m in members:
                if m != keep:
                    g6.pop(m, None)
            touched.add(keep)
            provisional = True
        else:
            plan.warnings.append(f"未知动作 {kind}，已忽略")

    # ---------------- 房间对应 ----------------
    merged_ids = {str(x) for a in actions if a.get("kind") == "merge" for x in (a.get("ids") or [])[1:]}
    merge_all = {str(x) for a in actions if a.get("kind") == "merge" for x in (a.get("ids") or [])}
    design_walls = {rid: pts for rid, pts in d["walls"].items() if rid not in merged_ids}
    mapping, score = resolve_rooms(design_walls, g6)
    used: dict[str, str] = {}
    for rid, m in mapping.items():
        nm = (d["rooms"].get(rid) or {}).get("name") or rid
        if m in used:
            plan.errors.append(f"设计房间「{nm}」和「{(d['rooms'].get(used[m]) or {}).get('name')}」对应到了同一个机器人房间 {m}")
        used[m] = rid
        if score.get(rid, 0) < 0.5:
            plan.warnings.append(f"设计房间「{nm}」和机器人房间 {m} 的重叠只有 {score.get(rid, 0):.0%}（其余落在别的房间里），请确认分房是否正确")
    for rid in design_walls:
        if rid not in mapping:
            plan.errors.append(f"设计房间「{(d['rooms'].get(rid) or {}).get('name') or rid}」对应不到机器人上的任何房间（是否漏了切割？）")
    for m in g6:
        if m not in used:
            plan.warnings.append(f"机器人房间 {sim_names.get(m, m)} 在设计里没有对应，名字和墙保持不变")

    # ---------------- 改名 ----------------
    for rid, m in sorted(mapping.items(), key=lambda kv: kv[1]):
        want = d["rooms"].get(rid) or {}
        if not want:
            continue
        dev = state.room(m) if m not in sim_names else None
        cleared = m in touched
        cur_name, cur_sub = (dev["name"], dev["subtype"]) if dev else ("", "0")
        if cleared or cur_name != want.get("name") or cur_sub != str(want.get("subtype", "0")):
            plan.steps.append(Step(
                "rename", f"改名 {sim_names.get(m, m)} → {want.get('name')}", "setMapSet_V2",
                {"mid": mid, "subsets": [{"act": "rename", "mssid": m, "type": "ar", "name": want.get("name", ""), "subtype": str(want.get("subtype", "0"))}]},
                provisional=provisional or m in sim_names,
            ))

    # ---------------- 其它命令（未实测） ----------------
    for c in d["extra"]:
        args = json.loads(json.dumps(c.get("args") or {}))
        args["mid"] = mid
        ok = True
        for s in args.get("subsets") or []:
            if s.get("type") == "ar" and "mssid" in s:
                if str(s["mssid"]) in mapping:
                    s["mssid"] = mapping[str(s["mssid"])]
                else:
                    ok = False
        if not ok:
            plan.warnings.append(f"「{c.get('label')}」里的房间对应不上，已跳过")
            continue
        if o["include_untested"]:
            plan.steps.append(Step("extra", c.get("label") or c.get("cmd"), c["cmd"], args, "未实测命令", provisional=provisional, level="warn"))
        else:
            plan.info.append(f"未实测、默认不发送：{c.get('label') or c.get('cmd')}")

    # ---------------- 写墙 ----------------
    if provisional:
        # 切割/合并后固件会重新生成全部组 1，具体墙点要执行时才知道
        for rid, _m in sorted(mapping.items(), key=lambda kv: kv[1]):
            if rid in merge_all:
                continue
            plan.steps.append(Step(
                "walls", f"写墙：{(d['rooms'].get(rid) or {}).get('name') or rid}（执行时按切割后的墙重新拆步）",
                "setMapInfo_V2", {"mid": mid, "type": "1", "info": ["…"]}, provisional=True, room=rid,
            ))
        for a in actions:
            if a.get("kind") == "merge":
                plan.info.append("合并后的房间不写墙，保留固件重新生成的墙；需要的话合并完再编辑一轮")
                break
    else:
        cur_walls = state.walls()
        replace: dict[str, list] = {}
        for m in state.order:
            rid = used.get(m)
            if rid is None or rid in merge_all:
                continue
            tgt = d["walls"][rid]
            cur = cur_walls.get(m) or []
            steps = wall_steps(cur, tgt, o["max_move_mm"])
            nm = (d["rooms"].get(rid) or {}).get("name") or rid
            for k, (label, pts) in enumerate(steps, 1):
                replace[m] = pts
                info = state.g1_with(replace)
                level = "warn" if G.self_intersects(xy(pts)) else "ok"
                plan.steps.append(Step(
                    "walls", f"写墙：{nm} 第 {k}/{len(steps)} 步 · {label}", "setMapInfo_V2",
                    {"mid": mid, "type": "1", "info": info}, "这一步的墙线自相交，固件可能不接受" if level == "warn" else "",
                    room=rid, level=level,
                ))
    return plan


def plan_summary(plan: Plan) -> str:
    lines = []
    for e in plan.errors:
        lines.append(f"✖ {e}")
    for w in plan.warnings:
        lines.append(f"⚠ {w}")
    for i in plan.info:
        lines.append(f"· {i}")
    for k, s in enumerate(plan.steps, 1):
        flag = "（待定）" if s.provisional else ""
        lines.append(f"{k:>3}. [{s.phase}] {s.label}{flag}")
        lines.append(f"      {s.cmd} {short_args(s.args)}")
        if s.note:
            lines.append(f"      说明：{s.note}")
    return "\n".join(lines)


def short_args(args: dict, limit: int = 160) -> str:
    if "info" in args and isinstance(args["info"], list) and len(args["info"]) > 1:
        a = dict(args)
        a["info"] = f"<组 1，共 {len(args['info']) - 1} 个房间段>"
        return json.dumps(a, ensure_ascii=False)
    s = json.dumps(args, ensure_ascii=False)
    return s if len(s) <= limit else s[:limit] + "…"


def walls_target(design: dict, state: MapState, mapping: dict[str, str], skip: set[str]) -> dict[str, list]:
    """执行阶段用：mssid → 目标墙点。"""
    out = {}
    for rid, m in mapping.items():
        if rid in skip:
            continue
        out[m] = design["walls"][rid]
    return out


__all__ = [
    "DEFAULTS", "DESIGN_KIND", "Plan", "Step", "build_plan", "check_divide", "design_hash",
    "normalize_design", "plan_summary", "resolve_rooms", "room_at", "wall_steps", "format_seg",
]
