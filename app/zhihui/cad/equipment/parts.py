"""部件几何（任务书 5.1 parts.py）：每个函数返回某个部件在某个视图里的 ops（toolsets/common.py 的格式，带 role）。

主视图的部件一律在“局部立式坐标”里画：x = 到设备轴线的径向距离（带正负号，表示转到哪一侧），y = 从尺寸基准（下/左封头环焊缝）量起的轴向距离。
立式设备局部坐标就是模型坐标；卧式设备由 views.py 关于 y=x 镜像（x↔y）后放进模型空间（transform_data 会正确处理圆弧和椭圆的方向）。
壁厚夸大：图上壁厚 tw = max(δ, 比例分母 × 1 mm)，只改图形不改参数；尺寸由 dims.py 按参数值标注。
"""
from __future__ import annotations

import math

from zhihui.cad.toolsets import common as c

from .profile import HeadProfile

N_SAMPLE = 48


def _hatch(loops: list[list[tuple[float, float]]], tags=None) -> dict:
    return {"kind": "hatch", "role": "hatch", "tags": tags or [], "data": {"pattern": "SOLID", "scale": 1.0, "angle": 0.0, "loops": [[[p[0], p[1], 0.0] for p in lp] for lp in loops],
                                                                           "island_style": "normal", "associative": False, "source_ids": []}}


def _ellipse(cx, cy, a, b, t0, t1, role="outline") -> dict:
    """major 沿 x（a ≥ b）或沿 y（a < b）的椭圆弧。参数角 t 按 ezdxf 定义（逆时针）。"""
    if a >= b:
        return {"kind": "ellipse", "role": role, "tags": [], "data": {"c": [cx, cy, 0.0], "major": [a, 0.0, 0.0], "ratio": b / a, "t0": t0 % 360.0, "t1": t1 % 360.0 if t1 % 360.0 else 360.0}}
    return {"kind": "ellipse", "role": role, "tags": [], "data": {"c": [cx, cy, 0.0], "major": [0.0, b, 0.0], "ratio": a / b, "t0": (t0 - 90) % 360.0, "t1": (t1 - 90) % 360.0 or 360.0}}


# ------------------------------------------------------------------ 筒体
def shell_profile(ri: float, tw: float, length: float, openings: list[tuple[float, float, float]]) -> list[dict]:
    """筒体（全剖）：两侧壁的内外轮廓线 + 涂黑的剖面（开孔处断开）+ 两条环焊缝线。openings = [(侧 ±1, y0, y1)]。"""
    ops: list[dict] = []
    for side in (1.0, -1.0):
        cuts = sorted((max(0.0, a), min(length, b)) for s, a, b in openings if s == side and b > 0 and a < length)
        segs, y = [], 0.0
        for a, b in cuts:
            if a > y:
                segs.append((y, a))
            y = max(y, b)
        if y < length:
            segs.append((y, length))
        for y0, y1 in segs:
            ops.append(c.line((side * ri, y0), (side * ri, y1), "outline"))
            ops.append(c.line((side * (ri + tw), y0), (side * (ri + tw), y1), "outline"))
            ops.append(_hatch([[(side * ri, y0), (side * (ri + tw), y0), (side * (ri + tw), y1), (side * ri, y1)]]))
        for a, b in cuts:                                       # 开孔两侧的壁厚断面线
            for yy in (a, b):
                if 0 < yy < length:
                    ops.append(c.line((side * ri, yy), (side * (ri + tw), yy), "outline"))
        for yy in (0.0, length):                                  # 环焊缝
            ops.append(c.line((side * ri, yy), (side * (ri + tw), yy), "thin", ["seam"]))
    return ops


