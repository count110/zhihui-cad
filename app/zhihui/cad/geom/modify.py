"""编辑运算：复制、镜像、偏移、阵列、移动/旋转/缩放/拉伸、拉长/修剪/延伸、打断、倒角/倒圆角、分解。

纯函数：输入图元（GeoEntity）和参数，输出“操作”列表，由工具层写回 IR：
  {"op": "create", "kind", "data", "tags", "from": 源图元 id（继承图层/颜色/线型）}
  {"op": "replace", "id", "data", "kind"?, "tags"?}
  {"op": "delete", "id"}
"""
from __future__ import annotations

import copy
import math
import uuid

from shapely.geometry import LineString, Polygon

from . import curves
from .draw import TAN_QUARTER, vtx
from .entities import (EPS, Arc, Pt, Seg, Transform, ang, arc_bulge, bulge_arc, ccw_span, dist, entity_bbox, entity_points, is_closed, pl, polar, primitives,
                       sample, transform_data)
from .errors import GeomError


def create(kind: str, data: dict, tags: list[str] | None = None, src: str | None = None) -> dict:
    return {"op": "create", "kind": kind, "data": data, "tags": list(tags or []), "from": src}


def replace(eid: str, data: dict, kind: str | None = None, tags: list[str] | None = None) -> dict:
    return {"op": "replace", "id": eid, "data": data, "kind": kind, "tags": tags}


def delete(eid: str) -> dict:
    return {"op": "delete", "id": eid}


# ------------------------------------------------------------------ 复制 / 镜像 / 变换
def copy_entities(ents, base: Pt, targets: list[Pt]) -> list[dict]:
    ops = []
    for t in targets:
        T = Transform.translation(t[0] - base[0], t[1] - base[1], (t[2] - base[2]) if len(t) > 2 and len(base) > 2 else 0.0)
        for e in ents:
            ops.append(create(e.kind, transform_data(e.kind, e.data, T), e.tags, e.id))
    return ops


def mirror_entities(ents, p1: Pt, p2: Pt, erase_source: bool = False) -> list[dict]:
    if dist(p1, p2) < EPS:
        raise GeomError("DEGENERATE", "镜像线的两点重合", "")
    T = Transform.mirror(p1, p2)
    ops = [create(e.kind, transform_data(e.kind, e.data, T), e.tags, e.id) for e in ents]
    if erase_source:
        ops += [delete(e.id) for e in ents]
    return ops


def apply_transform(ents, T: Transform, copy_mode: bool = False) -> list[dict]:
    if copy_mode:
        return [create(e.kind, transform_data(e.kind, e.data, T), e.tags, e.id) for e in ents]
    return [replace(e.id, transform_data(e.kind, e.data, T)) for e in ents]


def rotation_for(base: Pt, rotation_deg: float | None, reference_deg: float | None = None, target_deg: float | None = None) -> Transform:
    if rotation_deg is None:
        if reference_deg is None or target_deg is None:
            raise GeomError("MISSING_ARG", "旋转需要旋转角度，或参照角度和目标角度", "")
        rotation_deg = target_deg - reference_deg
    return Transform.rotation(base, rotation_deg)


def scaling_for(base: Pt, factor: float | None, reference_length: tuple[float, float] | None) -> Transform:
    if factor is None and reference_length:
        if reference_length[0] <= EPS:
            raise GeomError("DEGENERATE", "参照长度必须大于 0", "")
        factor = reference_length[1] / reference_length[0]
    if factor is None:
        raise GeomError("MISSING_ARG", "缩放需要比例因子，或参照长度 [旧, 新]", "")
    if factor <= EPS:
        raise GeomError("INVALID_ARGS", "比例因子必须大于 0", "")
    return Transform.scaling(base, factor)


def _in_box(p, box) -> bool:
    return box[0] - 1e-9 <= p[0] <= box[2] + 1e-9 and box[1] - 1e-9 <= p[1] <= box[3] + 1e-9


