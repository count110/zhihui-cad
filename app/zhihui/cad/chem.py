"""化工工艺流程图专用的图面要素（文章第二、三章；规则和取值在标准配置 hg_pfd 的 pipes 节，带 source / verified）。

- 管线交叉断开一根（文章图 2-6：横线连续、竖线断开；主物料与辅助物料交叉断辅助线），替换原来的跨线符号；
- 长管线按间距补箭头（文章 3.1 节：每根管线都要以箭头表示物料流向）；
- 厂房地平线 + EL±0.000，地下设备画一段带剖面线的地面，params.el（安装高度，m，只能来自用户）把设备抬高（文章 2.1 节）；
- 设备上的一段隔热层图例、一段伴热管（params.insulation / params.tracing，文章 2.1 节）；
- 施工流程图画设备管口（排液、放空、仪表接口，文章 3.1.3 节）；
- 相同的局部系统只画一套，其余画细双点画线方框（文章 2.1 节）；
- 图纸接续的空心箭头里写图号或序号（文章图 2-5）；
- 同一管段号等级不同时画等级分界线（文章 3.2 节）。
"""

from __future__ import annotations

import math

from ezdxf.enums import TextEntityAlignment as TA

from zhihui.ir import Drawing

from .standards import LY, for_drawing, text_h
from .symbols import WATER, body_box, footprint, get_symbol, inflate, symbol_box, usable_area

OFFSHEET = ("offsheet", "offsheet_left")


def cfg(d: Drawing) -> dict:
    return for_drawing(d).raw.get("pipes", {})


# ---------------------------------------------------------------- 管线交叉
def crossing_style(d: Drawing) -> str:
    if d.template_id == WATER:
        return "jump"
    return cfg(d).get("crossing", {}).get("style", "jump")


def _segs(d: Drawing):
    from .routing import EPS

    out = []
    for c in d.connections:
        for a, b in zip(c.path or [], (c.path or [])[1:]):
            if abs(a[1] - b[1]) < EPS and abs(a[0] - b[0]) > EPS:
                out.append((c, "h", a[1], min(a[0], b[0]), max(a[0], b[0])))
            elif abs(a[0] - b[0]) < EPS and abs(a[1] - b[1]) > EPS:
                out.append((c, "v", a[0], min(a[1], b[1]), max(a[1], b[1])))
    return out


def crossings(d: Drawing) -> list[tuple]:
    """[(横管连接, 竖管连接, (x, y))]：两条管线真正交叉（不含端点相接）的地方。"""
    from .routing import EPS

    segs = _segs(d)
    out = []
    for ch, _, y, x0, x1 in [t for t in segs if t[1] == "h"]:
        for cv, _, x, y0, y1 in [t for t in segs if t[1] == "v"]:
            if ch.id != cv.id and x0 + EPS < x < x1 - EPS and y0 + EPS < y < y1 - EPS:
                out.append((ch, cv, (x, y)))
    return out


def break_points(d: Drawing) -> dict[str, list[tuple[float, float]]]:
    """交叉处断开哪一根：主物料（process）和辅助物料交叉断辅助线；同级时按配置断竖线（文章图 2-6）。返回 {cid: [交叉点]}。"""
    cc = cfg(d).get("crossing", {})
    main = lambda c: c.line_type == "process"  # noqa: E731
    out: dict[str, list] = {}
    for ch, cv, p in crossings(d):
        if cc.get("aux_broken", True) and main(ch) != main(cv):
            broken = cv if main(ch) else ch
        else:
            broken = cv if cc.get("broken", "vertical") == "vertical" else ch
        out.setdefault(broken.id, []).append(p)
    return out


def gap_half(d: Drawing) -> float:
    return cfg(d).get("crossing", {}).get("gap_mm", 2.0) / 2 * d.scale


# ---------------------------------------------------------------- 箭头
def arrow_tri(tip, ux, uy, d: Drawing):
    alen, half = 3.0 * d.unit, 0.8 * d.unit
    bx, by = tip[0] - ux * alen, tip[1] - uy * alen
    return [tip, (bx - uy * half, by + ux * half), (bx + uy * half, by - ux * half)]


