"""设备一览表、管道一览表（《CATIA V5-6 R2014 工程图教程》第 7 章物料清单；GB/T 10609.2-2009 明细栏；HG/T 20519 设备一览表）。

- 设备一览表：序号 / 位号 / 名称 / 规格型号 / 材料 / 数量 / 备注，从设备属性生成；同名称、同规格（规格非空）、同材料的设备并成一行，
  数量为台数、位号写成「P-101A/B」或「P-101、P-102」；属性缺失的格子留空，不编造。
- 管道一览表：管道号 / DN / 介质 / 起点 / 终点，从连接生成；DN 只取用户给的规格；管道号按流程顺序编（代号-序号）。
- 顺序：流程顺序（沿连接的拓扑序，起点相同按 x 坐标）；位号自动编号（tags.renumber）也用这个顺序。
- 放置：inline 放在标题栏上方（明细栏：表头在下、序号自下而上），放不下就放标题栏左侧，再放不下改为单独一页；
  sheet 单独一页（表头在上）。两种都由 table.py 画。
"""

from __future__ import annotations

import re

from zhihui.ir import Drawing, Equipment

from .standards import LY
from .table import Cell, Table, draw as draw_table

NOT_EQUIPMENT = {"pipe_terminal", "dosing_point", "valve", "retaining_wall"}  # 管道去向、加药点、阀门、挡土墙不进设备表
SPEC_KEYS = ("规格型号", "规格", "型号", "spec", "model")
MAT_KEYS = ("材料", "材质", "material")
REMARK_KEYS = ("备注", "remark")
ROW_H = 7.0  # GB/T 10609.2 明细栏行高 7 mm
HEAD_H = 10.0
CAPTION_H = 10.0
EQUIP_COLS = [("序号", 10, "center"), ("位号", 25, "center"), ("名称", 35, "left"), ("规格型号", 45, "left"),
              ("材料", 22, "center"), ("数量", 12, "center"), ("备注", 31, "left")]
PIPE_COLS = [("管道号", 25, "center"), ("DN", 20, "center"), ("介质", 45, "left"), ("起点", 45, "left"), ("终点", 45, "left")]
CAPTION = {"equipment": "设备一览表", "pipe": "管道一览表"}


def _param(e: Equipment, keys) -> str:
    for k in keys:
        v = e.params.get(k)
        if isinstance(v, (str, int, float)) and str(v).strip():
            return str(v).strip()
    return ""


def flow_order(d: Drawing) -> list[Equipment]:
    """沿连接的拓扑序（Kahn）；入度为 0 的按（x 坐标, 登记顺序）先后；有环时取剩下的第一个继续。"""
    idx = {e.id: i for i, e in enumerate(d.equipment)}
    succ: dict[str, list[str]] = {e.id: [] for e in d.equipment}
    indeg = {e.id: 0 for e in d.equipment}
    for c in d.connections:
        if c.from_equip in succ and c.to_equip in indeg and c.from_equip != c.to_equip:
            succ[c.from_equip].append(c.to_equip)
            indeg[c.to_equip] += 1

    def key(eid: str):
        e = d.get_equipment(eid)
        return (e.x if e.placed else float("inf"), idx[eid])

    out, done = [], set()
    ready = sorted((k for k, v in indeg.items() if v == 0), key=key)
    while len(out) < len(d.equipment):
        if not ready:  # 环：取剩下的第一个
            ready = [min((k for k in indeg if k not in done), key=key)]
        cur = ready.pop(0)
        if cur in done:
            continue
        done.add(cur)
        out.append(d.get_equipment(cur))
        for n in succ[cur]:
            indeg[n] -= 1
            if indeg[n] <= 0 and n not in done:
                ready.append(n)
        ready.sort(key=key)
    return out


def merge_tags(tags: list[str]) -> str:
    """P-101A、P-101B → P-101A/B；其他用顿号连接。"""
    if len(tags) > 1:
        m = [re.fullmatch(r"(.*\d)([A-Z])", t) for t in tags]
        if all(m) and len({x.group(1) for x in m}) == 1:
            return m[0].group(1) + "/".join(x.group(2) for x in m)
    return "、".join(tags)