def stretch_entity(e, box: tuple[float, float, float, float], dx: float, dy: float) -> dict | None:
    """交叉框内的顶点移动，框外不动；完全在框内则整体移动。返回新 data，不受影响返回 None。"""
    k, d = e.kind, copy.deepcopy(e.data)
    mv = lambda p: pl((p[0] + dx, p[1] + dy, p[2] if len(p) > 2 else 0.0))
    pts = entity_points(e)
    if pts and all(_in_box(p, box) for p in pts):
        return transform_data(k, e.data, Transform.translation(dx, dy))
    changed = False
    if k == "line":
        for key in ("p1", "p2"):
            if _in_box(d[key], box):
                d[key] = mv(d[key])
                changed = True
    elif k == "polyline":
        for v in d["vertices"]:
            if _in_box(v["p"], box):
                v["p"] = mv(v["p"])
                changed = True
    elif k in ("mline", "leader") and d.get("points"):
        d["points"] = [mv(p) if _in_box(p, box) else p for p in d["points"]]
        changed = any(_in_box(p, box) for p in e.data["points"])
    elif k in ("circle", "arc", "ellipse", "text", "mtext", "insert", "point"):
        c = d.get("c") or d.get("p")
        if c is not None and _in_box(c, box):
            return transform_data(k, e.data, Transform.translation(dx, dy))
    elif k == "dimension":
        return None
    return d if changed else None


# ------------------------------------------------------------------ 偏移
def _polyline_offset(e, distance: float, side_pt: Pt) -> list[Pt] | None:
    pts: list[Pt] = []
    for pr in primitives(e):
        pts += sample(pr, 0.02)[:-1] if pts or True else []
    ps = primitives(e)
    if ps:
        pts.append(curves.endpoints(ps[-1])[1])
    ln = LineString([(p[0], p[1]) for p in pts]) if not is_closed(e) else Polygon([(p[0], p[1]) for p in pts]).exterior
    if is_closed(e):
        poly = Polygon([(p[0], p[1]) for p in pts])
        from shapely.geometry import Point
        inside = poly.contains(Point(side_pt[0], side_pt[1]))
        buf = poly.buffer(-distance if inside else distance, join_style=2, mitre_limit=10)
        if buf.is_empty:
            return None
        g = buf if buf.geom_type == "Polygon" else max(buf.geoms, key=lambda x: x.area)
        return [(x, y, 0.0) for x, y in list(g.exterior.coords)[:-1]]
    sign = 1 if _side(ln, side_pt) >= 0 else -1
    off = ln.offset_curve(sign * distance, join_style=2, mitre_limit=10)
    if off.is_empty:
        return None
    if off.geom_type == "MultiLineString":
        off = max(off.geoms, key=lambda x: x.length)
    return [(x, y, 0.0) for x, y in off.coords]


def _side(ln: LineString, p: Pt) -> float:
    """点在折线哪一侧：左为正（shapely offset_curve 的正方向是左侧）。"""
    from shapely.geometry import Point
    pt = Point(p[0], p[1])
    proj = ln.project(pt)
    a = ln.interpolate(max(0.0, proj - 1e-6))
    b = ln.interpolate(min(ln.length, proj + 1e-6))
    cross = (b.x - a.x) * (pt.y - a.y) - (b.y - a.y) * (pt.x - a.x)
    return cross


def offset_entity(e, distance: float, side_pt: Pt | None = None, through_pt: Pt | None = None) -> list[dict]:
    k, d = e.kind, e.data
    prims = primitives(e)
    if through_pt is not None and side_pt is None:
        side_pt = through_pt
    if through_pt is not None:
        distance = min(dist(curves.closest(pr, through_pt), through_pt) for pr in prims) if prims else distance
    if distance <= EPS:
        raise GeomError("INVALID_ARGS", "偏移距离必须大于 0", "")
    if side_pt is None:
        raise GeomError("MISSING_ARG", "偏移需要给出偏移到哪一侧（一个点），或通过点", "")
    if k == "line":
        o = curves.offset_prim(prims[0], distance, side_pt)
        return [create("line", {"p1": pl(o.a), "p2": pl(o.b)}, e.tags, e.id)]
    if k == "circle":
        o = curves.offset_prim(prims[0], distance, side_pt)
        if o is None:
            raise GeomError("OFFSET_TOO_LARGE", f"向内偏移 {distance:g} 超过半径 {d['r']:g}", f"向内偏移不能超过 {d['r']:g}", max_distance=d["r"])
        return [create("circle", {"c": d["c"], "r": o.r}, e.tags, e.id)]
    if k == "arc":
        o = curves.offset_prim(prims[0], distance, side_pt)
        if o is None:
            raise GeomError("OFFSET_TOO_LARGE", f"向内偏移 {distance:g} 超过半径 {d['r']:g}", "", max_distance=d["r"])
        return [create("arc", {"c": d["c"], "r": o.r, "a0": d["a0"], "a1": d["a1"]}, e.tags, e.id)]
    if k in ("polyline", "ellipse", "spline", "mline"):
        pts = _polyline_offset(e, distance, side_pt)
        if not pts:
            raise GeomError("OFFSET_TOO_LARGE", "偏移距离过大，轮廓退化", "减小偏移距离")
        closed = is_closed(e)
        out = {"vertices": [vtx(p) for p in pts], "closed": closed, "width": 0.0}
        has_arc = any(isinstance(p, Arc) for p in prims) or k != "polyline"
        return [create("polyline", out, [t for t in e.tags if t not in ("rectangle", "polygon")] if has_arc else e.tags, e.id)]
    raise GeomError("UNSUPPORTED", f"{k} 不能偏移", "只有直线、圆、圆弧、多段线、椭圆、样条可以偏移")