# ------------------------------------------------------------------ 封头
def head_profile(pr: HeadProfile, tw: float, y_seam: float, up: bool) -> list[dict]:
    """封头（全剖）：直边段 + 曲面段的内外轮廓 + 涂黑剖面。y_seam 为环焊缝；up=True 向 +y 方向（上/右封头）。pr 为真实尺寸，tw 为图上壁厚。"""
    sg = 1.0 if up else -1.0
    a, hsf = pr.a, pr.hsf
    yt = y_seam + sg * hsf                                       # 切线
    draw = HeadProfile(pr.kind, pr.a, tw, pr.hsf, pr.hc, pr.rc, pr.rk)
    ops: list[dict] = []
    if hsf > 0:
        for side in (1.0, -1.0):
            ops.append(c.line((side * a, y_seam), (side * a, yt), "outline"))
            ops.append(c.line((side * (a + tw), y_seam), (side * (a + tw), yt), "outline"))
    k = pr.kind
    if k == "EHA":
        t0, t1 = (0.0, 180.0) if up else (180.0, 360.0)
        ops.append(_ellipse(0.0, yt, a, pr.hc, t0, t1))
        ops.append(_ellipse(0.0, yt, a + tw, pr.hc + tw, t0, t1))
    elif k == "HHA":
        a0, a1 = (0.0, 180.0) if up else (180.0, 360.0)
        ops.append(c.arc((0.0, yt), a, a0, a1))
        ops.append(c.arc((0.0, yt), a + tw, a0, a1))
    elif k == "THA":
        yc = draw._yc()
        for rr, kk in ((pr.rc, pr.rk), (pr.rc + tw, pr.rk + tw)):
            phi = math.degrees(math.atan2(-yc, a - pr.rk))
            if up:
                ops.append(c.arc((0.0, yt + yc), rr, phi, 180.0 - phi))
                ops.append(c.arc((a - pr.rk, yt), kk, 0.0, phi))
                ops.append(c.arc((-(a - pr.rk), yt), kk, 180.0 - phi, 180.0))
            else:
                ops.append(c.arc((0.0, yt - yc), rr, 180.0 + phi, 360.0 - phi))
                ops.append(c.arc((a - pr.rk, yt), kk, 360.0 - phi, 0.0))
                ops.append(c.arc((-(a - pr.rk), yt), kk, 180.0, 180.0 + phi))
    elif k == "SDH":
        yc = -math.sqrt(max(0.0, pr.rc ** 2 - a * a))
        for rr in (pr.rc, pr.rc + tw):
            xe = min(rr, a if rr == pr.rc else a + tw)
            phi = math.degrees(math.acos(max(-1.0, min(1.0, xe / rr))))
            if up:
                ops.append(c.arc((0.0, yt + yc), rr, phi, 180.0 - phi))
            else:
                ops.append(c.arc((0.0, yt - yc), rr, 180.0 + phi, 360.0 - phi))
        for side in (1.0, -1.0):
            ops.append(c.line((side * (a + tw), yt), (side * (a + tw), yt + sg * draw.outer_y(a + tw - 1e-9)), "outline"))
    elif k == "FLAT":
        ops.append(c.line((-(a + tw), yt + sg * tw), (a + tw, yt + sg * tw), "outline"))
        ops.append(c.line((-a, yt), (a, yt), "outline"))
        for side in (1.0, -1.0):
            ops.append(c.line((side * (a + tw), yt), (side * (a + tw), yt + sg * tw), "outline"))
    # 剖面：直边段 + 曲面段之间的环带（按参数角取样）
    outer = [(x, yt + sg * draw.outer_y(x)) for x in _xs(a + tw)]
    inner = [(x, yt + sg * draw.inner_y(x)) for x in _xs(a)]
    loop = [(a, y_seam), (a + tw, y_seam)] + outer + [(-(a + tw), y_seam), (-a, y_seam)] + list(reversed(inner))
    ops.append(_hatch([_dedupe(loop)]))
    return ops


def _xs(r: float) -> list[float]:
    return [r * math.cos(math.pi * i / N_SAMPLE) for i in range(N_SAMPLE + 1)]


def _dedupe(pts):
    out = []
    for p in pts:
        if not out or math.dist(out[-1], p) > 1e-6:
            out.append(p)
    return out


