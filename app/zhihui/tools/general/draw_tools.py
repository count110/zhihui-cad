"""二、图形绘制（函数 11–17：点线、复杂曲线、多边形网格、圆弧椭圆、填充、坐标输入、对象捕捉）。"""
from __future__ import annotations

import math
from typing import Any, Literal, Optional

from pydantic import Field

from zhihui.cad.geom import boundary, curves, draw, snap
from zhihui.cad.geom.entities import EPS, P, dist, pl, primitives
from zhihui.cad.geom.errors import GeomError
from zhihui.tools.core import ToolError
from .ctx import Ctx
from .registry import Args, Ids, Pt, tool


def _need(cond, msg: str, hint: str = "缺少关键几何量，向用户确认后再画") -> None:
    if not cond:
        raise ToolError("MISSING_ARG", msg, hint)


def _prim(e):
    ps = primitives(e)
    if not ps:
        raise ToolError("INVALID_ARGS", f"{e.id} 不是线/圆/弧，不能作为相切对象", "")
    return ps[0]


# ------------------------------------------------------------------ 11 draw_linear_primitives
class LinearArgs(Args):
    primitive_type: Literal["point", "line", "ray", "xline"]
    points: list[Pt] = Field(default_factory=list)
    close_loop: bool = False
    xline_mode: Literal["two_points", "horizontal", "vertical", "angle", "bisect", "offset"] = "two_points"
    angle_deg: Optional[float] = None
    offset_dist: Optional[float] = Field(None, gt=0)
    reference_entity_id: Optional[str] = None
    point_style: Optional[dict] = None
    layer: Optional[str] = None


@tool("draw_linear_primitives", "2d_drafting", LinearArgs)
def draw_linear_primitives(ctx: Ctx, a: LinearArgs) -> dict:
    """绘制点、直线、射线和构造线。连续画线时每一段都是独立的 LINE；close_loop 闭合。xline_mode：two_points / horizontal / vertical / angle / bisect（顶点+两端点）/ offset（参照直线 + offset_dist，第一个点指明偏到哪一侧）。"""
    pts = [ctx.point(p) for p in a.points]
    if a.primitive_type == "point":
        _need(pts, "画点需要至少一个坐标")
        if a.point_style:
            ctx.d.point_style = {**ctx.d.point_style, **{k: a.point_style[k] for k in ("pdmode", "pdsize") if k in a.point_style}}
        for p in pts:
            ctx.add("point", {"p": pl(p)}, a.layer)
        return ctx.result(f"画了 {len(pts)} 个点")
    if a.primitive_type == "line":
        _need(len(pts) >= 2, "画直线至少需要两个点")
        pairs = list(zip(pts, pts[1:])) + ([(pts[-1], pts[0])] if a.close_loop and len(pts) > 2 else [])
        for p, q in pairs:
            if dist(p, q) < EPS:
                raise ToolError("DEGENERATE", "有一段线的两个端点重合", "")
            ctx.add("line", {"p1": pl(p), "p2": pl(q)}, a.layer)
        return ctx.result(f"画了 {len(pairs)} 条直线")
    if a.primitive_type == "ray":
        _need(len(pts) >= 2, "射线需要起点和方向上的一个点")
        for q in pts[1:]:
            L = dist(pts[0], q)
            if L < EPS:
                raise ToolError("DEGENERATE", "射线方向点与起点重合", "")
            ctx.add("ray", {"p": pl(pts[0]), "dir": [(q[0] - pts[0][0]) / L, (q[1] - pts[0][1]) / L]}, a.layer)
        return ctx.result(f"画了 {len(pts) - 1} 条射线")
    # xline
    mode = a.xline_mode
    if mode == "offset":
        _need(a.reference_entity_id and a.offset_dist and pts, "偏移构造线需要参照直线 reference_entity_id、offset_dist 和指明一侧的点", "向用户确认偏移距离")
        ref = ctx.entity(a.reference_entity_id)
        pr = _prim(ref)
        if not isinstance(pr, type(primitives(ref)[0])) or not hasattr(pr, "a"):
            raise ToolError("INVALID_ARGS", "偏移构造线的参照必须是直线", "")
        o = curves.offset_prim(pr, a.offset_dist, pts[0])
        L = o.length or 1.0
        ctx.add("xline", {"p": pl(o.a), "dir": [(o.b[0] - o.a[0]) / L, (o.b[1] - o.a[1]) / L]}, a.layer)
        return ctx.result(f"画了 1 条与 {ref.id} 相距 {a.offset_dist:g} 的构造线")
    if mode == "bisect":
        _need(len(pts) >= 3, "角平分线需要顶点和两个端点")
        ctx.add("xline", draw.xline_by_mode("bisect", pts[0], vertex_pts=(pts[1], pts[2])), a.layer)
        return ctx.result("画了 1 条角平分线")
    _need(pts, "构造线需要至少一个点")
    if mode == "two_points":
        _need(len(pts) >= 2, "两点构造线需要两个点")
        for q in pts[1:]:
            ctx.add("xline", draw.xline_by_mode("two_points", pts[0], p2=q), a.layer)
        return ctx.result(f"画了 {len(pts) - 1} 条构造线")
    if mode == "angle":
        _need(a.angle_deg is not None, "角度构造线需要 angle_deg")
    for p in pts:
        ctx.add("xline", draw.xline_by_mode(mode, p, angle_deg=a.angle_deg), a.layer)
    return ctx.result(f"画了 {len(pts)} 条{ {'horizontal': '水平', 'vertical': '垂直', 'angle': '角度'}.get(mode, '') }构造线")