def mid_arrows(d: Drawing, c, avoid: list[tuple[float, float]] = ()) -> list:
    """长管段中间补的箭头（尖端朝流向）：段长每满 arrow_repeat_mm 补一个，均匀分布，躲开交叉点和三通。"""
    rep = cfg(d).get("arrow_repeat_mm")
    if not rep or d.template_id == WATER or not c.path:
        return []
    step = rep * d.scale
    alen = 3.0 * d.unit
    out = []
    for a, b in zip(c.path, c.path[1:]):
        L = math.dist(a, b)
        n = int(L // step)
        if n < 1:
            continue
        ux, uy = (b[0] - a[0]) / L, (b[1] - a[1]) / L
        for k in range(1, n + 1):
            t = L * k / (n + 1) + alen / 2
            tip = (a[0] + ux * t, a[1] + uy * t)
            if any(math.dist(tip, p) < 3 * alen for p in avoid):
                continue
            out.append(arrow_tri(tip, ux, uy, d))
    return out


# ---------------------------------------------------------------- 地平线、安装高度
def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def elev_offset(d: Drawing, e) -> float:
    """布局时设备相对地平线的竖向偏移（模型 mm）：params.el 安装高度（m）按 mm_per_m 抬高；地下设备整台放到地平线下面。"""
    if d.template_id == WATER:
        return 0.0
    g = cfg(d).get("ground", {})
    if e.params.get("underground"):
        x0, y0, x1, y1 = get_symbol(e.symbol_id).bbox
        return -((y1 - y0) * d.unit + g.get("underhang_mm", 3) * d.scale * 2)
    el = _num(e.params.get("el"))
    return el * g.get("mm_per_m", 4) * d.unit if el else 0.0


def ground_on(d: Drawing) -> bool:
    if d.template_id == WATER or not any(e.placed for e in d.equipment):
        return False
    if d.ground is not None:
        return d.ground
    return d.stage == "construction" or any(e.params.get("underground") or _num(e.params.get("el")) for e in d.equipment)


def ground_line(d: Drawing) -> tuple[float, float, float] | None:
    """(y, x0, x1)：地平线在落地设备（没有抬高、不在地下、不是管道附件）底边下面一点。"""
    if not ground_on(d):
        return None
    u = d.unit
    eqs = [e for e in d.equipment if e.placed and get_symbol(e.symbol_id).category != "X"]
    standing = [e for e in eqs if not e.params.get("underground") and not _num(e.params.get("el"))] or eqs
    base = [symbol_box(e, u)[1] for e in standing]
    if not base:
        return None
    # 地平线压在落地设备和地上管线的下面（管线从设备底部引出后也在地面以上走），不和管线、管线上的文字打架
    under = {e.id for e in d.equipment if e.params.get("underground")}
    base += [p[1] for c in d.connections if c.from_equip not in under and c.to_equip not in under for p in c.path or []]
    y = min(base) - cfg(d).get("ground", {}).get("underhang_mm", 3) * d.scale
    xs = [symbol_box(e, u) for e in eqs]
    ux0, _, ux1, _ = usable_area(d)
    return y, max(ux0, min(b[0] for b in xs) - 8 * u), min(ux1, max(b[2] for b in xs) + 8 * u)


def draw_ground(msp, d: Drawing) -> None:
    from .dxf_io import APPID, _text

    gl = ground_line(d)
    if gl is None:
        return
    y, x0, x1 = gl
    s = d.scale
    xd = [(1000, "ground"), (1000, "line")]
    msp.add_line((x0, y), (x1, y), dxfattribs={"layer": LY["flow_arrow"]}).set_xdata(APPID, xd)
    txt = cfg(d).get("ground", {}).get("text", "EL±0.000")
    from .standards import text_width as _tw
    from .water import _seg_in_box

    th = text_h(d, "annotation", txt)
    tw = _tw(txt, th / s) * s
    lines = [(a, b) for c in d.connections for a, b in zip(c.path or [], (c.path or [])[1:])]
    boxes = [footprint(e, d.unit, d.template_id) for e in d.equipment if e.placed]
    tx = x0 + 1 * s  # 标高写在地平线左端上方；被管线或设备挡住就沿地平线往右挪
    k = 0.0
    while tx + tw < x1:
        b = (tx, y + 0.5 * s, tx + tw, y + 0.8 * s + th)
        if not any(_seg_in_box(p, q, b) for p, q in lines) and not any(b[0] < o[2] and o[0] < b[2] and b[1] < o[3] and o[1] < b[3] for o in boxes):
            break
        k += 1
        tx = x0 + 1 * s + k * 2 * s
    else:
        tx = x0 + 1 * s
    _text(msp, txt, (tx, y + 0.8 * s), th, LY["anno"]).set_xdata(APPID, xd)
    for e in d.equipment:  # 地下设备：在它上方画一段地面，地面下侧画剖面线
        if not (e.placed and e.params.get("underground")):
            continue
        bx0, _, bx1, _ = symbol_box(e, d.unit)
        h = msp.add_hatch(color=256, dxfattribs={"layer": LY["flow_arrow"]})
        h.set_pattern_fill("ANSI31", scale=1.5 * s / 3.175)
        h.paths.add_polyline_path([(bx0 - 2 * s, y), (bx1 + 2 * s, y), (bx1 + 2 * s, y - 1.5 * s), (bx0 - 2 * s, y - 1.5 * s)], is_closed=True)
        h.set_xdata(APPID, [(1000, "ground"), (1000, e.id)])


# ---------------------------------------------------------------- 设备隔热层、伴热管、管口
def _band(d: Drawing, e, side: str) -> tuple[float, float]:
    """在设备左（右）侧找一段不压端口的竖向区间，长为本体高度的 40%。"""
    x0, y0, x1, y1 = body_box(e, d.unit)
    H = y1 - y0
    from .symbols import label_box

    sym = get_symbol(e.symbol_id)
    pys = [e.y + p.y * d.unit for p in sym.ports if p.dir == side]
    lb = label_box(e, d.unit, d.template_id)  # 设备旁的位号也让开
    if (side == "right" and lb[0] >= x1 - 1) or (side == "left" and lb[2] <= x0 + 1):
        pys += [lb[1], lb[3], (lb[1] + lb[3]) / 2]
    if nozzles_on(d):  # 施工流程图的管口也要让开
        pys += [e.y + n["y"] * d.unit for n in sym.nozzles if n["dir"] == side]
    half = 0.2 * H
    for f in (0.5, 0.3, 0.7, 0.2, 0.8):
        cy = y0 + H * f
        if all(abs(py - cy) > half + 2 * d.unit for py in pys):
            return cy - half, cy + half
    return y0 + 0.4 * H, y0 + 0.6 * H


def draw_equipment_extras(msp, d: Drawing, e) -> None:
    from . import tracing
    from .dxf_io import APPID, _text

    if d.template_id == WATER:
        return
    s, u = d.scale, d.unit
    ins = str(e.params.get("insulation") or "").strip()
    tr = str(e.params.get("tracing") or "").strip().upper()
    x0, y0, x1, y1 = body_box(e, u)
    if ins:  # 隔热层图例：贴本体右侧一条细实线带，中间斜线（文章 2.1 节）
        ya, yb = _band(d, e, "right")
        w = 1.5 * s
        xd = [(1000, "insulation"), (1000, e.id), (1000, ins)]
        msp.add_lwpolyline([(x1, ya), (x1 + w, ya), (x1 + w, yb), (x1, yb)], dxfattribs={"layer": LY["flow_arrow"]}).set_xdata(APPID, xd)
        y = ya
        while y + w <= yb + 1e-6:
            msp.add_line((x1, y), (x1 + w, y + w), dxfattribs={"layer": LY["flow_arrow"]}).set_xdata(APPID, xd)
            y += 1.2 * s
        _text(msp, ins, (x1 + w + 0.8 * s, (ya + yb) / 2), text_h(d, "annotation", ins), LY["anno"], TA.MIDDLE_LEFT).set_xdata(APPID, xd)
    if tr:  # 伴热管：贴本体左侧一段带短钩的复合线 + 伴热类型代号（表 2-10）
        ya, yb = _band(d, e, "left")
        xd = [(1000, "tracing"), (1000, e.id), (1000, tr)]
        tracing.draw_path(msp, d, [(x0, ya), (x0, yb)], xd)
        _text(msp, tr, (x0 - 2.5 * s, (ya + yb) / 2), text_h(d, "annotation", tr), LY["anno"], TA.MIDDLE_RIGHT).set_xdata(APPID, xd)


def nozzles_on(d: Drawing) -> bool:
    return d.template_id != WATER and d.stage == "construction"


def nozzle_lines(d: Drawing, e) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    from .symbols import DIRS

    if not nozzles_on(d) or not e.placed:
        return []
    u = d.unit
    out = []
    for n in get_symbol(e.symbol_id).nozzles:
        dx, dy = DIRS[n["dir"]]
        a = (e.x + n["x"] * u, e.y + n["y"] * u)
        out.append((a, (a[0] + dx * n["len"] * u, a[1] + dy * n["len"] * u)))
    return out


def draw_nozzles(msp, d: Drawing, e) -> None:
    """管口：单根细实线（文章 3.1.3 节、规范化要求 5.2）。"""
    from .dxf_io import APPID

    for (a, b), n in zip(nozzle_lines(d, e), get_symbol(e.symbol_id).nozzles):
        msp.add_line(a, b, dxfattribs={"layer": LY["equip"]}).set_xdata(APPID, [(1000, "nozzle"), (1000, e.id), (1000, n["name"])])


def nozzle_boxes(d: Drawing) -> list:
    s = d.scale
    out = []
    for e in d.equipment:
        for a, b in nozzle_lines(d, e):
            out.append(inflate((min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])), 1.0 * s))
    return out


