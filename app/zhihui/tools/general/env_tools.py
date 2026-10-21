"""一、基础操作与绘图环境（函数 1–10，意图类别 environment_setup）。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import Field

from zhihui.cad.geom import presets
from zhihui.ir import Drawing, Layer
from zhihui.tools.core import ToolError
from .ctx import Ctx
from .registry import Args, Pt, tool

COLOR = Any   # ACI 整数 | "#RRGGBB" | [r,g,b] | 颜色名（红/黄/绿…）


BASIC_ACI = {"#FF0000": 1, "#FFFF00": 2, "#00FF00": 3, "#00FFFF": 4, "#0000FF": 5, "#FF00FF": 6}


def parse_color(v) -> tuple[int, str | None]:
    """→ (ACI, true_color 或 None)。正好等于 ACI 1–6 标准色的 RGB 写法按 ACI 存（“红色”写成 #FF0000 和写成 1 是同一个意思）。"""
    if v is None:
        return 7, None
    if isinstance(v, bool):
        raise ToolError("INVALID_ARGS", "颜色写法不对", "写 ACI 数字、#RRGGBB、[r,g,b] 或颜色名")
    if isinstance(v, int):
        if not 0 <= v <= 257:
            raise ToolError("INVALID_ARGS", f"ACI 颜色 {v} 超出 0–257", "")
        return v, None
    if isinstance(v, (list, tuple)) and len(v) == 3:
        r, g, b = (int(x) for x in v)
        if not all(0 <= x <= 255 for x in (r, g, b)):
            raise ToolError("INVALID_ARGS", "RGB 每个分量必须在 0–255", "")
        tc = f"#{r:02X}{g:02X}{b:02X}"
        return (BASIC_ACI[tc], None) if tc in BASIC_ACI else (7, tc)
    if isinstance(v, str):
        s = v.strip()
        if s.startswith("#") and len(s) == 7:
            int(s[1:], 16)
            return (BASIC_ACI[s.upper()], None) if s.upper() in BASIC_ACI else (7, s.upper())
        if s.lstrip("-").isdigit():
            return parse_color(int(s))
        if s.lower() in presets.COLOR_NAMES or s in presets.COLOR_NAMES:
            return presets.COLOR_NAMES.get(s, presets.COLOR_NAMES.get(s.lower())), None
    raise ToolError("INVALID_ARGS", f"无法识别的颜色：{v!r}", "写 ACI 数字、#RRGGBB、[r,g,b] 或颜色名（红、黄、绿、青、蓝、洋红、白）")


def norm_linetype(name: str | None) -> str | None:
    if not name:
        return None
    s = presets.LINETYPE_ALIASES.get(name.strip(), presets.LINETYPE_ALIASES.get(name.strip().lower(), name.strip()))
    for k in presets.KNOWN_LINETYPES:
        if k.lower() == s.lower():
            return k
    raise ToolError("INVALID_ARGS", f"线型「{name}」不可用", f"可用线型：{sorted(presets.KNOWN_LINETYPES - {'ByLayer', 'ByBlock'})}")


# ------------------------------------------------------------------ 1 manage_workspace_ui
class WorkspaceArgs(Args):
    workspace: Optional[Literal["2d_drafting", "annotation", "3d_modeling"]] = None
    show_panels: Optional[list[Literal["command_line", "toolbar", "layers", "properties", "status_bar"]]] = None
    explain_ui_layout: bool = False
    topic: str = ""


UI_HELP = {
    "command_line": "命令行：直接输入 AutoCAD 风格命令（如 L 0,0 @100,0）或中文口令，回车执行；Esc 取消。",
    "toolbar": "工具栏：绘图（直线、圆、矩形…）、修改（移动、复制、修剪…）、标注，点选后在图上取点。",
    "layers": "图层面板：开/关、冻结、锁定、颜色、线型、线宽，点“设为当前”后新图元画在该图层。",
    "properties": "特性面板：选中图元后显示并可编辑图层、颜色、线型、线宽和几何参数（半径、文字内容等）。",
    "status_bar": "状态栏：左下角显示光标坐标；SNAP 捕捉、GRID 栅格、ORTHO 正交、POLAR 极轴、OSNAP 对象捕捉、OTRACK 追踪、DYN 动态输入可点击开关。",
}