# ------------------------------------------------------------------ 12 draw_complex_curves
class CurveArgs(Args):
    curve_type: Literal["mline", "lwpolyline", "spline"]
    vertices: list[Any]
    closed: bool = False
    global_width: float = Field(0.0, ge=0)
    mline_style: str = "STANDARD"
    mline_scale: float = 1.0
    mline_justification: Literal["top", "zero", "bottom"] = "zero"
    spline_method: Literal["fit", "cv"] = "fit"
    degree: int = Field(3, ge=1, le=5)
    layer: Optional[str] = None


@tool("draw_complex_curves", "2d_drafting", CurveArgs)
def draw_complex_curves(ctx: Ctx, a: CurveArgs) -> dict:
    """绘制多线（MLINE）、多段线（LWPOLYLINE，支持圆弧段 bulge 和宽度）和样条曲线（拟合点或控制点）。vertices 每项是点，或 {point, bulge, start_width, end_width}。"""
    items = []
    for v in a.vertices:
        if isinstance(v, dict) and ("point" in v or "p" in v):
            items.append((ctx.point(v.get("point", v.get("p"))), float(v.get("bulge", 0) or 0), float(v.get("start_width", 0) or 0), float(v.get("end_width", 0) or 0)))
        else:
            items.append((ctx.point(v), 0.0, 0.0, 0.0))
    if a.curve_type == "lwpolyline":
        _need(len(items) >= 2, "多段线至少需要两个顶点")
        verts = [{"p": pl(p), "bulge": b, "sw": sw or a.global_width, "ew": ew or a.global_width} for p, b, sw, ew in items]
        e = ctx.add("polyline", {"vertices": verts, "closed": a.closed, "width": a.global_width}, a.layer)
        return ctx.result(f"画了多段线 {e.id}（{len(verts)} 个顶点{'，闭合' if a.closed else ''}）")
    pts = [p for p, *_ in items]
    if a.curve_type == "mline":
        _need(len(pts) >= 2, "多线至少需要两个点")
        e = ctx.add("mline", {"points": [pl(p) for p in pts], "style": a.mline_style, "scale": a.mline_scale, "justification": a.mline_justification, "closed": a.closed}, a.layer)
        if not any(s.get("name", "").upper() == a.mline_style.upper() for s in ctx.d.mline_styles):
            ctx.d.mline_styles.append({"name": a.mline_style, "elements": [0.5, -0.5]})
        return ctx.result(f"画了多线 {e.id}（样式 {a.mline_style}，比例 {a.mline_scale:g}，对正 {a.mline_justification}）")
    _need(len(pts) >= 2, "样条至少需要两个点")
    if a.spline_method == "cv" and len(pts) < a.degree + 1:
        raise ToolError("INVALID_ARGS", f"控制点法样条阶数 {a.degree} 至少需要 {a.degree + 1} 个控制点", "增加控制点或降低 degree")
    key = "fit" if a.spline_method == "fit" else "cv"
    e = ctx.add("spline", {key: [pl(p) for p in pts], "degree": min(a.degree, len(pts) - 1), "closed": a.closed}, a.layer)
    return ctx.result(f"画了样条 {e.id}（{'拟合点' if key == 'fit' else '控制点'} {len(pts)} 个）")


