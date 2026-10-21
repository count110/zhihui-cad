"""布局与出图（函数 33–35，意图类别 layout_plotting）：模型/图纸空间、视口与比例、打印设置与输出。"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import Field

from zhihui.cad.geom import layout as L
from zhihui.cad.geom.entities import entity_bbox
from zhihui.ir import Layer
from zhihui.tools.core import ToolError
from .ctx import Ctx
from .registry import Args, Ids, Pt, tool

ISO_NAMES = Literal["ISO_A0", "ISO_A1", "ISO_A2", "ISO_A3", "ISO_A4"]
FRAME_LAYER = "图框"


def _need(cond, msg: str, hint: str = "") -> None:
    if not cond:
        raise ToolError("MISSING_ARG", msg, hint or "向用户确认")


def get_layout(d, name: str) -> dict:
    lay = next((x for x in d.layouts if x["name"].lower() == name.lower()), None)
    if lay is None:
        raise ToolError("LAYOUT_NOT_FOUND", f"没有布局「{name}」", f"现有布局：{[x['name'] for x in d.layouts] or '无'}；先用 switch_model_and_layout_space(layout_action=create) 新建")
    return lay


def _ensure_frame_layer(ctx: Ctx) -> None:
    if ctx.d.get_layer(FRAME_LAYER) is None:
        ctx.d.layers.append(Layer(name=FRAME_LAYER, color=7, lineweight_mm=0.5, description="图框与标题栏"))
        ctx.notes.append(f"新建图层「{FRAME_LAYER}」放图框和标题栏")


def _draw_frame(ctx: Ctx, lay: dict, frame: str, fields: dict) -> list[str]:
    """在布局里画图框和标题栏（图元的 space = 布局名）。返回缺少的必填字段。"""
    d = ctx.d
    size = tuple(lay["page_setup"]["size"])
    old = d.active_space
    d.active_space = lay["name"]
    try:
        _ensure_frame_layer(ctx)
        drop = [e for e in d.entities if e.space == lay["name"] and any(t in e.tags for t in ("frame", "title_block"))]
        for e in drop:
            ctx.remove(e)
        ops, missing = L.title_block_ops(size, fields)
        for o in L.frame_ops(size) + ops:
            tags = ["frame"] if o["role"] in ("sheet_edge", "frame") else ["title_block", o["role"]]
            data = dict(o["data"])
            if o["kind"] == "text":
                data["style"] = d.current_text_style
            e = ctx.add(o["kind"], data, FRAME_LAYER, tags)
            if o["role"] in ("frame", "title_block_outer"):
                e.lineweight_mm = 0.5
    finally:
        d.active_space = old
    lay["frame"] = frame
    lay["title_block"] = {k: v for k, v in fields.items() if v}
    return missing


# ------------------------------------------------------------------ 33 switch_model_and_layout_space
class SwitchSpaceArgs(Args):
    target_space: str = "Model"
    layout_action: Literal["switch", "create", "rename", "delete", "copy"] = "switch"
    new_layout_name: Optional[str] = None
    insert_standard_frame: Optional[ISO_NAMES] = None
    title_block_fields: Optional[dict] = None
    paper_size: Optional[ISO_NAMES] = None          # 新建布局的图幅（不画图框）；画图框时以 insert_standard_frame 为准


@tool("switch_model_and_layout_space", "layout_plotting", SwitchSpaceArgs,
      quiet_if=lambda a: a.layout_action == "switch" and not a.insert_standard_frame and not a.title_block_fields)
def switch_model_and_layout_space(ctx: Ctx, a: SwitchSpaceArgs) -> dict:
    """切换模型空间/图纸空间，新建、重命名、删除、复制布局；新建时可插入标准图框（ISO_A0–A4）和标题栏。标题栏字段只能来自用户，没给的格子留占位说明。"""
    d = ctx.d
    act = a.layout_action
    if act == "create":
        name = a.new_layout_name or (a.target_space if a.target_space != "Model" else f"布局{len(d.layouts) + 1}")
        if name == "Model" or any(x["name"].lower() == name.lower() for x in d.layouts):
            raise ToolError("LAYOUT_EXISTS", f"布局名「{name}」已被占用", "换一个名字")
        paper = a.insert_standard_frame or a.paper_size or "ISO_A3"
        w, h = L.paper_size(paper)
        lay = {"name": name, "page_setup": {"paper": paper, "size": [w, h], "orientation": "landscape", "device": "pdf", "plot_area": "layout", "plot_scale": "1:1", "center_plot": True,
                                           "color_mode": "monochrome", "lineweights_on": True, "dpi": 150}, "viewports": [], "frame": None, "title_block": {}}
        d.layouts.append(lay)
        out = f"新建布局「{name}」（{paper} 横向）"
        missing: list[str] = []
        if a.insert_standard_frame:
            missing = _draw_frame(ctx, lay, a.insert_standard_frame, a.title_block_fields or {})
            out += "，已画图框和标题栏"
        if not a.insert_standard_frame:
            ctx.notes.append(f"布局按 {paper} 横向建立，没有画图框（需要时用 insert_standard_frame）" + ("" if a.paper_size else "；图幅没指定，取 ISO_A3"))
        d.active_space = name
        extra = {"missing_title_block_fields": missing} if missing else {}
        if missing:
            ctx.warnings.append(f"标题栏必填字段未提供：{'、'.join(missing)}（只能来自用户，已留占位说明）")
        return ctx.result(out, layout=name, **extra)
    target = a.target_space
    if act == "switch":
        if target != "Model":
            lay = get_layout(d, target)
            if a.insert_standard_frame:
                w, h = L.paper_size(a.insert_standard_frame)
                lay["page_setup"].update({"paper": a.insert_standard_frame, "size": [w, h]})
                miss = _draw_frame(ctx, lay, a.insert_standard_frame, a.title_block_fields or {})
                if miss:
                    ctx.warnings.append(f"标题栏必填字段未提供：{'、'.join(miss)}")
            elif a.title_block_fields:
                if not lay.get("frame"):
                    raise ToolError("NO_FRAME", f"布局「{lay['name']}」还没有图框，不能填标题栏", "同时给 insert_standard_frame")
                _draw_frame(ctx, lay, lay["frame"], {**lay.get("title_block", {}), **a.title_block_fields})
            d.active_space = lay["name"]
            return ctx.result(f"已切换到布局「{lay['name']}」（图纸空间，{len(lay['viewports'])} 个视口）", space=lay["name"], viewports=[v["id"] for v in lay["viewports"]])
        d.active_space = "Model"
        return ctx.result("已切换到模型空间", space="Model")
    lay = get_layout(d, target)
    if act == "rename":
        _need(a.new_layout_name, "重命名需要 new_layout_name")
        if any(x["name"].lower() == a.new_layout_name.lower() for x in d.layouts if x is not lay):
            raise ToolError("LAYOUT_EXISTS", f"布局名「{a.new_layout_name}」已被占用", "")
        old = lay["name"]
        for e in d.entities:
            if e.space == old:
                e.space = a.new_layout_name
        if d.active_space == old:
            d.active_space = a.new_layout_name
        lay["name"] = a.new_layout_name
        if old in d.plot_setups:
            d.plot_setups[a.new_layout_name] = d.plot_setups.pop(old)
        return ctx.result(f"布局「{old}」改名为「{a.new_layout_name}」")
    if act == "delete":
        n = [e for e in d.entities if e.space == lay["name"]]
        for e in n:
            ctx.remove(e)
        d.layouts = [x for x in d.layouts if x is not lay]
        d.plot_setups.pop(lay["name"], None)
        if d.active_space == lay["name"]:
            d.active_space = "Model"
        return ctx.result(f"已删除布局「{lay['name']}」及其中 {len(n)} 个图元（模型空间不受影响）")
    if act == "copy":
        import copy
        name = a.new_layout_name or f"{lay['name']} (2)"
        if any(x["name"].lower() == name.lower() for x in d.layouts):
            raise ToolError("LAYOUT_EXISTS", f"布局名「{name}」已被占用", "")
        new = copy.deepcopy(lay)
        new["name"] = name
        for v in new["viewports"]:
            v["id"] = f"{name}-{v['id'].split('-')[-1]}"
        d.layouts.append(new)
        old_active = d.active_space
        d.active_space = name
        for e in [x for x in d.entities if x.space == lay["name"]]:
            ctx.add(e.kind, copy.deepcopy(e.data), e.layer, list(e.tags), src=e)
        d.active_space = old_active
        return ctx.result(f"复制布局「{lay['name']}」为「{name}」", layout=name)
    raise ToolError("INVALID_ARGS", f"未知操作 {act}", "")


# ------------------------------------------------------------------ 34 manage_layout_viewports
class ViewportArgs(Args):
    layout_name: str
    action: Literal["create", "set_scale", "lock", "unlock", "freeze_layers", "delete"]
    viewport_id: Optional[str] = None
    viewport_corner1: Pt = None
    viewport_corner2: Pt = None
    model_target_center: Pt = None
    fit_entity_ids: Ids = None
    standard_scale: Optional[str] = None
    lock_display: Optional[bool] = None
    vp_frozen_layers: Optional[list[str]] = None


def _find_vp(lay: dict, vid: Optional[str]) -> dict:
    if not lay["viewports"]:
        raise ToolError("VIEWPORT_NOT_FOUND", f"布局「{lay['name']}」里还没有视口", "先 create")
    if vid is None:
        if len(lay["viewports"]) == 1:
            return lay["viewports"][0]
        raise ToolError("MISSING_ARG", f"布局里有多个视口，需要 viewport_id：{[v['id'] for v in lay['viewports']]}", "")
    vp = next((v for v in lay["viewports"] if v["id"].lower() == vid.lower()), None)
    if vp is None:
        raise ToolError("VIEWPORT_NOT_FOUND", f"没有视口 {vid}", f"现有：{[v['id'] for v in lay['viewports']]}")
    return vp


def _pt2(ctx: Ctx, expr) -> tuple[float, float]:
    p = ctx.point(expr, update_last=False)
    return (p[0], p[1])


@tool("manage_layout_viewports", "layout_plotting", ViewportArgs)
def manage_layout_viewports(ctx: Ctx, a: ViewportArgs) -> dict:
    """在布局里创建/调整视口：create（两角点 + 模型中心或要适配的图元 + 标准比例，不给比例就选能放下的最大标准比例并告知）、set_scale、lock/unlock、freeze_layers（视口内冻结图层）、delete。比例必须是标准比例（1:1、1:2、1:5、1:10…、2:1…）。"""
    d = ctx.d
    lay = get_layout(d, a.layout_name)
    act = a.action
    if act == "create":
        if a.viewport_corner1 is not None and a.viewport_corner2 is not None:
            c1, c2 = _pt2(ctx, a.viewport_corner1), _pt2(ctx, a.viewport_corner2)
        else:
            c1, c2 = L.default_viewport_corners(tuple(lay["page_setup"]["size"]), with_title_block=bool(lay.get("frame")))
            ctx.notes.append(f"视口范围未指定，按图框内框（避开标题栏）取 {c1} – {c2}")
        c1, c2 = (min(c1[0], c2[0]), min(c1[1], c2[1])), (max(c1[0], c2[0]), max(c1[1], c2[1]))
        if c2[0] - c1[0] < 5 or c2[1] - c1[1] < 5:
            raise ToolError("INVALID_ARGS", "视口太小（至少 5×5 mm）", "")
        model_ents = [e for e in d.entities if e.space == "Model"]
        if a.fit_entity_ids:
            ents = ctx.entities(a.fit_entity_ids)
            boxes = [b for b in (entity_bbox(e) for e in ents) if b]
        else:
            boxes = [b for b in (entity_bbox(e) for e in model_ents) if b]
        if a.model_target_center is not None:
            center = _pt2(ctx, a.model_target_center)
            bb = None
        elif boxes:
            bb = (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))
            center = ((bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2)
        else:
            center, bb = ((d.limits[0] + d.limits[2]) / 2, (d.limits[1] + d.limits[3]) / 2), None
        w, h = c2[0] - c1[0], c2[1] - c1[1]
        if a.standard_scale:
            scale = a.standard_scale
            if not L.is_standard_scale(scale):
                raise ToolError("NONSTANDARD_SCALE", f"比例 {scale} 不是标准比例", f"可选：{L.STANDARD_SCALES}")
        elif bb:
            scale = L.fit_scale(w, h, bb)
            ctx.notes.append(f"视口比例未指定，按图元范围选了能放下的最大标准比例 {scale}")
        else:
            scale = "1:1"
            ctx.notes.append("视口比例未指定，且没有可适配的图元，按 1:1")
        n = 1 + max([int(v["id"].split("-")[-1]) for v in lay["viewports"]] + [0])
        vp = {"id": f"{lay['name']}-{n}", "corners": [list(c1), list(c2)], "target_center": list(center), "view_height": L.view_height_for(h, scale), "scale": scale, "locked": False, "frozen_layers": []}
        lay["viewports"].append(vp)
        return ctx.result(f"在布局「{lay['name']}」创建视口 {vp['id']}，比例 {scale}，对准模型 ({center[0]:g},{center[1]:g})", viewport_id=vp["id"], scale=scale, view_height=vp["view_height"])
    vp = _find_vp(lay, a.viewport_id)
    if act == "set_scale":
        _need(a.standard_scale, "set_scale 需要 standard_scale")
        if vp.get("locked"):
            raise ToolError("VIEWPORT_LOCKED", f"视口 {vp['id']} 已锁定显示，不能改比例", "先 unlock")
        if not L.is_standard_scale(a.standard_scale):
            raise ToolError("NONSTANDARD_SCALE", f"比例 {a.standard_scale} 不是标准比例", f"可选：{L.STANDARD_SCALES}")
        (x0, y0), (x1, y1) = vp["corners"]
        vp["view_height"] = L.view_height_for(abs(y1 - y0), a.standard_scale)
        vp["scale"] = a.standard_scale
        if a.model_target_center is not None:
            vp["target_center"] = list(_pt2(ctx, a.model_target_center))
        return ctx.result(f"视口 {vp['id']} 比例改为 {a.standard_scale}", viewport_id=vp["id"], scale=a.standard_scale)
    if act in ("lock", "unlock"):
        vp["locked"] = (act == "lock") if a.lock_display is None else bool(a.lock_display)
        return ctx.result(f"视口 {vp['id']} 显示{'已锁定（缩放平移不会改变比例）' if vp['locked'] else '已解锁'}")
    if act == "freeze_layers":
        _need(a.vp_frozen_layers is not None, "freeze_layers 需要 vp_frozen_layers（图层名列表；空列表表示全部解冻）")
        bad = [n for n in a.vp_frozen_layers if d.get_layer(n) is None]
        if bad:
            raise ToolError("LAYER_NOT_FOUND", f"图层不存在：{bad}", f"现有图层：{[l.name for l in d.layers]}")
        vp["frozen_layers"] = [d.get_layer(n).name for n in a.vp_frozen_layers]
        return ctx.result(f"视口 {vp['id']} 冻结图层：{vp['frozen_layers'] or '无'}（只影响这个视口，不改全局图层状态）")
    if act == "delete":
        lay["viewports"] = [v for v in lay["viewports"] if v is not vp]
        return ctx.result(f"已删除视口 {vp['id']}")
    raise ToolError("INVALID_ARGS", f"未知操作 {act}", "")


# ------------------------------------------------------------------ 35 configure_plot_and_export
class PlotArgs(Args):
    space_or_layout: str = "Model"
    device: Literal["pdf", "png", "svg"] = "pdf"
    paper_size: Optional[ISO_NAMES] = None
    orientation: Optional[Literal["landscape", "portrait"]] = None
    plot_area: Literal["layout", "extents", "limits", "window", "display"] = "layout"
    window: Optional[list[Pt]] = None
    plot_scale: str = "fit"
    center_plot: bool = True
    color_mode: Literal["monochrome", "color"] = "monochrome"
    lineweights_on: bool = True
    dpi: int = Field(150, ge=50, le=600)
    export_now: bool = True


@tool("configure_plot_and_export", "layout_plotting", PlotArgs, quiet=True)
def configure_plot_and_export(ctx: Ctx, a: PlotArgs) -> dict:
    """配置打印设置并输出 PDF / PNG / SVG。纸张 ISO_A0–A4、方向、打印范围（布局/范围/界限/窗口/显示）、比例（"fit" 或标准比例）、单色/彩色、线宽、DPI。设置写入布局的页面设置（写入 DXF 的 PAGESETUP），实际输出由本系统渲染器完成；不读取 .ctb。"""
    d = ctx.d
    space = a.space_or_layout
    setup = {"device": a.device, "plot_area": a.plot_area, "plot_scale": a.plot_scale, "center_plot": a.center_plot, "color_mode": a.color_mode, "lineweights_on": a.lineweights_on, "dpi": a.dpi}
    if a.window:
        w = [ctx.point(p, update_last=False) for p in a.window]
        _need(len(w) == 2, "窗口需要两个角点")
        setup["window"] = [[min(w[0][0], w[1][0]), min(w[0][1], w[1][1])], [max(w[0][0], w[1][0]), max(w[0][1], w[1][1])]]
    if a.plot_area == "window" and "window" not in setup:
        raise ToolError("MISSING_ARG", "plot_area=window 需要 window（两个角点）", "")
    if a.plot_scale != "fit" and not L.is_standard_scale(a.plot_scale):
        raise ToolError("NONSTANDARD_SCALE", f"打印比例 {a.plot_scale} 不是标准比例", f"可选 fit 或 {L.STANDARD_SCALES}")
    if space != "Model":
        lay = get_layout(d, space)
        ps = lay["page_setup"]
        if a.paper_size or a.orientation:
            size = L.paper_size(a.paper_size or ps["paper"], a.orientation or ps.get("orientation", "landscape"))
            ps.update({"paper": a.paper_size or ps["paper"], "size": list(size), "orientation": a.orientation or ps.get("orientation", "landscape")})
        ps.update(setup)
        paper = ps["size"]
        space = lay["name"]
    else:
        pap = a.paper_size or d.plot_setups.get("Model", {}).get("paper", "ISO_A3")
        ori = a.orientation or d.plot_setups.get("Model", {}).get("orientation", "landscape")
        size = L.paper_size(pap, ori)
        d.plot_setups["Model"] = {**d.plot_setups.get("Model", {}), **setup, "paper": pap, "orientation": ori, "size": list(size)}
        paper = list(size)
    out: dict = {"space": space, "paper": paper, "setup": d.plot_setups.get(space) or setup}
    if not a.export_now:
        return ctx.result(f"已保存「{space}」的打印设置（{a.device}，{a.plot_scale}，{a.color_mode}）", **out)
    from zhihui.cad.render_general import plot
    try:
        path, info = plot(d, space, {**d.plot_setups.get(space, {}), **setup, "size": paper} if space == "Model" else {**setup, "size": paper}, ctx.export_dir())
    except ValueError as ex:
        raise ToolError("NOTHING_TO_PLOT", str(ex), "先画点东西，或改打印范围")
    out.update({k: v for k, v in info.items() if k != "warnings"})
    out["file"] = ctx.file_info(path)
    ctx.warnings += info.get("warnings", [])
    return ctx.result(f"已输出「{space}」→ {path.name}（{a.device}，比例 {info.get('scale_text', a.plot_scale)}）", **out)
