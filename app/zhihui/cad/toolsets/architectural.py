"""建筑制图工具集：轴网与墙体、门窗；明细表与零件序号（机械、建筑通用）。"""
from __future__ import annotations

import math
import re

from zhihui.cad.geom.entities import Transform, transform_data
from zhihui.cad.geom.errors import GeomError
from . import common as c

LETTERS = [ch for ch in "ABCDEFGHJKLMNPQRSTUVWXY"]                 # GB/T 50001：轴线编号字母不用 I、O、Z


def _spacings(name: str, v) -> list[float]:
    if not isinstance(v, list) or not v:
        raise GeomError("MISSING_ARG", f"缺少参数：{name}", f"{name} 是各轴线之间的间距列表（mm），必须由用户给出")
    return [c.positive(f"{name}[{i}]", x) for i, x in enumerate(v)]


def arch_grid_and_wall(p, ins, targets):
    xs_sp, ys_sp = _spacings("x_spacings", p.get("x_spacings")), _spacings("y_spacings", p.get("y_spacings"))
    if len(ys_sp) + 1 > len(LETTERS):
        raise GeomError("INVALID_ARGS", f"横向轴线最多 {len(LETTERS)} 条", "")
    x0, y0 = ins
    xs = [x0]
    for s in xs_sp:
        xs.append(xs[-1] + s)
    ys = [y0]
    for s in ys_sp:
        ys.append(ys[-1] + s)
    ext = c.positive("extension", p.get("extension", 1500.0))
    bd = c.positive("bubble_diameter", p.get("bubble_diameter", 800.0))
    th = bd * 0.5
    ops, notes = [], []
    if "extension" not in p:
        notes.append(f"轴线伸出长度默认 {ext:g}，编号圆直径默认 {bd:g}（模型单位 mm；1:100 出图时圆约 8 mm）")
    for i, x in enumerate(xs):
        ops.append(c.line((x, ys[0] - ext), (x, ys[-1] + ext), "center", ["grid", f"grid_x{i + 1}"]))
        cy = ys[0] - ext - bd / 2
        ops.append(c.circle((x, cy), bd / 2, "thin", ["grid_bubble"]))
        ops.append(c.text((x, cy - th / 2), str(i + 1), th, "text", ["grid_label"], align="center"))
    for j, y in enumerate(ys):
        ops.append(c.line((xs[0] - ext, y), (xs[-1] + ext, y), "center", ["grid", f"grid_y{LETTERS[j]}"]))
        cx = xs[0] - ext - bd / 2
        ops.append(c.circle((cx, y), bd / 2, "thin", ["grid_bubble"]))
        ops.append(c.text((cx, y - th / 2), LETTERS[j], th, "text", ["grid_label"], align="center"))
    node = lambda lab: _node(lab, xs, ys)
    walls = p.get("walls") or []
    for k, w in enumerate(walls):
        if w.get("thickness") is None:
            raise GeomError("MISSING_ARG", f"墙 {k + 1} 缺少 thickness", "墙厚必须由用户给出")
        t = c.positive(f"walls[{k}].thickness", w["thickness"])
        a, b = node(w["from"]), node(w["to"])
        L = math.hypot(b[0] - a[0], b[1] - a[1])
        if L < 1e-6:
            raise GeomError("INVALID_ARGS", f"墙 {k + 1} 起止点重合", "")
        u = ((b[0] - a[0]) / L, (b[1] - a[1]) / L)
        n = (-u[1], u[0])
        just = w.get("justification", "center")
        off = {"center": (t / 2, -t / 2), "left": (t, 0.0), "right": (0.0, -t)}[just]
        p1a, p1b = (a[0] + n[0] * off[0], a[1] + n[1] * off[0]), (b[0] + n[0] * off[0], b[1] + n[1] * off[0])
        p2a, p2b = (a[0] + n[0] * off[1], a[1] + n[1] * off[1]), (b[0] + n[0] * off[1], b[1] + n[1] * off[1])
        ops += [c.line(p1a, p1b, "outline", ["wall", f"wall{k + 1}"]), c.line(p2a, p2b, "outline", ["wall", f"wall{k + 1}"]),
                c.line(p1a, p2a, "outline", ["wall", f"wall{k + 1}"]), c.line(p1b, p2b, "outline", ["wall", f"wall{k + 1}"])]
    return c.result(ops, f"轴网 {len(xs)}×{len(ys)} 条轴线（横向 1–{len(xs)}，纵向 A–{LETTERS[len(ys) - 1]}）" + (f"，{len(walls)} 段墙" if walls else ""), x_axes=xs, y_axes=ys,
                    labels={"x": [str(i + 1) for i in range(len(xs))], "y": LETTERS[:len(ys)]}, notes=notes)


