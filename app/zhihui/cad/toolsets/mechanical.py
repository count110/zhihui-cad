"""机械制图工具集：中心线、阶梯轴、孔与螺纹、标准件、法兰、螺栓圆、键槽、轴承、齿轮。

每个生成器 gen(p, ins, targets) → {"ops","summary","info"}；ins = 插入点（x,y）；targets = 目标图元（有些生成器需要）。
尺寸一律来自用户（spec_params）或标准表（codes/mech_std.json，表里查不到就反问）。简化画法按 GB/T 4459 系列，做不到的地方在 info.notes 里说明。
"""
from __future__ import annotations

import math

from zhihui.cad.geom import mech
from zhihui.cad.geom.errors import GeomError
from . import common as c


def _size(table: str, size: str) -> dict:
    t = mech.mech_std()[table]
    s = str(size).upper().replace(" ", "")
    if s not in t:
        raise GeomError("SIZE_UNKNOWN", f"规格 {size} 不在本系统的 {table} 表里", f"覆盖：{', '.join(t)}；其他规格请用户给出 s、k 等尺寸")
    return {**t[s], "size": s, "d": float(s[1:]), "pitch": mech.mech_std()["coarse_pitch"].get(s)}


# ------------------------------------------------------------------ 中心线
def centermark_centerline(p, ins, targets):
    ext = c.positive("extension", p.get("extension", 3.0))
    ops, names = [], []
    circles = [t for t in targets if t["kind"] in ("circle", "arc")]
    lines = [t for t in targets if t["kind"] == "line"]
    for t in circles:
        cx, cy, _ = t["data"]["c"]
        r = t["data"]["r"]
        ops += [c.line((cx - r - ext, cy), (cx + r + ext, cy), "center", ["centermark"]), c.line((cx, cy - r - ext), (cx, cy + r + ext), "center", ["centermark"])]
        names.append(t["id"])
    if len(lines) == 2:
        a1, a2 = lines[0]["data"], lines[1]["data"]
        v1 = (a1["p2"][0] - a1["p1"][0], a1["p2"][1] - a1["p1"][1])
        v2 = (a2["p2"][0] - a2["p1"][0], a2["p2"][1] - a2["p1"][1])
        if abs(v1[0] * v2[1] - v1[1] * v2[0]) > 1e-6 * math.hypot(*v1) * math.hypot(*v2):
            raise GeomError("INVALID_ARGS", "两条线不平行，不能画它们的对称中心线", "")
        m0 = ((a1["p1"][0] + a2["p1"][0]) / 2, (a1["p1"][1] + a2["p1"][1]) / 2)
        m1 = ((a1["p2"][0] + a2["p2"][0]) / 2, (a1["p2"][1] + a2["p2"][1]) / 2)
        if (v1[0] * v2[0] + v1[1] * v2[1]) < 0:                               # 方向相反就换端点
            m1 = ((a1["p2"][0] + a2["p1"][0]) / 2, (a1["p2"][1] + a2["p1"][1]) / 2)
        L = math.hypot(m1[0] - m0[0], m1[1] - m0[1]) or 1.0
        u = ((m1[0] - m0[0]) / L, (m1[1] - m0[1]) / L)
        ops.append(c.line((m0[0] - u[0] * ext, m0[1] - u[1] * ext), (m1[0] + u[0] * ext, m1[1] + u[1] * ext), "center", ["centerline"]))
        names += [lines[0]["id"], lines[1]["id"]]
    elif len(lines) == 1:
        a = lines[0]["data"]
        v = (a["p2"][0] - a["p1"][0], a["p2"][1] - a["p1"][1])
        L = math.hypot(*v) or 1.0
        u = (v[0] / L, v[1] / L)
        ops.append(c.line((a["p1"][0] - u[0] * ext, a["p1"][1] - u[1] * ext), (a["p2"][0] + u[0] * ext, a["p2"][1] + u[1] * ext), "center", ["centerline"]))
        names.append(lines[0]["id"])
    if not ops:
        raise GeomError("INVALID_ARGS", "centermark_centerline 需要 target_entity_ids：圆/圆弧（画十字中心线），或一条线 / 两条平行线（画中心线）", "")
    return c.result(ops, f"为 {names} 画了 {len(ops)} 条中心线（超出轮廓 {ext:g} mm）", extension=ext)


