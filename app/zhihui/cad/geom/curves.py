"""原语（Seg / Arc）上的确定性几何运算：求交、最近点、垂足、切点、偏移、分割。"""
from __future__ import annotations

import math

from .entities import EPS, Arc, Pt, Seg, ang, ang_in_arc, ccw_span, dist, polar

TOL = 1e-7


def seg_dir(s: Seg) -> tuple[float, float]:
    L = s.length or 1.0
    return ((s.b[0] - s.a[0]) / L, (s.b[1] - s.a[1]) / L)


def seg_t(s: Seg, p) -> float:
    """点在线段所在直线上的参数 t（0=起点，1=终点，可超出）。"""
    dx, dy = s.b[0] - s.a[0], s.b[1] - s.a[1]
    L2 = dx * dx + dy * dy
    return 0.0 if L2 < EPS else ((p[0] - s.a[0]) * dx + (p[1] - s.a[1]) * dy) / L2


def seg_point(s: Seg, t: float) -> Pt:
    return (s.a[0] + (s.b[0] - s.a[0]) * t, s.a[1] + (s.b[1] - s.a[1]) * t, s.a[2] + (s.b[2] - s.a[2]) * t)


def point_on(prim, p, tol: float = 1e-6) -> bool:
    return dist(closest(prim, p), p) <= tol


def closest(prim, p) -> Pt:
    if isinstance(prim, Seg):
        t = max(0.0, min(1.0, seg_t(prim, p)))
        return seg_point(prim, t)
    a = ang(prim.c, p) if dist(prim.c, p) > EPS else prim.a0
    if ang_in_arc(a, prim.a0, prim.a0 + prim.span):
        return polar(prim.c, prim.r, a)
    s, e = prim.start(), prim.end()
    return s if dist(s, p) <= dist(e, p) else e


def midpoint(prim) -> Pt:
    if isinstance(prim, Seg):
        return seg_point(prim, 0.5)
    return polar(prim.c, prim.r, prim.a0 + prim.span / 2)


def endpoints(prim) -> tuple[Pt, Pt]:
    return (prim.a, prim.b) if isinstance(prim, Seg) else (prim.start(), prim.end())


def at_length(prim, s: float) -> Pt:
    """沿原语走 s（mm）后的点，s 可小于 0 或大于长度（沿延长线 / 延长圆）。"""
    if isinstance(prim, Seg):
        L = prim.length or 1.0
        return seg_point(prim, s / L)
    return polar(prim.c, prim.r, prim.a0 + math.degrees(s / prim.r))


def _line_line(a: Seg, b: Seg) -> list[tuple[float, float, Pt]]:
    """无限直线求交：[(ta, tb, point)]。"""
    d1 = (a.b[0] - a.a[0], a.b[1] - a.a[1])
    d2 = (b.b[0] - b.a[0], b.b[1] - b.a[1])
    den = d1[0] * d2[1] - d1[1] * d2[0]
    if abs(den) < 1e-12:
        return []
    ta = ((b.a[0] - a.a[0]) * d2[1] - (b.a[1] - a.a[1]) * d2[0]) / den
    tb = ((b.a[0] - a.a[0]) * d1[1] - (b.a[1] - a.a[1]) * d1[0]) / den
    return [(ta, tb, seg_point(a, ta))]


def _line_circle(s: Seg, c: Arc) -> list[tuple[float, float, Pt]]:
    dx, dy = s.b[0] - s.a[0], s.b[1] - s.a[1]
    fx, fy = s.a[0] - c.c[0], s.a[1] - c.c[1]
    A = dx * dx + dy * dy
    if A < EPS:
        return []
    B = 2 * (fx * dx + fy * dy)
    C = fx * fx + fy * fy - c.r * c.r
    disc = B * B - 4 * A * C
    if disc < -1e-9:
        return []
    disc = math.sqrt(max(0.0, disc))
    ts = {(-B - disc) / (2 * A), (-B + disc) / (2 * A)} if disc > 1e-9 else {-B / (2 * A)}
    out = []
    for t in sorted(ts):
        p = seg_point(s, t)
        out.append((t, ang(c.c, p), p))
    return out