def _node(ref, xs, ys):
    if isinstance(ref, (list, tuple)) and len(ref) >= 2:
        return (float(ref[0]), float(ref[1]))
    m = re.fullmatch(r"\s*([A-Za-z])\s*(\d+)\s*", str(ref))
    if not m:
        raise GeomError("INVALID_ARGS", f"墙端点 {ref!r} 写法不对", "写坐标 [x,y]，或轴线交点如 A1（字母=横轴线，数字=竖轴线）")
    j, i = LETTERS.index(m.group(1).upper()) if m.group(1).upper() in LETTERS else -1, int(m.group(2)) - 1
    if not (0 <= i < len(xs) and 0 <= j < len(ys)):
        raise GeomError("INVALID_ARGS", f"轴线交点 {ref} 不存在", f"竖轴线 1–{len(xs)}，横轴线 A–{LETTERS[len(ys) - 1]}")
    return (xs[i], ys[j])


def door_window(p, ins, targets):
    kind, width, wt = c.need(p, "kind", "width", "wall_thickness", hint="门窗类型、洞口宽度和墙厚必须由用户给出")
    if kind not in ("door", "window"):
        raise GeomError("INVALID_ARGS", "kind 必须是 door 或 window", "")
    W, T = c.positive("width", width), c.positive("wall_thickness", wt)
    rot = float(p.get("rotation_deg", 0.0))
    swing = p.get("swing", "left")
    side = p.get("side", "up")
    sgn = 1 if side == "up" else -1
    ops: list[dict] = []
    h = W / 2
    ops += [c.line((-h, -T / 2), (-h, T / 2), "outline", ["jamb"]), c.line((h, -T / 2), (h, T / 2), "outline", ["jamb"])]
    notes = []
    if kind == "window":
        for yy in (-T / 2, -T / 6, T / 6, T / 2):
            ops.append(c.line((-h, yy), (h, yy), "thin", ["window"]))
    else:
        leaf_t = 40.0 if W > 300 else max(W * 0.05, 1.0)
        if swing in ("left", "right"):
            hx = -h if swing == "left" else h
            ops.append(c.rect(hx - leaf_t / 2, 0, hx + leaf_t / 2, sgn * W, "thin", ["door_leaf"]))
            if sgn == 1:
                a0, a1 = (0.0, 90.0) if swing == "left" else (90.0, 180.0)
            else:
                a0, a1 = (270.0, 360.0) if swing == "left" else (180.0, 270.0)
            ops.append(c.arc((hx, 0), W, a0, a1, "thin", ["door_swing"]))
        elif swing == "both":
            for hx, left in ((-h, True), (h, False)):
                ops.append(c.rect(hx - leaf_t / 2, 0, hx + leaf_t / 2, sgn * W / 2, "thin", ["door_leaf"]))
                a0, a1 = ((0.0, 90.0) if left else (90.0, 180.0)) if sgn == 1 else ((270.0, 360.0) if left else (180.0, 270.0))
                ops.append(c.arc((hx, 0), W / 2, a0, a1, "thin", ["door_swing"]))
        else:
            ops.append(c.line((-h, 0), (h, 0), "thin", ["door_opening"]))
        if "swing" not in p:
            notes.append("开启方向未指定，按左开（铰链在左端，向上开）")
    T_ = Transform.rotation((0, 0), rot).then(Transform.translation(ins[0], ins[1]))
    ops = [{**o, "data": transform_data(o["kind"], o["data"], T_)} for o in ops]
    cx, cy = ins
    a = math.radians(rot)
    ux, uy = math.cos(a), math.sin(a)
    nx, ny = -uy, ux
    opening = [(cx - ux * h - nx * T / 2, cy - uy * h - ny * T / 2), (cx + ux * h - nx * T / 2, cy + uy * h - ny * T / 2), (cx + ux * h + nx * T / 2, cy + uy * h + ny * T / 2), (cx - ux * h + nx * T / 2, cy - uy * h + ny * T / 2)]
    return c.result(ops, f"{'门' if kind == 'door' else '窗'} 洞口宽 {W:g}，墙厚 {T:g}，转角 {rot:g}°", opening=opening, notes=notes)