# ------------------------------------------------------------------ 管口与法兰
def flange_simplified(x_face: float, y: float, d: float, cthk: float, direction: float, horizontal_axis: bool = True) -> list[dict]:
    """法兰简化画法：外形矩形（不画螺栓）。horizontal_axis=True：管口轴线沿 x（筒体管口），法兰面在 x_face，朝 direction 方向。"""
    if horizontal_axis:
        x0, x1 = sorted((x_face, x_face - direction * cthk))
        return [c.rect(x0, y - d / 2, x1, y + d / 2, "outline", ["flange"])]
    y0, y1 = sorted((y, y - direction * cthk))
    return [c.rect(x_face - d / 2, y0, x_face + d / 2, y1, "outline", ["flange"])]


def nozzle_side_view(side: float, axial: float, x_start: float, x_face: float, od: float, thk: float, d: float, cthk: float, extra_cover: float = 0.0) -> list[dict]:
    """筒体管口（主视图，旋转到投影面）：接管外壁两条线 + 内壁细线 + 法兰 + 轴线；extra_cover > 0 时再画人孔盖。x 为带符号的径向坐标。"""
    ops: list[dict] = []
    x_back = x_face - side * cthk
    for s in (1.0, -1.0):
        ops.append(c.line((x_start, axial + s * od / 2), (x_back, axial + s * od / 2), "outline", ["neck"]))
        ops.append(c.line((x_start, axial + s * (od / 2 - thk)), (x_back, axial + s * (od / 2 - thk)), "thin", ["bore"]))
    ops += flange_simplified(x_face, axial, d, cthk, side)
    if extra_cover > 0:
        x0, x1 = sorted((x_face, x_face + side * extra_cover))
        ops.append(c.rect(x0, axial - d / 2, x1, axial + d / 2, "outline", ["cover"]))
    end = x_face + side * (extra_cover + 0.3 * d)
    ops.append(c.line((x_start - side * 0.0, axial), (end, axial), "center", ["axis"]))
    return ops


def nozzle_head_view(x: float, y_surface_l: float, y_surface_r: float, y_face: float, direction: float, od: float, thk: float, d: float, cthk: float, extra_cover: float = 0.0) -> list[dict]:
    """封头管口（主视图）：竖直接管从封头外表面到法兰背面；direction = +1 向上（上封头）/ -1 向下。"""
    ops: list[dict] = []
    y_back = y_face - direction * cthk
    ops.append(c.line((x - od / 2, y_surface_l), (x - od / 2, y_back), "outline", ["neck"]))
    ops.append(c.line((x + od / 2, y_surface_r), (x + od / 2, y_back), "outline", ["neck"]))
    for s in (1.0, -1.0):
        ops.append(c.line((x + s * (od / 2 - thk), min(y_surface_l, y_surface_r) if direction > 0 else max(y_surface_l, y_surface_r)), (x + s * (od / 2 - thk), y_back), "thin", ["bore"]))
    ops += flange_simplified(x, y_face, d, cthk, direction, horizontal_axis=False)
    if extra_cover > 0:
        y0, y1 = sorted((y_face, y_face + direction * extra_cover))
        ops.append(c.rect(x - d / 2, y0, x + d / 2, y1, "outline", ["cover"]))
    ops.append(c.line((x, (y_surface_l + y_surface_r) / 2 - direction * 0.2 * d), (x, y_face + direction * (extra_cover + 0.3 * d)), "center", ["axis"]))
    return ops


def nozzle_end_view(cx: float, cy: float, od: float, d: float, hidden: bool = False) -> list[dict]:
    """管口端视（方位图/左视图里的封头管口）：法兰外圆 + 接管外圆。"""
    role = "hidden" if hidden else "thin"
    return [c.circle((cx, cy), d / 2, role, ["flange"]), c.circle((cx, cy), od / 2, role, ["neck"])]


