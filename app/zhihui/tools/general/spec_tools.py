"""工程制图专业函数（43–45，意图类别 engineering_specialty）：公差与粗糙度、专业工具集、符号库。"""
from __future__ import annotations

import copy
import json
import math
import re
from typing import Any, Literal, Optional

from pydantic import Field

from zhihui.cad.geom import mech
from zhihui.cad.geom.entities import P, entity_bbox, pl, polar
from zhihui.cad.geom.errors import GeomError
from zhihui.ir import Layer
from zhihui.tools.core import ToolError
from .ctx import Ctx, to_tool_error
from .registry import Args, Ids, Pt, tool

ROLE_LAYERS = {"outline": ["粗实线", "墙体", "Visible"], "thin": ["细实线", "Thin"], "center": ["中心线", "轴线", "Center"], "hidden": ["虚线", "Hidden"],
               "dim": ["尺寸标注", "标注", "Dimensions"], "text": ["文字", "Text"]}
ROLE_DEFAULT = {"outline": ("粗实线", "Continuous", 0.5, 7), "thin": ("细实线", "Continuous", 0.25, 7), "center": ("中心线", "CENTER", 0.25, 1), "hidden": ("虚线", "DASHED", 0.25, 2),
                "dim": ("尺寸标注", "Continuous", 0.25, 3), "text": ("文字", "Continuous", 0.25, 7)}


def _need(cond, msg: str, hint: str = "") -> None:
    if not cond:
        raise ToolError("MISSING_ARG", msg, hint or "向用户确认这个数值，不要自己编")


def role_layer(ctx: Ctx, role: str) -> str:
    """按用途找图层：先找现有的（GB 机械 / GB 建筑 / ISO 预设里的同类图层），没有才新建标准图层。"""
    d = ctx.d
    for name in ROLE_LAYERS.get(role, []):
        l = d.get_layer(name)
        if l is not None:
            if l.locked:
                raise ToolError("LAYER_LOCKED", f"图层「{l.name}」已锁定", f"先用 set_layer_state 解锁 {l.name}")
            return l.name
    name, lt, lw, col = ROLE_DEFAULT.get(role, ROLE_DEFAULT["thin"])
    d.layers.append(Layer(name=name, color=col, linetype=lt, lineweight_mm=lw))
    ctx.notes.append(f"新建图层「{name}」（{lt}，{lw} mm）")
    return name


def _next_group(d) -> str:
    n = 1
    for e in d.entities:
        for t in e.tags:
            m = re.fullmatch(r"group:G(\d+)", t)
            if m:
                n = max(n, int(m.group(1)) + 1)
    return f"G{n}"


def add_ops(ctx: Ctx, ops: list[dict], group: str | None = None, layer: str | None = None, extra_tags: list[str] | None = None) -> list[str]:
    ids = []
    st = ctx.d.current_text_style
    for o in ops:
        data = copy.deepcopy(o["data"])
        if o["kind"] == "text" and not data.get("style"):
            data["style"] = st
        tags = list(o.get("tags", [])) + (extra_tags or []) + ([f"group:{group}"] if group else [])
        e = ctx.add(o["kind"], data, layer or role_layer(ctx, o.get("role", "outline" if o["kind"] != "text" else "text")), tags)
        ids.append(e.id)
    return ids


def anchor_of(ctx: Ctx, e) -> tuple[float, float]:
    """图元上的一个定位点：直线中点、圆上端点、多段线第一段中点、文字/块插入点、其余取包围盒上边中点。"""
    k, d = e.kind, e.data
    if k == "line":
        a, b = P(d["p1"]), P(d["p2"])
        return ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
    if k in ("circle", "arc"):
        c = P(d["c"])
        return (c[0], c[1] + d["r"])
    if k == "polyline":
        v = d["vertices"]
        return ((v[0]["p"][0] + v[1]["p"][0]) / 2, (v[0]["p"][1] + v[1]["p"][1]) / 2)
    if k == "dimension":
        return (d["loc"][0], d["loc"][1])
    bb = entity_bbox(e)
    if bb:
        return ((bb[0] + bb[2]) / 2, bb[3])
    raise ToolError("INVALID_ARGS", f"{e.id} 没有可用的定位点", "给出 insert_point")


