"""视图与选择（函数 18、19）和 6 个只读感知函数（Recognizer 用）。"""
from __future__ import annotations

import math
from typing import Any, Literal, Optional

from pydantic import Field

from zhihui.cad.geom import boundary, select, snap
from zhihui.cad.geom.entities import P, dist, entity_bbox, entity_points, primitives
from zhihui.tools.core import ToolError
from .ctx import Ctx
from .registry import Args, Ids, Pt, tool
from . import draw_tools  # noqa: F401  (确保绘图函数先注册)


# ------------------------------------------------------------------ 18 control_viewport_view
class ViewArgs(Args):
    action: Literal["zoom_in", "zoom_out", "zoom_scale", "zoom_window", "zoom_extents", "zoom_all", "zoom_object", "zoom_previous", "pan", "save_view", "restore_view"]
    scale_factor: Optional[float] = Field(None, gt=0)
    window_min: Pt = None
    window_max: Pt = None
    target_entity_ids: Ids = None
    pan_delta: Pt = None
    direction: Optional[str] = None
    view_name: Optional[str] = None


_DIR = {"left": (-1, 0), "right": (1, 0), "up": (0, 1), "down": (0, -1), "向左": (-1, 0), "向右": (1, 0), "向上": (0, 1), "向下": (0, -1), "左": (-1, 0), "右": (1, 0), "上": (0, 1), "下": (0, -1)}


def _fit(v, box, margin: float = 1.08) -> None:
    w, h = max(box[2] - box[0], 1e-6), max(box[3] - box[1], 1e-6)
    v.center = ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
    v.height = max(h, w * 297 / 420) * margin


@tool("control_viewport_view", "view_select", ViewArgs, quiet=True)
def control_viewport_view(ctx: Ctx, a: ViewArgs) -> dict:
    """实时缩放、平移和范围显示：zoom_in/out/scale/window/extents/all/object/previous，pan（pan_delta 或方向词），save_view/restore_view。只改视图，不改几何，不产生新版本。"""
    d, v = ctx.d, ctx.d.view
    snapshot = {"center": list(v.center), "height": v.height}
    act = a.action
    if act in ("zoom_in", "zoom_out", "zoom_scale"):
        k = a.scale_factor or (2.0 if act == "zoom_in" else 0.5 if act == "zoom_out" else None)
        if k is None:
            raise ToolError("MISSING_ARG", "zoom_scale 需要 scale_factor", "")
        v.height = v.height / k
    elif act == "zoom_window":
        if a.window_min is None or a.window_max is None:
            raise ToolError("MISSING_ARG", "窗口缩放需要 window_min 和 window_max", "")
        p, q = ctx.point(a.window_min, False), ctx.point(a.window_max, False)
        _fit(v, (min(p[0], q[0]), min(p[1], q[1]), max(p[0], q[0]), max(p[1], q[1])), 1.0)
    elif act in ("zoom_extents", "zoom_all"):
        box = ctx.d.bbox() if act == "zoom_extents" else None
        lim = d.limits
        if act == "zoom_all" or box is None:
            box = (lim[0], lim[1], lim[2], lim[3]) if box is None else (min(box[0], lim[0]), min(box[1], lim[1]), max(box[2], lim[2]), max(box[3], lim[3]))
        _fit(v, box)
    elif act == "zoom_object":
        ents = ctx.entities(a.target_entity_ids)
        if not ents:
            raise ToolError("MISSING_ARG", "zoom_object 需要 target_entity_ids", "")
        boxes = [b for b in (entity_bbox(e) for e in ents) if b]
        _fit(v, (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)), 1.3)
    elif act == "zoom_previous":
        if not d.session.prev_views:
            raise ToolError("NO_PREVIOUS_VIEW", "没有上一个视图", "")
        prev = d.session.prev_views.pop()
        v.center, v.height = tuple(prev["center"]), prev["height"]
        return ctx.result("已恢复上一视图", center=list(v.center), height=v.height)
    elif act == "pan":
        if a.pan_delta is not None:
            dx, dy = (P(a.pan_delta)[0], P(a.pan_delta)[1]) if not isinstance(a.pan_delta, str) else ctx.point(a.pan_delta, False)[:2]
        elif a.direction in _DIR:
            ux, uy = _DIR[a.direction]
            dx, dy = ux * v.height * 0.25, uy * v.height * 0.25
        else:
            raise ToolError("MISSING_ARG", "平移需要 pan_delta 或方向词（left/right/up/down）", "")
        v.center = (v.center[0] + dx, v.center[1] + dy)
    elif act == "save_view":
        if not a.view_name:
            raise ToolError("MISSING_ARG", "save_view 需要 view_name", "")
        v.named_views[a.view_name] = {"center": list(v.center), "height": v.height}
        return ctx.result(f"已保存视图「{a.view_name}」")
    elif act == "restore_view":
        nv = v.named_views.get(a.view_name or "")
        if nv is None:
            raise ToolError("VIEW_NOT_FOUND", f"没有保存的视图「{a.view_name}」", f"已保存：{list(v.named_views)}")
        v.center, v.height = tuple(nv["center"]), nv["height"]
    if act != "zoom_previous":
        d.session.prev_views = (d.session.prev_views + [snapshot])[-20:]
    return ctx.result(f"视图：中心({v.center[0]:g},{v.center[1]:g})，高度 {v.height:g}", center=list(v.center), height=v.height)


