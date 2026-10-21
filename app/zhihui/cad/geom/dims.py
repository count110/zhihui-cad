"""标注几何：线性/对齐/角度/半径/直径/弧长标注的度量值、自动放置位置、基线与连续标注的偏移。"""
from __future__ import annotations

import math

from .entities import EPS, Arc, P, Pt, ang, ccw_span, dist, pl, polar, primitives
from .errors import GeomError


def fmt_measure(v: float, precision: int = 2) -> str:
    s = f"{v:.{precision}f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def linear_measure(p1: Pt, p2: Pt, dtype: str) -> float:
    if dtype == "linear_horizontal":
        return abs(p2[0] - p1[0])
    if dtype == "linear_vertical":
        return abs(p2[1] - p1[1])
    return dist(p1, p2)


def resolve_linear_type(p1: Pt, p2: Pt, dtype: str) -> str:
    if dtype != "linear_auto":
        return dtype
    return "linear_horizontal" if abs(p2[0] - p1[0]) >= abs(p2[1] - p1[1]) else "linear_vertical"


def auto_location(dtype: str, p1: Pt, p2: Pt, existing: list[dict], gap: float = 10.0, spacing: float = 7.0) -> Pt:
    """尺寸线位置自动避让：放在对象外侧 gap 处；已有同方向、同范围的标注则逐层外推 spacing。"""
    n = 0
    for d in existing:
        if d.get("dtype") == dtype and d.get("loc") is not None:
            if dtype == "linear_horizontal" and min(p1[0], p2[0]) <= max(d["p1"][0], d["p2"][0]) and max(p1[0], p2[0]) >= min(d["p1"][0], d["p2"][0]):
                n += 1
            elif dtype == "linear_vertical" and min(p1[1], p2[1]) <= max(d["p1"][1], d["p2"][1]) and max(p1[1], p2[1]) >= min(d["p1"][1], d["p2"][1]):
                n += 1
            elif dtype == "aligned":
                n += 1
    off = gap + n * spacing
    if dtype == "linear_horizontal":
        return ((p1[0] + p2[0]) / 2, max(p1[1], p2[1]) + off, 0.0)
    if dtype == "linear_vertical":
        return (max(p1[0], p2[0]) + off, (p1[1] + p2[1]) / 2, 0.0)
    a = math.radians(ang(p1, p2) + 90)
    return ((p1[0] + p2[0]) / 2 + off * math.cos(a), (p1[1] + p2[1]) / 2 + off * math.sin(a), 0.0)


def lines_angle(l1: tuple[Pt, Pt], l2: tuple[Pt, Pt]) -> tuple[float, Pt]:
    """两直线夹角（度，0–180）和交点（平行报错）。"""
    from .curves import intersect
    from .entities import Seg
    ip = intersect(Seg(l1[0], l1[1]), Seg(l2[0], l2[1]), extend=True)
    if not ip:
        raise GeomError("PARALLEL", "两条线平行，没有夹角", "")
    a1, a2 = ang(l1[0], l1[1]) % 180, ang(l2[0], l2[1]) % 180
    d = abs(a1 - a2)
    return min(d, 180 - d), ip[0]       # 取两线夹角中较小的一个（直角为 90°）


def circle_leader_point(c: Pt, r: float, angle_deg: float = 45.0, k: float = 1.6) -> Pt:
    return polar(c, r * k, angle_deg)


def arc_length(c: Pt, r: float, a0: float, a1: float) -> float:
    return math.radians(ccw_span(a0, a1)) * r
