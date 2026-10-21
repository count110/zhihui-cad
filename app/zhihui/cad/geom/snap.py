"""对象捕捉：图元的特征点、交点、垂足、切点、延伸点，以及用方位词/参考点消歧。"""
from __future__ import annotations

import math

from . import curves
from .entities import Arc, Pt, Seg, centroid, dist, entity_bbox, entity_points, is_closed, polar, primitives, sample

SNAP_TYPES = ("endpoint", "midpoint", "center", "geometric_center", "node", "quadrant", "intersection", "apparent_intersection",
              "perpendicular", "tangent", "nearest", "insertion", "extension", "m2p", "parallel", "otrack_intersection")
# 方位词 → 向量分量
HINT_VEC = {"left": (-1, 0), "right": (1, 0), "top": (0, 1), "bottom": (0, -1), "up": (0, 1), "down": (0, -1),
            "top-left": (-1, 1), "top-right": (1, 1), "bottom-left": (-1, -1), "bottom-right": (1, -1), "upper-left": (-1, 1),
            "upper-right": (1, 1), "lower-left": (-1, -1), "lower-right": (1, -1)}


def _add(out: list[dict], typ: str, pt: Pt, eid: str, **extra) -> None:
    if not any(o["type"] == typ and dist(o["coord"], pt) < 1e-6 for o in out):
        out.append({"type": typ, "coord": [round(pt[0], 9), round(pt[1], 9), round(pt[2], 9)], "entity_id": eid, **extra})


def entity_snap_points(e, types: list[str] | None = None, reference_point=None) -> list[dict]:
    """单个图元自己的捕捉点（交点除外，见 intersections）。"""
    types = list(types or ["endpoint", "midpoint", "center", "geometric_center", "node", "quadrant", "insertion"])
    out: list[dict] = []
    prims = primitives(e)
    k, d = e.kind, e.data
    if "endpoint" in types:
        if k == "polyline":
            for v in d["vertices"]:
                _add(out, "endpoint", tuple(v["p"]) + (0.0,) * (3 - len(v["p"])), e.id)
        elif k in ("line", "arc", "mline", "spline", "ellipse") or k == "ray":
            for pr in prims[:1] + prims[-1:]:
                a, b = curves.endpoints(pr)
                if k in ("arc", "ellipse") or pr is prims[0]:
                    _add(out, "endpoint", a, e.id)
                if k in ("arc", "ellipse") or pr is prims[-1]:
                    _add(out, "endpoint", b, e.id)
            if k == "mline":
                for pr in prims:
                    _add(out, "endpoint", pr.a, e.id)
                    _add(out, "endpoint", pr.b, e.id)
    if "midpoint" in types:
        for pr in prims:
            if k == "circle" or (k == "ellipse" and is_closed(e)):
                continue
            if not getattr(pr, "approx", False) or k == "line":
                _add(out, "midpoint", curves.midpoint(pr), e.id)
    if "center" in types:
        if k in ("circle", "arc"):
            _add(out, "center", tuple(d["c"]) + (0.0,) * (3 - len(d["c"])), e.id)
        elif k == "ellipse":
            _add(out, "center", tuple(d["c"]) + (0.0,) * (3 - len(d["c"])), e.id)
    if "geometric_center" in types:
        gc = geometric_center(e)
        if gc:
            _add(out, "geometric_center", gc, e.id)
    if "node" in types and k == "point":
        _add(out, "node", tuple(d["p"]) + (0.0,) * (3 - len(d["p"])), e.id)
    if "quadrant" in types and k in ("circle", "arc"):
        for pr in prims:
            for q in (0, 90, 180, 270):
                if curves.ang_in_arc(q, pr.a0, pr.a0 + pr.span):
                    _add(out, "quadrant", polar(pr.c, pr.r, q), e.id, angle=q)
    elif "quadrant" in types and k == "ellipse":
        for q in _ellipse_quads(d):
            _add(out, "quadrant", q, e.id)
    if "insertion" in types and k in ("text", "mtext", "insert", "tolerance"):
        _add(out, "insertion", tuple(d["p"]) + (0.0,) * (3 - len(d["p"])), e.id)
    if reference_point is not None:
        ref = tuple(reference_point) + (0.0,) * (3 - len(reference_point))
        if "perpendicular" in types:
            for pr in prims:
                for f in curves.perpendicular_feet(pr, ref):
                    _add(out, "perpendicular", f, e.id)
        if "tangent" in types:
            for pr in prims:
                for t in curves.tangent_points(pr, ref):
                    _add(out, "tangent", t, e.id)
        if "nearest" in types and prims:
            best = min((curves.closest(pr, ref) for pr in prims), key=lambda p: dist(p, ref))
            _add(out, "nearest", best, e.id)
    return out