# ------------------------------------------------------------------ 43 create_mechanical_tolerances
GDT_LITERAL = Literal["straightness", "flatness", "circularity", "cylindricity", "parallelism", "perpendicularity", "angularity", "position", "concentricity", "symmetry", "circular_runout", "total_runout"]


class TolArgs(Args):
    tolerance_category: Literal["dimensional_fit", "deviation", "symmetric", "limits", "geometric_gdt", "datum_feature", "surface_roughness"]
    target_dim_or_entity_id: Optional[str] = None
    insert_point: Pt = None
    iso_fit_code: Optional[str] = None
    upper_deviation: Optional[float] = None
    lower_deviation: Optional[float] = None
    gdt_symbol: Optional[GDT_LITERAL] = None
    gdt_value: Optional[float] = Field(None, gt=0)
    diameter_zone: bool = False
    datum_refs: Optional[list[str]] = None
    datum_letter: Optional[str] = None
    roughness_param: Literal["Ra", "Rz"] = "Ra"
    roughness_value: Optional[float] = Field(None, gt=0)
    process_symbol: Literal["any", "remove", "no_remove"] = "remove"
    layer: Optional[str] = None


def _dev(v: float) -> str:
    return "0" if abs(v) < 1e-12 else f"{v:+.3f}"


def _stack(up: float, lo: float) -> str:
    return "{\\S" + _dev(up) + "^" + _dev(lo) + ";}"


def _dim_nominal(e) -> float:
    m = e.data.get("measurement")
    if m is None:
        raise ToolError("INVALID_ARGS", f"{e.id} 没有测量值", "只能对线性/半径/直径标注加公差")
    return float(m)