# ---------------------------------------------------------------- 相同的局部系统
def repeat_boxes(d: Drawing) -> list[tuple]:
    """[(RepeatSystem, 方框, [文字行])]：方框放在那一套设备旁边的空白处（右、下、左、上），避开设备、管线和位号栏。"""
    from .standards import text_width as tw
    from .tagrow import tag_band

    if not d.repeats or not all(e.placed for e in d.equipment):
        return []
    s, u = d.scale, d.unit
    taken = [inflate(footprint(e, u, d.template_id), 1 * u) for e in d.equipment if e.placed]
    band = tag_band(d)
    if band:
        taken.append(band)
    lines = [(a, b) for c in d.connections for a, b in zip(c.path or [], (c.path or [])[1:])]
    area = usable_area(d)
    out = []
    from .water import _seg_in_box

    for r in d.repeats:
        rows = [r.name] + ([r.numbers] if r.numbers else [])
        h = text_h(d, "annotation", r.name)
        w = max(tw(t, h / s) for t in rows) * s + 8 * s
        H = len(rows) * h * 1.6 + 6 * s
        mem = [d.get_equipment(m) for m in r.members]
        mem = [m for m in mem if m and m.placed]
        if mem:
            boxes = [footprint(m, u, d.template_id) for m in mem]
            mx0, my0 = min(b[0] for b in boxes), min(b[1] for b in boxes)
            mx1, my1 = max(b[2] for b in boxes), max(b[3] for b in boxes)
        else:
            mx0, my0, mx1, my1 = area[0], area[1], area[0], area[1]
        g = 6 * u
        cands = []
        for k in range(0, 12):
            off = k * 5 * u
            cy = (my0 + my1) / 2
            cx = (mx0 + mx1) / 2
            cands += [(mx1 + g + off, cy - H / 2), (cx - w / 2 + off, my0 - g - H - off), (mx0 - g - w - off, cy - H / 2), (cx - w / 2 - off, my1 + g + off)]
        pick = None
        for x, y in cands:
            b = (x, y, x + w, y + H)
            if b[0] < area[0] or b[1] < area[1] or b[2] > area[2] or b[3] > area[3]:
                continue
            if any(b[0] < o[2] and o[0] < b[2] and b[1] < o[3] and o[1] < b[3] for o in taken):
                continue
            if any(_seg_in_box(p, q, inflate(b, 1 * s)) for p, q in lines):
                continue
            pick = b
            break
        if pick is None:
            x, y = cands[0]
            pick = (x, y, x + w, y + H)
        taken.append(inflate(pick, 2 * u))
        out.append((r, pick, rows))
    return out