# ------------------------------------------------------------------ 19 select_entities
class FilterModel(Args):
    entity_ids: Ids = None                    # 只在这些图元里找（识别器核对显式编号用）
    entity_types: Optional[list[str]] = None
    layer: Optional[str] = None
    color: Any = None
    linetype: Optional[str] = None
    lineweight_mm: Optional[float] = None
    tags: Optional[list[str]] = None
    where: Optional[list[dict]] = None
    spatial_region: Any = None
    window: Optional[list[Pt]] = None
    crossing: Optional[list[Pt]] = None
    fence: Optional[list[Pt]] = None
    near: Optional[dict] = None
    relation: Optional[dict] = None
    sort_by: Any = None
    limit: Optional[int] = Field(None, ge=1)


_FILTER_ALIASES = {"type": "entity_types", "types": "entity_types", "kind": "entity_types", "kinds": "entity_types", "entity_type": "entity_types",
                   "layers": "layer", "layer_name": "layer", "colour": "color", "line_type": "linetype", "line_weight": "lineweight_mm",
                   "lineweight": "lineweight_mm", "region": "spatial_region", "position": "spatial_region", "ids": "entity_ids"}


def parse_filter(raw: dict) -> dict:
    """filter_criteria 由模型写，键名可能不是精确的 select.query 参数名：先认常见别名，再用 FilterModel 校验，
    不认识的键给出明确的 BAD_ARGS（带可用键名），不再让 TypeError 漏成“内部错误”。"""
    fixed: dict = {}
    for k, v in (raw or {}).items():
        k2 = _FILTER_ALIASES.get(k, k)
        if k2 == "entity_types" and isinstance(v, str):
            v = [v]
        if k2 == "layer" and isinstance(v, list) and len(v) == 1:
            v = v[0]
        fixed.setdefault(k2, v)
    try:
        return FilterModel.model_validate(fixed).model_dump(exclude_none=True)
    except Exception as ex:                                          # pydantic.ValidationError
        raise ToolError("BAD_ARGS", f"filter_criteria 里有不认识或不合格的条件：{str(ex)[:200]}",
                        "可用条件：" + "、".join(FilterModel.model_fields)) from ex