# ------------------------------------------------------------------ 阶梯轴
def shaft_generator(p, ins, targets):
    segs, = c.need(p, "segments", hint="阶梯轴每一段的直径和长度必须由用户给出：segments=[{diameter, length}, …]")
    if not isinstance(segs, list) or not segs:
        raise GeomError("INVALID_ARGS", "segments 必须是非空列表", "")
    ch = float(p.get("chamfer", 0.0) or 0.0)
    x, y0 = ins
    bounds, top = [x], []
    for i, s in enumerate(segs):
        d, L = c.positive(f"segments[{i}].diameter", s.get("diameter")), c.positive(f"segments[{i}].length", s.get("length"))
        top.append((x, d / 2, x + L))
        x += L
        bounds.append(x)
    if ch and (ch >= segs[0]["length"] or ch >= segs[0]["diameter"] / 2 or ch >= segs[-1]["length"] or ch >= segs[-1]["diameter"] / 2):
        raise GeomError("INVALID_ARGS", "倒角太大", "倒角必须小于端段的长度和半径")
    up: list[tuple[float, float]] = []
    for i, (xa, r, xb) in enumerate(top):
        a = (xa, r)
        if i == 0 and ch:
            up += [(xa, r - ch), (xa + ch, r)]
        else:
            up.append(a)
        if i == len(top) - 1 and ch:
            up += [(xb - ch, r), (xb, r - ch)]
        else:
            up.append((xb, r))
    pts = [(px, y0 + py) for px, py in up] + [(px, y0 - py) for px, py in reversed(up)]
    # 去掉相邻重复点
    clean = [q for i, q in enumerate(pts) if i == 0 or (abs(q[0] - pts[i - 1][0]) > 1e-9 or abs(q[1] - pts[i - 1][1]) > 1e-9)]
    ops = [c.poly(clean, True, "outline", ["shaft"])]
    if ch:
        for xe, rr in ((top[0][0] + ch, top[0][1]), (top[-1][2] - ch, top[-1][1])):
            ops.append(c.line((xe, y0 + rr), (xe, y0 - rr), "outline", ["shaft_chamfer_edge"]))
    ops.append(c.line((bounds[0] - 3, y0), (bounds[-1] + 3, y0), "center", ["centerline"]))
    return c.result(ops, f"阶梯轴 {len(segs)} 段，总长 {bounds[-1] - bounds[0]:g}，最大直径 {max(s['diameter'] for s in segs):g}", total_length=bounds[-1] - bounds[0], step_positions=bounds)


