"""仪表控制点（文章 3.2.4 节，表 2-12、2-13）。

- 图形：直径 10 mm 细实线圆，上半圆写字母组合代号，下半圆写工段序号（3～5 位数字，只能来自用户）；
- 安装位置（表 2-12）：就地（圆内无线）、嵌在管道中（圆骑在管线上，管线在圆内断开）、集中盘面（一条实线）、集中盘后（一条虚线）、
  就地盘面（两条实线）、就地盘后（两条虚线，扫描图看不清，类推，冲突清单 #10）；
- 控制点：仪表圆圈引线与设备或管道的连接点，细实线把圆圈连到连接点；圆圈放在连接点正上 / 下 / 左 / 右的空位，
  避开设备、管线、位号栏和别的仪表（管线上的文字后画，会让开仪表）；
- 字母组合按表 2-13 校验：表里有 → 通过；变量和功能字母都认识但组合表里没有 → 警告并给最接近的组合；解析不了 → 错误。
"""

from __future__ import annotations

import difflib
import math

from zhihui.ir import Drawing, Instrument

from .codes import table
from .standards import LY, text_h

DIAMETER = 10.0  # 图纸 mm（文章 3.2.4 节）


def check_letters(letters: str) -> tuple[str, str]:
    """返回 (级别, 说明)：级别 ok / warn / error。"""
    tab = table("instrument_letters")
    s = str(letters or "").strip()
    variables = {**tab["variables"], **{k: v["name"] for k, v in tab["extra_variables"].items()}}
    if s in tab["combinations"]:
        return "ok", f"{tab['variables'][_split(s, variables)[0]]} · {tab['functions'][_split(s, variables)[1]]}"
    var, func = _split(s, variables)
    if var is None:
        return "error", f"字母代号 {s!r} 的第一个字母不是被测变量（表 2-13：{'、'.join(f'{k} {v}' for k, v in variables.items())}）"
    if func in tab["functions"]:
        if var in tab["extra_variables"]:
            return "warn", f"{var}（{variables[var]}）不在表 2-13 里（verified=false），功能 {tab['functions'][func]}"
        return "ok", ""
    if func and set(func) <= set("ICASRTL"):
        near = difflib.get_close_matches(s, tab["combinations"], n=3, cutoff=0.4)
        return "warn", f"{s} 不在表 2-13 的组合里，最接近的：{'、'.join(near) or '无'}"
    return "error", f"字母代号 {s!r} 解析不了：功能字母只有 I 指示、C 控制、A 报警、S 开关、R 记录、T 变送、L 指示灯（表 2-13）"


def _split(s: str, variables: dict) -> tuple[str | None, str]:
    for v in sorted(variables, key=len, reverse=True):
        if s.startswith(v):
            return v, s[len(v):]
    return None, s


def anchors(d: Drawing, ins: Instrument) -> list[tuple[tuple[float, float], list[tuple[float, float]]]]:
    """候选控制点：连在管线上只有 at 处一个；连在设备上取本体顶边（中、偏左、偏右）和左右边中点，避开端口和管口。"""
    from .symbols import body_box, get_symbol

    c = d.get_connection(ins.attach_to)
    if c is not None:
        a = anchor(d, ins)
        return [a] if a else []
    e = d.get_equipment(ins.attach_to)
    if e is None or not e.placed:
        return []
    x0, y0, x1, y1 = body_box(e, d.unit)
    sym = get_symbol(e.symbol_id)
    ports = [(e.x + p.x * d.unit, e.y + p.y * d.unit) for p in sym.ports] + [(e.x + n["x"] * d.unit, e.y + n["y"] * d.unit) for n in sym.nozzles]
    w, h = x1 - x0, y1 - y0
    from .chem import nozzles_on
    from .symbols import DIRS

    noz = []  # 施工流程图：优先接到设备的仪表接口管口（文章 3.1.3 节）
    if nozzles_on(d):
        for n in sym.nozzles:
            if n["kind"] == "instrument":
                dx, dy = DIRS[n["dir"]]
                noz.append(((e.x + n["x"] * d.unit + dx * n["len"] * d.unit, e.y + n["y"] * d.unit + dy * n["len"] * d.unit), [(dx, dy)]))
    cands = [((x0 + w * f, y1), [(0, 1)]) for f in (0.5, 0.25, 0.75)] + [((x1, y0 + h * 0.6), [(1, 0)]), ((x0, y0 + h * 0.6), [(-1, 0)])]
    gap = 3 * d.unit
    return noz + [cp for cp in cands if all(abs(cp[0][0] - px) > gap or abs(cp[0][1] - py) > gap for px, py in ports)
            and all(not (abs(cp[0][0] - px) < 1e-6 and cp[1][0] == (0, 1)) for px, py in ports if py >= y1 - 1e-6)] or cands[:1]


