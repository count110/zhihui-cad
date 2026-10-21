"""绘制图元所需的几何求解：矩形（圆角/倒角）、正多边形、网格、圆（2P/3P/TTR/TTT）、圆弧、椭圆、构造线。

每个函数返回 [{"kind", "data", "tags"}]，由工具层赋 id、图层后写入 IR。
"""
from __future__ import annotations

import math

import numpy as np

from . import curves
from .entities import EPS, Arc, Pt, Seg, ang, ccw_span, dist, pl, polar, primitives
from .errors import GeomError

TAN_QUARTER = math.tan(math.pi / 8)       # 90° 圆弧的 bulge


def ent(kind: str, data: dict, tags: list[str] | None = None) -> dict:
    return {"kind": kind, "data": data, "tags": tags or []}


def vtx(p, bulge: float = 0.0) -> dict:
    return {"p": pl(p), "bulge": round(bulge, 12), "sw": 0.0, "ew": 0.0}


# ------------------------------------------------------------------ 矩形 / 多边形 / 网格
def rectangle(c1: Pt, c2: Pt, rotation_deg: float = 0.0, fillet_r: float = 0.0, chamfer: tuple[float, float] | None = None) -> list[dict]:
    """以 c1、c2 为对角点的矩形（先在轴对齐坐标系中建，再绕 c1 旋转）。闭合多段线，tags=[rectangle]。"""
    w, h = abs(c2[0] - c1[0]), abs(c2[1] - c1[1])
    if w < EPS or h < EPS:
        raise GeomError("DEGENERATE", "矩形的长或宽为 0", "给出不重合的两个对角点，或长和宽")
    x0, y0 = min(c1[0], c2[0]), min(c1[1], c2[1])
    x1, y1 = x0 + w, y0 + h
    if fillet_r and chamfer:
        raise GeomError("PARAM_CONFLICT", "圆角和倒角不能同时给", "只保留其中一个")
    if fillet_r and fillet_r * 2 > min(w, h) + 1e-9:
        raise GeomError("RADIUS_TOO_LARGE", f"圆角半径 {fillet_r:g} 太大，矩形最短边 {min(w, h):g}", f"圆角半径不能超过 {min(w, h) / 2:g}", max_radius=min(w, h) / 2)
    if chamfer and (chamfer[0] + chamfer[1] > min(w, h) * 2 or chamfer[0] > w or chamfer[1] > h):
        raise GeomError("RADIUS_TOO_LARGE", "倒角距离太大", "倒角距离不能超过边长的一半")
    if fillet_r:
        r = fillet_r
        pts = [((x0 + r, y0), 0.0), ((x1 - r, y0), TAN_QUARTER), ((x1, y0 + r), 0.0), ((x1, y1 - r), TAN_QUARTER),
               ((x1 - r, y1), 0.0), ((x0 + r, y1), TAN_QUARTER), ((x0, y1 - r), 0.0), ((x0, y0 + r), TAN_QUARTER)]
    elif chamfer:
        a, b = chamfer
        pts = [((x0 + a, y0), 0.0), ((x1 - a, y0), 0.0), ((x1, y0 + b), 0.0), ((x1, y1 - b), 0.0), ((x1 - a, y1), 0.0), ((x0 + a, y1), 0.0), ((x0, y1 - b), 0.0), ((x0, y0 + b), 0.0)]
    else:
        pts = [((x0, y0), 0.0), ((x1, y0), 0.0), ((x1, y1), 0.0), ((x0, y1), 0.0)]
    base = (c1[0] if c1[0] <= c2[0] else c2[0], c1[1] if c1[1] <= c2[1] else c2[1])
    verts = []
    for (px, py), b in pts:
        if rotation_deg:
            t = math.radians(rotation_deg)
            dx, dy = px - base[0], py - base[1]
            px, py = base[0] + dx * math.cos(t) - dy * math.sin(t), base[1] + dx * math.sin(t) + dy * math.cos(t)
        verts.append(vtx((px, py, 0.0), b))
    return [ent("polyline", {"vertices": verts, "closed": True, "width": 0.0}, ["rectangle"])]


def polygon_center(n: int, center: Pt, radius: float, inscribed: bool = True, rotation_deg: float = 0.0) -> list[dict]:
    if not 3 <= n <= 1024:
        raise GeomError("INVALID_ARGS", f"边数 {n} 不在 3–1024 之内", "")
    if radius <= EPS:
        raise GeomError("DEGENERATE", "多边形半径必须大于 0", "")
    R = radius if inscribed else radius / math.cos(math.pi / n)
    verts = [vtx(polar(center, R, rotation_deg + 360.0 * i / n)) for i in range(n)]
    return [ent("polyline", {"vertices": verts, "closed": True, "width": 0.0}, ["polygon", f"sides:{n}"])]