# ------------------------------------------------------------------ 孔与螺纹
def hole_thread_feature(p, ins, targets):
    typ = p.get("type", "through")
    view = p.get("view", "top")
    d, = c.need(p, "diameter", hint="孔径必须由用户给出")
    d = c.positive("diameter", d)
    x, y = ins
    notes = []
    internal = bool(p.get("internal", True))
    ops = []
    if view == "top":
        if typ == "thread":
            minor = d * 0.85
            ops += [c.circle((x, y), (minor if internal else d) / 2, "outline", ["thread_hole"]), c.thread_arc((x, y), (d if internal else minor) / 2, "thin", ["thread_hole"])]
            notes.append("螺纹小径按 0.85·d 的简化画法")
        elif typ == "counterbore":
            cbd, = c.need(p, "cb_diameter", hint="沉孔直径必须由用户给出")
            ops += [c.circle((x, y), d / 2, "outline"), c.circle((x, y), c.positive("cb_diameter", cbd) / 2, "outline")]
        elif typ == "countersink":
            csd, = c.need(p, "cs_diameter", hint="锥孔大端直径必须由用户给出")
            ops += [c.circle((x, y), d / 2, "outline"), c.circle((x, y), c.positive("cs_diameter", csd) / 2, "outline")]
        else:
            ops.append(c.circle((x, y), d / 2, "outline", ["hole"]))
        ext = d / 2 + 3
        ops += [c.line((x - ext, y), (x + ext, y), "center", ["centermark"]), c.line((x, y - ext), (x, y + ext), "center", ["centermark"])]
    else:                                                                    # 剖视：表面在 y，孔向下
        t, = c.need(p, "plate_thickness", hint="剖视图需要板厚（通孔长度）")
        t = c.positive("plate_thickness", t)
        depth = c.positive("depth", p["depth"]) if typ == "blind" and p.get("depth") else t
        if typ == "blind" and not p.get("depth"):
            raise GeomError("MISSING_ARG", "盲孔需要 depth（孔深）", "向用户确认孔深")
        half = d / 2
        if typ == "thread":
            minor = d * 0.85 / 2
            ops += [c.line((x - minor, y), (x - minor, y - depth), "outline"), c.line((x + minor, y), (x + minor, y - depth), "outline"),
                    c.line((x - half, y), (x - half, y - depth), "thin"), c.line((x + half, y), (x + half, y - depth), "thin")]
            if typ == "thread" and p.get("depth"):
                ops.append(c.line((x - minor, y - depth), (x + minor, y - depth), "outline"))
        elif typ == "counterbore":
            cbd, cbh = c.need(p, "cb_diameter", "cb_depth", hint="沉孔直径和深度必须由用户给出")
            cb, ch_ = c.positive("cb_diameter", cbd) / 2, c.positive("cb_depth", cbh)
            ops.append(c.poly([(x - cb, y), (x - cb, y - ch_), (x - half, y - ch_), (x - half, y - depth)], False, "outline"))
            ops.append(c.poly([(x + cb, y), (x + cb, y - ch_), (x + half, y - ch_), (x + half, y - depth)], False, "outline"))
        elif typ == "countersink":
            csd, = c.need(p, "cs_diameter", hint="锥孔大端直径必须由用户给出")
            cs = c.positive("cs_diameter", csd) / 2
            ang_ = math.radians(float(p.get("cs_angle", 90.0)) / 2)
            hh = (cs - half) / math.tan(ang_)
            ops.append(c.poly([(x - cs, y), (x - half, y - hh), (x - half, y - depth)], False, "outline"))
            ops.append(c.poly([(x + cs, y), (x + half, y - hh), (x + half, y - depth)], False, "outline"))
        else:
            ops += [c.line((x - half, y), (x - half, y - depth), "outline"), c.line((x + half, y), (x + half, y - depth), "outline")]
        if typ == "blind":
            ops.append(c.line((x - half, y - depth), (x + half, y - depth), "outline"))
            notes.append("盲孔底部画平底，未画 118° 钻尖")
        ops.append(c.line((x, y + 3), (x, y - depth - 3), "center", ["centerline"]))
    return c.result(ops, f"{ {'through': '通孔', 'blind': '盲孔', 'counterbore': '沉孔', 'countersink': '锥形沉孔', 'thread': '螺纹孔'}.get(typ, typ) } ⌀{d:g}（{'俯视' if view == 'top' else '剖视'}）", notes=notes)