# ------------------------------------------------------------------ 阵列
def array_rect(ents, rows: int, cols: int, row_spacing: float, col_spacing: float, group: str | None = None) -> list[dict]:
    if rows < 1 or cols < 1 or rows * cols < 2:
        raise GeomError("INVALID_ARGS", "矩形阵列的行数、列数至少有一个大于 1", "")
    if rows * cols > 2000:
        raise GeomError("TOO_MANY", "阵列项目超过 2000 个", "减少行列数")
    g = group or "array:" + uuid.uuid4().hex[:6]
    ops = []
    for r in range(rows):
        for c in range(cols):
            if r == 0 and c == 0:
                continue
            T = Transform.translation(c * col_spacing, r * row_spacing)
            ops += [create(e.kind, transform_data(e.kind, e.data, T), e.tags + [g], e.id) for e in ents]
    return ops


def array_polar(ents, center: Pt, count: int, fill_angle_deg: float = 360.0, rotate_items: bool = True, group: str | None = None) -> list[dict]:
    if count < 2:
        raise GeomError("INVALID_ARGS", "环形阵列项目数至少 2（含原对象）", "")
    if count > 2000:
        raise GeomError("TOO_MANY", "阵列项目超过 2000 个", "")
    g = group or "array:" + uuid.uuid4().hex[:6]
    step = fill_angle_deg / count if abs(fill_angle_deg) >= 360 - 1e-9 else fill_angle_deg / (count - 1)
    ops = []
    for i in range(1, count):
        a = step * i
        T = Transform.rotation(center, a)
        if not rotate_items:
            # 只平移到旋转后的位置：用对象基点（包围盒中心）的轨迹
            for e in ents:
                b = entity_bbox(e)
                c0 = ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2, 0.0) if b else center
                c1 = T(c0)
                ops.append(create(e.kind, transform_data(e.kind, e.data, Transform.translation(c1[0] - c0[0], c1[1] - c0[1])), e.tags + [g], e.id))
        else:
            ops += [create(e.kind, transform_data(e.kind, e.data, T), e.tags + [g], e.id) for e in ents]
    return ops


def array_path(ents, path_entity, count: int, spacing: float | None, align: bool = False, group: str | None = None) -> list[dict]:
    prims = primitives(path_entity)
    if not prims:
        raise GeomError("INVALID_PATH", "路径图元没有可用的曲线", "")
    total = sum(p.length for p in prims)
    if count < 2:
        raise GeomError("INVALID_ARGS", "路径阵列项目数至少 2", "")
    step = spacing if spacing else total / (count - 1)
    if step * (count - 1) > total + 1e-6:
        raise GeomError("PATH_TOO_SHORT", f"路径长 {total:g}，放不下 {count} 个间距 {step:g}", "减少数量或间距", path_length=total)
    g = group or "array:" + uuid.uuid4().hex[:6]
    b0 = entity_bbox(ents[0]) if ents else None
    base = ((b0[0] + b0[2]) / 2, (b0[1] + b0[3]) / 2, 0.0) if b0 else (0.0, 0.0, 0.0)
    ops = []
    for i in range(1, count):
        s = step * i
        run = s
        pt, tang = None, 0.0
        for pr in prims:
            if run <= pr.length + 1e-9:
                pt = curves.at_length(pr, run)
                tang = ang(curves.at_length(pr, max(0.0, run - 0.01)), curves.at_length(pr, run + 0.01)) if isinstance(pr, Seg) or True else 0.0
                break
            run -= pr.length
        pt = pt or curves.endpoints(prims[-1])[1]
        T = Transform.translation(pt[0] - base[0], pt[1] - base[1])
        if align:
            T = Transform.translation(-base[0], -base[1]).then(Transform.rotation((0, 0, 0), tang)).then(Transform.translation(pt[0], pt[1]))
        ops += [create(e.kind, transform_data(e.kind, e.data, T), e.tags + [g], e.id) for e in ents]
    return ops