def equipment_rows(d: Drawing) -> list[list[str]]:
    groups: dict[tuple, list[Equipment]] = {}
    for e in flow_order(d):
        if e.symbol_id in NOT_EQUIPMENT:
            continue
        spec = _param(e, SPEC_KEYS)
        key = (e.name, spec, _param(e, MAT_KEYS), _param(e, REMARK_KEYS)) if spec else (e.id,)  # 没有规格的不并（不知道是不是同一种）
        groups.setdefault(key, []).append(e)
    rows = []
    for i, es in enumerate(groups.values(), 1):
        e = es[0]
        remark = _param(e, REMARK_KEYS)
        status = str(e.params.get("status", "")).strip()
        if status and status not in remark:
            remark = f"{remark} {status}".strip()
        rows.append([str(i), merge_tags([x.tag for x in es]), e.name, _param(e, SPEC_KEYS), _param(e, MAT_KEYS), str(len(es)), remark])
    return rows


def _end_name(d: Drawing, eid: str) -> str:
    e = d.get_equipment(eid)
    if e is None:
        return ""
    if e.symbol_id == "pipe_terminal":  # 管道去向本身就是一句话（如「至配水管网」）
        return e.name
    return f"{e.tag} {e.name}".strip()


def pipe_rows(d: Drawing) -> list[list[str]]:
    from .water import CODE_NAMES

    rank = {e.id: i for i, e in enumerate(flow_order(d))}
    conns = sorted(d.connections, key=lambda c: (rank.get(c.from_equip, 0), rank.get(c.to_equip, 0)))
    seq: dict[str, int] = {}
    rows = []
    for c in conns:
        prefix = c.code or "L"
        seq[prefix] = seq.get(prefix, 0) + 1
        medium = c.medium or d.code_names.get(c.code, "") or CODE_NAMES.get(c.code, "")
        dn = c.spec.strip()
        rows.append([f"{prefix}-{seq[prefix]:02d}", dn, medium, _end_name(d, c.from_equip), _end_name(d, c.to_equip)])
    return rows


def build(d: Drawing, kind: str, header_at_bottom: bool, rows: list[list[str]] | None = None) -> Table:
    """一张一览表。header_at_bottom：明细栏样式（本图标题栏上方，表头在下、序号自下而上）。"""
    cols = EQUIP_COLS if kind == "equipment" else PIPE_COLS
    rows = (equipment_rows(d) if kind == "equipment" else pipe_rows(d)) if rows is None else rows
    body = list(reversed(rows)) if header_at_bottom else rows
    heights = [CAPTION_H] + ([ROW_H] * len(body) + [HEAD_H] if header_at_bottom else [HEAD_H] + [ROW_H] * len(body))
    cells = [Cell(0, 0, CAPTION[kind], cs=len(cols), role="table_head", key="caption")]
    head_r = len(heights) - 1 if header_at_bottom else 1
    first = 1 if header_at_bottom else 2
    for j, (name, _, _) in enumerate(cols):
        cells.append(Cell(head_r, j, name, role="table_cell", key=f"head:{name}"))
    for i, row in enumerate(body):
        for j, (name, _, align) in enumerate(cols):
            cells.append(Cell(first + i, j, row[j], role="table_cell", align=align, key=name))
    return Table(cols=[w for _, w, _ in cols], rows=heights, cells=cells, kind=f"schedule_{kind}")


def _obstacles(msp, d: Drawing) -> list[tuple[float, float, float, float]]:
    from ezdxf import bbox

    from .symbols import inner_frame

    fx0, fy0, fx1, fy1 = inner_frame(d)
    out = []
    for e in msp:
        if e.dxftype() == "HATCH" or e.dxf.layer in (LY["border"], LY["frame"], LY["table"]):  # 图框、标题栏、表格本身不算障碍
            continue
        ext = bbox.extents([e], fast=True)
        if not ext.has_data:
            continue
        if ext.size.x > (fx1 - fx0) * 0.9 and ext.size.y > (fy1 - fy0) * 0.9:  # 纸边线
            continue
        out.append((ext.extmin.x, ext.extmin.y, ext.extmax.x, ext.extmax.y))
    return out


