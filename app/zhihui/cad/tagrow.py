"""设备位号栏（HG/T 20519.2 第 3.3.1 条、图 3.3.1-1；文章 2.2 节、图 3-1）。

每台设备一项：位号写在「位号线」上方，设备名称写在下方，尽可能正对设备；排不下时错开成两行。
阀门等管道附件不编设备位号，不进位号栏。物料流程图、施工流程图在名称下方再写一行特性参数（params.feature，如 F=60m²，只能来自用户）。

位号栏紧贴设备带：最下一行的底边在全图最高的设备或管线上方 GAP_BAND（10 mm，规范化要求 5.7 的 8～12 mm），
不再固定在图框顶部；图框顶部预留的那一条（symbols.tag_strip）放不下时才退回去。
"""

from __future__ import annotations

from dataclasses import dataclass

from zhihui.ir import Drawing

from .symbols import NAME_HEIGHT, TAG_HEIGHT, get_symbol, symbol_box, tag_strip, text_width

ROW_PITCH = 11.0  # 两行之间的距离（图纸 mm，乘 unit）
GAP = 3.0
GAP_BAND = 10.0  # 位号栏与设备带的距离（真实图纸 mm，乘 scale）
FEATURE_ROW = 5.0  # 特性参数多占的一行（图纸 mm，乘 unit）


@dataclass
class TagItem:
    equip_id: str
    tag: str
    name: str
    x: float  # 中心
    line_y: float  # 位号线高度
    width: float  # 位号线长度
    feature: str = ""  # 特性参数（物料流程图及以上）


def _bands(eqs: list, unit: float) -> list[list]:
    """按设备本体的上下重叠关系分排（自上而下），每排在位号栏里占一行。"""
    groups: list[tuple[float, float, list]] = []
    for e in sorted(eqs, key=lambda e: -e.y):
        _, y0, _, y1 = symbol_box(e, unit)
        for gi, (g0, g1, members) in enumerate(groups):
            if y0 < g1 and y1 > g0:
                members.append(e)
                groups[gi] = (min(g0, y0), max(g1, y1), members)
                break
        else:
            groups.append((y0, y1, [e]))
    return [m for _, _, m in sorted(groups, key=lambda g: -g[1])]


def layout_tag_row(d: Drawing) -> list[TagItem]:
    u = d.unit
    x0, y0, x1, y1 = tag_strip(d)
    eqs = [e for e in d.equipment if e.placed and get_symbol(e.symbol_id).category != "X"]
    if not eqs:
        return []

    def width(e) -> float:
        return max(text_width(e.tag, TAG_HEIGHT), text_width(e.name, NAME_HEIGHT), text_width(feature(d, e), NAME_HEIGHT)) * u + 2 * u

    def spread(row: list) -> dict[str, float] | None:
        """尽量正对设备，左右不重叠；整行超宽返回 None。"""
        xs: dict[str, float] = {}
        right = x0
        for e in row:
            cx = max(e.x, right + width(e) / 2)
            xs[e.id] = cx
            right = cx + width(e) / 2 + GAP * u
        overflow = right - GAP * u - x1
        if overflow > 0:
            for k in xs:
                xs[k] -= overflow
            if xs[row[0].id] - width(row[0]) / 2 < x0:
                return None
        return xs

    rows: list[list] = []
    for band in _bands(eqs, u):
        band = sorted(band, key=lambda e: (e.x, e.tag))
        if spread(band) is None:  # 一排设备的位号放不下，交错成两行
            rows += [band[0::2], band[1::2]]
        else:
            rows.append(band)
    has_feature = any(feature(d, e) for e in eqs)
    pitch = (ROW_PITCH + (FEATURE_ROW if has_feature else 0)) * u
    below = (1.0 + NAME_HEIGHT + (FEATURE_ROW if has_feature else 0) + 0.5) * u  # 位号线以下：名称（+ 特性参数）
    above = (1.0 + TAG_HEIGHT + 1.0) * u  # 位号线以上：位号
    top_line = y1 - (2 + TAG_HEIGHT + 1.5) * u
    floating = d.template_id == "pfd_default"
    if floating:  # 贴着设备带：最下一行的底边在全图内容顶上 GAP_BAND
        lowest = content_top(d) + GAP_BAND * d.scale + below
        cand = lowest + (len(rows) - 1) * pitch
        if cand + above <= y1 - 1 * u:
            top_line = cand
    else:
        pitch = min(pitch, ((y1 - y0) / len(rows)) - 0.5 * u)
    items = []
    for row_no, row in enumerate(rows):
        xs = spread(row) or {e.id: e.x for e in row}
        for e in row:
            items.append(TagItem(e.id, e.tag, e.name, xs[e.id], top_line - row_no * pitch, width(e), feature(d, e)))
    return items


def feature(d: Drawing, e) -> str:
    """特性参数只在物料流程图、施工流程图写（规范化要求 5.7）。"""
    return str(e.params.get("feature") or "").strip() if d.stage in ("material", "construction") else ""


def content_top(d: Drawing) -> float:
    """全图设备和管线的最高点（模型 mm），位号栏贴在它上面。"""
    from .symbols import label_box

    ys = []
    for e in d.equipment:
        if e.placed:
            ys += [symbol_box(e, d.unit)[3], label_box(e, d.unit, d.template_id)[3]]
    for c in d.connections:
        ys += [p[1] for p in c.path or []]
    return max(ys) if ys else tag_strip(d)[1]


def tag_band(d: Drawing):
    """位号栏实际占的范围 (x0, y0, x1, y1)；没有位号项时返回 None。"""
    items = layout_tag_row(d)
    if not items:
        return None
    u = d.unit
    has_feature = any(it.feature for it in items)
    below = (1.0 + NAME_HEIGHT + (FEATURE_ROW if has_feature else 0) + 0.5) * u
    above = (1.0 + TAG_HEIGHT + 1.0) * u
    x0, _, x1, _ = tag_strip(d)
    return (x0, min(it.line_y for it in items) - below, x1, max(it.line_y for it in items) + above)
