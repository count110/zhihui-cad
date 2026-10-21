"""DXF 读写：把语义标签写进文件，改图时据此精确还原拓扑。

- 设备 = 块参照 INSERT（块名 ZH_<symbol_id>），属性 TAG 可见（设备旁只注位号），NAME / SYMBOL / EID / PARAMS 不可见；
  位号和名称另在图纸上方的位号栏标注（TEXT/LINE，XDATA = ["tag_row", eid]）。
  INSERT 上另挂 XDATA(ZHIHUI) = ["equip", eid]。
- 管线 = LWPOLYLINE，箭头是单独的 SOLID（XDATA = ["arrow", cid]）；管线 XDATA(ZHIHUI) =
  ["conn", cid, from_eid, from_port, to_eid, to_port, line_type, medium]。
- 图纸级：根字典 ZHIHUI → XRECORD META，记录生成器版本、拓扑校验码和完整 CAD-IR JSON。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import ezdxf
from ezdxf.enums import TextEntityAlignment

from zhihui import GENERATOR
from zhihui.ir import Annotation, Connection, Dimension, Drawing, Equipment, Hatch

from .routing import simplify
from .standards import LY, PIPE_CODE_PREFIX, fit_h, for_drawing, layer_table, linetype_patterns, text_h
from .standards import load as _load_standard
from .symbols import NAME_HEIGHT, TAG_HEIGHT, TITLE_H, TITLE_W, WATER, get_symbol, inner_frame, load_symbols, port_point, text_width
from .tagrow import layout_tag_row

APPID = "ZHIHUI"
TEXT_STYLE = "ZH_CN"
_STD = _load_standard("gb_general")
# 文字样式：GB/T 14691 要求长仿宋。默认用 TrueType 仿宋 simfang.ttf + 宽度因子 0.7（AutoCAD、ODA Viewer 等都能直接显示）；
# 标准配置 text.dxf_font 改成 "shx" 则用设计院常用的 gbenor.shx + 大字体 gbcbig.shx（AutoCAD / 中望 / 浩辰自带，
# ODA Viewer 没有这两个 SHX，汉字会显示成 ?，已实测）。PNG/PDF 预览一律用仿宋 TTF（render.py 修正了「图」字被填实的问题），
# 可用环境变量 ZHIHUI_CJK_FONT 换成服务器上有的字体（不影响交付的 DXF）。
WIDTH_FACTOR = _STD.width_factor
LATIN_STYLE = "ZH_LATIN"  # 只有字母数字的文字：B 型斜体（GB/T 14691 第 3.4、3.5 条）
SYM_STYLE = "ZH_SYM"  # 带 ²、³ 的字母数字（如 F=60m²）：仿宋 TTF 没有这两个字形，改用宋体 TTF，宽度和倾斜同 ZH_LATIN
SYM_CHARS = "²³"
_ACTIVE: dict = {"std": None}  # 正在出图的那张图的标准配置（_text 没有 Drawing 参数，按它取字母数字宽度因子）
LATIN_OBLIQUE = float(_STD.raw["text"].get("latin_oblique", 15.0))


def dxf_font(std=None) -> tuple[str, str]:
    """(字体文件, 大字体)：ttf 模式大字体为空。"""
    t = (std or _STD).raw["text"]
    if t.get("dxf_font", "ttf") == "shx":
        return t["shx_font"], t["shx_bigfont"]
    return t.get("ttf_font", "simfang.ttf"), ""


def text_style(text: str, std=None) -> tuple[str, float, float]:
    """(样式名, 宽度因子, 倾斜角)：含汉字 → ZH_CN 正体、宽度因子 0.7（长仿宋，GB/T 14691 第 3.3 条）；
    只有字母数字 → ZH_LATIN、宽度因子 1、向右倾斜 15°（与水平基准线成 75°，第 3.5 条；SHX 模式下 gbeitc.shx 本身是斜体，不再加倾斜）。"""
    from .standards import is_cjk

    if is_cjk(text):
        return TEXT_STYLE, WIDTH_FACTOR, 0.0
    t = (std or _ACTIVE["std"] or _STD).raw["text"]
    shx = t.get("dxf_font", "ttf") == "shx"
    italic = t.get("latin_italic", True) and not shx
    name = SYM_STYLE if not shx and any(ch in SYM_CHARS for ch in text) else LATIN_STYLE
    return name, (1.0 if shx else float(t.get("latin_width_factor", 0.9))), (LATIN_OBLIQUE if italic else 0.0)
CHUNK = 200

# 图层、颜色、线型、线宽全部来自标准配置（standards/*.json 的 layers 表，按角色取层名 LY[...]）：
# 主物料管道粗线、公用工程中粗、辅助和设备轮廓细线（HG/T 20519.1 表 6.1.3、HG/T 20519.2 第 8.0.5 条），图框粗实线（GB/T 14689）
PHANTOM_STATUS = ("预留", "拆除", "reserved", "demolished")
PIPE_LAYER = {"process": LY["pipe_process"], "utility": LY["pipe_utility"], "auxiliary": LY["pipe_aux"], "instrument": LY["pipe_instrument"]}


def pipe_layer(c) -> str:
    """管道图层（文章表 2-5）：主要物料 / 辅助及公用 × 地上（实线）/ 埋地（虚线）；仪表管道（细虚线）；原有管道（双点画线）。"""
    if getattr(c, "existing", False):
        return LY["pipe_existing"]
    if c.line_type == "instrument":
        return LY["pipe_instrument"]
    if getattr(c, "buried", False):
        return LY["pipe_process_buried"] if c.line_type == "process" else LY["pipe_aux_buried"]
    return PIPE_LAYER.get(c.line_type, LY["pipe_process"])
ALIGN = {
    "center": TextEntityAlignment.BOTTOM_CENTER,
    "left": TextEntityAlignment.BOTTOM_LEFT,
    "right": TextEntityAlignment.BOTTOM_RIGHT,
}


# 程序生成的说明性文字（管线代号、位号栏、名称、图例、标题、说明、高程符号）：读回时不算图中自由文字
DECOR_TAGS = ("conn_label", "tag_row", "name_label", "legend", "title", "notes", "elev", "system", "anno_part", "detail", "table", "zone", "tracing",
              "ground", "insulation", "nozzle", "repeat", "offsheet", "grade_break", "fitting", "instrument")


class ForeignDXFError(Exception):
    """不是本系统生成的 DXF（MVP 不支持第三方图改图，P1 再做）。"""

    def __init__(self, summary: dict):
        super().__init__("not a zhihui drawing")
        self.summary = summary


# ---------------------------------------------------------------- 写

def _setup(doc, d: Drawing | None = None) -> None:
    """文字样式、线型、图层都按标准配置建（GB/T 14665 图层约定；颜色与线宽一一对应，实体一律随层）。"""
    std = for_drawing(d) if d is not None else _STD
    doc.appids.add(APPID)
    if TEXT_STYLE not in doc.styles:
        font, bigfont = dxf_font(std)
        style = doc.styles.add(TEXT_STYLE, font=font)
        if bigfont:
            style.dxf.bigfont = bigfont
        style.dxf.width = WIDTH_FACTOR
    if LATIN_STYLE not in doc.styles:
        font, bigfont = dxf_font(std)
        style = doc.styles.add(LATIN_STYLE, font=font)
        if bigfont:
            style.dxf.bigfont = bigfont
        _, wf, obl = text_style("A", std)
        style.dxf.width = wf
        style.dxf.oblique = obl
    if SYM_STYLE not in doc.styles and dxf_font(std)[1] == "":
        style = doc.styles.add(SYM_STYLE, font=std.raw["text"].get("sym_font", "simsun.ttc"))
        _, wf, obl = text_style("A", std)
        style.dxf.width = wf
        style.dxf.oblique = obl
    for name, (desc, elements) in linetype_patterns(std).items():  # 按 GB 线素长度（图纸 mm）重定义，$LTSCALE 换算到模型
        if name in doc.linetypes:
            doc.linetypes.remove(name)
        doc.linetypes.add(name, pattern=[sum(abs(e) for e in elements), *elements], description=desc)
    for name, attrs in layer_table(std).items():
        if name not in doc.layers:
            layer = doc.layers.add(name, color=attrs["color"], linetype=attrs["linetype"])
            layer.dxf.lineweight = attrs["lineweight"]
    if d is not None:
        doc.header["$LTSCALE"] = d.scale  # 线型按图纸 mm 定义，乘出图比例后打印间距才对
        doc.header["$PSLTSCALE"] = 1
        doc.header["$CELTSCALE"] = 1.0
        from .dims import setup_dimstyle

        setup_dimstyle(doc, d)  # 标注样式 GB_DIM 总是写进去：用户在 CAD 里补尺寸时直接用（GB/T 4458.4）


def pipe_code_layer(doc, d: Drawing, code: str) -> str:
    """按管道代号分层（ZH-管道-YS …）：主流程 YS/QS 粗线，其余中粗。"""
    name = PIPE_CODE_PREFIX + "".join(ch for ch in code.upper() if ch.isalnum())
    if name not in doc.layers:
        from .water import MAIN_CODES

        tmpl = layer_table(for_drawing(d))[LY["pipe_main_code" if code in MAIN_CODES else "pipe_other_code"]]
        doc.layers.add(name, color=tmpl["color"], linetype=tmpl["linetype"]).dxf.lineweight = tmpl["lineweight"]
    return name


def _add_ellipse(space, c, rx: float, ry: float, a0: float, a1: float, attrs: dict) -> None:
    """按参数角画椭圆弧：点 = c + (rx·cosθ, ry·sinθ)，θ 从 a0 逆时针到 a1（度）。"""
    full = (a1 - a0) % 360 == 0
    if abs(rx - ry) < 1e-9:
        if full:
            space.add_circle(c, rx, dxfattribs=attrs)
        else:
            space.add_arc(c, rx, a0, a1, dxfattribs=attrs)
        return
    if rx >= ry:
        major, ratio, off = (rx, 0), ry / rx, 0.0
    else:
        major, ratio, off = (0, ry), rx / ry, -90.0
    t0, t1 = (0.0, 2 * math.pi) if full else (math.radians(a0 + off), math.radians(a1 + off))
    space.add_ellipse(c, major_axis=major, ratio=ratio, start_param=t0, end_param=t1, dxfattribs=attrs)


def fix_insert(t):
    """居中 / 右对齐的 TEXT、ATTDEF：把第一对齐点（insert）算到文字左下基线处。

    ezdxf 的 set_placement 把 insert 和 align_point 写成同一点；AutoCAD 打开时会按 align_point 重算，
    但 ODA Viewer 等不重算的查看器直接从 insert 起画，居中的字会整体右移、上下错位（实测）。
    宽度按标准配置估算（与 AutoCAD 的实际字宽略有出入，AutoCAD 仍以 align_point 为准，不受影响）。"""
    h_al, v_al = t.dxf.get("halign", 0), t.dxf.get("valign", 0)
    if not h_al and not v_al:
        return t
    a = t.dxf.get("align_point", t.dxf.insert)
    h = float(t.dxf.height)
    w = _STD.text_width(t.dxf.text, h, float(t.dxf.get("width", 1.0)))
    dx = {1: -w / 2, 2: -w, 4: -w / 2}.get(h_al, 0.0)
    if h_al in (3, 5):  # ALIGNED / FIT：两点之间，insert 本来就是起点
        return t
    dy = {1: 0.2 * h, 2: -h / 2, 3: -h}.get(v_al, 0.0)
    if h_al == 4:  # MIDDLE：水平、竖直都居中
        dy = -h / 2
    r = math.radians(float(t.dxf.get("rotation", 0.0)))
    t.dxf.insert = (a[0] + dx * math.cos(r) - dy * math.sin(r), a[1] + dx * math.sin(r) + dy * math.cos(r), 0)
    return t


def text_anchor(t) -> tuple[float, float]:
    """文字的定位点：有对齐方式时是 align_point，否则是 insert。"""
    p = t.dxf.get("align_point") if (t.dxf.get("halign", 0) or t.dxf.get("valign", 0)) else None
    p = p if p is not None else t.dxf.insert
    return (p[0], p[1])


def _text(space, text: str, pos, height: float, layer: str, align=TextEntityAlignment.BOTTOM_LEFT):
    style, wf, obl = text_style(text)
    attrs = {"layer": layer, "style": style, "width": wf}
    if obl:
        attrs["oblique"] = obl
    return fix_insert(space.add_text(text, height=height, dxfattribs=attrs).set_placement(pos, align=align))


def restyle(t) -> None:
    """按内容重新定文字样式（属性值插入后才知道是汉字还是字母数字）。"""
    style, wf, obl = text_style(t.dxf.text)
    t.dxf.style, t.dxf.width = style, wf
    if obl:
        t.dxf.oblique = obl
    else:
        t.dxf.discard("oblique")


def _hatch_layer(doc) -> None:
    """填充图层（ZH-剖面线，细线、颜色 8）：_setup 已按图层表建好，这里只兜底。"""
    if LY["hatch"] not in doc.layers:
        doc.layers.add(LY["hatch"], color=8).dxf.lineweight = 25


def _define_block(doc, symbol_id: str, unit: float, tag_visible: bool = True, d: Drawing | None = None) -> str:
    """tag_visible=False（高程布置图）：位号只留在块属性里，不显示，也不画位号下划线。"""
    name = f"ZH_{symbol_id}"
    if name in doc.blocks:
        return name
    sym = get_symbol(symbol_id)
    blk = doc.blocks.new(name=name)
    k = unit
    attrs = {"layer": "0"}  # 块里的图元都放 0 层（GB/T 14665 / 通行做法），颜色线型线宽随插入层
    for g in sym.graphics:
        t = g["type"]
        if t == "circle":
            blk.add_circle((g["c"][0] * k, g["c"][1] * k), g["r"] * k, dxfattribs=attrs)
        elif t == "arc":
            blk.add_arc((g["c"][0] * k, g["c"][1] * k), g["r"] * k, g["start"], g["end"], dxfattribs=attrs)
        elif t == "ellipse":
            _add_ellipse(blk, (g["c"][0] * k, g["c"][1] * k), g["rx"] * k, g["ry"] * k, g["start"], g["end"], attrs)
        elif t == "polyline":
            blk.add_lwpolyline([(x * k, y * k) for x, y in g["points"]], close=g.get("closed", False), dxfattribs=attrs)
        elif t == "text":  # 图元里的文字也按字高系列，汉字不低于 3.5（GB/T 14691），不随图元缩放
            th = text_h(d, "symbol_text", g["text"]) if d is not None else _STD.text_height("symbol_text", "A3", g["text"]) * k
            _text(blk, g["text"], (g["p"][0] * k, g["p"][1] * k), th, "0", TextEntityAlignment.MIDDLE_CENTER)
        elif t == "hatch":  # 图元里的剖面线（池壁、挡土墙、滤料……）：paper_spacing 为图纸上的线距（mm）
            from .hatch import SOLID, base_spacing, check_pattern

            _hatch_layer(doc)
            pat = check_pattern(g.get("pattern", "ANSI31"))
            h = blk.add_hatch(color=256, dxfattribs={"layer": "0"})  # 块里的图元都放 0 层，插入后随插入层显示
            if pat == SOLID:
                h.set_solid_fill(color=256)
            else:
                spacing = g.get("paper_spacing", g.get("spacing", 2.0))
                h.set_pattern_fill(pat, scale=spacing * k / base_spacing(pat), angle=g.get("angle", 0.0), color=256)
            h.paths.add_polyline_path([(x * k, y * k) for x, y in g.get("boundary") or g["points"]], is_closed=True, flags=1)
            for hole in g.get("holes", []):
                h.paths.add_polyline_path([(x * k, y * k) for x, y in hole], is_closed=True)
    # 设备旁只注位号，下加短横线（HG/T 20519.2 图 3.3.1-1）
    lx, ly = sym.label["x"] * k, (sym.label["y"] - TAG_HEIGHT) * k
    align = sym.label.get("align", "left")
    text_attrs = {"layer": "0", "style": TEXT_STYLE, "width": WIDTH_FACTOR}
    tag_h = text_h(d, "tag") if d is not None else TAG_HEIGHT * k
    tag_attrs = {**text_attrs, "height": tag_h} | ({} if tag_visible else {"flags": 1})
    fix_insert(blk.add_attdef("TAG", dxfattribs=tag_attrs).set_placement((lx, ly), align=ALIGN[align]))
    if tag_visible:
        lw = text_width("P-101A", TAG_HEIGHT) * k
        x0 = {"left": lx, "center": lx - lw / 2, "right": lx - lw}[align]
        blk.add_line((x0, ly - 0.8 * k), (x0 + lw, ly - 0.8 * k), dxfattribs={"layer": "0"})
    for i, tag in enumerate(("NAME", "SYMBOL", "EID", "PARAMS")):
        blk.add_attdef(tag, dxfattribs={**text_attrs, "height": tag_h, "flags": 1}).set_placement(
            (0, -i * k), align=TextEntityAlignment.BOTTOM_LEFT
        )
    return name


def _arrow(path: list[tuple[float, float]], unit: float) -> list[tuple[float, float]] | None:
    """末端箭头三角形（尖端在终点端口）。"""
    if len(path) < 2:
        return None
    (x0, y0), (x1, y1) = path[-2], path[-1]
    seg = math.hypot(x1 - x0, y1 - y0)
    if seg < 1e-6:
        return None
    alen, half = min(3.0 * unit, seg / 2), 0.8 * unit
    ux, uy = (x1 - x0) / seg, (y1 - y0) / seg
    bx, by = x1 - ux * alen, y1 - uy * alen
    return [(x1, y1), (bx - uy * half, by + ux * half), (bx + uy * half, by - ux * half)]


def _anno_symbol(msp, doc, d: Drawing, an) -> None:
    """专业符号标注（块）：坡度、索引符号、指北针插在标注位置；剖切符号成对插在锚点设备两侧，设备下方写「A—A」。"""
    from . import anno
    from .symbols import footprint, symbol_box

    xd = [(1000, "anno"), (1000, an.id), (1000, an.anchor_ref), (1000, an.style), (1000, f"{an.x:.3f}"), (1000, f"{an.y:.3f}")]
    if an.style == "slope":
        anno.insert(msp, doc, d, "slope", (an.x, an.y), LY["anno"], {"VALUE": an.text}, xdata=xd)
    elif an.style == "north":
        anno.insert(msp, doc, d, "north", (an.x, an.y), LY["anno"], xdata=xd)
    elif an.style == "index":
        no, _, sheet = an.text.partition("/")
        anno.insert(msp, doc, d, "index", (an.x, an.y), LY["anno"], {"NO": no or "1", "SHEET": sheet or "-"}, xdata=xd)
    elif an.style == "section":
        e = d.get_equipment(an.anchor_ref)
        if e is None or not e.placed:
            return
        x0, y0, x1, y1 = symbol_box(e, d.unit)
        cy, g = (y0 + y1) / 2, 2.0 * d.scale
        letter = an.text.strip()[:2] or "A"
        anno.insert(msp, doc, d, "section", (x0 - g, cy - 3 * d.scale), LY["anno"], {"LETTER": letter}, xdata=xd)
        anno.insert(msp, doc, d, "section", (x1 + g, cy - 3 * d.scale), LY["anno"], {"LETTER": letter}, xdata=[(1000, "anno_part"), (1000, an.id)])
        fp = footprint(e, d.unit, d.template_id)
        label = f"{letter}—{letter}"
        _text(msp, label, ((x0 + x1) / 2, fp[1] - 1.5 * d.scale), text_h(d, "annotation", label), LY["anno"],
              TextEntityAlignment.TOP_CENTER).set_xdata(APPID, [(1000, "anno_part"), (1000, an.id)])


def _medium_label(msp, d: Drawing, c, taken: list | None = None, text: str | None = None) -> None:
    """介质名（汉字，3.5 号）：标在水平段上方（放不下再试下方、再换段），避开设备、所有管线和已经写好的文字；
    没有放得下的水平段时，写在附近空白处，用细实线引出线连到管线（GB/T 4457.2）。taken：已占用的文字框，跨管线共享。"""
    from .symbols import footprint
    from .water import _seg_in_box, leader_clear, leader_spots

    s = d.scale
    taken = [] if taken is None else taken
    label = c.medium if text is None else text
    from .standards import is_cjk

    # 管道标注只有字母数字（CW0601-32×3.5A）：按字母数字字高（HG/T 20519.1 第 6.2 条 2～3 mm）；介质名含汉字仍不低于 3.5
    h = text_h(d, "pipe_label" if is_cjk(label) else "pipe_code", label)
    tw = _STD.text_width(label, h)
    sides = for_drawing(d).raw.get("pipes", {}).get("label_sides")  # 化工流程图：横向标在上方，竖向标在左侧（文章 3.2 节）
    xd = [(1000, "conn_label"), (1000, c.id)]
    segs = list(zip(c.path, c.path[1:]))
    boxes = [footprint(e, d.unit, d.template_id) for e in d.equipment if e.placed] + taken
    lines = [(p, q) for cc in d.connections for p, q in zip(cc.path or [], (cc.path or [])[1:])]

    from .symbols import label_area_ok

    def free(b) -> bool:
        if not label_area_ok(d, b):  # 不出图框、不进位号栏、不压标题栏
            return False
        return not any(b[0] < o[2] and o[0] < b[2] and b[1] < o[3] and o[1] < b[3] for o in boxes) and not any(_seg_in_box(p, q, b) for p, q in lines)

    horiz = sorted([(a, b) for a, b in segs if abs(a[1] - b[1]) < 1e-6], key=lambda ab: -abs(ab[0][0] - ab[1][0]))
    vert = sorted([(a, b) for a, b in segs if abs(a[0] - b[0]) < 1e-6 and abs(a[1] - b[1]) > 1e-6], key=lambda ab: -abs(ab[0][1] - ab[1][1]))
    ts = [0.5] + sorted((i / 10 for i in range(1, 10) if i != 5), key=lambda t: abs(t - 0.5))

    def put_h(a, b, up: bool) -> bool:
        x0, x1 = sorted((a[0], b[0]))
        if x1 - x0 < tw + 4 * s:
            return False
        for t in ts:
            cx = x0 + (x1 - x0) * t
            if cx - tw / 2 < x0 + 2 * s or cx + tw / 2 > x1 - 2 * s:
                continue
            y = a[1] + 1.2 * s if up else a[1] - 1.2 * s - h
            box = (cx - tw / 2, y, cx + tw / 2, y + h)
            if free(box):
                _text(msp, label, (cx, y), h, LY["pipe_text"], TextEntityAlignment.BOTTOM_CENTER).set_xdata(APPID, xd)
                taken.append(box)
                return True
        return False

    def put_v(a, b, left: bool) -> bool:
        """竖向管线：文字旋转 90°（字头向左），写在管线左侧（或右侧）。"""
        y0, y1 = sorted((a[1], b[1]))
        if y1 - y0 < tw + 4 * s:
            return False
        for t in ts:
            cy = y0 + (y1 - y0) * t
            if cy - tw / 2 < y0 + 2 * s or cy + tw / 2 > y1 - 2 * s:
                continue
            x = a[0] - 1.2 * s if left else a[0] + 1.2 * s + h
            box = (x - h, cy - tw / 2, x, cy + tw / 2)
            if free(box):
                t_ = _text(msp, label, (x, cy), h, LY["pipe_text"], TextEntityAlignment.BOTTOM_CENTER)
                t_.dxf.rotation = 90
                fix_insert(t_)
                t_.set_xdata(APPID, xd)
                taken.append(box)
                return True
        return False

    if sides:  # 按配置的先后：上方 → 竖管左侧 → 竖管右侧，都放不下再用引出线
        tries = {"above": lambda: any(put_h(a, b, True) for a, b in horiz), "below": lambda: any(put_h(a, b, False) for a, b in horiz),
                 "left": lambda: any(put_v(a, b, True) for a, b in vert), "right": lambda: any(put_v(a, b, False) for a, b in vert)}
        for k in sides:
            if tries[k]():
                return
    else:  # 原做法：每段先上后下
        for a, b in horiz:
            if put_h(a, b, True) or put_h(a, b, False):
                return
    # 引出线：从最长段中点引出，文字写在附近空白处的下划线上
    a, b = max(segs, key=lambda ab: math.dist(*ab))
    m = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
    pick = None
    for dy, dx in leader_spots(tw / s):
        x, y = m[0] + dx * s, m[1] + dy * s
        box = (x, y - 0.6 * h, x + tw, y + 0.6 * s + h)
        # 文字在管线上方且正对着时竖直引出到下划线；否则引到下划线靠近的一端（在下方时竖线会穿过文字）
        end = (m[0], y) if (x <= m[0] <= x + tw and y > m[1]) else ((x, y) if abs(x - m[0]) < abs(x + tw - m[0]) else (x + tw, y))
        own = (x, y + 0.3 * s, x + tw, y + 0.6 * s + h)  # 引出线不穿过自己的文字
        if free(box) and not any(_seg_in_box(m, end, o) for o in boxes + [own]) and leader_clear(m, end, lines, 0.5 * s):
            pick = (x, y, end, box)
            break
    if pick is None and sides and any(put_h(a, b, False) for a, b in horiz):  # 化工流程图：引出线也放不下时才写到横管下方
        return
    if pick is None:  # 实在没有空白：仍写在右上方，宁可略挤也不丢介质名
        x, y = m[0] + 3 * s, m[1] + 3 * s
        pick = (x, y, (x, y), (x, y, x + tw, y + h))
    x, y, end, box = pick
    msp.add_lwpolyline([m, end], dxfattribs={"layer": LY["pipe_text"]}).set_xdata(APPID, xd)
    msp.add_lwpolyline([(x, y), (x + tw + 1 * s, y)], dxfattribs={"layer": LY["pipe_text"]}).set_xdata(APPID, xd)
    _text(msp, label, (x + 0.5 * s, y + 0.6 * s), h, LY["pipe_text"], TextEntityAlignment.BOTTOM_LEFT).set_xdata(APPID, xd)
    taken.append(box)


def _frame(msp, d: Drawing) -> None:
    """纸边线（细）+ 图框线（粗，GB/T 14689）+ 右下角标题栏（GB/T 10609.1，模板见 zhihui/cad/titleblocks）。"""
    w, h = d.frame_size()
    thin, thick = {"layer": LY["frame"]}, {"layer": LY["border"]}
    msp.add_lwpolyline([(0, 0), (w, 0), (w, h), (0, h)], close=True, dxfattribs=thin)
    if not d.frame:  # 方案流程图可以不画图框和标题栏（文章 3.1.1 节、图 3-1），只留纸边线
        return
    fx0, fy0, fx1, fy1 = inner_frame(d)
    msp.add_lwpolyline([(fx0, fy0), (fx1, fy0), (fx1, fy1), (fx0, fy1)], close=True, dxfattribs=thick)

    from . import zones
    from .titleblock import draw as draw_title_block

    zones.draw(msp, d)  # 图幅分区、对中符号（A2 及以上）
    draw_title_block(msp, d)  # 标题栏按 JSON 模板画（流程图按文章图 2-3），右、下两边与图框线重合不重复画


def _tag_row(msp, d: Drawing) -> None:
    """图纸上方的位号栏：位号在位号线上方，设备名称在下方。"""
    u = d.unit
    for it in layout_tag_row(d):
        xd = [(1000, "tag_row"), (1000, it.equip_id)]
        _text(msp, it.tag, (it.x, it.line_y + 1.0 * u), text_h(d, "tag", it.tag), LY["tag_row"], TextEntityAlignment.BOTTOM_CENTER).set_xdata(APPID, xd)
        msp.add_line((it.x - it.width / 2, it.line_y), (it.x + it.width / 2, it.line_y), dxfattribs={"layer": LY["tag_row"]}).set_xdata(APPID, xd)
        if it.name:
            _text(msp, it.name, (it.x, it.line_y - 1.0 * u), text_h(d, "name", it.name), LY["tag_row"], TextEntityAlignment.TOP_CENTER).set_xdata(APPID, xd)
        if it.feature:  # 物料流程图及以上：名称下方再写一行特性参数（文章图 3-3，F=60m²）
            from .tagrow import NAME_HEIGHT

            fy = it.line_y - (1.0 + NAME_HEIGHT + 1.2) * u
            _text(msp, it.feature, (it.x, fy), text_h(d, "name", it.feature), LY["tag_row"], TextEntityAlignment.TOP_CENTER).set_xdata(APPID, xd)


def _chunks(text: str) -> list[str]:
    return [text[i : i + CHUNK] for i in range(0, len(text), CHUNK)] or [""]


def write_dxf(d: Drawing, path: str | Path) -> str:
    """写出 DXF，返回文件 sha256。要求所有设备已布局。"""
    doc = build_doc(d)
    if d.hatches:  # 填充最后画：按当前图形重新求边界（关联）
        from .hatch_ir import write_hatches

        write_hatches(doc, d)
    root = doc.rootdict.add_new_dict(APPID)
    xr = root.add_xrecord("META")
    meta = {
        "generator": GENERATOR,
        "topology_hash": d.topology_hash(),
        "drawing_id": d.id,
        "revision": d.revision,
    }
    tags = [(1, "META"), (1, json.dumps(meta, ensure_ascii=False)), (1, "IR")]
    tags += [(1, ch) for ch in _chunks(d.compat_json())]
    xr.reset(tags)

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(path)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_doc(d: Drawing):
    """按 IR 画出整张图（不含填充和元数据），填充求边界时也用它。"""
    doc = ezdxf.new("R2018", setup=True)
    doc.units = ezdxf.units.MM
    doc.header["$INSUNITS"] = 4
    _ACTIVE["std"] = for_drawing(d)
    _setup(doc, d)
    msp = doc.modelspace()
    s = d.unit
    water = d.template_id == WATER
    from . import water as wt

    from . import chem

    broken = chem.crossing_style(d) == "break"
    jumps = chem.break_points(d) if broken else wt.jump_points(d)  # 化工流程图：交叉处断开一根（文章图 2-6）；高程图仍用跨线符号
    cut_r = chem.gap_half(d) if broken else None
    label_boxes: list = []
    from . import fittings as fit_mod
    from . import instruments as ins_mod

    from . import chem_tables

    if not water:  # 管线上的文字让开管件符号、仪表圆圈、物料组成表和图例
        label_boxes += [b for c in d.connections for b in fit_mod.boxes(d, c)] + ins_mod.boxes(d) + chem_tables.reserved_boxes(d)  # 已写好的介质名，后面的避开
    breaks = wt.break_spans(d) if all(e.placed for e in d.equipment) else {}
    if water:
        wt.setup_layers(doc, d)
        wt.draw_frame(msp, d)
        wt.draw_title_and_notes(msp, d)
        wt.draw_legend(msp, d)
        wt.draw_ground(msp, d)
        wt.draw_systems(msp, d)
    else:
        _frame(msp, d)
        _tag_row(msp, d)
        chem.draw_ground(msp, d)  # 厂房地平线 EL±0.000（施工流程图，或有安装高度 / 地下设备时）

    for e in d.equipment:
        if not e.placed:
            raise ValueError(f"设备 {e.tag} 尚未布局")
        from .symbols import TERMINALS

        bname = _define_block(doc, e.symbol_id, s, tag_visible=not water and e.symbol_id not in TERMINALS, d=d)
        # 预留、拆除的设备用细双点画线（GB/T 4457.4：相邻辅助零件、极限位置的轮廓线）
        status = str(e.params.get("status", ""))
        layer = LY["phantom"] if status in PHANTOM_STATUS else LY["equip"]
        ref = msp.add_blockref(bname, (e.x, e.y), dxfattribs={"layer": layer})
        sym = get_symbol(e.symbol_id)
        if sym.axes:  # 对称中心线；图元小于 10 mm 时用细实线代替点画线
            x0, y0, x1, y1 = sym.bbox
            small = max(x1 - x0, y1 - y0) < for_drawing(d).raw["lines"]["small_symmetric_mm"]
            from . import dashsplit

            for (ax, ay), (bx, by) in sym.axes:  # 点画线从中心处断成两段排相位：十字交点、两端都落在画上（GB/T 17450 第 5.2.1 条）
                a, b = (e.x + ax * s, e.y + ay * s), (e.x + bx * s, e.y + by * s)
                dashsplit.draw(msp, d, [a, b], LY["flow_arrow"] if small else LY["center"], cuts=((e.x, e.y),),
                               xdata=[(1000, "axis"), (1000, e.id)])
        params = json.dumps(e.params, ensure_ascii=False)[:240]
        ref.add_auto_attribs({"TAG": e.tag, "NAME": e.name, "SYMBOL": e.symbol_id, "EID": e.id, "PARAMS": params})
        for a in ref.attribs:  # 属性跟着插入层（定义在 0 层），0 层不放实体
            a.dxf.layer = layer
            restyle(a)
            fix_insert(a)
        ref.set_xdata(APPID, [(1000, "equip"), (1000, e.id)])
        if not water:
            chem.draw_equipment_extras(msp, d, e)  # 隔热层、伴热管（params.insulation / tracing）
            chem.draw_nozzles(msp, d, e)  # 施工流程图的管口
            chem.draw_offsheet_text(msp, d, e)  # 空心箭头里的图号
            chem.draw_terminal_name(msp, d, e)  # 管道起点、终点写物料名称和来去
    if water:
        wt.draw_names(msp, d)
    else:
        chem.draw_repeats(msp, d)  # 相同的局部系统：细双点画线方框

    for c in d.connections:
        if not c.path:
            continue
        layer = pipe_layer(c)
        vals = ["conn", c.id, c.from_equip, c.from_port, c.to_equip, c.to_port, c.line_type, c.medium]
        if c.code or c.spec:  # 只有给了代号/规格才多写两项，旧图的 XDATA 不变
            vals += [c.code, c.spec]
        if water:
            wt.draw_pipe(msp, d, c, pipe_code_layer(doc, d, c.code) if c.code else layer, [(1000, v) for v in vals], jumps, breaks)
            continue
        # 管线交叉处用跨线符号（半圆）：在跨的那条线上断开，第一段带完整语义，其余段 ["conn_part", cid, 序号]
        pieces, _, hops = wt.pipe_pieces(d, c, jumps, breaks, r=cut_r, code_gap=False)
        if broken:
            hops = []  # 断开即可，不画半圆
        fits = fit_mod.placements(d, c)
        pieces = fit_mod.apply_cuts(pieces, [cut for pl in fits for cut in pl["cuts"]] + ins_mod.inline_cuts(d, c.id))  # 阀门管件、嵌在管道中的仪表处断开
        from . import anno

        for pt, ang in wt.break_marks(d, c, breaks):  # 很长的空管段折断，断口画双折线
            anno.insert(msp, doc, d, "break", pt, layer, rotation=ang, xdata=[(1000, "break"), (1000, c.id)])
        from . import dashsplit

        k = 0
        for pts in pieces:  # 虚线类管道（埋地、仪表、原有）按线素相位拆开，拐角和三通落在画线上（GB/T 17450 第 5.2.1 条）
            made = dashsplit.draw(msp, d, pts, layer, xdata=[(1000, v) for v in vals] if k == 0 else [(1000, "conn_part"), (1000, c.id), (1000, str(k))],
                                  part_xdata=lambda i, k0=k: [(1000, "conn_part"), (1000, c.id), (1000, str(k0 + i))])
            k += len(made)
        for (hx, hy), vertical in hops:
            a0, a1 = (270, 90) if vertical else (0, 180)
            msp.add_arc((hx, hy), wt.JUMP_R * s, a0, a1, dxfattribs={"layer": layer}).set_xdata(APPID, [(1000, "jump"), (1000, c.id)])
        fit_mod.draw(msp, d, c)  # 阀门、管件、管道附件（文章表 2-11）
        if c.tracing:  # 伴热：管道旁带短钩的复合线（GB/T 17450 第 3.3.4 条）
            from . import tracing

            tracing.draw(msp, d, c)
        tri = _arrow(c.path, s)
        if tri:
            msp.add_solid(tri, dxfattribs={"layer": layer}).set_xdata(APPID, [(1000, "arrow"), (1000, c.id)])
        avoid = [p for pts in jumps.values() for p in pts] + [c.path[0], c.path[-1]] + [pl["p"] for pl in fits]
        for t3 in chem.mid_arrows(d, c, avoid):  # 长管线中间补箭头
            msp.add_solid(t3, dxfattribs={"layer": layer}).set_xdata(APPID, [(1000, "arrow"), (1000, c.id)])
        text = chem.label_text(d, c)  # 施工流程图写管道标注（文章图 2-7），其余写介质名
        if text:
            _medium_label(msp, d, c, label_boxes, text)
        ptxt = chem_tables.params_text(d, c)  # 物料流程图及以上：管线旁的温度、压力（只能来自用户）
        if ptxt:
            _medium_label(msp, d, c, label_boxes, ptxt)
    if not water:
        chem.draw_grade_breaks(msp, d)  # 同一管段号等级不同处的等级分界线
        ins_mod.draw(msp, d)  # 仪表控制点（文章 3.2.4 节）
        chem_tables.draw_streams(msp, d)  # 物料组成表（文章图 3-2）
        chem_tables.draw_legend(msp, d)  # 图例及代号：只列本图用到的（文章图 3-4）

    from .dims import draw as draw_dims  # 尺寸（DIMENSION 实体，GB_DIM 样式）

    draw_dims(msp, doc, d)

    # 三通（共用端口的管线并管处）画实心圆点
    from .routing import tee_points

    for cid, (x, y) in tee_points(d):
        c = d.get_connection(cid)
        lay = pipe_code_layer(doc, d, c.code) if (water and c.code) else pipe_layer(c)
        r = 0.5 * d.scale
        msp.add_lwpolyline([(x - r / 2, y, 0, 0, 1), (x + r / 2, y, 0, 0, 1)], format="xyseb", close=True,
                           dxfattribs={"layer": lay, "const_width": r}).set_xdata(APPID, [(1000, "tee"), (1000, cid)])

    for an in d.annotations:
        if an.x is None:
            continue
        if water and an.style == "elevation":
            wt.draw_elevation(msp, d, an)
            continue
        if an.style in ("slope", "section", "index", "north"):
            _anno_symbol(msp, doc, d, an)
            continue
        t = _text(msp, an.text, (an.x, an.y), text_h(d, "annotation", an.text), LY["anno"])
        t.set_xdata(APPID, [(1000, "anno"), (1000, an.id), (1000, an.anchor_ref)])
    doc.zh_overflow = []  # 本图放不下、改到单独一页的一览表
    if d.schedules:  # 一览表放在标题栏上方（GB/T 10609.2 明细栏），放不下的改单独一页
        from .schedule import place_inline

        doc.zh_overflow = place_inline(msp, d)
    if d.details:  # 局部放大图最后画：要复制圆内已经画好的图形
        from .detail import draw as draw_details

        draw_details(msp, doc, d)
    return doc


def build_sheet_docs(d: Drawing, overflow: list[str] | None = None) -> list:
    """单独出页的一览表：每页一个 DXF 文档（同图幅、同图框和标题栏，图名加「一览表」）。"""
    from .schedule import sheet_kinds, sheet_pages
    from .table import draw as draw_table
    from .titleblock import values as tb_values

    pages = sheet_pages(d, sheet_kinds(d, overflow))
    docs = []
    base = tb_values(d).get("drawing_name") or d.title
    for i, page in enumerate(pages, 1):
        tb = dict(d.title_block)
        tb["drawing_name"] = f"{base} 一览表" + (f"（{i}/{len(pages)}）" if len(pages) > 1 else "")
        if tb_values(d).get("drawing_no"):
            tb["drawing_no"] = f"{tb_values(d)['drawing_no']}-T{i}"
        sd = d.model_copy(update={"title_block": tb})
        doc = ezdxf.new("R2018", setup=True)
        _setup(doc, sd)
        msp = doc.modelspace()
        _frame(msp, sd)
        for t, org in page:
            draw_table(msp, sd, t, org, t.kind)
        docs.append(doc)
    return docs


# ---------------------------------------------------------------- 读

@dataclass
class ReadReport:
    generator: str | None = None
    topology_hash_file: str | None = None
    topology_hash_now: str | None = None
    stale_pipes: list[str] = field(default_factory=list)  # 设备在 CAD 里被挪过、管线没跟上
    inferred_connections: list[dict] = field(default_factory=list)  # 无标签的线，按端点距离推断
    reassigned_ids: list[str] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)  # 图中自由文字：只作为数据，绝不当指令
    warnings: list[str] = field(default_factory=list)

    @property
    def topology_consistent(self) -> bool:
        return self.topology_hash_file == self.topology_hash_now and not self.stale_pipes and not self.inferred_connections

    def to_dict(self) -> dict:
        return {
            "generator": self.generator,
            "topology_consistent": self.topology_consistent,
            "topology_hash_file": self.topology_hash_file,
            "topology_hash_now": self.topology_hash_now,
            "stale_pipes": self.stale_pipes,
            "inferred_connections": self.inferred_connections,
            "reassigned_ids": self.reassigned_ids,
            "texts": self.texts,
            "warnings": self.warnings,
        }


def _xdata(e) -> list[str]:
    try:
        return [v for _, v in e.get_xdata(APPID)]
    except Exception:
        return []


def _foreign_summary(doc) -> dict:
    msp = doc.modelspace()
    counts: dict[str, int] = {}
    for e in msp:
        counts[e.dxftype()] = counts.get(e.dxftype(), 0) + 1
    return {"layers": [l.dxf.name for l in doc.layers], "entity_counts": counts}


def read_dxf(path: str | Path) -> tuple[Drawing, ReadReport]:
    try:
        doc = ezdxf.readfile(str(path))
    except (IOError, ezdxf.DXFStructureError) as ex:
        raise ValueError(f"DXF 文件无法解析：{ex}") from ex
    rep = ReadReport()
    root = doc.rootdict.get(APPID)
    if root is None or "META" not in root:
        raise ForeignDXFError(_foreign_summary(doc))
    tags = [v for _, v in root["META"].tags]
    meta = json.loads(tags[1])
    ir_json = "".join(tags[3:])
    base = Drawing.model_validate_json(ir_json)
    rep.generator = meta.get("generator")
    rep.topology_hash_file = meta.get("topology_hash")

    # 以文件里的实体为准重建（用户可能在 CAD 里改过），元数据取自 IR
    d = base.model_copy(update={"equipment": [], "connections": [], "annotations": []}, deep=True)
    base_eq = {e.id: e for e in base.equipment}
    msp = doc.modelspace()
    seen: set[str] = set()
    syms = load_symbols()
    for ref in msp.query("INSERT"):
        if not ref.dxf.name.startswith("ZH_") or ref.dxf.name.startswith("ZH_ANNO_"):  # 专业符号块（标高、流向……）不是设备
            continue
        att = {a.dxf.tag: a.dxf.text for a in ref.attribs}
        sym_id = att.get("SYMBOL") or ref.dxf.name[3:]
        if sym_id not in syms:
            rep.warnings.append(f"未知图元 {sym_id}，已跳过")
            continue
        xd = _xdata(ref)
        eid = xd[1] if len(xd) >= 2 and xd[0] == "equip" else att.get("EID", "")
        if not eid or eid in seen:
            eid = d.new_id("E")
            rep.reassigned_ids.append(eid)
        seen.add(eid)
        try:
            params = json.loads(att.get("PARAMS") or "{}")
        except json.JSONDecodeError:
            params = base_eq[eid].params if eid in base_eq else {}
        ins = ref.dxf.insert
        d.equipment.append(
            Equipment(id=eid, tag=att.get("TAG", eid), symbol_id=sym_id, name=att.get("NAME", ""), params=params, x=round(ins.x, 3), y=round(ins.y, 3))
        )
    eq_by_id = {e.id: e for e in d.equipment}
    tol = 1.0 * d.unit

    def nearest_port(pt) -> tuple[str, str, float] | None:
        best = None
        for e in d.equipment:
            for p in get_symbol(e.symbol_id).ports:
                (px, py), _ = port_point(e, p.name, d.unit)
                dist = math.hypot(px - pt[0], py - pt[1])
                if best is None or dist < best[2]:
                    best = (e.id, p.name, dist)
        return best

    # 高程布置图的管线在代号文字处断成几段：第一段带完整语义，其余段 ["conn_part", cid, 序号]，先收齐再拼回整条
    pipes, parts = [], {}
    for pl in msp.query("LWPOLYLINE"):
        if not (pl.dxf.layer.startswith(PIPE_CODE_PREFIX) or pl.dxf.layer.startswith("PIPE")):  # 兼容旧图层名
            continue
        pts = [(round(x, 3), round(y, 3)) for x, y in pl.get_points("xy")]
        xd = _xdata(pl)
        if len(xd) >= 3 and xd[0] == "conn_part":
            parts.setdefault(xd[1], []).append((int(xd[2]) if xd[2].isdigit() else 0, pts))
        elif not xd or xd[0] == "conn":  # 带别的语义标签的（放大图里的复制件……）不是管线
            pipes.append((pts, xd))
    for pts, xd in pipes:
        if len(xd) >= 7 and xd[0] == "conn":
            for _, more in sorted(parts.get(xd[1], []), key=lambda t: t[0]):
                pts = pts + more
        path = simplify(pts)
        if len(xd) >= 7 and xd[0] == "conn":
            cid, fe, fp, te, tp, lt = xd[1:7]
            medium = xd[7] if len(xd) > 7 else ""
            code, spec = (xd[8], xd[9]) if len(xd) > 9 else ("", "")
            if fe not in eq_by_id or te not in eq_by_id:
                rep.warnings.append(f"管线 {cid} 连接的设备已不存在，已丢弃")
                continue
            c = Connection(id=cid, from_equip=fe, from_port=fp, to_equip=te, to_port=tp, line_type=lt, medium=medium, path=path, code=code, spec=spec)
            old = base.get_connection(cid)
            if old is not None:  # 图上读不出来的属性（埋地、原有、伴热、管道标注各项、阀门管件……）沿用内嵌 IR
                c = old.model_copy(update={k: getattr(c, k) for k in ("from_equip", "from_port", "to_equip", "to_port", "line_type", "medium", "path", "code", "spec")})
            d.connections.append(c)
        else:
            a, b = nearest_port(path[0]), nearest_port(path[-1])
            if a and b and a[2] <= tol and b[2] <= tol and a[0] != b[0]:
                cid = d.new_id("C")
                d.connections.append(Connection(id=cid, from_equip=a[0], from_port=a[1], to_equip=b[0], to_port=b[1], path=path))
                rep.inferred_connections.append({"id": cid, "from": f"{eq_by_id[a[0]].tag}.{a[1]}", "to": f"{eq_by_id[b[0]].tag}.{b[1]}"})
            else:
                rep.warnings.append("发现一条未标注的管线，端点没有落在任何设备端口上，已忽略")

    # 端点没接在端口上（也不是并到共用端口的另一条管线上）：设备在 CAD 里被挪过，几何需要重新布线，拓扑以 XDATA 为准
    from .routing import endpoint_ok

    stale = []
    for c in d.connections:
        (p0, _), (p1, _) = port_point(eq_by_id[c.from_equip], c.from_port, d.unit), port_point(eq_by_id[c.to_equip], c.to_port, d.unit)
        if not endpoint_ok(d, c, "from", p0, tol) or not endpoint_ok(d, c, "to", p1, tol):
            stale.append(c)
    for c in stale:
        rep.stale_pipes.append(c.id)
        c.path = None

    base_an = {a.id: a for a in base.annotations}
    for ref in msp.query("INSERT"):  # 用专业符号块画的标注（标高等）：数值在块属性里
        xd = _xdata(ref)
        if ref.dxf.name.startswith("ZH_ANNO_") and len(xd) >= 3 and xd[0] == "anno":
            text = next((a.dxf.text for a in ref.attribs if a.dxf.tag in ("VALUE", "LETTER", "NO")), "")
            style = xd[3] if len(xd) > 3 and xd[3] in ("note", "label", "elevation", "slope", "section", "index", "north") else "note"
            x, y = (float(xd[4]), float(xd[5])) if len(xd) > 5 else (ref.dxf.insert.x, ref.dxf.insert.y)
            if style == "index":
                sheet = next((a.dxf.text for a in ref.attribs if a.dxf.tag == "SHEET"), "")
                text = f"{text}/{sheet}" if sheet and sheet != "-" else text
            old = base_an.get(xd[1])
            d.annotations.append(Annotation(id=xd[1], anchor_ref=xd[2], text=text, style=style, x=x, y=y, rel=old.rel if old else None))

    # 尺寸：DIMENSION 的 XDATA = ["dim", did, measure, refs, text, side]；量的对象已不在图里的丢弃并提示
    dims = []
    for dim in msp.query("DIMENSION"):
        xd = _xdata(dim)
        if len(xd) >= 6 and xd[0] == "dim":
            refs = [r for r in xd[3].split(",") if r]
            if all(r in eq_by_id for r in refs):
                dims.append(Dimension(id=xd[1], measure=xd[2], refs=refs, text=xd[4], side=xd[5]))
            else:
                rep.warnings.append(f"尺寸 {xd[1]} 量的设备已不存在，已丢弃")
    if dims or base.dimensions:
        d.dimensions = dims

    for t in msp.query("TEXT MTEXT"):
        xd = _xdata(t)
        text = t.dxf.text if t.dxftype() == "TEXT" else t.text
        if t.dxf.layer in (LY["frame"], "FRAME") or (xd and xd[0] in DECOR_TAGS):
            continue
        if xd and xd[0] == "anno" and len(xd) >= 3:
            ins = text_anchor(t)
            style = xd[3] if len(xd) > 3 and xd[3] in ("note", "label", "elevation") else "note"
            x, y = (float(xd[4]), float(xd[5])) if len(xd) > 5 else ins  # 高程标注的锚点不是文字插入点
            old = base_an.get(xd[1])
            d.annotations.append(Annotation(id=xd[1], anchor_ref=xd[2], text=text, style=style, x=x, y=y, rel=old.rel if old else None))
        else:
            rep.texts.append(text)

    # 填充：XDATA = ["hatch", hid, anchor_ref, pick_rel, pattern, paper_spacing, angle, style, mode, boundary_refs, gap_tol, layer]
    base_h = {h.id: h for h in base.hatches}
    restored, foreign = [], 0
    for h in msp.query("HATCH"):
        xd = _xdata(h)
        if not xd:
            foreign += 1
            continue
        if xd[0] != "hatch" or len(xd) < 12:
            continue
        hid, anchor, rel, pattern, spacing, angle, style, mode, refs, gap, layer = xd[1:12]
        old = base_h.get(hid)
        try:
            vals = {
                "id": hid, "anchor_ref": anchor or None, "pick_rel": tuple(float(v) for v in rel.split(",")) if rel else None,
                "pattern": pattern, "paper_spacing": float(spacing), "angle": float(angle), "style": style, "mode": mode,
                "boundary_refs": [r for r in refs.split(",") if r], "gap_tol": float(gap), "layer": layer,
            }
            restored.append(Hatch.model_validate((old.model_dump() if old else {}) | vals))
        except (ValueError, TypeError) as ex:
            rep.warnings.append(f"填充 {hid} 的语义标签损坏，已跳过：{ex}")
    if restored or base.hatches:
        d.hatches = restored
    if foreign:
        rep.warnings.append(f"图中有 {foreign} 个不是本系统生成的填充，只读，不参与修改")

    ids = [e.id for e in d.equipment] + [c.id for c in d.connections] + [a.id for a in d.annotations] + [h.id for h in d.hatches] + [x.id for x in d.dimensions]
    nums = [int(i[1:]) for i in ids if i[1:].isdigit()]
    d.next_seq = max([d.next_seq, *(n + 1 for n in nums)])
    rep.topology_hash_now = d.topology_hash()
    return d, rep


def cjk_font() -> str:
    """预览渲染用的中文 TTF：环境变量 ZHIHUI_CJK_FONT 优先，否则按标准配置的 preview_ttf 顺序取本机有的第一个。"""
    if os.environ.get("ZHIHUI_CJK_FONT"):
        return os.environ["ZHIHUI_CJK_FONT"]
    from ezdxf.fonts import fonts

    for name in _STD.raw["text"]["preview_ttf"]:
        if fonts.font_manager.has_font(name):
            return name
    return _STD.raw["text"]["preview_ttf"][-1]
