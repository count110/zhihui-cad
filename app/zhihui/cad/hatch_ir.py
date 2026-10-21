"""图纸里的填充：把 CAD-IR 的 Hatch 落到当前图形上（关联性）。

DXF 每次都从 IR 重新生成，所以「关联」就是：每次出图、每次工具改完图，都按
「锚点（设备 / 管线）当前位置 + pick_rel」重新拾取一次边界。设备移动、换图元、重新布局后填充跟着走；
求不出边界时标记 disassociated（校验给警告），沿用上一次求出的边界照常画出。
"""

from __future__ import annotations

import math

from shapely.geometry import Polygon

from zhihui.ir import Drawing, Hatch

from .hatch import FLATTEN_MM, HatchError, Prepared, Region, Ring, _closed_polygon, boundary_from_entities, find_boundary, paper_scale, to_lines, write_hatch

APPID = "ZHIHUI"
SKIP = ("HATCH", "TEXT", "MTEXT", "SOLID")
FRAME_LAYERS = ("ZH-图框", "ZH-标题栏", "FRAME", "BORDER")  # 图框、标题栏（兼容旧层名）


def anchor_point(d: Drawing, ref: str) -> tuple[float, float] | None:
    """设备：图元原点（包围盒中心附近）；管线：最长一段的中点。"""
    e = d.get_equipment(ref)
    if e is not None:
        return (e.x, e.y) if e.placed else None
    c = d.get_connection(ref)
    if c is not None and c.path and len(c.path) > 1:
        a, b = max(zip(c.path, c.path[1:]), key=lambda ab: math.dist(*ab))
        return ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
    return None


def pick_point(d: Drawing, h: Hatch) -> tuple[float, float] | None:
    if h.pick_rel is None:
        return None
    if h.anchor_ref:
        a = anchor_point(d, h.anchor_ref)
        if a is None:
            return None
        return (a[0] + h.pick_rel[0] * d.unit, a[1] + h.pick_rel[1] * d.unit)  # 相对锚点的偏移按图纸 mm 给
    return (h.pick_rel[0], h.pick_rel[1])


class Scene:
    """一张图的可拾取图形（不含填充、文字）+ 文字包围盒 + 「对象 id → 实体」。"""

    def __init__(self, d: Drawing, doc=None):
        from ezdxf import bbox

        from .dxf_io import build_doc

        self.doc = doc if doc is not None else build_doc(d)
        msp = self.doc.modelspace()
        # 对称中心线不是轮廓，不参与求边界
        ents = [e for e in msp if e.dxftype() not in SKIP and (_xdata(e)[:1] != ["axis"])]
        items = to_lines(ents, FLATTEN_MM * d.unit)
        self.prep = Prepared(items, FLATTEN_MM * d.unit)  # 全部图形（绝对坐标拾取，可以填标题栏格子）
        # 锚在设备 / 管线上的填充不认图框和标题栏：边界被改开口时，宁可失去关联，也不要悄悄扩成「图框里的一大片」
        self.prep_local = Prepared([it for it in items if it[1] is None or it[1].dxf.layer not in FRAME_LAYERS], FLATTEN_MM * d.unit)
        self.texts: list[tuple[float, float, float, float]] = []
        texts = list(msp.query("TEXT MTEXT"))
        self.by_id: dict[str, list] = {}
        for ins in msp.query("INSERT"):
            texts += [a for a in ins.attribs if not (a.dxf.flags & 1)]
            texts += [v for v in ins.virtual_entities() if v.dxftype() in ("TEXT", "MTEXT")]
            xd = _xdata(ins)
            if len(xd) >= 2 and xd[0] == "equip":
                self.by_id[xd[1]] = [ins]
        for pl in msp.query("LWPOLYLINE"):
            xd = _xdata(pl)
            if len(xd) >= 2 and xd[0] in ("conn", "conn_part"):
                self.by_id.setdefault(xd[1], []).append(pl)
        for t in texts:
            ext = bbox.extents([t], fast=True)
            if ext.has_data:
                self.texts.append((ext.extmin.x, ext.extmin.y, ext.extmax.x, ext.extmax.y))


def _xdata(e) -> list[str]:
    try:
        return [v for _, v in e.get_xdata(APPID)]
    except Exception:
        return []