def draw_repeats(msp, d: Drawing) -> None:
    from . import dashsplit
    from .dxf_io import APPID, _text

    for r, (x0, y0, x1, y1), rows in repeat_boxes(d):
        xd = [(1000, "repeat"), (1000, r.id)]
        dashsplit.draw(msp, d, [(x0, y0), (x1, y0), (x1, y1), (x0, y1)], LY["phantom"], closed=True, xdata=xd, part_xdata=lambda i: xd)
        h = text_h(d, "annotation", r.name)
        pitch = h * 1.6
        top = (y0 + y1) / 2 + pitch * (len(rows) - 1) / 2
        for i, t in enumerate(rows):
            _text(msp, t, ((x0 + x1) / 2, top - i * pitch), text_h(d, "annotation", t), LY["anno"], TA.MIDDLE_CENTER).set_xdata(APPID, xd)


# ---------------------------------------------------------------- 图纸接续、等级分界
def draw_offsheet_text(msp, d: Drawing, e) -> None:
    """空心箭头内写相关图纸的图号或序号（params.sheet，只能来自用户）。"""
    from .dxf_io import APPID, _text

    if e.symbol_id not in OFFSHEET:
        return
    sheet = str(e.params.get("sheet") or "").strip()
    if not sheet:
        return
    x0, y0, x1, y1 = symbol_box(e, d.unit)
    rect = (x1 - x0) * 10 / 13
    cx = x0 + rect / 2 if e.symbol_id == "offsheet" else x1 - rect / 2
    h = min(text_h(d, "tag", sheet), (y1 - y0) * 0.6)
    _text(msp, sheet, (cx, (y0 + y1) / 2), h, LY["anno"], TA.MIDDLE_CENTER).set_xdata(APPID, [(1000, "offsheet"), (1000, e.id)])