def _ellipse_quads(d: dict) -> list[Pt]:
    """椭圆参数角 0/90/180/270 度的点（整椭圆的象限点）。"""
    c = tuple(d["c"]) + (0.0,) * (3 - len(d["c"]))
    mx, my = d["major"][0], d["major"][1]
    nx, ny = -my * float(d.get("ratio", 1)), mx * float(d.get("ratio", 1))
    return [(c[0] + sx * mx + sy * nx, c[1] + sx * my + sy * ny, 0.0) for sx, sy in ((1, 0), (0, 1), (-1, 0), (0, -1))]


def geometric_center(e) -> Pt | None:
    k, d = e.kind, e.data
    if k in ("circle", "arc", "ellipse"):
        return tuple(d["c"]) + (0.0,) * (3 - len(d["c"]))
    if k == "line":
        return curves.midpoint(primitives(e)[0])
    if is_closed(e) and k in ("polyline", "spline", "mline"):
        pts: list[Pt] = []
        for pr in primitives(e):
            pts += sample(pr)[:-1]
        return centroid(pts) if len(pts) > 2 else None
    if k == "hatch" and d.get("loops"):
        return centroid([tuple(p) + (0.0,) * (3 - len(p)) for p in d["loops"][0]])
    b = entity_bbox(e)
    if b:
        return ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2, 0.0)
    return None


def intersections(entities: list, apparent: bool = False) -> list[dict]:
    """给定图元两两求交（apparent=True 为延伸交点）。"""
    out: list[dict] = []
    for i in range(len(entities)):
        for j in range(i + 1, len(entities)):
            for p in primitives(entities[i]):
                for q in primitives(entities[j]):
                    for pt in curves.intersect(p, q, extend=apparent):
                        _add(out, "apparent_intersection" if apparent else "intersection", pt, f"{entities[i].id},{entities[j].id}")
    return out


def extension_point(e, distance: float, end: str = "end") -> Pt | None:
    prims = primitives(e)
    if not prims:
        return None
    pr = prims[-1] if end == "end" else prims[0]
    L = pr.length
    return curves.at_length(pr, L + distance) if end == "end" else curves.at_length(pr, -distance)


def hint_score(pt, hint: str, box: tuple[float, float, float, float] | None) -> float:
    vx, vy = HINT_VEC.get(hint, (0, 0))
    w = (box[2] - box[0]) or 1.0 if box else 1.0
    h = (box[3] - box[1]) or 1.0 if box else 1.0
    return vx * pt[0] / w + vy * pt[1] / h


def choose(points: list[dict], reference_point=None, hint: str | None = None, region_box=None) -> tuple[dict | None, list[dict]]:
    """从候选里选唯一点：有方位词按方位，有参考点取最近，否则只有一个才算唯一。返回 (选中点或 None, 候选列表)。"""
    if not points:
        return None, []
    if len(points) == 1:
        return points[0], points
    if hint and hint != "nearest" and hint in HINT_VEC:
        best = max(points, key=lambda p: hint_score(p["coord"], hint, region_box))
        # 若并列（方位无法区分）则视为歧义
        ties = [p for p in points if abs(hint_score(p["coord"], hint, region_box) - hint_score(best["coord"], hint, region_box)) < 1e-9]
        return (best if len(ties) == 1 else None), points
    ref = reference_point if reference_point is not None else None
    if ref is not None or hint == "nearest":
        r = ref if ref is not None else (0.0, 0.0, 0.0)
        ranked = sorted(points, key=lambda p: dist(p["coord"], r))
        if len(ranked) > 1 and abs(dist(ranked[0]["coord"], r) - dist(ranked[1]["coord"], r)) < 1e-9:
            return None, points
        return ranked[0], points
    return None, points


def otrack_intersections(e1, e2) -> list[dict]:
    """对象追踪交点：先“捕获”两个图元的特征点（端点、中点、圆心、象限点、节点），再取一个点的水平追踪线与另一个点的竖直追踪线的交点。
    只做水平/竖直两个方向（正交追踪）；其它极轴角度的追踪线没有实现。"""
    types = ["endpoint", "midpoint", "center", "quadrant", "node"]
    pa = [c["coord"] for c in entity_snap_points(e1, types)]
    pb = [c["coord"] for c in entity_snap_points(e2, types)]
    out: list[dict] = []
    for a in pa:
        for b in pb:
            for x, y in ((b[0], a[1]), (a[0], b[1])):
                _add(out, "otrack_intersection", (x, y, 0.0), f"{e1.id},{e2.id}")
    return out
