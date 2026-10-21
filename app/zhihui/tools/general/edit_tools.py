"""二维编辑（函数 20–27：复制镜像偏移阵列、移动旋转缩放拉伸、拉长修剪延伸、打断倒角、分解、夹点、特性、特性匹配）。"""
from __future__ import annotations

import math
from typing import Any, Literal, Optional

from pydantic import Field

from zhihui.cad.geom import curves, grips, modify
from zhihui.cad.geom.entities import EPS, P, Transform, ang, dist, entity_bbox, pl, primitives, transform_data
from zhihui.tools.core import ToolError
from .ctx import Ctx
from .env_tools import norm_linetype, parse_color
from .registry import Args, Ids, Pt, tool
from . import view_tools  # noqa: F401


def _need(cond, msg: str, hint: str = "缺少关键参数，向用户确认后再执行") -> None:
    if not cond:
        raise ToolError("MISSING_ARG", msg, hint)


# ------------------------------------------------------------------ 20 replicate_entities
class ReplicateArgs(Args):
    operation: Literal["copy", "mirror", "offset", "array_rect", "array_polar", "array_path"]
    entity_ids: Ids
    base_point: Pt = None
    target_points: Optional[list[Pt]] = None
    mirror_line: Optional[list[Pt]] = None
    erase_source: bool = False
    offset_distance: Optional[float] = Field(None, gt=0)
    offset_side_point: Pt = None
    offset_through_point: Pt = None
    offset_side: Optional[Literal["outside", "inside", "left", "right"]] = None
    array_params: Optional[dict] = None


def _side_point(e, which: str):
    """“向外/向内/向左/向右偏移”→ 一个落在对应一侧的点（由程序算，不让模型算）。"""
    from shapely.geometry import Point, Polygon
    k, d = e.kind, e.data
    if k in ("circle", "arc"):
        c = P(d["c"])
        if which in ("outside", "left", "right"):
            return (c[0] + d["r"] + 1.0, c[1], 0.0)
        return (c[0], c[1], 0.0)
    prs = primitives(e)
    if not prs:
        raise ToolError("INVALID_ARGS", f"{e.id} 不能确定偏移的一侧", "给出 offset_side_point")
    pr = prs[0]
    a, b = (pr.a, pr.b) if hasattr(pr, "a") else (pr.start(), pr.end())
    mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
    L = math.hypot(b[0] - a[0], b[1] - a[1]) or 1.0
    nx, ny = -(b[1] - a[1]) / L, (b[0] - a[0]) / L                     # 行进方向的左侧
    if which in ("left", "right"):
        sg = 1.0 if which == "left" else -1.0
        return (mid[0] + sg * nx, mid[1] + sg * ny, 0.0)
    from zhihui.cad.geom.entities import is_closed
    if not is_closed(e):
        raise ToolError("INVALID_ARGS", f"{e.id} 不是闭合图形，不能说向外/向内", "改用 left/right，或给出 offset_side_point")
    pts: list = []
    for q in prs:
        pts.append((q.a[0], q.a[1]) if hasattr(q, "a") else (q.start()[0], q.start()[1]))
    poly = Polygon(pts)
    cand = (mid[0] + nx * 0.5, mid[1] + ny * 0.5)
    inside = poly.contains(Point(cand))
    if (which == "outside") == inside:
        cand = (mid[0] - nx * 0.5, mid[1] - ny * 0.5)
    return (cand[0], cand[1], 0.0)