# ------------------------------------------------------------------ 13 draw_polygons_and_meshes
class PolyArgs(Args):
    shape_type: Literal["rectangle", "polygon", "grid2d", "mesh"]
    corner1: Pt = None
    corner2: Pt = None
    width: Optional[float] = Field(None, gt=0)
    height: Optional[float] = Field(None, gt=0)
    rotation_deg: float = 0.0
    rect_fillet_r: float = Field(0.0, ge=0)
    rect_chamfer: Optional[tuple[float, float]] = None
    num_sides: Optional[int] = Field(None, ge=3, le=1024)
    center: Pt = None
    radius: Optional[float] = Field(None, gt=0)
    inscribed: bool = True
    edge_points: Optional[list[Pt]] = None
    rows: Optional[int] = Field(None, ge=1)
    cols: Optional[int] = Field(None, ge=1)
    cell_size: Any = None
    layer: Optional[str] = None


@tool("draw_polygons_and_meshes", "2d_drafting", PolyArgs)
def draw_polygons_and_meshes(ctx: Ctx, a: PolyArgs) -> dict:
    """绘制矩形（可圆角/倒角/旋转）、正多边形（中心+半径，或一条边）、二维网格（m×n 格线）和三维网格。矩形、正多边形导出为闭合 LWPOLYLINE，带 rectangle/polygon 标签。"""
    st = a.shape_type
    if st == "rectangle":
        _need(a.corner1 is not None, "矩形需要 corner1（一个角点）")
        c1 = ctx.point(a.corner1)
        if a.corner2 is not None:
            c2 = ctx.point(a.corner2)
        else:
            _need(a.width and a.height, "矩形需要 corner2，或同时给 width 和 height", "向用户确认长和宽")
            c2 = (c1[0] + a.width, c1[1] + a.height, c1[2])
        if a.width and a.height and a.corner2 is not None:
            ctx.warnings.append("同时给了 corner2 和 width/height，以 corner2 为准")
        made = draw.rectangle(c1, c2, a.rotation_deg, a.rect_fillet_r, a.rect_chamfer)
    elif st == "polygon":
        _need(a.num_sides, "正多边形需要 num_sides（边数）")
        if a.edge_points:
            _need(len(a.edge_points) == 2, "按边画多边形需要 2 个端点")
            made = draw.polygon_edge(a.num_sides, ctx.point(a.edge_points[0]), ctx.point(a.edge_points[1]))
        else:
            _need(a.center is not None and a.radius, "正多边形需要 center 和 radius（或 edge_points）", "向用户确认中心和半径")
            made = draw.polygon_center(a.num_sides, ctx.point(a.center), a.radius, a.inscribed, a.rotation_deg)
    else:
        _need(a.rows and a.cols and a.cell_size is not None, "网格需要 rows、cols 和 cell_size", "向用户确认行数、列数和单元尺寸")
        cs = (float(a.cell_size), float(a.cell_size)) if isinstance(a.cell_size, (int, float)) else tuple(float(x) for x in a.cell_size)
        origin = ctx.point(a.corner1) if a.corner1 is not None else (0.0, 0.0, 0.0)
        made = (draw.grid2d if st == "grid2d" else draw.mesh_grid)(origin, a.rows, a.cols, cs)
    for m in made:
        ctx.add(m["kind"], m["data"], a.layer, m["tags"])
    desc = {"rectangle": "矩形", "polygon": f"正{a.num_sides}边形", "grid2d": f"{a.rows}×{a.cols} 网格", "mesh": f"{a.rows}×{a.cols} 网格面"}[st]
    return ctx.result(f"画了{desc}（{len(made)} 个图元：{','.join(ctx.created[:4])}{'…' if len(ctx.created) > 4 else ''}）")


