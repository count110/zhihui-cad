"""选择与查询：按类型/图层/属性/尺寸/方位/关系/窗口筛选图元，并给出排序、得分和理由。"""
from __future__ import annotations

import math

from . import curves
from .entities import (Arc, Seg, ang, centroid, dist, entity_area, entity_bbox, entity_length, entity_points, is_closed, polygon_area,
                       primitives, sample)

TYPE_ALIASES = {"rectangle": ("polyline", "rectangle"), "polygon": ("polyline", "polygon"), "矩形": ("polyline", "rectangle"), "多边形": ("polyline", "polygon")}
REGIONS = ("all", "top", "bottom", "left", "right", "center", "top_left", "top_right", "bottom_left", "bottom_right")


def key_params(e) -> dict:
    k, d = e.kind, e.data
    out: dict = {}
    if k == "circle":
        out = {"center": d["c"], "radius": d["r"], "diameter": 2 * d["r"], "area": entity_area(e)}
    elif k == "arc":
        out = {"center": d["c"], "radius": d["r"], "start_angle": d["a0"], "end_angle": d["a1"], "length": entity_length(e)}
    elif k == "line":
        out = {"p1": d["p1"], "p2": d["p2"], "length": entity_length(e), "angle": ang(d["p1"], d["p2"])}
    elif k == "polyline":
        out = {"vertices": len(d["vertices"]), "closed": bool(d.get("closed")), "length": entity_length(e)}
        if d.get("closed"):
            out["area"] = entity_area(e)
        b = entity_bbox(e)
        if b:
            out["width"], out["height"] = b[2] - b[0], b[3] - b[1]
    elif k in ("text", "mtext"):
        out = {"text": d.get("text", ""), "height": d.get("height"), "at": d["p"]}
    elif k == "ellipse":
        out = {"center": d["c"], "ratio": d.get("ratio"), "major_length": math.hypot(*d["major"][:2])}
    elif k == "insert":
        out = {"block": d.get("block"), "at": d["p"]}
    elif k == "hatch":
        out = {"pattern": d.get("pattern"), "loops": len(d.get("loops", []))}
    elif k == "dimension":
        out = {"dtype": d.get("dtype"), "measurement": d.get("measurement"), "text": d.get("text")}
    elif k == "point":
        out = {"p": d["p"]}
    out = {a: (round(b, 6) if isinstance(b, float) else b) for a, b in out.items() if b is not None}
    return out


def summary(e) -> str:
    """一句中文摘要，写进 recent_entities。"""
    k, d = e.kind, e.data
    f = lambda v: f"{v:g}"
    if k == "circle":
        return f"圆心({f(d['c'][0])},{f(d['c'][1])}) ⌀{f(2 * d['r'])}"
    if k == "arc":
        return f"圆弧 圆心({f(d['c'][0])},{f(d['c'][1])}) R{f(d['r'])} {f(d['a0'])}°→{f(d['a1'])}°"
    if k == "line":
        return f"直线 ({f(d['p1'][0])},{f(d['p1'][1])})→({f(d['p2'][0])},{f(d['p2'][1])}) 长{f(round(entity_length(e), 4))}"
    if k == "polyline":
        b = entity_bbox(e)
        tag = "矩形" if "rectangle" in e.tags else "多边形" if "polygon" in e.tags else "闭合多段线" if d.get("closed") else "多段线"
        return f"{tag} ({f(b[0])},{f(b[1])})-({f(b[2])},{f(b[3])})" if b else tag
    if k in ("text", "mtext"):
        return f"文字「{str(d.get('text', ''))[:12]}」"
    return f"{e.kind} {e.id}"


def describe(e) -> dict:
    return {"entity_id": e.id, "handle": e.handle, "type": e.kind, "tags": e.tags, "layer": e.layer, "bbox": entity_bbox(e), "key_params": key_params(e)}


# ------------------------------------------------------------------ 条件
def _field(e, name: str):
    kp = key_params(e)
    if name == "diameter":
        return kp.get("diameter")
    if name == "length":
        return kp.get("length") if "length" in kp else entity_length(e) or None
    if name in ("width", "height"):
        b = entity_bbox(e)
        return (b[2] - b[0] if name == "width" else b[3] - b[1]) if b else None
    return kp.get(name)


