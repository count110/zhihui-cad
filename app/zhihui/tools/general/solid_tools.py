"""三维建模（函数 40–42，意图类别 3d_modeling）：拉伸、旋转、三维视图。

实体用网格表示（导出 DXF 为 MESH，另可导出 STL），不生成 ACIS 3DSOLID。布尔运算：两个实体都是 z 向棱柱时精确求解；其它实体（旋转体、锥度体、扫掠体、布尔结果）需要可选依赖 manifold3d，没装报 BOOLEAN_UNSUPPORTED（见 geom/solid.py）。
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import Field

from zhihui.cad.geom import boundary, solid
from zhihui.cad.geom.entities import P, dist, primitives, sample
from zhihui.tools.core import ToolError
from .ctx import Ctx
from .registry import Args, Ids, Pt, tool

BoolOp = Literal["none", "union", "subtract", "intersect"]


def _need(cond, msg: str, hint: str = "") -> None:
    if not cond:
        raise ToolError("MISSING_ARG", msg, hint or "向用户确认这个数值，不要自己编")


def _section(ctx: Ctx, ids, pick, include_islands: bool):
    if ids:
        ents = ctx.entities(ids)
        return solid.section_from_entities(ents, include_islands), [e.id for e in ents]
    _need(pick is not None, "需要 profile_entity_ids（闭合轮廓）或 pick_internal_point（轮廓内部一点）")
    pp = ctx.point(pick)
    vis = [e for e in ctx.d.entities if e.space == ctx.d.active_space and (ctx.d.layer_of(e) is None or (ctx.d.layer_of(e).on and not ctx.d.layer_of(e).frozen))]
    res = boundary.find_closed_boundary(vis, seed_point=pp)
    if not res["is_closed"]:
        raise ToolError("BOUNDARY_NOT_CLOSED", res.get("message", "边界不闭合"), "先补上缺口，或直接指定闭合图元", gaps=res.get("gaps", []))
    from shapely.geometry import Polygon
    outer = Polygon([(p[0], p[1]) for p in res["outer_loop_points"]], [[(p[0], p[1]) for p in i["points"]] for i in res["island_loops"]] if include_islands else [])
    ids2 = list(dict.fromkeys(res["outer_loop_entity_ids"] + ([x for i in res["island_loops"] for x in i["entity_ids"]] if include_islands else [])))
    return solid._as_multi(outer), ids2


def _finish(ctx: Ctx, new: dict, op: str, target_id: Optional[str], layer: Optional[str], src_ids: list[str], what: str) -> dict:
    d = ctx.d
    if op != "none":
        _need(target_id, f"{op} 布尔运算需要 target_solid_id（被运算的实体）")
        tgt = ctx.entity(target_id, for_edit=True)
        if tgt.kind != "solid3d":
            raise ToolError("INVALID_ARGS", f"{tgt.id} 不是三维实体（{tgt.kind}）", "布尔运算的目标必须是拉伸、旋转或布尔生成的三维实体")
        res = solid.boolean(tgt.data, new, op)
        res["source_ids"] = tgt.data.get("source_ids", []) + src_ids
        ctx.update(tgt, res)
        tgt.tags = [x for x in tgt.tags if x not in ("prism", "tapered", "revolve", "sweep", "boolean", "mesh")] + [res["solid_kind"]]
        bb = solid.mesh_bbox(res["vertices"])
        return ctx.result(f"{what}后对 {tgt.id} 做了{ {'union': '并集', 'subtract': '差集', 'intersect': '交集'}[op] }，体积 {res['volume']:.6g}", solid_id=tgt.id, volume=round(res["volume"], 6), bbox3d=[round(x, 6) for x in bb])
    new["source_ids"] = src_ids
    tags = ["solid", new["solid_kind"]]
    e = ctx.add("solid3d", new, layer, tags)
    bb = solid.mesh_bbox(new["vertices"])
    ctx.notes.append("轮廓图元已保留（AutoCAD 默认会删除，本系统不删用户的原图元）")
    return ctx.result(f"{what}，生成实体 {e.id}（{new['solid_kind']}，体积 {new['volume']:.6g}，高度范围 z={bb[2]:g}…{bb[5]:g}）", solid_id=e.id, volume=round(new["volume"], 6), bbox3d=[round(x, 6) for x in bb])


# ------------------------------------------------------------------ 40 extrude_2d_to_3d
class ExtrudeArgs(Args):
    profile_entity_ids: Ids = None
    pick_internal_point: Pt = None
    height: Optional[float] = Field(None, gt=0)
    taper_angle_deg: float = Field(0.0, ge=-45, le=45)
    direction: Literal["+z", "-z", "both"] = "+z"
    path_entity_id: Optional[str] = None
    include_islands: bool = True
    boolean_op: BoolOp = "none"
    target_solid_id: Optional[str] = None
    layer: Optional[str] = None


@tool("extrude_2d_to_3d", "3d_modeling", ExtrudeArgs)
def extrude_2d_to_3d(ctx: Ctx, a: ExtrudeArgs) -> dict:
    """把二维闭合轮廓拉伸成三维实体。带孔的板用 include_islands=true（默认）一次成型，内部孤岛自动成为通孔；可带锥度、双向，或沿 path_entity_id 扫掠；boolean_op 做并/差/交（两个都是 z 向棱柱时精确求解；其它实体需要可选依赖 manifold3d，没装报 BOOLEAN_UNSUPPORTED）。高度必须来自用户。"""
    sec, ids = _section(ctx, a.profile_entity_ids, a.pick_internal_point, a.include_islands)
    if a.path_entity_id:
        pe = ctx.entity(a.path_entity_id)
        pts: list = []
        for pr in primitives(pe):
            s = sample(pr, 0.2)
            pts += s if not pts else s[1:]
        res = solid.sweep(sec, pts)
        what = f"沿 {pe.id} 扫掠"
    else:
        _need(a.height is not None, "拉伸需要高度", "向用户确认拉伸高度")
        res = solid.extrude(sec, a.height, a.direction, a.taper_angle_deg)
        what = f"拉伸 {a.height:g}" + (f"，锥度 {a.taper_angle_deg:g}°" if a.taper_angle_deg else "") + (f"（含 {sum(len(p.interiors) for p in sec.geoms)} 个孔）" if sum(len(p.interiors) for p in sec.geoms) else "")
    return _finish(ctx, res, a.boolean_op, a.target_solid_id, a.layer, ids, what)


# ------------------------------------------------------------------ 41 revolve_2d_to_3d
class RevolveArgs(Args):
    profile_entity_ids: Ids = None
    pick_internal_point: Pt = None
    axis_mode: Literal["two_points", "entity_line", "x_axis", "y_axis"] = "two_points"
    axis_points: Optional[list[Pt]] = None
    axis_entity_id: Optional[str] = None
    revolve_angle_deg: float = Field(360.0, gt=0, le=360)
    segments: int = Field(48, ge=8, le=360)
    boolean_op: BoolOp = "none"
    target_solid_id: Optional[str] = None
    layer: Optional[str] = None


@tool("revolve_2d_to_3d", "3d_modeling", RevolveArgs)
def revolve_2d_to_3d(ctx: Ctx, a: RevolveArgs) -> dict:
    """把二维闭合轮廓绕轴旋转成回转体（默认 360°）。轴：两点、一条直线、X 轴或 Y 轴；轮廓必须整体在轴的一侧。boolean_op 对目标实体做并/差/交（可选依赖 manifold3d，没装报 BOOLEAN_UNSUPPORTED）。"""
    sec, ids = _section(ctx, a.profile_entity_ids, a.pick_internal_point, True)
    if a.axis_mode == "x_axis":
        p1, p2 = (0.0, 0.0, 0.0), (1.0, 0.0, 0.0)
    elif a.axis_mode == "y_axis":
        p1, p2 = (0.0, 0.0, 0.0), (0.0, 1.0, 0.0)
    elif a.axis_mode == "entity_line":
        _need(a.axis_entity_id, "需要 axis_entity_id（作为旋转轴的直线）")
        ae = ctx.entity(a.axis_entity_id)
        if ae.kind != "line":
            raise ToolError("INVALID_ARGS", f"{ae.id} 不是直线，不能做旋转轴", "")
        p1, p2 = P(ae.data["p1"]), P(ae.data["p2"])
    else:
        _need(a.axis_points and len(a.axis_points) == 2, "两点轴需要 axis_points（两个点）")
        p1, p2 = ctx.point(a.axis_points[0]), ctx.point(a.axis_points[1])
    res = solid.revolve(sec, p1, p2, a.revolve_angle_deg, a.segments)
    return _finish(ctx, res, a.boolean_op, a.target_solid_id, a.layer, ids, f"旋转 {a.revolve_angle_deg:g}°")


# ------------------------------------------------------------------ 42 switch_3d_view_and_visual_style
STD_VIEWS = {"top": "top", "bottom": "bottom", "front": "front", "back": "back", "left": "left", "right": "right", "sw_isometric": "sw_iso", "se_isometric": "se_iso", "ne_isometric": "ne_iso",
             "nw_isometric": "nw_iso", "custom_orbit": "custom"}
STYLES = {"2d_wireframe": "2d_wireframe", "wireframe": "wireframe", "hidden": "hidden", "shaded": "shaded", "shaded_with_edges": "shaded_with_edges",
          "conceptual": "shaded", "realistic": "shaded"}


class View3DArgs(Args):
    standard_view: Optional[Literal["top", "bottom", "front", "back", "left", "right", "sw_isometric", "se_isometric", "ne_isometric", "nw_isometric", "custom_orbit"]] = None
    orbit_angles_deg: Optional[dict] = None
    visual_style: Optional[Literal["2d_wireframe", "wireframe", "hidden", "shaded", "shaded_with_edges", "conceptual", "realistic"]] = None
    projection: Optional[Literal["parallel", "perspective"]] = None


@tool("switch_3d_view_and_visual_style", "3d_modeling", View3DArgs, quiet=True)
def switch_3d_view_and_visual_style(ctx: Ctx, a: View3DArgs) -> dict:
    """切换三维视图方向（俯/仰/前/后/左/右/四个等轴测/自定义轨道）、视觉样式（二维线框/线框/消隐/着色/带边着色）和投影（平行/透视）。conceptual、realistic 按着色渲染。只改视图，不改几何，不产生新版本。"""
    v = ctx.d.view
    parts = []
    if a.standard_view:
        v.view3d = STD_VIEWS[a.standard_view]
        from zhihui.cad.render_general import ELEV_AZIM
        if a.standard_view == "custom_orbit":
            o = a.orbit_angles_deg or {}
            if "azimuth" not in o or "elevation" not in o:
                raise ToolError("MISSING_ARG", "custom_orbit 需要 orbit_angles_deg: {azimuth, elevation}", "向用户确认两个角度")
            v.orbit = {"azimuth": float(o["azimuth"]), "elevation": float(o["elevation"])}
        else:
            el, az = ELEV_AZIM[v.view3d]
            v.orbit = {"azimuth": az, "elevation": el}
        parts.append(f"视图 {a.standard_view}")
    if a.visual_style:
        v.visual_style = STYLES[a.visual_style]
        if a.visual_style in ("conceptual", "realistic"):
            ctx.notes.append(f"视觉样式 {a.visual_style} 按着色（shaded）渲染")
        parts.append(f"视觉样式 {v.visual_style}")
    if a.projection:
        v.projection = a.projection
        parts.append(f"{'平行' if a.projection == 'parallel' else '透视'}投影")
    if a.standard_view or a.visual_style or a.projection:
        ctx.d.workspace = "3d_modeling" if v.view3d != "top" else ctx.d.workspace
    return ctx.result("，".join(parts) or "没有改动", view3d=v.view3d, visual_style=v.visual_style, projection=v.projection, orbit=v.orbit,
                      solids=[e.id for e in ctx.d.entities if e.kind in ("solid3d", "mesh")])