def anchor(d: Drawing, ins: Instrument) -> tuple[tuple[float, float], list[tuple[float, float]]] | None:
    """控制点和候选方向：连在管线上取 at 处，连在设备上取本体顶边中点（再试右、左边中点）。"""
    from .symbols import body_box

    c = d.get_connection(ins.attach_to)
    if c is not None and c.path and len(c.path) >= 2:
        cum = [0.0]
        for a, b in zip(c.path, c.path[1:]):
            cum.append(cum[-1] + math.dist(a, b))
        s = max(0.0, min(1.0, ins.at)) * cum[-1]
        j = max(jj for jj in range(len(c.path) - 1) if cum[jj] <= s + 1e-9)
        j = min(j, len(c.path) - 2)
        a, b = c.path[j], c.path[j + 1]
        L = math.dist(a, b) or 1.0
        p = (a[0] + (b[0] - a[0]) * (s - cum[j]) / L, a[1] + (b[1] - a[1]) * (s - cum[j]) / L)
        horiz = abs(a[1] - b[1]) < 1e-6
        dirs = [(0, 1), (0, -1)] if horiz else [(-1, 0), (1, 0)]
        return p, dirs
    e = d.get_equipment(ins.attach_to)
    if e is None or not e.placed:
        return None
    x0, y0, x1, y1 = body_box(e, d.unit)
    return ((x0 + x1) / 2, y1), [(0, 1), (1, 0), (-1, 0)]


def layout(d: Drawing) -> list[dict]:
    """[{ins, c（圆心）, p（控制点）, r}]：确定性地给每个仪表找位置（按登记顺序，后面的让前面的）。"""
    from .symbols import footprint, inflate, label_area_ok
    from .water import _seg_in_box

    s = d.scale
    r = DIAMETER / 2 * s
    boxes = [inflate(footprint(e, d.unit, d.template_id), 1 * s) for e in d.equipment if e.placed]
    lines = [(a, b) for c in d.connections for a, b in zip(c.path or [], (c.path or [])[1:])]
    out = []
    for ins in d.instruments:
        if ins.location == "local_inline" and d.get_connection(ins.attach_to) is not None:
            # 嵌在管道中：圆骑在管线上；at 处压到别的管线或设备时沿管线往两边找
            own = d.get_connection(ins.attach_to)
            others = [(a, b) for cc in d.connections if cc.id != own.id for a, b in zip(cc.path or [], (cc.path or [])[1:])]
            pick = None
            for k in range(0, 21):
                for t in ((ins.at + k * 0.025), (ins.at - k * 0.025)):
                    if not 0.05 <= t <= 0.95:
                        continue
                    an = anchor(d, ins.model_copy(update={"at": t}))
                    if an is None:
                        continue
                    p = an[0]
                    box = (p[0] - r - 1 * s, p[1] - r - 1 * s, p[0] + r + 1 * s, p[1] + r + 1 * s)
                    if any(_seg_in_box(a, b, box) for a, b in others) or any(box[0] < o[2] and o[0] < box[2] and box[1] < o[3] and o[1] < box[3] for o in boxes):
                        continue
                    if any(abs(p[0] - q[0]) < 1e-6 or abs(p[1] - q[1]) < 1e-6 for q in own.path[1:-1] if math.dist(p, q) < r + 2 * s):
                        continue  # 不骑在拐角上
                    pick = p
                    break
                if pick:
                    break
            if pick is None:
                an = anchor(d, ins)
                if an is None:
                    continue
                pick = an[0]
            out.append({"ins": ins, "c": pick, "p": pick, "r": r, "inline": True})
            boxes.append((pick[0] - r - 2 * s, pick[1] - r - 2 * s, pick[0] + r + 2 * s, pick[1] + r + 2 * s))
            continue
        cps = anchors(d, ins)
        if not cps:
            continue

        def free(p, dx, dy, dist, off=0.0):
            c = (p[0] + dx * dist * s - dy * off * s, p[1] + dy * dist * s + dx * off * s)  # off：垂直于引出方向错开，引线斜着连
            box = (c[0] - r - 1 * s, c[1] - r - 1 * s, c[0] + r + 1 * s, c[1] + r + 1 * s)
            if not label_area_ok(d, box) or any(box[0] < o[2] and o[0] < box[2] and box[1] < o[3] and o[1] < box[3] for o in boxes):
                return None
            if any(_seg_in_box(a, b, box) for a, b in lines):
                return None
            L = math.dist(p, c) or 1.0
            lead = (p, (c[0] - (c[0] - p[0]) / L * r, c[1] - (c[1] - p[1]) / L * r))  # 引线不穿过设备（本体外扩后的范围只许碰到起点）
            if any(_seg_in_box(lead[0], lead[1], inflate(o, -1.5 * s)) for o in boxes):
                return None
            return c

        pick, p = None, cps[0][0]
        tries = [(dist, 0.0) for dist in (12, 16, 20, 25, 30, 36)] + [(dist, off) for dist in (12, 16, 20, 25) for off in (8, -8, 14, -14, 20, -20)]
        for dist, off in tries:  # 先正对、由近到远，再错开；同一档按候选控制点、方向的先后
            hit = next(((cp, c) for cp, dirs in cps for dx, dy in dirs for c in [free(cp, dx, dy, dist, off)] if c), None)
            if hit:
                p, pick = hit
                break
        if pick is None:
            p, dirs = cps[0]
            pick = (p[0] + dirs[0][0] * 12 * s, p[1] + dirs[0][1] * 12 * s)
        out.append({"ins": ins, "c": pick, "p": p, "r": r, "inline": False})
        boxes.append((pick[0] - r - 2 * s, pick[1] - r - 2 * s, pick[0] + r + 2 * s, pick[1] + r + 2 * s))
        lines.append((p, pick))
    return out