def _cmp(a, op: str, b) -> bool:
    if a is None:
        return False
    try:
        if op in ("=", "==", "eq"):
            return abs(float(a) - float(b)) <= 1e-6 * max(1.0, abs(float(b))) if isinstance(a, (int, float)) and not isinstance(a, bool) else str(a) == str(b)
        if op in ("!=", "ne"):
            return not _cmp(a, "=", b)
        if op in (">", "gt"):
            return float(a) > float(b)
        if op in (">=", "ge"):
            return float(a) >= float(b)
        if op in ("<", "lt"):
            return float(a) < float(b)
        if op in ("<=", "le"):
            return float(a) <= float(b)
        if op in ("contains", "in"):
            return str(b) in str(a)
    except (TypeError, ValueError):
        return False
    return False


def drawing_box(d, exclude: set[str] | None = None):
    boxes = [b for e in d.entities if (not exclude or e.id not in exclude) for b in [entity_bbox(e)] if b]
    if not boxes:
        return None
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))


def in_region(e, region, box) -> bool:
    if region in (None, "all") or box is None:
        return True
    b = entity_bbox(e)
    if b is None:
        return False
    cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    if isinstance(region, dict):
        r = region.get("bbox") or region.get("box")
        return bool(r) and r[0] <= cx <= r[2] and r[1] <= cy <= r[3]
    mx, my = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    w, h = (box[2] - box[0]) or 1.0, (box[3] - box[1]) or 1.0
    left, right, top, bottom = cx < mx, cx >= mx, cy >= my, cy < my
    return {"left": left, "right": right, "top": top, "bottom": bottom, "top_left": top and left, "top_right": top and right,
            "bottom_left": bottom and left, "bottom_right": bottom and right,
            "center": abs(cx - mx) <= w / 6 and abs(cy - my) <= h / 6}.get(region, True)


def _rect_poly(p1, p2):
    x0, x1, y0, y1 = min(p1[0], p2[0]), max(p1[0], p2[0]), min(p1[1], p2[1]), max(p1[1], p2[1])
    return [(x0, y0, 0.0), (x1, y0, 0.0), (x1, y1, 0.0), (x0, y1, 0.0)]


def _inside_poly(e, poly) -> bool:
    pts = entity_points(e)
    return bool(pts) and all(curves.contains_point(poly, p) for p in pts)


def _crosses_poly(e, poly) -> bool:
    if _inside_poly(e, poly) or any(curves.contains_point(poly, p) for p in entity_points(e)):
        return True
    edges = [Seg(poly[i], poly[(i + 1) % len(poly)]) for i in range(len(poly))]
    return any(curves.intersect(pr, ed) for pr in primitives(e) for ed in edges)