# ------------------------------------------------------------------ 14 draw_circular_primitives
class CircularArgs(Args):
    curve_type: Literal["circle", "arc", "ellipse", "ellipse_arc"]
    method: Optional[Literal["center_radius", "center_diameter", "2p", "3p", "ttr", "ttt", "start_center_end", "start_center_angle", "start_end_radius", "center_axes", "axis_end"]] = None
    center: Pt = None
    radius: Optional[float] = Field(None, gt=0)
    diameter: Optional[float] = Field(None, gt=0)
    points: Optional[list[Pt]] = None
    start_pt: Pt = None
    end_pt: Pt = None
    major_axis_vec: Optional[Any] = None
    axis_ratio: Optional[float] = Field(None, gt=0, le=1)
    start_angle_deg: Optional[float] = None
    end_angle_deg: Optional[float] = None
    included_angle_deg: Optional[float] = None
    tangent_entities: Ids = None
    layer: Optional[str] = None


@tool("draw_circular_primitives", "2d_drafting", CircularArgs)
def draw_circular_primitives(ctx: Ctx, a: CircularArgs) -> dict:
    """绘制圆（圆心半径/圆心直径/2P/3P/相切相切半径 TTR/三相切 TTT）、圆弧（3P、起点圆心终点、起点圆心角度、起点终点半径）、椭圆和椭圆弧。radius 与 diameter 同时给时必须一致，否则 PARAM_CONFLICT。"""
    r = a.radius
    if a.radius is not None and a.diameter is not None and abs(2 * a.radius - a.diameter) > 1e-9:
        raise ToolError("PARAM_CONFLICT", f"radius={a.radius:g} 与 diameter={a.diameter:g} 不一致", "只保留其中一个，或让两者满足 diameter = 2 × radius")
    if r is None and a.diameter is not None:
        r = a.diameter / 2
    pts = [ctx.point(p) for p in (a.points or [])]
    tans = ctx.entities(a.tangent_entities)
    ct = a.curve_type
    m = a.method
    if ct == "circle":
        if m is None:
            m = ("ttt" if len(tans) == 3 else "ttr" if len(tans) == 2 else "3p" if len(pts) == 3 else "2p" if len(pts) == 2 else "center_diameter" if a.diameter is not None else "center_radius")
        if m in ("center_radius", "center_diameter"):
            _need(a.center is not None and r, "画圆需要圆心和半径（或直径）", "向用户确认圆心和半径/直径")
            made = draw.circle(ctx.point(a.center), r)
        elif m == "2p":
            _need(len(pts) == 2, "两点圆需要 2 个点")
            made = draw.circle_2p(pts[0], pts[1])
        elif m == "3p":
            _need(len(pts) == 3, "三点圆需要 3 个点")
            made = draw.circle_3p(*pts)
        elif m == "ttr":
            _need(len(tans) == 2 and r, "TTR 需要两个相切对象和半径", "向用户确认半径")
            made = draw.circle_ttr(_prim(tans[0]), _prim(tans[1]), r, pts[0] if pts else None)
        elif m == "ttt":
            _need(len(tans) == 3, "TTT 需要三个相切对象")
            made = draw.circle_ttt(_prim(tans[0]), _prim(tans[1]), _prim(tans[2]), pts[0] if pts else None)
        else:
            raise ToolError("INVALID_ARGS", f"画圆不支持方法 {m}", "")
        e = ctx.add(made["kind"], made["data"], a.layer, made["tags"])
        return ctx.result(f"画了圆 {e.id}：圆心({e.data['c'][0]:g},{e.data['c'][1]:g}) ⌀{2 * e.data['r']:g}")
    if ct == "arc":
        if m is None:
            m = "3p" if len(pts) == 3 else "start_end_radius" if (a.start_pt is not None and a.end_pt is not None and r) else "start_center_angle" if a.included_angle_deg is not None else "start_center_end"
        if m == "3p":
            _need(len(pts) == 3, "三点圆弧需要 3 个点")
            made = draw.arc_3p(*pts)
        elif m == "start_center_end":
            _need(a.start_pt is not None and a.center is not None and a.end_pt is not None, "需要起点、圆心、终点")
            made = draw.arc_start_center_end(ctx.point(a.start_pt), ctx.point(a.center), ctx.point(a.end_pt))
        elif m == "start_center_angle":
            _need(a.start_pt is not None and a.center is not None and a.included_angle_deg is not None, "需要起点、圆心和包含角")
            made = draw.arc_start_center_angle(ctx.point(a.start_pt), ctx.point(a.center), a.included_angle_deg)
        elif m == "start_end_radius":
            _need(a.start_pt is not None and a.end_pt is not None and r, "需要起点、终点和半径", "向用户确认半径")
            made = draw.arc_start_end_radius(ctx.point(a.start_pt), ctx.point(a.end_pt), r)
        else:
            raise ToolError("INVALID_ARGS", f"画圆弧不支持方法 {m}", "")
        e = ctx.add(made["kind"], made["data"], a.layer, made["tags"])
        return ctx.result(f"画了圆弧 {e.id}：圆心({e.data['c'][0]:g},{e.data['c'][1]:g}) R{e.data['r']:g} {e.data['a0']:g}°→{e.data['a1']:g}°")
    # ellipse / ellipse_arc
    if m is None:
        m = "axis_end" if len(pts) == 2 else "center_axes"
    if m == "axis_end":
        _need(len(pts) == 2 and a.axis_ratio is not None or (len(pts) == 2 and r), "轴端点椭圆需要两个轴端点和另一半轴长（radius）或 axis_ratio")
        other = r if r else dist(pts[0], pts[1]) / 2 * a.axis_ratio
        made = draw.ellipse_axis_end(pts[0], pts[1], other)
    else:
        _need(a.center is not None and a.major_axis_vec is not None and a.axis_ratio, "椭圆需要 center、major_axis_vec 和 axis_ratio", "向用户确认长轴和轴比")
        mv = ctx.point(a.major_axis_vec, update_last=False) if isinstance(a.major_axis_vec, str) else P(a.major_axis_vec)
        c = ctx.point(a.center)
        vec = (mv[0] - c[0], mv[1] - c[1]) if isinstance(a.major_axis_vec, str) else (mv[0], mv[1])
        t0, t1 = (a.start_angle_deg or 0.0, a.end_angle_deg if a.end_angle_deg is not None else 360.0) if ct == "ellipse_arc" else (0.0, 360.0)
        made = draw.ellipse_center_axes(c, vec, a.axis_ratio, t0, t1)
    e = ctx.add(made["kind"], made["data"], a.layer, made["tags"])
    return ctx.result(f"画了{'椭圆弧' if ct == 'ellipse_arc' else '椭圆'} {e.id}")


