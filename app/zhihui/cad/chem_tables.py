"""化工流程图上的表：物料组成表（文章 3.1.2 节、图 3-2）和「图例及代号」（文章「流程图上应配备图例」、图 3-4）。

物料组成表：表头 序号 / 名称 / 流量(kg/h) / 摩尔分率(%)，最后一行合计由程序计算（各项相加，小数位取输入里最多的）；
数值只能来自用户；表格线和指引线细实线；放在所在管线附近的空白处，指引线连到管线。
图例及代号：只列本图实际用到的阀门管件符号、图纸接续标记、伴热管、物料代号（表 2-6）、管道材料代号（表 2-7）、
仪表字母组合（表 2-13）；放在图框右上角，放不下再试标题栏上方、左上角。每个条目带 XDATA ["legend", 键]，自检核对图上是否都还用着。
"""

from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation

from ezdxf.enums import TextEntityAlignment as TA

from zhihui.ir import Drawing

from .standards import LY, text_h, text_width
from .table import Cell, Table
from .table import draw as draw_table

ROW_H = 6.0  # 图纸 mm
HEAD = ("序号", "名称", "流量(kg/h)", "摩尔分率(%)")


# ---------------------------------------------------------------- 物料组成表
def _dec(s: str) -> Decimal | None:
    try:
        return Decimal(str(s).strip())
    except (InvalidOperation, ValueError):
        return None


def total(vals: list[str]) -> str:
    """合计：各项相加，小数位取输入里最多的；有一项不是数字就留空（不猜）。"""
    ds = [_dec(v) for v in vals if str(v).strip() != ""]
    if not ds or any(x is None for x in ds):
        return ""
    places = max((-x.as_tuple().exponent if x.as_tuple().exponent < 0 else 0) for x in ds)
    return f"{sum(ds):.{places}f}"


def stream_table(st) -> Table:
    rows = [[str(i + 1), r.name, r.flow_kg_h, r.mole_pct] for i, r in enumerate(st.rows)]
    rows.append([str(len(st.rows) + 1), "合计", total([r.flow_kg_h for r in st.rows]), total([r.mole_pct for r in st.rows])])
    data = [list(HEAD)] + rows
    cols = [max(10.0, max(text_width(row[k], 3.5) for row in data) + 4.0) for k in range(4)]
    cells = [Cell(r, c, text, key=HEAD[c]) for r, row in enumerate(data) for c, text in enumerate(row)]
    return Table(cols=cols, rows=[ROW_H] * len(data), cells=cells, kind="stream")


def _anchor_point(d: Drawing, cid: str):
    c = d.get_connection(cid)
    if c is None or not c.path or len(c.path) < 2:
        return None
    segs = list(zip(c.path, c.path[1:]))
    a, b = max(segs, key=lambda ab: math.dist(*ab))
    return ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)


def _free(d: Drawing, box, taken, lines) -> bool:
    from .symbols import label_area_ok
    from .water import _seg_in_box

    if not label_area_ok(d, box):
        return False
    if any(box[0] < o[2] and o[0] < box[2] and box[1] < o[3] and o[1] < box[3] for o in taken):
        return False
    return not any(_seg_in_box(p, q, box) for p, q in lines)


def _content(d: Drawing):
    from . import instruments
    from .symbols import footprint, inflate
    from .tagrow import tag_band

    s = d.scale
    taken = [inflate(footprint(e, d.unit, d.template_id), 1.5 * s) for e in d.equipment if e.placed] + instruments.boxes(d)
    band = tag_band(d)
    if band:
        taken.append(band)
    lines = [(a, b) for c in d.connections for a, b in zip(c.path or [], (c.path or [])[1:])]
    return taken, lines


def layout(d: Drawing) -> list[dict]:
    """[{st, table, box, origin, anchor, lead}]：物料组成表的位置（确定性，按登记顺序）；图例在后面单独放，避开这些表。"""
    if not d.stream_tables or not all(e.placed for e in d.equipment):
        return []
    s = d.scale
    taken, lines = _content(d)
    out = []
    for st in d.stream_tables:
        p = _anchor_point(d, st.anchor)
        if p is None:
            continue
        t = stream_table(st)
        w, h = t.width * s, t.height * s
        pick = None
        for dist in (8, 14, 20, 28, 36, 46, 58):
            g = dist * s
            for ox, oy in ((-w / 2, g), (-w / 2, -g - h), (-w - g, -h / 2), (g, -h / 2), (-w - g, g), (g, g), (-w - g, -g - h), (g, -g - h)):
                box = (p[0] + ox, p[1] + oy, p[0] + ox + w, p[1] + oy + h)
                if _free(d, box, taken, lines):
                    pick = box
                    break
            if pick:
                break
        if pick is None:
            pick = (p[0] - w / 2, p[1] + 8 * s, p[0] + w / 2, p[1] + 8 * s + h)
        # 指引线：表格边上离控制点最近的点 → 管线上的点
        q = (min(max(p[0], pick[0]), pick[2]), min(max(p[1], pick[1]), pick[3]))
        out.append({"st": st, "table": t, "box": pick, "origin": (pick[0], pick[1]), "anchor": p, "lead": (q, p)})
        taken.append(pick)
        lines.append((q, p))
    return out