# ------------------------------------------------------------------ 标准件
def standard_fastener(p, ins, targets):
    typ = p.get("type")
    if typ not in ("hex_bolt", "hex_nut", "washer", "socket_head_cap_screw"):
        raise GeomError("INVALID_ARGS", "type 必须是 hex_bolt / hex_nut / washer / socket_head_cap_screw", "")
    size, = c.need(p, "size", hint="规格（如 M8）必须由用户给出")
    t = _size({"hex_bolt": "hex_bolt", "hex_nut": "hex_nut", "washer": "washer", "socket_head_cap_screw": "socket_head_cap_screw"}[typ], size)
    d = t["d"]
    view = p.get("view", "side")
    x, y = ins                                                              # 插入点：头部（或螺母/垫圈）与杆的轴线上、顶面中心
    ops, notes = [], ["简化画法，尺寸取自 codes/mech_std.json（GB/T 5782/6170/97.1/70.1），使用前核对最新版标准"]
    if typ in ("hex_bolt", "socket_head_cap_screw"):
        L, = c.need(p, "length", hint="螺栓/螺钉的公称长度必须由用户给出")
        L = c.positive("length", L)
        k = t["k"]
        b = min(L, 2 * d + 6)                                               # 螺纹长度按 GB/T 5782 的 l≤125 取 2d+6
        notes.append(f"螺纹长度按 2d+6 = {2 * d + 6:g}（l≤125）取 {b:g}")
    if view == "top":
        if typ == "hex_bolt":
            ops += [c.hexagon((x, y), t["s"], 0, "outline"), c.circle((x, y), t["s"] / 2, "thin"), c.thread_arc((x, y), d * 0.85 / 2, "thin")]
        elif typ == "hex_nut":
            ops += [c.hexagon((x, y), t["s"], 0, "outline"), c.circle((x, y), t["s"] / 2, "thin"), c.circle((x, y), d * 0.85 / 2, "outline"), c.thread_arc((x, y), d / 2, "thin")]
        elif typ == "washer":
            ops += [c.circle((x, y), t["d2"] / 2, "outline"), c.circle((x, y), t["d1"] / 2, "outline")]
        else:
            ops += [c.circle((x, y), t["dk"] / 2, "outline"), c.hexagon((x, y), t["s"], 0, "outline"), c.thread_arc((x, y), d * 0.85 / 2, "thin")]
        ops += [c.line((x - t.get("dk", t.get("d2", t.get("s", d))) / 2 - 3, y), (x + t.get("dk", t.get("d2", t.get("s", d))) / 2 + 3, y), "center"), c.line((x, y - t.get("dk", t.get("d2", t.get("s", d))) / 2 - 3), (x, y + t.get("dk", t.get("d2", t.get("s", d))) / 2 + 3), "center")]
        return c.result(ops, f"{typ} {t['size']} 俯视图", params={k: v for k, v in t.items() if k not in ("size",)}, notes=notes)
    if typ == "hex_bolt":
        e = t["s"] * 1.1547
        ops.append(c.rect(x - e / 2, y - k, x + e / 2, y, "outline", ["bolt_head"]))
        ops += [c.line((x - d / 2, y - k), (x - d / 2, y - k - L), "outline"), c.line((x + d / 2, y - k), (x + d / 2, y - k - L), "outline"), c.line((x - d / 2, y - k - L), (x + d / 2, y - k - L), "outline")]
        m = d * 0.85 / 2
        ops += [c.line((x - m, y - k - L + b), (x - m, y - k - L), "thin"), c.line((x + m, y - k - L + b), (x + m, y - k - L), "thin"), c.line((x - d / 2, y - k - L + b), (x + d / 2, y - k - L + b), "outline")]
        ops.append(c.line((x, y + 3), (x, y - k - L - 3), "center"))
    elif typ == "socket_head_cap_screw":
        ops.append(c.rect(x - t["dk"] / 2, y - k, x + t["dk"] / 2, y, "outline", ["screw_head"]))
        ops += [c.line((x - d / 2, y - k), (x - d / 2, y - k - L), "outline"), c.line((x + d / 2, y - k), (x + d / 2, y - k - L), "outline"), c.line((x - d / 2, y - k - L), (x + d / 2, y - k - L), "outline")]
        m = d * 0.85 / 2
        ops += [c.line((x - m, y - k - L + b), (x - m, y - k - L), "thin"), c.line((x + m, y - k - L + b), (x + m, y - k - L), "thin"), c.line((x - d / 2, y - k - L + b), (x + d / 2, y - k - L + b), "outline")]
        sk = t["s"] / 2
        ops += [c.line((x - sk, y), (x - sk, y - k * 0.6), "hidden"), c.line((x + sk, y), (x + sk, y - k * 0.6), "hidden"), c.line((x - sk, y - k * 0.6), (x + sk, y - k * 0.6), "hidden")]
        ops.append(c.line((x, y + 3), (x, y - k - L - 3), "center"))
    elif typ == "hex_nut":
        e = t["s"] * 1.1547
        ops.append(c.rect(x - e / 2, y - t["m"], x + e / 2, y, "outline", ["nut"]))
        m = d * 0.85 / 2
        ops += [c.line((x - m, y), (x - m, y - t["m"]), "hidden"), c.line((x + m, y), (x + m, y - t["m"]), "hidden")]
        ops.append(c.line((x, y + 3), (x, y - t["m"] - 3), "center"))
    else:
        ops.append(c.rect(x - t["d2"] / 2, y - t["h"], x + t["d2"] / 2, y, "outline", ["washer"]))
        ops += [c.line((x - t["d1"] / 2, y), (x - t["d1"] / 2, y - t["h"]), "hidden"), c.line((x + t["d1"] / 2, y), (x + t["d1"] / 2, y - t["h"]), "hidden")]
        ops.append(c.line((x, y + 3), (x, y - t["h"] - 3), "center"))
    return c.result(ops, f"{typ} {t['size']}" + (f" × {p['length']:g}" if "length" in p else "") + "（主视图）", params={k: v for k, v in t.items() if k != "size"}, notes=notes)


# ------------------------------------------------------------------ 螺栓圆 / 法兰
def _bolt_holes(center, K, n, d, start):
    holes = []
    for i in range(n):
        a = math.radians(start + 360.0 * i / n)
        holes.append((center[0] + K / 2 * math.cos(a), center[1] + K / 2 * math.sin(a)))
    return holes


