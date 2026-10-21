"""尺寸标注（GB/T 4458.4-2003；《CATIA V5-6 R2014 工程图教程》1.3 节、5.3 节）。

- 用真正的 DIMENSION 实体（ezdxf add_linear_dim），标注样式 GB_DIM 取自标准配置：数字 3.5 号、尺寸界线超出尺寸线 2 mm、
  实心箭头、尺寸线和尺寸界线细实线（ZH-尺寸 层，随层）。
- 量的是对象当前的几何：设备移动、换图元后尺寸跟着走。示意图不按比例时 text 写用户给的尺寸值。
- 排列：同一侧、同一方向的尺寸按基准等间距排列（间距 7 mm），内小外大，尺寸线之间不交叉。
- 检查（check）：尺寸线不与其他图线重合、不画在轮廓线的延长线上、尺寸线之间不交叉、同一尺寸不重复标注、数字不被图线穿过。
"""

from __future__ import annotations

from zhihui.ir import Dimension, Drawing

from .standards import LY, for_drawing, text_h
from .symbols import WATER, footprint, symbol_box

STYLE = "GB_DIM"
APPID = "ZHIHUI"


def setup_dimstyle(doc, d: Drawing) -> None:
    from .dxf_io import TEXT_STYLE

    dm = for_drawing(d).raw["dim"]
    if STYLE in doc.dimstyles:
        doc.dimstyles.remove(STYLE)
    ds = doc.dimstyles.new(STYLE)
    ds.dxf.dimscale = d.scale  # 下面的尺寸都按图纸 mm 给，乘出图比例
    ds.dxf.dimtxt = dm["text_height"]
    ds.dxf.dimasz = dm["arrow_mm"]
    ds.dxf.dimexe = dm["ext_beyond_mm"]  # 尺寸界线超出尺寸线 2 mm
    ds.dxf.dimexo = dm["ext_offset_mm"]
    ds.dxf.dimgap = 1.0
    ds.dxf.dimtad = 1  # 数字写在尺寸线上方
    ds.dxf.dimtih = 0
    ds.dxf.dimtoh = 0
    ds.dxf.dimdec = 0
    ds.dxf.dimtxsty = TEXT_STYLE
    ds.dxf.dimclrd = 0  # 随块：尺寸实体本身随层（ZH-尺寸，细线）
    ds.dxf.dimclre = 0
    ds.dxf.dimclrt = 0
    ds.dxf.dimlwd = -2
    ds.dxf.dimlwe = -2


def _side(d: Drawing, dim: Dimension) -> str:
    if dim.side != "auto":
        return dim.side
    if dim.measure in ("width", "between_h"):
        return "top" if d.template_id == WATER else "bottom"  # 高程图构筑物下方写名称，尺寸放上方
    return "left"


def points(d: Drawing, dim: Dimension):
    """(p1, p2, 水平?)：两个被量点（模型坐标）。"""
    u = d.unit
    eqs = [d.get_equipment(r) for r in dim.refs]
    if any(e is None or not e.placed for e in eqs):
        return None
    if dim.measure in ("width", "height"):
        x0, y0, x1, y1 = symbol_box(eqs[0], u)
        side = _side(d, dim)
        if dim.measure == "width":
            y = y1 if side == "top" else y0
            return (x0, y), (x1, y), True
        x = x0 if side == "left" else x1
        return (x, y0), (x, y1), False
    if len(eqs) < 2:
        return None
    a, b = eqs[0], eqs[1]
    if dim.measure == "between_h":
        return (a.x, a.y), (b.x, b.y), True
    return (a.x, a.y), (b.x, b.y), False


def arrange(d: Drawing) -> dict[str, float]:
    """每个尺寸的尺寸线位置（横尺寸为 y，竖尺寸为 x）：从对象外沿起 7 mm 一档，同侧重叠的按长度内小外大往外排。"""
    gap = for_drawing(d).raw["dim"]["gap_mm"] * d.scale
    u = d.unit
    groups: dict[str, list] = {}
    for dim in d.dimensions:
        pts = points(d, dim)
        if pts is None:
            continue
        p1, p2, horiz = pts
        fps = [footprint(d.get_equipment(r), u, d.template_id) for r in dim.refs]
        side = _side(d, dim)
        lo, hi = (min(p1[0], p2[0]), max(p1[0], p2[0])) if horiz else (min(p1[1], p2[1]), max(p1[1], p2[1]))
        edge = {"top": max(f[3] for f in fps), "bottom": min(f[1] for f in fps), "left": min(f[0] for f in fps), "right": max(f[2] for f in fps)}[side]
        groups.setdefault(side, []).append((hi - lo, lo, hi, edge, dim.id))
    out: dict[str, float] = {}
    for side, items in groups.items():
        sign = 1 if side in ("top", "right") else -1
        placed: list[tuple[float, float, float]] = []  # (lo, hi, 位置)
        for length, lo, hi, edge, did in sorted(items):  # 短的先放（靠里）
            pos = edge + sign * gap
            for plo, phi, ppos in placed:
                if lo < phi and hi > plo and sign * (ppos - pos) >= -1e-6:
                    pos = ppos + sign * gap
            placed.append((lo, hi, pos))
            out[did] = pos
    return out


def draw(msp, doc, d: Drawing) -> None:
    if not d.dimensions:
        return
    setup_dimstyle(doc, d)
    level = arrange(d)
    for dim in d.dimensions:
        pts = points(d, dim)
        if pts is None or dim.id not in level:
            continue
        p1, p2, horiz = pts
        base = ((p1[0] + p2[0]) / 2, level[dim.id]) if horiz else (level[dim.id], (p1[1] + p2[1]) / 2)
        dm = msp.add_linear_dim(base=base, p1=p1, p2=p2, angle=0 if horiz else 90, dimstyle=STYLE,
                                text=dim.text or "<>", dxfattribs={"layer": LY["dim"]})
        ent = dm.render()
        ent.dimension.set_xdata(APPID, [(1000, v) for v in ("dim", dim.id, dim.measure, ",".join(dim.refs), dim.text, dim.side)])