def place_inline(msp, d: Drawing) -> list[str]:
    """本图里的一览表：标题栏上方往上叠，叠不下放标题栏左侧（底边对齐图框）往左排。返回放不下、改到单独一页的表。"""
    from .symbols import inner_frame
    from .titleblock import size as tb_size

    inline = [s.kind for s in d.schedules if s.placement == "inline"]
    if not inline:
        return []
    s = d.scale
    fx0, fy0, fx1, fy1 = inner_frame(d)
    tbw, tbh = tb_size(d)
    boxes = _obstacles(msp, d)
    m = 2 * s

    def free(x0, y0, x1, y1) -> bool:
        if x0 < fx0 or y1 > fy1:
            return False
        return not any(x0 - m < b[2] and x1 + m > b[0] and y0 - m < b[3] and y1 + m > b[1] for b in boxes)

    moved = []
    above_y = fy0 + tbh * s  # 标题栏顶
    left_x = fx1 - tbw * s  # 标题栏左边
    for kind in inline:
        t = build(d, kind, header_at_bottom=True)
        w, h = t.width * s, t.height * s
        if free(fx1 - w, above_y, fx1, above_y + h):
            org = (fx1 - w, above_y)
            above_y += h
        elif free(left_x - w, fy0, left_x, fy0 + h):
            org = (left_x - w, fy0)
            left_x -= w
        else:
            moved.append(kind)
            continue
        draw_table(msp, d, t, org, t.kind)
    return moved


def sheet_kinds(d: Drawing, overflow: list[str] | None = None) -> list[str]:
    return [s.kind for s in d.schedules if s.placement == "sheet"] + list(overflow or [])


def sheet_pages(d: Drawing, kinds: list[str]) -> list[list[tuple[Table, tuple[float, float]]]]:
    """单独出页的一览表排版：表头在上，从图框左上角起一列一列往右排，列满换页；表太长就拆成几段（续表）。
    返回每页的 [(表格, 左下角模型坐标)]。"""
    from .symbols import inner_frame
    from .titleblock import size as tb_size

    if not kinds:
        return []
    s = d.scale
    fx0, fy0, fx1, fy1 = inner_frame(d)
    tbw, tbh = tb_size(d)
    gap = 10.0
    pieces: list[Table] = []
    for kind in kinds:
        rows = equipment_rows(d) if kind == "equipment" else pipe_rows(d)
        per = max(1, int(((fy1 - fy0) / s - 2 * gap - CAPTION_H - HEAD_H) // ROW_H))
        chunks = [rows[i:i + per] for i in range(0, len(rows), per)] or [[]]
        for k, ch in enumerate(chunks):
            t = build(d, kind, header_at_bottom=False, rows=ch)
            if k:
                t.cells[0].text = f"{CAPTION[kind]}（续）"
            pieces.append(t)
    pages: list[list] = [[]]
    top = fy1 - gap * s
    x, y, colw = fx0 + gap * s, top, 0.0
    for t in pieces:
        w, h = t.width * s, t.height * s
        for _ in range(3):
            # 和标题栏左右有重叠的列，底边让到标题栏上方
            bottom = fy0 + gap * s if x + w <= fx1 - tbw * s + 1e-6 else fy0 + (tbh + gap) * s
            if x + w <= fx1 - gap * s + 1e-6 and y - h >= bottom - 1e-6:
                break
            if y < top - 1e-6:  # 这一列已经有表：换下一列
                x, y, colw = x + colw + gap * s, top, 0.0
            elif pages[-1]:  # 这一页已经有表：换页
                pages.append([])
                x, y, colw = fx0 + gap * s, top, 0.0
            else:
                break  # 空页也放不下（表比图幅还大），只能硬放
        pages[-1].append((t, (x, y - h)))
        y -= h + gap * s
        colw = max(colw, w)
    return [p for p in pages if p]