@tool("manage_workspace_ui", "environment_setup", WorkspaceArgs, quiet=True)
def manage_workspace_ui(ctx: Ctx, a: WorkspaceArgs) -> dict:
    """切换工作空间（二维草图/注释/三维建模）、显示或隐藏面板，并说明界面布局和命令行用法。对应前端的工作空间布局，不是 AutoCAD 的 Ribbon。"""
    d = ctx.d
    if a.workspace:
        d.workspace = a.workspace
    if a.show_panels is not None:
        d.view.panels = list(a.show_panels)
    out: dict = {"workspace": d.workspace, "panels": d.view.panels}
    if a.explain_ui_layout:
        topics = [a.topic] if a.topic in UI_HELP else list(UI_HELP)
        out["help"] = {k: UI_HELP[k] for k in topics}
        out["shortcuts"] = {"ORTHO": "F8", "SNAP": "F9", "GRID": "F7", "OSNAP": "F3", "POLAR": "F10", "DYN": "F12", "缩放": "滚轮", "平移": "空格+拖动", "全部显示": "Z E"}
    return ctx.result(f"工作空间：{d.workspace}；面板：{'、'.join(d.view.panels) or '无'}", **out)


# ------------------------------------------------------------------ 2 manage_document_file
class DocumentArgs(Args):
    action: Literal["new", "open", "save", "save_as", "close"]
    drawing_id: Optional[str] = None
    file_ref: Optional[str] = None
    template_name: str = "blank_a3"
    title: str = ""
    format: Literal["dxf", "dwg", "json", "stl"] = "dxf"
    dxf_version: Literal["R2018", "R2013", "R2010", "R2007"] = "R2018"


def new_general_drawing(template: str, title: str = "") -> Drawing:
    t = presets.TEMPLATES.get(template)
    if t is None:
        raise ToolError("TEMPLATE_NOT_FOUND", f"没有样板 {template}", f"可用样板：{sorted(presets.TEMPLATES)}")
    w, h = presets.PAPERS[t["paper"]]
    d = Drawing(id="dwg_pending", title=title or "通用图", mode="general", limits=(0, 0, float(w), float(h)), frame=False)
    d.layers = [Layer(name="0")]
    d.view.center, d.view.height = (w / 2, h / 2), float(h)
    if t.get("layers"):
        for spec in presets.LAYER_PRESETS[t["layers"]]:
            d.layers.append(Layer(**spec))
        d.current_layer = presets.PRESET_CURRENT[t["layers"]]
    if t.get("text"):
        d.text_styles = [dict(presets.TEXT_STYLES[t["text"]]), dict(presets.TEXT_STYLES["Standard"])]
        d.current_text_style = t["text"]
    if t.get("dim"):
        d.dim_styles = [dict(presets.DIM_STYLES[t["dim"]])]
        d.current_dim_style = presets.DIM_STYLES[t["dim"]]["name"]
    d.ltscale, _ = presets.nice_ltscale(d.limits)
    if t.get("equipment"):                                         # 化工设备图样板：标题默认“化工设备图”，另建图框层（参数和图由 vessel_define 生成）
        d.title = title or "化工设备图"
        d.workspace = "chem_equipment"
        d.layers.append(Layer(name="图框", color=7, lineweight_mm=0.5, description="图框与标题栏"))
    return d


