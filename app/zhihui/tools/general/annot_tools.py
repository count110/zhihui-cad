"""三、注释（函数 28–32：文字、文字样式、线性/角度标注、半径直径连续标注、标注样式）。"""
from __future__ import annotations

import math
import re
from typing import Any, Literal, Optional

from pydantic import Field

from zhihui.cad.geom import dims, presets
from zhihui.cad.geom.entities import entity_bbox, P, ang, dist, pl, primitives
from zhihui.tools.core import ToolError
from .ctx import Ctx
from .registry import Args, Ids, Pt, tool
from . import edit_tools  # noqa: F401


def _need(cond, msg: str, hint: str = "缺少关键参数，向用户确认后再执行") -> None:
    if not cond:
        raise ToolError("MISSING_ARG", msg, hint)


def convert_symbols(text: str) -> str:
    """%%c → ⌀，%%d → °，%%p → ±（AutoCAD 控制码）。"""
    return re.sub(r"%%[cC]", "⌀", re.sub(r"%%[dD]", "°", re.sub(r"%%[pP]", "±", text)))


def _pref_layer(ctx: Ctx, layer: str | None, names: tuple[str, ...]) -> str | None:
    if layer:
        return layer
    for n in names:
        if ctx.d.get_layer(n):
            return n
    return None


# ------------------------------------------------------------------ 28 create_text_annotation
class TextArgs(Args):
    text_type: Literal["single_line", "mtext"] = "single_line"
    content: Optional[str] = None
    insert_point: Pt = None
    height: Optional[float] = Field(None, gt=0)
    rotation_deg: float = 0.0
    width_box: Optional[float] = Field(None, gt=0)
    line_spacing: float = Field(1.0, gt=0)
    alignment: Literal["left", "center", "right", "top_left", "top_center", "top_right", "middle_center", "bottom_left"] = "left"
    style_name: Optional[str] = None
    bg_mask: bool = False
    layer: Optional[str] = None
    action: Literal["create", "edit"] = "create"
    entity_id: Optional[str] = None


@tool("create_text_annotation", "annotation_dimension", TextArgs)
def create_text_annotation(ctx: Ctx, a: TextArgs) -> dict:
    """创建或编辑单行/多行文字。自动转换 %%c→⌀、%%d→°、%%p→±；字高默认 3.5，样式默认当前文字样式，中文沿用仿宋长字体。action=edit 时按 entity_id 修改内容和属性。"""
    style = a.style_name or ctx.d.current_text_style
    if a.style_name and not any(s["name"].lower() == a.style_name.lower() for s in ctx.d.text_styles) and a.style_name.lower() != str(ctx.d.current_text_style).lower():
        names = [s["name"] for s in ctx.d.text_styles]
        raise ToolError("STYLE_NOT_FOUND", f"文字样式「{a.style_name}」不存在", f"已有样式：{'、'.join(names) or '（无）'}；先用 manage_text_style 建立", styles=names)
    if a.action == "edit":
        _need(a.entity_id, "编辑文字需要 entity_id")
        e = ctx.entity(a.entity_id, for_edit=True)
        if e.kind not in ("text", "mtext"):
            raise ToolError("INVALID_ARGS", f"{e.id} 不是文字", "")
        d = dict(e.data)
        if a.content is not None:
            d["text"] = convert_symbols(a.content)
        if a.height:
            d["height"] = a.height
        if a.insert_point is not None:
            d["p"] = pl(ctx.point(a.insert_point))
        if a.style_name:
            d["style"] = a.style_name
        d["rotation"] = a.rotation_deg if "rotation_deg" in a.model_fields_set else d.get("rotation", 0.0)
        ctx.update(e, d)
        return ctx.result(f"修改了文字 {e.id}：「{d['text'][:20]}」")
    _need(a.content, "文字需要 content（文字内容）", "向用户确认要写的文字")
    _need(a.insert_point is not None, "文字需要 insert_point（插入点）", "向用户确认文字位置")
    h = a.height
    if h is None:
        h = 3.5
        ctx.notes.append("文字高度使用默认值 3.5")
    p = ctx.point(a.insert_point)
    txt = convert_symbols(a.content)
    layer = _pref_layer(ctx, a.layer, ("文字", "Text"))
    if a.text_type == "single_line":
        e = ctx.add("text", {"p": pl(p), "text": txt.replace("\n", " "), "height": h, "rotation": a.rotation_deg, "style": style, "align": a.alignment}, layer)
    else:
        e = ctx.add("mtext", {"p": pl(p), "text": txt, "height": h, "width": a.width_box or 0.0, "rotation": a.rotation_deg, "line_spacing": a.line_spacing, "attach": a.alignment,
                              "bg_mask": a.bg_mask, "style": style}, layer)
    return ctx.result(f"写了{'单行' if a.text_type == 'single_line' else '多行'}文字 {e.id}：「{txt[:20]}」，字高 {h:g}，样式 {style}")