def boxes(d: Drawing) -> list[tuple[float, float, float, float]]:
    s = d.scale
    return [(it["c"][0] - it["r"] - 0.5 * s, it["c"][1] - it["r"] - 0.5 * s, it["c"][0] + it["r"] + 0.5 * s, it["c"][1] + it["r"] + 0.5 * s)
            for it in layout(d)]


def inline_cuts(d: Drawing, cid: str) -> list[tuple[tuple[float, float], float]]:
    """嵌在管道中的就地仪表：管线在圆内断开。"""
    return [(it["c"], it["r"]) for it in layout(d) if it["inline"] and it["ins"].attach_to == cid]


def draw(msp, d: Drawing) -> None:
    from ezdxf.enums import TextEntityAlignment as TA

    from . import dashsplit
    from .dxf_io import APPID, _text

    s = d.scale
    locs = table("instrument_letters")["locations"]
    for it in layout(d):
        ins, (cx, cy), r = it["ins"], it["c"], it["r"]
        xd = [(1000, "instrument"), (1000, ins.id)]
        msp.add_circle((cx, cy), r, dxfattribs={"layer": LY["instrument"]}).set_xdata(APPID, xd)
        loc = locs.get(ins.location, {})
        n, dashed = loc.get("lines", 0), loc.get("dashed", False)
        ys = [cy] if n == 1 else ([cy + 0.45 * s, cy - 0.45 * s] if n == 2 else [])
        for y in ys:  # 表 2-12：盘面一条 / 两条实线，盘后虚线
            hw = math.sqrt(max(r * r - (y - cy) ** 2, 0))
            pts = [(cx - hw, y), (cx + hw, y)]
            if dashed:
                dashsplit.draw(msp, d, pts, LY["hidden"], xdata=xd, part_xdata=lambda i: xd)
            else:
                msp.add_line(*pts, dxfattribs={"layer": LY["instrument"]}).set_xdata(APPID, xd)
        # 字母代号在上半圆，工段序号在下半圆；盘装仪表的横线居中，文字分在两侧
        th = 2.5 * s  # 字母数字 2.5 号（字高系列；HG/T 20519.1 第 6.2 条 2～3 mm），10 mm 的圆里放两行
        gap = (0.9 * s if n == 2 else 0.5 * s)
        _text(msp, ins.letters, (cx, cy + gap), th, LY["instrument"], TA.BOTTOM_CENTER).set_xdata(APPID, xd)
        if ins.loop_no:
            _text(msp, ins.loop_no, (cx, cy - gap), th, LY["instrument"], TA.TOP_CENTER).set_xdata(APPID, xd)
        if not it["inline"]:  # 引线：圆周 → 控制点（细实线）
            px, py = it["p"]
            L = math.dist((px, py), (cx, cy)) or 1.0
            e = (cx + (px - cx) / L * r, cy + (py - cy) / L * r)
            msp.add_line(e, (px, py), dxfattribs={"layer": LY["instrument"]}).set_xdata(APPID, xd)
        else:  # 嵌在管道中：圆两侧接管道（管线已在圆内断开）
            pass
