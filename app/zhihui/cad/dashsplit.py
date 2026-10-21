"""虚线、点画线、双点画线按线素相位拆成短线段（GB/T 17450-1998 第 5.2.1 条：No.02～06 应恰当地相交于画线处）。

DXF 线型从整条线的起点开始排线素，拐角、十字交点落在哪里是碰运气；需要精确相交的线（虚线框的四个角、中心线的十字、
虚线管道的拐角和三通）由这里算好相位：
- 每段直线（两个顶点之间，或被交点切开）单独排，两端各是半个「画」，中间整数个线素周期，线素按比例略微伸缩；
- 相邻两段在顶点处的两个半画连成一个折线画，拐角正好落在画线上；交点处同理（图 7、图 9、图 11）；
- 点（长度 0）画成 0.5d 的短线（GB/T 17450 第 2.1 条注 2：长度不大于线宽一半的图线为点）；
- 太短放不下一个周期的段整段画实。
拆出来的线段写在「原层名-分段」层（同色同宽、实线，见 standards.layer_table），XDATA 照原线写，read_dxf 读回时拼回整条。
"""

from __future__ import annotations

import math

Pt = tuple[float, float]


def _runs(points: list[Pt], closed: bool, cuts: tuple[Pt, ...]) -> list[tuple[Pt, Pt]]:
    """折线 → 直线段列表；cuts 里落在某段上的点把该段切开（十字交点）。"""
    pts = list(points) + ([points[0]] if closed and points[0] != points[-1] else [])
    runs = []
    for a, b in zip(pts, pts[1:]):
        L = math.dist(a, b)
        if L < 1e-9:
            continue
        ts = sorted({0.0, 1.0} | {
            ((c[0] - a[0]) * (b[0] - a[0]) + (c[1] - a[1]) * (b[1] - a[1])) / (L * L)
            for c in cuts
            if abs((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])) / L < 1e-6 * max(L, 1.0)
        })
        ts = [t for t in ts if -1e-9 <= t <= 1 + 1e-9]
        for t0, t1 in zip(ts, ts[1:]):
            if t1 - t0 > 1e-9:
                runs.append(((a[0] + (b[0] - a[0]) * t0, a[1] + (b[1] - a[1]) * t0), (a[0] + (b[0] - a[0]) * t1, a[1] + (b[1] - a[1]) * t1)))
    return runs


def _intervals(L: float, elements: list[float], dot: float) -> list[tuple[float, float]]:
    """一段长 L 的直线上要画的区间 [t0, t1]（沿线距离）：两端各半个画，中间整数个周期。"""
    dash = elements[0]
    unit = sum(abs(e) for e in elements)
    if dash <= 0 or unit <= 0 or L < unit * 0.75:  # 放不下一个周期：整段画实
        return [(0.0, L)]
    k = max(1, round(L / unit))
    f = L / (k * unit)
    out: list[tuple[float, float]] = []
    pos = dash / 2 * f
    out.append((0.0, pos))
    for i in range(k):
        for e in elements[1:]:
            if e < 0:
                pos += -e * f
            elif e == 0:
                out.append((max(0.0, pos - dot / 2), min(L, pos + dot / 2)))
            else:
                out.append((pos, pos + e * f))
                pos += e * f
        seg = dash * f if i < k - 1 else dash / 2 * f
        out.append((pos, min(L, pos + seg)))
        pos += seg
    return out


def split(points: list[Pt], elements: list[float], dot: float, closed: bool = False, cuts: tuple[Pt, ...] = ()) -> list[list[Pt]]:
    """按线素拆成若干条折线（每条是一个画或一个点；跨过顶点的画是一条折线）。elements 为模型单位、画为正、间隔为负、点为 0。"""
    runs = _runs(points, closed, cuts)
    if not runs:
        return []
    pieces: list[list[Pt]] = []
    carry: list[Pt] | None = None  # 上一段末尾的半个画，等着和下一段开头的半个画连起来
    for a, b in runs:
        L = math.dist(a, b)
        ux, uy = (b[0] - a[0]) / L, (b[1] - a[1]) / L
        iv = _intervals(L, elements, dot)
        at = lambda t: (a[0] + ux * t, a[1] + uy * t)  # noqa: E731
        for j, (t0, t1) in enumerate(iv):
            seg = [at(t0), at(t1)]
            first, last = j == 0, j == len(iv) - 1
            if first and carry is not None:
                seg = carry + seg[1:]
                carry = None
            if last and abs(t1 - L) < 1e-9:
                carry = seg
                continue
            pieces.append(seg)
    if carry is not None:
        if closed and pieces:
            pieces[0] = carry + pieces[0][1:]
        else:
            pieces.append(carry)
    return [_dedupe(p) for p in pieces if len(p) >= 2]


def _dedupe(p: list[Pt]) -> list[Pt]:
    out = [p[0]]
    for q in p[1:]:
        if math.dist(q, out[-1]) > 1e-9:
            out.append(q)
    return out if len(out) >= 2 else [p[0], p[-1]]


def layer_info(d, layer: str):
    """(要不要拆, 线素（模型单位）, 点长, 拆后的层名)。图层线型不是 GB 虚线类时不拆。"""
    from .standards import for_drawing, layer_table, linetype_elements

    std = for_drawing(d)
    spec = layer_table(std).get(layer)
    if not spec or not str(spec["linetype"]).startswith("ZH_"):
        return False, [], 0.0, layer
    lt = spec["linetype"]
    w = spec["lineweight"] / 100
    elements = [e * d.scale for e in linetype_elements(std, lt[3:5], w)]
    return True, elements, 0.5 * w * d.scale, layer + std.raw["layers"].get("split_suffix", "-分段")


def draw(msp, d, points: list[Pt], layer: str, closed: bool = False, cuts: tuple[Pt, ...] = (), xdata=None, part_xdata=None) -> list:
    """画一条按相位拆开的虚线类折线；不是虚线类图层就照常画整条。
    xdata：第一段（或每段，part_xdata 为空时）的 XDATA；part_xdata(i)：第 i 段（i ≥ 1）的 XDATA。"""
    from .dxf_io import APPID

    yes, elements, dot, split_layer = layer_info(d, layer)
    if not yes:
        e = msp.add_lwpolyline(points, close=closed, dxfattribs={"layer": layer})
        if xdata:
            e.set_xdata(APPID, xdata)
        return [e]
    out = []
    for i, pc in enumerate(split(points, elements, dot, closed, cuts)):
        e = msp.add_lwpolyline(pc, dxfattribs={"layer": split_layer})
        xd = xdata if (i == 0 or part_xdata is None) else part_xdata(i)
        if xd:
            e.set_xdata(APPID, xd)
        out.append(e)
    return out