# ------------------------------------------------------------------ 29 manage_text_style
class TextStyleArgs(Args):
    style_name: str
    font_file: Optional[str] = None
    bigfont_file: Optional[str] = None
    fixed_height: Optional[float] = Field(None, ge=0)
    width_factor: Optional[float] = Field(None, gt=0, le=10)
    oblique_angle_deg: Optional[float] = Field(None, ge=-85, le=85)
    annotative: Optional[bool] = None
    set_current: bool = False


@tool("manage_text_style", "annotation_dimension", TextStyleArgs)
def manage_text_style(ctx: Ctx, a: TextStyleArgs) -> dict:
    """创建/修改文字样式：字体文件（gbenor.shx、仿宋…）、大字体（gbcbig.shx）、固定字高、宽度因子（长仿宋 0.7）、倾斜角、注释性；set_current 设为当前。"""
    d = ctx.d
    st = next((s for s in d.text_styles if s["name"].lower() == a.style_name.lower()), None)
    if st is None:
        base = presets.TEXT_STYLES.get(a.style_name) or {"name": a.style_name, "font_file": "仿宋", "bigfont_file": "", "fixed_height": 0.0, "width_factor": 0.7, "oblique_angle_deg": 0.0, "annotative": False}
        st = dict(base)
        st["name"] = a.style_name
        d.text_styles.append(st)
    for src, key in (("font_file", "font_file"), ("bigfont_file", "bigfont_file"), ("fixed_height", "fixed_height"), ("width_factor", "width_factor"), ("oblique_angle_deg", "oblique_angle_deg"), ("annotative", "annotative")):
        v = getattr(a, src)
        if v is not None:
            st[key] = v
    if a.set_current:
        d.current_text_style = st["name"]
    return ctx.result(f"文字样式「{st['name']}」：字体 {st['font_file']}{'+' + st['bigfont_file'] if st.get('bigfont_file') else ''}，宽度因子 {st['width_factor']:g}" + ("，已设为当前" if a.set_current else ""), style=st)


# ------------------------------------------------------------------ 30 create_linear_and_angular_dim
class LinearDimArgs(Args):
    dim_type: Literal["linear_horizontal", "linear_vertical", "linear_auto", "aligned", "angular"]
    ext_line1_point: Pt = None
    ext_line2_point: Pt = None
    target_entity_id: Optional[str] = None
    edge_index: Optional[int] = Field(None, ge=0)
    dim_line_location: Pt = None
    angle_entity_ids: Ids = None
    text_override: Optional[str] = None
    dimstyle: Optional[str] = None
    layer: Optional[str] = None


def _style(ctx: Ctx, name: str | None) -> dict:
    n = name or ctx.d.current_dim_style
    st = next((s for s in ctx.d.dim_styles if s["name"].lower() == n.lower()), None)
    if st is None:
        st = next((dict(v) for v in presets.DIM_STYLES.values() if v["name"].lower() == n.lower()), None)
        if st is None and not ctx.d.dim_styles:
            st = dict(presets.DIM_STYLES["ISO_25"])
        if st is None:
            raise ToolError("DIMSTYLE_NOT_FOUND", f"标注样式「{n}」不存在", f"现有：{[s['name'] for s in ctx.d.dim_styles]}；用 manage_dim_style 创建")
        ctx.d.dim_styles.append(st)
    return st