@tool("replicate_entities", "2d_editing", ReplicateArgs)
def replicate_entities(ctx: Ctx, a: ReplicateArgs) -> dict:
    """复制、镜像、偏移和阵列。copy：base_point + target_points；mirror：mirror_line=[p1,p2]；offset：offset_distance + offset_side_point（或 offset_through_point）；
    array_rect {rows, cols, row_spacing, col_spacing}；array_polar {center, count(含原对象), fill_angle_deg, rotate_items}；array_path {path_entity_id, count, spacing, align}。阵列结果是独立图元并共享 array:<组号> 标签。"""
    ents = ctx.entities(a.entity_ids)
    _need(ents, "需要 entity_ids")
    op = a.operation
    if op == "copy":
        _need(a.base_point is not None and a.target_points, "复制需要 base_point 和 target_points")
        base = ctx.point(a.base_point)
        tgts = [ctx.point(p) for p in a.target_points]
        ops = modify.copy_entities(ents, base, tgts)
    elif op == "mirror":
        _need(a.mirror_line and len(a.mirror_line) == 2, "镜像需要 mirror_line（两点）")
        ops = modify.mirror_entities(ents, ctx.point(a.mirror_line[0]), ctx.point(a.mirror_line[1]), a.erase_source)
    elif op == "offset":
        _need(a.offset_distance or a.offset_through_point is not None, "偏移需要 offset_distance 或 offset_through_point", "向用户确认偏移距离")
        side = ctx.point(a.offset_side_point) if a.offset_side_point is not None else None
        thr = ctx.point(a.offset_through_point) if a.offset_through_point is not None else None
        ops = []
        for e in ents:
            sp = side if side is not None or a.offset_side is None else _side_point(e, a.offset_side)
            ops += modify.offset_entity(e, a.offset_distance or 0.0, sp, thr)
    else:
        ap = a.array_params or {}
        if op == "array_rect":
            _need(ap.get("rows") and ap.get("cols") and ap.get("row_spacing") is not None and ap.get("col_spacing") is not None, "矩形阵列需要 rows、cols、row_spacing、col_spacing", "向用户确认行列数和间距")
            ops = modify.array_rect(ents, int(ap["rows"]), int(ap["cols"]), float(ap["row_spacing"]), float(ap["col_spacing"]))
        elif op == "array_polar":
            _need(ap.get("center") is not None and ap.get("count"), "环形阵列需要 center 和 count", "向用户确认阵列中心和数量")
            ops = modify.array_polar(ents, ctx.point(ap["center"]), int(ap["count"]), float(ap.get("fill_angle_deg", 360)), bool(ap.get("rotate_items", True)))
        else:
            _need(ap.get("path_entity_id") and ap.get("count"), "路径阵列需要 path_entity_id 和 count")
            ops = modify.array_path(ents, ctx.entity(ap["path_entity_id"]), int(ap["count"]), ap.get("spacing"), bool(ap.get("align", False)))
        grp = next((t for o in ops for t in o["tags"] if t.startswith("array:")), None)
        if grp:
            for e in ents:
                if grp not in e.tags:
                    e.tags.append(grp)
            ctx.data["group"] = grp
    ctx.apply_ops(ops)
    name = {"copy": "复制", "mirror": "镜像", "offset": "偏移", "array_rect": "矩形阵列", "array_polar": "环形阵列", "array_path": "路径阵列"}[op]
    return ctx.result(f"{name}：新建 {len(ctx.created)} 个图元" + (f"，删除源对象 {len(ctx.deleted)} 个" if ctx.deleted else ""))


# ------------------------------------------------------------------ 删除（规格 45 项里没有，但演示口令 6 和 Few-shot 2 都要用，作为第 46 个函数补上）
class EraseArgs(Args):
    entity_ids: Ids
    confirmed: bool = False


@tool("erase_entities", "2d_editing", EraseArgs)
def erase_entities(ctx: Ctx, a: EraseArgs) -> dict:
    """删除图元。一次删除超过 20 个对象需要 confirmed=true（先向用户确认）；锁定图层上的图元不能删（LAYER_LOCKED）。撤销用 history_undo_redo。"""
    ents = ctx.entities(a.entity_ids, for_edit=True)
    _need(ents, "需要 entity_ids（要删除的图元）")
    if len(ents) > 20 and not a.confirmed:
        raise ToolError("CONFIRM_REQUIRED", f"一次删除 {len(ents)} 个对象，超过 20 个，需要用户确认", "先问用户，得到确认后带 confirmed=true 重新调用", count=len(ents))
    gone = {e.id: e.kind for e in ents}
    for e in ents:
        ctx.remove(e)
    return ctx.result(f"删除 {len(ents)} 个图元：{'、'.join(f'{i}({k})' for i, k in list(gone.items())[:8])}" + ("…" if len(gone) > 8 else ""), deleted_kinds=gone)


# ------------------------------------------------------------------ 21 transform_entities
class TransformArgs(Args):
    operation: Literal["move", "rotate", "scale", "stretch"]
    entity_ids: Ids
    base_point: Pt = None
    target_point: Pt = None
    displacement: Pt = None
    rotation_angle_deg: Optional[float] = None
    reference_angle: Optional[float] = None
    scale_factor: Optional[float] = Field(None, gt=0)
    reference_length: Optional[tuple[float, float]] = None
    stretch_crossing_box: Optional[list[Pt]] = None
    copy_mode: bool = False