def draw_streams(msp, d: Drawing) -> None:
    from .dxf_io import APPID, _text

    for it in layout(d):
        draw_table(msp, d, it["table"], it["origin"], xd_tag=f"stream:{it['st'].id}", outer_layer=LY["table"])  # 表格线细实线（文章 3.1.2 节）
        msp.add_line(*it["lead"], dxfattribs={"layer": LY["table"]}).set_xdata(APPID, [(1000, "table"), (1000, f"stream:{it['st'].id}")])
        if it["st"].title:
            x0, _, x1, y1 = it["box"]
            _text(msp, it["st"].title, ((x0 + x1) / 2, y1 + 1 * d.scale), text_h(d, "table_cell", it["st"].title), LY["table"],
                  TA.BOTTOM_CENTER).set_xdata(APPID, [(1000, "table"), (1000, f"stream:{it['st'].id}")])


# ---------------------------------------------------------------- 图例及代号
def legend_entries(d: Drawing) -> list[dict]:
    """本图实际用到的条目（顺序：符号类在前，代号类在后）：[{key, kind, label, fitting?}]。"""
    from . import fittings
    from .chem import OFFSHEET
    from .codes import lookup
    from .instruments import check_letters

    if d.template_id == "water_elevation":
        return []
    out: list[dict] = []
    seen = set()

    def add(key, **kw):
        if key not in seen:
            seen.add(key)
            out.append({"key": key, **kw})

    for c in d.connections:
        for f in c.fittings:
            add(f"fitting:{f.type}", kind="fitting", fitting=f.type, label=fittings.library()[f.type]["name_cn"])
    if any(e.symbol_id in OFFSHEET for e in d.equipment):
        add("offsheet", kind="offsheet", label="图纸接续标记")
    if any(c.tracing for c in d.connections) or any(e.params.get("tracing") for e in d.equipment):
        add("tracing", kind="tracing", label="伴热管")
    codes = []
    for c in d.connections:
        code = c.code or (c.pipe_no.rstrip("0123456789") if c.pipe_no else "")
        if code and lookup("material_codes", code):
            codes.append(code)
    for code in sorted(set(codes)):
        add(f"code:{code}", kind="text", label=f"{code}  {lookup('material_codes', code)['name']}")
    for m in sorted({c.material for c in d.connections if c.material}):
        add(f"material:{m}", kind="text", label=f"{m}  {lookup('pipe_material', m)['name']}")
    for letters in sorted({i.letters for i in d.instruments}):
        lvl, msg = check_letters(letters)
        meaning = msg.replace(" · ", "") if lvl == "ok" and msg else ""
        add(f"instrument:{letters}", kind="text", label=f"{letters}  {meaning}".rstrip())
    return out


def _legend_metrics(d: Drawing) -> tuple[float, float, float]:
    """(标题字高, 条目字高, 行高)，图纸 mm：字高按图幅升档，行高跟着字高走。"""
    tt = text_h(d, "legend_title", "图例及代号") / d.scale
    tn = text_h(d, "legend_name", "图例") / d.scale
    return tt, tn, max(ROW_H, tn + 2.5)


def legend_table(d: Drawing) -> tuple[float, float, list[dict]] | None:
    """(宽, 高, 行)：图纸 mm。第一行标题「图例及代号」，后面每行一个条目（符号类左边画符号）。"""
    ents = legend_entries(d)
    if not ents:
        return None
    tt, tn, row = _legend_metrics(d)
    sym_w = 14.0
    w = max(max(text_width(e["label"], tn) for e in ents) + sym_w + 6, text_width("图例及代号", tt) + 8)
    return w, tt + 3 + row * len(ents) + 1, ents


def legend_box(d: Drawing):
    """图例的位置（模型坐标）：右上角 → 标题栏上方 → 左上角，避开设备、管线、位号栏、仪表、物料组成表。"""
    from .symbols import inner_frame, title_h
    from .titleblock import size as tb_size

    lt = legend_table(d)
    if lt is None or not all(e.placed for e in d.equipment):
        return None
    s = d.scale
    w, h = lt[0] * s, lt[1] * s
    taken, lines = _content(d)
    taken += [it["box"] for it in layout(d)]
    fx0, fy0, fx1, fy1 = inner_frame(d)
    m = 3 * s
    tb_top = fy0 + title_h(d) * s
    tw = tb_size(d)[0] * s if d.frame else 0
    cands = [(fx1 - m - w, fy1 - m - h), (fx1 - m - w, tb_top + m), (fx0 + m, fy1 - m - h), (fx1 - tw - m - w, fy0 + m)]
    for x, y in cands:
        box = (x, y, x + w, y + h)
        if _free(d, box, taken, lines):
            return box
    x, y = cands[0]  # 都放不下：仍放右上角，自检会报文字与图线冲突
    return (x, y, x + w, y + h)