def bolt_circle(p, ins, targets):
    K, n, hd = c.need(p, "diameter", "count", "hole_diameter", hint="螺栓圆直径、孔数和孔径必须由用户给出")
    K, hd = c.positive("diameter", K), c.positive("hole_diameter", hd)
    n = int(n)
    if n < 1 or n > 200:
        raise GeomError("INVALID_ARGS", "孔数必须在 1–200", "")
    ctr = tuple(p["center"][:2]) if p.get("center") else ins
    start = float(p.get("start_angle_deg", 90.0))
    ops = [c.circle(ctr, K / 2, "center", ["bolt_circle"])]
    ops += [c.circle(h, hd / 2, "outline", ["hole"]) for h in _bolt_holes(ctr, K, n, hd, start)]
    ops += [c.line((ctr[0] - K / 2 - hd, ctr[1]), (ctr[0] + K / 2 + hd, ctr[1]), "center"), c.line((ctr[0], ctr[1] - K / 2 - hd), (ctr[0], ctr[1] + K / 2 + hd), "center")]
    return c.result(ops, f"螺栓圆 ⌀{K:g}，{n} 个 ⌀{hd:g} 孔，起始角 {start:g}°", notes=["起始角默认 90°（正上方）"] if "start_angle_deg" not in p else [])


def flange_generator(p, ins, targets):
    D, K, n, hd = c.need(p, "outer_diameter", "bolt_circle_diameter", "hole_count", "hole_diameter", hint="法兰外径、螺栓圆直径、孔数、孔径必须由用户给出（本系统不内置法兰标准表）")
    D, K, hd = c.positive("outer_diameter", D), c.positive("bolt_circle_diameter", K), c.positive("hole_diameter", hd)
    if K + hd >= D:
        raise GeomError("INVALID_ARGS", "螺栓孔会超出法兰外圆（螺栓圆直径 + 孔径 ≥ 外径）", "检查尺寸")
    ops = [c.circle(ins, D / 2, "outline", ["flange"])]
    if p.get("bore_diameter"):
        bd = c.positive("bore_diameter", p["bore_diameter"])
        if bd >= K - hd:
            raise GeomError("INVALID_ARGS", "内孔会碰到螺栓孔", "")
        ops.append(c.circle(ins, bd / 2, "outline", ["bore"]))
    sub = bolt_circle({"diameter": K, "count": n, "hole_diameter": hd, "start_angle_deg": p.get("start_angle_deg", 90.0)}, ins, targets)
    ops += sub["ops"]
    return c.result(ops, f"法兰 ⌀{D:g}，螺栓圆 ⌀{K:g}，{int(n)}×⌀{hd:g}", notes=["俯视（端面）简化画法"])


# ------------------------------------------------------------------ 键槽
def keyway(p, ins, targets):
    d, = c.need(p, "shaft_diameter", hint="轴径必须由用户给出")
    d = c.positive("shaft_diameter", d)
    part = p.get("part", "shaft")
    if p.get("key_width") and (p.get("t1") or p.get("t2")):
        row = {"b": float(p["key_width"]), "t1": float(p.get("t1") or 0), "t2": float(p.get("t2") or 0), "h": float(p.get("key_height") or 0)}
        src = "用户给出"
    else:
        row = mech.keyway(d)
        src = "GB/T 1095 表"
    b = float(row["b"])
    r = d / 2
    if b >= d:
        raise GeomError("INVALID_ARGS", "键宽不能大于轴径", "")
    x, y = ins
    half = b / 2
    notes = [f"键槽尺寸取自{src}：b={b:g}, t1={row['t1']:g}, t2={row['t2']:g}；使用前核对最新版标准"]
    ops = []
    if part == "shaft":
        yb = y + r - row["t1"]
        ye = y + math.sqrt(r * r - half * half)
        if yb >= ye:
            raise GeomError("INVALID_ARGS", "键槽深度太小，没有切进轴里", "")
        a0 = math.degrees(math.atan2(ye - y, half))                          # 右边缘点对应的角度
        ops.append(c.arc((x, y), r, 180 - a0, a0, "outline", ["shaft_end"]))      # 从槽左缘逆时针绕一圈回到槽右缘
        ops.append(c.poly([(x + half, ye), (x + half, yb), (x - half, yb), (x - half, ye)], False, "outline", ["keyway"]))
        ops.append(c.line((x - r - 3, y), (x + r + 3, y), "center"))
        ops.append(c.line((x, y - r - 3), (x, y + r + 3), "center"))
        info = {"dimension_d_minus_t1": round(d - row["t1"], 3)}
    else:
        yt = y + r + row["t2"]
        ye = y + math.sqrt(r * r - half * half)
        a0 = math.degrees(math.atan2(ye - y, half))
        ops.append(c.arc((x, y), r, 180 - a0, a0, "outline", ["hub_bore"]))
        ops.append(c.poly([(x + half, ye), (x + half, yt), (x - half, yt), (x - half, ye)], False, "outline", ["keyway"]))
        ops.append(c.line((x - r - 3, y), (x + r + 3, y), "center"))
        ops.append(c.line((x, y - r - 3), (x, yt + 3), "center"))
        info = {"dimension_d_plus_t2": round(d + row["t2"], 3)}
    return c.result(ops, f"{'轴' if part == 'shaft' else '轮毂'}键槽 d={d:g}，键宽 {b:g}", notes=notes, **info)