@tool("create_mechanical_tolerances", "engineering_specialty", TolArgs)
def create_mechanical_tolerances(ctx: Ctx, a: TolArgs) -> dict:
    """机械公差：尺寸公差（配合代号 H7、H7/g6 查 ISO 286 表；上下偏差；对称偏差；极限尺寸）、形位公差框格（12 种特征符号，GB/T 1182）、基准符号、表面粗糙度符号（GB/T 131）。偏差只从标准表算，表里没有就反问用户给偏差，不编造；公差值、粗糙度值必须来自用户。"""
    d = ctx.d
    cat = a.tolerance_category
    try:
        if cat in ("dimensional_fit", "deviation", "symmetric", "limits"):
            _need(a.target_dim_or_entity_id, "尺寸公差需要 target_dim_or_entity_id（要加公差的标注）")
            e = ctx.entity(a.target_dim_or_entity_id, for_edit=True)
            if e.kind != "dimension":
                raise ToolError("INVALID_ARGS", f"{e.id} 不是尺寸标注", "先用 create_linear_and_angular_dim 或 create_radial_and_chain_dim 标注，再加公差")
            nom = _dim_nominal(e)
            tol: dict
            if cat == "dimensional_fit" or (cat == "limits" and a.iso_fit_code):
                _need(a.iso_fit_code, "配合代号需要 iso_fit_code（如 H7、H7/g6）")
                devs = mech.fit(nom, a.iso_fit_code)
                if cat == "limits":
                    up, lo = devs[0]["upper_mm"], devs[0]["lower_mm"]
                    text = "{\\S" + f"{nom + up:.3f}^{nom + lo:.3f}" + ";}"
                    tol = {"mode": "limits", "code": a.iso_fit_code, "upper": up, "lower": lo}
                elif len(devs) == 1:
                    dv = devs[0]
                    text = f"<>{a.iso_fit_code.strip()}" + _stack(dv["upper_mm"], dv["lower_mm"])
                    tol = {"mode": "fit", "code": a.iso_fit_code, "upper": dv["upper_mm"], "lower": dv["lower_mm"]}
                else:
                    text = f"<>{a.iso_fit_code.strip()}"
                    tol = {"mode": "fit_pair", "code": a.iso_fit_code, "hole": [devs[0]["upper_mm"], devs[0]["lower_mm"]], "shaft": [devs[1]["upper_mm"], devs[1]["lower_mm"]]}
                src = f"ISO 286 / GB/T 1800 表（公称尺寸 {nom:g}）"
            else:
                if cat == "symmetric":
                    v = a.upper_deviation if a.upper_deviation is not None else a.lower_deviation
                    _need(v is not None, "对称偏差需要 upper_deviation（±值）")
                    v = abs(v)
                    text, tol, src = f"<>±{v:g}", {"mode": "symmetric", "upper": v, "lower": -v}, "用户给出"
                else:
                    _need(a.upper_deviation is not None and a.lower_deviation is not None, f"{'极限尺寸' if cat == 'limits' else '极限偏差'}需要 upper_deviation 和 lower_deviation（或给 iso_fit_code）")
                    if a.lower_deviation > a.upper_deviation:
                        raise ToolError("INVALID_ARGS", "下偏差不能大于上偏差", "")
                    if cat == "limits":
                        text = "{\\S" + f"{nom + a.upper_deviation:.3f}^{nom + a.lower_deviation:.3f}" + ";}"
                        tol = {"mode": "limits", "upper": a.upper_deviation, "lower": a.lower_deviation}
                    else:
                        text, tol = "<>" + _stack(a.upper_deviation, a.lower_deviation), {"mode": "deviation", "upper": a.upper_deviation, "lower": a.lower_deviation}
                    src = "用户给出"
            ctx.update(e, {**e.data, "text": text, "tol": tol})
            return ctx.result(f"{e.id}（{nom:g}）加公差：{a.iso_fit_code or ''} {tol.get('upper', '')}/{tol.get('lower', '')}（{src}）", dim_id=e.id, tolerance=tol)
        # ---- 图形类：形位公差 / 基准 / 粗糙度 ----
        tgt = ctx.entity(a.target_dim_or_entity_id) if a.target_dim_or_entity_id else None
        lay = ctx.layer_name(a.layer) if a.layer else None
        group = _next_group(d)
        if cat == "geometric_gdt":
            _need(a.gdt_symbol, "形位公差需要 gdt_symbol")
            _need(a.gdt_value is not None, "形位公差需要 gdt_value（公差值，mm）", "公差值必须由用户给出")
            datums = [x.strip().upper() for x in (a.datum_refs or [])]
            if a.gdt_symbol in mech.FORM_ONLY and datums:
                raise ToolError("INVALID_ARGS", f"{mech.GDT_NAMES[a.gdt_symbol]}是形状公差，不应有基准", "去掉 datum_refs")
            if a.gdt_symbol in mech.NEEDS_DATUM and not datums:
                raise ToolError("MISSING_ARG", f"{mech.GDT_NAMES[a.gdt_symbol]}需要基准（datum_refs，如 [\"A\"]）", "向用户确认基准")
            if a.diameter_zone and a.gdt_symbol not in ("straightness", "position", "concentricity", "parallelism", "perpendicularity", "angularity", "circular_runout"):
                ctx.warnings.append(f"{mech.GDT_NAMES[a.gdt_symbol]}一般不用 ⌀ 公差带，请确认")
            tp = ctx.point(a.insert_point) if a.insert_point is not None else None
            if tp is None:
                _need(tgt is not None, "需要 insert_point（框格位置）或 target_dim_or_entity_id（被测要素，框格放在它上方）")
                ax, ay = anchor_of(ctx, tgt)
                tp = (ax + 12.0, ay + 12.0, 0.0)
                ctx.notes.append(f"框格位置未指定，放在被测要素定位点 ({ax:g},{ay:g}) 的右上方 12 mm")
            ops, bb = mech.gdt_frame_ops(tp, a.gdt_symbol, f"{a.gdt_value:g}", a.diameter_zone, datums)
            if tgt is not None:
                ax, ay = anchor_of(ctx, tgt)
                ops += mech.gdt_leader_ops(bb, (ax, ay))
            ids = add_ops(ctx, [{**o, "role": "thin"} for o in ops], group, lay)
            return ctx.result(f"形位公差框格：{mech.GDT_NAMES[a.gdt_symbol]} {'⌀' if a.diameter_zone else ''}{a.gdt_value:g}" + (f" | {' | '.join(datums)}" if datums else "") + f"（{len(ids)} 个图元，组 {group}）",
                              group=group, frame_bbox=[round(v, 3) for v in bb], notes=["TOLERANCE 实体未生成：框格用线和文字绘制，预览和 DXF 里都可见"])
        if cat == "datum_feature":
            _need(a.datum_letter and re.fullmatch(r"[A-Za-z]", a.datum_letter), "基准符号需要 datum_letter（一个字母）")
            if a.insert_point is not None:
                tp = ctx.point(a.insert_point)
            else:
                _need(tgt is not None, "需要 insert_point 或 target_dim_or_entity_id（基准要素）")
                tp = anchor_of(ctx, tgt)
            ids = add_ops(ctx, [{**o, "role": "outline" if o["kind"] == "hatch" else "thin"} for o in mech.datum_symbol_ops(tp, a.datum_letter.upper())], group, lay)
            return ctx.result(f"基准符号 {a.datum_letter.upper()}（组 {group}）", group=group)
        # surface_roughness
        _need(a.roughness_value is not None, "表面粗糙度需要 roughness_value", "粗糙度数值必须由用户给出")
        if a.insert_point is not None:
            tp = ctx.point(a.insert_point)
        else:
            _need(tgt is not None, "需要 insert_point（符号尖点）或 target_dim_or_entity_id（被标注的表面）")
            tp = anchor_of(ctx, tgt)
        ok, series = mech.nice_roughness(a.roughness_param, a.roughness_value)
        if not ok:
            ctx.warnings.append(f"{a.roughness_param} {a.roughness_value:g} 不在 GB/T 1031 优先数系列里（{series}），请确认")
        ids = add_ops(ctx, mech.roughness_ops(tp, a.roughness_param, f"{a.roughness_value:g}", a.process_symbol), group, lay)
        return ctx.result(f"粗糙度符号 {a.roughness_param} {a.roughness_value:g}（{ {'any': '任意方法', 'remove': '去除材料', 'no_remove': '不去除材料'}[a.process_symbol] }，组 {group}）", group=group, ids=ids)
    except GeomError as ex:
        if ex.code == "DEVIATION_UNKNOWN":
            raise ToolError("DEVIATION_UNKNOWN", ex.message, "请直接问用户要上、下偏差（mm），然后用 tolerance_category=deviation 重新调用；不要自己编偏差", **ex.extra)
        raise to_tool_error(ex)


