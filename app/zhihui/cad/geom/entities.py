"""几何图元 data 的格式与基础运算（纯函数，无 IO、无全局状态）。

单位 mm；角度为十进制度，逆时针为正，0° 指向 +X。点一律是 3 个数 [x, y, z]（z 缺省 0）。

kind      data
point     {p}
line      {p1, p2}
ray       {p, dir}                         dir = [dx, dy]
xline     {p, dir}
mline     {points, style, scale, justification, closed}
polyline  {vertices:[{p, bulge, sw, ew}], closed, width}      矩形/正多边形也是闭合多段线，用 tags 区分
spline    {fit | cv, degree, closed}
circle    {c, r}
arc       {c, r, a0, a1}                   从 a0 逆时针到 a1
ellipse   {c, major, ratio, t0, t1}        major = 长轴向量；t0/t1 为参数角（度），整椭圆 0..360
text      {p, text, height, rotation, style, align, width_factor}
mtext     {p, text, height, width, rotation, line_spacing, attach, bg_mask, style}
dimension {dtype, p1, p2, loc, text, style, measurement, center, radius, angle, ...}
hatch     {pattern, scale, angle, loops:[[pt..]], island_style, associative, source_ids, gradient}
insert    {block, p, scale:[sx,sy,sz], rotation, attribs:{tag: value}}
mesh      {vertices, faces}                solid3d 同 mesh，另有 meta
tolerance {p, text}
leader    {points, text}

求交、修剪、倒角、边界追踪都建立在“原语”之上：Seg（线段）和 Arc（圆弧，整圆是跨度 360 的弧）。
椭圆和样条取折线近似（approx=True），只用于求交和边界，不用于精确倒角。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

EPS = 1e-9
Pt = tuple[float, float, float]


def P(v) -> Pt:
    """任何 2/3 元序列 → 3 个 float。"""
    if isinstance(v, dict):
        v = v.get("p", v)
    x, y = float(v[0]), float(v[1])
    return (x, y, float(v[2]) if len(v) > 2 else 0.0)


def pl(p) -> list[float]:
    return [round(float(p[0]), 9), round(float(p[1]), 9), round(float(p[2]) if len(p) > 2 else 0.0, 9)]


def dist(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def ang(a, b) -> float:
    """a→b 的方向角（度，0..360）。"""
    return math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) % 360


def polar(c, r: float, deg: float) -> Pt:
    t = math.radians(deg)
    return (c[0] + r * math.cos(t), c[1] + r * math.sin(t), c[2] if len(c) > 2 else 0.0)


def norm_deg(a: float) -> float:
    return a % 360


def ccw_span(a0: float, a1: float) -> float:
    s = (a1 - a0) % 360
    return 360.0 if s < EPS and abs(a1 - a0) > EPS else s


def ang_in_arc(a: float, a0: float, a1: float, tol: float = 1e-7) -> bool:
    span = ccw_span(a0, a1)
    if span >= 360 - tol:
        return True
    d = (a - a0) % 360
    return d <= span + tol or d >= 360 - tol


# ------------------------------------------------------------------ 原语
@dataclass(frozen=True)
class Seg:
    a: Pt
    b: Pt
    approx: bool = False

    @property
    def length(self) -> float:
        return dist(self.a, self.b)


@dataclass(frozen=True)
class Arc:
    c: Pt
    r: float
    a0: float
    a1: float          # 逆时针 a0→a1；整圆 a1 = a0 + 360

    @property
    def span(self) -> float:
        return ccw_span(self.a0, self.a1) if self.a1 - self.a0 < 360 - EPS else 360.0

    @property
    def length(self) -> float:
        return math.radians(self.span) * self.r

    def start(self) -> Pt:
        return polar(self.c, self.r, self.a0)

    def end(self) -> Pt:
        return polar(self.c, self.r, self.a0 + self.span)

    @property
    def full(self) -> bool:
        return self.span >= 360 - 1e-7


def bulge_arc(p1, p2, bulge: float) -> Arc:
    """多段线 bulge（= tan(θ/4)，θ 为圆心角，正为逆时针）→ 圆弧。"""
    chord = dist(p1, p2)
    theta = 4 * math.atan(bulge)
    r = chord / (2 * math.sin(abs(theta) / 2))
    mx, my = (p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2
    h = r * math.cos(abs(theta) / 2) * (1 if abs(theta) < math.pi else -1)
    dx, dy = (p2[0] - p1[0]) / chord, (p2[1] - p1[1]) / chord
    sign = 1 if bulge > 0 else -1
    cx, cy = mx - sign * dy * h, my + sign * dx * h
    a_start, a_end = ang((cx, cy, 0), p1), ang((cx, cy, 0), p2)
    if bulge > 0:
        return Arc((cx, cy, p1[2]), r, a_start, a_start + ccw_span(a_start, a_end))
    return Arc((cx, cy, p1[2]), r, a_end, a_end + ccw_span(a_end, a_start))


def arc_bulge(arc: Arc, from_start: bool = True) -> float:
    return math.tan(math.radians(arc.span) / 4) * (1 if from_start else -1)


def _sample_ellipse(d: dict, n: int = 96) -> list[Pt]:
    c, major, ratio = P(d["c"]), P(d["major"]), float(d.get("ratio", 1))
    a = math.hypot(major[0], major[1])
    rot = math.atan2(major[1], major[0])
    t0, t1 = float(d.get("t0", 0)), float(d.get("t1", 360))
    span = ccw_span(t0, t1) if abs(t1 - t0) < 360 - EPS else 360.0
    m = max(8, int(n * span / 360))
    pts = []
    for i in range(m + 1):
        t = math.radians(t0 + span * i / m)
        x, y = a * math.cos(t), a * ratio * math.sin(t)
        pts.append((c[0] + x * math.cos(rot) - y * math.sin(rot), c[1] + x * math.sin(rot) + y * math.cos(rot), c[2]))
    return pts


def _bspline_points(pts: list[Pt], degree: int, closed: bool, n: int = 16) -> list[Pt]:
    """样条近似：对控制/拟合点做 Catmull-Rom 采样（只用于求交和预览，不追求与 AutoCAD 完全一致）。"""
    if len(pts) < 3:
        return list(pts)
    q = list(pts)
    if closed:
        q = [q[-1], *q, q[0], q[1]]
    else:
        q = [q[0], *q, q[-1]]
    out: list[Pt] = []
    for i in range(1, len(q) - 2):
        p0, p1, p2, p3 = q[i - 1], q[i], q[i + 1], q[i + 2]
        for k in range(n):
            t = k / n
            t2, t3 = t * t, t * t * t
            out.append(tuple(0.5 * ((2 * p1[j]) + (-p0[j] + p2[j]) * t + (2 * p0[j] - 5 * p1[j] + 4 * p2[j] - p3[j]) * t2 + (-p0[j] + 3 * p1[j] - 3 * p2[j] + p3[j]) * t3) for j in range(3)))
    out.append(q[-2])
    return out


def polyline_vertices(d: dict) -> list[tuple[Pt, float]]:
    return [(P(v["p"]), float(v.get("bulge", 0) or 0)) for v in d["vertices"]]


def primitives(e) -> list[Seg | Arc]:
    """图元 → 原语列表（有向，沿图元走向）。不能求交的类型（文字、块……）返回空列表。"""
    k, d = e.kind, e.data
    if k == "line":
        return [Seg(P(d["p1"]), P(d["p2"]))]
    if k == "circle":
        return [Arc(P(d["c"]), float(d["r"]), 0.0, 360.0)]
    if k == "arc":
        a0, a1 = float(d["a0"]), float(d["a1"])
        return [Arc(P(d["c"]), float(d["r"]), a0 % 360, a0 % 360 + ccw_span(a0, a1))]
    if k == "polyline":
        vs = polyline_vertices(d)
        out: list[Seg | Arc] = []
        n = len(vs)
        rng = n if d.get("closed") else n - 1
        for i in range(rng):
            p1, b = vs[i]
            p2 = vs[(i + 1) % n][0]
            if dist(p1, p2) < EPS:
                continue
            out.append(bulge_arc(p1, p2, b) if abs(b) > 1e-12 else Seg(p1, p2))
        return out
    if k == "ellipse":
        pts = _sample_ellipse(d)
        return [Seg(pts[i], pts[i + 1], True) for i in range(len(pts) - 1)]
    if k == "spline":
        src = [P(p) for p in (d.get("fit") or d.get("cv") or [])]
        pts = _bspline_points(src, int(d.get("degree", 3)), bool(d.get("closed")))
        return [Seg(pts[i], pts[i + 1], True) for i in range(len(pts) - 1) if dist(pts[i], pts[i + 1]) > EPS]
    if k == "mline":
        pts = [P(p) for p in d["points"]]
        segs = [Seg(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
        if d.get("closed") and len(pts) > 2:
            segs.append(Seg(pts[-1], pts[0]))
        return segs
    if k in ("ray", "xline"):
        p, dr = P(d["p"]), d["dir"]
        L = 1e6
        n = math.hypot(dr[0], dr[1]) or 1.0
        ux, uy = dr[0] / n, dr[1] / n
        a = (p[0] - ux * L, p[1] - uy * L, p[2]) if k == "xline" else p
        return [Seg(a, (p[0] + ux * L, p[1] + uy * L, p[2]))]
    return []


def is_closed(e) -> bool:
    if e.kind == "circle" or (e.kind == "ellipse" and abs(float(e.data.get("t1", 360)) - float(e.data.get("t0", 0))) >= 360 - EPS):
        return True
    if e.kind in ("polyline", "spline", "mline"):
        return bool(e.data.get("closed"))
    return False


def sample(prim: Seg | Arc, tol: float = 0.05) -> list[Pt]:
    """原语 → 折线采样（弧按弦高误差 tol）。"""
    if isinstance(prim, Seg):
        return [prim.a, prim.b]
    r = max(prim.r, EPS)
    step = 2 * math.degrees(math.acos(max(0.0, min(1.0, 1 - tol / r)))) if tol < r else 90.0
    step = max(1.0, min(step, 30.0))
    n = max(2, int(math.ceil(prim.span / step)))
    return [polar(prim.c, prim.r, prim.a0 + prim.span * i / n) for i in range(n + 1)]


def entity_points(e, tol: float = 0.05) -> list[Pt]:
    """图元的一串代表点（用于包围盒、窗选、相近判断）。"""
    k, d = e.kind, e.data
    if k == "point":
        return [P(d["p"])]
    if k in ("text", "mtext", "tolerance", "insert"):
        return [P(d["p"])]
    if k == "leader":
        return [P(p) for p in d["points"]]
    if k == "dimension":
        return [P(d[key]) for key in ("p1", "p2", "loc", "center") if d.get(key) is not None]
    if k == "hatch":
        return [P(p) for loop in d.get("loops", []) for p in loop]
    if k in ("mesh", "solid3d"):
        return [P(p) for p in d.get("vertices", [])]
    pts: list[Pt] = []
    for pr in primitives(e):
        pts += sample(pr, tol)
    return pts


def entity_bbox(e) -> tuple[float, float, float, float] | None:
    if e.kind in ("ray", "xline"):
        return None
    if e.kind == "insert":
        from .blocks import insert_bbox
        return insert_bbox(e.data)
    pts = entity_points(e)
    if e.kind == "text" and pts:
        h = float(e.data.get("height", 3.5))
        w = h * 0.7 * max(1, len(str(e.data.get("text", ""))))
        x, y, _ = pts[0]
        return (x, y, x + w, y + h)
    if e.kind == "mtext" and pts:
        h = float(e.data.get("height", 3.5))
        lines = str(e.data.get("text", "")).split("\n")
        w = float(e.data.get("width") or h * 0.7 * max(len(l) for l in lines))
        x, y, _ = pts[0]
        return (x, y - h * len(lines), x + w, y)
    if not pts:
        return None
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    if e.kind in ("circle", "arc", "polyline"):          # 圆、弧（含多段线里的弧段）用精确范围：端点加上落在弧内的象限点，而不是折线采样的近似值
        for pr in primitives(e):
            if isinstance(pr, Arc):
                for q in (0, 90, 180, 270):
                    if pr.full or ang_in_arc(q, pr.a0, pr.a0 + pr.span):
                        pq = polar(pr.c, pr.r, q)
                        xs.append(pq[0])
                        ys.append(pq[1])
    return (min(xs), min(ys), max(xs), max(ys))


def entity_length(e) -> float:
    return sum(p.length for p in primitives(e))


def polygon_area(pts: list[Pt]) -> float:
    return 0.5 * sum(pts[i][0] * pts[(i + 1) % len(pts)][1] - pts[(i + 1) % len(pts)][0] * pts[i][1] for i in range(len(pts)))


def entity_area(e) -> float | None:
    if e.kind == "circle":
        return math.pi * float(e.data["r"]) ** 2
    if e.kind == "ellipse" and is_closed(e):
        return math.pi * math.hypot(*e.data["major"][:2]) ** 2 * float(e.data.get("ratio", 1))
    if e.kind in ("polyline", "spline") and is_closed(e):
        pts: list[Pt] = []
        for pr in primitives(e):
            pts += sample(pr, 1e-5)[:-1]
        return abs(polygon_area(pts)) if len(pts) > 2 else None
    return None


def centroid(pts: list[Pt]) -> Pt:
    a = polygon_area(pts)
    if abs(a) < EPS:
        return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts), 0.0)
    cx = cy = 0.0
    for i in range(len(pts)):
        x0, y0, _ = pts[i]
        x1, y1, _ = pts[(i + 1) % len(pts)]
        f = x0 * y1 - x1 * y0
        cx += (x0 + x1) * f
        cy += (y0 + y1) * f
    return (cx / (6 * a), cy / (6 * a), 0.0)


# ------------------------------------------------------------------ 变换
@dataclass(frozen=True)
class Transform:
    """2D 仿射 + z 平移/缩放： (x,y) → (a x + b y + tx, c x + d y + ty)，z → z*sz + tz"""
    a: float = 1.0
    b: float = 0.0
    c: float = 0.0
    d: float = 1.0
    tx: float = 0.0
    ty: float = 0.0
    sz: float = 1.0
    tz: float = 0.0

    def __call__(self, p) -> Pt:
        x, y = p[0], p[1]
        z = p[2] if len(p) > 2 else 0.0
        return (self.a * x + self.b * y + self.tx, self.c * x + self.d * y + self.ty, z * self.sz + self.tz)

    def vec(self, v) -> tuple[float, float]:
        return (self.a * v[0] + self.b * v[1], self.c * v[0] + self.d * v[1])

    @property
    def det(self) -> float:
        return self.a * self.d - self.b * self.c

    @property
    def mirrored(self) -> bool:
        return self.det < 0

    @property
    def scale(self) -> float:
        return math.sqrt(abs(self.det))

    @property
    def rot_deg(self) -> float:
        return math.degrees(math.atan2(self.c, self.a)) if not self.mirrored else math.degrees(math.atan2(self.c, self.a))

    @staticmethod
    def translation(dx: float, dy: float, dz: float = 0.0) -> "Transform":
        return Transform(tx=dx, ty=dy, tz=dz)

    @staticmethod
    def rotation(base, deg: float) -> "Transform":
        t = math.radians(deg)
        co, si = math.cos(t), math.sin(t)
        return Transform(co, -si, si, co, base[0] - (co * base[0] - si * base[1]), base[1] - (si * base[0] + co * base[1]))

    @staticmethod
    def scaling(base, s: float) -> "Transform":
        return Transform(s, 0, 0, s, base[0] * (1 - s), base[1] * (1 - s), sz=s)

    @staticmethod
    def mirror(p1, p2) -> "Transform":
        t = math.radians(ang(p1, p2))
        co, si = math.cos(2 * t), math.sin(2 * t)
        # 关于过 p1、方向 t 的直线反射
        a, b, c, d = co, si, si, -co
        return Transform(a, b, c, d, p1[0] - (a * p1[0] + b * p1[1]), p1[1] - (c * p1[0] + d * p1[1]))

    def then(self, other: "Transform") -> "Transform":
        """先 self 后 other。"""
        return Transform(other.a * self.a + other.b * self.c, other.a * self.b + other.b * self.d, other.c * self.a + other.d * self.c, other.c * self.b + other.d * self.d,
                         other.a * self.tx + other.b * self.ty + other.tx, other.c * self.tx + other.d * self.ty + other.ty, self.sz * other.sz, self.tz * other.sz + other.tz)


_POINT_KEYS = {"point": ("p",), "line": ("p1", "p2"), "text": ("p",), "mtext": ("p",), "insert": ("p",), "tolerance": ("p",),
               "dimension": ("p1", "p2", "loc", "center", "p3", "text_pos")}
_POINT_LISTS = {"mline": "points", "leader": "points", "mesh": "vertices", "solid3d": "vertices", "spline": None}


def transform_data(kind: str, data: dict, T: Transform) -> dict:
    """返回变换后的新 data（不改原对象）。"""
    import copy

    d = copy.deepcopy(data)
    if kind in _POINT_KEYS:
        for key in _POINT_KEYS[kind]:
            if d.get(key) is not None:
                d[key] = pl(T(P(d[key])))
    if kind in ("ray", "xline"):
        d["p"] = pl(T(P(d["p"])))
        v = T.vec(d["dir"])
        d["dir"] = [v[0], v[1]]
    elif kind == "circle":
        d["c"] = pl(T(P(d["c"])))
        d["r"] = float(d["r"]) * T.scale
    elif kind == "arc":
        c = P(d["c"])
        a0, a1 = float(d["a0"]), float(d["a1"])
        n0, n1 = ang((0, 0, 0), T.vec(polar((0, 0, 0), 1, a0))), ang((0, 0, 0), T.vec(polar((0, 0, 0), 1, a1)))
        d["c"], d["r"] = pl(T(c)), float(d["r"]) * T.scale
        d["a0"], d["a1"] = (n1, n0) if T.mirrored else (n0, n1)
    elif kind == "ellipse":
        d["c"] = pl(T(P(d["c"])))
        mv = T.vec(d["major"])
        d["major"] = [mv[0], mv[1], 0.0]
        if T.mirrored:
            t0, t1 = float(d.get("t0", 0)), float(d.get("t1", 360))
            d["t0"], d["t1"] = (-t1) % 360, (-t0) % 360
            if abs(t1 - t0) >= 360 - EPS:
                d["t0"], d["t1"] = 0, 360
    elif kind == "polyline":
        for v in d["vertices"]:
            v["p"] = pl(T(P(v["p"])))
            if T.mirrored:
                v["bulge"] = -float(v.get("bulge", 0) or 0)
            v["sw"] = float(v.get("sw", 0) or 0) * T.scale
            v["ew"] = float(v.get("ew", 0) or 0) * T.scale
        d["width"] = float(d.get("width", 0) or 0) * T.scale
    elif kind == "spline":
        for key in ("fit", "cv"):
            if d.get(key):
                d[key] = [pl(T(P(p))) for p in d[key]]
    elif kind in ("mline", "leader", "mesh", "solid3d"):
        key = _POINT_LISTS[kind]
        d[key] = [pl(T(P(p))) for p in d[key]]
        if kind in ("mesh", "solid3d"):
            if T.mirrored != (T.sz < 0):
                d["faces"] = [list(reversed(f)) for f in d.get("faces", [])]      # 镜像后保持外向法线
            if kind == "solid3d":
                d["volume"] = float(d.get("volume", 0)) * abs(T.det) * abs(T.sz)
                if d.get("slabs"):
                    if abs(T.sz) < EPS or T.sz < 0 or abs(T.det) < EPS:
                        d.pop("slabs")                         # 翻转 z 或退化的变换不再保持 z 向棱柱结构
                        d["solid_kind"] = "mesh"
                    else:
                        d["slabs"] = [{"z0": s["z0"] * T.sz + T.tz, "z1": s["z1"] * T.sz + T.tz,
                                       "polys": [{"outer": [[*T(P([x, y, 0]))[:2]] for x, y in p["outer"]], "holes": [[[*T(P([x, y, 0]))[:2]] for x, y in h] for h in p["holes"]]} for p in s["polys"]]}
                                      for s in d["slabs"]]
    elif kind == "hatch":
        d["loops"] = [[pl(T(P(p))) for p in loop] for loop in d.get("loops", [])]
    if kind in ("text", "mtext"):
        d["rotation"] = (float(d.get("rotation", 0)) + T.rot_deg) % 360
        if abs(T.scale - 1) > EPS:
            d["height"] = float(d.get("height", 3.5)) * T.scale
    if kind == "insert":
        d["rotation"] = (float(d.get("rotation", 0)) + T.rot_deg) % 360
        sc = d.get("scale", [1, 1, 1])
        k = T.scale
        d["scale"] = [sc[0] * k * (-1 if T.mirrored else 1), sc[1] * k, sc[2] * T.sz]
    return d


def translate(e, dx: float, dy: float, dz: float = 0.0) -> dict:
    return transform_data(e.kind, e.data, Transform.translation(dx, dy, dz))


def first_point(e) -> Pt | None:
    pts = entity_points(e)
    return pts[0] if pts else None