# ------------------------------------------------------------------ 原语 → 实体
def prims_to_entities(prims: list, src, keep_tags: bool = False) -> list[dict]:
    """若干首尾相连的原语合成一个多段线（或单条直线/圆弧）。"""
    if not prims:
        return []
    if len(prims) == 1:
        p = prims[0]
        if isinstance(p, Seg):
            return [create("line", {"p1": pl(p.a), "p2": pl(p.b)}, src.tags if keep_tags else [], src.id)]
        if p.full:
            return [create("circle", {"c": pl(p.c), "r": p.r}, src.tags if keep_tags else [], src.id)]
        return [create("arc", {"c": pl(p.c), "r": p.r, "a0": p.a0 % 360, "a1": (p.a0 + p.span) % 360}, src.tags if keep_tags else [], src.id)]
    verts = []
    for p in prims:
        s = p.a if isinstance(p, Seg) else p.start()
        b = 0.0 if isinstance(p, Seg) else arc_bulge(p)
        verts.append(vtx(s, b))
    last = prims[-1]
    verts.append(vtx(last.b if isinstance(last, Seg) else last.end()))
    return [create("polyline", {"vertices": verts, "closed": False, "width": 0.0}, [], src.id)]


# ------------------------------------------------------------------ 拉长
def lengthen(e, mode: str, value: float, end: str = "end", pick: Pt | None = None) -> dict:
    prims = primitives(e)
    if e.kind not in ("line", "arc", "polyline") or not prims or is_closed(e):
        raise GeomError("UNSUPPORTED", f"{e.kind} 不能拉长", "只有直线、圆弧和不闭合的多段线可以拉长")
    total = sum(p.length for p in prims)
    if mode == "percent":
        delta = total * value / 100.0 - total
    elif mode == "total":
        delta = value - total
    else:
        delta = value
    if total + delta <= EPS:
        raise GeomError("TOO_SHORT", "拉长后长度 ≤ 0", f"最多缩短 {total:g}", length=total)
    at_end = end == "end" or (end == "nearest" and pick is not None and dist(pick, curves.endpoints(prims[-1])[1]) <= dist(pick, curves.endpoints(prims[0])[0]))
    d = copy.deepcopy(e.data)
    if e.kind == "line":
        a, b = Seg(*[tuple(d["p1"]) + (0.0,) * (3 - len(d["p1"])), tuple(d["p2"]) + (0.0,) * (3 - len(d["p2"]))]), None
        s = prims[0]
        L = s.length
        if at_end:
            d["p2"] = pl(curves.seg_point(s, (L + delta) / L))
        else:
            d["p1"] = pl(curves.seg_point(s, -delta / L))
    elif e.kind == "arc":
        da = math.degrees(delta / d["r"])
        if at_end:
            d["a1"] = (d["a1"] + da) % 360
        else:
            d["a0"] = (d["a0"] - da) % 360
    else:
        # 多段线：延长/缩短首末段（直线段）
        vs = d["vertices"]
        i = len(vs) - 1 if at_end else 0
        j = i - 1 if at_end else 1
        if abs(float(vs[j].get("bulge", 0) if at_end else vs[0].get("bulge", 0))) > 1e-12:
            raise GeomError("UNSUPPORTED", "末段是圆弧的多段线不能拉长", "先分解")
        p, q = vs[i]["p"], vs[j]["p"]
        L = dist(p, q)
        if L + delta <= EPS:
            raise GeomError("TOO_SHORT", "末段缩短后长度 ≤ 0", "", length=L)
        k = (L + delta) / L
        vs[i]["p"] = pl((q[0] + (p[0] - q[0]) * k, q[1] + (p[1] - q[1]) * k, 0.0))
    return d


# ------------------------------------------------------------------ 修剪 / 延伸
def _cut_points(prim, boundaries: list, extend: bool = False) -> list[Pt]:
    pts: list[Pt] = []
    for b in boundaries:
        for q in primitives(b):
            pts += curves.intersect(prim, q, extend=False)
    out: list[Pt] = []
    for p in pts:
        if not any(dist(p, o) < 1e-6 for o in out):
            out.append(p)
    return out