def _outline(ins, tol: float):
    """选对象时选了设备：取图元里面积最大的闭合图形（设备外轮廓）。"""
    best = None
    for v in ins.virtual_entities():
        if v.dxftype() in SKIP:
            continue
        p = _closed_polygon(v, tol)
        if p is not None and (best is None or p.area > best[0]):
            best = (p.area, v)
    return best[1] if best else None


def resolve(d: Drawing, h: Hatch, sc: Scene) -> Region:
    if h.region:
        from .named_regions import resolve_named
        return resolve_named(d, h)
    if h.mode == "select":
        ents = []
        for ref in h.boundary_refs:
            found = sc.by_id.get(ref)
            if not found:
                raise HatchError("E_NOT_FOUND", f"选中的对象 {ref} 已不在图中", "重新选对象，或改用拾取点方式")
            if found[0].dxftype() == "INSERT":
                o = _outline(found[0], sc.prep.tol)
                if o is None:
                    raise HatchError("E_HATCH_NOT_CLOSED", f"设备 {ref} 没有闭合外轮廓", "改用拾取点方式，在设备图形里拾取")
                ents.append(o)
            else:
                ents += found
        return boundary_from_entities(ents, style=h.style, unit=d.unit, text_boxes=sc.texts, flatten_tol=sc.prep.tol)
    pick = pick_point(d, h)
    if pick is None:
        raise HatchError("E_NOT_FOUND", f"填充 {h.id} 的锚点 {h.anchor_ref} 已不在图中", "重新指定拾取点")
    prep = sc.prep_local if h.anchor_ref else sc.prep
    return find_boundary(prep, pick, gap_tol=h.gap_tol * d.unit, text_boxes=sc.texts, style=h.style, unit=d.unit)


def store(h: Hatch, region: Region) -> None:
    """把求出的边界记进 IR：面积、孤岛数、是否精确、边界环（失去关联时沿用）。"""
    h.area = round(region.area, 3)
    h.islands = region.islands
    h.exact = region.exact
    h.rings = [[(round(x, 3), round(y, 3)) for x, y in r.coords] for r in region.rings]
    h.exterior_count = region.exterior_count
    h.disassociated = False


def refresh(d: Drawing) -> list[str]:
    """改图后重新求所有填充的边界；返回有变化的填充 id（改图工具据此声明影响集）。"""
    if not d.hatches or not all(e.placed for e in d.equipment):
        return []
    sc = Scene(d)
    changed = []
    for h in d.hatches:
        before = (h.area, h.rings, h.disassociated)
        try:
            store(h, resolve(d, h, sc))
        except HatchError:
            h.disassociated = True
        if (h.area, h.rings, h.disassociated) != before:
            changed.append(h.id)
    return changed


def region_from_rings(h: Hatch) -> Region | None:
    """失去关联时，用上一次求出的边界画。"""
    if not h.rings:
        return None
    geom = None
    for coords in h.rings:
        if len(coords) < 3:
            continue
        p = Polygon(coords).buffer(0)
        geom = p if geom is None else geom.symmetric_difference(p)
    if geom is None:
        return None
    return Region(geom, [Ring(list(map(tuple, r))) for r in h.rings], h.exterior_count, h.islands, style=h.style)


def xdata(h: Hatch) -> list[tuple[int, str]]:
    rel = f"{h.pick_rel[0]:.6g},{h.pick_rel[1]:.6g}" if h.pick_rel else ""
    vals = ["hatch", h.id, h.anchor_ref or "", rel, h.pattern, f"{h.paper_spacing:g}", f"{h.angle:g}", h.style, h.mode,
            ",".join(h.boundary_refs), f"{h.gap_tol:g}", h.layer]
    return [(1000, v) for v in vals]


def write_hatches(doc, d: Drawing) -> list[str]:
    """出图时把填充画进 DXF（重新求边界）；返回失去关联、只能沿用旧边界的填充 id。"""
    from .dxf_io import _hatch_layer

    _hatch_layer(doc)
    msp = doc.modelspace()
    sc = Scene(d, doc)
    lost = []
    for h in d.hatches:
        if h.layer not in doc.layers:
            doc.layers.add(h.layer, color=8).dxf.lineweight = 18
        try:
            region = resolve(d, h, sc)
        except HatchError:
            region = region_from_rings(h)
            lost.append(h.id)
            if region is None:
                continue
        ent = write_hatch(msp, region, h.pattern, paper_scale(h.pattern, d.unit, h.paper_spacing), h.angle, h.layer, h.style)
        ent.set_xdata(APPID, xdata(h))
    return lost
