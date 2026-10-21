"""视图（任务书 5.1 views.py）：主视图（全剖、旋转画法、壁厚夸大）、立式的管口方位图（俯视）、卧式的左视图。

视图 = 部件 ops + 中心线 + 剖面 + 基准线 + 管口符号；返回 ViewOut（ops、几何包围盒、各管口在模型空间的位置，供尺寸、件号和自检用）。
生成图元的编号由 Builder 给：EQ-<视图>-<部件>-<序号>，同一份参数重新生成时编号不变。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from zhihui.cad.geom.entities import Transform, transform_data
from zhihui.cad.toolsets import common as c

from . import parts
from . import std_tables as T
from .profile import head_profile
from .rules import flange_dims

VIEW_NAMES = {"M": "主视图", "O": "管口方位图", "L": "左视图"}


class Builder:
    """收集某个视图的 ops，并给每个 op 一个确定性编号和标签。"""

    def __init__(self, view: str, transform: Transform | None = None):
        self.view = view
        self.T = transform
        self.ops: list[dict] = []
        self._n: dict[str, int] = {}

    def _id(self, part: str) -> str:
        k = self._n.get(part, 0) + 1
        self._n[part] = k
        return f"EQ-{self.view}-{part}-{k}"

    def add(self, part: str, ops: list[dict], local: bool = True) -> None:
        for o in ops:
            data = transform_data(o["kind"], o["data"], self.T) if (local and self.T is not None) else o["data"]
            self.ops.append({"id": self._id(part), "kind": o["kind"], "role": o.get("role", "outline"), "data": data,
                             "tags": ["equip:auto", f"equip_part:{part}", f"equip_view:{self.view}"] + list(o.get("tags", []))})

    def pt(self, p) -> tuple[float, float]:
        if self.T is None:
            return (p[0], p[1])
        q = self.T(p)
        return (q[0], q[1])

    def text(self, part: str, p, s: str, h: float, align: str = "left", rotation: float = 0.0, local: bool = True, tags=None) -> None:
        q = self.pt(p) if local else (p[0], p[1])
        self.add(part, [c.text(q, s, h, "text", tags or [], rotation, align)], local=False)


@dataclass
class ViewOut:
    view: str
    ops: list[dict]
    bbox: tuple[float, float, float, float]
    den: float
    nozzles: dict = field(default_factory=dict)          # mark → {"face": (x,y), "anchor": (x,y), "side": ±1, "axis": "x"/"y"}
    anchors: dict = field(default_factory=dict)          # 部件名 → 模型空间点（件号指引线的起点）
    info: dict = field(default_factory=dict)


def drawn_thickness(t: float, den: float) -> float:
    """图上壁厚 = max(δ·s, 1 mm)/s（s = 1/den），向外夸大，不改参数。"""
    return max(float(t), den * 1.0)


def lateral_reach(spec, den: float) -> float:
    """主视图侧面（立式向右、卧式向上）能伸出去的最远距离（到设备轴线）的上界：只看有哪些管口和支座，与它们转到哪一侧无关。
    件号列和方位图/左视图的位置按它定，这样转动一个管口方位时，别的管口和另一个视图的图元不会跟着整体平移。"""
    sh = spec.shell
    ri = sh.inner_diameter_mm / 2
    ro_true, ro_d = ri + float(sh.thickness_mm), ri + drawn_thickness(float(sh.thickness_mm), den)
    r = ro_d
    for n in spec.nozzles:
        if n.dn is None or n.mark is None:
            continue
        fl = flange_dims(n)
        cover = float(T.lookup("manhole", dn=n.dn)["row"]["cover_thk"]) if n.kind == "manhole" else 0.0
        if n.location == "shell":
            r = max(r, ro_true + float(n.projection_mm or 0.0) + cover + max(0.3 * fl["D"], 6.0 * den))      # 轴线超出法兰 0.3D；符号文字
        else:
            r = max(r, float(n.radial_mm or 0.0) + fl["D"] / 2 + 3.0 * den)
    for s in spec.supports:
        if s.kind == "lug":
            r = max(r, ro_d + float(T.lookup("lug_support_nbt47065_3", dn=sh.inner_diameter_mm)["row"]["l1"]))
        elif s.kind == "leg":
            r = max(r, ro_d + 1.5 * float(T.rule_value("leg_width_by_dn")[-1]["mm"]))
    return r + 10.0 * den                        # 管口符号文字、尺寸文字的余量


def side_of(spec, n) -> float:
    th = (n.orientation_deg or 0.0) % 360.0
    if spec.vertical:
        return 1.0 if th < 180.0 else -1.0
    return 1.0 if (th >= 270.0 or th < 90.0) else -1.0


def ops_bbox(ops: list[dict]) -> tuple[float, float, float, float]:
    from types import SimpleNamespace

    from zhihui.cad.geom.entities import entity_bbox
    xs: list[float] = []
    ys: list[float] = []
    for o in ops:
        if o["kind"] == "dimension":
            pts = [o["data"].get(k) for k in ("p1", "p2", "loc") if o["data"].get(k)]
            xs += [p[0] for p in pts]
            ys += [p[1] for p in pts]
            continue
        if o["kind"] == "text":
            from zhihui.cad.standards import text_width
            p, h = o["data"]["p"], float(o["data"]["height"])
            w = text_width(o["data"]["text"], h)
            al = o["data"].get("align", "left")
            x0 = p[0] - (w if al == "right" else w / 2 if al in ("center", "middle") else 0.0)
            xs += [x0, x0 + w]
            ys += [p[1] - (h / 2 if al == "middle" else 0.0), p[1] + h]
            continue
        b = entity_bbox(SimpleNamespace(id="x", kind=o["kind"], data=o["data"], tags=[], layer="0"))
        if b:
            xs += [b[0], b[2]]
            ys += [b[1], b[3]]
    if not xs:
        return (0.0, 0.0, 1.0, 1.0)
    return (min(xs), min(ys), max(xs), max(ys))


# ------------------------------------------------------------------ 主视图
def main_view(spec, den: float) -> ViewOut:
    sh = spec.shell
    ri, delta, L = sh.inner_diameter_mm / 2, float(sh.thickness_mm), float(sh.length_mm)
    tw = drawn_thickness(delta, den)
    ro_true, ro_d = ri + delta, ri + tw
    T_ = None if spec.vertical else Transform(0.0, 1.0, 1.0, 0.0)            # 卧式：关于 y=x 镜像（局部 x 径向 → 模型 y，局部 y 轴向 → 模型 x）
    b = Builder("M", T_)
    th = 3.5 * den
    out = ViewOut("M", [], (0, 0, 0, 0), den)
    # ---- 管口（先算开孔，筒体剖面要断开）
    openings: list[tuple[float, float, float]] = []
    heads = {h.position: h for h in spec.heads}
    start, end = ("bottom", "top") if spec.vertical else ("left", "right")
    prof = {p: head_profile(spec, heads[p]) for p in (start, end)}
    twh = {p: drawn_thickness(heads[p].thickness_mm, den) for p in (start, end)}
    for n in spec.nozzles:
        if n.location == "shell" and n.axial_mm is not None:
            openings.append((side_of(spec, n), n.axial_mm - n.pipe_od_mm / 2, n.axial_mm + n.pipe_od_mm / 2))
    b.add("SHELL", parts.shell_profile(ri, tw, L, openings))
    b.add("HEAD-" + ("B" if spec.vertical else "L"), parts.head_profile(prof[start], twh[start], 0.0, up=False))
    b.add("HEAD-" + ("T" if spec.vertical else "R"), parts.head_profile(prof[end], twh[end], L, up=True))
    out.anchors["shell"] = b.pt((ri + tw / 2, L * 0.62))
    out.anchors["head_" + end] = b.pt((ri * 0.55, L + prof[end].hsf + prof[end].outer_y(ri * 0.55)))
    out.anchors["head_" + start] = b.pt((ri * 0.55, -(prof[start].hsf + prof[start].outer_y(ri * 0.55))))
    # ---- 轴线、基准线
    top = L + prof[end].total_height
    bot = -prof[start].total_height
    ext = 8.0 * den
    b.add("AXIS", [c.line((0.0, bot - ext), (0.0, top + ext), "center", ["axis"])])
    b.add("DATUM", [c.line((-(ro_d + 4 * den), 0.0), (ro_d + 4 * den, 0.0), "thin", ["datum"])])
    if spec.vertical:
        b.text("DATUM", (-(ro_d + 0.5 * den), 0.6 * den), "基准线", th, "right")        # 写在筒体外侧，不压筒壁
    else:
        b.text("DATUM", (ro_d + 4.6 * den, -0.5 * den), "基准线", th, "left")
    # ---- 管口
    for n in spec.nozzles:
        if n.dn is None or n.mark is None:
            continue
        fl = flange_dims(n)
        d_, cth = fl["D"], fl["C"]
        cover = 0.0
        if n.kind == "manhole":
            cover = float(T.lookup("manhole", dn=n.dn)["row"]["cover_thk"])
        sd = side_of(spec, n)
        part = f"NZ-{n.mark}"
        if n.location == "shell":
            x_start, x_face = sd * ri, sd * (ro_true + n.projection_mm)
            b.add(part, parts.nozzle_side_view(sd, n.axial_mm, x_start, x_face, n.pipe_od_mm, n.pipe_thk_mm, d_, cth, cover))
            face = (x_face, n.axial_mm)
            lab = (x_face + sd * (cover + 1.0 * den), n.axial_mm + d_ / 2 + 0.8 * den)
            out.nozzles[n.mark] = {"face": b.pt(face), "anchor": b.pt((sd * (ro_d + (n.projection_mm - cth) * 0.5), n.axial_mm + n.pipe_od_mm / 2)), "side": sd, "loc": "shell",
                                   "axial": n.axial_mm, "label": b.pt(lab)}
        else:
            pos = n.location in ("top_head", "right_head")
            p_ = end if pos else start
            pr = prof[p_]
            x = sd * float(n.radial_mm or 0.0)
            dr = 1.0 if pos else -1.0
            y_seam = L if pos else 0.0

            def surf(xx, pr=pr, p_=p_, y_seam=y_seam, dr=dr):
                hp = parts.HeadProfile(pr.kind, pr.a, twh[p_], pr.hsf, pr.hc, pr.rc, pr.rk)
                return y_seam + dr * (pr.hsf + hp.outer_y(min(abs(xx), pr.a + twh[p_] - 1e-9)))

            y_face = y_seam + dr * n.projection_mm
            b.add(part, parts.nozzle_head_view(x, surf(x - n.pipe_od_mm / 2), surf(x + n.pipe_od_mm / 2), y_face, dr, n.pipe_od_mm, n.pipe_thk_mm, d_, cth, cover))
            lab = (x + d_ / 2 + 0.8 * den, y_face + dr * (cover + 0.8 * den) - (th if dr < 0 else 0.0))
            out.nozzles[n.mark] = {"face": b.pt((x, y_face)), "anchor": b.pt((x + n.pipe_od_mm / 2, (surf(x) + y_face - dr * cth) / 2)), "side": sd, "loc": n.location,
                                   "radial": float(n.radial_mm or 0.0), "label": b.pt(lab)}
        lp = out.nozzles[n.mark]["label"]
        align = "left"
        if spec.vertical and n.location == "shell" and sd < 0:
            align = "right"
        b.text(part, lp, n.mark, th, align, local=False, tags=["equip_mark", f"equip_mark:{n.mark}"])
    # 同侧同高度投影重叠：给警告（P0 不画局部视图）
    seen: list[tuple] = []
    for n in spec.nozzles:
        if n.location != "shell" or n.axial_mm is None or n.mark is None:
            continue
        sd = side_of(spec, n)
        for m, sd2 in seen:
            if sd2 == sd and abs(m.axial_mm - n.axial_mm) < (flange_dims(m)["D"] + flange_dims(n)["D"]) / 2:
                out.info.setdefault("warnings", []).append(f"W_ROTATED_OVERLAP：管口 {m.mark} 与 {n.mark} 旋转到同一侧后在主视图里投影重叠（P0 未另画局部视图，以管口方位图为准）")
        seen.append((n, sd))
    # ---- 支座
    for s in spec.supports:
        if s.kind == "lug" and s.positions:
            row = T.lookup("lug_support_nbt47065_3", dn=sh.inner_diameter_mm)["row"]
            sides = sorted({side_of(spec, type("N", (), {"orientation_deg": p["orientation_deg"]})()) for p in s.positions}, reverse=True)
            e = s.positions[0].get("elevation_mm") or 0.0
            for sd in sides:
                b.add("LUG", parts.lug_support(sd, ro_d, e, row))
            out.anchors["lug"] = b.pt((sides[0] * (ro_d + float(row["l1"]) * 0.3), e + float(row["H"]) * 0.5))
            out.info["lug_elev"] = e
            out.info["lug_x"] = b.pt((sides[0] * (ro_d + float(row["l1"])), e))
            out.info["lug_sides"], out.info["lug_l1"], out.info["lug_h"] = sides, float(row["l1"]), float(row["H"])
        elif s.kind == "leg" and s.positions:
            w = float(T.rule_value("leg_width_by_dn")[-1]["mm"])
            for r in T.rule_value("leg_width_by_dn"):
                if sh.inner_diameter_mm <= r["dn_max"]:
                    w = float(r["mm"])
                    break
            sides = sorted({side_of(spec, type("N", (), {"orientation_deg": p["orientation_deg"]})()) for p in s.positions}, reverse=True)
            for sd in sides:
                b.add("LEG", parts.leg_support(sd, ro_d, float(T.rule_value("leg_overlap_mm")), float(s.height_mm), w))
            out.anchors["leg"] = b.pt((sides[0] * (ro_d + w / 2), -float(s.height_mm) * 0.5))
        elif s.kind == "saddle" and s.positions:
            row = T.lookup("saddle_nbt47065_1", dn=sh.inner_diameter_mm)["row"]
            for i, p in enumerate(s.positions, 1):
                b.add(f"SADDLE-{i}", parts.saddle_support(p["axial_mm"], ri, ro_d, row))
                b.text(f"SADDLE-{i}", (-(ri + float(row["h"])) - 1.2 * den - th, p["axial_mm"]), "F" if p.get("fixed") else "S", th, "center")
            out.anchors["saddle"] = b.pt((-(ro_d + float(row["h"]) * 0.4), s.positions[0]["axial_mm"]))
            out.info["saddles"] = [p["axial_mm"] for p in s.positions]
            out.info["saddle_bottom"] = -(ri + float(row["h"]))
    out.ops = b.ops
    out.bbox = ops_bbox(b.ops)
    out.info.update({"tw": tw, "ri": ri, "ro_true": ro_true, "ro_drawn": ro_d, "L": L, "bottom_true": -(prof[start].total_height), "top_true": L + prof[end].total_height,
                     "head_" + start: prof[start].total_height, "head_" + end: prof[end].total_height})
    return out


# ------------------------------------------------------------------ 管口方位图（立式俯视）/ 左视图（卧式）
def _polar(cx, cy, r, theta_deg):
    """方位角：0° 朝图纸上方、顺时针为正。"""
    t = math.radians(theta_deg)
    return (cx + r * math.sin(t), cy + r * math.cos(t))


def orientation_view(spec, den: float, center: tuple[float, float]) -> ViewOut:
    return _end_view(spec, den, center, "O")


def left_view(spec, den: float, center: tuple[float, float]) -> ViewOut:
    return _end_view(spec, den, center, "L")


def _end_view(spec, den: float, center: tuple[float, float], view: str) -> ViewOut:
    sh = spec.shell
    ri, delta = sh.inner_diameter_mm / 2, float(sh.thickness_mm)
    tw = drawn_thickness(delta, den)
    ro_true, ro_d = ri + delta, ri + tw
    cx, cy = center
    b = Builder(view, None)
    th = 3.5 * den
    out = ViewOut(view, [], (0, 0, 0, 0), den)
    b.add("SHELL", [c.circle((cx, cy), ri, "outline"), c.circle((cx, cy), ro_d, "outline")], local=False)
    proj_max = max([float(n.projection_mm or 0.0) for n in spec.nozzles if n.location == "shell"] + [0.0])
    from zhihui.cad.standards import text_width
    labels = {}
    for m in spec.nozzles:
        if m.location == "shell" and m.mark and m.orientation_deg is not None:
            labels.setdefault(round(m.orientation_deg % 360.0, 6), []).append(m.mark)
    lab_w = max([text_width("、".join(v) + "（000°）", th) for v in labels.values()] + [0.0])
    r_ext = ro_true + proj_max + 6 * den + lab_w
    b.add("AXIS", [c.line((cx - r_ext, cy), (cx + r_ext, cy), "center"), c.line((cx, cy - r_ext), (cx, cy + r_ext), "center")], local=False)
    for ang, s in ((0, "0°"), (90, "90°"), (180, "180°"), (270, "270°")):
        p = _polar(cx, cy, r_ext + 2.0 * den, ang)
        al = {0: "center", 90: "left", 180: "center", 270: "right"}[ang]
        b.text("ANGLE", (p[0], p[1] - (th / 2 if ang in (90, 270) else (th + 0.5 * den if ang == 180 else 0.0))), s, th, al, local=False)
    for n in spec.nozzles:
        if n.dn is None or n.mark is None or n.orientation_deg is None:
            continue
        fl = flange_dims(n)
        part = f"NZ-{n.mark}"
        th_ = n.orientation_deg % 360.0
        if n.location == "shell":
            p0, p1 = _polar(cx, cy, ro_d, th_), _polar(cx, cy, ro_true + n.projection_mm, th_)
            t = math.radians(th_)
            ux, uy = math.cos(t), -math.sin(t)                                    # 径向的垂直方向（画法兰短线）
            hd = fl["D"] / 2
            same = [m for m in spec.nozzles if m.location == "shell" and m.mark and m.orientation_deg is not None and abs((m.orientation_deg % 360.0) - th_) < 1e-6]
            twin = next((m for m in same[:same.index(n)] if m.projection_mm == n.projection_mm and flange_dims(m)["D"] == fl["D"]), None)
            if twin is None:                                                       # 同方位、同伸出、同法兰的管口在俯视图里完全重合（如一对液位计口）：只画一次
                b.add(part, [c.line(p0, p1, "outline", ["neck"]), c.line((p1[0] - ux * hd, p1[1] - uy * hd), (p1[0] + ux * hd, p1[1] + uy * hd), "outline", ["flange"])], local=False)
            out.nozzles[n.mark] = {"face": p1, "angle": th_}
            if same[0] is n:                                                       # 同一方位的筒体管口只写一个标签：“b1、b2、M1（0°）”
                rmax = max(ro_true + float(m.projection_mm or 0.0) for m in same)
                lp = _polar(cx, cy, rmax + 4.0 * den, th_)
                al = "left" if 15 < th_ < 165 else "right" if 195 < th_ < 345 else "center"
                dy = -th / 2 if al != "center" else (0.4 * den if th_ < 90 or th_ > 270 else -th - 0.4 * den)
                b.text(part, (lp[0], lp[1] + dy), "、".join(m.mark for m in same) + f"（{th_:g}°）", th, al, local=False,
                       tags=["equip_mark"] + [f"equip_mark:{m.mark}" for m in same])
        else:
            hidden = (view == "O" and n.location == "bottom_head") or (view == "L" and n.location == "right_head")
            pc = _polar(cx, cy, float(n.radial_mm or 0.0), th_)
            b.add(part, parts.nozzle_end_view(pc[0], pc[1], n.pipe_od_mm, fl["D"], hidden), local=False)
            off = fl["D"] / 2 + 0.8 * den
            b.text(part, (pc[0] + off * 0.7, pc[1] + off * 0.7), n.mark + ("（下）" if n.location == "bottom_head" else "（右）" if n.location == "right_head" else ""), th, "left", local=False,
                   tags=["equip_mark", f"equip_mark:{n.mark}"])
            out.nozzles[n.mark] = {"face": pc, "angle": th_}
    for s in spec.supports:
        if s.kind in ("lug", "leg") and s.positions:
            row = T.lookup("lug_support_nbt47065_3", dn=sh.inner_diameter_mm)["row"] if s.kind == "lug" else {"l1": 120.0, "b1": 80.0}
            for p in s.positions:
                th_ = p["orientation_deg"]
                t = math.radians(th_)
                ux, uy = math.cos(t), -math.sin(t)
                a0, a1 = _polar(cx, cy, ro_d, th_), _polar(cx, cy, ro_d + float(row["l1"]), th_)
                hw = float(row["b1"]) / 2
                b.add("LUG" if s.kind == "lug" else "LEG", [c.poly([(a0[0] - ux * hw, a0[1] - uy * hw), (a1[0] - ux * hw, a1[1] - uy * hw), (a1[0] + ux * hw, a1[1] + uy * hw), (a0[0] + ux * hw, a0[1] + uy * hw)], True, "outline")], local=False)
        elif s.kind == "saddle" and view == "L":
            row = T.lookup("saddle_nbt47065_1", dn=sh.inner_diameter_mm)["row"]
            b.add("SADDLE", parts.saddle_end_view(cx, cy, ri, ro_d, row), local=False)
    conv = spec.drawing.get("orientation_convention") or ("方位：0° 朝上，顺时针为正（俯视）" if spec.vertical else "方位：从左封头端向右看，0° 为正上方，顺时针为正")
    bb = ops_bbox(b.ops)
    b.text("NOTE", (cx, bb[1] - 2.5 * den - th), conv, th, "center", local=False)
    b.text("TITLE", (cx, bb[3] + 2.0 * den), VIEW_NAMES[view], 5.0 * den, "center", local=False)
    out.ops = b.ops
    out.bbox = ops_bbox(b.ops)
    out.info.update({"tw": tw, "convention": conv})
    return out