# ------------------------------------------------------------------ 15 create_hatch
class HatchArgs(Args):
    boundary_mode: Literal["entity_ids", "pick_internal_point", "custom_loop"]
    entity_ids: Ids = None
    pick_point: Pt = None
    loop_points: Optional[list[Pt]] = None
    pattern_name: str = "ANSI31"
    scale: float = Field(1.0, gt=0)
    angle_deg: float = 0.0
    associative: bool = True
    island_style: Literal["normal", "outer", "ignore"] = "normal"
    gradient: Optional[dict] = None
    layer: Optional[str] = None


def _hatch_layer(ctx: Ctx, layer: str | None) -> str | None:
    if layer:
        return layer
    for name in ("剖面线", "Hatch", "填充"):
        if ctx.d.get_layer(name):
            return name
    return None


@tool("create_hatch", "2d_drafting", HatchArgs)
def create_hatch(ctx: Ctx, a: HatchArgs) -> dict:
    """用图案填充表达剖切区域。边界：entity_ids（闭合图元，内含的闭合图元自动成为孤岛）、pick_internal_point（拾取内部点追踪边界）或 custom_loop。不闭合时报 BOUNDARY_NOT_CLOSED 并给出缺口坐标。默认 ANSI31、比例 1。"""
    layer = _hatch_layer(ctx, a.layer)
    if a.boundary_mode == "custom_loop":
        _need(a.loop_points and len(a.loop_points) >= 3, "自定义边界至少需要 3 个点")
        loops = [[pl(ctx.point(p)) for p in a.loop_points]]
        src: list[str] = []
        outer_area = None
    else:
        if a.boundary_mode == "entity_ids":
            _need(a.entity_ids, "按图元填充需要 entity_ids")
            ents = ctx.entities(a.entity_ids)
            res = boundary.find_closed_boundary(ents, candidate_ids=[e.id for e in ents])
        else:
            _need(a.pick_point is not None, "拾取点填充需要 pick_point（边界内部的一点）")
            pp = ctx.point(a.pick_point)
            vis = [e for e in ctx.d.entities if (ctx.d.layer_of(e) is None or (ctx.d.layer_of(e).on and not ctx.d.layer_of(e).frozen))]
            res = boundary.find_closed_boundary(vis, seed_point=pp)
        if not res["is_closed"]:
            raise ToolError("BOUNDARY_NOT_CLOSED", res.get("message", "边界不闭合"), "先补上缺口再填充，或改用 custom_loop", gaps=res.get("gaps", []))
        loops = [res["outer_loop_points"]]
        if a.island_style == "normal":
            loops += [i["points"] for i in res["island_loops"]]
        src = list(dict.fromkeys(res["outer_loop_entity_ids"] + [i for il in res["island_loops"] for i in il["entity_ids"]])) if a.island_style != "ignore" else res["outer_loop_entity_ids"]
        outer_area = res["enclosed_area"]
    data = {"pattern": a.pattern_name, "scale": a.scale, "angle": a.angle_deg, "loops": loops, "island_style": a.island_style, "associative": a.associative and a.boundary_mode != "custom_loop",
            "source_ids": src, "gradient": a.gradient, "pick": pl(ctx.point(a.pick_point, False)) if a.boundary_mode == "pick_internal_point" and a.pick_point is not None else None}
    e = ctx.add("hatch", data, layer)
    if a.pattern_name == "ANSI31" and a.scale == 1.0 and a.angle_deg == 0.0:
        ctx.notes.append("填充使用默认值：图案 ANSI31、比例 1、角度 0")
    return ctx.result(f"画了填充 {e.id}（{a.pattern_name}，{len(loops)} 个边界环" + (f"，面积 {outer_area:.6g}" if outer_area else "") + "）", hatch_id=e.id)