def manhole_simplified(*args, **kw) -> list[dict]:
    """人孔简化外形 = 筒节 + 法兰 + 盖板（按 nozzle_side_view / nozzle_head_view 带 extra_cover 画）。"""
    if kw.pop("on_head", False):
        return nozzle_head_view(*args, **kw)
    return nozzle_side_view(*args, **kw)


# ------------------------------------------------------------------ 支座
def lug_support(side: float, ro: float, elev: float, row: dict) -> list[dict]:
    """耳式支座（主视图侧视示意）：底板 + 筋板。elev 为支承面（底板底面）的轴向位置。"""
    h, l1, t1 = float(row["H"]), float(row["l1"]), float(row["t1"])
    x0, x1 = side * ro, side * (ro + l1)
    ops = [c.poly([(x0, elev), (x1, elev), (x1, elev + t1), (x0, elev + t1)], True, "outline", ["lug_base"])]
    ops.append(c.poly([(x0, elev + t1), (side * (ro + l1 * 0.9), elev + t1), (side * (ro + l1 * 0.35), elev + h), (x0, elev + h)], True, "outline", ["lug_rib"]))
    return ops


def leg_support(side: float, ro: float, overlap: float, height: float, width: float) -> list[dict]:
    """支腿（角钢，示意）：从筒体搭接段顶部到底板底面；height 为基准线到底板底面的距离。"""
    x0, x1 = side * ro, side * (ro + width)
    base, tb = width * 2.0, 12.0
    xc = side * (ro + width / 2)
    ops = [c.poly([(x0, overlap), (x1, overlap), (x1, -height + tb), (x0, -height + tb)], True, "outline", ["leg"])]
    ops.append(c.rect(xc - base / 2, -height, xc + base / 2, -height + tb, "outline", ["leg_base"]))
    return ops


def saddle_support(axial: float, ri: float, ro: float, row: dict) -> list[dict]:
    """鞍座（主视图侧视示意，局部坐标：鞍座在 -x 一侧）：底板 + 腹板。鞍座高度 h 从底板底面量到筒体内壁最低点。"""
    h, b1, t1, t2 = float(row["h"]), float(row["b1"]), float(row["t1"]), float(row["t2"])
    xb = -(ri + h)
    ops = [c.rect(xb, axial - b1 / 2, xb + t1, axial + b1 / 2, "outline", ["saddle_base"])]
    ops.append(c.rect(xb + t1, axial - t2 / 2, -ro, axial + t2 / 2, "outline", ["saddle_web"]))
    for s in (-1.0, 1.0):                                         # 两块筋板（示意）
        ops.append(c.line((xb + t1, axial + s * b1 / 2 * 0.8), (-(ro + (h - (ro - ri)) * 0.35), axial + s * t2 / 2), "thin", ["saddle_rib"]))
    return ops


def saddle_end_view(cx: float, cy: float, ri: float, ro: float, row: dict) -> list[dict]:
    """鞍座（左视图）：底板 + 腹板外形 + 包角弧（120° 包角，示意）。"""
    h, l1, t1 = float(row["h"]), float(row["l1"]), float(row["t1"])
    wrap = float(row.get("wrap_deg", 120.0))
    yb = cy - (ri + h)
    ops = [c.rect(cx - l1 / 2, yb, cx + l1 / 2, yb + t1, "outline", ["saddle_base"])]
    a0, a1 = 270.0 - wrap / 2, 270.0 + wrap / 2
    p0 = (cx + ro * math.cos(math.radians(a0)), cy + ro * math.sin(math.radians(a0)))
    p1 = (cx + ro * math.cos(math.radians(a1)), cy + ro * math.sin(math.radians(a1)))
    xw = l1 / 2 * 0.92
    ops.append(c.line((cx - xw, yb + t1), p0, "outline", ["saddle_web"]))
    ops.append(c.line((cx + xw, yb + t1), p1, "outline", ["saddle_web"]))
    ops.append(c.arc((cx, cy), ro, a0, a1, "outline", ["saddle_web"]))
    return ops