def _resolve_filter_points(ctx: Ctx, f: dict) -> dict:
    out = dict(f)
    for key in ("window", "crossing"):
        if out.get(key):
            out[key] = [ctx.point(p, False) for p in out[key]]
    if out.get("fence"):
        out["fence"] = [ctx.point(p, False) for p in out["fence"]]
    if out.get("near"):
        out["near"] = {**out["near"], "point": ctx.point(out["near"]["point"], False)}
    if out.get("color") is not None:
        from .env_tools import parse_color
        aci, tc = parse_color(out["color"])
        out["color"] = tc or aci
    if isinstance(out.get("sort_by"), dict) and out["sort_by"].get("distance_to") is not None:
        out["sort_by"] = {"distance_to": ctx.point(out["sort_by"]["distance_to"], False)}
    return out


class SelectArgs(Args):
    selection_mode: Literal["by_ids", "point_pick", "window", "crossing", "window_polygon", "crossing_polygon", "fence", "all", "filter", "previous", "last"]
    entity_ids: Ids = None
    pick_point: Pt = None
    box_corner1: Pt = None
    box_corner2: Pt = None
    polygon_points: Optional[list[Pt]] = None
    filter_criteria: Optional[dict] = None
    add_to_selection: bool = False
    remove_from_selection: bool = False
    pick_tolerance: float = Field(2.0, gt=0)


@tool("select_entities", "view_select", SelectArgs, quiet=True)
def select_entities(ctx: Ctx, a: SelectArgs) -> dict:
    """选择单个或批量对象：by_ids、point_pick、window（完全在框内）、crossing（框内或相交）、多边形窗口/交叉、fence（栅栏线）、all、filter（条件同 query_entities_by_filter）、previous、last。结果写入 session.selection，前端高亮。"""
    d = ctx.d
    mode = a.selection_mode
    unlocked = [e for e in d.entities if (d.layer_of(e) is None or (d.layer_of(e).on and not d.layer_of(e).frozen))]
    ids: list[str]
    if mode == "by_ids":
        ids = [e.id for e in ctx.entities(a.entity_ids)]
    elif mode == "point_pick":
        if a.pick_point is None:
            raise ToolError("MISSING_ARG", "点选需要 pick_point", "")
        p = ctx.point(a.pick_point, False)
        near = [(min(dist(__import__("zhihui.cad.geom.curves", fromlist=["closest"]).closest(pr, p), p) for pr in primitives(e)), e) for e in unlocked if primitives(e)]
        near = [(dd, e) for dd, e in near if dd <= a.pick_tolerance]
        ids = [min(near, key=lambda t: t[0])[1].id] if near else []
    elif mode in ("window", "crossing"):
        if a.box_corner1 is None or a.box_corner2 is None:
            raise ToolError("MISSING_ARG", f"{mode} 需要 box_corner1 和 box_corner2", "")
        c1, c2 = ctx.point(a.box_corner1, False), ctx.point(a.box_corner2, False)
        r = select.query(d, **{mode: [c1, c2]}, candidates={e.id for e in unlocked})
        ids = [c["entity_id"] for c in r["candidates"]]
    elif mode in ("window_polygon", "crossing_polygon"):
        pts = [ctx.point(p, False) for p in (a.polygon_points or [])]
        if len(pts) < 3:
            raise ToolError("MISSING_ARG", "多边形选择至少需要 3 个点", "")
        fn = select._inside_poly if mode == "window_polygon" else select._crosses_poly
        ids = [e.id for e in unlocked if fn(e, pts)]
    elif mode == "fence":
        pts = [ctx.point(p, False) for p in (a.polygon_points or [])]
        if len(pts) < 2:
            raise ToolError("MISSING_ARG", "栅栏选择至少需要 2 个点", "")
        ids = [e.id for e in unlocked if select._fence_hit(e, pts)]
    elif mode == "all":
        ids = [e.id for e in unlocked]
    elif mode == "filter":
        crit = _resolve_filter_points(ctx, parse_filter(a.filter_criteria or {}))
        pool = {e.id for e in unlocked}
        given = crit.pop("entity_ids", None)
        if given:
            pool &= {ctx.entity(i).id for i in given}
        ids = [c["entity_id"] for c in select.query(d, **crit, candidates=pool)["candidates"]]
    elif mode == "previous":
        ids = list(d.session.selection)
    else:  # last
        items = [r for r in d.session.recent_entities if r["op"] == "created"]
        ids = [items[0]["entity_id"]] if items else []
    cur = list(d.session.selection)
    if a.add_to_selection:
        ids = cur + [i for i in ids if i not in cur]
    elif a.remove_from_selection:
        ids = [i for i in cur if i not in ids]
    d.session.selection = ids
    return ctx.result(f"选中 {len(ids)} 个图元" + (f"：{'、'.join(ids[:8])}{'…' if len(ids) > 8 else ''}" if ids else ""), selection=ids, count=len(ids))


