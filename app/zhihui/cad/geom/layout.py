"""布局（图纸空间）几何：纸张尺寸、标准比例、视口比例换算、图框和标题栏图元。"""
from __future__ import annotations

import json
import math
from pathlib import Path

from .entities import P
from .errors import GeomError

# ISO 图幅（宽×高 mm，横向）
ISO = {"ISO_A0": (1189.0, 841.0), "ISO_A1": (841.0, 594.0), "ISO_A2": (594.0, 420.0), "ISO_A3": (420.0, 297.0), "ISO_A4": (297.0, 210.0)}

# GB/T 14690：优先比例 + 第二系列
STANDARD_SCALES = ["1:1", "1:2", "1:5", "1:10", "1:20", "1:50", "1:100", "1:200", "1:500", "1:1000", "1:2000", "1:5000", "1:10000",
                   "2:1", "5:1", "10:1", "20:1", "50:1"]
SECOND_SCALES = ["1:1.5", "1:2.5", "1:3", "1:4", "1:6", "1:15", "1:25", "1:30", "1:40", "1:60", "2.5:1", "4:1"]


def paper_size(name: str, orientation: str = "landscape") -> tuple[float, float]:
    if name not in ISO:
        raise GeomError("INVALID_ARGS", f"没有图幅 {name}", f"可选：{sorted(ISO)}")
    w, h = ISO[name]
    return (w, h) if orientation == "landscape" else (h, w)


def scale_ratio(text: str) -> float:
    """"1:50" → 0.02（图纸 1 mm 对应模型 50 mm）；"2:1" → 2。"""
    try:
        a, b = text.replace("：", ":").split(":")
        r = float(a) / float(b)
    except Exception:
        raise GeomError("INVALID_ARGS", f"比例写法不对：{text}", '写成 "1:50" 这样')
    if r <= 0:
        raise GeomError("INVALID_ARGS", f"比例必须为正：{text}", "")
    return r


def is_standard_scale(text: str) -> bool:
    return text in STANDARD_SCALES or text in SECOND_SCALES


def ratio_to_text(r: float) -> str:
    for s in STANDARD_SCALES + SECOND_SCALES:
        if abs(scale_ratio(s) - r) < 1e-9 * max(1.0, r):
            return s
    return f"1:{1 / r:g}" if r < 1 else f"{r:g}:1"


def view_height_for(vp_h_mm: float, scale_text: str) -> float:
    return vp_h_mm / scale_ratio(scale_text)


def fit_scale(vp_w: float, vp_h: float, bbox: tuple[float, float, float, float], margin: float = 0.9, series: str = "priority") -> str:
    """能把 bbox 装进视口的最大标准比例（找不到就取最小的标准比例）。series="all" 时把 GB/T 14690 第二系列（1:15、1:25、1:30…）也算进来。"""
    w, h = max(bbox[2] - bbox[0], 1e-9), max(bbox[3] - bbox[1], 1e-9)
    best = None
    for s in STANDARD_SCALES + (SECOND_SCALES if series == "all" else []):
        r = scale_ratio(s)
        if w * r <= vp_w * margin and h * r <= vp_h * margin:
            if best is None or r > scale_ratio(best):
                best = s
    return best or STANDARD_SCALES[STANDARD_SCALES.index("1:10000")]


def viewport_scale(vp: dict) -> str:
    """视口当前比例：视口高度 / view_height。"""
    (x0, y0), (x1, y1) = vp["corners"]
    r = abs(y1 - y0) / max(float(vp["view_height"]), 1e-12)
    return ratio_to_text(r)


# ------------------------------------------------------------------ 图框与标题栏
def frame_ops(size: tuple[float, float], binding: bool = True) -> list[dict]:
    """图框：纸边细线 + 内框粗线（装订边 25，其余 10，GB/T 14689）。返回 [{kind,data,role}]。"""
    W, H = size
    a, c = (25.0, 10.0) if binding else (10.0, 10.0)
    outer = {"kind": "polyline", "role": "sheet_edge", "data": {"vertices": [{"p": [x, y, 0.0], "bulge": 0.0, "sw": 0.0, "ew": 0.0} for x, y in ((0, 0), (W, 0), (W, H), (0, H))], "closed": True, "width": 0.0}}
    inner = {"kind": "polyline", "role": "frame", "data": {"vertices": [{"p": [x, y, 0.0], "bulge": 0.0, "sw": 0.0, "ew": 0.0} for x, y in ((a, c), (W - c, c), (W - c, H - c), (a, H - c))], "closed": True, "width": 0.0}}
    return [outer, inner]


def inner_frame(size: tuple[float, float], binding: bool = True) -> tuple[float, float, float, float]:
    W, H = size
    a, c = (25.0, 10.0) if binding else (10.0, 10.0)
    return a, c, W - c, H - c