def trim_entity(e, pick: Pt, boundaries: list) -> list[dict]:
    """沿拾取点所在的一段，从最近的两个交点处剪掉。"""
    prims = primitives(e)
    if e.kind not in ("line", "arc", "circle", "polyline", "spline", "ellipse") or not prims:
        raise GeomError("UNSUPPORTED", f"{e.kind} 不能修剪", "")
    # 先把每个原语在交点处切开，再按顺序拼成片段
    pieces: list[list] = [[]]
    cuts_total = 0
    for pr in prims:
        cuts = _cut_points(pr, [b for b in boundaries if b.id != e.id])
        cuts_total += len(cuts)
        parts = curves.split_at(pr, cuts) if cuts else [pr]
        for i, part in enumerate(parts):
            pieces[-1].append(part)
            if i < len(parts) - 1:
                pieces.append([])
    if cuts_total == 0:
        raise GeomError("NO_INTERSECTION", "这个对象和边界没有交点，不能修剪", "检查边界对象，或改用延伸")
    closed = is_closed(e)
    if closed and len(pieces) > 1:          # 闭合对象：首尾片段本是连着的
        pieces[0] = pieces.pop() + pieces[0]
    # 要删除的片段：离拾取点最近
    def dpiece(pc):
        return min(dist(curves.closest(p, pick), pick) for p in pc) if pc else 1e18
    pieces = [pc for pc in pieces if pc]
    if len(pieces) == 1 and cuts_total == 1 and not closed:
        pass
    target = min(range(len(pieces)), key=lambda i: dpiece(pieces[i]))
    keep = [pc for i, pc in enumerate(pieces) if i != target]
    ops: list[dict] = []
    for pc in keep:
        ops += prims_to_entities(pc, e, keep_tags=False)
    # 第一段沿用原图元的 id（AutoCAD 里修剪改的是原对象；关联填充、标注和后续引用都靠这个 id）
    first = next((i for i, op in enumerate(ops) if op["op"] == "create"), None)
    if first is None:
        return [delete(e.id)]
    c = ops[first]
    ops[first] = {"op": "replace", "id": e.id, "kind": c["kind"], "data": c["data"], "tags": c.get("tags") or []}
    return ops


def extend_entity(e, pick: Pt, boundaries: list) -> dict:
    """把离拾取点近的一端延伸到最近的边界。返回新 data。"""
    prims = primitives(e)
    if e.kind not in ("line", "arc") or not prims:
        raise GeomError("UNSUPPORTED", f"{e.kind} 暂不支持延伸", "直线和圆弧可以延伸")
    pr = prims[0]
    ends = curves.endpoints(pr)
    at_end = dist(pick, ends[1]) <= dist(pick, ends[0])
    cands: list[tuple[float, Pt]] = []
    for b in boundaries:
        if b.id == e.id:
            continue
        for q in primitives(b):
            for pt in curves.intersect(pr, q, extend=True):
                # 必须落在边界原语上
                if dist(curves.closest(q, pt), pt) > 1e-6:
                    continue
                if isinstance(pr, Seg):
                    t = curves.seg_t(pr, pt)
                    beyond = t - 1.0 if at_end else -t
                    if beyond > 1e-7:
                        cands.append((beyond, pt))
                else:
                    a = (ang(pr.c, pt) - (pr.a0 + pr.span if at_end else pr.a0)) % 360
                    off = a if at_end else (360 - a) % 360
                    if off > 1e-7:
                        cands.append((off, pt))
    if not cands:
        raise GeomError("NO_INTERSECTION", "这个端点方向上没有可延伸到的边界", "")
    _, pt = min(cands, key=lambda c: c[0])
    d = copy.deepcopy(e.data)
    if e.kind == "line":
        d["p2" if at_end else "p1"] = pl(pt)
    else:
        a_new = ang(pr.c, pt)
        if at_end:
            d["a1"] = a_new
        else:
            d["a0"] = a_new
    return d