@tool("transform_entities", "2d_editing", TransformArgs)
def transform_entities(ctx: Ctx, a: TransformArgs) -> dict:
    """移动、旋转、比例缩放和拉伸。move：displacement 或 base_point→target_point；rotate：base_point + rotation_angle_deg（逆时针为正；给 reference_angle 时 rotation_angle_deg 是目标角）；
    scale：base_point + scale_factor 或 reference_length=[旧,新]；stretch：stretch_crossing_box（完全在框内整体移动，跨框对象只移动框内顶点）+ displacement。copy_mode 保留源对象。"""
    ents = ctx.entities(a.entity_ids, for_edit=True)
    _need(ents, "需要 entity_ids")
    op = a.operation
    if op == "move":
        if a.displacement is not None:
            dv = ctx.point(a.displacement, False) if isinstance(a.displacement, str) else P(a.displacement)
            dx, dy, dz = dv
        else:
            _need(a.base_point is not None and a.target_point is not None, "移动需要 displacement，或 base_point 和 target_point", "向用户确认移动距离或目标点")
            b, t = ctx.point(a.base_point), ctx.point(a.target_point)
            dx, dy, dz = t[0] - b[0], t[1] - b[1], t[2] - b[2]
        ctx.apply_ops(modify.apply_transform(ents, Transform.translation(dx, dy, dz), a.copy_mode))
        return ctx.result(f"{'复制' if a.copy_mode else '移动'} {len(ents)} 个图元，位移 ({dx:g},{dy:g})")
    if op == "rotate":
        _need(a.base_point is not None, "旋转需要 base_point")
        base = ctx.point(a.base_point)
        if a.reference_angle is not None:
            _need(a.rotation_angle_deg is not None, "参照角度旋转需要目标角度 rotation_angle_deg")
            T = modify.rotation_for(base, None, a.reference_angle, a.rotation_angle_deg)
        else:
            _need(a.rotation_angle_deg is not None, "旋转需要 rotation_angle_deg", "向用户确认旋转角度")
            T = modify.rotation_for(base, a.rotation_angle_deg)
        ctx.apply_ops(modify.apply_transform(ents, T, a.copy_mode))
        return ctx.result(f"绕 ({base[0]:g},{base[1]:g}) 旋转 {len(ents)} 个图元")
    if op == "scale":
        _need(a.base_point is not None, "缩放需要 base_point")
        _need(a.scale_factor is not None or a.reference_length, "缩放需要 scale_factor 或 reference_length", "向用户确认比例")
        T = modify.scaling_for(ctx.point(a.base_point), a.scale_factor, a.reference_length)
        ctx.apply_ops(modify.apply_transform(ents, T, a.copy_mode))
        return ctx.result(f"缩放 {len(ents)} 个图元，比例 {T.scale:g}")
    _need(a.stretch_crossing_box and len(a.stretch_crossing_box) == 2 and a.displacement is not None, "拉伸需要 stretch_crossing_box（两点）和 displacement")
    c1, c2 = ctx.point(a.stretch_crossing_box[0], False), ctx.point(a.stretch_crossing_box[1], False)
    box = (min(c1[0], c2[0]), min(c1[1], c2[1]), max(c1[0], c2[0]), max(c1[1], c2[1]))
    dv = ctx.point(a.displacement, False) if isinstance(a.displacement, str) else P(a.displacement)
    pool = ents
    n = 0
    for e in pool:
        nd = modify.stretch_entity(e, box, dv[0], dv[1])
        if nd is not None:
            ctx.update(e, nd)
            n += 1
    if n == 0:
        raise ToolError("NOTHING_TO_STRETCH", "交叉框没有碰到所选对象的任何顶点", "把交叉框框住要移动的端点")
    return ctx.result(f"拉伸 {n} 个图元，位移 ({dv[0]:g},{dv[1]:g})")


# ------------------------------------------------------------------ 22 adjust_line_lengths
class AdjustArgs(Args):
    operation: Literal["lengthen", "trim", "extend"]
    target_entities: list[dict]
    boundary_entity_ids: Ids = None
    lengthen_mode: Literal["delta", "percent", "total"] = "delta"
    lengthen_value: Optional[float] = None
    end: Literal["start", "end", "nearest"] = "end"