# ------------------------------------------------------------------ 44 invoke_engineering_toolset
class ToolsetArgs(Args):
    domain: Literal["mechanical", "architectural", "chem", "water"]
    tool_name: Optional[Literal["centermark_centerline", "shaft_generator", "hole_thread_feature", "standard_fastener", "flange_generator", "bolt_circle", "keyway", "bearing_simplified",
                                "gear_generator", "arch_grid_and_wall", "door_window", "bom_balloon_and_table"]] = None
    target_entity_ids: Ids = None
    insert_point: Pt = None
    spec_params: Optional[dict] = None


@tool("invoke_engineering_toolset", "engineering_specialty", ToolsetArgs, quiet_if=lambda a: a.domain in ("chem", "water"))
def invoke_engineering_toolset(ctx: Ctx, a: ToolsetArgs) -> dict:
    """调用专业工具集生成器（画化工设备图不用它，用 vessel_define）。机械：中心线、阶梯轴、孔与螺纹、标准件（六角螺栓/螺母/垫圈/内六角螺钉，取自标准表）、法兰、螺栓圆、键槽（GB/T 1095 表）、轴承通用画法、齿轮端视图、明细表与序号球；建筑：轴网与墙体、门窗、明细表。化工/给水：不在通用图里画，新建一张流程图（pfd 模式）并返回其 drawing_id。尺寸一律来自 spec_params（用户给）或标准表，缺了就反问。"""
    from zhihui.cad import toolsets
    d = ctx.d
    if a.domain in toolsets.PFD_DOMAINS:
        svc = ctx.svc
        if svc is None:
            raise ToolError("INTERNAL", "没有文件库，不能新建流程图", "")
        title = (a.spec_params or {}).get("title") or ("给水处理工艺流程图" if a.domain == "water" else "工艺流程图")
        res = svc.create_drawing(title=title, template_id="water_elevation" if a.domain == "water" else "pfd_default")
        if not res.get("ok"):
            raise ToolError(res.get("error_code", "E_CREATE"), res.get("message", "新建流程图失败"), res.get("hint", ""))
        return ctx.result(f"{'给水' if a.domain == 'water' else '化工'}流程图不在通用图里画：已新建流程图 {res['drawing_id']}（pfd 模式），请在新图里用流程图工具（add_equipment、connect_ports 等）继续",
                          switch_to={"drawing_id": res["drawing_id"], "mode": "pfd", "domain": a.domain})
    _need(a.tool_name, "需要 tool_name", f"{a.domain} 可用：{sorted(toolsets.DOMAIN_TOOLS[a.domain])}")
    tools = toolsets.DOMAIN_TOOLS[a.domain]
    if a.tool_name not in tools:
        raise ToolError("INVALID_ARGS", f"{a.domain} 领域没有 {a.tool_name}", f"可用：{sorted(tools)}；必填参数：{ {k: v[1] for k, v in tools.items()} }")
    gen, req = tools[a.tool_name]
    targets = [{"id": e.id, "kind": e.kind, "data": e.data} for e in ctx.entities(a.target_entity_ids)] if a.target_entity_ids else []
    needs_point = a.tool_name != "centermark_centerline"
    if needs_point:
        _need(a.insert_point is not None, "需要 insert_point（插入点）", "向用户确认放在哪里")
        ins = ctx.point(a.insert_point)
    else:
        ins = (0.0, 0.0, 0.0)
    try:
        r = gen(dict(a.spec_params or {}), (ins[0], ins[1]), targets)
    except GeomError as ex:
        if ex.code in ("MISSING_ARG", "SIZE_UNKNOWN", "KEYWAY_UNKNOWN"):
            raise ToolError(ex.code, ex.message, f"{ex.hint}（{a.tool_name} 必填：{req}）", **ex.extra)
        raise to_tool_error(ex)
    group = _next_group(d)
    ids = add_ops(ctx, r["ops"], group, None, [f"toolset:{a.tool_name}"])
    d.toolset = a.domain
    for n in r["info"].get("notes", []):
        if n:
            ctx.notes.append(n)
    if r["info"].get("opening"):                           # 门窗：在带 wall 标签的线上开洞
        cut = _cut_walls(ctx, r["info"]["opening"])
        r["summary"] += f"；在 {cut} 条墙线上开了洞口" if cut else "；没有找到带 wall 标签的墙线，未开洞"
    info = {k: v for k, v in r["info"].items() if k not in ("notes", "opening")}
    return ctx.result(f"{r['summary']}（{len(ids)} 个图元，组 {group}）", group=group, ids=ids, **info)