def _fence_hit(e, pts) -> bool:
    segs = [Seg(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
    return any(curves.intersect(pr, sg) for pr in primitives(e) for sg in segs)


def relation_holds(e, rel: str, other) -> bool:
    pa, pb = primitives(e), primitives(other)
    if rel in ("intersects", "touches"):
        return any(curves.intersect(a, b) for a in pa for b in pb) or (rel == "touches" and any(dist(curves.closest(a, curves.endpoints(b)[0]), curves.endpoints(b)[0]) < 1e-6 for a in pa for b in pb))
    if rel in ("parallel", "perpendicular"):
        if not (pa and pb and isinstance(pa[0], Seg) and isinstance(pb[0], Seg)):
            return False
        a1, a2 = ang(pa[0].a, pa[0].b) % 180, ang(pb[0].a, pb[0].b) % 180
        diff = abs(a1 - a2)
        diff = min(diff, 180 - diff)
        return diff < 1e-4 if rel == "parallel" else abs(diff - 90) < 1e-4
    if rel == "tangent":
        for a in pa:
            for b in pb:
                if isinstance(a, Arc) and isinstance(b, Arc):
                    d = dist(a.c, b.c)
                    if abs(d - (a.r + b.r)) < 1e-6 or abs(d - abs(a.r - b.r)) < 1e-6:
                        return True
                elif isinstance(a, Arc) and isinstance(b, Seg):
                    if abs(abs((a.c[0] - b.a[0]) * -curves.seg_dir(b)[1] + (a.c[1] - b.a[1]) * curves.seg_dir(b)[0]) - a.r) < 1e-6:
                        return True
                elif isinstance(a, Seg) and isinstance(b, Arc):
                    return relation_holds(other, "tangent", e)
        return False
    if rel == "concentric":
        ca = e.data.get("c")
        cb = other.data.get("c")
        return ca is not None and cb is not None and dist(ca, cb) < 1e-6
    if rel == "inside":
        ob = other
        if not is_closed(ob):
            return False
        poly = []
        for pr in primitives(ob):
            poly += sample(pr)[:-1]
        return _inside_poly(e, poly) and e.id != other.id
    return False


def _eff_color(d, e):
    """图元的实际颜色：自己没设（随层）就取图层颜色。"""
    c = e.color
    if c is None or str(c).upper() in ("BYLAYER", "256"):
        lay = d.get_layer(e.layer) if hasattr(d, "get_layer") else None
        return (lay.true_color or lay.color) if lay else 7
    return c


def query(d, entity_types=None, layer=None, color=None, linetype=None, lineweight_mm=None, tags=None, where=None, spatial_region=None,
          window=None, crossing=None, fence=None, near=None, relation=None, sort_by=None, limit=None, candidates=None) -> dict:
    space = getattr(d, "active_space", "Model")
    hidden = {l.name for l in getattr(d, "layers", []) if not l.on or l.frozen}
    pool = [e for e in d.entities if (candidates is None or e.id in candidates) and (candidates is not None or (e.space == space and e.layer not in hidden))]
    box = drawing_box(d, {e.id for e in d.entities if e.space != space})
    reasons: dict[str, list[str]] = {e.id: [] for e in pool}
    out = []
    for e in pool:
        why = reasons[e.id]
        if entity_types:
            ok = False
            for t in entity_types:
                kind, tag = TYPE_ALIASES.get(t, (t, None))
                if e.kind == kind and (tag is None or tag in e.tags):
                    ok = True
            if not ok:
                continue
            why.append("类型")
        if layer and e.layer.lower() != layer.lower():
            continue
        if color is not None and str(_eff_color(d, e)).upper() != str(color).upper():
            continue
        if linetype and (e.linetype or "").lower() != linetype.lower():
            continue
        if lineweight_mm is not None and e.lineweight_mm != lineweight_mm:
            continue
        if tags and not all(t in e.tags for t in tags):
            continue
        if where and not all(_cmp(_field(e, w["field"]), w.get("op", "="), w.get("value")) for w in where):
            continue
        if where:
            why.append("条件")
        if spatial_region and not in_region(e, spatial_region, box):
            continue
        if spatial_region:
            why.append(f"位于{spatial_region}")
        if window and not _inside_poly(e, _rect_poly(window[0], window[1])):
            continue
        if crossing and not _crosses_poly(e, _rect_poly(crossing[0], crossing[1])):
            continue
        if fence and not _fence_hit(e, [tuple(p) for p in fence]):
            continue
        if near:
            pts = entity_points(e)
            if not pts or min(dist(p, near["point"]) for p in pts) > float(near.get("radius", 1e9)) and not any(dist(curves.closest(pr, near["point"]), near["point"]) <= float(near.get("radius", 1e9)) for pr in primitives(e)):
                continue
        if relation:
            other = d.get_entity(relation["to"])
            if other is None or e.id == other.id or not relation_holds(e, relation["type"], other):
                continue
            why.append(f"与 {other.id} {relation['type']}")
        out.append(e)
    # 排序
    key = None
    cx = lambda e: (entity_bbox(e)[0] + entity_bbox(e)[2]) / 2 if entity_bbox(e) else 0.0
    cy = lambda e: (entity_bbox(e)[1] + entity_bbox(e)[3]) / 2 if entity_bbox(e) else 0.0
    order = {id(e): i for i, e in enumerate(d.entities)}
    if sort_by == "created_desc":
        out.sort(key=lambda e: -order[id(e)])
    elif sort_by == "area_desc":
        out.sort(key=lambda e: -(entity_area(e) or (entity_bbox(e) and (entity_bbox(e)[2] - entity_bbox(e)[0]) * (entity_bbox(e)[3] - entity_bbox(e)[1])) or 0))
    elif sort_by == "length_desc":
        out.sort(key=lambda e: -entity_length(e))
    elif sort_by == "x_asc":
        out.sort(key=cx)
    elif sort_by == "x_desc":
        out.sort(key=lambda e: -cx(e))
    elif sort_by == "y_asc":
        out.sort(key=cy)
    elif sort_by == "y_desc":
        out.sort(key=lambda e: -cy(e))
    elif isinstance(sort_by, dict) and sort_by.get("distance_to"):
        p = sort_by["distance_to"]
        out.sort(key=lambda e: dist((cx(e), cy(e), 0.0), p))
    total = len(out)
    if limit:
        out = out[: int(limit)]
    cands = []
    for i, e in enumerate(out):
        info = describe(e)
        info["score"] = round(1.0 if len(out) == 1 else max(0.2, 1.0 - 0.15 * i), 3) if sort_by or total == 1 else round(1.0 / max(1, total) ** 0.5, 3)
        info["reason"] = "、".join(reasons[e.id]) or "符合条件"
        cands.append(info)
    return {"candidates": cands, "total": total}