@tool("adjust_line_lengths", "2d_editing", AdjustArgs)
def adjust_line_lengths(ctx: Ctx, a: AdjustArgs) -> dict:
    """拉长（delta 增量 / percent 百分比 / total 总长）、修剪、延伸。target_entities=[{entity_id, pick_point}]；boundary_entity_ids 为空表示图中所有其他对象互为边界。修剪/延伸找不到交点报 NO_INTERSECTION。"""
    _need(a.target_entities, "需要 target_entities=[{entity_id, pick_point}]")
    done = 0
    for item in a.target_entities:
        e = ctx.entity(item.get("entity_id"), for_edit=True)
        pick = ctx.point(item["pick_point"]) if item.get("pick_point") is not None else None
        if a.operation == "lengthen":
            _need(a.lengthen_value is not None, "拉长需要 lengthen_value", "向用户确认拉长量")
            ctx.update(e, modify.lengthen(e, a.lengthen_mode, a.lengthen_value, a.end, pick))
        else:
            _need(pick is not None, f"{a.operation} 需要 pick_point（点在要{'剪掉' if a.operation == 'trim' else '延伸'}的那一侧）")
            bnds = ctx.entities(a.boundary_entity_ids) if a.boundary_entity_ids else [x for x in ctx.d.entities if x.id != e.id and primitives(x)]
            if a.operation == "trim":
                ctx.apply_ops(modify.trim_entity(e, pick, bnds))
            else:
                ctx.update(e, modify.extend_entity(e, pick, bnds))
        done += 1
    return ctx.result(f"{ {'lengthen': '拉长', 'trim': '修剪', 'extend': '延伸'}[a.operation] } {done} 个对象")


# ------------------------------------------------------------------ 23 modify_corners_and_breaks
class CornerArgs(Args):
    operation: Literal["break_at_point", "break_two_points", "chamfer", "fillet"]
    entity_id_1: str
    entity_id_2: Optional[str] = None
    break_pt1: Pt = None
    break_pt2: Pt = None
    chamfer_dist1: Optional[float] = Field(None, ge=0)
    chamfer_dist2: Optional[float] = Field(None, ge=0)
    chamfer_dist: Optional[float] = Field(None, ge=0)
    chamfer_angle_deg: Optional[float] = Field(None, gt=0, lt=90)
    fillet_radius: Optional[float] = Field(None, ge=0)
    trim_mode: Literal["trim", "no_trim"] = "trim"
    apply_to_entire_polyline: bool = False
    pick_point_1: Pt = None
    pick_point_2: Pt = None


@tool("modify_corners_and_breaks", "2d_editing", CornerArgs)
def modify_corners_and_breaks(ctx: Ctx, a: CornerArgs) -> dict:
    """打断（一点处断开 / 两点之间删除）、倒角（两个距离，或距离+角度）、倒圆角（R0 表示直接闭合成角；半径过大报 RADIUS_TOO_LARGE 并给出最大值）。apply_to_entire_polyline 对整条直线段多段线的所有拐角处理。"""
    e1 = ctx.entity(a.entity_id_1, for_edit=True)
    op = a.operation
    if op in ("break_at_point", "break_two_points"):
        _need(a.break_pt1 is not None, "打断需要 break_pt1")
        p1 = ctx.point(a.break_pt1)
        p2 = ctx.point(a.break_pt2) if (op == "break_two_points" and a.break_pt2 is not None) else None
        if op == "break_two_points":
            _need(p2 is not None, "两点打断需要 break_pt2")
        ctx.apply_ops(modify.break_entity(e1, p1, p2))
        return ctx.result("在点处打断" if p2 is None else "删除两点之间的部分")
    if a.apply_to_entire_polyline or a.entity_id_2 is None:
        _need(e1.kind == "polyline", "只给一个对象时，必须是多段线并设置 apply_to_entire_polyline=true", "")
        if op == "fillet":
            _need(a.fillet_radius is not None, "倒圆角需要 fillet_radius", "向用户确认圆角半径")
            nd = modify.polyline_corners(e1, "fillet", a.fillet_radius)
            msg = f"整条多段线倒圆角 R{a.fillet_radius:g}"
        else:
            d1, d2 = _chamfer_dists(a)
            nd = modify.polyline_corners(e1, "chamfer", d1, d2)
            msg = f"整条多段线倒角 {d1:g}×{d2:g}"
        ctx.update(e1, nd, tags=[t for t in e1.tags if t not in ("rectangle", "polygon")] if op == "fillet" else e1.tags)
        return ctx.result(msg)
    e2 = ctx.entity(a.entity_id_2, for_edit=True)
    p1 = ctx.point(a.pick_point_1, False) if a.pick_point_1 is not None else None
    p2 = ctx.point(a.pick_point_2, False) if a.pick_point_2 is not None else None
    if op == "fillet":
        _need(a.fillet_radius is not None, "倒圆角需要 fillet_radius", "向用户确认圆角半径；0 表示直接成角")
        ctx.apply_ops(modify.fillet_lines(e1, e2, a.fillet_radius, p1, p2, a.trim_mode == "trim"))
        return ctx.result(f"倒圆角 R{a.fillet_radius:g}")
    d1, d2 = _chamfer_dists(a)
    ctx.apply_ops(modify.chamfer_lines(e1, e2, d1, d2, p1, p2))
    return ctx.result(f"倒角 {d1:g}×{d2:g}")