def _cut_walls(ctx: Ctx, opening: list) -> int:
    """用洞口矩形切断带 wall 标签的线（只处理直线）。"""
    from shapely.geometry import LineString, Polygon
    poly = Polygon([(x, y) for x, y in opening]).buffer(1e-6)
    n = 0
    for e in [x for x in ctx.d.entities if x.kind == "line" and "wall" in x.tags]:
        ls = LineString([(e.data["p1"][0], e.data["p1"][1]), (e.data["p2"][0], e.data["p2"][1])])
        if not ls.intersects(poly):
            continue
        diff = ls.difference(poly)
        parts = [g for g in (diff.geoms if hasattr(diff, "geoms") else [diff]) if not g.is_empty and g.length > 1e-6]
        keep = [g for g in parts if g.geom_type == "LineString"]
        if len(keep) == 1 and abs(keep[0].length - ls.length) < 1e-9:
            continue
        for i, g in enumerate(keep):
            (x0, y0), (x1, y1) = g.coords[0], g.coords[-1]
            if i == 0:
                ctx.update(e, {**e.data, "p1": [x0, y0, 0.0], "p2": [x1, y1, 0.0]})
            else:
                ctx.add("line", {"p1": [x0, y0, 0.0], "p2": [x1, y1, 0.0]}, e.layer, list(e.tags), src=e)
        if not keep:
            ctx.remove(e)
        n += 1
    return n