def _dim_layer(ctx: Ctx, layer):
    return _pref_layer(ctx, layer, ("尺寸标注", "Dimensions", "标注"))


@tool("create_linear_and_angular_dim", "annotation_dimension", LinearDimArgs)
def create_linear_and_angular_dim(ctx: Ctx, a: LinearDimArgs) -> dict:
    """标注线性（水平/垂直/自动）、对齐尺寸和角度。两个界线起点，或 target_entity_id（直线；多段线用 edge_index 指明哪条边）；dim_line_location 为空时自动避让放置；角度用 angle_entity_ids（两条直线）。返回 data.dim_id 和 measurement。"""
    d = ctx.d
    st = _style(ctx, a.dimstyle)
    layer = _dim_layer(ctx, a.layer)
    existing = [e.data for e in d.entities if e.kind == "dimension"]
    if a.dim_type == "angular":
        _need(a.angle_entity_ids and len(a.angle_entity_ids) == 2, "角度标注需要两条直线 angle_entity_ids")
        ls = []
        for i in a.angle_entity_ids:
            e = ctx.entity(i)
            pr = primitives(e)
            if not pr or not hasattr(pr[0], "a"):
                raise ToolError("INVALID_ARGS", f"{e.id} 不是直线", "")
            ls.append((pr[0].a, pr[0].b))
        meas, ip = dims.lines_angle(*ls)
        r = max(dist(ip, x) for l in ls for x in l) * 0.6
        loc = ctx.point(a.dim_line_location) if a.dim_line_location is not None else (ip[0] + r, ip[1] + r * 0.3, 0.0)
        data = {"dtype": "angular", "l1": [pl(ls[0][0]), pl(ls[0][1])], "l2": [pl(ls[1][0]), pl(ls[1][1])], "center": pl(ip), "p1": pl(ls[0][1]), "p2": pl(ls[1][1]), "loc": pl(loc),
                "text": a.text_override or "", "style": st["name"], "measurement": round(meas, 6)}
    else:
        if a.target_entity_id:
            e = ctx.entity(a.target_entity_id)
            prs = primitives(e)
            if not prs:
                raise ToolError("INVALID_ARGS", f"{e.id} 没有可标注的边", "")
            bb = entity_bbox(e)
            if e.kind == "polyline" and a.edge_index is None and a.dim_type in ("linear_horizontal", "linear_vertical") and bb and len(prs) > 1:
                # 对整个图形标“长/宽”：量包围盒的总长总宽（带圆角的矩形也是 120×80，不是被圆角截短的那段直边）
                p1, p2 = ((bb[0], bb[1], 0.0), (bb[2], bb[1], 0.0)) if a.dim_type == "linear_horizontal" else ((bb[2], bb[1], 0.0), (bb[2], bb[3], 0.0))
            else:
                if e.kind == "polyline" and a.edge_index is None and len(prs) > 1:
                    raise ToolError("AMBIGUOUS", f"{e.id} 有 {len(prs)} 条边，要标哪一条？", "给 edge_index（0 起），或直接给两个界线起点；标总长总宽用 linear_horizontal / linear_vertical", edges=len(prs))
                pr = prs[(a.edge_index or 0) % len(prs)]
                if not hasattr(pr, "a"):
                    raise ToolError("INVALID_ARGS", "线性标注的对象必须是直线段（圆和弧用半径/直径标注）", "")
                p1, p2 = pr.a, pr.b
        else:
            _need(a.ext_line1_point is not None and a.ext_line2_point is not None, "线性标注需要两个界线起点，或 target_entity_id")
            p1, p2 = ctx.point(a.ext_line1_point), ctx.point(a.ext_line2_point)
        if dist(p1, p2) < 1e-9:
            raise ToolError("DEGENERATE", "两个界线起点重合", "")
        dtype = dims.resolve_linear_type(p1, p2, a.dim_type)
        loc = ctx.point(a.dim_line_location) if a.dim_line_location is not None else dims.auto_location(dtype, p1, p2, existing, gap=st["text_height"] * 3, spacing=st.get("base_spacing", 7.0))
        meas = dims.linear_measure(p1, p2, dtype)
        data = {"dtype": dtype, "p1": pl(p1), "p2": pl(p2), "loc": pl(loc), "text": a.text_override or "", "style": st["name"], "measurement": round(meas, 6)}
    e = ctx.add("dimension", data, layer)
    if a.dim_line_location is None:
        ctx.notes.append("尺寸线位置自动避让放置")
    return ctx.result(f"标注 {e.id}：{a.text_override or dims.fmt_measure(data['measurement'], st['precision'])}" + ("°" if data["dtype"] == "angular" else ""), dim_id=e.id, measurement=data["measurement"])


