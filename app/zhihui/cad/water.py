"""出图模板 water_elevation：给水处理「工艺流程及高程布置图」。

与化工 PFD（pfd_default）的区别：
- 布局按高程：主流程（代号 YS/QS 的管线串起来的构筑物）从左到右排在图纸下半部，
  每个构筑物的水面线按真实高程定竖向位置，体现水流靠重力逐级跌落；投药等子系统放在上半部。
- 不画位号栏，构筑物正下方注中文名称；管线按代号区分（线上写代号、线旁平行半箭头）；
  右上角图例、顶部大标题、标题栏左侧说明、水利行业标题栏。
布局函数只算坐标和标注位置，画图函数供 dxf_io.write_dxf 调用；同一输入永远得到同一张图。
"""

from __future__ import annotations

import itertools
import math

from zhihui.ir import Annotation, Drawing, Equipment

from .layout import SPACING, LayoutError, LayoutResult, _layers
from .standards import LY, PIPE_CODE_PREFIX, fit_h, for_drawing, text_h
from .standards import load as _load_std


def for_drawing_default_text_width(text: str, h: float) -> float:
    return _load_std("water_supply").text_width(text, h)
from .routing import collect_segments, route_connections
from .symbols import (
    WATER,
    WATER_NAME_HEIGHT,
    WATER_NAME_STYLES,
    WATER_TITLE_H,
    WATER_TITLE_W,
    Box,
    boxes_overlap,
    footprint,
    footprint_rel,
    get_symbol,
    inflate,
    inner_frame,
    label_box,
    port_point,
    symbol_box,
    tag_strip,
    text_width,
    usable_area,
)

# 管道代号 → 名称（图例默认文字，Drawing.code_names 可覆盖）
CODE_NAMES: dict[str, str] = {
    "YS": "原水处理管线",
    "QS": "清水管线",
    "FX": "滤池反冲洗管道",
    "CL": "二氧化氯投加管道",
    "PAC": "碱式氯化铝投加管道",
    "Q": "气体反冲洗管道",
}
MAIN_CODES = {"YS", "QS"}  # 主流程管线
# 线宽（0.01 mm）：主流程粗线，加药等中粗
CODE_LINEWEIGHT = {"YS": 70, "QS": 70, "FX": 35, "CL": 35, "PAC": 35, "Q": 35}

# 高程对齐：params.elevations 里的键 → 图元 elev_ports 里的水平线，按顺序取第一个两边都有的
ALIGN_KEYS: tuple[tuple[str, str], ...] = (
    ("water", "water"),
    ("water_in", "water"),
    ("max_water", "water"),
    ("water_out", "water_out"),
    ("water_out", "water"),
    ("min_water", "min_water"),
    ("top", "top"),
    ("bottom", "bottom"),
)
VSCALE_MAX = 12.0  # 自动竖向比例的上限（图纸 mm / m），高差小时也不过分夸大
VSCALE_MIN = 0.5
# 竖向比例只能取 GB/T 14690 的比例系列：1 m 在图上画 1000/N mm（1:100 → 10 mm/m，1:150 → 6.67 mm/m）
GROUP_GAP = 10.0  # 子系统内设备间距（图纸 mm）
SYS_TITLE_H = 5.0  # 子系统标题字高（字高系列 5 号，加粗下划线）
SYS_TITLE_ROOM = 8.5  # 子系统下方留给标题的高度
BAND_GAP = 12.0  # 上半部子系统与下半部主流程之间
ELEV_TEXT = 2.5  # 标高数值字高（标高符号块：直角等腰三角形高 3 mm，GB/T 50001）
# 引线长度：三角尖 4 + 数值起点 3.6 + 「000.00」6 个字母数字的宽度 + 0.6 余量（字宽按 B 型估算，见 standards.text_width）
ELEV_W = round(max(16.0, 4.0 + 3.6 + for_drawing_default_text_width("000.00", ELEV_TEXT) + 0.6), 1)
ELEV_APEX, ELEV_SYM_H = 4.0, 3.0  # 三角形尖端离引线起点的距离、三角形高
CODE_TEXT = 2.5  # 管道代号字高
LEGEND_ROW, LEGEND_HEAD = 6.0, 8.0
JUMP_R = 1.2  # 跨线符号半径（图纸 mm）
MIN_GAP_MM = 0.7  # 平行线最小间隙（GB/T 4457.4）
GROUND_STRIP = 2.4  # 地面线下方素土剖面带的高度（图纸 mm）
EARTH_SPACING = 2.0  # 地面素土剖面线（EARTH）在图纸上的重复间距（mm）
WATER_WF = 0.7  # 本模板文字的宽度因子（与全局一致）


def doc_of(layout):
    return layout.doc


def code_layer(code: str) -> str:
    return PIPE_CODE_PREFIX + "".join(ch for ch in code.upper() if ch.isalnum())


def elevations(e: Equipment) -> dict[str, float]:
    raw = e.params.get("elevations")
    if not isinstance(raw, dict):
        return {}
    out = {}
    for k, v in raw.items():
        try:
            out[str(k)] = float(v)
        except (TypeError, ValueError):
            continue
    return out


def align_key(e: Equipment) -> tuple[str, str] | None:
    """(高程键, 图元水平线) ：构筑物按哪条线对到哪个高程。没有可对齐的返回 None。"""
    ev, ports = elevations(e), get_symbol(e.symbol_id).elev_ports
    for key, port in ALIGN_KEYS:
        if key in ev and port in ports:
            return key, port
    return None


def legend_items(d: Drawing) -> list[dict[str, str]]:
    """图例 = 图中实际用到的代号 ∪ 用户要求列出的代号，按默认表顺序。"""
    used = {c.code for c in d.connections if c.code} | {c for c in d.legend_codes if c}
    order = list(CODE_NAMES)
    codes = sorted(used, key=lambda c: (order.index(c) if c in order else len(order), c))
    return [{"code": c, "name": d.code_names.get(c) or CODE_NAMES.get(c, c)} for c in codes]


def legend_box(d: Drawing, items: list[dict] | None = None) -> Box | None:
    """右上角图例的范围（模型 mm）；没有图例返回 None。"""
    items = legend_items(d) if items is None else items
    if not items:
        return None
    u = d.unit
    _, _, ux1, uy1 = usable_area(d)
    w = 28 + max(text_width(it["name"], 3.5) for it in items) + 4
    h = LEGEND_HEAD + LEGEND_ROW * len(items)
    return (ux1 - w * u, uy1 - h * u, ux1, uy1)


def main_ids(d: Drawing) -> list[str]:
    """主流程设备：YS/QS 管线的两端；图里没写代号时退回到 process 管线，再加上所有给了高程的设备。"""
    conns = [c for c in d.connections if c.code in MAIN_CODES] or [c for c in d.connections if not c.code and c.line_type == "process"]
    ids = {x for c in conns for x in (c.from_equip, c.to_equip)}
    ids |= {e.id for e in d.equipment if elevations(e)}
    return [e.id for e in sorted(d.equipment, key=lambda e: (e.tag, e.id)) if e.id in ids]


def _groups(d: Drawing, main: set[str]) -> list[tuple[str, list[str]]]:
    """上半部的子系统：先按 Drawing.systems，剩下的按连通分量。返回 [(名称, 设备 id)]。"""
    rest = [e.id for e in sorted(d.equipment, key=lambda e: (e.tag, e.id)) if e.id not in main]
    out: list[tuple[str, list[str]]] = []
    taken: set[str] = set()
    for g in d.systems:
        mem = [i for i in rest if i in g.members and i not in taken]
        if mem:
            out.append((g.name, mem))
            taken.update(mem)
    left = [i for i in rest if i not in taken]
    adj: dict[str, set[str]] = {i: set() for i in left}
    for c in d.connections:
        if c.from_equip in adj and c.to_equip in adj:
            adj[c.from_equip].add(c.to_equip)
            adj[c.to_equip].add(c.from_equip)
    seen: set[str] = set()
    for i in left:
        if i in seen:
            continue
        comp, stack = [], [i]
        while stack:
            v = stack.pop()
            if v in seen:
                continue
            seen.add(v)
            comp.append(v)
            stack.extend(sorted(adj[v] - seen))
        out.append(("", [x for x in left if x in comp]))
    return out


def _group_layout(d: Drawing, members: list[str], rel: dict[str, Box], gap: float, max_h: float) -> tuple[dict[str, tuple[float, float]], float, float]:
    """子系统内部排布，返回 ({设备: 相对组左下角的坐标}, 宽, 高)。

    先按组内连接单独分列（_layers），同列自下而上叠放；某列每台设备都只接前一列的一台不同设备时
    （如 溶液池→计量泵），按端口对齐高度，管线走直线。分列后太高（如 4 个药剂罐叠成一列）就改成排成一排。
    """
    s = d.unit
    eqs = {i: d.get_equipment(i) for i in members}
    cols = _layers(d, list(eqs.values()))
    widths = [max(rel[i][2] - rel[i][0] for i in col) for col in cols]
    x: dict[str, float] = {}
    cursor = 0.0
    for col, w in zip(cols, widths):
        for i in col:
            x[i] = cursor + w / 2 - (rel[i][0] + rel[i][2]) / 2
        cursor += w + gap
    y: dict[str, float] = {}
    for ci, col in enumerate(cols):
        aligned = False
        if ci:
            prev = set(cols[ci - 1])
            feed = {}
            for i in col:
                ins = [c for c in d.connections if c.to_equip == i and c.from_equip in prev]
                if len(ins) == 1:
                    feed[i] = ins[0]
            if len(feed) == len(col) and len({c.from_equip for c in feed.values()}) == len(col):
                cand = {}
                for i, c in feed.items():
                    po = get_symbol(eqs[c.from_equip].symbol_id).port(c.from_port)
                    pi = get_symbol(eqs[i].symbol_id).port(c.to_port)
                    cand[i] = y[c.from_equip] + po.y * s - pi.y * s
                srt = sorted(col, key=lambda i: cand[i])
                if all(cand[a] + rel[a][3] + gap / 2 <= cand[b] + rel[b][1] for a, b in zip(srt, srt[1:])):
                    y.update(cand)
                    aligned = True
        if not aligned:
            level = 0.0
            for i in reversed(col):
                y[i] = level - rel[i][1]
                level = y[i] + rel[i][3] + gap
    lo = min(y[i] + rel[i][1] for i in members)
    hi = max(y[i] + rel[i][3] for i in members)
    if hi - lo <= max_h:
        return {i: (x[i], y[i] - lo) for i in members}, cursor - gap, hi - lo
    pos, cursor = {}, 0.0
    for i in _row_order(d, members):
        pos[i] = (cursor - rel[i][0], -rel[i][1])
        cursor += rel[i][2] - rel[i][0] + gap
    return pos, cursor - gap, max(rel[i][3] - rel[i][1] for i in members)


