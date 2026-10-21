"""专业工具集的共用构件：ops = [{kind, data, tags, role}]；role 决定画在哪个图层（outline 粗实线 / thin 细实线 / center 中心线 / hidden 虚线 / dim 标注 / text 文字）。"""
from __future__ import annotations

import math

from zhihui.cad.geom.errors import GeomError


def line(a, b, role="outline", tags=None) -> dict:
    return {"kind": "line", "role": role, "tags": tags or [], "data": {"p1": [a[0], a[1], 0.0], "p2": [b[0], b[1], 0.0]}}


def poly(pts, closed=False, role="outline", tags=None) -> dict:
    return {"kind": "polyline", "role": role, "tags": tags or [], "data": {"vertices": [{"p": [q[0], q[1], 0.0], "bulge": 0.0, "sw": 0.0, "ew": 0.0} for q in pts], "closed": closed, "width": 0.0}}


def circle(c, r, role="outline", tags=None) -> dict:
    return {"kind": "circle", "role": role, "tags": tags or [], "data": {"c": [c[0], c[1], 0.0], "r": float(r)}}


def arc(c, r, a0, a1, role="outline", tags=None) -> dict:
    return {"kind": "arc", "role": role, "tags": tags or [], "data": {"c": [c[0], c[1], 0.0], "r": float(r), "a0": a0 % 360.0, "a1": a1 % 360.0}}


def text(p, s, h, role="text", tags=None, rotation=0.0, align="left") -> dict:
    return {"kind": "text", "role": role, "tags": tags or [], "data": {"p": [p[0], p[1], 0.0], "text": s, "height": h, "rotation": rotation, "style": "", "align": align}}


def hexagon(c, across_flats: float, rot_deg: float = 0.0, role="outline", tags=None) -> dict:
    R = across_flats / math.sqrt(3)                                   # 外接圆半径
    return poly([(c[0] + R * math.cos(math.radians(rot_deg + 60 * i)), c[1] + R * math.sin(math.radians(rot_deg + 60 * i))) for i in range(6)], True, role, tags)


def rect(x0, y0, x1, y1, role="outline", tags=None) -> dict:
    return poly([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], True, role, tags)


def thread_arc(c, r, role="thin", tags=None) -> dict:
    """螺纹的 3/4 圈细实线（GB/T 4459.1：端视图约 3/4 圈，缺口在右上角）。"""
    return arc(c, r, 0.0 + 90.0, 360.0, role, tags)


def need(p: dict, *keys: str, hint: str = "") -> list:
    """必填参数：缺了就让上层反问用户（不替用户编数值）。"""
    miss = [k for k in keys if p.get(k) in (None, "", [])]
    if miss:
        raise GeomError("MISSING_ARG", f"缺少参数：{'、'.join(miss)}", hint or "这些尺寸必须由用户给出，请向用户确认")
    return [p[k] for k in keys]


def positive(name: str, v) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise GeomError("INVALID_ARGS", f"{name} 必须是数字", "")
    if f <= 0:
        raise GeomError("INVALID_ARGS", f"{name} 必须大于 0", "")
    return f


def text_width(s: str, h: float) -> float:
    """估算文字宽度：汉字（全角）约 1.4×字高，西文数字约 0.65×字高（ezdxf 按字形渲染，字高是大写字母高度）。"""
    return sum(h * (1.4 if ord(ch) > 255 else 0.65) for ch in s)


def fit_h(s: str, width: float, h: float, pad: float = 2.0, min_h: float = 1.6) -> float:
    """让文字装进宽度 width 的格子：字高不超过 h，放不下就缩小，最小 min_h。"""
    w = text_width(s, h)
    if w <= width - pad:
        return h
    return max(min_h, h * (width - pad) / w)


def shift(ops: list[dict], dx: float, dy: float) -> list[dict]:
    from zhihui.cad.geom.entities import Transform, transform_data
    T = Transform.translation(dx, dy)
    return [{**o, "data": transform_data(o["kind"], o["data"], T)} for o in ops]


def result(ops: list[dict], summary: str, **info) -> dict:
    return {"ops": ops, "summary": summary, "info": info}