# ------------------------------------------------------------------ 31 create_radial_and_chain_dim
class RadialDimArgs(Args):
    dim_type: Literal["radius", "diameter", "arc_length", "baseline", "continue"]
    target_entity_id: Optional[str] = None
    leader_point: Pt = None
    base_dim_id: Optional[str] = None
    chain_points: Optional[list[Pt]] = None
    dimstyle: Optional[str] = None
    layer: Optional[str] = None


@tool("create_radial_and_chain_dim", "annotation_dimension", RadialDimArgs)
def create_radial_and_chain_dim(ctx: Ctx, a: RadialDimArgs) -> dict:
    """标注半径、直径、弧长，以及基线标注 / 连续标注（需要 base_dim_id 和 chain_points，偏移量由程序计算）。leader_point 为空时自动放置。"""
    d = ctx.d
    st = _style(ctx, a.dimstyle)
    layer = _dim_layer(ctx, a.layer)
    t = a.dim_type
    if t in ("radius", "diameter", "arc_length"):
        _need(a.target_entity_id, f"{t} 标注需要 target_entity_id（圆或圆弧）")
        e = ctx.entity(a.target_entity_id)
        if e.kind not in ("circle", "arc") or (t == "arc_length" and e.kind != "arc"):
            raise ToolError("INVALID_ARGS", f"{e.id}（{e.kind}）不能做 {t} 标注", "半径/直径用于圆和圆弧，弧长只用于圆弧")
        c, r = P(e.data["c"]), float(e.data["r"])
        loc = ctx.point(a.leader_point) if a.leader_point is not None else dims.circle_leader_point(c, r)
        meas = r if t == "radius" else 2 * r if t == "diameter" else dims.arc_length(c, r, e.data["a0"], e.data["a1"])
        data = {"dtype": t, "center": pl(c), "radius": r, "p1": pl(c), "p2": pl(c), "loc": pl(loc), "target": e.id, "text": "", "style": st["name"], "measurement": round(meas, 6)}
        if t == "arc_length":
            data.update({"a0": e.data["a0"], "a1": e.data["a1"]})
        n = ctx.add("dimension", data, layer)
        if a.leader_point is None:
            ctx.notes.append("引线位置自动放置")
        prefix = {"radius": "R", "diameter": "⌀", "arc_length": "⌒"}[t]
        return ctx.result(f"标注 {n.id}：{prefix}{dims.fmt_measure(meas, st['precision'])}", dim_id=n.id, measurement=data["measurement"])
    _need(a.base_dim_id and a.chain_points, f"{t} 标注需要 base_dim_id 和 chain_points")
    base = ctx.entity(a.base_dim_id)
    if base.kind != "dimension" or not base.data.get("dtype", "").startswith(("linear", "aligned")):
        raise ToolError("INVALID_ARGS", f"{base.id} 不是线性或对齐标注，不能作基线/连续标注的基准", "")
    bd = base.data
    dtype = bd["dtype"]
    pts = [ctx.point(p) for p in a.chain_points]
    ids, meas = [], []
    spacing = st.get("base_spacing", 7.0)
    cur_start = P(bd["p1"]) if t == "baseline" else P(bd["p2"])
    loc_base = P(bd["loc"])
    for i, p in enumerate(pts):
        if t == "baseline":
            p1, p2 = P(bd["p1"]), p
            off = spacing * (i + 1)
        else:
            p1, p2 = cur_start, p
            off = 0.0
        shift = (0.0, off, 0.0) if dtype == "linear_horizontal" else (off, 0.0, 0.0) if dtype == "linear_vertical" else (0.0, 0.0, 0.0)
        if dtype == "aligned" and off:
            ar = math.radians(ang(p1, p2) + 90)
            shift = (off * math.cos(ar), off * math.sin(ar), 0.0)
        loc = (loc_base[0] + shift[0], loc_base[1] + shift[1], 0.0)
        if dtype == "linear_horizontal" and t == "continue":
            loc = (loc[0], loc_base[1], 0.0)
        m = dims.linear_measure(p1, p2, dtype)
        n = ctx.add("dimension", {"dtype": dtype, "p1": pl(p1), "p2": pl(p2), "loc": pl(loc), "text": "", "style": st["name"], "measurement": round(m, 6)}, layer or base.layer)
        ids.append(n.id)
        meas.append(round(m, 6))
        cur_start = p
    return ctx.result(f"{'基线' if t == 'baseline' else '连续'}标注 {len(ids)} 个：" + "、".join(dims.fmt_measure(m, st["precision"]) for m in meas), dim_ids=ids, dim_id=ids[-1], measurements=meas)