def polygon_edge(n: int, p1: Pt, p2: Pt) -> list[dict]:
    if not 3 <= n <= 1024:
        raise GeomError("INVALID_ARGS", f"边数 {n} 不在 3–1024 之内", "")
    s = dist(p1, p2)
    if s < EPS:
        raise GeomError("DEGENERATE", "边的两个端点重合", "")
    R = s / (2 * math.sin(math.pi / n))
    mid = ((p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2)
    ux, uy = (p2[0] - p1[0]) / s, (p2[1] - p1[1]) / s
    apothem = R * math.cos(math.pi / n)
    center = (mid[0] - uy * apothem, mid[1] + ux * apothem, 0.0)
    a0 = ang(center, p1)
    verts = [vtx(polar(center, R, a0 + 360.0 * i / n)) for i in range(n)]
    return [ent("polyline", {"vertices": verts, "closed": True, "width": 0.0}, ["polygon", f"sides:{n}"])]


def grid2d(origin: Pt, rows: int, cols: int, cell: tuple[float, float]) -> list[dict]:
    if rows < 1 or cols < 1 or cell[0] <= EPS or cell[1] <= EPS:
        raise GeomError("INVALID_ARGS", "网格的行数、列数和单元尺寸都必须大于 0", "")
    if (rows + cols + 2) > 400:
        raise GeomError("TOO_MANY", "网格线超过 400 条", "减少行列数")
    x0, y0 = origin[0], origin[1]
    W, H = cols * cell[0], rows * cell[1]
    out = []
    for i in range(rows + 1):
        y = y0 + i * cell[1]
        out.append(ent("line", {"p1": pl((x0, y, 0)), "p2": pl((x0 + W, y, 0))}, ["grid"]))
    for j in range(cols + 1):
        x = x0 + j * cell[0]
        out.append(ent("line", {"p1": pl((x, y0, 0)), "p2": pl((x, y0 + H, 0))}, ["grid"]))
    return out


def mesh_grid(origin: Pt, rows: int, cols: int, cell: tuple[float, float]) -> list[dict]:
    if rows < 1 or cols < 1:
        raise GeomError("INVALID_ARGS", "网格行列数必须 ≥ 1", "")
    verts = [pl((origin[0] + j * cell[0], origin[1] + i * cell[1], origin[2] if len(origin) > 2 else 0.0)) for i in range(rows + 1) for j in range(cols + 1)]
    faces = [[i * (cols + 1) + j, i * (cols + 1) + j + 1, (i + 1) * (cols + 1) + j + 1, (i + 1) * (cols + 1) + j] for i in range(rows) for j in range(cols)]
    return [ent("mesh", {"vertices": verts, "faces": faces}, ["mesh"])]


# ------------------------------------------------------------------ 圆、圆弧、椭圆
def circle(c: Pt, r: float) -> dict:
    if r <= EPS:
        raise GeomError("DEGENERATE", "圆的半径必须大于 0", "给出半径或直径")
    return ent("circle", {"c": pl(c), "r": r})


def circle_2p(p1: Pt, p2: Pt) -> dict:
    return circle(((p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2, 0.0), dist(p1, p2) / 2)


def circle_3p(p1: Pt, p2: Pt, p3: Pt) -> dict:
    r = curves.circle_from_3(p1, p2, p3)
    if r is None:
        raise GeomError("COLLINEAR", "三点共线，不能确定一个圆", "换三个不共线的点")
    return circle(r[0], r[1])


def _offset_curves(prim, r: float) -> list:
    """与原语相切、半径 r 的圆，圆心的轨迹（取无限直线 / 整圆）。"""
    if isinstance(prim, Seg):
        ux, uy = curves.seg_dir(prim)
        nx, ny = -uy * r, ux * r
        return [Seg((prim.a[0] + s * nx, prim.a[1] + s * ny, 0.0), (prim.b[0] + s * nx, prim.b[1] + s * ny, 0.0)) for s in (1, -1)]
    out = [Arc(prim.c, prim.r + r, 0.0, 360.0)]
    if prim.r - r > EPS:
        out.append(Arc(prim.c, prim.r - r, 0.0, 360.0))
    return out


def circle_ttr(a, b, r: float, hint: Pt | None = None) -> dict:
    """相切、相切、半径：a、b 是原语（线/圆/弧）；有多解时取圆心离 hint 最近的。"""
    if r <= EPS:
        raise GeomError("DEGENERATE", "半径必须大于 0", "")
    cands: list[Pt] = []
    for oa in _offset_curves(a, r):
        for ob in _offset_curves(b, r):
            cands += curves.intersect(oa, ob, extend=True)
    if not cands:
        raise GeomError("NO_SOLUTION", "没有与这两个对象都相切且半径为该值的圆", "增大或减小半径，或检查两个对象的相对位置")
    if hint is None:
        ca, cb = curves.midpoint(a), curves.midpoint(b)
        hint = ((ca[0] + cb[0]) / 2, (ca[1] + cb[1]) / 2, 0.0)
    c = min(cands, key=lambda p: dist(p, hint))
    return circle(c, r)


def circle_ttt(a, b, c, hint: Pt | None = None) -> dict:
    """三对象相切：数值求解（牛顿法，遍历内外切符号组合），取圆心离 hint 最近的解。"""
    objs = [a, b, c]
    sols: list[tuple[float, float, float]] = []
    seeds = []
    mids = [curves.midpoint(o) for o in objs]
    cx0, cy0 = sum(m[0] for m in mids) / 3, sum(m[1] for m in mids) / 3
    span = max(dist(mids[i], mids[j]) for i in range(3) for j in range(i + 1, 3)) or 1.0
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            seeds.append((cx0 + dx * span * 0.4, cy0 + dy * span * 0.4, span * 0.25))
    for signs in np.ndindex(2, 2, 2):
        sg = [1 if s else -1 for s in signs]
        for x, y, r in seeds:
            v = np.array([x, y, r], dtype=float)
            ok = False
            for _ in range(60):
                F = np.zeros(3)
                J = np.zeros((3, 3))
                for i, o in enumerate(objs):
                    if isinstance(o, Seg):
                        ux, uy = curves.seg_dir(o)
                        nx, ny = -uy, ux
                        d = (v[0] - o.a[0]) * nx + (v[1] - o.a[1]) * ny
                        F[i] = d - sg[i] * v[2]
                        J[i] = [nx, ny, -sg[i]]
                    else:
                        rr = math.hypot(v[0] - o.c[0], v[1] - o.c[1]) or 1e-12
                        F[i] = rr - (o.r + sg[i] * v[2])
                        J[i] = [(v[0] - o.c[0]) / rr, (v[1] - o.c[1]) / rr, -sg[i]]
                try:
                    step = np.linalg.solve(J, -F)
                except np.linalg.LinAlgError:
                    break
                v = v + step
                if np.linalg.norm(step) < 1e-10:
                    ok = True
                    break
                if abs(v[2]) > 1e7:
                    break
            if ok and v[2] > 1e-6 and not any(abs(v[0] - s[0]) < 1e-5 and abs(v[1] - s[1]) < 1e-5 and abs(v[2] - s[2]) < 1e-5 for s in sols):
                sols.append((float(v[0]), float(v[1]), float(v[2])))
    if not sols:
        raise GeomError("NO_SOLUTION", "没有同时与三个对象相切的圆", "检查三个对象的位置关系")
    if hint is None:
        hint = (cx0, cy0, 0.0)
    x, y, r = min(sols, key=lambda s: math.hypot(s[0] - hint[0], s[1] - hint[1]))
    return circle((x, y, 0.0), r)


def arc_3p(p1: Pt, p2: Pt, p3: Pt) -> dict:
    r = curves.circle_from_3(p1, p2, p3)
    if r is None:
        raise GeomError("COLLINEAR", "三点共线，不能确定圆弧", "")
    c, rad = r
    a1, a2, a3 = ang(c, p1), ang(c, p2), ang(c, p3)
    if ccw_span(a1, a2) < ccw_span(a1, a3):      # 逆时针 p1→p2→p3
        return ent("arc", {"c": pl(c), "r": rad, "a0": a1, "a1": a3})
    return ent("arc", {"c": pl(c), "r": rad, "a0": a3, "a1": a1})


def arc_start_center_end(start: Pt, center: Pt, end: Pt) -> dict:
    r = dist(center, start)
    if r < EPS:
        raise GeomError("DEGENERATE", "圆心与起点重合", "")
    return ent("arc", {"c": pl(center), "r": r, "a0": ang(center, start), "a1": ang(center, end)})


def arc_start_center_angle(start: Pt, center: Pt, included_deg: float) -> dict:
    r = dist(center, start)
    if r < EPS:
        raise GeomError("DEGENERATE", "圆心与起点重合", "")
    a0 = ang(center, start)
    return ent("arc", {"c": pl(center), "r": r, "a0": a0 if included_deg >= 0 else (a0 + included_deg) % 360, "a1": (a0 + included_deg) % 360 if included_deg >= 0 else a0})


def arc_start_end_radius(start: Pt, end: Pt, radius: float) -> dict:
    ch = dist(start, end)
    if ch < EPS:
        raise GeomError("DEGENERATE", "起点和终点重合", "")
    if abs(radius) < ch / 2 - 1e-9:
        raise GeomError("RADIUS_TOO_SMALL", f"半径 {abs(radius):g} 小于弦长的一半 {ch / 2:g}", f"半径至少 {ch / 2:g}", min_radius=ch / 2)
    r = abs(radius)
    h = math.sqrt(max(0.0, r * r - (ch / 2) ** 2))
    mx, my = (start[0] + end[0]) / 2, (start[1] + end[1]) / 2
    ux, uy = (end[0] - start[0]) / ch, (end[1] - start[1]) / ch
    sign = 1 if radius > 0 else -1     # 正半径：圆心在 start→end 的左侧（劣弧，逆时针）
    c = (mx - uy * h * sign, my + ux * h * sign, 0.0)
    return ent("arc", {"c": pl(c), "r": r, "a0": ang(c, start), "a1": ang(c, end)})


def ellipse_center_axes(center: Pt, major_vec: tuple[float, float], ratio: float, t0: float = 0.0, t1: float = 360.0) -> dict:
    if not 0 < ratio <= 1:
        raise GeomError("INVALID_ARGS", "椭圆轴比必须在 (0, 1] 之间", "短轴 / 长轴")
    if math.hypot(*major_vec) < EPS:
        raise GeomError("DEGENERATE", "长轴向量为 0", "")
    return ent("ellipse", {"c": pl(center), "major": [major_vec[0], major_vec[1], 0.0], "ratio": ratio, "t0": t0, "t1": t1})


def ellipse_axis_end(a1: Pt, a2: Pt, other_axis_half: float) -> dict:
    """由一根轴的两个端点和另一根轴的半长定椭圆。"""
    c = ((a1[0] + a2[0]) / 2, (a1[1] + a2[1]) / 2, 0.0)
    half = dist(a1, c)
    if half < EPS or other_axis_half <= EPS:
        raise GeomError("DEGENERATE", "椭圆轴长必须大于 0", "")
    if other_axis_half <= half:
        return ent("ellipse", {"c": pl(c), "major": [a2[0] - c[0], a2[1] - c[1], 0.0], "ratio": other_axis_half / half, "t0": 0.0, "t1": 360.0})
    # 给定的轴是短轴
    major = (-(a2[1] - c[1]) * other_axis_half / half, (a2[0] - c[0]) * other_axis_half / half)
    return ent("ellipse", {"c": pl(c), "major": [major[0], major[1], 0.0], "ratio": half / other_axis_half, "t0": 0.0, "t1": 360.0})


# ------------------------------------------------------------------ 构造线 / 射线
def xline_by_mode(mode: str, p: Pt, p2: Pt | None = None, angle_deg: float | None = None, vertex_pts: tuple[Pt, Pt] | None = None) -> dict:
    if mode == "horizontal":
        d = [1.0, 0.0]
    elif mode == "vertical":
        d = [0.0, 1.0]
    elif mode == "angle":
        if angle_deg is None:
            raise GeomError("MISSING_ARG", "构造线角度模式需要 angle_deg", "")
        d = [math.cos(math.radians(angle_deg)), math.sin(math.radians(angle_deg))]
    elif mode == "bisect":
        if vertex_pts is None:
            raise GeomError("MISSING_ARG", "角平分线需要顶点和两个端点", "")
        a1, a2 = ang(p, vertex_pts[0]), ang(p, vertex_pts[1])
        mid = a1 + ccw_span(a1, a2) / 2 if ccw_span(a1, a2) <= 180 else a2 + ccw_span(a2, a1) / 2
        d = [math.cos(math.radians(mid)), math.sin(math.radians(mid))]
    else:
        if p2 is None:
            raise GeomError("MISSING_ARG", "两点构造线需要第二个点", "")
        L = dist(p, p2)
        if L < EPS:
            raise GeomError("DEGENERATE", "两点重合", "")
        d = [(p2[0] - p[0]) / L, (p2[1] - p[1]) / L]
    return {"p": pl(p), "dir": d}