# ------------------------------------------------------------------ 明细表与序号球
BOM_COLS = [("序号", 10.0, "no"), ("代号", 40.0, "code"), ("名称", 50.0, "name"), ("数量", 10.0, "qty"), ("材料", 40.0, "material"), ("备注", 30.0, "remark")]


def bom_balloon_and_table(p, ins, targets):
    items, = c.need(p, "items", hint="明细栏的内容（序号、名称、数量…）只能来自用户：items=[{no,name,qty,…}]")
    if not isinstance(items, list) or not items:
        raise GeomError("INVALID_ARGS", "items 必须是非空列表", "")
    seen = set()
    for it in items:
        if not it.get("name") or it.get("no") in (None, ""):
            raise GeomError("MISSING_ARG", f"明细行缺少 no 或 name：{it}", "每行至少需要序号和名称")
        if it["no"] in seen:
            raise GeomError("INVALID_ARGS", f"序号 {it['no']} 重复", "")
        seen.add(it["no"])
    x0, y0 = ins
    rh, th = 8.0, 3.5
    total = sum(w for _, w, _ in BOM_COLS)
    nrows = len(items) + 1
    ops = []
    xs = [x0]
    for _, w, _ in BOM_COLS:
        xs.append(xs[-1] + w)
    for r in range(nrows + 1):
        ops.append(c.line((x0, y0 + r * rh), (x0 + total, y0 + r * rh), "outline" if r in (0, nrows) else "thin", ["bom"]))
    for xx in xs:
        ops.append(c.line((xx, y0), (xx, y0 + nrows * rh), "outline" if xx in (xs[0], xs[-1]) else "thin", ["bom"]))
    ordered = sorted(items, key=lambda it: str(it["no"]).zfill(6))
    for r, it in enumerate(ordered):                                         # 自下而上，序号小的在下
        yy = y0 + r * rh + rh / 2 - th / 2
        for (label, w, key), xa in zip(BOM_COLS, xs):
            v = str(it.get(key, "") if it.get(key) is not None else "")
            if v:
                hh = c.fit_h(v, w, th)
                ops.append(c.text((xa + 1.0, y0 + r * rh + rh / 2 - hh / 2), v, hh, "text", ["bom"]))
    for (label, w, key), xa in zip(BOM_COLS, xs):
        hh = c.fit_h(label, w, th)
        ops.append(c.text((xa + w / 2 - c.text_width(label, hh) / 2, y0 + len(items) * rh + rh / 2 - hh / 2), label, hh, "text", ["bom"]))
    balloons = p.get("balloons") or []
    off = p.get("balloon_offset") or [12.0, 12.0]
    for b in balloons:
        no, tgt = b.get("no"), b.get("target")
        if no not in seen:
            raise GeomError("INVALID_ARGS", f"序号球 {no} 在明细表里没有对应行", "")
        if not (isinstance(tgt, (list, tuple)) and len(tgt) >= 2):
            raise GeomError("INVALID_ARGS", f"序号球 {no} 缺少 target 点 [x,y]", "")
        tx, ty = float(tgt[0]), float(tgt[1])
        cx, cy = tx + float(off[0]), ty + float(off[1])
        R = 4.0
        L = math.hypot(cx - tx, cy - ty)
        u = ((cx - tx) / L, (cy - ty) / L)
        ops += [c.circle((cx, cy), R, "thin", ["balloon"]), c.line((tx, ty), (cx - u[0] * R, cy - u[1] * R), "thin", ["balloon"]), c.circle((tx, ty), 0.6, "outline", ["balloon"]),
                c.text((cx - len(str(no)) * th * 0.65 / 2, cy - th / 2), str(no), th, "text", ["balloon"])]
    return c.result(ops, f"明细表 {len(items)} 行" + (f"，{len(balloons)} 个序号球" if balloons else ""), table_bbox=[x0, y0, x0 + total, y0 + nrows * rh],
                    notes=["明细栏自下而上排列、表头在最上（GB/T 10609.2 的接法：紧靠标题栏上方）；列宽 10/40/50/10/40/30"])


TOOLS = {
    "arch_grid_and_wall": (arch_grid_and_wall, "x_spacings,y_spacings"),
    "door_window": (door_window, "kind,width,wall_thickness"),
    "bom_balloon_and_table": (bom_balloon_and_table, "items"),
}