@tool("manage_document_file", "environment_setup", DocumentArgs, needs_drawing=False, no_history=True)
def manage_document_file(svc, a: DocumentArgs) -> dict:
    """新建、打开、保存、另存或关闭图形文件。new 用样板（blank_a3、gb_a3_mech、gb_a3_arch、iso_a3…）；open 打开 file_ref（.dxf 或 .json）；save/save_as 导出 DXF/DWG/JSON。"""
    store = svc.store
    if a.action == "new":
        d = new_general_drawing(a.template_name, a.title)
        d.id = store.new_id()
        store.save(d)
        return {"drawing_id": d.id, "revision": d.revision, "summary": f"已新建通用图（样板 {a.template_name}），图形界限 {d.limits}", "layers": [l.name for l in d.layers], "current_layer": d.current_layer,
                "limits": list(d.limits)}
    if a.action == "open":
        if not a.file_ref:
            raise ToolError("MISSING_ARG", "open 需要 file_ref", "")
        try:
            path = store.resolve_ref(a.file_ref)
        except PermissionError:
            raise ToolError("E_FORBIDDEN", "file_ref 越界", "只能使用任务给出的 file_ref")
        if not path.exists():
            raise ToolError("E_FILE_NOT_FOUND", f"找不到文件 {a.file_ref}", "")
        try:
            if path.suffix.lower() == ".json":
                d = Drawing.model_validate_json(path.read_text(encoding="utf-8"))
            elif path.suffix.lower() == ".dxf":
                from zhihui.cad.general_dxf import read_any_dxf
                d = read_any_dxf(path)
            else:
                d = None
        except (ValueError, OSError, UnicodeDecodeError) as ex:       # pydantic.ValidationError 也是 ValueError
            raise ToolError("FILE_UNREADABLE", f"{path.suffix.upper().lstrip('.')} 文件无法解析：{str(ex)[:200]}", "确认文件没有损坏；DXF 请用文本头 0/SECTION 开头的标准文件，DWG 先转成 DXF")
        if d is None:
            raise ToolError("E_FILE_REJECTED", "只能打开 .dxf 或 .json", "")
        d.id = store.new_id()
        d.revision = 0
        d.source, d.source_file = "uploaded", a.file_ref
        store.save(d)
        return {"drawing_id": d.id, "revision": d.revision, "mode": d.mode, "summary": f"已打开 {Path(a.file_ref).name}（{d.mode} 模式，{len(d.entities)} 个图元）", "entity_count": len(d.entities)}
    if not a.drawing_id:
        raise ToolError("MISSING_ARG", f"{a.action} 需要 drawing_id", "")
    d = svc._read(a.drawing_id)
    if a.action == "close":
        return {"drawing_id": d.id, "revision": d.revision, "summary": "已关闭（图纸仍保存在版本库里，可随时重新打开）"}
    out_dir = store.export_dir(d)
    stem = f"{d.id}_r{d.revision}"
    if a.format == "json":
        p = out_dir / f"{stem}.json"
        p.write_text(d.to_json(), encoding="utf-8")
    elif a.format == "stl":
        from zhihui.cad.geom.solid import to_stl_ascii
        meshes = [(e.data["vertices"], e.data["faces"]) for e in d.entities if e.kind in ("mesh", "solid3d")]
        if not meshes:
            raise ToolError("NO_SOLID", "图中没有三维实体或网格，不能导出 STL", "先用 extrude_2d_to_3d 或 revolve_2d_to_3d 生成实体")
        p = out_dir / f"{stem}.stl"
        p.write_text(to_stl_ascii(stem, meshes), encoding="utf-8")
    else:
        from zhihui.cad.general_dxf import write_general_dxf
        p = out_dir / f"{stem}.dxf"
        write_general_dxf(d, p, version=a.dxf_version)
        if a.format == "dwg":
            from zhihui.cad.dwg import find_oda, to_dwg
            if find_oda() is None:
                raise ToolError("E_NO_ODA", "导出 DWG 需要 ODA File Converter，本机没有安装", "改存 DXF（AutoCAD 可直接打开）")
            p = to_dwg(p, p.with_suffix(".dwg"))
    import hashlib
    return {"drawing_id": d.id, "revision": d.revision, "summary": f"已保存 {p.name}", "file": {"format": a.format, "file_ref": store.rel(p), "bytes": p.stat().st_size,
            "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}}


# ------------------------------------------------------------------ 3 configure_canvas_and_limits
class CanvasArgs(Args):
    bg_color: COLOR = None
    limits_lower_left: Pt = None
    limits_upper_right: Pt = None
    limits_check_on: Optional[bool] = None
    zoom_all: bool = False


@tool("configure_canvas_and_limits", "environment_setup", CanvasArgs)
def configure_canvas_and_limits(ctx: Ctx, a: CanvasArgs) -> dict:
    """设置绘图窗口背景色和图形界限。背景色只影响预览和前端，不写入 DXF；界限写入 $LIMMIN/$LIMMAX/$LIMCHECK。"""
    d = ctx.d
    parts = []
    if a.bg_color is not None:
        aci, tc = parse_color(a.bg_color)
        d.background = tc or {7: "#FFFFFF", 0: "#000000", 1: "#FF0000", 2: "#FFFF00", 3: "#00FF00", 4: "#00FFFF", 5: "#0000FF", 6: "#FF00FF", 8: "#808080"}.get(aci, "#FFFFFF")
        if isinstance(a.bg_color, str) and a.bg_color in ("黑", "黑色", "black"):
            d.background = "#000000"
        parts.append(f"背景色 {d.background}")
    ll = ctx.point(a.limits_lower_left, update_last=False) if a.limits_lower_left is not None else None
    ur = ctx.point(a.limits_upper_right, update_last=False) if a.limits_upper_right is not None else None
    if ll or ur:
        x0, y0 = (ll or (d.limits[0], d.limits[1], 0))[:2]
        x1, y1 = (ur or (d.limits[2], d.limits[3], 0))[:2]
        if x1 <= x0 or y1 <= y0:
            raise ToolError("INVALID_ARGS", "图形界限的右上角必须在左下角的右上方", "")
        d.limits = (x0, y0, x1, y1)
        parts.append(f"界限 ({x0:g},{y0:g})–({x1:g},{y1:g})")
    if a.limits_check_on is not None:
        d.limits_check = a.limits_check_on
        parts.append("界限检查" + ("开" if a.limits_check_on else "关"))
    if a.zoom_all or ll or ur:
        d.view.center = ((d.limits[0] + d.limits[2]) / 2, (d.limits[1] + d.limits[3]) / 2)
        d.view.height = d.limits[3] - d.limits[1]
        parts.append("已全部显示")
    return ctx.result("；".join(parts) or "没有改动", background=d.background, limits=list(d.limits))


# ------------------------------------------------------------------ 4 set_linetype_scale
class LtsArgs(Args):
    ltscale: Optional[float] = Field(None, gt=0)
    celtscale: Optional[float] = Field(None, gt=0)
    psltscale: Optional[Literal[0, 1]] = None
    auto: bool = False


@tool("set_linetype_scale", "environment_setup", LtsArgs)
def set_linetype_scale(ctx: Ctx, a: LtsArgs) -> dict:
    """调整线型比例让虚线、点画线正常显示。auto=true 时按图形界限和出图比例计算推荐值（不是固定写 1）；预览会按新比例重新渲染。"""
    d = ctx.d
    note = ""
    if a.ltscale is None and (a.auto or (a.celtscale is None and a.psltscale is None)):
        v, note = presets.nice_ltscale(d.limits)
        d.ltscale = v
        ctx.notes.append(note)
    if a.ltscale is not None:
        d.ltscale = a.ltscale
    if a.celtscale is not None:
        d.celtscale = a.celtscale
    if a.psltscale is not None:
        d.psltscale = a.psltscale
    return ctx.result(f"LTSCALE={d.ltscale:g}，CELTSCALE={d.celtscale:g}，PSLTSCALE={d.psltscale}" + (f"（{note}）" if note else ""), ltscale=d.ltscale, celtscale=d.celtscale, psltscale=d.psltscale)


# ------------------------------------------------------------------ 5 manage_layers
class LayersArgs(Args):
    action: Literal["create", "create_standard_preset", "rename", "delete", "purge", "list"]
    layer_name: str = ""
    new_name: str = ""
    preset_standard: Optional[Literal["GB_MECH", "GB_ARCH", "ISO"]] = None


def _layer_usage(d: Drawing) -> dict[str, int]:
    use: dict[str, int] = {}
    for e in d.entities:
        use[e.layer] = use.get(e.layer, 0) + 1
    return use


@tool("manage_layers", "environment_setup", LayersArgs)
def manage_layers(ctx: Ctx, a: LayersArgs) -> dict:
    """创建和管理图层：create 新建；create_standard_preset 建一套标准图层（GB_MECH 机械、GB_ARCH 建筑、ISO）；rename/delete/purge/list。"""
    d = ctx.d
    if a.action == "list":
        use = _layer_usage(d)
        return ctx.result(f"共 {len(d.layers)} 个图层，当前层「{d.current_layer}」", layers=[{**l.model_dump(), "entity_count": use.get(l.name, 0), "current": l.name == d.current_layer} for l in d.layers])
    if a.action == "create_standard_preset":
        std = a.preset_standard or "GB_MECH"
        added = []
        for spec in presets.LAYER_PRESETS[std]:
            if d.get_layer(spec["name"]) is None:
                d.layers.append(Layer(**spec))
                added.append(spec["name"])
        if added and d.current_layer == "0":
            d.current_layer = presets.PRESET_CURRENT[std]
        return ctx.result(f"已建标准图层 {std}：{'、'.join(added) or '（都已存在）'}；当前层「{d.current_layer}」", layers=added)
    if not a.layer_name and a.action != "purge":
        raise ToolError("MISSING_ARG", f"{a.action} 需要 layer_name", "")
    name = (a.layer_name or "").strip()
    cur = d.get_layer(name) if name else None
    if a.action == "create":
        if cur is not None:
            raise ToolError("LAYER_EXISTS", f"图层「{name}」已存在", "换个名字，或用 set_layer_properties 修改")
        if any(ch in name for ch in '<>/\\":;?*|=`'):
            raise ToolError("INVALID_ARGS", f"图层名含非法字符：{name}", "")
        d.layers.append(Layer(name=name))
        return ctx.result(f"已创建图层「{name}」（颜色 7，实线，默认线宽）", layer=name)
    if cur is None and a.action != "purge":
        raise ToolError("LAYER_NOT_FOUND", f"图层「{name}」不存在", f"现有图层：{[l.name for l in d.layers]}")
    if a.action == "rename":
        if not a.new_name:
            raise ToolError("MISSING_ARG", "rename 需要 new_name", "")
        if name == "0":
            raise ToolError("LAYER_PROTECTED", "不能重命名图层 0", "")
        if d.get_layer(a.new_name):
            raise ToolError("LAYER_EXISTS", f"图层「{a.new_name}」已存在", "")
        for e in d.entities:
            if e.layer == cur.name:
                e.layer = a.new_name
        if d.current_layer == cur.name:
            d.current_layer = a.new_name
        cur.name = a.new_name
        return ctx.result(f"图层「{name}」已改名为「{a.new_name}」")
    if a.action in ("delete", "purge"):
        use = _layer_usage(d)
        targets = [cur] if a.action == "delete" else [l for l in d.layers if (not name or l.name == name) and l.name != "0" and l.name != d.current_layer and not use.get(l.name)]
        removed = []
        for l in targets:
            if l.name == "0":
                raise ToolError("LAYER_PROTECTED", "不能删除图层 0", "")
            if l.name == d.current_layer:
                raise ToolError("LAYER_IN_USE", f"「{l.name}」是当前图层，不能删除", "先把别的图层设为当前")
            if use.get(l.name):
                raise ToolError("LAYER_IN_USE", f"图层「{l.name}」上还有 {use[l.name]} 个图元，不能删除", "先把图元移到别的图层，或先删除它们", entity_count=use[l.name])
            d.layers = [x for x in d.layers if x.name != l.name]
            removed.append(l.name)
        return ctx.result(f"已{'清理' if a.action == 'purge' else '删除'}图层：{'、'.join(removed) or '（无）'}", removed=removed)
    raise ToolError("INVALID_ARGS", f"未知操作 {a.action}", "")


# ------------------------------------------------------------------ 6 set_layer_properties
class LayerPropsArgs(Args):
    layer_name: str
    color: COLOR = None
    linetype: Optional[str] = None
    lineweight_mm: Optional[float] = Field(None, ge=0, le=2.11)
    transparency: Optional[int] = Field(None, ge=0, le=90)
    description: Optional[str] = None


@tool("set_layer_properties", "environment_setup", LayerPropsArgs)
def set_layer_properties(ctx: Ctx, a: LayerPropsArgs) -> dict:
    """设置图层颜色（ACI、#hex 或 [r,g,b]）、线型（未加载的自动加载）、线宽、透明度和说明。"""
    l = ctx.d.get_layer(a.layer_name)
    if l is None:
        raise ToolError("LAYER_NOT_FOUND", f"图层「{a.layer_name}」不存在", f"现有图层：{[x.name for x in ctx.d.layers]}")
    parts = []
    if a.color is not None:
        l.color, l.true_color = parse_color(a.color)
        parts.append(f"颜色 {l.true_color or l.color}")
    if a.linetype is not None:
        l.linetype = norm_linetype(a.linetype) or "Continuous"
        parts.append(f"线型 {l.linetype}")
    if a.lineweight_mm is not None:
        l.lineweight_mm = a.lineweight_mm
        parts.append(f"线宽 {a.lineweight_mm:g} mm")
    if a.transparency is not None:
        l.transparency = a.transparency
        parts.append(f"透明度 {a.transparency}")
    if a.description is not None:
        l.description = a.description
    return ctx.result(f"图层「{l.name}」：" + ("；".join(parts) or "没有改动"))


# ------------------------------------------------------------------ 7 set_layer_state
class LayerStateArgs(Args):
    layer_name: str
    set_current: Optional[bool] = None
    is_on: Optional[bool] = None
    is_frozen: Optional[bool] = None
    is_locked: Optional[bool] = None
    is_plottable: Optional[bool] = None


@tool("set_layer_state", "environment_setup", LayerStateArgs)
def set_layer_state(ctx: Ctx, a: LayerStateArgs) -> dict:
    """控制图层可见性与当前状态：设为当前、开关、冻结、锁定、是否打印。锁定图层上的对象不能修改；当前图层不能冻结。"""
    d = ctx.d
    l = d.get_layer(a.layer_name)
    if l is None:
        raise ToolError("LAYER_NOT_FOUND", f"图层「{a.layer_name}」不存在", f"现有图层：{[x.name for x in d.layers]}")
    if a.set_current:
        if l.frozen or a.is_frozen:
            raise ToolError("LAYER_FROZEN", f"冻结的图层「{l.name}」不能设为当前", "先解冻")
        d.current_layer = l.name
    if a.is_frozen and (d.current_layer == l.name):
        raise ToolError("LAYER_IS_CURRENT", f"当前图层「{l.name}」不能冻结", "先把别的图层设为当前")
    for field, v in (("on", a.is_on), ("frozen", a.is_frozen), ("locked", a.is_locked), ("plot", a.is_plottable)):
        if v is not None:
            setattr(l, field, v)
    st = [f"当前层「{d.current_layer}」", f"开={l.on}", f"冻结={l.frozen}", f"锁定={l.locked}", f"打印={l.plot}"]
    return ctx.result(f"图层「{l.name}」：" + "，".join(st))


# ------------------------------------------------------------------ 8 configure_drafting_modes
class DraftArgs(Args):
    snap_on: Optional[bool] = None
    snap_spacing: Optional[tuple[float, float]] = None
    grid_on: Optional[bool] = None
    grid_spacing: Optional[tuple[float, float]] = None
    ortho_on: Optional[bool] = None
    polar_on: Optional[bool] = None
    polar_angle_deg: Optional[float] = Field(None, gt=0, le=90)


@tool("configure_drafting_modes", "environment_setup", DraftArgs, quiet=True)
def configure_drafting_modes(ctx: Ctx, a: DraftArgs) -> dict:
    """设置捕捉、栅格、正交和极轴。正交和极轴互斥（同时要求开启会报错）；这些设置会影响“向右100”“@50<47”这类输入的解释。"""
    dr = ctx.d.draft
    if a.ortho_on and a.polar_on:
        raise ToolError("PARAM_CONFLICT", "正交和极轴互斥，不能同时开启", "只开其中一个")
    if a.snap_on is not None:
        dr.snap_on = a.snap_on
    if a.snap_spacing is not None:
        if min(a.snap_spacing) <= 0:
            raise ToolError("INVALID_ARGS", "捕捉间距必须大于 0", "")
        dr.snap_spacing = a.snap_spacing
    if a.grid_on is not None:
        dr.grid_on = a.grid_on
    if a.grid_spacing is not None:
        if min(a.grid_spacing) <= 0:
            raise ToolError("INVALID_ARGS", "栅格间距必须大于 0", "")
        dr.grid_spacing = a.grid_spacing
    if a.polar_angle_deg is not None:
        dr.polar_angle_deg = a.polar_angle_deg
    if a.ortho_on is not None:
        dr.ortho_on = a.ortho_on
        if a.ortho_on:
            dr.polar_on = False
    if a.polar_on is not None:
        dr.polar_on = a.polar_on
        if a.polar_on:
            dr.ortho_on = False
    return ctx.result(f"捕捉={'开' if dr.snap_on else '关'} 栅格={'开' if dr.grid_on else '关'} 正交={'开' if dr.ortho_on else '关'} 极轴={'开' if dr.polar_on else '关'}（增量 {dr.polar_angle_deg:g}°）",
                      draft=dr.model_dump())


# ------------------------------------------------------------------ 9 configure_osnap_and_otracking
OSNAP_MODES = ("endpoint", "midpoint", "center", "geometric_center", "node", "quadrant", "intersection", "extension", "insertion", "perpendicular", "tangent", "nearest", "parallel")


class OsnapArgs(Args):
    osnap_on: Optional[bool] = None
    otrack_on: Optional[bool] = None
    active_modes: Optional[list[Literal["endpoint", "midpoint", "center", "geometric_center", "node", "quadrant", "intersection", "extension", "insertion", "perpendicular", "tangent", "nearest", "parallel"]]] = None


@tool("configure_osnap_and_otracking", "environment_setup", OsnapArgs, quiet=True)
def configure_osnap_and_otracking(ctx: Ctx, a: OsnapArgs) -> dict:
    """开启对象捕捉与对象追踪，并选择启用的捕捉模式。"""
    dr = ctx.d.draft
    if a.osnap_on is not None:
        dr.osnap_on = a.osnap_on
    if a.otrack_on is not None:
        dr.otrack_on = a.otrack_on
        if a.otrack_on and not dr.osnap_on:
            dr.osnap_on = True
            ctx.warnings.append("对象追踪依赖对象捕捉，已同时开启对象捕捉")
    if a.active_modes is not None:
        dr.osnap_modes = list(dict.fromkeys(a.active_modes))
    return ctx.result(f"对象捕捉={'开' if dr.osnap_on else '关'}（{'、'.join(dr.osnap_modes)}），对象追踪={'开' if dr.otrack_on else '关'}", draft=dr.model_dump())


# ------------------------------------------------------------------ 10 configure_dynamic_input_and_coords
class DynArgs(Args):
    dynmode_on: Optional[bool] = None
    pointer_input_on: Optional[bool] = None
    dimension_input_on: Optional[bool] = None
    coords_display_mode: Optional[Literal["static", "continuous_xy", "continuous_polar"]] = None


@tool("configure_dynamic_input_and_coords", "environment_setup", DynArgs, quiet=True)
def configure_dynamic_input_and_coords(ctx: Ctx, a: DynArgs) -> dict:
    """设置动态输入（指针输入、标注输入）和状态栏坐标显示方式（静态/连续直角/连续极坐标）。"""
    dr = ctx.d.draft
    for f in ("dynmode_on", "pointer_input_on", "dimension_input_on", "coords_display_mode"):
        v = getattr(a, f)
        if v is not None:
            setattr(dr, f, v)
    return ctx.result(f"动态输入={'开' if dr.dynmode_on else '关'}（指针输入 {'开' if dr.pointer_input_on else '关'}，标注输入 {'开' if dr.dimension_input_on else '关'}），坐标显示 {dr.coords_display_mode}", draft=dr.model_dump())