# ------------------------------------------------------------------ 感知函数（只读）
class QueryArgs(FilterModel):
    pass


@tool("query_entities_by_filter", "perception", QueryArgs, readonly=True)
def query_entities_by_filter(ctx: Ctx, a: QueryArgs) -> dict:
    """按类型、图层、颜色、线型、标签、尺寸条件（length/radius/diameter/area/angle/text/width/height）、方位（left/right/top/bottom/center/top_left…）、窗口/交叉/栅栏、附近、与某图元的关系（intersects/parallel/perpendicular/tangent/concentric/inside/touches）筛选图元，并排序。返回候选及得分和理由。"""
    crit = _resolve_filter_points(ctx, a.model_dump(exclude_none=True))
    ids = crit.pop("entity_ids", None)
    if ids:
        crit["candidates"] = [ctx.entity(i).id for i in ids]
    r = select.query(ctx.d, **crit)
    return ctx.result(f"找到 {r['total']} 个图元", **r)


class ContextRefArgs(Args):
    reference_type: Literal["last_created", "last_modified", "last_selected", "nth_from_last"]
    entity_type_filter: Optional[str] = None
    offset_index: int = Field(0, ge=0)
    count: int = Field(1, ge=1)


@tool("resolve_contextual_reference", "perception", ContextRefArgs, readonly=True)
def resolve_contextual_reference(ctx: Ctx, a: ContextRefArgs) -> dict:
    """把“刚才画的”“上一个”“倒数第二个圆”“它”这类指代解析成图元 id：last_created / last_modified / last_selected / nth_from_last（配合类型过滤和 offset_index）。"""
    s = ctx.d.session
    alive = {e.id: e for e in ctx.d.entities}
    if a.reference_type == "last_selected":
        ids = [i for i in s.selection if i in alive]
    else:
        op = {"last_created": "created", "last_modified": "modified"}.get(a.reference_type)
        items = [r for r in s.recent_entities if (op is None or r["op"] == op) and r["entity_id"] in alive]
        if op is None:   # nth_from_last：按创建顺序（栈顶=最近）
            items = [r for r in s.recent_entities if r["op"] == "created" and r["entity_id"] in alive]
        if a.entity_type_filter:
            kind, tag = select.TYPE_ALIASES.get(a.entity_type_filter, (a.entity_type_filter, None))
            items = [r for r in items if r["type"] == kind and (tag is None or tag in r.get("tags", []))]
        seen, ids = set(), []
        for r in items:
            if r["entity_id"] not in seen:
                seen.add(r["entity_id"])
                ids.append(r["entity_id"])
        ids = ids[a.offset_index:a.offset_index + a.count]
    if not ids:
        raise ToolError("NOT_FOUND", "上下文里没有符合的图元", "图纸里还没有画过东西，或最近的图元类型不匹配；改用 query_entities_by_filter")
    return ctx.result(f"解析为 {'、'.join(ids)}", entity_ids=ids, entities=[select.describe(alive[i]) for i in ids])