def _circle_circle(a: Arc, b: Arc) -> list[tuple[float, float, Pt]]:
    d = dist(a.c, b.c)
    if d < EPS or d > a.r + b.r + 1e-9 or d < abs(a.r - b.r) - 1e-9:
        return []
    x = (d * d + a.r * a.r - b.r * b.r) / (2 * d)
    h2 = a.r * a.r - x * x
    h = math.sqrt(max(0.0, h2))
    ux, uy = (b.c[0] - a.c[0]) / d, (b.c[1] - a.c[1]) / d
    mx, my = a.c[0] + ux * x, a.c[1] + uy * x
    pts = [(mx - uy * h, my + ux * h, 0.0)] + ([(mx + uy * h, my - ux * h, 0.0)] if h > 1e-9 else [])
    return [(ang(a.c, p), ang(b.c, p), p) for p in pts]


def intersect(p, q, extend: bool = False) -> list[Pt]:
    """两原语的交点。extend=True 时线段按无限直线、圆弧按整圆（视延伸交点）。"""
    res: list[Pt] = []
    if isinstance(p, Seg) and isinstance(q, Seg):
        for ta, tb, pt in _line_line(p, q):
            if extend or (-TOL <= ta <= 1 + TOL and -TOL <= tb <= 1 + TOL):
                res.append(pt)
    elif isinstance(p, Seg) and isinstance(q, Arc):
        for ta, aq, pt in _line_circle(p, q):
            if extend or ((-TOL <= ta <= 1 + TOL) and ang_in_arc(aq, q.a0, q.a0 + q.span)):
                res.append(pt)
    elif isinstance(p, Arc) and isinstance(q, Seg):
        res = intersect(q, p, extend)
    else:
        for ap, aq, pt in _circle_circle(p, q):
            if extend or (ang_in_arc(ap, p.a0, p.a0 + p.span) and ang_in_arc(aq, q.a0, q.a0 + q.span)):
                res.append(pt)
    # 去重
    out: list[Pt] = []
    for pt in res:
        if not any(dist(pt, o) < 1e-6 for o in out):
            out.append(pt)
    return out


def param(prim, p) -> float:
    """点在原语上的参数：Seg → t（0..1）；Arc → 沿弧走过的角度（度，0..span，可为负或超出表示延长线）。"""
    if isinstance(prim, Seg):
        return seg_t(prim, p)
    a = (ang(prim.c, p) - prim.a0) % 360
    if a > prim.span and (a - prim.span) > (360 - a):   # 更靠近起点一侧：取负
        a -= 360
    return a


def split_at(prim, pts: list[Pt]) -> list:
    """按若干点把原语切成多段（点必须在原语上，已按参数排序）。"""
    if not pts:
        return [prim]
    if isinstance(prim, Seg):
        ts = sorted({0.0, 1.0, *[min(1.0, max(0.0, seg_t(prim, p))) for p in pts]})
        return [Seg(seg_point(prim, ts[i]), seg_point(prim, ts[i + 1]), prim.approx) for i in range(len(ts) - 1) if ts[i + 1] - ts[i] > 1e-9]
    angs = sorted({0.0, *[(ang(prim.c, p) - prim.a0) % 360 for p in pts]})
    if prim.full:
        # 整圆：切点之间依次相连，首尾合并成跨过 a0 的弧
        cuts = angs[1:] if len(angs) > 1 else []
        if not cuts:
            return [prim]
        arcs = []
        for i in range(len(cuts)):
            a = cuts[i]
            b = cuts[(i + 1) % len(cuts)] + (360 if i == len(cuts) - 1 else 0)
            arcs.append(Arc(prim.c, prim.r, prim.a0 + a, prim.a0 + b))
        return arcs
    angs = sorted({0.0, prim.span, *[a for a in angs if 0 < a < prim.span]})
    return [Arc(prim.c, prim.r, prim.a0 + angs[i], prim.a0 + angs[i + 1]) for i in range(len(angs) - 1) if angs[i + 1] - angs[i] > 1e-7]