def _fit(s: str, w: float, h: float, pad: float = 2.0, cjk: float = 1.4, floor: float = 1.6) -> float:
    tw = sum(h * (cjk if ord(c) > 255 else 0.65) for c in s)
    return h if tw <= w - pad else max(floor, h * (w - pad) / tw)


_fit_base = _fit


def _tpl(template: str = "gb10609") -> dict:
    return json.loads((Path(__file__).resolve().parent.parent / "titleblocks" / f"{template}.json").read_text(encoding="utf-8"))


def title_block_height(template: str = "gb10609") -> float:
    return float(_tpl(template)["size"][1])


def title_block_ops(size: tuple[float, float], fields: dict[str, str], binding: bool = True, template: str = "gb10609") -> tuple[list[dict], list[str]]:
    """标题栏（默认 GB/T 10609.1 简化版 180×40；template="hg_equipment" 为化工设备图 180×56），放在内框右下角 → 线和文字图元。
    字段值只来自用户或程序（图名、比例）；没给的格子写占位说明，不替用户填。返回 (ops, 缺少的字段名)。"""
    tpl = _tpl(template)
    tw, th = tpl["size"]
    # 模板可以给：label_h 签字区角色字高、placeholder_ratio 占位说明字高比例、min_h 最小字高、cjk_advance 汉字字宽估算（宽度因子 0.7 的长仿宋取 1.05）
    lab_h, ph_ratio, floor, cjk = float(tpl.get("label_h", 2.5)), float(tpl.get("placeholder_ratio", 0.8)), float(tpl.get("min_h", 1.6)), float(tpl.get("cjk_advance", 1.4))

    def _fit(s, w, h, pad=2.0):
        return _fit_base(s, w, h, pad, cjk, floor)

    x1, y0, x2, _ = inner_frame(size, binding)
    ox, oy = x2 - tw, y0                                           # 标题栏左下角
    ops: list[dict] = []
    missing: list[str] = []

    def rect(x0, y0_, x1_, y1_, role):
        ops.append({"kind": "polyline", "role": role, "data": {"vertices": [{"p": [ox + x, oy + y, 0.0], "bulge": 0.0, "sw": 0.0, "ew": 0.0} for x, y in ((x0, y0_), (x1_, y0_), (x1_, y1_), (x0, y1_))], "closed": True, "width": 0.0}})

    def text(x, y, s, h, role):
        ops.append({"kind": "text", "role": role, "data": {"p": [ox + x, oy + y, 0.0], "text": s, "height": h, "rotation": 0.0, "style": "", "align": "left"}})

    rect(0, 0, tw, th, "title_block_outer")
    sa = tpl.get("sign_area")
    if sa:
        xs, (ya, yb) = sa["x"], sa["y"]
        n = len(sa["roles"])
        rh = (yb - ya) / n
        for i, role in enumerate(sa["roles"]):
            y = yb - (i + 1) * rh
            rect(xs[0], y, xs[-1], y + rh, "title_block_cell")
            hh = _fit(role, xs[1] - xs[0], lab_h)
            text(xs[0] + 1.0, y + rh / 2 - hh / 2, role, hh, "title_block_label")
        for x in xs[1:-1]:
            ops.append({"kind": "line", "role": "title_block_cell", "data": {"p1": [ox + x, oy + ya, 0.0], "p2": [ox + x, oy + yb, 0.0]}})
    for c in tpl["cells"]:
        (cx0, cx1), (cy0, cy1) = c["x"], c["y"]
        rect(cx0, cy0, cx1, cy1, "title_block_cell")
        h = min(3.5, (cy1 - cy0) * 0.45)
        if "text" in c:
            h = _fit(c["text"], cx1 - cx0, h)
            text(cx0 + 1.0, cy0 + (cy1 - cy0) / 2 - h / 2, c["text"], h, c.get("role", "title_block_label"))
            continue
        f = c.get("field")
        v = fields.get(f)
        if not v and c.get("default") and f != "drawing_name":
            v = None
        if v:
            hv = _fit(str(v), cx1 - cx0, h)
            text(cx0 + 1.0, cy0 + (cy1 - cy0) / 2 - hv / 2, str(v), hv, "title_block_value")
        else:
            if f and f in tpl.get("required", []):
                missing.append(f)
            ph = c.get("placeholder", "")
            hp = _fit(ph, cx1 - cx0, h * ph_ratio)
            text(cx0 + 1.0, cy0 + (cy1 - cy0) / 2 - hp / 2, ph, hp, "title_block_placeholder")
    return ops, missing


def default_viewport_corners(size: tuple[float, float], binding: bool = True, with_title_block: bool = True, template: str = "gb10609") -> tuple[tuple[float, float], tuple[float, float]]:
    """默认视口：内框内，避开右下角标题栏所占的高度（高度读标题栏模板）。"""
    x0, y0, x1, y1 = inner_frame(size, binding)
    tb_h = title_block_height(template) if with_title_block else 0.0
    return (x0 + 3, y0 + tb_h + 3), (x1 - 3, y1 - 3)
