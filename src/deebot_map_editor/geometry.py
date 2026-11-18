"""平面几何小工具。坐标单位都是 mm，点用 (x, y) 元组或列表表示。"""
from __future__ import annotations

import math
from collections.abc import Iterable, Sequence

Pt = Sequence[float]


def dist(a: Pt, b: Pt) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def project(p: Pt, a: Pt, b: Pt) -> tuple[float, tuple[float, float]]:
    """p 投影到线段 ab 上，返回 (参数 t∈[0,1], 投影点)。"""
    dx, dy = b[0] - a[0], b[1] - a[1]
    l2 = dx * dx + dy * dy
    if l2 == 0:
        return 0.0, (a[0], a[1])
    t = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / l2
    t = max(0.0, min(1.0, t))
    return t, (a[0] + t * dx, a[1] + t * dy)


def dist_seg(p: Pt, a: Pt, b: Pt) -> float:
    return dist(p, project(p, a, b)[1])


def edges(ring: Sequence[Pt]) -> Iterable[tuple[int, Pt, Pt]]:
    """闭合多边形的每条边 (i, 起点, 终点)，第 i 条边以第 i 个点结尾（和墙点 T 的含义一致）。"""
    n = len(ring)
    for i in range(n):
        yield i, ring[(i - 1) % n], ring[i]


def point_in_poly(p: Pt, ring: Sequence[Pt]) -> bool:
    x, y = p[0], p[1]
    inside = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i][0], ring[i][1]
        x2, y2 = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
        if (y1 > y) != (y2 > y):
            xi = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < xi:
                inside = not inside
    return inside


def area(ring: Sequence[Pt]) -> float:
    s = 0.0
    n = len(ring)
    for i in range(n):
        a, b = ring[i], ring[(i + 1) % n]
        s += a[0] * b[1] - b[0] * a[1]
    return s / 2


def _orient(a: Pt, b: Pt, c: Pt) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def seg_cross(a: Pt, b: Pt, c: Pt, d: Pt) -> bool:
    """两条线段严格相交（不含端点接触、不含共线重叠）。"""
    o1, o2 = _orient(a, b, c), _orient(a, b, d)
    o3, o4 = _orient(c, d, a), _orient(c, d, b)
    return o1 * o2 < 0 and o3 * o4 < 0


def line_intersection(a: Pt, b: Pt, c: Pt, d: Pt) -> tuple[float, float, tuple[float, float]] | None:
    """直线 ab 与线段 cd 的交点：返回 (ab 上参数 s, cd 上参数 u, 交点)；平行返回 None。"""
    r = (b[0] - a[0], b[1] - a[1])
    q = (d[0] - c[0], d[1] - c[1])
    den = r[0] * q[1] - r[1] * q[0]
    if abs(den) < 1e-9:
        return None
    s = ((c[0] - a[0]) * q[1] - (c[1] - a[1]) * q[0]) / den
    u = ((c[0] - a[0]) * r[1] - (c[1] - a[1]) * r[0]) / den
    return s, u, (a[0] + s * r[0], a[1] + s * r[1])


def self_intersects(ring: Sequence[Pt]) -> bool:
    n = len(ring)
    if n < 4:
        return False
    for i in range(n):
        a, b = ring[i], ring[(i + 1) % n]
        for j in range(i + 2, n):
            if i == 0 and j == n - 1:
                continue
            c, d = ring[j], ring[(j + 1) % n]
            if seg_cross(a, b, c, d):
                return True
    return False


def nearest_on_ring(p: Pt, ring: Sequence[Pt]) -> tuple[float, int, float, tuple[float, float]]:
    """环上离 p 最近的点：(距离, 边下标 i, 参数 t, 点)。边 i 从 ring[i-1] 到 ring[i]。"""
    best = None
    for i, a, b in edges(ring):
        t, q = project(p, a, b)
        d = dist(p, q)
        if best is None or d < best[0]:
            best = (d, i, t, q)
    assert best is not None
    return best


def collinear_overlap(a: Pt, b: Pt, c: Pt, d: Pt, tol: float = 10.0, min_len: float = 50.0) -> bool:
    """线段 ab 与 cd 是否几乎共线且有重叠部分。"""
    lab = dist(a, b)
    lcd = dist(c, d)
    if lab < 1 or lcd < 1:
        return False
    # 方向夹角
    cross = abs((b[0] - a[0]) * (d[1] - c[1]) - (b[1] - a[1]) * (d[0] - c[0])) / (lab * lcd)
    if cross > math.sin(math.radians(3)):
        return False
    # 横向距离
    if dist_seg(c, a, b) > tol and dist_seg(d, a, b) > tol and dist_seg(a, c, d) > tol and dist_seg(b, c, d) > tol:
        return False
    # 沿 ab 方向的投影重叠
    ux, uy = (b[0] - a[0]) / lab, (b[1] - a[1]) / lab
    s1 = (c[0] - a[0]) * ux + (c[1] - a[1]) * uy
    s2 = (d[0] - a[0]) * ux + (d[1] - a[1]) * uy
    lo, hi = max(0.0, min(s1, s2)), min(lab, max(s1, s2))
    return hi - lo >= min_len


def sample_points(ring: Sequence[Pt], step: float = 100.0, limit: int = 4000) -> list[tuple[float, float]]:
    """在多边形内部按网格取样，用来判断两个多边形的重叠关系。"""
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    w, h = max(xs) - min(xs), max(ys) - min(ys)
    while w * h / (step * step) > limit:
        step *= 1.5
    out = []
    y = min(ys) + step / 2
    while y < max(ys):
        x = min(xs) + step / 2
        while x < max(xs):
            if point_in_poly((x, y), ring):
                out.append((x, y))
            x += step
        y += step
    if not out:
        cx = sum(xs) / len(xs)
        cy = sum(ys) / len(ys)
        out.append((cx, cy))
    return out


def split_ring(ring: Sequence[Pt], p1: Pt, p2: Pt) -> tuple[list, list] | None:
    """用弦 p1→p2 把多边形切成两半（p1、p2 需在边界上）。返回两个点列表；失败返回 None。"""
    n = len(ring)
    d1, i1, t1, q1 = nearest_on_ring(p1, ring)
    d2, i2, t2, q2 = nearest_on_ring(p2, ring)
    pos1 = ((i1 - 1) + t1) % n
    pos2 = ((i2 - 1) + t2) % n
    if abs(pos1 - pos2) < 1e-9:
        return None

    def between(pa: float, pb: float) -> list[int]:
        db = (pb - pa) % n
        js = [(j, (j - pa) % n) for j in range(n)]
        return [j for j, d in sorted(js, key=lambda x: x[1]) if 1e-9 < d < db - 1e-9]

    h1 = [tuple(q1)] + [tuple(ring[j][:2]) for j in between(pos1, pos2)] + [tuple(q2)]
    h2 = [tuple(q2)] + [tuple(ring[j][:2]) for j in between(pos2, pos1)] + [tuple(q1)]
    if len(h1) < 3 or len(h2) < 3:
        return None
    return h1, h2


def convex_hull(points: Iterable[Pt]) -> list[tuple[float, float]]:
    pts = sorted({(p[0], p[1]) for p in points})
    if len(pts) <= 2:
        return pts
    lower: list = []
    for p in pts:
        while len(lower) >= 2 and _orient(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: list = []
    for p in reversed(pts):
        while len(upper) >= 2 and _orient(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]