# ------------------------------------------------------------------ 打断
def break_entity(e, p1: Pt, p2: Pt | None) -> list[dict]:
    prims = primitives(e)
    if e.kind not in ("line", "arc", "circle", "polyline") or not prims:
        raise GeomError("UNSUPPORTED", f"{e.kind} 不能打断", "")
    if p2 is None:
        # 在一点处断成两段
        pt = min((curves.closest(pr, p1) for pr in prims), key=lambda p: dist(p, p1))
        if dist(pt, p1) > 1e-3 * max(1.0, (entity_bbox(e) or (0, 0, 1, 1))[2]):
            raise GeomError("POINT_NOT_ON_ENTITY", "打断点不在图元上", "用 nearest 捕捉得到图元上的点")
        pieces: list[list] = [[]]
        for pr in prims:
            if dist(curves.closest(pr, pt), pt) < 1e-6:
                parts = curves.split_at(pr, [pt])
                for i, part in enumerate(parts):
                    pieces[-1].append(part)
                    if i < len(parts) - 1:
                        pieces.append([])
            else:
                pieces[-1].append(pr)
        pieces = [pc for pc in pieces if pc]
        if len(pieces) < 2 and not is_closed(e):
            raise GeomError("AT_ENDPOINT", "打断点在端点上，无法打断", "")
        ops = [delete(e.id)]
        for pc in pieces:
            ops += prims_to_entities(pc, e, keep_tags=False)
        return ops
    # 两点之间删除
    pts = []
    for p in (p1, p2):
        pt = min((curves.closest(pr, p) for pr in prims), key=lambda q: dist(q, p))
        pts.append(pt)
    if e.kind == "circle":
        c = prims[0]
        a1, a2 = ang(c.c, pts[0]), ang(c.c, pts[1])
        # AutoCAD：从第一点逆时针到第二点之间的部分被删除，留下的是第二点 → 第一点
        return [delete(e.id), create("arc", {"c": e.data["c"], "r": e.data["r"], "a0": a2, "a1": a1}, e.tags, e.id)]
    ops = [delete(e.id)]
    # 拉成一串原语：按沿线参数排序后删掉中间那段
    flat = []
    for pr in prims:
        cuts = [pt for pt in pts if dist(curves.closest(pr, pt), pt) < 1e-6]
        flat += curves.split_at(pr, cuts) if cuts else [pr]
    # 起止片段：端点落在 pts 上的边界
    def at_pt(p):
        return any(dist(p, q) < 1e-6 for q in pts)
    seq = flat
    order = [curves.endpoints(x)[0] for x in seq]
    # 找到两处断点的位置
    idxs = [i for i, x in enumerate(seq) if at_pt(curves.endpoints(x)[0])]
    if len(idxs) < 2:
        raise GeomError("POINT_NOT_ON_ENTITY", "两个打断点没有同时落在图元上", "")
    i0, i1 = idxs[0], idxs[1]
    left, right = seq[:i0], seq[i1:]
    for pc in (left, right):
        if pc:
            ops += prims_to_entities(pc, e)
    return ops


# ------------------------------------------------------------------ 倒角 / 倒圆角
def _corner(p1: Seg, p2: Seg) -> Pt | None:
    pts = curves.intersect(p1, p2, extend=True)
    return pts[0] if pts else None


def fillet_lines(e1, e2, radius: float, pick1: Pt | None = None, pick2: Pt | None = None, trim: bool = True) -> list[dict]:
    """两条直线倒圆角（半径 0 则直接相交成角）。"""
    s1, s2 = primitives(e1)[0], primitives(e2)[0]
    if not (isinstance(s1, Seg) and isinstance(s2, Seg)):
        raise GeomError("UNSUPPORTED", "目前只支持直线与直线的倒圆角/倒角（圆弧请先分解或用 TTR）", "")
    X = _corner(s1, s2)
    if X is None:
        raise GeomError("PARALLEL", "两条线平行，不能倒角", "")
    # 每条线保留离交点较远的那一端（有拾取点则保留拾取点所在的一侧）
    def far_end(s: Seg, pick):
        a, b = s.a, s.b
        if pick is not None:
            t = curves.seg_t(s, pick)
            tx = curves.seg_t(s, X)
            return "b" if (t > tx) else "a"
        return "b" if dist(b, X) >= dist(a, X) else "a"
    k1, k2 = far_end(s1, pick1), far_end(s2, pick2)
    keep1 = s1.b if k1 == "b" else s1.a
    keep2 = s2.b if k2 == "b" else s2.a
    u1 = ((keep1[0] - X[0]) / (dist(keep1, X) or 1), (keep1[1] - X[1]) / (dist(keep1, X) or 1))
    u2 = ((keep2[0] - X[0]) / (dist(keep2, X) or 1), (keep2[1] - X[1]) / (dist(keep2, X) or 1))
    cosang = max(-1.0, min(1.0, u1[0] * u2[0] + u1[1] * u2[1]))
    theta = math.acos(cosang)
    if theta < 1e-6 or abs(theta - math.pi) < 1e-6:
        raise GeomError("PARALLEL", "两条线共线或平行，不能倒角", "")
    ops = []
    if radius <= EPS:                       # R0：两线延伸/修剪到交点，直接成角
        d1, d2 = copy.deepcopy(e1.data), copy.deepcopy(e2.data)
        d1["p1" if k1 == "b" else "p2"] = pl(X)
        d2["p1" if k2 == "b" else "p2"] = pl(X)
        return [replace(e1.id, d1), replace(e2.id, d2)]
    t = radius / math.tan(theta / 2)                          # 交点到切点的距离
    L1, L2 = dist(keep1, X), dist(keep2, X)
    if t > L1 + 1e-9 or t > L2 + 1e-9:
        mx = min(L1, L2) * math.tan(theta / 2)
        raise GeomError("RADIUS_TOO_LARGE", f"圆角半径 {radius:g} 太大，最大 {mx:g}", f"圆角半径不能超过 {mx:g}", max_radius=mx)
    T1 = (X[0] + u1[0] * t, X[1] + u1[1] * t, 0.0)
    T2 = (X[0] + u2[0] * t, X[1] + u2[1] * t, 0.0)
    bis = (u1[0] + u2[0], u1[1] + u2[1])
    bl = math.hypot(*bis)
    cd = radius / math.sin(theta / 2)
    C = (X[0] + bis[0] / bl * cd, X[1] + bis[1] / bl * cd, 0.0)
    a_t1, a_t2 = ang(C, T1), ang(C, T2)
    arc_d = {"c": pl(C), "r": radius, "a0": a_t1, "a1": a_t2} if ccw_span(a_t1, a_t2) < 180 else {"c": pl(C), "r": radius, "a0": a_t2, "a1": a_t1}
    d1, d2 = copy.deepcopy(e1.data), copy.deepcopy(e2.data)
    d1["p1" if k1 == "b" else "p2"] = pl(T1)
    d2["p1" if k2 == "b" else "p2"] = pl(T2)
    d1["p2" if k1 == "b" else "p1"] = pl(keep1)
    d2["p2" if k2 == "b" else "p1"] = pl(keep2)
    return [replace(e1.id, d1), replace(e2.id, d2), create("arc", arc_d, [], e1.id)]