class SnapPointsArgs(Args):
    entity_ids: Ids
    snap_types: list[str] = Field(default_factory=lambda: ["endpoint", "midpoint", "center", "geometric_center", "quadrant"])
    reference_point: Pt = None
    spatial_hint: Optional[str] = None


@tool("get_entity_snap_points", "perception", SnapPointsArgs, readonly=True)
def get_entity_snap_points(ctx: Ctx, a: SnapPointsArgs) -> dict:
    """求图元的捕捉点（端点、中点、圆心、几何中心、象限点、节点、插入点、垂足、切点、最近点、交点）；spatial_hint（left/right/top/bottom/bottom-left…）用来在多个候选里挑一个。"""
    ents = ctx.entities(a.entity_ids)
    ref = ctx.point(a.reference_point, False) if a.reference_point is not None else None
    pts: list[dict] = []
    for e in ents:
        pts += snap.entity_snap_points(e, [t for t in a.snap_types if t not in ("intersection", "apparent_intersection")], ref)
    if any(t in a.snap_types for t in ("intersection", "apparent_intersection")) and len(ents) >= 2:
        pts += snap.intersections(ents, apparent="apparent_intersection" in a.snap_types)
    if a.spatial_hint:
        chosen, _ = snap.choose(pts, ref, a.spatial_hint, select.drawing_box(ctx.d))
        if chosen is not None:
            pts = [chosen]
    return ctx.result(f"{len(pts)} 个捕捉点", snap_points=pts)


class ClosedArgs(Args):
    seed_point: Pt = None
    candidate_entity_ids: Ids = None
    tolerance: Optional[float] = Field(None, gt=0)


@tool("find_closed_boundary", "perception", ClosedArgs, readonly=True)
def find_closed_boundary(ctx: Ctx, a: ClosedArgs) -> dict:
    """求闭合边界：给一个内部点（seed_point）或一组图元，返回外轮廓（图元 id 和点）、孤岛、面积；不闭合时给出缺口位置和大小。"""
    seed = ctx.point(a.seed_point, False) if a.seed_point is not None else None
    if seed is None and not a.candidate_entity_ids:
        raise ToolError("MISSING_ARG", "需要 seed_point 或 candidate_entity_ids", "")
    pool = ctx.d.entities
    r = boundary.find_closed_boundary(pool, seed_point=seed, candidate_ids=a.candidate_entity_ids, tolerance=a.tolerance)
    return ctx.result(("闭合，面积 %.6g" % r["enclosed_area"]) if r["is_closed"] else r.get("message", "不闭合"), **r)


class GripsArgs(Args):
    entity_id: str
    boundary_entity_ids: Ids = None
    analyze_trim_segments: bool = False


@tool("get_entity_grips_and_topology", "perception", GripsArgs, readonly=True)
def get_entity_grips_and_topology(ctx: Ctx, a: GripsArgs) -> dict:
    """求图元的夹点（端点/中点/圆心/象限点/顶点…）；analyze_trim_segments=true 时按边界图元把它切成若干段，供修剪和延伸挑段。"""
    from zhihui.cad.geom import grips, curves
    e = ctx.entity(a.entity_id)
    out: dict = {"grips": grips.grips_of(e)}
    if a.analyze_trim_segments:
        bnds = ctx.entities(a.boundary_entity_ids) if a.boundary_entity_ids else [x for x in ctx.d.entities if x.id != e.id]
        segs = []
        idx = 0
        for pr in primitives(e):
            cuts = []
            for b in bnds:
                if b.id == e.id:
                    continue
                for q in primitives(b):
                    cuts += curves.intersect(pr, q)
            for part in curves.split_at(pr, cuts) if cuts else [pr]:
                s0, s1 = curves.endpoints(part)
                mid = curves.midpoint(part)
                segs.append({"index": idx, "from": [round(v, 6) for v in s0], "to": [round(v, 6) for v in s1], "pick_hint": [round(v, 6) for v in mid]})
                idx += 1
        out["trim_segments"] = segs
    return ctx.result(f"{len(out['grips'])} 个夹点" + (f"，{len(out.get('trim_segments', []))} 段" if a.analyze_trim_segments else ""), **out)


