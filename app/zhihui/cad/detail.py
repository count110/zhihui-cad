"""局部放大图（《CATIA V5-6 R2014 工程图教程》3.5.6 节；GB/T 4458.1 局部放大图的画法）。

阀组、加药点、计量泵组这类细节多的部位：在原处画细实线索引圆并写罗马数字（I、II…），
在图纸空白处按放大比例（2:1、5:1、2.5:1、4:1）画出圆内的图形，上方写「I 放大 2:1」。
圆内的直线、多段线、圆弧（含设备块里的图形）按圆裁剪后放大；文字只搬位置、字高不变（仍在字高系列里）。
"""

from __future__ import annotations

import math

from shapely.geometry import LineString, Point

from zhihui.ir import Drawing

from .standards import LY, text_h

ROMAN = ["I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X"]
APPID = "ZHIHUI"


def center(d: Drawing, dv) -> tuple[float, float] | None:
    from .hatch_ir import anchor_point

    a = anchor_point(d, dv.anchor_ref)
    if a is None:
        return None
    return (a[0] + dv.rel[0] * d.scale, a[1] + dv.rel[1] * d.scale)


def auto_radius(d: Drawing, ref: str) -> float:
    """没给半径时：圈住整台设备再留 3 mm（图纸 mm），限制在 6～25。"""
    from .symbols import footprint

    e = d.get_equipment(ref)
    if e is None or not e.placed:
        return 10.0
    x0, y0, x1, y1 = footprint(e, d.unit, d.template_id)
    half = max(x1 - x0, y1 - y0) / 2 / d.scale
    return float(min(25.0, max(6.0, round(half + 3))))


def _geometry(msp, cx, cy, r):
    """圆内的图形：[(图层, 折线点列)] 和 [(文字, 位置, 字高, 图层)]。"""
    from ezdxf import path as ezpath

    circle = Point(cx, cy).buffer(r, 64)
    lines, texts = [], []

    def take(e, layer):
        t = e.dxftype()
        if t in ("TEXT", "ATTRIB"):
            from ezdxf import bbox
            from shapely.geometry import box

            ext = bbox.extents([e], fast=True)
            if ext.has_data and e.dxf.text.strip() and circle.contains(box(ext.extmin.x, ext.extmin.y, ext.extmax.x, ext.extmax.y)):
                texts.append((e.dxf.text, (ext.extmin.x, ext.extmin.y), e.dxf.height, layer))  # 整段文字在圆内才搬
            return
        if t not in ("LINE", "LWPOLYLINE", "ARC", "CIRCLE", "ELLIPSE", "SPLINE"):
            return
        try:
            pts = [(v.x, v.y) for v in ezpath.make_path(e).flattening(r / 200)]
        except TypeError:
            return
        if len(pts) < 2:
            return
        cut = LineString(pts).intersection(circle)
        for g in getattr(cut, "geoms", [cut]):
            if g.geom_type == "LineString" and g.length > 0:
                lines.append((layer, list(g.coords)))

    for e in msp:
        xd = [v for _, v in e.get_xdata(APPID)] if e.has_xdata(APPID) else []
        if xd[:1] == ["detail"] or e.dxftype() in ("HATCH", "DIMENSION"):
            continue
        if e.dxftype() == "INSERT":
            for v in e.virtual_entities():
                take(v, e.dxf.layer if v.dxf.layer == "0" else v.dxf.layer)
            for a in e.attribs:
                if not (a.dxf.flags & 1):
                    take(a, e.dxf.layer)
        else:
            take(e, e.dxf.layer)
    return lines, texts


def free_spot(d: Drawing, msp, w: float, h: float):
    """图纸空白处放得下 w×h 的位置（左下角），找不到返回 None。从右上往左下扫。"""
    from ezdxf import bbox

    from .symbols import usable_area

    ux0, uy0, ux1, uy1 = usable_area(d)
    boxes = []
    for e in msp:
        if e.dxftype() in ("HATCH",):
            continue
        ext = bbox.extents([e], fast=True)
        if ext.has_data and (ext.size.x < (ux1 - ux0) * 0.9 or ext.size.y < (uy1 - uy0) * 0.9):  # 图框、纸边线不算
            boxes.append((ext.extmin.x, ext.extmin.y, ext.extmax.x, ext.extmax.y))
    step = 5 * d.scale
    m = 2 * d.scale
    y = uy1 - h
    while y >= uy0:
        x = ux1 - w
        while x >= ux0:
            if not any(x - m < b[2] and x + w + m > b[0] and y - m < b[3] and y + h + m > b[1] for b in boxes):
                return x, y
            x -= step
        y -= step
    return None