def _chamfer_dists(a: CornerArgs) -> tuple[float, float]:
    if a.chamfer_dist1 is not None:
        return a.chamfer_dist1, a.chamfer_dist2 if a.chamfer_dist2 is not None else a.chamfer_dist1
    if a.chamfer_dist is not None and a.chamfer_angle_deg is not None:
        return a.chamfer_dist, a.chamfer_dist * math.tan(math.radians(a.chamfer_angle_deg))
    raise ToolError("MISSING_ARG", "倒角需要 chamfer_dist1(+chamfer_dist2)，或 chamfer_dist + chamfer_angle_deg", "向用户确认倒角距离")


# ------------------------------------------------------------------ 24 explode_compound_entities
class ExplodeArgs(Args):
    entity_ids: Ids
    recursive: bool = False


@tool("explode_compound_entities", "2d_editing", ExplodeArgs)
def explode_compound_entities(ctx: Ctx, a: ExplodeArgs) -> dict:
    """分解复合对象：块参照、多段线（含矩形、正多边形）、标注、填充（只保留边界）、多线分解成独立图元，返回新图元 id。recursive=true 时分解到不能再分解为止。"""
    blocks = {b["name"]: b for b in ctx.d.blocks}
    todo = ctx.entities(a.entity_ids, for_edit=True)
    _need(todo, "需要 entity_ids")
    compound = ("polyline", "mline", "hatch", "dimension", "insert")
    produced: list[str] = []
    rounds = 0
    while todo and rounds < 8:
        rounds += 1
        nxt = []
        for e in todo:
            if e.kind not in compound:
                if rounds == 1:
                    raise ToolError("UNSUPPORTED", f"{e.id}（{e.kind}）不是复合对象，不能分解", "可分解：块参照、多段线/矩形/多边形、多线、标注、填充")
                continue
            before = set(ctx.created)
            ops = modify.explode(e, blocks)
            ctx.apply_ops(ops)
            new = [i for i in ctx.created if i not in before]
            produced += new
            if a.recursive:
                nxt += [ctx.d.get_entity(i) for i in new if ctx.d.get_entity(i) and ctx.d.get_entity(i).kind in compound and ctx.d.get_entity(i).kind != "polyline" or (ctx.d.get_entity(i) and ctx.d.get_entity(i).kind == "insert")]
        todo = nxt if a.recursive else []
    return ctx.result(f"分解成 {len(produced)} 个图元", new_entity_ids=produced)


# ------------------------------------------------------------------ 25 grip_edit_entity
class GripArgs(Args):
    entity_id: str
    grip_type_or_index: Any
    grip_mode: Literal["stretch", "move", "rotate", "scale", "mirror"] = "stretch"
    new_position: Pt = None
    rotation_deg: Optional[float] = None
    scale_factor: Optional[float] = Field(None, gt=0)
    copy_on_edit: bool = False