def draw_legend(msp, d: Drawing) -> None:
    from . import fittings
    from .dxf_io import APPID, _text

    box = legend_box(d)
    if box is None:
        return
    w, h, ents = legend_table(d)
    s = d.scale
    x0, y0, x1, y1 = box
    xd0 = [(1000, "legend"), (1000, "_title")]
    _text(msp, "图例及代号", ((x0 + x1) / 2, y1 - 1 * s), text_h(d, "legend_title", "图例及代号"), LY["legend"], TA.TOP_CENTER).set_xdata(APPID, xd0)
    tt, tn, row = _legend_metrics(d)
    for i, e in enumerate(ents):
        cy = y1 - (tt + 3 + (i + 0.5) * row) * s
        xd = [(1000, "legend"), (1000, e["key"])]
        sx = x0 + 2 * s
        if e["kind"] == "fitting":  # 在一小段管线上画符号
            lib = fittings.library()[e["fitting"]]
            k = fittings.scale(d)
            mid = sx + 6 * s
            span = max(lib["cut"] * k, 0.5 * s)
            if lib["kind"] == "end":
                msp.add_line((sx, cy), (mid, cy), dxfattribs={"layer": LY["legend"]}).set_xdata(APPID, xd)
            else:
                msp.add_line((sx, cy), (mid - span, cy), dxfattribs={"layer": LY["legend"]}).set_xdata(APPID, xd)
                msp.add_line((mid + span, cy), (sx + 12 * s, cy), dxfattribs={"layer": LY["legend"]}).set_xdata(APPID, xd)
            _draw_symbol(msp, d, lib, (mid, cy), k, xd, corner=lib["kind"] == "corner")
        elif e["kind"] == "offsheet":
            pts = [(sx, cy - 1.5 * s), (sx + 8 * s, cy - 1.5 * s), (sx + 10 * s, cy), (sx + 8 * s, cy + 1.5 * s), (sx, cy + 1.5 * s)]
            msp.add_lwpolyline(pts, close=True, dxfattribs={"layer": LY["legend"]}).set_xdata(APPID, xd)
        elif e["kind"] == "tracing":
            from . import tracing

            msp.add_line((sx, cy - 0.6 * s), (sx + 12 * s, cy - 0.6 * s), dxfattribs={"layer": LY["legend"]}).set_xdata(APPID, xd)
            tracing.draw_path(msp, d, [(sx, cy - 0.6 * s), (sx + 12 * s, cy - 0.6 * s)], xd)
        tx = sx + (14 * s if e["kind"] != "text" else 0)
        _text(msp, e["label"], (tx, cy), text_h(d, "legend_name", e["label"]), LY["legend"], TA.MIDDLE_LEFT).set_xdata(APPID, xd)


def _draw_symbol(msp, d, lib, p, k, xd, corner=False):
    from .dxf_io import APPID

    px, py = p
    for g in lib["graphics"]:
        t = g["type"]
        T = (lambda x, y: (px + x * k, py + y * k))  # noqa: E731
        if t == "polyline":
            msp.add_lwpolyline([T(x, y) for x, y in g["points"]], close=g.get("closed", False), dxfattribs={"layer": LY["legend"]}).set_xdata(APPID, xd)
        elif t == "circle":
            msp.add_circle(T(*g["c"]), g["r"] * k, dxfattribs={"layer": LY["legend"]}).set_xdata(APPID, xd)
            if g.get("fill"):
                h = msp.add_hatch(color=256, dxfattribs={"layer": LY["legend"]})
                h.set_solid_fill(color=256)
                h.paths.add_edge_path().add_arc(T(*g["c"]), g["r"] * k, 0, 360)
                h.set_xdata(APPID, xd)
        elif t == "arc":
            msp.add_arc(T(*g["c"]), g["r"] * k, g["start"], g["end"], dxfattribs={"layer": LY["legend"]}).set_xdata(APPID, xd)
        elif t == "fill":
            h = msp.add_hatch(color=256, dxfattribs={"layer": LY["legend"]})
            h.set_solid_fill(color=256)
            h.paths.add_polyline_path([T(x, y) for x, y in g["points"]], is_closed=True)
            h.set_xdata(APPID, xd)


def reserved_boxes(d: Drawing) -> list:
    """管线文字要让开的范围：物料组成表（含指引线所在的表框）、图例。"""
    out = [it["box"] for it in layout(d)]
    lb = legend_box(d)
    if lb:
        out.append(lb)
    return out


def params_text(d: Drawing, c) -> str:
    """管线旁的工艺参数（温度、压力）：物料流程图及以上才显示（规范化要求 9.2）。"""
    if d.stage == "scheme" or not c.params or d.template_id == "water_elevation":
        return ""
    return "  ".join(f"{k}={v}" if not str(k).startswith("_") else str(v) for k, v in c.params.items())
