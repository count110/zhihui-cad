"""机械制图几何：ISO 286 公差带偏差查表、形位公差框格、基准符号、表面粗糙度符号（GB/T 131）。

偏差只从 codes/iso286.json 的标准表算出；表里没有的（公称尺寸 >500、公差带不在覆盖范围）直接报 DEVIATION_UNKNOWN，
由调用方反问用户给出偏差，绝不编造。生成的图形都是 ops：[{kind, data, tags}]，由工具函数落成图元。
"""
from __future__ import annotations

import json
import math
import re
from functools import lru_cache
from pathlib import Path

from .entities import P, polar
from .errors import GeomError

CODES = Path(__file__).resolve().parent.parent / "codes"


@lru_cache(maxsize=1)
def iso286() -> dict:
    return json.loads((CODES / "iso286.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def mech_std() -> dict:
    return json.loads((CODES / "mech_std.json").read_text(encoding="utf-8"))


# ------------------------------------------------------------------ ISO 286
def _range_index(size: float) -> int:
    rs = iso286()["ranges"]
    if not 0 < size <= rs[-1][1]:
        raise GeomError("DEVIATION_UNKNOWN", f"公称尺寸 {size:g} 超出本系统偏差表范围（0–{rs[-1][1]} mm）", "请用户直接给出上、下偏差")
    for i, (lo, hi) in enumerate(rs):
        if size <= hi:
            return i
    raise AssertionError


def _IT(grade: int, i: int) -> float:
    row = iso286()["IT"].get(str(grade))
    if row is None:
        raise GeomError("DEVIATION_UNKNOWN", f"本系统没有 IT{grade} 的标准公差值", "请用户直接给出上、下偏差")
    return float(row[i])


def parse_code(code: str) -> tuple[str, str, int]:
    """"H7" → ("hole","H",7)；"g6" → ("shaft","g",6)；"js6"/"JS6" 按大小写分孔轴。"""
    m = re.fullmatch(r"\s*([A-Za-z]{1,2})\s*(\d{1,2})\s*", code or "")
    if not m:
        raise GeomError("INVALID_ARGS", f"公差带代号写法不对：{code}", "写成 H7、g6、H7/g6")
    letter, grade = m.group(1), int(m.group(2))
    return ("hole" if letter[0].isupper() else "shaft"), letter, grade


def deviation(nominal: float, code: str) -> dict:
    """单个公差带 → {upper_um, lower_um, ...}（μm 和 mm 都给）。查不到抛 DEVIATION_UNKNOWN。"""
    kind, letter, g = parse_code(code)
    data = iso286()
    allowed = data["allowed"][kind]
    key = letter if kind == "hole" else letter.lower()
    if key not in allowed or g not in allowed[key]:
        raise GeomError("DEVIATION_UNKNOWN", f"公差带 {code} 不在本系统偏差表覆盖范围内",
                        f"覆盖：孔 {', '.join(f'{k}{min(v)}–{max(v)}' for k, v in data['allowed']['hole'].items())}；轴 {', '.join(f'{k}{min(v)}–{max(v)}' for k, v in data['allowed']['shaft'].items())}。请用户直接给出上、下偏差")
    i = _range_index(nominal)
    it = _IT(g, i)
    low = key.lower()
    if low == "h":
        up, lo = (it, 0.0) if kind == "hole" else (0.0, -it)
    elif low == "js":
        half = (it - 1) / 2 if (g >= 7 and int(it) % 2 == 1) else it / 2
        up, lo = half, -half
    else:
        fd = data["shaft_fundamental"][low]["values"][i]
        fk = data["shaft_fundamental"][low]["kind"]
        if kind == "shaft":
            up, lo = (fd, fd - it) if fk == "es" else (fd + it, fd)
        else:                                              # 孔：由同字母轴的基本偏差换算
            if low in ("f", "g"):
                lo = -fd                                   # EI = −es
                up = lo + it
            else:                                          # K M N P：ES = −ei + Δ（IT≤8）
                delta = it - _IT(g - 1, i)
                up = -fd + delta
                lo = up - it
    return {"code": code.strip(), "kind": kind, "grade": g, "upper_um": up, "lower_um": lo, "upper_mm": round(up / 1000, 6), "lower_mm": round(lo / 1000, 6)}


def fit(nominal: float, code: str) -> list[dict]:
    """"H7" 或 "H7/g6" → 偏差列表（配合时孔在前）。"""
    parts = [p for p in re.split(r"[/／]", code) if p.strip()]
    if not 1 <= len(parts) <= 2:
        raise GeomError("INVALID_ARGS", f"配合代号写法不对：{code}", "写成 H7 或 H7/g6")
    out = [deviation(nominal, p) for p in parts]
    out.sort(key=lambda x: 0 if x["kind"] == "hole" else 1)
    return out


def fmt_dev3(v: float) -> str:
    return "0" if abs(v) < 1e-12 else f"{v:+.3f}"


# ------------------------------------------------------------------ 图形构件
def _line(a, b, tags=None) -> dict:
    return {"kind": "line", "data": {"p1": [a[0], a[1], 0.0], "p2": [b[0], b[1], 0.0]}, "tags": tags or []}


def _poly(pts, closed=False, tags=None) -> dict:
    return {"kind": "polyline", "data": {"vertices": [{"p": [q[0], q[1], 0.0], "bulge": 0.0, "sw": 0.0, "ew": 0.0} for q in pts], "closed": closed, "width": 0.0}, "tags": tags or []}


def _circle(c, r, tags=None) -> dict:
    return {"kind": "circle", "data": {"c": [c[0], c[1], 0.0], "r": r}, "tags": tags or []}


def _text(p, s, h, align="left", tags=None) -> dict:
    return {"kind": "text", "data": {"p": [p[0], p[1], 0.0], "text": s, "height": h, "rotation": 0.0, "style": "", "align": align}, "tags": tags or []}


def _solid_tri(pts, tags=None) -> dict:
    return {"kind": "hatch", "data": {"pattern": "SOLID", "scale": 1.0, "angle": 0.0, "loops": [[[x, y, 0.0] for x, y in pts]], "island_style": "normal", "associative": False, "source_ids": [], "gradient": None}, "tags": tags or []}


def _shift(ops: list[dict], dx: float, dy: float) -> list[dict]:
    from .entities import Transform, transform_data
    T = Transform.translation(dx, dy)
    return [{**o, "data": transform_data(o["kind"], o["data"], T)} for o in ops]


GDT_SYMBOLS = ("straightness", "flatness", "circularity", "cylindricity", "parallelism", "perpendicularity", "angularity", "position", "concentricity", "symmetry", "circular_runout", "total_runout")
GDT_NAMES = {"straightness": "直线度", "flatness": "平面度", "circularity": "圆度", "cylindricity": "圆柱度", "parallelism": "平行度", "perpendicularity": "垂直度", "angularity": "倾斜度",
             "position": "位置度", "concentricity": "同轴度", "symmetry": "对称度", "circular_runout": "圆跳动", "total_runout": "全跳动"}
FORM_ONLY = ("straightness", "flatness", "circularity", "cylindricity")        # 形状公差：没有基准
NEEDS_DATUM = ("parallelism", "perpendicularity", "angularity", "concentricity", "symmetry", "circular_runout", "total_runout")


def gdt_glyph(symbol: str, cx: float, cy: float, k: float) -> list[dict]:
    """形位公差符号（按 7×7 的格子居中，k = 字高/3.5）。"""
    T = ["gdt_symbol"]
    pt = lambda x, y: (cx + x * k, cy + y * k)
    if symbol == "straightness":
        return [_line(pt(-2.4, 0), pt(2.4, 0), T)]
    if symbol == "flatness":
        return [_poly([pt(-2.4, -1.0), pt(1.4, -1.0), pt(2.4, 1.0), pt(-1.4, 1.0)], True, T)]
    if symbol == "circularity":
        return [_circle(pt(0, 0), 1.9 * k, T)]
    if symbol == "cylindricity":
        d = (math.cos(math.radians(60)), math.sin(math.radians(60)))
        n = (-d[1], d[0])
        r = 1.5 * k
        out = [_circle(pt(0, 0), r, T)]
        for s in (1, -1):
            c = (cx + s * r * n[0], cy + s * r * n[1])
            out.append(_line((c[0] - 2.6 * k * d[0], c[1] - 2.6 * k * d[1]), (c[0] + 2.6 * k * d[0], c[1] + 2.6 * k * d[1]), T))
        return out
    if symbol == "parallelism":
        return [_line(pt(-2.2, -2.0), pt(0.2, 2.0), T), _line(pt(-0.2, -2.0), pt(2.2, 2.0), T)]
    if symbol == "perpendicularity":
        return [_line(pt(-2.4, -2.0), pt(2.4, -2.0), T), _line(pt(0, -2.0), pt(0, 2.4), T)]
    if symbol == "angularity":
        return [_line(pt(-2.4, -2.0), pt(2.4, -2.0), T), _line(pt(-2.4, -2.0), pt(1.2, 2.2), T)]
    if symbol == "position":
        return [_circle(pt(0, 0), 1.7 * k, T), _line(pt(-3.0, 0), pt(3.0, 0), T), _line(pt(0, -3.0), pt(0, 3.0), T)]
    if symbol == "concentricity":
        return [_circle(pt(0, 0), 1.0 * k, T), _circle(pt(0, 0), 2.0 * k, T)]
    if symbol == "symmetry":
        return [_line(pt(-2.4, 1.2), pt(2.4, 1.2), T), _line(pt(-3.0, 0), pt(3.0, 0), T), _line(pt(-2.4, -1.2), pt(2.4, -1.2), T)]
    if symbol in ("circular_runout", "total_runout"):
        def arrow(dx):
            a, b = pt(-2.4 + dx, -2.0), pt(1.6 + dx, 2.0)
            u = (b[0] - a[0], b[1] - a[1])
            L = math.hypot(*u)
            u = (u[0] / L, u[1] / L)
            n = (-u[1], u[0])
            return [_line(a, b, T), _line(b, (b[0] - 1.4 * k * u[0] + 0.7 * k * n[0], b[1] - 1.4 * k * u[1] + 0.7 * k * n[1]), T), _line(b, (b[0] - 1.4 * k * u[0] - 0.7 * k * n[0], b[1] - 1.4 * k * u[1] - 0.7 * k * n[1]), T)]
        return arrow(-0.4) if symbol == "circular_runout" else arrow(-1.0) + arrow(1.0)
    raise GeomError("INVALID_ARGS", f"没有形位公差符号 {symbol}", f"可选：{list(GDT_SYMBOLS)}")


def text_w(s: str, h: float) -> float:
    return sum(h * (1.4 if ord(c) > 255 else 0.65) for c in s)


def gdt_frame_ops(origin, symbol: str, value: str, diameter_zone: bool, datums: list[str], h: float = 3.5) -> tuple[list[dict], tuple[float, float, float, float]]:
    """形位公差框格（GB/T 1182）：框高 2h；格子依次是 符号 | ⌀公差值 | 基准 A | B | C。origin = 框格左下角。返回 (ops, 框格 bbox)。"""
    H = 2 * h
    k = h / 3.5
    x0, y0 = origin[0], origin[1]
    ops: list[dict] = []
    cells = [(H, None)]
    vt = ("⌀" if diameter_zone else "") + value
    cells.append((max(H, text_w(vt, h) + h), vt))
    for dt in datums:
        cells.append((H, dt))
    x = x0
    for i, (w, txt) in enumerate(cells):
        ops.append(_poly([(x, y0), (x + w, y0), (x + w, y0 + H), (x, y0 + H)], True, ["gdt_cell"]))
        if i == 0:
            ops += gdt_glyph(symbol, x + w / 2, y0 + H / 2, k)
        else:
            ops.append(_text((x + w / 2 - text_w(txt, h) / 2, y0 + H / 2 - h / 2), txt, h, tags=["gdt_text"]))
        x += w
    return ops, (x0, y0, x, y0 + H)


def datum_symbol_ops(p, letter: str, h: float = 3.5, up: float = 6.0) -> list[dict]:
    """基准符号：涂黑三角（底边在被指表面 p 处）+ 连线 + 方框内的字母。"""
    H = 2 * h
    s = 5.0 * h / 3.5
    tri = [(p[0] - s / 2, p[1]), (p[0] + s / 2, p[1]), (p[0], p[1] + s * 0.87)]
    top = p[1] + s * 0.87
    ops = [_solid_tri(tri, ["datum"]), _line((p[0], top), (p[0], top + up), ["datum"])]
    by = top + up
    ops.append(_poly([(p[0] - H / 2, by), (p[0] + H / 2, by), (p[0] + H / 2, by + H), (p[0] - H / 2, by + H)], True, ["datum"]))
    ops.append(_text((p[0] - text_w(letter, h) / 2, by + H / 2 - h / 2), letter, h, tags=["datum"]))
    return ops


def gdt_leader_ops(frame_bbox, target, h: float = 3.5) -> list[dict]:
    """指引线：从框格左边中点折到被测要素 target，箭头在 target。"""
    x0, y0, x1, y1 = frame_bbox
    a = (x0, (y0 + y1) / 2)
    mid = (a[0] - 6.0, a[1]) if target[0] < a[0] else (a[0], y1 + 5.0)
    ops = [_poly([a, mid, (target[0], target[1])], False, ["gdt_leader"])]
    u = (target[0] - mid[0], target[1] - mid[1])
    L = math.hypot(*u) or 1.0
    u = (u[0] / L, u[1] / L)
    n = (-u[1], u[0])
    s = 3.0 * h / 3.5
    base = (target[0] - u[0] * s, target[1] - u[1] * s)
    ops.append(_solid_tri([(target[0], target[1]), (base[0] + n[0] * s * 0.18, base[1] + n[1] * s * 0.18), (base[0] - n[0] * s * 0.18, base[1] - n[1] * s * 0.18)], ["gdt_leader"]))
    return ops


def roughness_ops(p, param: str, value: str, process: str, h: float = 3.5) -> list[dict]:
    """表面粗糙度符号（GB/T 131 简化）：p = 符号尖点（落在被指表面上）。"""
    H1 = 1.4 * h
    H2 = 2.8 * h
    t60 = math.tan(math.radians(60))
    left = (p[0] - H1 / t60, p[1] + H1)
    right = (p[0] + H2 / t60, p[1] + H2)
    txt = f"{param} {value}" if value else ""
    bar_end = (right[0] + max(text_w(txt, h), 6.0) + 1.5, right[1])
    T = ["roughness"]
    ops: list[dict] = []
    if process == "remove":                                   # 去除材料：封闭的三角形
        ops.append(_poly([p, left, (p[0] + H1 / t60, left[1])], True, T))
        ops.append(_line((p[0] + H1 / t60, left[1]), right, T))
    else:
        ops.append(_poly([left, p, right], False, T))
    ops.append(_line(right, bar_end, T))
    if process == "no_remove":
        ops.append(_circle((p[0], p[1] + H1 * 0.62), H1 * 0.32, T))
    if txt:
        ops.append(_text((right[0] + 1.0, right[1] + 0.8), txt, h, tags=T))
    return ops


def nice_roughness(param: str, value: float) -> tuple[bool, list[float]]:
    series = mech_std()["roughness_series"][param]
    return any(abs(value - s) < 1e-9 for s in series), series


def keyway(d: float) -> dict:
    for row in mech_std()["keyway"]:
        lo, hi = row["d"]
        if lo < d <= hi:
            return row
    raise GeomError("KEYWAY_UNKNOWN", f"轴径 {d:g} 不在键槽表范围（>6–110 mm）内", "请用户直接给出键宽 key_width、键槽深 t1")