def draw_terminal_name(msp, d: Drawing, e) -> None:
    """管道来源 / 去向（pipe_terminal）在箭头正上方写名称（如「上水」「液态物料去物料贮槽」），位号不显示（文章 3.1 节）。"""
    from .dxf_io import APPID, _text
    from .symbols import TERMINALS

    if e.symbol_id not in TERMINALS:
        return
    sym = get_symbol(e.symbol_id)
    txt = e.name or e.tag
    lx, ly = sym.label["x"] * d.unit, sym.label["y"] * d.unit
    _text(msp, txt, (e.x + lx, e.y + ly), text_h(d, "name", txt), LY["pipe_text"], TA.TOP_CENTER).set_xdata(APPID, [(1000, "name_label"), (1000, e.id)])


def grade_breaks(d: Drawing) -> list[tuple[str, tuple[float, float], bool]]:
    """同一管段号但管道等级不同：在后一段的起点画等级分界线（文章 3.2 节）。返回 [(cid, 点, 管段是否竖直)]。"""
    seen: dict[str, object] = {}
    out = []
    for c in d.connections:
        if not c.pipe_no or not c.path or len(c.path) < 2:
            continue
        o = seen.get(c.pipe_no)
        if o is not None and o.grade != c.grade:
            a, b = c.path[0], c.path[1]
            out.append((c.id, a, abs(a[0] - b[0]) < 1e-6))
        seen.setdefault(c.pipe_no, c)
    return out


def draw_grade_breaks(msp, d: Drawing) -> None:
    from .dxf_io import APPID

    s = d.scale
    for cid, (x, y), vertical in grade_breaks(d):
        c = d.get_connection(cid)
        a, b = c.path[0], c.path[1]
        L = math.dist(a, b) or 1.0
        t = min(4 * s, L / 2)
        px, py = x + (b[0] - a[0]) / L * t, y + (b[1] - a[1]) / L * t
        p0, p1 = ((px - 2 * s, py), (px + 2 * s, py)) if vertical else ((px, py - 2 * s), (px, py + 2 * s))
        msp.add_line(p0, p1, dxfattribs={"layer": LY["pipe_text"]}).set_xdata(APPID, [(1000, "grade_break"), (1000, cid)])


def label_text(d: Drawing, c) -> str:
    """管线上写什么：施工流程图写管道标注（文章图 2-7），同一管段号且其余都相同的后几段只标管径；其他阶段写介质名。"""
    from .codes import pipe_label

    if d.template_id == WATER:
        return c.medium
    from .symbols import TERMINALS

    ends = [d.get_equipment(c.from_equip), d.get_equipment(c.to_equip)]
    if d.stage != "construction" and any(e is not None and e.symbol_id in TERMINALS and e.name == c.medium for e in ends):
        return ""  # 起点、终点已经写了这个名称，管线上不重复
    if d.stage == "construction" and c.pipe_no:
        for o in d.connections:
            if o.id == c.id:
                break
            if o.pipe_no == c.pipe_no and (o.material, o.grade, o.temp_code, o.insulation_code) == (c.material, c.grade, c.temp_code, c.insulation_code):
                return pipe_label(c, size_only=True) or ""
        return pipe_label(c)
    return c.medium