def system_regions(d: Drawing) -> list[dict]:
    """各子系统的标题（加粗下划线，写在组的下方）和虚线框位置，由成员当前坐标算出，布局、布线、出图共用。"""
    u, tpl = d.unit, d.template_id
    main = set(main_ids(d))
    out = []
    for g in d.systems:
        mem = [d.get_equipment(i) for i in g.members if i not in main]
        mem = [e for e in mem if e is not None and e.placed]
        if not mem:
            continue
        fps = [footprint(e, u, tpl) for e in mem]
        x0, y0 = min(f[0] for f in fps), min(f[1] for f in fps)
        x1 = max(f[2] for f in fps)
        cx, top = (x0 + x1) / 2, y0 - 1.5 * u
        w = text_width(g.name, SYS_TITLE_H) * u
        title_box = (cx - w / 2, top - (SYS_TITLE_H + 1.5) * u, cx + w / 2, top)
        ids = {e.id for e in mem}
        boxed_ids = sorted(ids) if g.boxed is True else [i for i in (g.boxed or []) if i in ids]
        box = None
        if boxed_ids:
            bf = [footprint(d.get_equipment(i), u, tpl) for i in boxed_ids]
            # 外扩 1.5 mm：小于管口引出段（3 mm），框边不会贴着进出的管线走（平行线间距 ≥ 0.7 mm）
            box = inflate((min(f[0] for f in bf), min(f[1] for f in bf), max(f[2] for f in bf), max(f[3] for f in bf)), 1.5 * u)
        out.append({"id": g.id, "name": g.name, "title_pos": (cx, top), "title_box": title_box, "box": box})
    return out


def _row_order(d: Drawing, members: list[str]) -> list[str]:
    """子系统排成一排：每台「汇点」设备两侧放它的上游（接左侧端口的放左边），如 盐酸罐-发生器-氯酸钠罐。"""
    mset = set(members)
    pred: dict[str, list[tuple[float, str]]] = {i: [] for i in members}
    has_succ = {i: False for i in members}
    for c in sorted(d.connections, key=lambda c: c.id):
        if c.from_equip in mset and c.to_equip in mset and c.from_equip != c.to_equip:
            p = get_symbol(d.get_equipment(c.to_equip).symbol_id).port(c.to_port)
            pred[c.to_equip].append((p.x if p else 0.0, c.from_equip))
            has_succ[c.from_equip] = True
    order: list[str] = []
    seen: set[str] = set()

    def place(v: str) -> list[str]:
        if v in seen:
            return []
        seen.add(v)
        ps = sorted(pred[v])
        lefts = [u for x, u in ps if x < 0]
        rights = [u for x, u in ps if x >= 0]
        return [w for u in lefts for w in place(u)] + [v] + [w for u in rights for w in place(u)]

    for v in [i for i in members if not has_succ[i]] + members:
        order += place(v)
    return order


def vscale_grid(d: Drawing) -> list[float]:
    """可选的竖向比例（图纸 mm / m），从大到小：都在 GB/T 14690 比例系列里（1:N，N = 1000 / vscale）。"""
    std = for_drawing(d)
    vals = [1000.0 / n for n in std.scale_series(1000 / VSCALE_MAX, 1000 / VSCALE_MIN)]
    return sorted(vals, reverse=True)


def vertical_ratio(vscale: float) -> float:
    """竖向比例 1:N 的 N。"""
    return 1000.0 / vscale


def _inherit_ground(eqs: list[Equipment]) -> list[float | None]:
    """按从左到右的顺序给每台设备一个地面高程：没给的沿用左边，最左边的沿用右边第一个。"""
    grounds = [elevations(e).get("ground") for e in eqs]
    first = next((g for g in grounds if g is not None), None)
    out, last = [], first
    for g in grounds:
        last = g if g is not None else last
        out.append(last)
    return out


def _labels_fit(d: Drawing, ids: list[str], ys: dict[str, float], v: float, tol: float = 3.0) -> bool:
    """每个高程标注都落在本构筑物图形的竖向范围内（±tol 图纸 mm），标注不会飘离构筑物。"""
    s = d.unit
    for i in ids:
        e = d.get_equipment(i)
        ak = align_key(e)
        if not ak:
            continue
        ev, sym = elevations(e), get_symbol(e.symbol_id)
        ref = ys[i] + sym.elev_ports[ak[1]] * s
        lo, hi = ys[i] + sym.bbox[1] * s - tol * s, ys[i] + sym.bbox[3] * s + tol * s
        if any(not lo <= ref + (h - ev[ak[0]]) * v * s <= hi for h in ev.values()):
            return False
    return True


def _main_ys(d: Drawing, ids: list[str], v: float) -> tuple[dict[str, float], float, float]:
    """给定竖向比例，算主流程各设备的 y（基准高程 = 0）。返回 (y, 基准高程, 地面基准线 y)。"""
    s = d.unit
    eqs = {i: d.get_equipment(i) for i in ids}
    vals = [h for e in eqs.values() for h in elevations(e).values()]
    datum = min(vals) if vals else 0.0
    grounds = [elevations(e)["ground"] for e in eqs.values() if "ground" in elevations(e)]
    ground_y = (min(grounds) - datum) * v * s if grounds else 0.0
    near_ground = dict(zip(ids, _inherit_ground(list(eqs.values()))))
    y: dict[str, float] = {}
    for i, e in eqs.items():
        ak = align_key(e)
        sym = get_symbol(e.symbol_id)
        if ak:
            y[i] = (elevations(e)[ak[0]] - datum) * v * s - sym.elev_ports[ak[1]] * s
        elif sym.elev_ports or not sym.ports:
            # 没给高程的构筑物（如取水头部）：顶面落在相邻构筑物的地面上，没有顶面线就底部落地
            g = near_ground[i]
            gy = (g - datum) * v * s if g is not None else ground_y
            y[i] = gy - (sym.elev_ports["top"] if "top" in sym.elev_ports else sym.bbox[1]) * s
    # 管道上的小图元（管道混合器、加药点等）：端口对齐到相邻构筑物的端口，让主管线走直线；
    # 优先对齐按高程定位的构筑物（管道混合器对沉淀池进水口，而不是取水头部）
    aligned = {i for i in y if align_key(eqs[i])}
    inline = [i for i in ids if i not in y]
    for _ in range(len(inline) + 1):
        for i in inline:
            if i in y:
                continue
            e = eqs[i]
            conns = sorted(d.connections, key=lambda c: (c.from_equip not in aligned and c.to_equip not in aligned, c.id))
            for c in conns:
                if c.from_equip == i and c.to_equip in y:
                    other, op, mine = eqs.get(c.to_equip) or d.get_equipment(c.to_equip), c.to_port, c.from_port
                elif c.to_equip == i and c.from_equip in y:
                    other, op, mine = eqs.get(c.from_equip) or d.get_equipment(c.from_equip), c.from_port, c.to_port
                else:
                    continue
                po, pm = get_symbol(other.symbol_id).port(op), get_symbol(e.symbol_id).port(mine)
                if po and pm:
                    y[i] = y[other.id] + po.y * s - pm.y * s
                    break
    for i in inline:
        y.setdefault(i, ground_y - get_symbol(eqs[i].symbol_id).bbox[1] * s)
    return y, datum, ground_y


def elevation_layout(d: Drawing, spacing: str = "normal") -> LayoutResult:
    s = d.unit
    tpl = d.template_id
    col_gap = SPACING[spacing][0] * s
    if not d.equipment:
        raise LayoutError("E_EMPTY", "图中还没有设备", "先调用 place_equipment 放置设备")
    ux0, uy0, ux1, uy1 = usable_area(d)
    avail_w, avail_h = ux1 - ux0, uy1 - uy0
    main = main_ids(d)
    mset = set(main)
    eqs = sorted(d.equipment, key=lambda e: (e.tag, e.id))
    rel = {e.id: footprint_rel(e, s, tpl) for e in eqs}
    items = legend_items(d)
    leg = legend_box(d, items)
    leg_h = (leg[3] - leg[1]) if leg else 0.0

    # ---- 上半部：子系统，各排成一排，底边对齐
    groups = _groups(d, mset)
    gap_in = GROUP_GAP * s
    layouts = [_group_layout(d, mem, rel, gap_in, 0.5 * avail_h) for _, mem in groups]
    group_w = [w for _, w, _ in layouts]
    group_h = [h + (SYS_TITLE_ROOM * s if name else 0.0) for (name, _), (_, _, h) in zip(groups, layouts)]
    upper_h = max([leg_h, *group_h]) if (groups or leg) else 0.0

    # ---- 下半部：主流程从左到右
    cols = _layers(d, [d.get_equipment(i) for i in main]) if main else []
    order = [i for col in cols for i in col]
    widths = [rel[i][2] - rel[i][0] for i in order]
    # 两座构筑物之间要放高程标注，间距给满；挨着管道小图元（混合器、加药点等）的间距减半
    big = {i for i in order if get_symbol(d.get_equipment(i).symbol_id).elev_ports}
    weights = [1.0 if (a in big and b in big) else 0.5 for a, b in zip(order, order[1:])]
    room = avail_w - sum(widths)
    if weights and col_gap * sum(weights) > room:
        squeezed = room / sum(weights)
        if squeezed * min(weights) >= GROUP_GAP * s:
            col_gap = squeezed
    gaps = [col_gap * w for w in weights]
    need_w = sum(widths) + sum(gaps)
    if need_w > avail_w:
        raise LayoutError(
            "E_LAYOUT_OVERFLOW",
            f"主流程排不下：需要 {need_w / s:.0f} mm 宽，{d.paper_size} 可用 {avail_w / s:.0f} mm",
            "传 paper_size='A2' 换大图幅",
            {"need_mm": [round(need_w / s), round(avail_h / s)]},
        )
    main_h = avail_h - upper_h - (BAND_GAP * s if upper_h else 0)

    def span(v: float) -> tuple[float, dict[str, float], float, float]:
        y, datum, gy = _main_ys(d, order, v)
        if not y:
            return 0.0, y, datum, gy
        return max(y[i] + rel[i][3] for i in y) - min(y[i] + rel[i][1] for i in y), y, datum, gy

    if d.vscale:
        v = d.vscale  # create_drawing 已校验在比例系列里
        h, ys, datum, _ = span(v)
        if h > main_h:
            raise LayoutError(
                "E_LAYOUT_OVERFLOW",
                f"按竖向比例 1 m = {v:g} mm，主流程需要 {h / s:.0f} mm 高，{d.paper_size} 只剩 {main_h / s:.0f} mm",
                "减小 vscale，或传 paper_size='A2' 换大图幅",
                {"need_mm": [round(need_w / s), round(h / s)]},
            )
    else:
        # 取放得下的最大比例；同时要求各构筑物的高程标注不飘出它自己的图形（放不到就只看放不放得下）
        v = None
        grid = vscale_grid(d)
        for need_fit in (True, False):
            for vv in grid:
                h, ys, datum, _ = span(vv)
                if h <= main_h and (not need_fit or _labels_fit(d, order, ys, vv)):
                    v = vv
                    break
            if v is not None:
                break
        if v is None:
            raise LayoutError(
                "E_LAYOUT_OVERFLOW",
                f"构筑物高程差太大，{d.paper_size} 下半部只有 {main_h / s:.0f} mm 高",
                "减小 vscale，或传 paper_size='A2' 换大图幅",
                {"need_mm": [round(need_w / s), round(h / s)]},
            )
    res = LayoutResult(mode="full", columns=len(order), rows=2 if groups else 1)
    base_y = 0.0
    if order:
        oy = uy0 - min(ys[i] + rel[i][1] for i in order)
        ox = (ux0 + ux1) / 2 - need_w / 2
        cursor = ox
        for i, w, g in zip(order, widths, gaps + [0.0]):
            e = d.get_equipment(i)
            e.x = round(cursor - rel[i][0], 1)
            e.y = round(ys[i] + oy, 1)
            cursor += w + g
        base_y = oy
    # 记下高程坐标系，标注、地面线、测试都按它换算：y = base_y + (高程 - datum) × vscale × unit
    d.elev_frame = {"datum": round(datum, 3) if order else 0.0, "base_y": round(base_y, 3), "vscale": v}

    # ---- 上半部各子系统：尽量正对它连到的主流程设备，左右不重叠，不压图例
    right_limit = (leg[0] - 6 * s) if leg else ux1
    want: list[float] = []
    for (_, mem), w in zip(groups, group_w):
        targets = [d.get_equipment(c.to_equip if c.from_equip in mem else c.from_equip) for c in d.connections
                   if (c.from_equip in mem) != (c.to_equip in mem)]
        xs = [t.x for t in targets if t is not None and t.id in mset and t.x is not None]
        want.append(sum(xs) / len(xs) - w / 2 if xs else math.inf)
    seq = sorted(range(len(groups)), key=lambda k: (want[k], k))
    gap_g = 16 * s
    lefts: dict[int, float] = {}
    cur = ux0
    for k in seq:  # 从左往右：不早于期望位置，不与左边的组重叠
        lefts[k] = max(cur, want[k] if want[k] != math.inf else cur)
        cur = lefts[k] + group_w[k] + gap_g
    limit = right_limit
    for k in reversed(seq):  # 从右往左：超出右边界（图例）的往左推，必要时挤掉组间的空当
        lefts[k] = min(lefts[k], limit - group_w[k])
        limit = lefts[k] - gap_g
    if seq and lefts[seq[0]] < ux0 - 1e-6:
        total = sum(group_w) + gap_g * (len(groups) - 1)
        raise LayoutError(
            "E_LAYOUT_OVERFLOW",
            f"上半部子系统排不下：需要 {total / s:.0f} mm 宽，只有 {(right_limit - ux0) / s:.0f} mm",
            "传 paper_size='A2' 换大图幅",
            {"need_mm": [round(total / s), round(upper_h / s)]},
        )
    band_bottom = uy1 - upper_h
    for k, (name, _) in enumerate(groups):
        base = band_bottom + (SYS_TITLE_ROOM * s if name else 0.0)  # 有名称的子系统下方留出标题
        for i, (px, py) in layouts[k][0].items():
            e = d.get_equipment(i)
            e.x = round(lefts[k] + px, 1)
            e.y = round(base + py, 1)
    for e in eqs:
        res.positions[e.tag] = (e.x, e.y)
    d.legend = items
    res.failed_routes = route_connections(d)
    res.routed = [c.id for c in d.connections if c.path]
    place_elevation_marks(d)
    return res


# ---------------------------------------------------------------- 高程标注

def elev_y(d: Drawing, h: float) -> float:
    f = d.elev_frame
    return f["base_y"] + (h - f["datum"]) * f["vscale"] * d.unit


def elev_mark_box(d: Drawing, x: float, y: float, side: int, below: bool = False) -> Box:
    """标高符号占的范围；below=True 是倒置写法（△ 在线下、数值写在线下方），上方挤不下时用。"""
    u = d.unit
    xs = (x, x + side * ELEV_W * u)
    m = MIN_GAP_MM + 0.2  # 引线与其他平行图线至少隔 0.7 mm（GB/T 4457.4）
    top = max(ELEV_SYM_H, ELEV_TEXT + 0.6) + 0.6
    if below:
        return (min(xs), y - top * u, max(xs), y + m * u)
    return (min(xs), y - m * u, max(xs), y + top * u)


def _inner_segments(e: Equipment, u: float) -> tuple[list, list]:
    """图元内部的横、竖线段（模型坐标），格式同 collect_segments：横 (y, x0, x1)，竖 (x, y0, y1)。"""
    hs, vs = [], []
    for g in get_symbol(e.symbol_id).graphics:
        if g["type"] != "polyline":
            continue
        pts = [(e.x + x * u, e.y + y * u) for x, y in g["points"]]
        if g.get("closed"):
            pts.append(pts[0])
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            if abs(y0 - y1) < 1e-9 and abs(x0 - x1) > 1e-9:
                hs.append((y0, min(x0, x1), max(x0, x1)))
            elif abs(x0 - x1) < 1e-9 and abs(y0 - y1) > 1e-9:
                vs.append((x0, min(y0, y1), max(y0, y1)))
    return hs, vs


def snap_conns(d: Drawing, eid: str, key: str) -> list:
    """标高可以吸附到哪些管：出水类（water_out、min_water）只吸到本构筑物的出水管，进水类（water_in、water、max_water）只吸到进水管。"""
    if "out" in key or key == "min_water":
        return [c for c in d.connections_of(eid) if c.from_equip == eid]
    if "water" in key:
        return [c for c in d.connections_of(eid) if c.to_equip == eid]
    return []


def snap_pipe(d: Drawing, eid: str, y: float, lo: float, hi: float, key: str = "") -> tuple[float, float, float] | None:
    """本构筑物自己的进出水管里，和标高引线平行且隔不到 0.7 mm 的横段 (y, x0, x1)：标高就标在这根管上（标管中心标高的画法），
    引线与管重合的部分不再画（图线重合只画一条）。没有返回 None。"""
    gap = (MIN_GAP_MM + 0.2) * d.unit
    best = None
    for c in snap_conns(d, eid, key):
        for (x0, y0), (x1, y1) in zip(c.path or [], (c.path or [])[1:]):
            if abs(y0 - y1) < 1e-9 and abs(y0 - y) < gap and min(x0, x1) < hi and max(x0, x1) > lo:
                if best is None or abs(y0 - y) < abs(best[0] - y):
                    best = (y0, min(x0, x1), max(x0, x1))
    return best


def _mark_pipe(d: Drawing, an: Annotation) -> tuple[float, float, float] | None:
    side, _ = mark_flags(an.id)
    lo, hi = sorted((an.x, an.x + side * ELEV_W * d.unit))
    key = an.id[len(f"EL_{an.anchor_ref}_"):].rstrip("<>v") if an.id.startswith(f"EL_{an.anchor_ref}_") else ""
    return snap_pipe(d, an.anchor_ref, an.y, lo, hi, key)


def mark_draw_y(d: Drawing, an: Annotation) -> float:
    """标高符号实际画在哪个 y：吸附到本构筑物进出水管时是管的 y，否则就是标高本身的 y。"""
    pipe = _mark_pipe(d, an)
    return pipe[0] if pipe is not None else an.y


def mark_flags(an_id: str) -> tuple[int, bool]:
    """标高标注的画法记在 id 末尾：> 向右 / < 向左，再跟 v 表示数值写在线下方。"""
    below = an_id.endswith("v")
    return (-1 if an_id.rstrip("v").endswith("<") else 1), below


def place_elevation_marks(d: Drawing) -> None:
    """在每个给出的高程处画 ▽ + 数值 + 短水平线，避开设备、名称、管线、图例和已放的标注。"""
    d.annotations = [a for a in d.annotations if a.style != "elevation"]
    if not d.elev_frame:
        return
    u, tpl = d.unit, d.template_id
    placed = [e for e in d.equipment if e.placed]
    blocks = [inflate(symbol_box(e, u), 0.3 * u) for e in placed] + [label_box(e, u, tpl) for e in placed]
    leg = legend_box(d)
    if leg:
        blocks.append(leg)
    blocks += [r["title_box"] for r in system_regions(d)]
    for poly, (lx, ly), back in retaining_walls(d):  # 挡土墙和它的字
        xs, ys = [q[0] for q in poly], [q[1] for q in poly]
        blocks.append((min(xs), min(ys), max(xs), max(ys)))
        w = text_width("挡土墙", 3.5) * u
        blocks.append((lx - w, ly, lx, ly + 2.5 * u) if back < 0 else (lx, ly, lx + w, ly + 2.5 * u))
    leader_labels: list[tuple[str, Box]] = []  # 引出线标注（能挪）：本构筑物进出水管上的这种标注不挡本构筑物的标高，出图时它再让开
    for c in d.connections:  # 管线代号、规格文字
        if c.path:
            for t in pipe_texts(d, c):
                if t[4] == 2:
                    leader_labels.append((c.id, pipe_text_box(*t)))
                else:
                    blocks.append(pipe_text_box(*t))
    skip_labels: set[str] = set()
    segs = collect_segments(d.connections)
    arrow_h: list[tuple[str, tuple[float, float, float]]] = []  # 流向半箭头（出图时会让开标高）：本构筑物进出水管上的不挡本构筑物的标高
    arrow_v: list[tuple[str, tuple[float, float, float]]] = []
    for c in d.connections:  # 线旁的流向半箭头也要避开
        for p0, p1, _ in flow_arrows(d, c):
            if abs(p0[1] - p1[1]) < 1e-9:
                arrow_h.append((c.id, (p0[1], min(p0[0], p1[0]), max(p0[0], p1[0]))))
            else:
                arrow_v.append((c.id, (p0[0], min(p0[1], p1[1]), max(p0[1], p1[1]))))
    for (x0, y0), (x1, y1) in ground_lines(d):  # 地面线和它下面的素土剖面带也要避开
        if abs(y0 - y1) < 1e-9:
            segs.horizontal.append((y0, min(x0, x1), max(x0, x1)))
            blocks.append((min(x0, x1), y0 - GROUND_STRIP * u, max(x0, x1), y0))
        else:
            segs.vertical.append((x0, min(y0, y1), max(y0, y1)))
    area = usable_area(d)
    taken: list[Box] = []

    snap_segs: set[tuple[float, float, float]] = set()
    snap_vert: set[tuple[float, float, float]] = set()  # 吸附管线自己的竖段（拐弯处）：只碰到符号下沿留的 0.7 mm 余量不算
    extra_h: list = []  # 贴墙内放时：本构筑物内部的图线（集水槽、斜管、隔墙……）
    extra_v: list = []

    def cost(b: Box, skip: Box | None = None, on_y: float | None = None, lead_y: float | None = None) -> int:
        """0 = 完全不碰；压到文字/设备/别的标注比压到管线更糟。skip：贴墙内放时不算本构筑物。
        on_y：地面标高就画在地面线上，这条线和它下面的素土带不算碰撞。"""
        if b[0] < area[0] or b[2] > area[2] or b[1] < area[1] or b[3] > area[3]:
            return 100
        same = (lambda yy: on_y is not None and abs(yy - on_y) < 1e-6)  # noqa: E731
        n = 3 * sum(boxes_overlap(b, o) for o in blocks + taken if o is not skip and not same(o[3]))
        n += 3 * sum(boxes_overlap(b, o) for cid, o in leader_labels if cid not in skip_labels)
        n += sum(1 for cid, (y, x0, x1) in arrow_h if cid not in skip_labels and b[1] < y < b[3] and x0 < b[2] and x1 > b[0])
        n += sum(1 for cid, (x, y0, y1) in arrow_v if cid not in skip_labels and b[0] < x < b[2] and y0 < b[3] and y1 > b[1])
        n += sum(1 for y, x0, x1 in segs.horizontal + extra_h if b[1] < y < b[3] and x0 < b[2] and x1 > b[0] and not same(y) and (y, x0, x1) not in snap_segs)
        n += sum(1 for x, y0, y1 in segs.vertical + extra_v if b[0] < x < b[2] and y0 < b[3] and y1 > b[1]
                 and not ((x, y0, y1) in snap_vert and min(y1, b[3]) - max(y0, b[1]) <= (MIN_GAP_MM + 0.2) * u + 1e-6))
        if lead_y is not None:  # 引线和别的横线平行且隔不到 0.7 mm（GB/T 4457.4）比压字更不行
            gap = (MIN_GAP_MM + 0.2) * u
            n += 20 * sum(1 for y, x0, x1 in segs.horizontal + extra_h if 1e-6 < abs(y - lead_y) < gap and x0 < b[2] and x1 > b[0] and not same(y) and (y, x0, x1) not in snap_segs)
        return n

    for eid in main_ids(d):
        e = d.get_equipment(eid)
        if not e.placed:
            continue
        sb = symbol_box(e, u)
        own = blocks[placed.index(e)] if e in placed else None
        def place_one(key: str, h: float):
            """一个标高的最佳位置：(score, x, box, direction, below)。"""
            y = elev_y(d, h)
            gap = (MIN_GAP_MM + 0.2) * u
            snap_segs.clear()  # 可吸附的本构筑物进出水管横段（按整段认，进出水管同高时不会把进水管也放过）
            snap_segs.update((sy, min(sx, tx), max(sx, tx)) for c in snap_conns(d, e.id, key)
                             for (sx, sy), (tx, ty) in zip(c.path or [], (c.path or [])[1:]) if abs(sy - ty) < 1e-9 and abs(sy - y) < gap)
            skip_labels.clear()
            skip_labels.update(c.id for c in snap_conns(d, e.id, key))
            snap_vert.clear()
            snap_vert.update((sx, min(sy, ty), max(sy, ty)) for c in snap_conns(d, e.id, key)
                             for (sx, sy), (tx, ty) in zip(c.path or [], (c.path or [])[1:]) if abs(sx - tx) < 1e-9)
            # 进水一侧（左）：进水水位、池内水位、地面；出水一侧（右）：出水水位、最低水位、池顶等
            pref = -1 if ("in" in key or key in ("water", "ground")) else 1
            best = None
            cands = [(sd, p, False) for sd in (pref, -pref) for p in (0, 3, 6, 10, 15)]
            if "water" in key:  # 外面都挤时，水位可以贴着池壁写在池内水面线上（地面、池顶等不进池内）
                cands += [(sd, -2, False) for sd in (pref, -pref)]
            cands += [(sd, p, True) for sd in (pref, -pref) for p in (0, 3, 6, 10, 15)]  # 再不行，数值写到线下方
            for rank, (side, push, below) in enumerate(cands):
                x = (sb[2] + push * u) if side > 0 else (sb[0] - push * u)
                if push < 0:  # 从池壁往池内画，方向与 side 相反
                    if not (sb[1] < y < sb[3]):
                        continue
                    direction = -side
                    b = elev_mark_box(d, x, y, direction, below)
                    extra_h[:], extra_v[:] = _inner_segments(e, u)
                    score = (cost(b, own, lead_y=y) + 1, rank)
                    extra_h[:], extra_v[:] = [], []
                else:
                    direction = side
                    b = elev_mark_box(d, x, y, direction, below)
                    score = (cost(b, on_y=y if key == "ground" else None, lead_y=y), rank)
                if best is None or score < best[0]:
                    best = (score, x, b, direction, below)
                if score[0] == 0:
                    break
            if best[0][0] > 0:  # 外面都碰：标在本构筑物的进出水管段上，整个符号夹在池壁和管道拐弯之间
                for sy, sx0, sx1 in sorted(snap_segs):
                    if sx1 - sx0 < (ELEV_W + 2 * MIN_GAP_MM) * u:
                        continue
                    for below in (False, True):
                        for x, direction in ((sx0 + MIN_GAP_MM * u, 1), (sx1 - MIN_GAP_MM * u, -1)):
                            b = elev_mark_box(d, x, y, direction, below)
                            score = (cost(b, own, lead_y=y), len(cands))
                            if score < best[0]:
                                best = (score, x, b, direction, below)
            return best

        items = sorted(elevations(e).items(), key=lambda kv: -kv[1])
        orders = [items] + ([list(o) for o in itertools.permutations(items) if list(o) != items] if 1 < len(items) <= 3 else [])
        plan = None
        base_taken = list(taken)
        for order in orders:  # 每种先后顺序都试：总碰撞最少的胜出，打平取高程从高到低的顺序
            taken[:] = base_taken
            got = []
            for key, h in order:
                best = place_one(key, h)
                taken.append(best[2])
                got.append((key, h, best))
            total = sum(b[0][0] for _, _, b in got)
            if plan is None or total < plan[0]:
                plan = (total, got)
            if total == 0:
                break
        taken[:] = base_taken
        for key, h, best in sorted(plan[1], key=lambda t: -t[1]):
            _, x, b, direction, below = best  # 实在避不开就取碰得最少的位置，宁可略挤也不丢高程
            y = elev_y(d, h)
            taken.append(b)
            # 画法记在 id 末尾（> 向右、< 向左，v 写在线下方），读回 DXF 后照样画
            suffix = (">" if direction > 0 else "<") + ("v" if below else "")
            d.annotations.append(
                Annotation(id=f"EL_{e.id}_{key}{suffix}", anchor_ref=e.id, text=f"{h:.2f}", style="elevation", x=round(x, 3), y=round(y, 3),
                           rel=(round((x - e.x) / u, 4), round((y - e.y) / u, 4)))  # 构筑物移动后标高跟着走
            )


def ground_lines(d: Drawing, steps_only: bool = False) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """主流程下方的地面线（各构筑物地面高程不同时成台阶），在设备和名称处断开。返回线段列表。

    steps_only=True 只返回台阶处的竖线（未断开），用来在台阶处画挡土墙。
    """
    if not d.elev_frame:
        return []
    u, tpl = d.unit, d.template_id
    main = [d.get_equipment(i) for i in main_ids(d)]
    main = sorted([e for e in main if e.placed], key=lambda e: e.x)
    filled = _inherit_ground(main)
    if not any(g is not None for g in filled):
        return []
    fps = [footprint(e, u, tpl) for e in main]
    cuts = [inflate(footprint(e, u, tpl), 0.8 * u) for e in d.equipment if e.placed]
    ux0, _, ux1, _ = usable_area(d)
    raw: list[tuple[tuple[float, float], tuple[float, float]]] = []
    bounds = []
    for k, fp in enumerate(fps):
        left = (fps[k - 1][2] + fp[0]) / 2 if k else max(ux0, fp[0] - 10 * u)
        right = (fp[2] + fps[k + 1][0]) / 2 if k + 1 < len(fps) else min(ux1, fp[2] + 10 * u)
        bounds.append((left, right))
        y = elev_y(d, filled[k])
        raw.append(((left, y), (right, y)))
        if k and abs(elev_y(d, filled[k - 1]) - y) > 1e-6:
            raw.append(((left, elev_y(d, filled[k - 1])), (left, y)))
    if steps_only:
        return [seg for seg in raw if abs(seg[0][0] - seg[1][0]) < 1e-9]
    out = []
    for (x0, y0), (x1, y1) in raw:
        pieces = [((x0, y0), (x1, y1))]
        for b in cuts:
            nxt = []
            for (a0, b0), (a1, b1) in pieces:
                if abs(b0 - b1) < 1e-9 and b[1] < b0 < b[3] and a0 < b[2] and a1 > b[0]:  # 水平段穿过设备
                    if a0 < b[0]:
                        nxt.append(((a0, b0), (b[0], b0)))
                    if a1 > b[2]:
                        nxt.append(((b[2], b0), (a1, b0)))
                elif abs(a0 - a1) < 1e-9 and b[0] < a0 < b[2] and min(b0, b1) < b[3] and max(b0, b1) > b[1]:
                    lo, hi = min(b0, b1), max(b0, b1)
                    if lo < b[1]:
                        nxt.append(((a0, lo), (a0, b[1])))
                    if hi > b[3]:
                        nxt.append(((a0, b[3]), (a0, hi)))
                else:
                    nxt.append(((a0, b0), (a1, b1)))
            pieces = nxt
        out += [p for p in pieces if math.dist(*p) > 1.5 * u]
    return out


# ---------------------------------------------------------------- 画图（write_dxf 调用）

def setup_layers(doc, d: Drawing) -> None:
    """管道代号图层（ZH-管道-YS …）：主流程粗线、其余中粗，颜色线宽按标准配置。其他图层 _setup 已建好。"""
    from .dxf_io import pipe_code_layer

    codes = set(CODE_LINEWEIGHT) | {c.code for c in d.connections if c.code} | {it["code"] for it in d.legend}
    for code in sorted(codes):
        pipe_code_layer(doc, d, code)


def _wtext(space, text: str, pos, height: float, layer: str, align):
    """本模板的标题、名称、说明。"""
    from .dxf_io import _text

    return _text(space, text, pos, height, layer, align)  # 样式、宽度因子按内容（dxf_io.text_style）


def _bold_text(space, text: str, pos, height: float, layer: str, align, xdata: list) -> None:
    """加大字高 + 错开 0.12 mm 再写一遍，模拟粗体（DXF 文字样式本身没有粗体）。"""
    from .dxf_io import APPID

    for dx in (0.0, 0.12 * height / 3.0):
        _wtext(space, text, (pos[0] + dx, pos[1]), height, layer, align).set_xdata(APPID, xdata)


def draw_frame(msp, d: Drawing) -> None:
    """图框（外细内粗）+ 右下角水利行业标题栏 180×50（JSON 模板）。"""
    from ezdxf.enums import TextEntityAlignment as TA

    from .dxf_io import _text

    s = d.scale
    w, h = d.frame_size()
    thin, thick = {"layer": LY["frame"]}, {"layer": LY["border"]}
    msp.add_lwpolyline([(0, 0), (w, 0), (w, h), (0, h)], close=True, dxfattribs=thin)
    if not d.frame:
        return
    fx0, fy0, fx1, fy1 = inner_frame(d)
    msp.add_lwpolyline([(fx0, fy0), (fx1, fy0), (fx1, fy1), (fx0, fy1)], close=True, dxfattribs=thick)
    from . import zones

    zones.draw(msp, d)
    from .titleblock import draw as draw_title_block

    draw_title_block(msp, d)  # 水利院标题栏模板（water_institute）；字段取自 title_block，没给的显示格子说明


def scale_note(d: Drawing) -> str:
    """「水平 1:50　竖向 1:150」；没有竖向比例（还没布局）时返回空。"""
    v = d.elev_frame.get("vscale") if d.elev_frame else None
    if not v:
        return ""
    return f"水平比例 1:{d.scale:g}　竖向比例 1:{vertical_ratio(v):g}"


def draw_title_and_notes(msp, d: Drawing) -> None:
    from ezdxf.enums import TextEntityAlignment as TA

    from .dxf_io import APPID, _text

    u, s = d.unit, d.scale
    x0, y0, x1, y1 = tag_strip(d)
    th = text_h(d, "drawing_title", d.title)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2 + 0.5 * u
    _bold_text(msp, d.title, (cx, cy), th, LY["title"], TA.MIDDLE_CENTER, [(1000, "title")])
    tw = text_width(d.title, th)
    uy = cy - th / 2 - 1.6 * s
    msp.add_line((cx - tw / 2, uy), (cx + tw / 2, uy), dxfattribs={"layer": LY["title_rule"]}).set_xdata(APPID, [(1000, "title")])
    # 水平、竖向比例不同（高程图）：标题下方注明两个比例，标题栏比例栏写「见图」（GB/T 14690）
    note = scale_note(d)
    if note:
        nh = text_h(d, "scale_note", note)
        _text(msp, note, (cx, uy - 1.2 * s), nh, LY["title"], TA.TOP_CENTER).set_xdata(APPID, [(1000, "title")])
    if not d.notes:
        return
    fx0, fy0, fx1, _ = inner_frame(d)
    left = fx0 + 6 * s
    right = fx1 - WATER_TITLE_W * s - 6 * s
    top = fy0 + WATER_TITLE_H * s - 4 * s
    nh = text_h(d, "notes", "说明")
    pitch = for_drawing(d).line_pitch(nh)  # 基准线间距 b2 = 15/10 h（GB/T 14691 表 2，B 型、无发音符号）
    xd = [(1000, "notes")]
    _wtext(msp, "说明：", (left, top), nh, LY["notes"], TA.TOP_LEFT).set_xdata(APPID, xd)
    y = top - pitch
    indent = text_width("1. ", nh)
    for i, note in enumerate(d.notes, 1):
        note = note.strip()
        head = "" if note[:1].isdigit() else f"{i}. "
        lines = _wrap(head + note, right - left - indent, nh)
        for k, ln in enumerate(lines):
            if y < fy0 + 2 * s:
                return  # 说明区写满了，剩下的不硬塞
            _wtext(msp, ln, (left + (indent if k else 0), y), nh, LY["notes"], TA.TOP_LEFT).set_xdata(APPID, xd)
            y -= pitch


def _wrap(text: str, width: float, h: float) -> list[str]:
    out, cur = [], ""
    for ch in text:
        if cur and text_width(cur + ch, h) > width:
            out.append(cur)
            cur = ""
        cur += ch
    return out + ([cur] if cur else [])


def draw_legend(msp, d: Drawing) -> None:
    from ezdxf.enums import TextEntityAlignment as TA

    from .dxf_io import APPID, _text

    items = d.legend or legend_items(d)
    box = legend_box(d, items)
    if not box:
        return
    u = d.unit
    x0, _, x1, y1 = box
    xd = [(1000, "legend")]
    _bold_text(msp, "图    例", ((x0 + x1) / 2, y1 - LEGEND_HEAD / 2 * u), text_h(d, "legend_title", "图例"), LY["legend"], TA.MIDDLE_CENTER, xd)
    for k, it in enumerate(items):
        y = y1 - (LEGEND_HEAD + (k + 0.5) * LEGEND_ROW) * u
        layer = code_layer(it["code"])
        ch = text_h(d, "pipe_code", it["code"])
        tw = text_width(it["code"], ch)
        mid = x0 + 13 * u
        msp.add_line((x0 + 2 * u, y), (mid - tw / 2 - 1 * u, y), dxfattribs={"layer": layer}).set_xdata(APPID, xd)
        msp.add_line((mid + tw / 2 + 1 * u, y), (x0 + 24 * u, y), dxfattribs={"layer": layer}).set_xdata(APPID, xd)
        _text(msp, it["code"], (mid, y), ch, LY["legend"], TA.MIDDLE_CENTER).set_xdata(APPID, xd)
        _text(msp, it["name"], (x0 + 28 * u, y), text_h(d, "legend_name", it["name"]), LY["legend"], TA.MIDDLE_LEFT).set_xdata(APPID, xd)


def draw_names(msp, d: Drawing) -> None:
    """构筑物正下方注中文名称（加大字高 + 下划线）；药剂罐把药剂名竖排写在罐里。"""
    from ezdxf.enums import TextEntityAlignment as TA

    from .dxf_io import APPID, _text

    u = d.unit
    for e in d.equipment:
        if not e.placed or not e.name:
            continue
        sym = get_symbol(e.symbol_id)
        xd = [(1000, "name_label"), (1000, e.id)]
        if sym.name_style in WATER_NAME_STYLES:
            lb = label_box(e, u, d.template_id)
            h = text_h(d, "struct_name" if sym.name_style == "below" else "name", e.name)
            cx = (lb[0] + lb[2]) / 2
            if sym.name_style == "below":
                _bold_text(msp, e.name, (cx, lb[3]), h, LY["equip_text"], TA.TOP_CENTER, xd)
                w = text_width(e.name, h)
                ly = lb[3] - h - 0.9 * d.scale
                msp.add_line((cx - w / 2, ly), (cx + w / 2, ly), dxfattribs={"layer": LY["equip_text"]}).set_xdata(APPID, xd)
            else:
                _text(msp, e.name, (cx, lb[3]), h, LY["equip_text"], TA.TOP_CENTER).set_xdata(APPID, xd)
        elif sym.name_style == "inside_vertical":
            x0, y0, x1, y1 = symbol_box(e, u)
            h = text_h(d, "name", e.name)
            pitch = h * 1.25
            n = len(e.name)
            cy = (y0 + y1) / 2 - 1.5 * u
            for k, ch in enumerate(e.name):
                yk = cy + (n - 1) / 2 * pitch - k * pitch
                _text(msp, ch, ((x0 + x1) / 2, yk), h, LY["equip_text"], TA.MIDDLE_CENTER).set_xdata(APPID, xd)


def draw_elevation(msp, d: Drawing, an: Annotation) -> None:
    """▽ + 数值 + 短水平线。数值文字带 XDATA["anno", id, anchor, "elevation", x, y]，读回时还原标注锚点。"""
    from ezdxf.enums import TextEntityAlignment as TA

    from .dxf_io import APPID, _text

    u = d.unit
    side, below = mark_flags(an.id)
    x, y = an.x, an.y
    xd = [(1000, "elev"), (1000, an.id)]
    lo, hi = sorted((x, x + side * ELEV_W * u))
    pipe = _mark_pipe(d, an)
    if pipe is not None:  # 标在本构筑物的进出水管上，与管重合的那段引线不画
        y = pipe[0]
    spans = [(lo, hi)]
    if pipe is not None:
        spans = [piece for s0, s1 in spans for piece in ((s0, min(s1, pipe[1])), (max(s0, pipe[2]), s1)) if piece[1] - piece[0] > 1e-6]
    if "_ground" in an.id:  # 地面标高画在地面线上：已经有地面线的那一段不再重复画引线（图线重合只画一条）
        for (gx0, gy0), (gx1, gy1) in ground_lines(d):
            if abs(gy0 - y) < 1e-6 and abs(gy1 - y) < 1e-6:
                a, b = sorted((gx0, gx1))
                spans = [piece for s0, s1 in spans for piece in ((s0, min(s1, a)), (max(s0, b), s1)) if piece[1] - piece[0] > 1e-6]
    for s0, s1 in spans:
        msp.add_line((s0, y), (s1, y), dxfattribs={"layer": LY["elev"]}).set_xdata(APPID, xd)
    # 标高符号块（三角形 + 数值属性），尖端落在标高线上；XDATA 存真实标高位置（不是吸附到管上的位置），读回时还原标注
    from . import anno

    aid = "elevation_" + ("r" if side > 0 else "l") + ("_below" if below else "")
    anno.insert(msp, doc_of(msp), d, aid, (x + side * ELEV_APEX * u, y), LY["elev"], {"VALUE": an.text},
                xdata=[(1000, "anno"), (1000, an.id), (1000, an.anchor_ref), (1000, "elevation"), (1000, f"{an.x:.3f}"), (1000, f"{an.y:.3f}")])


def retaining_walls(d: Drawing) -> list[tuple[list[tuple[float, float]], tuple[float, float], int]]:
    """地面台阶处的挡土墙（参考图：沉淀池地面 288.40 跌到滤池地面 286.00 处）：竖直墙面朝低处，墙背斜向高处。

    返回 [(墙身多边形, 「挡土墙」字的位置, 墙背方向 -1 左 / 1 右)]。
    """
    u = d.unit
    out = []
    for (x, ya), (_, yb) in ground_lines(d, steps_only=True):
        hi, lo = max(ya, yb), min(ya, yb)
        back = -1 if ya > yb else 1  # 高处在左（地面向右跌落）时墙背朝左
        top, bot = hi + 1.0 * u, lo - 1.5 * u
        poly = [(x, bot), (x, top), (x + back * 2.0 * u, top), (x + back * 4.5 * u, bot)]
        out.append((poly, (x + back * 2.5 * u, top + 0.6 * u), back))
    return out


def draw_ground(msp, d: Drawing) -> None:
    from .dxf_io import APPID

    from ezdxf.enums import TextEntityAlignment as TA

    from .dxf_io import _text
    from .hatch import base_spacing

    u = d.unit
    xd = [(1000, "ground")]
    earth = EARTH_SPACING * u / base_spacing("EARTH")
    for (x0, y0), (x1, y1) in ground_lines(d):
        if abs(x0 - x1) < 1e-9:  # 台阶处的竖线就是挡土墙的墙面，由挡土墙画，不重复画（图线重合只画一条）
            continue
        msp.add_line((x0, y0), (x1, y1), dxfattribs={"layer": LY["ground"]}).set_xdata(APPID, xd)
        if abs(y0 - y1) < 1e-9:  # 水平段下方一条素土剖面线（EARTH）
            a, b = min(x0, x1), max(x0, x1)
            h = msp.add_hatch(color=256, dxfattribs={"layer": LY["ground"]})
            h.set_pattern_fill("EARTH", scale=earth, angle=45, color=256)
            h.paths.add_polyline_path([(a, y0), (b, y0), (b, y0 - GROUND_STRIP * u), (a, y0 - GROUND_STRIP * u)], is_closed=True)
            h.set_xdata(APPID, xd)
    for poly, (lx, ly), back in retaining_walls(d):
        msp.add_lwpolyline(poly, close=True, dxfattribs={"layer": LY["ground"]}).set_xdata(APPID, xd)
        for pat, sp in (("ANSI31", 1.2), ("AR-CONC", 4.0)):
            h = msp.add_hatch(color=256, dxfattribs={"layer": LY["ground"]})
            h.set_pattern_fill(pat, scale=sp * u / base_spacing(pat), color=256)
            h.paths.add_polyline_path(poly, is_closed=True, flags=1)
            h.set_xdata(APPID, xd)
        t = _text(msp, "挡土墙", (lx, ly), text_h(d, "annotation", "挡土墙"), LY["ground"], TA.BOTTOM_RIGHT if back < 0 else TA.BOTTOM_LEFT)
        t.set_xdata(APPID, [(1000, "name_label"), (1000, "ground")])


def jump_points(d: Drawing) -> dict[str, list[tuple[float, float]]]:
    """管线交叉处由哪条线跨过去（画跨线符号）：主流程 YS/QS 不断，另一条跨；同级时竖线跨横线。返回 {cid: [交叉点]}。"""
    from .routing import EPS

    segs = []
    for c in d.connections:
        for a, b in zip(c.path or [], (c.path or [])[1:]):
            if abs(a[1] - b[1]) < EPS and abs(a[0] - b[0]) > EPS:
                segs.append((c, "h", a[1], min(a[0], b[0]), max(a[0], b[0])))
            elif abs(a[0] - b[0]) < EPS and abs(a[1] - b[1]) > EPS:
                segs.append((c, "v", a[0], min(a[1], b[1]), max(a[1], b[1])))
    out: dict[str, list[tuple[float, float]]] = {}
    for ch, _, y, x0, x1 in [t for t in segs if t[1] == "h"]:
        for cv, _, x, y0, y1 in [t for t in segs if t[1] == "v"]:
            if ch.id == cv.id or not (x0 + EPS < x < x1 - EPS and y0 + EPS < y < y1 - EPS):
                continue
            jumper = ch if (cv.code in MAIN_CODES and ch.code not in MAIN_CODES) else cv
            out.setdefault(jumper.id, []).append((x, y))
    return out


def break_spans(d: Drawing) -> dict[str, dict[int, tuple[float, float]]]:
    """折断（书 3.5.7 节破断视图）：直线段长超过可用高度（横段为可用宽度）的 30%、且中间没有其他对象时，
    中间断开、两端各留 15 mm，断口画双折线。返回 {cid: {段序号: (断开起点, 断开终点)}}（沿段的距离，模型单位）。"""
    if not d.breaks:  # 固定图幅上折断只是把线去掉、并不腾出地方，默认不开；用户要求或加长幅面时打开
        return {}
    std = for_drawing(d)
    br = std.raw["break"]
    keep = br["keep_mm"] * d.scale
    u = d.unit
    ux0, uy0, ux1, uy1 = usable_area(d)
    obstacles = [inflate(footprint(e, u, d.template_id), 1.0 * u) for e in d.equipment if e.placed]
    leg = legend_box(d) if d.template_id == WATER else None
    if leg:
        obstacles.append(leg)
    obstacles += [elev_mark_box(d, an.x, an.y, *mark_flags(an.id)) for an in d.annotations if an.style == "elevation" and an.x is not None]
    obstacles += [(an.x - 2 * u, an.y - 2 * u, an.x + 2 * u, an.y + 2 * u) for an in d.annotations if an.style != "elevation" and an.x is not None]
    allsegs = [(c.id, a, b) for c in d.connections for a, b in zip(c.path or [], (c.path or [])[1:])]
    out: dict[str, dict[int, tuple[float, float]]] = {}
    for c in d.connections:
        for k, (a, b) in enumerate(zip(c.path or [], (c.path or [])[1:])):
            L = math.dist(a, b)
            vertical = abs(a[0] - b[0]) < 1e-9
            limit = br["min_ratio"] * ((uy1 - uy0) if vertical else (ux1 - ux0))
            if L <= limit or L <= 2 * keep + 10 * u:
                continue
            ux, uy = (b[0] - a[0]) / L, (b[1] - a[1]) / L
            m0 = (a[0] + ux * keep, a[1] + uy * keep)
            m1 = (b[0] - ux * keep, b[1] - uy * keep)
            w = 3.0 * u  # 中间段两侧 3 mm 内不能有别的对象
            corridor = (min(m0[0], m1[0]) - (w if vertical else 0), min(m0[1], m1[1]) - (0 if vertical else w),
                        max(m0[0], m1[0]) + (w if vertical else 0), max(m0[1], m1[1]) + (0 if vertical else w))
            if any(boxes_overlap(corridor, o) for o in obstacles):
                continue
            hit = False
            for cid, p, q in allsegs:  # 别的管线穿过或贴着中间段
                if cid == c.id:
                    continue
                box = (min(p[0], q[0]), min(p[1], q[1]), max(p[0], q[0]), max(p[1], q[1]))
                if box[0] <= corridor[2] and box[2] >= corridor[0] and box[1] <= corridor[3] and box[3] >= corridor[1]:
                    hit = True
                    break
            if not hit:
                out.setdefault(c.id, {})[k] = (keep, L - keep)
    return out


def break_marks(d: Drawing, c, breaks: dict | None = None) -> list[tuple[tuple[float, float], float]]:
    """断口位置和方向：[(点, 管段角度)]，每处折断两个。"""
    spans = (break_spans(d) if breaks is None else breaks).get(c.id, {})
    out = []
    for k, (t0, t1) in spans.items():
        a, b = c.path[k], c.path[k + 1]
        L = math.dist(a, b)
        ux, uy = (b[0] - a[0]) / L, (b[1] - a[1]) / L
        ang = math.degrees(math.atan2(uy, ux))
        out += [((a[0] + ux * t0, a[1] + uy * t0), ang), ((a[0] + ux * t1, a[1] + uy * t1), ang)]
    return out


def break_boxes(d: Drawing, c, breaks: dict | None = None) -> list[Box]:
    """折断掉的那一段（管线上的文字、半箭头都要让开）。"""
    pts = break_marks(d, c, breaks)
    u = d.unit
    return [(min(p[0], q[0]) - 3 * u, min(p[1], q[1]) - 3 * u, max(p[0], q[0]) + 3 * u, max(p[1], q[1]) + 3 * u)
            for (p, _), (q, _) in zip(pts[0::2], pts[1::2])]


def pipe_pieces(d: Drawing, c, jumps: dict | None = None, breaks: dict | None = None, r: float | None = None, code_gap: bool = True) -> tuple[list[list[tuple[float, float]]], list[tuple[tuple[float, float], bool]], list[tuple[tuple[float, float], bool]]]:
    """把管线在代号文字处、跨线处和折断处断开。返回 (分段折线, [(代号中心, 是否竖直)], [(跨线中心, 是否竖直)])。"""
    u = d.unit
    path = c.path
    segs = list(zip(path, path[1:]))
    if not segs:
        return [path], [], []
    lens = [math.dist(a, b) for a, b in segs]
    marks: list[int] = []
    gap = 0.0
    if c.code and code_gap:  # 高程图：代号写在管线断口里；流程图不写，不断开
        gap = (text_width(c.code, CODE_TEXT) + 3.0) * u
        marks = [k for k, L in enumerate(lens) if L >= 30 * u]
        if not marks:
            k = max(range(len(segs)), key=lambda k: lens[k])
            marks = [k] if lens[k] >= max(gap + 6 * u, 24 * u) else []  # 太短的管段不写代号，免得挤成一团
    pts = (jump_points(d) if jumps is None else jumps).get(c.id, [])
    spans = (break_spans(d) if breaks is None else breaks).get(c.id, {})
    edge_ok = r is not None  # 化工流程图的断口可以一直延到拐角（跨线半圆不行，所以高程图仍要求离两端至少一个半径）
    r = JUMP_R * u if r is None else r  # 化工流程图交叉处断开（chem.break_points）时 r 是断口的半宽
    pieces: list[list[tuple[float, float]]] = []
    labels, hops = [], []
    cur = [path[0]]
    for k, (a, b) in enumerate(segs):
        L = lens[k]
        ux, uy = (b[0] - a[0]) / L, (b[1] - a[1]) / L
        cuts = []  # (沿线距离, 半宽, 类别, 中心)
        if k in spans:  # 折断：中间整段不画；代号挪到开头留下的那一小段上
            t0, t1 = spans[k]
            cuts.append(((t0 + t1) / 2, (t1 - t0) / 2, "break", None))
            if k in marks and t0 >= gap + 2 * u:
                tc = t0 / 2
                cuts.append((tc, gap / 2, "code", (a[0] + ux * tc, a[1] + uy * tc)))
        elif k in marks:
            cuts.append((L / 2, gap / 2, "code", ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)))
        for p in pts:
            t = (p[0] - a[0]) * ux + (p[1] - a[1]) * uy
            on_line = abs((p[0] - a[0]) * uy - (p[1] - a[1]) * ux) < 1e-6
            inside = (r < t < L - r) or (edge_ok and 1e-6 < t < L - 1e-6)
            if on_line and inside and not any(abs(t - tt) < h0 + r for tt, h0, _, _ in cuts):
                cuts.append((t, r, "jump", p))
        for t, h, kind, m in sorted(cuts):
            t0, t1 = max(t - h, 0.0), min(t + h, L)
            cur.append((a[0] + ux * t0, a[1] + uy * t0))
            pieces.append(cur)
            cur = [(a[0] + ux * t1, a[1] + uy * t1)]
            if kind == "code":
                labels.append((m, abs(ux) < 1e-9))
            elif kind == "jump":
                hops.append((m, abs(ux) < 1e-9))
        cur.append(b)
    pieces.append(cur)
    if edge_ok:  # 断口到拐角时会留下重复点、零长度段，去掉
        clean = []
        for pc in pieces:
            q = [pt for i, pt in enumerate(pc) if i == 0 or math.dist(pt, pc[i - 1]) > 1e-6]
            if len(q) >= 2:
                clean.append(q)
        pieces = clean
    return pieces, labels, hops


def pipe_texts(d: Drawing, c, avoid_marks: bool = False) -> list[tuple[str, tuple[float, float], float, bool, int]]:
    """管线上的文字：代号写在断口处（side=0），介质名 + 规格（如「进水管DN200」）写在最长段旁（side=±1 为法向哪一侧）。

    side=2 是引出线标注：没有一段直管放得下文字时，文字放在附近空白处，用细实线引出线连到管线（GB/T 4457.2 引出线）。
    返回 [(文字, 位置, 字高, 是否竖排, side)]；画图和高程标注避让都用它，两边算出来的位置一致。
    """
    u = d.unit
    out = [(c.code, pos, text_h(d, "pipe_code", c.code), vertical, 0) for pos, vertical in pipe_pieces(d, c, {})[1]]
    label = f"{c.medium}{c.spec}"
    segs = list(zip(c.path or [], (c.path or [])[1:]))
    if label and segs:
        lens = [math.dist(a, b) for a, b in segs]
        lh = text_h(d, "pipe_label", label)
        tw = text_width(label, lh)
        avoid = [footprint(e, u, d.template_id) for e in d.equipment if e.placed]
        avoid += [pipe_text_box(*t) for t in out] + break_boxes(d, c)
        if avoid_marks:  # 出图时再让开已经放好的高程标注（标注定位时文字还在默认位置，两边错开一轮）
            avoid += [elev_mark_box(d, an.x, mark_draw_y(d, an), *mark_flags(an.id)) for an in d.annotations
                      if an.style == "elevation" and an.x is not None]
        lines = [(p, q) for cc in d.connections for p, q in zip(cc.path or [], (cc.path or [])[1:])] + ground_lines(d)

        from .symbols import label_area_ok

        def free(box) -> bool:
            if not label_area_ok(d, box):  # 不出图框、不进标题区、不压标题栏
                return False
            return not any(boxes_overlap(box, o) for o in avoid) and not any(_seg_in_box(p, q, box) for p, q in lines)

        cands = []
        for k in sorted(range(len(segs)), key=lambda k: (-lens[k], k)):  # 先最长段，放不下再换段
            (a, b), L = segs[k], lens[k]
            if L < tw + 2 * u:  # 文字不能超出这段直管
                continue
            ux, uy = (b[0] - a[0]) / L, (b[1] - a[1]) / L
            vertical = abs(ux) < 1e-9
            pref = (0.72, 0.5, 0.3) if c.code else (0.5, 0.3, 0.7)
            ts = list(pref) + sorted((i / 20 for i in range(1, 20)), key=lambda t: abs(t - pref[0]))
            for sgn in (1, -1):  # 先放流向右侧（半箭头在左侧）
                nx, ny = sgn * uy, -sgn * ux
                side = (1 if nx > 0 else -1) if vertical else (1 if ny > 0 else -1)
                for t in ts:
                    if min(t, 1 - t) * L < tw / 2 + u:
                        continue
                    pos = (a[0] + ux * L * t + nx * 1.8 * u, a[1] + uy * L * t + ny * 1.8 * u)
                    cands.append((label, pos, lh, vertical, side))
        pick = next((t for t in cands if free(pipe_text_box(*t))), None)
        if pick is None:  # 引出线：从最长段中点引出，文字横写在附近空白处（近的先试，上方优先）
            anchor = leader_anchor(c)
            for dy, dx in leader_spots(tw / u):
                t = (label, (anchor[0] + dx * u, anchor[1] + dy * u), lh, False, 2)
                lead = leader_points(t, anchor)
                own = (t[1][0], t[1][1] + 0.3 * u, t[1][0] + tw, t[1][1] + 0.6 * u + lh)  # 引出线不穿过自己的文字
                if free(pipe_text_box(*t)) and not any(_seg_in_box(lead[0], lead[1], o) for o in avoid + [own]) and leader_clear(*lead, lines, 0.5 * u):
                    pick = t
                    break
        if pick is None:  # 实在找不到空白：仍按引出线写在上方，宁可略挤也不丢管道规格
            anchor = leader_anchor(c)
            pick = cands[0] if cands else (label, (anchor[0] - tw / 2, anchor[1] + 6 * u), lh, False, 2)
        if pick is not None:
            out.append(pick)
    return out


# 引出线标注的候选位置（图纸 mm）：文字下划线离管线的竖向距离、文字左端相对引出点的横向偏移。近的先试；
# 竖向要够远——相邻设备之间放不下文字时，得引到设备排的上方或下方（「余热回收后烟气」在锅炉和急冷塔之间就要往下 19 mm）
LEADER_DY = (4, 6, 8, 11, 14, 18, 23, 29, 36, 45, 55, -5, -7, -9, -12, -15, -19, -24, -30, -37, -45)


def leader_spots(tw_mm: float) -> list[tuple[float, float]]:
    """[(dy, dx)]，按「上方优先、离引出点近」排序。"""
    spots = [(dy, dx) for dy in LEADER_DY for dx in range(-int(tw_mm) - 16, 17, 2)]
    return sorted(spots, key=lambda q: (abs(q[0]) + (0 if q[0] > 0 else 2), abs(q[1] + tw_mm / 2)))


def leader_clear(anchor, end, lines, gap: float) -> bool:
    """引出线除了起点，不碰任何管线（不交叉、不沿管线走）。gap：起点附近让开的长度。"""
    L = math.dist(anchor, end)
    if L <= gap:
        return True
    ux, uy = (end[0] - anchor[0]) / L, (end[1] - anchor[1]) / L
    a = (anchor[0] + ux * gap, anchor[1] + uy * gap)
    pad = 1e-6
    box = (min(a[0], end[0]) - pad, min(a[1], end[1]) - pad, max(a[0], end[0]) + pad, max(a[1], end[1]) + pad)
    for p, q in lines:
        if max(p[0], q[0]) < box[0] or min(p[0], q[0]) > box[2] or max(p[1], q[1]) < box[1] or min(p[1], q[1]) > box[3]:
            continue
        d1 = (q[0] - p[0]) * (a[1] - p[1]) - (q[1] - p[1]) * (a[0] - p[0])
        d2 = (q[0] - p[0]) * (end[1] - p[1]) - (q[1] - p[1]) * (end[0] - p[0])
        d3 = (end[0] - a[0]) * (p[1] - a[1]) - (end[1] - a[1]) * (p[0] - a[0])
        d4 = (end[0] - a[0]) * (q[1] - a[1]) - (end[1] - a[1]) * (q[0] - a[0])
        if (d1 * d2 <= 0) and (d3 * d4 <= 0):  # 相交或共线重叠
            return False
    return True


def _seg_in_box(a, b, box) -> bool:
    """线段穿进矩形内部（贴边不算）。"""
    from .standards_check import _seg_hits_box

    return _seg_hits_box(a, b, box)


def leader_points(t, anchor) -> tuple[tuple[float, float], tuple[float, float]]:
    """引出线标注的引出线：从管线上的点到文字下划线靠近管线的一端。"""
    text, (x, y), h, _, _ = t
    w = text_width(text, 1.0) * h
    if x <= anchor[0] <= x + w and y > anchor[1]:  # 文字在管线上方、正对着：竖直引出到下划线（在下方时竖线会穿过文字）
        return anchor, (anchor[0], y)
    end = (x, y) if abs(x - anchor[0]) <= abs(x + w - anchor[0]) else (x + w, y)
    return anchor, end


def leader_anchor(c) -> tuple[float, float]:
    segs = list(zip(c.path, c.path[1:]))
    a, b = max(segs, key=lambda ab: math.dist(*ab))
    return (a[0] + b[0]) / 2, (a[1] + b[1]) / 2


def pipe_text_box(text: str, pos: tuple[float, float], h: float, vertical: bool, side: int) -> Box:
    w = text_width(text, 1.0) * h
    x, y = pos
    if side == 2:  # 引出线标注：文字左下角在 pos，下面是下划线
        return (x, y - 0.6 * h, x + w, y + h)
    if not vertical:
        if side == 0:
            return (x - w / 2, y - h / 2, x + w / 2, y + h / 2)
        return (x - w / 2, y, x + w / 2, y + h) if side > 0 else (x - w / 2, y - h, x + w / 2, y)
    if side == 0:
        return (x - h / 2, y - w / 2, x + h / 2, y + w / 2)
    return (x, y - w / 2, x + h, y + w / 2) if side > 0 else (x - h, y - w / 2, x, y + w / 2)


def draw_pipe(msp, d: Drawing, c, layer: str, xd_conn: list, jumps: dict | None = None, breaks: dict | None = None) -> None:
    """管线：在代号处断开；第一段带完整语义 XDATA，其余段带 ["conn_part", cid, 序号]，read_dxf 拼回整条。"""
    from ezdxf.enums import TextEntityAlignment as TA

    from .dxf_io import APPID, _text

    u = d.unit
    from . import anno

    pieces, _, hops = pipe_pieces(d, c, jumps, breaks)
    for k, pts in enumerate(pieces):
        pl = msp.add_lwpolyline(pts, dxfattribs={"layer": layer})
        pl.set_xdata(APPID, xd_conn if k == 0 else [(1000, "conn_part"), (1000, c.id), (1000, str(k))])
    for (hx, hy), vertical in hops:  # 跨线符号：半圆弧，竖线向右拱、横线向上拱
        a0, a1 = (270, 90) if vertical else (0, 180)
        msp.add_arc((hx, hy), JUMP_R * u, a0, a1, dxfattribs={"layer": layer}).set_xdata(APPID, [(1000, "jump"), (1000, c.id)])
    for text, pos, h, vertical, side in pipe_texts(d, c, avoid_marks=True):
        if side == 2:  # 引出线标注：细实线引出线 + 下划线，文字写在下划线上
            w = text_width(text, 1.0) * h
            xd = [(1000, "conn_label"), (1000, c.id)]
            _text(msp, text, (pos[0], pos[1] + 0.6 * u), h, LY["pipe_text"], TA.BOTTOM_LEFT).set_xdata(APPID, xd)
            msp.add_line(pos, (pos[0] + w, pos[1]), dxfattribs={"layer": LY["anno"]}).set_xdata(APPID, xd)
            a, b = leader_points((text, pos, h, False, 2), leader_anchor(c))
            msp.add_line(a, b, dxfattribs={"layer": LY["anno"]}).set_xdata(APPID, xd)
            continue
        if side == 0:
            al = TA.MIDDLE_CENTER
        elif vertical:  # 旋转 90° 后文字的「上方」朝左
            al = TA.TOP_CENTER if side > 0 else TA.BOTTOM_CENTER
        else:
            al = TA.BOTTOM_CENTER if side > 0 else TA.TOP_CENTER
        t = _text(msp, text, pos, h, LY["pipe_text"], al)
        if vertical:
            t.dxf.rotation = 90
        t.set_xdata(APPID, [(1000, "conn_label"), (1000, c.id)])
    marks = [elev_mark_box(d, an.x, mark_draw_y(d, an), *mark_flags(an.id)) for an in d.annotations if an.style == "elevation" and an.x is not None]
    marks += break_boxes(d, c, breaks)
    for pt, ang in break_marks(d, c, breaks):  # 断口画双折线（GB/T 4457.4）
        anno.insert(msp, doc_of(msp), d, "break", pt, layer, rotation=ang, xdata=[(1000, "break"), (1000, c.id)])
    for p0, p1, barb in flow_arrows(d, c, avoid=marks):  # 流向半箭头块，沿流向旋转，倒钩朝管线外侧
        ang = math.degrees(math.atan2(p1[1] - p0[1], p1[0] - p0[0]))
        cross = (p1[0] - p0[0]) * (barb[1] - p1[1]) - (p1[1] - p0[1]) * (barb[0] - p1[0])
        anno.insert(msp, doc_of(msp), d, "flow", ((p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2), LY["flow_arrow"],
                    rotation=ang, mirror_y=cross < 0, xdata=[(1000, "arrow"), (1000, c.id)])


def flow_arrows(d: Drawing, c, avoid: list | None = None) -> list[tuple[tuple[float, float], tuple[float, float], tuple[float, float]]]:
    """流向：线旁平行的半箭头 (杆起点, 杆终点=尖, 倒钩端)，画在最长段上（代号文字之前），长段再各补一个。

    avoid：要让开的范围（如高程标注）；在这一段上前后挪动，找一处离它们都在 0.7 mm 以上的位置。
    """
    u = d.unit
    segs = list(zip(c.path or [], (c.path or [])[1:]))
    if not segs:
        return []
    lens = [math.dist(a, b) for a, b in segs]
    longest = max(range(len(segs)), key=lambda k: lens[k])
    picks = sorted({longest, *[k for k, L in enumerate(lens) if L >= 60 * u]})
    out = []
    for k in picks:
        (a, b), L = segs[k], lens[k]
        if L < 14 * u:
            continue
        ux, uy = (b[0] - a[0]) / L, (b[1] - a[1]) / L
        half = 4.0 * u  # 与流向符号块的杆长一致（8 mm）
        cand = []
        sides = ((-uy, ux), (uy, -ux))  # 先放流向左侧，左侧挤不下再放右侧
        for (nx, ny), t in ((n, t) for n in sides for t in ((0.25, 0.15, 0.4, 0.75, 0.85) if c.code else (0.5, 0.3, 0.7, 0.15, 0.85))):
            cx, cy = a[0] + ux * L * t + nx * 2.2 * u, a[1] + uy * L * t + ny * 2.2 * u
            p0, p1 = (cx - ux * half, cy - uy * half), (cx + ux * half, cy + uy * half)
            barb = (p1[0] - ux * 2.2 * u + nx * 1.0 * u, p1[1] - uy * 2.2 * u + ny * 1.0 * u)
            cand.append((p0, p1, barb))
        m = (MIN_GAP_MM + 0.2) * u

        def clear(arrow) -> bool:
            xs, ys = [q[0] for q in arrow], [q[1] for q in arrow]
            box = (min(xs) - m, min(ys) - m, max(xs) + m, max(ys) + m)
            return not any(boxes_overlap(box, o) for o in avoid or [])

        out.append(next((a_ for a_ in cand if clear(a_)), cand[0]))
    return out


def draw_systems(msp, d: Drawing) -> None:
    """子系统标题（加粗、下划线）和虚线框。"""
    from ezdxf.enums import TextEntityAlignment as TA

    from .dxf_io import APPID

    u = d.unit
    for r in system_regions(d):
        xd = [(1000, "system"), (1000, r["id"])]
        cx, top = r["title_pos"]
        th = text_h(d, "system_title", r["name"])
        _bold_text(msp, r["name"], (cx, top), th, LY["system"], TA.TOP_CENTER, xd)
        w = text_width(r["name"], th)
        ly = top - th - 0.9 * d.scale
        msp.add_line((cx - w / 2, ly), (cx + w / 2, ly), dxfattribs={"layer": LY["title_rule"]}).set_xdata(APPID, xd)
        if r["box"]:
            x0, y0, x1, y1 = r["box"]
            from . import dashsplit

            dashsplit.draw(msp, d, [(x0, y0), (x1, y0), (x1, y1), (x0, y1)], LY["system_box"], closed=True, xdata=xd)  # 四个角交在画线上
