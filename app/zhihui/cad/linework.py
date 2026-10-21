"""图线检查（GB/T 4457.4-2002 第 4 条，《CATIA V5-6 R2014 工程图教程》1.3 节）。

- 两条平行线之间的最小间隙不小于 0.7 mm（图纸尺寸）；
- 同一处图线重合时只画一种，优先级：可见轮廓线 > 不可见轮廓线 > 对称中心线 > 尺寸界线。
检查对象是出图后的 DXF 模型空间（含块参照展开后的图形）；同一个块参照内部的图元视为图元库设计，不互相比较。
"""

from __future__ import annotations

from dataclasses import dataclass

EPS = 1e-6
PRIORITY = {"visible": 0, "hidden": 1, "center": 2, "extension": 3}


@dataclass
class Seg:
    a: tuple[float, float]
    b: tuple[float, float]
    layer: str
    kind: str  # visible / hidden / center / extension
    owner: str  # 实体句柄；块参照里的图元用块参照的句柄

    @property
    def horizontal(self) -> bool:
        return abs(self.a[1] - self.b[1]) < EPS and abs(self.a[0] - self.b[0]) > EPS

    @property
    def vertical(self) -> bool:
        return abs(self.a[0] - self.b[0]) < EPS and abs(self.a[1] - self.b[1]) > EPS

    def span(self) -> tuple[float, float, float]:
        """(所在直线坐标, 起, 止)：横线为 (y, x0, x1)，竖线为 (x, y0, y1)。"""
        if self.horizontal:
            return self.a[1], min(self.a[0], self.b[0]), max(self.a[0], self.b[0])
        return self.a[0], min(self.a[1], self.b[1]), max(self.a[1], self.b[1])


def _kind(doc, layer: str) -> str:
    if layer.endswith("-分段"):  # 按线素相位拆开的虚线 / 点画线：类别跟原图层
        layer = layer[: -len("-分段")]
    lt = doc.layers.get(layer).dxf.linetype.upper() if layer in doc.layers else "CONTINUOUS"
    if "尺寸" in layer:
        return "extension"
    from .standards import linetype_kind

    return linetype_kind(lt)


def _segments_of(e, layer: str, owner: str, kind: str) -> list[Seg]:
    t = e.dxftype()
    if t == "LINE":
        return [Seg((e.dxf.start.x, e.dxf.start.y), (e.dxf.end.x, e.dxf.end.y), layer, kind, owner)]
    if t == "LWPOLYLINE":
        if e.dxf.const_width > 0:  # 实心圆点（三通）
            return []
        pts = list(e.get_points("xyb"))
        if e.closed and pts:
            pts.append(pts[0])
        return [Seg((x0, y0), (x1, y1), layer, kind, owner) for (x0, y0, b), (x1, y1, _) in zip(pts, pts[1:]) if abs(b) < EPS]
    return []


def collect(doc) -> list[Seg]:
    """模型空间里所有直线段（文字、填充、圆弧不算）。块里放在 0 层的图元按插入层算。"""
    msp = doc.modelspace()
    out: list[Seg] = []
    for e in msp:
        t = e.dxftype()
        if t in ("LINE", "LWPOLYLINE"):
            out += _segments_of(e, e.dxf.layer, e.dxf.handle, _kind(doc, e.dxf.layer))
        elif t == "INSERT":
            for v in e.virtual_entities():
                layer = e.dxf.layer if v.dxf.layer == "0" else v.dxf.layer
                out += _segments_of(v, layer, e.dxf.handle, _kind(doc, layer))
    return [s for s in out if s.horizontal or s.vertical]


def _pairs(segs: list[Seg], window: float):
    """同方向、所在直线坐标相差不超过 window 的线段对（排序后滑窗，避免两两比较）。"""
    for horiz in (True, False):
        group = sorted((s for s in segs if s.horizontal == horiz), key=lambda s: s.span()[0])
        for i, s in enumerate(group):
            c0 = s.span()[0]
            for o in group[i + 1:]:
                if o.span()[0] - c0 > window:
                    break
                yield s, o


def overlaps(segs: list[Seg], tol: float) -> list[dict]:
    """重合（共线且有重叠长度）的图线对，以及按优先级应保留哪条。tol 为模型单位的重叠长度下限。"""
    out = []
    for s, o in _pairs(segs, EPS * 10):
        if s.owner == o.owner:
            continue
        c, a0, a1 = s.span()
        _, b0, b1 = o.span()
        length = min(a1, b1) - max(a0, b0)
        if length > tol:
            keep = s if PRIORITY[s.kind] <= PRIORITY[o.kind] else o
            out.append({"at": c, "length": length, "layers": (s.layer, o.layer), "keep": keep.kind, "owners": (s.owner, o.owner)})
    return out


def close_parallels(segs: list[Seg], min_gap: float, min_len: float) -> list[dict]:
    """间距大于 0、小于 min_gap 且并行长度超过 min_len 的平行线对（模型单位）。"""
    out = []
    for s, o in _pairs(segs, min_gap):
        if s.owner == o.owner:
            continue
        c0, a0, a1 = s.span()
        c1, b0, b1 = o.span()
        gap = abs(c1 - c0)
        if EPS * 10 < gap < min_gap and min(a1, b1) - max(a0, b0) > min_len:
            out.append({"gap": gap, "at": (c0 + c1) / 2, "layers": (s.layer, o.layer), "owners": (s.owner, o.owner)})
    return out


def check(doc, scale: float, min_gap_mm: float = 0.7) -> dict:
    """出图后的图线检查：返回 {overlaps, close_parallels}，长度、间隙都换算成图纸 mm。"""
    segs = collect(doc)
    ov = overlaps(segs, 0.1 * scale)
    cp = close_parallels(segs, min_gap_mm * scale, 1.0 * scale)
    for x in ov:
        x["length_mm"] = round(x.pop("length") / scale, 2)
    for x in cp:
        x["gap_mm"] = round(x.pop("gap") / scale, 2)
    return {"segments": len(segs), "overlaps": ov, "close_parallels": cp}
