"""出图组装（任务书 5.1 sheet.py）：选幅面比例 → 视图放进模型空间（1:1）→ 建布局「设备图」→ 图框 + 标题栏 → 视口（各用标准比例并锁定）
→ 表格和技术要求放在图纸空间 → 每个视口一套标注样式（dimscale = 比例分母）。

compose(spec) 只算“应该有哪些生成图元”（确定性编号）；apply(d, plan) 按编号做差量：没变的不动，变了的更新，消失的删除；
带 equip:detached 的图元、用户自己画的图元原样保留。
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field

from zhihui.cad.geom import layout as LYT
from zhihui.ir import GeoEntity, Layer

from . import std_tables as T
from . import tables_draw as TD
from .balloons import balloon_ops, part_list
from .dims import dim_style_name, main_view_dims
from .spec_ops import fmt_value
from .views import Builder, ViewOut, lateral_reach, left_view, main_view, ops_bbox, orientation_view

LAYOUT = "设备图"
ORI_LAYOUT = "管口方位图"
PRIORITY = [1.0, 2.0, 5.0, 10.0, 20.0, 50.0, 100.0, 200.0]
SECOND = [15.0, 25.0, 30.0]
ROLE_LAYER = {"outline": "粗实线", "thin": "细实线", "center": "中心线", "hidden": "虚线", "dim": "尺寸标注", "text": "文字", "hatch": "剖面线", "frame": "图框",
              "sheet_edge": "图框", "title_block_outer": "图框", "title_block_cell": "图框", "title_block_label": "文字", "title_block_value": "文字",
              "title_block_placeholder": "文字", "title_block_note": "文字", "unit_name": "文字", "drawing_name": "文字"}
LAYER_DEF = {"粗实线": (7, "Continuous", 0.5), "细实线": (7, "Continuous", 0.25), "中心线": (1, "CENTER", 0.25), "虚线": (2, "DASHED", 0.25), "尺寸标注": (3, "Continuous", 0.25),
             "文字": (7, "Continuous", 0.25), "剖面线": (7, "Continuous", 0.25), "图框": (7, "Continuous", 0.5)}
RIGHT_W = 180.0
GAP = 4.0


@dataclass
class SheetPlan:
    ops: list[dict] = field(default_factory=list)          # [{id, kind, role, data, tags, space}]
    layouts: list[dict] = field(default_factory=list)
    dim_styles: list[dict] = field(default_factory=list)
    record: dict = field(default_factory=dict)              # 自检用：视口、表格、件号、尺寸链
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _area(sheet: str):
    W, H = LYT.paper_size(f"ISO_{sheet}")
    x0, y0, x1, y1 = LYT.inner_frame((W, H), True)
    return (W, H), (x0, y0, x1, y1)


def _annotated_main(spec, den: float):
    v = main_view(spec, den)
    dops, dmeta = main_view_dims(spec, v)
    bb = ops_bbox(v.ops + dops)
    rows = part_list(spec, v)
    gap = 10.0 * den
    reach = lateral_reach(spec, den)
    pos = (max(bb[2], reach) + gap + 5.0 * den) if spec.vertical else (max(bb[3], reach) + gap + 5.0 * den)
    bops, numbered = balloon_ops(spec, v, rows, pos)
    allops = v.ops + dops + bops
    return v, allops, ops_bbox(allops), dmeta, numbered


def _fits(bb, den, w, h, margin=6.0) -> bool:
    return (bb[2] - bb[0]) / den + margin <= w and (bb[3] - bb[1]) / den + margin <= h


def choose_scale(spec, area_w: float, area_h: float, reserve_w: float, min_util: float, forced: str | None = None):
    """R-SHEET-01：优先系列里能放下主视图的最大比例；放不下或图面利用率低于阈值时允许第二系列（1:15/1:25/1:30）。"""
    cands: list[tuple[float, tuple]] = []
    if forced:
        den = 1.0 / LYT.scale_ratio(forced)
        return den, _annotated_main(spec, den), [f"主视图比例按用户指定 {forced}"]

    def rw(den: float) -> float:                                 # 右侧留给左视图/方位图的宽度（可随比例变化）
        return reserve_w(den) if callable(reserve_w) else reserve_w
    notes: list[str] = []
    best = None
    for den in PRIORITY:
        out = _annotated_main(spec, den)
        if _fits(out[2], den, area_w - rw(den), area_h):
            best = (den, out)
            break
        cands.append((den, out))
    util = 0.0
    if best is not None:
        bb = best[1][2]
        util = ((bb[2] - bb[0]) / best[0]) * ((bb[3] - bb[1]) / best[0]) / ((area_w - rw(best[0])) * area_h)
    if best is None or util < min_util:
        for den in sorted(SECOND):
            if best is not None and den >= best[0]:
                break
            out = _annotated_main(spec, den)
            if _fits(out[2], den, area_w - rw(den), area_h):
                best = (den, out)
                notes.append(f"按 GB/T 14690 第二系列取比例 1:{den:g}（优先系列图面利用率过低或放不下）")
                break
    if best is None:
        den = PRIORITY[-1]
        best = (den, _annotated_main(spec, den))
        notes.append("主视图在这个幅面上放不下，已取最小比例；建议加大幅面")
    return best[0], best[1], notes


def _end_view(spec, den, center):
    return orientation_view(spec, den, center) if spec.vertical else left_view(spec, den, center)


def compose(spec, title_fields: dict | None = None, extra_layouts: list[str] | None = None) -> SheetPlan:
    plan = SheetPlan()
    sheet = spec.drawing.get("sheet") or "A1"
    (W, H), (ix0, iy0, ix1, iy1) = _area(sheet)
    # ---- 右侧一列：表格（先算高度，决定技术要求放哪里）
    rc_x0 = ix1 - RIGHT_W
    view_x0, view_x1 = ix0 + 5.0, rc_x0 - 5.0
    view_y0, view_y1 = iy0 + 5.0, iy1 - 5.0
    min_util = float(T.rule_value("min_utilization"))
    forced = spec.drawing.get("scale") if spec.sources.get("drawing.scale", "").startswith("user:") else None
    # 立式：主视图 + 右侧方位图（方位图约占 1/3 宽）；卧式：主视图 + 右侧左视图（同比例，宽度 = 左视图实际包围盒，含标签和方位说明文字）
    area_w, area_h = view_x1 - view_x0, view_y1 - view_y0
    if spec.vertical:
        reserve = area_w * 0.36
    else:
        def reserve(den: float) -> float:
            return (lambda bb: (bb[2] - bb[0]) / den + 6.0 + GAP * 3)(_end_view(spec, den, (0.0, 0.0)).bbox)
    den, (mv, mops, mbb, dmeta, numbered), notes = choose_scale(spec, area_w, area_h, reserve, min_util, forced)
    plan.notes += notes
    # ---- 方位图 / 左视图
    if spec.vertical:
        ow = area_w - (mbb[2] - mbb[0]) / den - GAP * 3
        oh = area_h * 0.62
        den2 = None
        for d2 in PRIORITY + SECOND:
            if d2 < den / 2:
                continue
            ov = _end_view(spec, d2, (0.0, 0.0))
            if _fits(ov.bbox, d2, ow, oh):
                den2 = d2 if den2 is None or d2 < den2 else den2
        if den2 is None:
            den2 = PRIORITY[-1]
            plan.warnings.append("管口方位图在剩余空间里放不下，已取最小比例")
    else:
        den2 = den
    off_x = mbb[2] + 40.0 * max(den, den2) + 3.0 * spec.shell.inner_diameter_mm
    ov = _end_view(spec, den2, (off_x, (mv.info["L"] / 2) if spec.vertical else 0.0))
    # ---- 视口布置
    mw, mh = (mbb[2] - mbb[0]) / den + 6.0, (mbb[3] - mbb[1]) / den + 6.0
    ow_, oh_ = (ov.bbox[2] - ov.bbox[0]) / den2 + 6.0, (ov.bbox[3] - ov.bbox[1]) / den2 + 6.0
    mvx0 = view_x0
    mvy0 = view_y0 + max(0.0, (area_h - mh) / 2)
    vp_main = (mvx0, mvy0, mvx0 + mw, mvy0 + mh)
    ovx0 = min(view_x1 - ow_, vp_main[2] + GAP * 2)
    if spec.vertical:
        ovy1 = view_y1
        vp_ori = (ovx0, ovy1 - oh_, ovx0 + ow_, ovy1)
    else:                                                     # 左视图与主视图按投影关系对齐（轴线同高）
        axis_y = mvy0 + 3.0 + (0.0 - mbb[1]) / den
        vp_ori = (ovx0, axis_y - (0.0 - ov.bbox[1]) / den2 - 3.0, ovx0 + ow_, axis_y - (0.0 - ov.bbox[1]) / den2 - 3.0 + oh_)
    st1, st2 = dim_style_name(den), dim_style_name(den2)
    plan.dim_styles = [_dim_style(st1, den), *([_dim_style(st2, den2)] if st2 != st1 else [])]
    # ---- 图纸空间：图框、标题栏、表格、技术要求
    pb = Builder("P", None)
    fields = {"drawing_name": f"{spec.name or spec.tag or ('立式储罐' if spec.vertical else '卧式储罐')}设备图（方案图）", "scale_text": f"1:{fmt_value(den)}",
              "stage": "方案", "sheet": "共 1 张 第 1 张", **(title_fields or {})}
    frame = LYT.frame_ops((W, H), True)
    tb_ops, tb_missing = LYT.title_block_ops((W, H), fields, True, template="hg_equipment")
    pb.add("FRAME", [_role(o) for o in frame], local=False)
    pb.add("TB", [_role(o) for o in tb_ops], local=False)
    tbh = LYT.title_block_height("hg_equipment")
    y = iy0 + tbh
    tt = TD.tpl()
    p_cols = [(t, w) for t, w in tt["parts_table"]["cols"]]
    p_ops, p_info = TD.draw_table("明细栏", rc_x0, y, p_cols, TD.parts_rows(numbered), header_at_bottom=True)
    pb.add("TBL-PARTS", p_ops, local=False)
    y = p_info["rect"][3] + GAP
    n_cols = [(t, w) for t, w in tt["nozzle_table"]["cols"]]
    n_rows = TD.nozzle_rows(spec)
    d_cols = [(t, w) for t, w in tt["design_table"]["cols"]]
    d_rows = TD.design_rows(spec)
    n_h = TD.table_height(n_cols, n_rows, True)
    d_h = TD.table_height(d_cols, d_rows, True)
    unverified = T.unverified_summary([t for t in spec.drawing.get("unverified_tables", []) if t != "rules"]) + (["默认规则经验值（rules.json）"] if "rules" in spec.drawing.get("unverified_tables", []) else [])
    tr_lines = TD.tech_requirements(spec, wall_exaggerated=mv.info["tw"] > float(spec.shell.thickness_mm) + 1e-9, unverified=unverified, pending_flange_sketch=TD.uses_pending_sketch(spec))
    tr_h = TD.tech_height(tr_lines, RIGHT_W)
    y_nozzle_top = y + n_h
    n_ops, n_info = TD.draw_table("管口表", rc_x0, y_nozzle_top, n_cols, n_rows, title="管口表")
    pb.add("TBL-NOZ", n_ops, local=False)
    y = y_nozzle_top + GAP
    d_ops, d_info = TD.draw_table("设计数据表", rc_x0, y + d_h, d_cols, d_rows, title="设计数据表")
    pb.add("TBL-DESIGN", d_ops, local=False)
    y = y + d_h + GAP
    tables = [p_info, n_info, d_info]
    if y + tr_h <= iy1 - 2.0:
        t_ops, t_info = TD.tech_ops(tr_lines, rc_x0, y + tr_h, RIGHT_W)
    else:                                                     # 右栏放不下：技术要求放到视图区左下角（视口已经让开）
        tw_ = min(300.0, view_x1 - view_x0)
        tr_h2 = TD.tech_height(tr_lines, tw_)
        t_ops, t_info = TD.tech_ops(tr_lines, view_x0, view_y0 + tr_h2, tw_)
        if vp_main[1] < view_y0 + tr_h2 + GAP and vp_main[0] < view_x0 + tw_:
            plan.warnings.append("技术要求移到了视图区左下角；如与视图重叠，请换更大幅面")
    pb.add("TECH", t_ops, local=False)
    tables.append(t_info)
    # ---- 视口
    vps = [
        {"id": f"{LAYOUT}-1", "corners": [[vp_main[0], vp_main[1]], [vp_main[2], vp_main[3]]], "target_center": [(mbb[0] + mbb[2]) / 2, (mbb[1] + mbb[3]) / 2],
         "view_height": (vp_main[3] - vp_main[1]) * den, "scale": f"1:{fmt_value(den)}", "locked": True, "frozen_layers": [], "equip_view": "M"},
        {"id": f"{LAYOUT}-2", "corners": [[vp_ori[0], vp_ori[1]], [vp_ori[2], vp_ori[3]]], "target_center": [(ov.bbox[0] + ov.bbox[2]) / 2, (ov.bbox[1] + ov.bbox[3]) / 2],
         "view_height": (vp_ori[3] - vp_ori[1]) * den2, "scale": f"1:{fmt_value(den2)}", "locked": True, "frozen_layers": [], "equip_view": ov.view}]
    lay = {"name": LAYOUT, "page_setup": {"paper": f"ISO_{sheet}", "size": [W, H], "orientation": "landscape", "device": "pdf", "plot_area": "layout", "plot_scale": "1:1", "center_plot": True,
                                          "color_mode": "monochrome", "lineweights_on": True, "dpi": 150},
           "viewports": vps, "frame": f"ISO_{sheet}", "title_block": {k: v for k, v in fields.items() if v}, "equip": True}
    plan.layouts.append(lay)
    # ---- 汇总图元（模型空间：主视图 + 方位图/左视图；图纸空间：上面这些）
    for o in mops:
        o = dict(o)
        if o["kind"] == "dimension":
            o["data"] = {**o["data"], "style": st1}
        plan.ops.append({**o, "space": "Model"})
    for o in ov.ops:
        plan.ops.append({**o, "space": "Model"})
    for o in pb.ops:
        plan.ops.append({**o, "space": LAYOUT})
    plan.warnings += mv.info.get("warnings", [])
    if tb_missing:
        plan.notes.append("标题栏里设计单位、图号等没给的格子留了占位说明")
    plan.record = {"sheet": sheet, "paper": [W, H], "inner": [ix0, iy0, ix1, iy1], "den_main": den, "den_end": den2,
                   "viewports": [{"id": v["id"], "rect": [*v["corners"][0], *v["corners"][1]], "scale": v["scale"], "view": v["equip_view"]} for v in vps],
                   "tables": [{"name": t["name"], "rect": list(t["rect"]), "first_col": t.get("first_col", [])} for t in tables],
                   "title_block": [ix1 - 180.0, iy0, ix1, iy0 + tbh],
                   "nozzle_rows": [r[0] for r in n_rows], "parts": [{"no": r["no"], "name": r["name"], "qty": r["qty"]} for r in numbered],
                   "main_marks": sorted(mv.nozzles), "end_marks": sorted(ov.nozzles), "dim": dmeta, "tw": mv.info["tw"], "end_view": ov.view,
                   "tech_requirements": tr_lines, "unverified": unverified}
    # ---- 单独的管口方位图布局（vessel_generate_drawing(views=["orientation"]) 时；要在 record 建好之后，它往 record 里补自己的矩形）
    if extra_layouts and ORI_LAYOUT in extra_layouts:
        _orientation_layout(spec, plan, ov, den2, n_cols, n_rows, fields)
    return plan


def _orientation_layout(spec, plan: SheetPlan, ov: ViewOut, den2: float, n_cols, n_rows, fields: dict) -> None:
    """单独一张 A2 的“管口方位图”：方位图（或左视图）视口 + 管口表 + 标题栏。"""
    (W, H), (ix0, iy0, ix1, iy1) = _area("A2")
    pb = Builder("Q", None)
    f2 = {**fields, "drawing_name": fields["drawing_name"].replace("设备图", "管口方位图"), "scale_text": f"1:{fmt_value(den2)}"}
    pb.add("FRAME", [_role(o) for o in LYT.frame_ops((W, H), True)], local=False)
    tb_ops, _ = LYT.title_block_ops((W, H), f2, True, template="hg_equipment")
    pb.add("TB", [_role(o) for o in tb_ops], local=False)
    tbh = LYT.title_block_height("hg_equipment")
    n_h = TD.table_height(n_cols, n_rows, True)
    n_ops, n_info = TD.draw_table("管口表", ix1 - RIGHT_W, iy0 + tbh + GAP + n_h, n_cols, n_rows, title="管口表")
    pb.add("TBL-NOZ", n_ops, local=False)
    aw, ah = ix1 - RIGHT_W - ix0 - 15.0, iy1 - iy0 - 10.0
    best = den2
    for d2 in sorted(PRIORITY + SECOND):
        if _fits(ov.bbox, d2, aw, ah):
            best = d2
            break
    w, h = (ov.bbox[2] - ov.bbox[0]) / best + 6.0, (ov.bbox[3] - ov.bbox[1]) / best + 6.0
    x0, y0 = ix0 + 5.0 + max(0.0, (aw - w) / 2), iy0 + 5.0 + max(0.0, (ah - h) / 2)
    vp = {"id": f"{ORI_LAYOUT}-1", "corners": [[x0, y0], [x0 + w, y0 + h]], "target_center": [(ov.bbox[0] + ov.bbox[2]) / 2, (ov.bbox[1] + ov.bbox[3]) / 2], "view_height": h * best,
          "scale": f"1:{fmt_value(best)}", "locked": True, "frozen_layers": [], "equip_view": ov.view}
    plan.layouts.append({"name": ORI_LAYOUT, "page_setup": {"paper": "ISO_A2", "size": [W, H], "orientation": "landscape", "device": "pdf", "plot_area": "layout", "plot_scale": "1:1",
                                                            "center_plot": True, "color_mode": "monochrome", "lineweights_on": True, "dpi": 150},
                         "viewports": [vp], "frame": "ISO_A2", "title_block": {k: v for k, v in f2.items() if v}, "equip": True})
    for o in pb.ops:
        plan.ops.append({**o, "space": ORI_LAYOUT})
    plan.record["orientation_layout"] = {"viewport": [x0, y0, x0 + w, y0 + h], "scale": vp["scale"], "nozzle_rows": [r[0] for r in n_rows]}


def _role(o: dict) -> dict:
    return {**o, "role": o.get("role", "frame"), "tags": list(o.get("tags", [])) + ([o["role"]] if o.get("role") else [])}


def _dim_style(name: str, den: float) -> dict:
    from zhihui.cad.geom import presets
    base = dict(presets.DIM_STYLES["GB_MECH_MM"])
    base.update({"name": name, "dimscale": float(den), "precision": 0, "text_height": float(T.rule_value("dim_text_h_paper"))})
    return base


# ------------------------------------------------------------------ 写进图纸（差量）
def apply(d, plan: SheetPlan, ctx=None) -> dict:
    """把 SheetPlan 写进 Drawing：按编号差量更新生成图元，补图层、标注样式、布局。返回 {created, modified, deleted, unchanged}。"""
    spec = d.vessel
    detached = set((spec.drawing.get("detached") if spec else None) or []) | {e.id for e in d.entities if "equip:detached" in e.tags}
    for name, (col, lt, lw) in LAYER_DEF.items():
        if d.get_layer(name) is None:
            d.layers.append(Layer(name=name, color=col, linetype=lt, lineweight_mm=lw))
    for st in plan.dim_styles:
        old = next((x for x in d.dim_styles if x["name"] == st["name"]), None)
        if old is None:
            d.dim_styles.append(st)
        else:
            old.update(st)
    existing = {e.id: e for e in d.entities if "equip:auto" in e.tags}
    want_ids = set()
    created, modified, unchanged = [], [], 0
    rev = d.revision + 1
    for o in plan.ops:
        oid = o["id"]
        if oid in detached:
            continue
        want_ids.add(oid)
        data = copy.deepcopy(o["data"])
        if o["kind"] == "text":
            if not data.get("style"):
                data["style"] = d.current_text_style
            data.setdefault("width_factor", 0.7)                    # 长仿宋（GB/T 14691），字宽估算也按 0.7
        layer = ROLE_LAYER.get(o.get("role", "outline"), "细实线")
        tags = list(dict.fromkeys(o["tags"]))
        lw = 0.5 if o.get("role") in ("frame", "title_block_outer") else None
        e = existing.get(oid)
        if e is None:
            ent = GeoEntity(id=oid, kind=o["kind"], layer=layer, data=data, tags=tags, space=o["space"], created_rev=rev, modified_rev=rev, lineweight_mm=lw)
            d.entities.append(ent)
            created.append(oid)
            continue
        if (e.kind, e.layer, e.space, json.dumps(e.data, sort_keys=True), e.tags, e.lineweight_mm) != (o["kind"], layer, o["space"], json.dumps(data, sort_keys=True), tags, lw):
            e.kind, e.layer, e.space, e.data, e.tags, e.lineweight_mm, e.modified_rev = o["kind"], layer, o["space"], data, tags, lw, rev
            modified.append(oid)
        else:
            unchanged += 1
    gone = [i for i in existing if i not in want_ids]
    if gone:
        d.entities = [e for e in d.entities if e.id not in set(gone)]
        d.session.selection = [s for s in d.session.selection if s not in set(gone)]
    names = {l["name"] for l in plan.layouts}
    d.layouts = [l for l in d.layouts if not l.get("equip") and l["name"] not in names] + plan.layouts
    model = [e for e in d.entities if e.space == "Model"]
    if model:
        bb = d.bbox()
        if bb:
            d.limits = (min(0.0, bb[0]), min(0.0, bb[1]), max(bb[2], d.limits[2]), max(bb[3], d.limits[3]))
    if ctx is not None:
        ctx.created += [i for i in created if i not in ctx.created]
        ctx.modified += [i for i in modified if i not in ctx.modified]
        ctx.deleted += [i for i in gone if i not in ctx.deleted]
    return {"created": created, "modified": modified, "deleted": gone, "unchanged": unchanged}