class InspectArgs(Args):
    include_tables: list[Literal["layers", "textstyles", "dimstyles", "blocks", "layouts", "xrefs", "sysvars", "scene_digest"]] = Field(default_factory=lambda: ["layers"])


def scene_digest(d, max_items: int = 12) -> list[dict]:
    """图元超过 30 个时给的按区域聚合摘要：把图面分 3×3，列出每格的类型计数。"""
    box = select.drawing_box(d)
    if box is None:
        return []
    w, h = (box[2] - box[0]) or 1.0, (box[3] - box[1]) or 1.0
    cells: dict[tuple[int, int], dict[str, int]] = {}
    for e in d.entities:
        if "equip:auto" in e.tags:
            continue
        b = entity_bbox(e)
        if not b:
            continue
        cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        key = (min(2, int(3 * (cx - box[0]) / w)), min(2, int(3 * (cy - box[1]) / h)))
        cells.setdefault(key, {})
        cells[key][e.kind] = cells[key].get(e.kind, 0) + 1
    names = {(0, 0): "左下", (1, 0): "下中", (2, 0): "右下", (0, 1): "左中", (1, 1): "中间", (2, 1): "右中", (0, 2): "左上", (1, 2): "上中", (2, 2): "右上"}
    return [{"region": names[k], "counts": v} for k, v in sorted(cells.items(), key=lambda kv: -sum(kv[1].values()))][:max_items]


def _inventory(d) -> dict:
    """图里有什么：按类型计数，闭合多段线单独列（带圆角的矩形已经不再带 rectangle 标签）。"""
    inv: dict[str, int] = {}
    for e in d.entities:
        if e.space != d.active_space or "equip:auto" in e.tags:      # 设备图生成的图元不计（上下文里另有 vessel 段）
            continue
        key = e.kind
        if e.kind == "polyline" and e.data.get("closed"):
            key = "polyline(closed)"
        inv[key] = inv.get(key, 0) + 1
    return inv


def build_context(d) -> dict:
    """5.4 节的上下文注入（约 1500 token 以内）。"""
    layer = d.get_layer(d.current_layer)
    dr = d.draft
    return {
        "mode": d.mode, "current_space": d.active_space, "workspace": d.workspace,
        "active_layer": layer.model_dump(include={"name", "color", "linetype", "lineweight_mm", "on", "locked"}) if layer else {"name": d.current_layer},
        "layers": [l.name for l in d.layers],
        "drafting_aids": {"snap": dr.snap_on, "grid": dr.grid_on, "ortho": dr.ortho_on, "polar": dr.polar_on, "osnap": dr.osnap_modes if dr.osnap_on else [], "otrack": dr.otrack_on, "dyn": dr.dynmode_on},
        "linetype_scale": {"ltscale": d.ltscale, "celtscale": d.celtscale}, "limits": list(d.limits),
        "current_text_style": d.current_text_style, "current_dim_style": d.current_dim_style,
        "last_point": list(d.session.last_point) if d.session.last_point else None,
        "selection": d.session.selection[:12],
        "recent_entities": [{k: v for k, v in r.items() if k in ("entity_id", "type", "tags", "layer", "op", "summary")} for r in d.session.recent_entities[:10]],
        "entity_count": len(d.entities),
        "inventory": _inventory(d),
        **({"scene_digest": scene_digest(d)} if len([e for e in d.entities if "equip:auto" not in e.tags]) > 30 else {}),
        **({"vessel": vessel_context(d)} if d.vessel is not None else {}),
    }