def chamfer_lines(e1, e2, d1: float, d2: float, pick1: Pt | None = None, pick2: Pt | None = None) -> list[dict]:
    s1, s2 = primitives(e1)[0], primitives(e2)[0]
    if not (isinstance(s1, Seg) and isinstance(s2, Seg)):
        raise GeomError("UNSUPPORTED", "目前只支持直线与直线的倒角", "")
    X = _corner(s1, s2)
    if X is None:
        raise GeomError("PARALLEL", "两条线平行，不能倒角", "")
    def far(s: Seg, pick):
        if pick is not None:
            return "b" if curves.seg_t(s, pick) > curves.seg_t(s, X) else "a"
        return "b" if dist(s.b, X) >= dist(s.a, X) else "a"
    k1, k2 = far(s1, pick1), far(s2, pick2)
    keep1, keep2 = (s1.b if k1 == "b" else s1.a), (s2.b if k2 == "b" else s2.a)
    L1, L2 = dist(keep1, X), dist(keep2, X)
    if d1 > L1 + 1e-9 or d2 > L2 + 1e-9:
        raise GeomError("RADIUS_TOO_LARGE", f"倒角距离太大（{d1:g}, {d2:g}），可用长度 {L1:g}, {L2:g}", "减小倒角距离", max_distance=min(L1, L2))
    u1 = ((keep1[0] - X[0]) / L1, (keep1[1] - X[1]) / L1)
    u2 = ((keep2[0] - X[0]) / L2, (keep2[1] - X[1]) / L2)
    T1 = (X[0] + u1[0] * d1, X[1] + u1[1] * d1, 0.0)
    T2 = (X[0] + u2[0] * d2, X[1] + u2[1] * d2, 0.0)
    a, b = copy.deepcopy(e1.data), copy.deepcopy(e2.data)
    a["p1" if k1 == "b" else "p2"] = pl(T1)
    a["p2" if k1 == "b" else "p1"] = pl(keep1)
    b["p1" if k2 == "b" else "p2"] = pl(T2)
    b["p2" if k2 == "b" else "p1"] = pl(keep2)
    return [replace(e1.id, a), replace(e2.id, b), create("line", {"p1": pl(T1), "p2": pl(T2)}, [], e1.id)]