# ------------------------------------------------------------------ 32 manage_dim_style
class DimStyleArgs(Args):
    style_name: str
    standard_preset: Optional[Literal["GB_MECH_MM", "GB_ARCH_MM", "ISO_25"]] = None
    arrow_type: Optional[Literal["closed_filled", "architectural_tick", "dot"]] = None
    arrow_size: Optional[float] = Field(None, gt=0)
    text_height: Optional[float] = Field(None, gt=0)
    text_style: Optional[str] = None
    ext_offset: Optional[float] = Field(None, ge=0)
    ext_beyond: Optional[float] = Field(None, ge=0)
    precision: Optional[int] = Field(None, ge=0, le=8)
    dimscale: Optional[float] = Field(None, gt=0)
    tolerance_display: Optional[Literal["none", "symmetric", "deviation", "limits"]] = None
    set_current: bool = False
    apply_to_existing: bool = False


@tool("manage_dim_style", "annotation_dimension", DimStyleArgs)
def manage_dim_style(ctx: Ctx, a: DimStyleArgs) -> dict:
    """创建/修改标注样式：标准预设（GB_MECH_MM / GB_ARCH_MM / ISO_25）、箭头、字高、文字样式、界线偏移和超出、精度、全局比例、公差显示；set_current 设为当前，apply_to_existing 套用到已有标注。"""
    d = ctx.d
    st = next((s for s in d.dim_styles if s["name"].lower() == a.style_name.lower()), None)
    if st is None:
        base = presets.DIM_STYLES.get(a.standard_preset or "") or next((v for v in presets.DIM_STYLES.values() if v["name"].lower() == a.style_name.lower()), None) or presets.DIM_STYLES["GB_MECH_MM"]
        st = dict(base)
        st["name"] = a.style_name
        d.dim_styles.append(st)
    elif a.standard_preset:
        st.update({k: v for k, v in presets.DIM_STYLES[a.standard_preset].items() if k != "name"})
    for k in ("arrow_type", "arrow_size", "text_height", "text_style", "ext_offset", "ext_beyond", "precision", "dimscale", "tolerance_display"):
        v = getattr(a, k)
        if v is not None:
            st[k] = v
    if a.set_current:
        d.current_dim_style = st["name"]
    n = 0
    if a.apply_to_existing:
        for e in d.entities:
            if e.kind == "dimension":
                ctx.update(e, {**e.data, "style": st["name"]})
                n += 1
    return ctx.result(f"标注样式「{st['name']}」：箭头 {st['arrow_type']} {st['arrow_size']:g}，字高 {st['text_height']:g}，精度 {st['precision']}" + ("，已设为当前" if a.set_current else "") + (f"，套用到 {n} 个标注" if n else ""), style=st)