def refresh_hatches(d) -> list[str]:
    """关联填充：边界图元改动后重新求边界；求不出来就标记 disassociated。返回改动的填充 id。"""
    changed = []
    for h in d.entities:
        if h.kind != "hatch" or not h.data.get("associative") or not h.data.get("source_ids"):
            continue
        srcs = [e for e in d.entities if e.id in h.data["source_ids"]]
        if len(srcs) != len(h.data["source_ids"]):
            if "disassociated" not in h.tags:
                h.tags.append("disassociated")
                changed.append(h.id)
            continue
        res = boundary.find_closed_boundary(srcs, candidate_ids=[e.id for e in srcs])
        if not res["is_closed"]:
            if "disassociated" not in h.tags:
                h.tags.append("disassociated")
                changed.append(h.id)
            continue
        loops = [res["outer_loop_points"]] + ([i["points"] for i in res["island_loops"]] if h.data.get("island_style") == "normal" else [])
        if loops != h.data.get("loops"):
            h.data = {**h.data, "loops": loops}
            changed.append(h.id)
    return changed


# ------------------------------------------------------------------ 16 parse_coordinate_input
class ParseCoordArgs(Args):
    coord_expressions: list[Any]
    last_point: Pt = None
    coordinate_system: Literal["WCS", "UCS"] = "WCS"


@tool("parse_coordinate_input", "precision_snap", ParseCoordArgs, quiet=True)
def parse_coordinate_input(ctx: Ctx, a: ParseCoordArgs) -> dict:
    """解析键盘坐标输入：绝对/相对直角和极坐标（100,50 / @30,0 / 80<30 / @50<45）、方向加距离（向右100）、图元特征点（C8.center）、交点/垂足/切点等（int(L1,L2)）。相对坐标依次以前一个点为基准；成功后更新 session.last_point。"""
    if a.last_point is not None:
        ctx.d.session.last_point = P(a.last_point) if not isinstance(a.last_point, str) else ctx.point(a.last_point, False)
    if a.coordinate_system == "UCS":
        ctx.warnings.append("尚未支持用户坐标系，按世界坐标系 WCS 处理")
    out = []
    for ex in a.coord_expressions:
        p = ctx.point(ex)
        out.append({"expression": ex, "point": [round(v, 9) for v in p]})
    return ctx.result("解析 " + "；".join(f"{o['expression']} → ({o['point'][0]:g},{o['point'][1]:g},{o['point'][2]:g})" for o in out), points=out)