def vessel_context(d) -> dict:
    """设备图上下文（任务书 6.3，约 800 token 以内）：类型、位号、Di×L×δ、封头、支座、管口（符号/DN/用途/位置/方位/来源）、缺项、最近的默认项、警告摘要。"""
    from zhihui.cad.equipment import spec_ops as S
    v = d.vessel
    sh = v.shell

    def src(key):
        s = v.sources.get(key, "")
        return "user" if s.startswith("user:") else "rule" if s.startswith("rule:") else "table" if s.startswith("table:") else "calc" if s.startswith("calc:") else ""

    return {
        "kind": v.kind, "tag": v.tag, "name": v.name,
        "shell": f"Di{S.fmt_value(sh.inner_diameter_mm)}×L{S.fmt_value(sh.length_mm)}×δ{S.fmt_value(sh.thickness_mm)}" + (f"（切线长 {S.fmt_value(sh.tangent_length_mm)}）" if sh.tangent_length_mm else "")
                 + f"，长度口径={'切线长' if src('shell.tangent_length_mm') == 'user' else '筒体长度'}",
        "heads": [f"{h.position}:{h.kind}" for h in v.heads],
        "supports": [f"{s.kind}×{s.count}" for s in v.supports],
        "nozzles": [{"mark": n.mark, "dn": n.dn, "purpose": n.purpose or n.purpose_text, "location": n.location, "orientation_deg": n.orientation_deg,
                     "src": {"orientation": src(S.nkey(n, "orientation_deg")), "position": src(S.nkey(n, "axial_mm")) or src(S.nkey(n, "radial_mm"))}} for n in v.nozzles][:16],
        "design": {k: x for k, x in v.design.model_dump(exclude_none=True).items() if k in ("design_pressure_mpa", "design_temp_c", "medium", "design_standard")},
        "pending": [p["path"] for p in S.missing(v)],
        "warnings": [w[:60] for w in v.warnings[:4]],
    }


@tool("inspect_drawing_context", "perception", InspectArgs, readonly=True)
def inspect_drawing_context(ctx: Ctx, a: InspectArgs) -> dict:
    """查看图纸上下文：当前空间和图层、实体数量，以及按需的图层表、文字样式、标注样式、块、布局、外部参照、系统变量和按区域聚合的场景摘要。"""
    d = ctx.d
    tables: dict = {}
    for t in a.include_tables:
        if t == "layers":
            tables["layers"] = [l.model_dump() for l in d.layers]
        elif t == "textstyles":
            tables["textstyles"] = d.text_styles
        elif t == "dimstyles":
            tables["dimstyles"] = d.dim_styles
        elif t == "blocks":
            tables["blocks"] = [{"name": b["name"], "entities": len(b.get("entities", [])), "attdefs": [x.get("tag") for x in b.get("attdefs", [])]} for b in d.blocks]
        elif t == "layouts":
            tables["layouts"] = [{"name": l["name"], "viewports": len(l.get("viewports", []))} for l in d.layouts]
        elif t == "xrefs":
            tables["xrefs"] = d.xrefs
        elif t == "sysvars":
            tables["sysvars"] = {"LTSCALE": d.ltscale, "CELTSCALE": d.celtscale, "PSLTSCALE": d.psltscale, "LIMMIN": list(d.limits[:2]), "LIMMAX": list(d.limits[2:]), "LIMCHECK": d.limits_check,
                                 "PDMODE": d.point_style.get("pdmode", 0), "PDSIZE": d.point_style.get("pdsize", 0), "ORTHOMODE": d.draft.ortho_on, "SNAPMODE": d.draft.snap_on, "GRIDMODE": d.draft.grid_on}
        elif t == "scene_digest":
            tables["scene_digest"] = scene_digest(d)
    return ctx.result(f"空间 {d.active_space}，当前层「{d.current_layer}」，{len(d.entities)} 个图元", current_space=d.active_space, active_layer=d.current_layer, tables=tables,
                      entity_count=len(d.entities), context=build_context(d))