# ---------------------------------------------------------------- 检查（GB/T 4458.4）

def _dim_parts(doc):
    """每个尺寸实体：(id, 尺寸线段, 尺寸界线段, 数字范围)。"""
    from ezdxf import bbox

    out = []
    for dim in doc.modelspace().query("DIMENSION"):
        xd = [v for _, v in dim.get_xdata(APPID)] if dim.has_xdata(APPID) else []
        lines, text_box = [], None
        for v in dim.virtual_entities():
            if v.dxftype() == "LINE":
                lines.append(((v.dxf.start.x, v.dxf.start.y), (v.dxf.end.x, v.dxf.end.y)))
            elif v.dxftype() in ("MTEXT", "TEXT"):
                ext = bbox.extents([v], fast=True)
                if ext.has_data:
                    text_box = (ext.extmin.x, ext.extmin.y, ext.extmax.x, ext.extmax.y)
        dp = dim.dxf.defpoint
        horiz = abs(dim.dxf.get("angle", 0.0)) < 1e-6
        dim_line = [ln for ln in lines if (abs(ln[0][1] - ln[1][1]) < 1e-6 and horiz and abs(ln[0][1] - dp.y) < 1e-6)
                    or (abs(ln[0][0] - ln[1][0]) < 1e-6 and not horiz and abs(ln[0][0] - dp.x) < 1e-6)]
        ext_lines = [ln for ln in lines if ln not in dim_line]
        out.append({"id": xd[1] if len(xd) > 1 else dim.dxf.handle, "key": (xd[2], xd[3]) if len(xd) > 3 else None,
                    "dim_line": dim_line, "ext": ext_lines, "text": text_box, "handle": dim.dxf.handle})
    return out


def _cross(a, b, c, e) -> bool:
    """两条正交线段是否在内部相交（端点相接不算）。"""
    (ax0, ay0), (ax1, ay1) = a, b
    (cx0, cy0), (cx1, cy1) = c, e
    if abs(ay0 - ay1) < 1e-6 and abs(cx0 - cx1) < 1e-6:
        x, y = cx0, ay0
        return min(ax0, ax1) + 1e-6 < x < max(ax0, ax1) - 1e-6 and min(cy0, cy1) + 1e-6 < y < max(cy0, cy1) - 1e-6
    if abs(ax0 - ax1) < 1e-6 and abs(cy0 - cy1) < 1e-6:
        return _cross(c, e, a, b)
    return False


def check(doc, d: Drawing) -> list[dict]:
    """GB/T 4458.4 的五条尺寸规则；返回问题列表 [{code, message, refs}]。"""
    from .linework import collect

    issues = []
    parts = _dim_parts(doc)
    if not parts:
        return issues
    segs = [s for s in collect(doc) if not s.layer.startswith(LY["dim"])]
    u = d.unit
    boxes = [symbol_box(e, u) for e in d.equipment if e.placed]
    seen: dict = {}
    for p in parts:
        if p["key"] is not None:  # 同一尺寸不重复标注
            if p["key"] in seen:
                issues.append({"code": "E_DIM_DUPLICATE", "message": f"尺寸 {p['id']} 与 {seen[p['key']]} 重复", "refs": [p["id"]]})
            seen[p["key"]] = p["id"]
        for (a, b) in p["dim_line"]:
            horiz = abs(a[1] - b[1]) < 1e-6
            c = a[1] if horiz else a[0]
            lo, hi = sorted((a[0], b[0]) if horiz else (a[1], b[1]))
            for s in segs:  # 尺寸线不与其他图线重合
                if s.horizontal == horiz and abs(s.span()[0] - c) < 1e-3 and min(hi, s.span()[2]) - max(lo, s.span()[1]) > 1e-3:
                    issues.append({"code": "E_DIM_OVERLAP", "message": f"尺寸 {p['id']} 的尺寸线与图线重合（{s.layer}）", "refs": [p["id"]]})
                    break
            for bx in boxes:  # 不画在轮廓线的延长线上
                edges = (bx[1], bx[3]) if horiz else (bx[0], bx[2])
                if any(abs(c - ed) < 1e-3 for ed in edges):
                    issues.append({"code": "E_DIM_ON_OUTLINE", "message": f"尺寸 {p['id']} 的尺寸线落在轮廓线的延长线上", "refs": [p["id"]]})
                    break
            for q in parts:  # 尺寸线之间、尺寸线与别的尺寸界线不交叉
                if q is p:
                    continue
                if any(_cross(a, b, c0, c1) for c0, c1 in q["dim_line"] + q["ext"]):
                    issues.append({"code": "E_DIM_CROSS", "message": f"尺寸 {p['id']} 与 {q['id']} 交叉", "refs": [p["id"], q["id"]]})
                    break
        t = p["text"]  # 数字不被任何图线穿过
        if t is not None:
            for s in segs:
                (x0, y0), (x1, y1) = s.a, s.b
                if min(x0, x1) < t[2] and max(x0, x1) > t[0] and min(y0, y1) < t[3] and max(y0, y1) > t[1]:
                    issues.append({"code": "E_DIM_TEXT_CROSSED", "message": f"尺寸 {p['id']} 的数字被图线穿过（{s.layer}）", "refs": [p["id"]]})
                    break
    return issues