def label_spot(d: Drawing, msp, c, r, label: str, th: float) -> tuple[float, float]:
    """索引圆外侧写罗马数字：45°、135°、315°、225°、90°… 里第一个不压图线、不压文字的位置。"""
    from ezdxf import bbox
    from shapely.geometry import box

    from .linework import collect
    from .standards import text_width

    segs = [LineString([s.a, s.b]) for s in collect(msp.doc)]
    tboxes = []
    for e in msp.query("TEXT MTEXT"):
        ext = bbox.extents([e], fast=True)
        if ext.has_data:
            tboxes.append(box(ext.extmin.x, ext.extmin.y, ext.extmax.x, ext.extmax.y))
    w = text_width(label, th)
    best = None
    for k, ang in enumerate((45, 135, 315, 225, 90, 270, 0, 180)):
        a = math.radians(ang)
        off = r + 0.6 * d.scale + max(w, th) / 2
        p = (c[0] + off * math.cos(a), c[1] + off * math.sin(a))
        b = box(p[0] - w / 2 - 0.5 * d.scale, p[1] - th / 2 - 0.5 * d.scale, p[0] + w / 2 + 0.5 * d.scale, p[1] + th / 2 + 0.5 * d.scale)
        hits = sum(b.intersects(g) for g in segs) + sum(b.intersects(t) for t in tboxes)
        if best is None or (hits, k) < best[0]:
            best = ((hits, k), p)
        if hits == 0:
            break
    return best[1]


def draw(msp, doc, d: Drawing) -> list[str]:
    """画局部放大图；返回放不下的放大图 id（校验给提示）。"""
    from ezdxf.enums import TextEntityAlignment as TA

    from .dxf_io import _text

    failed = []
    for i, dv in enumerate(d.details):
        c = center(d, dv)
        if c is None:
            failed.append(dv.id)
            continue
        r = dv.radius * d.scale
        label = dv.label or ROMAN[i % len(ROMAN)]
        lines, texts = _geometry(msp, c[0], c[1], r)
        xd = [(1000, "detail"), (1000, dv.id)]
        R = r * dv.factor
        th = text_h(d, "annotation", label)
        spot = free_spot(d, msp, 2 * R, 2 * R + 2 * th)
        # 原处：细实线索引圆 + 罗马数字
        msp.add_circle(c, r, dxfattribs={"layer": LY["flow_arrow"]}).set_xdata(APPID, xd)
        _text(msp, label, label_spot(d, msp, c, r, label, th), th, LY["anno"], TA.MIDDLE_CENTER).set_xdata(APPID, xd)
        if spot is None:
            failed.append(dv.id)
            continue
        ox, oy = spot[0] + R, spot[1] + R
        for layer, pts in lines:
            msp.add_lwpolyline([(ox + (x - c[0]) * dv.factor, oy + (y - c[1]) * dv.factor) for x, y in pts],
                               dxfattribs={"layer": layer}).set_xdata(APPID, xd)
        for text, (x, y), h, layer in texts:
            _text(msp, text, (ox + (x - c[0]) * dv.factor, oy + (y - c[1]) * dv.factor), h, layer, TA.LEFT).set_xdata(APPID, xd)
        msp.add_circle((ox, oy), R, dxfattribs={"layer": LY["flow_arrow"]}).set_xdata(APPID, xd)
        title = f"{label} 放大 {dv.factor:g}:1"
        _text(msp, title, (ox, oy + R + 1.0 * d.scale), text_h(d, "annotation", title), LY["anno"], TA.BOTTOM_CENTER).set_xdata(APPID, xd)
    return failed