def perpendicular_feet(prim, frm: Pt) -> list[Pt]:
    """从 frm 向原语作垂线的垂足（线段所在直线 / 圆弧所在圆上的正交点，必须落在原语上）。"""
    if isinstance(prim, Seg):
        t = seg_t(prim, frm)
        return [seg_point(prim, t)] if -TOL <= t <= 1 + TOL else []
    if dist(prim.c, frm) < EPS:
        return []
    a = ang(prim.c, frm)
    out = []
    for aa in (a, (a + 180) % 360):
        if ang_in_arc(aa, prim.a0, prim.a0 + prim.span):
            out.append(polar(prim.c, prim.r, aa))
    return out


def tangent_points(prim, frm: Pt) -> list[Pt]:
    """从 frm 向圆/弧作切线的切点（落在弧上的）。直线没有切点。"""
    if isinstance(prim, Seg):
        return []
    d = dist(prim.c, frm)
    if d < prim.r - 1e-9:
        return []
    if abs(d - prim.r) < 1e-9:
        return [frm]
    base = ang(prim.c, frm)
    off = math.degrees(math.acos(prim.r / d))
    return [polar(prim.c, prim.r, a) for a in (base + off, base - off) if ang_in_arc(a % 360, prim.a0, prim.a0 + prim.span)]


def offset_prim(prim, distance: float, side_pt: Pt):
    """向 side_pt 所在一侧偏移 distance（>0）。返回新原语，无法偏移（圆内偏移过大）返回 None。"""
    if isinstance(prim, Seg):
        ux, uy = seg_dir(prim)
        nx, ny = -uy, ux
        sgn = 1.0 if ((side_pt[0] - prim.a[0]) * nx + (side_pt[1] - prim.a[1]) * ny) >= 0 else -1.0
        dx, dy = nx * distance * sgn, ny * distance * sgn
        return Seg((prim.a[0] + dx, prim.a[1] + dy, prim.a[2]), (prim.b[0] + dx, prim.b[1] + dy, prim.b[2]), prim.approx)
    outside = dist(prim.c, side_pt) >= prim.r
    r = prim.r + distance if outside else prim.r - distance
    if r <= 1e-9:
        return None
    return Arc(prim.c, r, prim.a0, prim.a0 + prim.span)


def circle_from_3(p1: Pt, p2: Pt, p3: Pt) -> tuple[Pt, float] | None:
    ax, ay = p1[0], p1[1]
    bx, by = p2[0], p2[1]
    cx, cy = p3[0], p3[1]
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:
        return None
    ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay) + (cx * cx + cy * cy) * (ay - by)) / d
    uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx) + (cx * cx + cy * cy) * (bx - ax)) / d
    return (ux, uy, 0.0), math.hypot(ax - ux, ay - uy)


def contains_point(poly: list[Pt], p: Pt) -> bool:
    """射线法：点是否在多边形内（边界上视为内）。"""
    n, inside = len(poly), False
    for i in range(n):
        x1, y1, _ = poly[i]
        x2, y2, _ = poly[(i + 1) % n]
        if abs((x2 - x1) * (p[1] - y1) - (y2 - y1) * (p[0] - x1)) < 1e-9 and min(x1, x2) - 1e-9 <= p[0] <= max(x1, x2) + 1e-9 and min(y1, y2) - 1e-9 <= p[1] <= max(y1, y2) + 1e-9:
            return True
        if (y1 > p[1]) != (y2 > p[1]) and p[0] < (x2 - x1) * (p[1] - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside
