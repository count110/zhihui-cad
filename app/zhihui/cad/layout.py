"""auto_layout：由设备和连接拓扑确定性地计算坐标。

full 模式（生成）：
  1. 按介质流向拓扑排序，把设备分入从左到右的若干列；有回流时先断开回边。
  2. 同列设备上下排列，按连接重心调整顺序，减少交叉。
  3. 设备底部落在同一条地面基准线上；列太多时折行；整体居中到图框可用区域。
  4. 调用正交布线。
incremental 模式（改图）：已有设备一律不动，只给新设备找空位，只为相关连接布线。
同一输入（设备集合 + 连接，与调用顺序无关）永远得到同一张图。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from zhihui.ir import PAPER_SIZES, Drawing, Equipment

from .routing import STUB, collect_segments, route_connections
from .symbols import WATER, Box, boxes_overlap, footprint, footprint_rel, get_symbol, inflate, usable_area

# (列间距, 行间距)，图纸 mm
SPACING = {"compact": (16.0, 10.0), "normal": (26.0, 16.0), "loose": (38.0, 24.0)}


class LayoutError(Exception):
    def __init__(self, code: str, message: str, hint: str, detail: dict | None = None):
        super().__init__(message)
        self.code, self.message, self.hint, self.detail = code, message, hint, detail or {}


@dataclass
class LayoutResult:
    mode: str
    positions: dict[str, tuple[float, float]] = field(default_factory=dict)
    routed: list[str] = field(default_factory=list)
    failed_routes: list[str] = field(default_factory=list)
    columns: int = 0
    rows: int = 1


def _break_cycles(order: list[str], succ: dict[str, list[str]]) -> set[tuple[str, str]]:
    """DFS 找回边（回流），按位号顺序遍历保证确定性。"""
    state: dict[str, int] = {}
    back: set[tuple[str, str]] = set()

    def dfs(u: str) -> None:
        state[u] = 1
        for v in succ[u]:
            if state.get(v) == 1:
                back.add((u, v))
            elif v not in state:
                dfs(v)
        state[u] = 2

    for u in order:
        if u not in state:
            dfs(u)
    return back


def _layers(d: Drawing, eqs: list[Equipment]) -> list[list[str]]:
    ids = [e.id for e in eqs]
    idset = set(ids)
    edges = sorted(
        {(c.from_equip, c.to_equip) for c in d.connections if c.from_equip in idset and c.to_equip in idset and c.from_equip != c.to_equip}
    )
    succ: dict[str, list[str]] = {i: [] for i in ids}
    indeg = {i: 0 for i in ids}
    for u, v in edges:
        succ[u].append(v)
        indeg[v] += 1
    # 源点优先，其余按位号
    order = sorted(ids, key=lambda i: (indeg[i] > 0, ids.index(i)))
    back = _break_cycles(order, succ)
    dag = [(u, v) for u, v in edges if (u, v) not in back]
    pred: dict[str, list[str]] = {i: [] for i in ids}
    for u, v in dag:
        pred[v].append(u)

    layer: dict[str, int] = {}

    def depth(v: str) -> int:
        if v not in layer:
            layer[v] = 0  # 防御：理论上 DAG 不会递归回来
            layer[v] = max((depth(u) + 1 for u in pred[v]), default=0)
        return layer[v]

    for i in ids:
        depth(i)
    n = max(layer.values(), default=-1) + 1
    cols: list[list[str]] = [[] for _ in range(n)]
    for i in ids:
        cols[layer[i]].append(i)

    # 重心排序：上下各扫两遍
    pos = {i: k for col in cols for k, i in enumerate(col)}
    succ_dag: dict[str, list[str]] = {i: [] for i in ids}
    for u, v in dag:
        succ_dag[u].append(v)
    for sweep in range(4):
        rng = range(1, n) if sweep % 2 == 0 else range(n - 2, -1, -1)
        for li in rng:
            nb = pred if sweep % 2 == 0 else succ_dag

            def key(i: str) -> tuple:
                ns = nb[i]
                bc = sum(pos[u] for u in ns) / len(ns) if ns else pos[i]
                return (bc, pos[i])

            cols[li].sort(key=key)
            for k, i in enumerate(cols[li]):
                pos[i] = k
    return cols


def full_layout(d: Drawing, spacing: str = "normal") -> LayoutResult:
    s = d.unit
    col_gap, row_gap = (g * s for g in SPACING[spacing])
    eqs = sorted(d.equipment, key=lambda e: (e.tag, e.id))
    if not eqs:
        raise LayoutError("E_EMPTY", "图中还没有设备", "先调用 place_equipment 放置设备")
    cols = _layers(d, eqs)
    _pin_offsheets(d, cols)
    rel = {e.id: footprint_rel(e, s, d.template_id) for e in eqs}
    sym_bottom = {e.id: get_symbol(e.symbol_id).bbox[1] * s for e in eqs}

    ux0, uy0, ux1, uy1 = usable_area(d)
    widths = [max(rel[i][2] - rel[i][0] for i in col) for col in cols]

    # 流程过长时不再折成多排：先收紧列间距（不低于 compact），还放不下就按文章第 5 页的做法加长幅面，
    # 每次加长图样宽度的 1/4（A1 → A1+1/4 → A1+2/4 …），到上限还不够就报错，提示分张（空心箭头接续，标题栏写第 张 共 张）
    avail = ux1 - ux0
    row_width = lambda idx, gap: sum(widths[i] for i in idx) + gap * (len(idx) - 1)  # noqa: E731
    all_cols = list(range(len(cols)))
    min_gap = SPACING["compact"][0] * s
    if row_width(all_cols, col_gap) > avail and len(cols) > 1:
        squeezed = (avail - sum(widths)) / (len(cols) - 1)
        if squeezed >= min_gap:
            col_gap = squeezed
        else:
            ext = _extend_paper(d, row_width(all_cols, SPACING[spacing][0] * s))
            if ext is None:
                raise LayoutError(
                    "E_LAYOUT_TOO_LONG",
                    f"流程过长：一排需要 {row_width(all_cols, min_gap) / s:.0f} mm，{d.paper_size} 加长到上限也放不下",
                    "分张：把流程拆成两张图（create_drawing 时给 sheet_no / sheet_total），断开处用图纸接续标记（空心箭头）连接；或改用更大的基本幅面",
                    {"need_mm": [round(row_width(all_cols, min_gap) / s), 0]},
                )
            d.paper_size = ext
            ux0, uy0, ux1, uy1 = usable_area(d)
            avail = ux1 - ux0
            col_gap = SPACING[spacing][0] * s
            if row_width(all_cols, col_gap) > avail:
                col_gap = max(min_gap, (avail - sum(widths)) / (len(cols) - 1))
    bands: list[list[int]] = [all_cols]

    x: dict[str, float] = {}
    y: dict[str, float] = {}
    band_top = 0.0
    for band in bands:
        # x：逐列累加
        cursor = 0.0
        for ci in band:
            for i in cols[ci]:
                fx0, _, fx1, _ = rel[i]
                x[i] = cursor + widths[ci] / 2 - (fx0 + fx1) / 2
            cursor += widths[ci] + col_gap
        # y：所有设备底部落在同一条地面基准线上（设备高低关系与实际布置相吻合，
        # HG/T 20519.2 第 3.3.1 条）；同列多台设备自下而上叠放
        by: dict[str, float] = {}
        for ci in band:
            level = 0.0
            for n_, i in enumerate(reversed(cols[ci])):
                if n_ == 0:
                    by[i] = -sym_bottom[i]
                else:
                    by[i] = level - rel[i][1]
                level = by[i] + rel[i][3] + row_gap
        hi = max(by[i] + rel[i][3] for i in by)
        lo = min(by[i] + rel[i][1] for i in by)
        from .chem import elev_offset  # 安装高度（params.el，用户给）抬高，地下设备放到地平线以下（文章 2.1 节）

        for i, v in by.items():
            y[i] = v - hi + band_top + elev_offset(d, d.get_equipment(i))
        band_top -= (hi - lo) + row_gap * 2.5

    # 整体居中到可用区域
    bx0 = min(x[i] + rel[i][0] for i in x)
    bx1 = max(x[i] + rel[i][2] for i in x)
    by0 = min(y[i] + rel[i][1] for i in y)
    by1 = max(y[i] + rel[i][3] for i in y)
    need_w, need_h = bx1 - bx0, by1 - by0
    if need_w > ux1 - ux0 or need_h > uy1 - uy0:
        raise LayoutError(
            "E_LAYOUT_OVERFLOW",
            f"布局超出图幅：需要 {need_w / s:.0f}×{need_h / s:.0f} mm，{d.paper_size} 可用 {(ux1 - ux0) / s:.0f}×{(uy1 - uy0) / s:.0f} mm",
            "改用 spacing='compact'，或传 paper_size='A2' 换大图幅",
            {"need_mm": [round(need_w / s), round(need_h / s)]},
        )
    ox = (ux0 + ux1) / 2 - (bx0 + bx1) / 2
    oy = (uy0 + uy1) / 2 - (by0 + by1) / 2
    res = LayoutResult(mode="full", columns=len(cols), rows=len(bands))
    for e in eqs:
        e.x = round(x[e.id] + ox, 1)
        e.y = round(y[e.id] + oy, 1)
        res.positions[e.tag] = (e.x, e.y)
    res.failed_routes = route_connections(d)
    res.routed = [c.id for c in d.connections if c.path]
    if d.template_id != WATER:
        _center_vertically(d)
        res.positions = {e.tag: (e.x, e.y) for e in eqs}
    return res


def _pin_offsheets(d: Drawing, cols: list[list[str]]) -> None:
    """图纸接续的空心箭头画在图的左方或右方（文章 3.1 节）：只有流出连接的放最右一列，只有流入的放最左一列。"""
    from .chem import OFFSHEET
    from .symbols import TERMINALS

    if len(cols) < 2:
        return
    pinned = OFFSHEET + (TERMINALS if d.template_id != WATER else ())  # 管道来源 / 去向也画在图的左方、右方（文章 3.1 节）
    for e in d.equipment:
        if e.symbol_id not in pinned:
            continue
        outs = any(c.from_equip == e.id for c in d.connections)
        ins = any(c.to_equip == e.id for c in d.connections)
        target = 0 if (outs and not ins) else (len(cols) - 1 if (ins and not outs) else None)
        if e.symbol_id == "offsheet_left" and ins and not outs:
            target = 0  # 向左流出（文章图 3-4 的 V0607）
        if target is None:
            continue
        for col in cols:
            if e.id in col:
                col.remove(e.id)
        cols[target].append(e.id)


def _center_vertically(d: Drawing) -> None:
    """位号栏贴着设备带以后，把设备、管线连同位号栏整体在图框里上下居中（不改相对位置和拓扑）。"""
    from .symbols import inner_frame
    from .tagrow import tag_band

    s = d.unit
    boxes = [footprint(e, s, d.template_id) for e in d.equipment if e.placed]
    ys = [b[1] for b in boxes] + [b[3] for b in boxes] + [p[1] for c in d.connections for p in c.path or []]
    band = tag_band(d)
    if band:
        ys += [band[1], band[3]]
    if not ys:
        return
    lo, hi = min(ys), max(ys)
    bottom = usable_area(d)[1]
    top = inner_frame(d)[3] - 2 * s
    if hi - lo > top - bottom:
        return
    dy = round(((bottom + top) - (lo + hi)) / 2, 1)
    if abs(dy) < 0.05:
        return
    for e in d.equipment:
        if e.placed:
            e.y = round(e.y + dy, 1)
    for c in d.connections:
        if c.path:
            c.path = [(x, y + dy) for x, y in c.path]  # 不取整：端点要和端口坐标严格一致


def _fits(d: Drawing, e: Equipment, others: list[Box], area: Box, segs) -> bool:
    fp = footprint(e, d.unit, d.template_id)
    if fp[0] < area[0] or fp[1] < area[1] or fp[2] > area[2] or fp[3] > area[3]:
        return False
    if any(boxes_overlap(fp, o) for o in others):
        return False
    pad = inflate(fp, 2 * d.unit)
    for y, x0, x1 in segs.horizontal:
        if pad[1] < y < pad[3] and x0 < pad[2] and x1 > pad[0]:
            return False
    for x, y0, y1 in segs.vertical:
        if pad[0] < x < pad[2] and y0 < pad[3] and y1 > pad[1]:
            return False
    return True


def incremental_layout(d: Drawing, spacing: str = "normal") -> LayoutResult:
    s = d.unit
    col_gap, row_gap = (g * s for g in SPACING[spacing])
    area = usable_area(d)
    res = LayoutResult(mode="incremental")
    new = sorted([e for e in d.equipment if not e.placed], key=lambda e: (e.tag, e.id))
    new_ids = {e.id for e in new}
    keep_segs = collect_segments([c for c in d.connections if c.from_equip not in new_ids and c.to_equip not in new_ids])
    # 新设备与已有设备之间至少留出两侧管口引出段 + 净距，否则管线接不进去
    min_gap = max(row_gap / 4, (2 * STUB + 2) * s)
    for e in new:
        others = [inflate(footprint(o, s, d.template_id), min_gap) for o in d.equipment if o.placed]
        mine = [c.id for c in d.connections if e.id in (c.from_equip, c.to_equip)]

        def routable() -> bool:
            """试布这台设备的连接（只连到已布局的设备），布不通就换位置。"""
            probe = d.model_copy(deep=True)
            ids = {cid for cid in mine if all(probe.get_equipment(x).placed for x in (probe.get_connection(cid).from_equip, probe.get_connection(cid).to_equip))}
            return not route_connections(probe, ids) if ids else True
        ups = [d.get_equipment(c.from_equip) for c in d.connections if c.to_equip == e.id]
        downs = [d.get_equipment(c.to_equip) for c in d.connections if c.from_equip == e.id]
        ups = [u for u in ups if u and u.placed]
        downs = [v for v in downs if v and v.placed]
        rel = footprint_rel(e, s, d.template_id)
        if ups and downs:
            tx, ty = (ups[0].x + downs[0].x) / 2, (ups[0].y + downs[0].y) / 2
        elif ups:
            tx, ty = ups[0].x + footprint_rel(ups[0], s, d.template_id)[2] + col_gap - rel[0], ups[0].y
        elif downs:
            tx, ty = downs[0].x + footprint_rel(downs[0], s, d.template_id)[0] - col_gap - rel[2], downs[0].y
        else:
            tx, ty = area[0] - rel[0], area[3] - rel[3]
        step = 5 * s
        found = False
        for r in range(0, 80):
            ring = [(dx, dy) for dx in range(-r, r + 1) for dy in range(-r, r + 1) if max(abs(dx), abs(dy)) == r]
            ring.sort(key=lambda t: (abs(t[1]), abs(t[0]), t))  # 优先水平方向挪动
            for dx, dy in ring:
                e.x, e.y = round(tx + dx * step, 1), round(ty + dy * step, 1)
                if _fits(d, e, others, area, keep_segs) and routable():
                    found = True
                    break
            if found:
                break
        if not found:
            e.x = e.y = None
            raise LayoutError("E_NO_SPACE", f"图中没有空位放 {e.tag}", "改用 paper_size='A2'，或先移动其他设备腾出位置")
        res.positions[e.tag] = (e.x, e.y)
    targets = {c.id for c in d.connections if c.path is None or c.from_equip in new_ids or c.to_equip in new_ids}
    res.failed_routes = route_connections(d, targets)
    res.routed = sorted(t for t in targets if d.get_connection(t).path)
    return res


def auto_layout(d: Drawing, spacing: str = "normal", mode: str = "auto", paper_size: str | None = None) -> LayoutResult:
    if spacing not in SPACING:
        raise LayoutError("E_BAD_ARG", f"未知的 spacing: {spacing}", f"可选 {list(SPACING)}")
    if paper_size:
        if paper_size not in PAPER_SIZES:
            raise LayoutError("E_BAD_ARG", f"未知的图幅: {paper_size}", f"可选 {list(PAPER_SIZES)}")
        if paper_size != d.paper_size:
            d.paper_size = paper_size
            mode = "full"
    if mode == "auto":
        mode = "incremental" if any(e.placed for e in d.equipment) else "full"
    if mode == "full" and d.template_id == WATER:
        from .water import elevation_layout  # 给水处理高程布置图：按高程定竖向位置

        return elevation_layout(d, spacing)
    return full_layout(d, spacing) if mode == "full" else incremental_layout(d, spacing)

def _extend_paper(d: Drawing, need_w: float) -> str | None:
    """按「每次加长图样宽度的 1/4」找最小的加长幅面，使可用宽度不小于 need_w（模型单位）；找不到返回 None。"""
    import re

    from zhihui.ir.models import BASE_FORMAT

    from .standards import for_drawing

    if d.template_id == WATER:
        return None
    cur = d.paper_size
    m = re.fullmatch(r"(A[0-4])(?:\+(\d)/4)?", cur)
    if not m:
        return None
    base, n0 = m.group(1), int(m.group(2) or 0)
    limit = int(for_drawing(d).raw.get("paper", {}).get("extend_quarters_max", 4))
    for n in range(n0 + 1, limit + 1):
        d.paper_size = f"{base}+{n}/4"
        ux0, _, ux1, _ = usable_area(d)
        d.paper_size = cur
        if ux1 - ux0 >= need_w:
            return f"{base}+{n}/4"
    _ = BASE_FORMAT
    return None