# ------------------------------------------------------------------ 45 manage_symbol_library
class SymbolArgs(Args):
    action: Literal["list_categories", "search_symbols", "insert_symbol", "register_custom_symbol", "import", "export", "delete"]
    category: Optional[Literal["mech_weld", "mech_surface", "mech_gdt", "arch_annotation", "electrical", "hydraulic_pid", "chem_equipment", "water"]] = None
    symbol_keyword_or_id: Optional[str] = None
    insert_point: Pt = None
    attach_to_entity_id: Optional[str] = None
    scale: float = Field(1.0, gt=0)
    rotation_deg: float = 0.0
    tag_attributes: Optional[dict] = None
    source_block_name: Optional[str] = None


def _ensure_block(ctx: Ctx, sym: dict) -> tuple[str, dict]:
    d = ctx.d
    name = f"SYM_{sym['id']}"
    blk = next((b for b in d.blocks if b["name"] == name), None)
    if blk is not None:
        return name, blk
    ents, attdefs = [], []
    if sym.get("block"):                                                # 自定义：直接带块定义
        for i, be in enumerate(sym["block"]["entities"]):
            lay = be.get("layer", "0")
            if d.get_layer(lay) is None:
                d.layers.append(Layer(name=lay))
                ctx.notes.append(f"新建图层「{lay}」（符号带来的图层）")
            ents.append({**copy.deepcopy(be), "id": f"E{i + 1}", "layer": lay})
        attdefs = copy.deepcopy(sym["block"].get("attdefs", []))
        base = sym["block"].get("base_point", [0, 0, 0])
    else:
        for i, o in enumerate(sym["ops"]):
            role = o.get("role", "text" if o["kind"] == "text" else "outline")
            data = copy.deepcopy(o["data"])
            if o["kind"] == "text" and not data.get("style"):
                data["style"] = d.current_text_style
            ents.append({"id": f"E{i + 1}", "kind": o["kind"], "data": data, "layer": role_layer(ctx, role), "tags": list(o.get("tags", []))})
        base = [0.0, 0.0, 0.0]
        if sym.get("pfd"):
            attdefs = [{"tag": "TAG", "prompt": "位号", "default_val": "", "position": [0.0, -8.0, 0.0], "height": 2.5, "invisible": False}]
    blk = {"name": name, "base_point": base, "entities": ents, "attdefs": attdefs, "symbol": sym["id"]}
    d.blocks.append(blk)
    return name, blk


@tool("manage_symbol_library", "engineering_specialty", SymbolArgs)
def manage_symbol_library(ctx: Ctx, a: SymbolArgs) -> dict:
    """符号库管理：list_categories、search_symbols（按关键词/别名/类别）、insert_symbol（落成块 SYM_<id>，可附着到图元、缩放旋转、填属性）、register_custom_symbol（把已有块注册成自定义符号，存到 data/symbols/custom/）、import（导入符号 JSON 或 DXF 里的块）、export（导出符号 JSON）、delete（只能删自定义符号）。类别：机械焊接/表面/形位公差、建筑标注、电气、液压、化工设备、给水。"""
    from zhihui.cad import symbols_general as SG
    d = ctx.d
    act = a.action
    try:
        if act == "list_categories":
            allsym = SG.all_symbols()
            rows = [{"category": k, "name": v, "count": sum(1 for s in allsym.values() if s["category"] == k)} for k, v in SG.CATEGORIES.items()]
            return ctx.result(f"{len(rows)} 个类别，共 {len(allsym)} 个符号", categories=rows)
        if act == "search_symbols":
            rows = SG.search(a.symbol_keyword_or_id or "", a.category)
            return ctx.result(f"找到 {len(rows)} 个符号" + (f"（类别 {a.category}）" if a.category else ""), symbols=rows)
        if act == "insert_symbol":
            _need(a.symbol_keyword_or_id, "需要 symbol_keyword_or_id（符号 id 或关键词）")
            allsym = SG.all_symbols()
            sym = allsym.get(a.symbol_keyword_or_id)
            if sym is None:
                rows = SG.search(a.symbol_keyword_or_id, a.category)
                if not rows:
                    raise ToolError("SYMBOL_NOT_FOUND", f"符号库里没有「{a.symbol_keyword_or_id}」", "先 search_symbols 看有哪些；没有的可以让用户提供或用 register_custom_symbol 登记")
                top = rows[0]
                ties = [r for r in rows if r["name"] == top["name"] or r["id"] == top["id"]]
                exact = [r for r in rows if a.symbol_keyword_or_id.lower() in (r["name"].lower(), *[x.lower() for x in r["aliases"]])]
                if len(exact) == 1:
                    sym = allsym[exact[0]["id"]]
                elif len(rows) == 1:
                    sym = allsym[rows[0]["id"]]
                else:
                    raise ToolError("SYMBOL_AMBIGUOUS", f"「{a.symbol_keyword_or_id}」匹配到 {len(rows)} 个符号，请指定", "让用户从候选里选一个，用符号 id 重新调用", candidates=rows[:8])
            if a.attach_to_entity_id:
                tgt = ctx.entity(a.attach_to_entity_id)
                ip = anchor_of(ctx, tgt)
                ip = (ip[0], ip[1], 0.0)
            else:
                _need(a.insert_point is not None, "需要 insert_point 或 attach_to_entity_id", "向用户确认放在哪里")
                ip = ctx.point(a.insert_point)
            name, blk = _ensure_block(ctx, sym)
            tags = a.tag_attributes or {}
            have = {x["tag"].upper() for x in blk.get("attdefs", [])}
            bad = [k for k in tags if k.upper() not in have]
            if bad:
                raise ToolError("ATTRIB_NOT_FOUND", f"符号「{sym['name_cn']}」没有属性 {bad}", f"可填属性：{sorted(have) or '无（这个符号不带属性）'}")
            e = ctx.add("insert", {"block": name, "p": pl(ip), "scale": [a.scale, a.scale, a.scale], "rotation": a.rotation_deg,
                                   "attribs": {x["tag"]: str(tags.get(x["tag"], tags.get(x["tag"].upper(), x.get("default_val", "")))) for x in blk.get("attdefs", [])}}, None, ["symbol", f"symbol:{sym['id']}"])
            return ctx.result(f"插入符号「{sym['name_cn']}」{e.id}（块 {name}）于 ({ip[0]:g},{ip[1]:g})，比例 {a.scale:g}，旋转 {a.rotation_deg:g}°", insert_id=e.id, block=name, symbol=sym["id"])
        if act == "register_custom_symbol":
            _need(a.symbol_keyword_or_id, "需要 symbol_keyword_or_id（新符号的 id）")
            _need(a.source_block_name, "需要 source_block_name（已有的块）")
            _need(a.category, "需要 category（符号类别）")
            sid = SG.valid_id(a.symbol_keyword_or_id)
            if sid in SG.builtins() or sid in SG.pfd_symbols():
                raise ToolError("SYMBOL_EXISTS", f"符号 id「{sid}」是内置符号，不能覆盖", "换一个 id")
            blk = next((b for b in d.blocks if b["name"].lower() == a.source_block_name.lower()), None)
            if blk is None:
                raise ToolError("BLOCK_NOT_FOUND", f"没有块「{a.source_block_name}」", f"现有块：{[b['name'] for b in d.blocks]}")
            if blk.get("xref"):
                raise ToolError("INVALID_ARGS", "外部参照不能注册成符号", "先 bind")
            sym = {"id": sid, "name_cn": a.symbol_keyword_or_id, "category": a.category, "aliases": [blk["name"]], "desc": f"由块 {blk['name']} 注册", "source": "用户自定义",
                   "block": {"base_point": blk.get("base_point", [0, 0, 0]), "entities": [{k: v for k, v in be.items() if k != "id"} for be in blk["entities"]], "attdefs": blk.get("attdefs", [])}}
            p = SG.save_custom(sym)
            return ctx.result(f"块「{blk['name']}」已注册为自定义符号 {sid}（{SG.CATEGORIES[a.category]}），保存在 {p.name}", symbol=sid)
        if act == "import":
            _need(a.symbol_keyword_or_id, "import 需要 symbol_keyword_or_id（符号 JSON 或 DXF 的 file_ref）")
            try:
                path = ctx.svc.store.resolve_ref(a.symbol_keyword_or_id)
            except PermissionError:
                raise ToolError("E_FORBIDDEN", "file_ref 越界", "只能使用任务给出的 file_ref")
            if not path.exists():
                raise ToolError("E_FILE_NOT_FOUND", f"找不到文件 {a.symbol_keyword_or_id}", "")
            if path.suffix.lower() == ".json":
                sym = json.loads(path.read_text(encoding="utf-8"))
                for k in ("id", "name_cn", "category"):
                    if k not in sym:
                        raise ToolError("E_FILE_REJECTED", f"符号文件缺少 {k}", "")
                if sym["category"] not in SG.CATEGORIES or not (sym.get("block") or sym.get("ops")):
                    raise ToolError("E_FILE_REJECTED", "符号文件的类别不对或没有图形", "")
                SG.valid_id(sym["id"])
                if sym["id"] in SG.builtins() or sym["id"] in SG.pfd_symbols():
                    raise ToolError("SYMBOL_EXISTS", f"符号 id「{sym['id']}」是内置符号，不能覆盖", "改 id 后再导入")
                SG.save_custom(sym)
                return ctx.result(f"已导入符号 {sym['id']}（{sym['name_cn']}）", symbol=sym["id"])
            if path.suffix.lower() == ".dxf":
                _need(a.source_block_name, "从 DXF 导入需要 source_block_name（块名）与 category", "")
                _need(a.category, "从 DXF 导入需要 category")
                from zhihui.cad.general_dxf import read_any_dxf
                sub = read_any_dxf(path)
                blk = next((b for b in sub.blocks if b["name"].lower() == a.source_block_name.lower()), None)
                if blk is None:
                    raise ToolError("BLOCK_NOT_FOUND", f"DXF 里没有块「{a.source_block_name}」", f"有：{[b['name'] for b in sub.blocks]}")
                sid = SG.valid_id(re.sub(r"\W", "_", blk["name"]))
                sym = {"id": sid, "name_cn": blk["name"], "category": a.category, "aliases": [], "desc": f"从 {path.name} 导入", "source": "用户导入",
                       "block": {"base_point": blk.get("base_point", [0, 0, 0]), "entities": [{k: v for k, v in be.items() if k != "id"} for be in blk["entities"]], "attdefs": blk.get("attdefs", [])}}
                SG.save_custom(sym)
                return ctx.result(f"已从 {path.name} 导入块「{blk['name']}」为符号 {sid}", symbol=sid)
            raise ToolError("E_FILE_REJECTED", "只能导入 .json 或 .dxf", "")
        if act == "export":
            _need(a.symbol_keyword_or_id, "需要 symbol_keyword_or_id（符号 id）")
            sym = SG.all_symbols().get(a.symbol_keyword_or_id)
            if sym is None:
                raise ToolError("SYMBOL_NOT_FOUND", f"没有符号 {a.symbol_keyword_or_id}", "")
            out = ctx.export_dir() / f"symbol_{sym['id']}.json"
            out.write_text(json.dumps({k: v for k, v in sym.items() if k not in ("builtin", "pfd")}, ensure_ascii=False, indent=1), encoding="utf-8")
            return ctx.result(f"符号 {sym['id']} 已导出", file=ctx.file_info(out))
        if act == "delete":
            _need(a.symbol_keyword_or_id, "需要 symbol_keyword_or_id（符号 id）")
            cs = SG.custom_symbols()
            if a.symbol_keyword_or_id not in cs:
                if a.symbol_keyword_or_id in SG.all_symbols():
                    raise ToolError("SYMBOL_BUILTIN", f"{a.symbol_keyword_or_id} 是内置符号，不能删除", "只能删除自定义符号")
                raise ToolError("SYMBOL_NOT_FOUND", f"没有符号 {a.symbol_keyword_or_id}", "")
            (SG.custom_dir() / f"{a.symbol_keyword_or_id}.json").unlink()
            return ctx.result(f"已删除自定义符号 {a.symbol_keyword_or_id}（已插入图中的块保留）")
    except GeomError as ex:
        raise to_tool_error(ex)
    raise ToolError("INVALID_ARGS", f"未知操作 {act}", "")