# ------------------------------------------------------------------ 轴承（通用画法）
def bearing_simplified(p, ins, targets):
    d, D, B = c.need(p, "inner_diameter", "outer_diameter", "width", hint="轴承内径、外径、宽度必须由用户给出（本系统不内置轴承型号表）")
    d, D, B = c.positive("inner_diameter", d), c.positive("outer_diameter", D), c.positive("width", B)
    if D <= d:
        raise GeomError("INVALID_ARGS", "外径必须大于内径", "")
    x, y = ins
    ops = []
    for s in (1, -1):
        y0, y1 = (y + s * d / 2, y + s * D / 2)
        lo, hi = min(y0, y1), max(y0, y1)
        ops.append(c.rect(x, lo, x + B, hi, "outline", ["bearing"]))
        cx, cy = x + B / 2, (lo + hi) / 2
        k = min(B, hi - lo) * 0.5                                           # 通用画法：矩形线框内画十字（GB/T 4459.7）
        ops += [c.line((cx - k / 2, cy), (cx + k / 2, cy), "outline"), c.line((cx, cy - k / 2), (cx, cy + k / 2), "outline")]
    ops.append(c.line((x - 3, y), (x + B + 3, y), "center"))
    return c.result(ops, f"轴承通用画法 d={d:g} D={D:g} B={B:g}", notes=["GB/T 4459.7 通用画法（矩形加十字），不区分轴承类型"])


# ------------------------------------------------------------------ 齿轮（端视图）
def gear_generator(p, ins, targets):
    m, z = c.need(p, "module", "teeth", hint="模数和齿数必须由用户给出")
    m = c.positive("module", m)
    z = int(z)
    if z < 6 or z > 500:
        raise GeomError("INVALID_ARGS", "齿数应在 6–500", "")
    d, da, df = m * z, m * (z + 2), m * (z - 2.5)
    ops = [c.circle(ins, da / 2, "outline", ["gear_tip"]), c.circle(ins, d / 2, "center", ["gear_pitch"]), c.circle(ins, df / 2, "thin", ["gear_root"])]
    if p.get("bore_diameter"):
        bd = c.positive("bore_diameter", p["bore_diameter"])
        if bd >= df:
            raise GeomError("INVALID_ARGS", "孔径不能超过齿根圆", "")
        ops.append(c.circle(ins, bd / 2, "outline", ["bore"]))
    ext = da / 2 + 3
    ops += [c.line((ins[0] - ext, ins[1]), (ins[0] + ext, ins[1]), "center"), c.line((ins[0], ins[1] - ext), (ins[0], ins[1] + ext), "center")]
    return c.result(ops, f"直齿圆柱齿轮端视图 m={m:g} z={z}：分度圆 {d:g}，齿顶圆 {da:g}，齿根圆 {df:g}", pitch_d=d, tip_d=da, root_d=df,
                    notes=["GB/T 4459.2 简化画法：齿顶圆粗实线、分度圆点画线、齿根圆细实线；标准正常齿 ha*=1, c*=0.25，不画齿形"])


TOOLS = {
    "centermark_centerline": (centermark_centerline, "target_entity_ids"),
    "shaft_generator": (shaft_generator, "segments"),
    "hole_thread_feature": (hole_thread_feature, "diameter"),
    "standard_fastener": (standard_fastener, "type,size,length(螺栓/螺钉)"),
    "flange_generator": (flange_generator, "outer_diameter,bolt_circle_diameter,hole_count,hole_diameter"),
    "bolt_circle": (bolt_circle, "diameter,count,hole_diameter"),
    "keyway": (keyway, "shaft_diameter"),
    "bearing_simplified": (bearing_simplified, "inner_diameter,outer_diameter,width"),
    "gear_generator": (gear_generator, "module,teeth"),
}