def polyline_corners(e, kind: str, size1: float, size2: float | None = None) -> dict:
    """整条（直线段）多段线的所有拐角倒圆角/倒角，返回新 data。"""
    if e.kind != "polyline" or any(isinstance(p, Arc) for p in primitives(e)):
        raise GeomError("UNSUPPORTED", "整条倒角只支持由直线段组成的多段线", "")
    vs = [tuple(v["p"]) + (0.0,) * (3 - len(v["p"])) for v in e.data["vertices"]]
    n, closed = len(vs), bool(e.data.get("closed"))
    new: list[dict] = []
    corners = range(n) if closed else range(1, n - 1)
    for i in range(n):
        p = vs[i]
        if i not in corners:
            new.append(vtx(p))
            continue
        a, b = vs[i - 1], vs[(i + 1) % n]
        u1 = ((a[0] - p[0]) / (dist(a, p) or 1), (a[1] - p[1]) / (dist(a, p) or 1))
        u2 = ((b[0] - p[0]) / (dist(b, p) or 1), (b[1] - p[1]) / (dist(b, p) or 1))
        theta = math.acos(max(-1.0, min(1.0, u1[0] * u2[0] + u1[1] * u2[1])))
        if theta < 1e-6 or abs(theta - math.pi) < 1e-6:
            new.append(vtx(p))
            continue
        if kind == "fillet":
            t = size1 / math.tan(theta / 2)
            lim = min(dist(a, p), dist(b, p)) / (2 if (closed or (i - 1 in corners) and (i + 1 in corners)) else 1)
            if t > lim + 1e-9:
                mx = lim * math.tan(theta / 2)
                raise GeomError("RADIUS_TOO_LARGE", f"圆角半径 {size1:g} 太大，最大 {mx:g}", f"圆角半径不能超过 {mx:g}", max_radius=mx)
            t1 = (p[0] + u1[0] * t, p[1] + u1[1] * t, 0.0)
            t2 = (p[0] + u2[0] * t, p[1] + u2[1] * t, 0.0)
            turn = (u1[0] * u2[1] - u1[1] * u2[0])
            # 沿 a→p→b 行进：拐向左（逆时针）bulge 为正
            cross = (p[0] - a[0]) * (b[1] - p[1]) - (p[1] - a[1]) * (b[0] - p[0])
            bulge = math.tan((math.pi - theta) / 4) * (1 if cross > 0 else -1)
            new.append(vtx(t1))
            new.append(vtx(t2, 0.0))
            new[-2]["bulge"] = round(bulge, 12)
        else:
            d1, d2 = size1, size2 if size2 is not None else size1
            if d1 > dist(a, p) or d2 > dist(b, p):
                raise GeomError("RADIUS_TOO_LARGE", "倒角距离太大", "")
            new.append(vtx((p[0] + u1[0] * d1, p[1] + u1[1] * d1, 0.0)))
            new.append(vtx((p[0] + u2[0] * d2, p[1] + u2[1] * d2, 0.0)))
    return {"vertices": new, "closed": closed, "width": e.data.get("width", 0.0)}


# ------------------------------------------------------------------ 分解
def explode(e, blocks: dict | None = None) -> list[dict]:
    k, d = e.kind, e.data
    if k == "polyline":
        out = []
        for pr in primitives(e):
            out += prims_to_entities([pr], e, keep_tags=False)
        return [delete(e.id)] + out
    if k == "mline":
        out = []
        for pr in primitives(e):
            out += prims_to_entities([pr], e)
        return [delete(e.id)] + out
    if k == "hatch":
        out = []
        for loop in d.get("loops", []):
            out.append(create("polyline", {"vertices": [vtx(p) for p in loop], "closed": True, "width": 0.0}, ["hatch_boundary"], e.id))
        return [delete(e.id)] + out
    if k == "dimension":
        out = []
        pts = [d.get("p1"), d.get("p2")]
        if d.get("loc") and d.get("p1") and d.get("p2"):
            out.append(create("line", {"p1": pl(d["p1"]), "p2": pl(d["p2"])}, ["dim_line"], e.id))
        txt = d.get("text") or str(d.get("measurement", ""))
        out.append(create("text", {"p": pl(d.get("text_pos") or d.get("loc") or d.get("p1") or (0, 0, 0)), "text": txt, "height": 3.5, "rotation": 0.0, "style": "Standard"}, [], e.id))
        return [delete(e.id)] + out
    if k == "insert":
        blk = (blocks or {}).get(d.get("block"))
        if not blk:
            raise GeomError("BLOCK_NOT_FOUND", f"块 {d.get('block')} 没有定义", "")
        sc = d.get("scale", [1, 1, 1])
        T = Transform(sc[0], 0, 0, sc[1], 0, 0, sz=sc[2]).then(Transform.rotation((0, 0, 0), d.get("rotation", 0))).then(Transform.translation(*P3(d["p"])))
        base = blk.get("base_point", [0, 0, 0])
        T = Transform.translation(-base[0], -base[1], 0).then(T)
        out = [create(be["kind"], transform_data(be["kind"], be["data"], T), be.get("tags", []), e.id) for be in blk.get("entities", [])]
        return [delete(e.id)] + out
    raise GeomError("UNSUPPORTED", f"{k} 不能分解", "矩形/多边形/多段线/多线/标注/填充/块参照可以分解")


def P3(p):
    return (float(p[0]), float(p[1]), float(p[2]) if len(p) > 2 else 0.0)