@tool("grip_edit_entity", "2d_editing", GripArgs)
def grip_edit_entity(ctx: Ctx, a: GripArgs) -> dict:
    """夹点编辑：选中图元的某个夹点（start/end/mid/center/quadrant_90/vertex_2 或索引），按模式拉伸、移动、旋转、缩放、镜像。前端拖动夹点时直接调用本函数。"""
    e = ctx.entity(a.entity_id, for_edit=True)
    g = grips.find_grip(e, a.grip_type_or_index)
    gp = P(g["coord"])
    mode = a.grip_mode
    if mode == "stretch":
        _need(a.new_position is not None, "拉伸夹点需要 new_position")
        nd = grips.stretch_grip(e, g, ctx.point(a.new_position))
        msg = f"拉伸 {e.id} 的夹点 {g['role']}"
    elif mode == "move":
        _need(a.new_position is not None, "移动需要 new_position")
        t = ctx.point(a.new_position)
        nd = transform_data(e.kind, e.data, Transform.translation(t[0] - gp[0], t[1] - gp[1]))
        msg = f"以夹点 {g['role']} 为基点移动 {e.id}"
    elif mode == "rotate":
        _need(a.rotation_deg is not None, "旋转需要 rotation_deg")
        nd = transform_data(e.kind, e.data, Transform.rotation(gp, a.rotation_deg))
        msg = f"以夹点 {g['role']} 为中心旋转 {e.id} {a.rotation_deg:g}°"
    elif mode == "scale":
        _need(a.scale_factor, "缩放需要 scale_factor")
        nd = transform_data(e.kind, e.data, Transform.scaling(gp, a.scale_factor))
        msg = f"以夹点 {g['role']} 为基点缩放 {e.id}"
    else:
        _need(a.new_position is not None, "镜像需要 new_position（镜像线的另一点）")
        nd = transform_data(e.kind, e.data, Transform.mirror(gp, ctx.point(a.new_position)))
        msg = f"以 {g['role']} 到新点为镜像线镜像 {e.id}"
    if a.copy_on_edit:
        ctx.add(e.kind, nd, tags=e.tags, src=e)
    else:
        ctx.update(e, nd)
    return ctx.result(msg)


# ------------------------------------------------------------------ 26 modify_entity_properties
class PropsArgs(Args):
    action: Literal["get", "set"] = "get"
    entity_ids: Ids
    common_props: Optional[dict] = None
    geometry_props: Optional[dict] = None


def _geometry_props(e) -> dict:
    from zhihui.cad.geom.select import key_params
    return key_params(e)


def apply_geometry_props(ctx: Ctx, e, gp: dict) -> list[str]:
    d = dict(e.data)
    k = e.kind
    done = []
    for key, v in gp.items():
        if k == "circle" and key in ("radius", "diameter", "center"):
            if key == "radius":
                d["r"] = float(v)
            elif key == "diameter":
                d["r"] = float(v) / 2
            else:
                d["c"] = pl(ctx.point(v, False))
        elif k == "arc" and key in ("radius", "center", "start_angle", "end_angle"):
            if key == "radius":
                d["r"] = float(v)
            elif key == "center":
                d["c"] = pl(ctx.point(v, False))
            else:
                d["a0" if key == "start_angle" else "a1"] = float(v) % 360
        elif k == "line" and key in ("p1", "p2", "start", "end"):
            d["p1" if key in ("p1", "start") else "p2"] = pl(ctx.point(v, False))
        elif k in ("text", "mtext") and key in ("content", "text", "height", "rotation", "insert_point", "at"):
            if key in ("content", "text"):
                d["text"] = str(v)
            elif key in ("insert_point", "at"):
                d["p"] = pl(ctx.point(v, False))
            else:
                d[key] = float(v)
        elif k == "polyline" and key in ("global_width", "width", "closed"):
            if key == "closed":
                d["closed"] = bool(v)
            else:
                d["width"] = float(v)
                for vv in d["vertices"]:
                    vv["sw"] = vv["ew"] = float(v)
        elif k == "ellipse" and key == "ratio":
            d["ratio"] = float(v)
        elif k == "insert" and key in ("rotation", "scale", "attribs"):
            d[key] = v
        elif k == "hatch" and key in ("pattern", "scale", "angle"):
            d[key] = v
        elif key in ("area", "length", "vertices"):
            raise ToolError("READ_ONLY_PROP", f"{key} 是只读特性", "")
        else:
            raise ToolError("UNSUPPORTED_PROP", f"{e.id}（{k}）没有可修改的特性 {key}", "")
        done.append(key)
    ctx.update(e, d)
    return done