# ------------------------------------------------------------------ 17 resolve_osnap_point
class OsnapPointArgs(Args):
    snap_mode: Literal["endpoint", "midpoint", "center", "geometric_center", "quadrant", "intersection", "apparent_intersection", "perpendicular", "tangent", "nearest", "node", "insertion", "extension", "m2p", "otrack_intersection"]
    target_entity_ids: Ids = None
    reference_point: Pt = None
    hint_region: Optional[str] = None
    quadrant_angle: Optional[float] = None
    extension_distance: Optional[float] = None


@tool("resolve_osnap_point", "precision_snap", OsnapPointArgs, readonly=True)
def resolve_osnap_point(ctx: Ctx, a: OsnapPointArgs) -> dict:
    """利用对象捕捉精确定位特殊点（端点、中点、圆心、几何中心、象限点、交点、延伸交点、垂足、切点、最近点、节点、插入点、延伸点、两点中点、对象追踪交点（水平/竖直追踪）；插入点只对块参照/文字有效）。有多个候选时按 hint_region（left/right/top/bottom/bottom-left…）或 reference_point 消歧，仍无法唯一则返回候选。"""
    ents = ctx.entities(a.target_entity_ids)
    _need(ents, "捕捉需要 target_entity_ids")
    ref = ctx.point(a.reference_point, False) if a.reference_point is not None else None
    mode = a.snap_mode
    if mode in ("intersection", "apparent_intersection"):
        _need(len(ents) >= 2, "交点需要两个图元")
        cands = snap.intersections(ents, apparent=(mode == "apparent_intersection"))
    elif mode == "otrack_intersection":
        _need(len(ents) == 2, "对象追踪交点需要两个图元：取各自特征点沿水平/竖直方向追踪线的交点")
        cands = snap.otrack_intersections(ents[0], ents[1])
    elif mode == "m2p":
        _need(len(ents) == 2, "两点中点需要两个图元（取各自几何中心）")
        c = [snap.geometric_center(e) for e in ents]
        cands = [{"type": "m2p", "coord": [(c[0][0] + c[1][0]) / 2, (c[0][1] + c[1][1]) / 2, 0.0], "entity_id": ",".join(e.id for e in ents)}]
    elif mode == "extension":
        d = a.extension_distance or 0.0
        cands = [{"type": "extension", "coord": list(snap.extension_point(e, d)), "entity_id": e.id} for e in ents if snap.extension_point(e, d)]
    else:
        cands = [c for e in ents for c in snap.entity_snap_points(e, [mode], ref)]
        if mode == "quadrant" and a.quadrant_angle is not None:
            cands = [c for c in cands if abs((c.get("angle", -1) - a.quadrant_angle) % 360) < 1e-6]
    if not cands:
        raise ToolError("NO_SNAP_POINT", f"在 {','.join(e.id for e in ents)} 上没有 {mode} 捕捉点", "换一种捕捉方式，或检查图元类型")
    box = None
    from zhihui.cad.geom.select import drawing_box
    box = drawing_box(ctx.d)
    chosen, allc = snap.choose(cands, ref, a.hint_region, box)
    if chosen is None:
        raise ToolError("AMBIGUOUS", f"{mode} 有 {len(allc)} 个候选，无法唯一确定", "给出 hint_region（如 left/right/top/bottom）或 reference_point，或让用户选择", candidates=allc[:12])
    return ctx.result(f"{mode} 点 ({chosen['coord'][0]:g},{chosen['coord'][1]:g},{chosen['coord'][2]:g})", point=chosen["coord"], snap_type=chosen["type"], entity_id=chosen["entity_id"], candidates=len(allc))
