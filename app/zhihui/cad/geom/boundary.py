"""闭合边界追踪：给定一点（拾取内部点）或一组图元，求外轮廓、孤岛、面积；不闭合时给出缺口位置。"""
from __future__ import annotations

import math

from shapely.geometry import LineString, Point, Polygon
from shapely.ops import polygonize_full, unary_union

from .entities import Pt, dist, entity_bbox, primitives, sample

CURVE_KINDS = ("line", "polyline", "circle", "arc", "ellipse", "spline", "mline")


def _lines(entities, tol: float):
    q = max(tol, 1e-9)
    out, owner = [], []
    for e in entities:
        for pr in primitives(e):
            pts = [(round(p[0] / q) * q, round(p[1] / q) * q) for p in sample(pr, tol=max(0.005, tol))]
            pts = [p for i, p in enumerate(pts) if i == 0 or p != pts[i - 1]]
            if len(pts) >= 2:
                out.append(LineString(pts))
                owner.append(e.id)
    return out, owner


def _ring_ids(ring: LineString, lines, owner, tol: float) -> list[str]:
    ids: list[str] = []
    for ln, oid in zip(lines, owner):
        if oid not in ids and ring.distance(ln.interpolate(0.5, normalized=True)) <= max(tol * 3, 1e-6) and ring.distance(ln.interpolate(0.25, normalized=True)) <= max(tol * 3, 1e-6):
            ids.append(oid)
    return ids


def _pts(coords) -> list[list[float]]:
    return [[round(x, 9), round(y, 9), 0.0] for x, y in list(coords)[:-1]]


def find_closed_boundary(entities: list, seed_point=None, candidate_ids: list[str] | None = None, tolerance: float | None = None) -> dict:
    pool = [e for e in entities if e.kind in CURVE_KINDS and (candidate_ids is None or e.id in candidate_ids)]
    if candidate_ids is not None:
        missing = [i for i in candidate_ids if i not in {e.id for e in entities}]
        if missing:
            return {"is_closed": False, "reason": "ENTITY_NOT_FOUND", "message": "找不到图元：" + "、".join(missing), "outer_loop_entity_ids": [], "outer_loop_points": [], "island_loops": [], "enclosed_area": 0.0, "gaps": []}
    boxes = [b for b in (entity_bbox(e) for e in pool) if b]
    diag = math.hypot(max(b[2] for b in boxes) - min(b[0] for b in boxes), max(b[3] for b in boxes) - min(b[1] for b in boxes)) if boxes else 1.0
    tol = tolerance if tolerance is not None else 1e-3 * max(diag, 1.0)
    lines, owner = _lines(pool, tol)
    if not lines:
        return {"is_closed": False, "outer_loop_entity_ids": [], "outer_loop_points": [], "island_loops": [], "enclosed_area": 0.0, "gaps": [], "message": "没有可追踪的线条"}
    noded = unary_union(lines)
    polys, cuts, dangles, invalid = polygonize_full(noded if noded.geom_type != "LineString" else [noded])
    faces = [g for g in polys.geoms if g.area > tol * tol]
    gaps = _gaps([d for d in dangles.geoms], tol)
    if not faces:
        return {"is_closed": False, "outer_loop_entity_ids": [], "outer_loop_points": [], "island_loops": [], "enclosed_area": 0.0, "gaps": gaps,
                "message": "轮廓不闭合" + (f"，最近的缺口约 {gaps[0]['size']:.3g} mm，位于 {gaps[0]['at'][:2]}" if gaps else "")}
    face = None
    if seed_point is not None:
        sp = Point(seed_point[0], seed_point[1])
        inside = [f for f in faces if f.contains(sp)]
        if inside:
            face = min(inside, key=lambda f: f.area)
        else:
            return {"is_closed": False, "outer_loop_entity_ids": [], "outer_loop_points": [], "island_loops": [], "enclosed_area": 0.0, "gaps": gaps,
                    "message": "拾取点不在任何闭合区域内" + (f"；附近有缺口约 {gaps[0]['size']:.3g} mm，位于 {gaps[0]['at'][:2]}" if gaps else "")}
    else:
        # 一组图元：面积最大的面是外轮廓（带孔的面连同孔一起）
        face = max(faces, key=lambda f: Polygon(f.exterior).area)
    outer_ring = LineString(face.exterior.coords)
    islands = []
    for ring in face.interiors:
        pr = LineString(ring.coords)
        islands.append({"points": _pts(ring.coords), "entity_ids": _ring_ids(pr, lines, owner, tol), "area": Polygon(ring).area})
    return {"is_closed": True, "outer_loop_entity_ids": _ring_ids(outer_ring, lines, owner, tol), "outer_loop_points": _pts(face.exterior.coords), "island_loops": islands,
            "enclosed_area": round(face.area, 9), "gaps": gaps}


def _gaps(dangles: list, tol: float) -> list[dict]:
    ends: list[Pt] = []
    for d in dangles:
        c = list(d.coords)
        ends += [(c[0][0], c[0][1], 0.0), (c[-1][0], c[-1][1], 0.0)]
    # 只关心悬空端点之间最近的一对（视为“差一点没接上”）
    out = []
    for i in range(len(ends)):
        for j in range(i + 1, len(ends)):
            s = dist(ends[i], ends[j])
            if s > tol * 0.5:
                out.append({"at": [round((ends[i][0] + ends[j][0]) / 2, 6), round((ends[i][1] + ends[j][1]) / 2, 6), 0.0], "size": round(s, 6)})
    out.sort(key=lambda g: g["size"])
    return out[:5]