@tool("modify_entity_properties", "2d_editing", PropsArgs)
def modify_entity_properties(ctx: Ctx, a: PropsArgs) -> dict:
    """特性面板：get 返回选中对象的特性（多个对象值不同显示“多种”）；set 修改 common_props（layer/color/linetype/linetype_scale/lineweight_mm/transparency）和 geometry_props（圆的 radius/diameter/center、文字的 content/height、多段线的 global_width 等）。"""
    ents = ctx.entities(a.entity_ids, for_edit=(a.action == "set"))
    _need(ents, "需要 entity_ids")
    if a.action == "get":
        keys = ("layer", "color", "linetype", "linetype_scale", "lineweight_mm", "transparency")
        common = {}
        for k in keys:
            vals = {str(getattr(e, k)) for e in ents}
            common[k] = (getattr(ents[0], k) if len(vals) == 1 else "多种")
        data: dict = {"common_props": common, "types": sorted({e.kind for e in ents}), "count": len(ents)}
        if len(ents) == 1:
            data["geometry_props"] = _geometry_props(ents[0])
        return ctx.result(f"{len(ents)} 个图元的特性", **data)
    cp = a.common_props or {}
    changed = []
    for e in ents:
        for k, v in cp.items():
            if k == "layer":
                if ctx.d.get_layer(v) is None:
                    raise ToolError("LAYER_NOT_FOUND", f"图层「{v}」不存在", f"现有：{[l.name for l in ctx.d.layers]}")
                ctx.update(e, layer=ctx.d.get_layer(v).name)
            elif k == "color":
                if v in (None, "ByLayer", "bylayer", "随层"):
                    ctx.update(e, color=None)
                else:
                    aci, tc = parse_color(v)
                    ctx.update(e, color=tc or aci)
            elif k == "linetype":
                ctx.update(e, linetype=None if v in (None, "ByLayer", "随层") else norm_linetype(v))
            elif k in ("linetype_scale", "lineweight_mm", "transparency"):
                ctx.update(e, **{k: v})
            else:
                raise ToolError("UNSUPPORTED_PROP", f"不能修改通用特性 {k}", "可改：layer/color/linetype/linetype_scale/lineweight_mm/transparency")
            changed.append(k)
        if a.geometry_props:
            changed += apply_geometry_props(ctx, e, a.geometry_props)
    return ctx.result(f"修改了 {len(ents)} 个图元的 {'、'.join(dict.fromkeys(changed)) or '（无）'}")


# ------------------------------------------------------------------ 27 match_entity_properties
class MatchArgs(Args):
    source_entity_id: str
    target_entity_ids: Ids
    property_filter: list[Literal["layer", "color", "linetype", "ltscale", "lineweight", "transparency", "textstyle", "dimstyle", "hatch"]] = Field(
        default_factory=lambda: ["layer", "color", "linetype", "ltscale", "lineweight", "transparency"])


@tool("match_entity_properties", "2d_editing", MatchArgs)
def match_entity_properties(ctx: Ctx, a: MatchArgs) -> dict:
    """特性匹配：把源对象的格式（图层、颜色、线型、线型比例、线宽、透明度、文字样式、标注样式、填充图案）复制给目标对象。"""
    src = ctx.entity(a.source_entity_id)
    tgts = ctx.entities(a.target_entity_ids, for_edit=True)
    _need(tgts, "需要 target_entity_ids")
    mp = {"layer": "layer", "color": "color", "linetype": "linetype", "ltscale": "linetype_scale", "lineweight": "lineweight_mm", "transparency": "transparency"}
    for t in tgts:
        for p in a.property_filter:
            if p in mp:
                ctx.update(t, **{mp[p]: getattr(src, mp[p])})
            elif p == "textstyle" and t.kind in ("text", "mtext") and src.kind in ("text", "mtext"):
                ctx.update(t, {**t.data, "style": src.data.get("style", "Standard")})
            elif p == "dimstyle" and t.kind == "dimension" and src.kind == "dimension":
                ctx.update(t, {**t.data, "style": src.data.get("style")})
            elif p == "hatch" and t.kind == "hatch" and src.kind == "hatch":
                ctx.update(t, {**t.data, "pattern": src.data.get("pattern"), "scale": src.data.get("scale"), "angle": src.data.get("angle")})
    return ctx.result(f"已把 {src.id} 的 {'、'.join(a.property_filter)} 匹配给 {len(tgts)} 个对象")
