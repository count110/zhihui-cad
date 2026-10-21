"""阀门、管件、管道附件（文章表 2-11、3.2.3 节）：不是设备，不编位号，记在 Connection.fittings；出图时在管线上断开并插入符号。

符号库 fittings.json：坐标是阀门框 6×3 时的图纸 mm，管线沿 +x。配置 hg_pfd.fittings.valve_mm 选 6×3 或 8×4（文章：阀门长 6 宽 3，或长 8 宽 4）。
四种放法：
  inline  串在管线上，管线在 [-cut, cut] 断开（阀门、过滤器、视镜……）；
  across  跨在管线上，断口很短（盲板、法兰）；
  corner  角式阀门，放在离 at 最近的拐角：来向沿 +x 到拐角，去向沿 -y；
  end     管端（管帽、放空管、漏斗……），at < 0.5 放起点、否则放终点，符号朝管线外。
法兰、螺纹管帽这类管道之间的连接件一般不画，为安装检修加的才画（for_maintenance=true，文章 3.2.3 节）。
旧图里当作设备的 valve 图元用 migrate_valves 迁移成管件：一进一出的 valve 设备与两条连接合并成一条带闸阀的连接，
「阀门折叠」拓扑校验码（Drawing.topology_hash(fold_valves=True)）迁移前后相同。
"""

from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path

from zhihui.ir import Connection, Drawing, Fitting

from .standards import LY, for_drawing, text_h

LIB = Path(__file__).with_name("fittings.json")
MARGIN = 1.5  # 管件离拐角、端口至少留的直管（图纸 mm）


@lru_cache(maxsize=1)
def library() -> dict:
    return json.loads(LIB.read_text(encoding="utf-8"))["fittings"]


def resolve(name: str) -> str | None:
    """管件类型：id、中文名或别名都认。"""
    n = str(name or "").strip()
    lib = library()
    if n in lib:
        return n
    for fid, f in lib.items():
        if n == f["name_cn"] or n in f.get("aliases", []):
            return fid
    return None


def scale(d: Drawing) -> float:
    """符号库坐标 → 模型 mm：阀门框长度 / 6 × 出图比例。"""
    vl = for_drawing(d).raw.get("fittings", {}).get("valve_mm", [6, 3])[0]
    return vl / 6.0 * d.scale


def _cum(path) -> list[float]:
    out = [0.0]
    for a, b in zip(path, path[1:]):
        out.append(out[-1] + math.dist(a, b))
    return out


def _target(d: Drawing, at, total: float) -> float:
    if isinstance(at, (int, float)):
        return max(0.0, min(1.0, float(at))) * total
    s = str(at).replace(" ", "")
    for key, base, sign in (("from", 0.0, 1), ("to", total, -1)):
        if s.startswith(key):
            try:
                return base + sign * abs(float(s[len(key):])) * d.scale
            except ValueError:
                break
    return 0.5 * total


def placements(d: Drawing, c: Connection) -> list[dict]:
    """每个管件的位置：{i, type, kind, p, u（来向单位向量）, v（corner 的去向）, cuts: [(中心点, 半长)]}；放不下的跳过。"""
    path = c.path or []
    if len(path) < 2 or not c.fittings:
        return []
    k = scale(d)
    m = MARGIN * d.scale
    cum = _cum(path)
    total = cum[-1]
    lib = library()
    taken: list[tuple[float, float]] = []  # 已占用的沿线区间
    out = []
    for i, f in enumerate(c.fittings):
        fd = lib.get(f.type)
        if fd is None:
            continue
        kind, h = fd["kind"], fd["cut"] * k
        s = _target(d, f.at, total)
        if kind == "end":
            at_end = not (isinstance(f.at, (int, float)) and float(f.at) < 0.5) and not str(f.at).startswith("from")
            a, b = (path[-2], path[-1]) if at_end else (path[1], path[0])
            L = math.dist(a, b) or 1.0
            out.append({"i": i, "type": f.type, "kind": kind, "p": b, "u": ((b[0] - a[0]) / L, (b[1] - a[1]) / L), "cuts": []})
            continue
        if kind == "corner":
            if len(path) < 3:
                continue
            j = min(range(1, len(path) - 1), key=lambda j: abs(cum[j] - s))
            a, p, b = path[j - 1], path[j], path[j + 1]
            La, Lb = math.dist(a, p), math.dist(p, b)
            if La < h + m or Lb < h + m:
                continue
            u = ((p[0] - a[0]) / La, (p[1] - a[1]) / La)
            v = ((b[0] - p[0]) / Lb, (b[1] - p[1]) / Lb)
            out.append({"i": i, "type": f.type, "kind": kind, "p": p, "u": u, "v": v,
                        "cuts": [((p[0] - u[0] * h / 2, p[1] - u[1] * h / 2), h / 2), ((p[0] + v[0] * h / 2, p[1] + v[1] * h / 2), h / 2)]})
            taken.append((cum[j] - h, cum[j] + h))
            continue
        # inline / across：落在某段直管中间，离两端至少 m；和本管线上已放的管件不重叠
        ivs = [(cum[j] + h + m, cum[j + 1] - h - m) for j in range(len(path) - 1) if cum[j + 1] - cum[j] >= 2 * (h + m)]
        if not ivs:
            continue
        cands = sorted({min(max(s, lo), hi) for lo, hi in ivs} | {lo for lo, _ in ivs} | {hi for _, hi in ivs}, key=lambda t: abs(t - s))
        step = max(h, 1.0 * d.scale)
        pick = None
        for t0 in cands:
            for kk in range(0, 40):
                for t in (t0 + kk * step, t0 - kk * step):
                    if not any(lo - 1e-6 <= t <= hi + 1e-6 for lo, hi in ivs):
                        continue
                    if any(t - h - m < b and a < t + h + m for a, b in taken):
                        continue
                    pick = t
                    break
                if pick is not None:
                    break
            if pick is not None:
                break
        if pick is None:
            continue
        j = max(jj for jj in range(len(path) - 1) if cum[jj] <= pick + 1e-9)
        a, b = path[j], path[j + 1]
        L = cum[j + 1] - cum[j]
        u = ((b[0] - a[0]) / L, (b[1] - a[1]) / L)
        p = (a[0] + u[0] * (pick - cum[j]), a[1] + u[1] * (pick - cum[j]))
        out.append({"i": i, "type": f.type, "kind": kind, "p": p, "u": u, "cuts": [(p, h)] if h > 0 else []})
        taken.append((pick - h, pick + h))
    return out


def apply_cuts(pieces: list[list[tuple[float, float]]], cuts: list[tuple[tuple[float, float], float]]) -> list[list[tuple[float, float]]]:
    """在分段折线上按 (中心点, 半长) 断开：中心点所在的直段两侧各去掉半长。"""
    for (cx, cy), h in cuts:
        nxt = []
        for pts in pieces:
            done = False
            for q in range(len(pts) - 1):
                a, b = pts[q], pts[q + 1]
                L = math.dist(a, b)
                if L < 1e-9:
                    continue
                ux, uy = (b[0] - a[0]) / L, (b[1] - a[1]) / L
                t = (cx - a[0]) * ux + (cy - a[1]) * uy
                if abs((cx - a[0]) * uy - (cy - a[1]) * ux) > 1e-6 or not (-1e-6 <= t <= L + 1e-6):
                    continue
                left = pts[:q + 1] + ([(a[0] + ux * (t - h), a[1] + uy * (t - h))] if t - h > 1e-6 else [])
                right = ([(a[0] + ux * (t + h), a[1] + uy * (t + h))] if t + h < L - 1e-6 else []) + pts[q + 1:]
                for part in (left, right):
                    if len(part) >= 2:
                        nxt.append(part)
                done = True
                break
            if not done:
                nxt.append(pts)
        pieces = nxt
    return pieces


def _frame(pl: dict):
    """局部坐标 → 模型坐标的变换：x 轴沿管线（u），y 轴为 u 逆时针转 90°；corner 的 -y 轴为去向 v。"""
    ux, uy = pl["u"]
    if pl["kind"] == "corner":
        vx, vy = pl["v"]
        yx, yy = -vx, -vy
    else:
        yx, yy = -uy, ux
    return ux, uy, yx, yy


def boxes(d: Drawing, c: Connection) -> list[tuple[float, float, float, float]]:
    """管件占的范围（文字要让开）。"""
    k = scale(d)
    out = []
    for pl in placements(d, c):
        ux, uy, yx, yy = _frame(pl)
        xs, ys = [], []
        for g in library()[pl["type"]]["graphics"]:
            pts = g.get("points") or ([[g["c"][0] - g["r"], g["c"][1] - g["r"]], [g["c"][0] + g["r"], g["c"][1] + g["r"]]] if "c" in g else [])
            for x, y in pts:
                xs.append(pl["p"][0] + (x * ux + y * yx) * k)
                ys.append(pl["p"][1] + (x * uy + y * yy) * k)
        if xs:
            out.append((min(xs), min(ys), max(xs), max(ys)))
    return out


def draw(msp, d: Drawing, c: Connection) -> None:
    """画管件符号（细实线，文章 3.2.3 节），XDATA ["fitting", cid, 序号, 类型]；异径管的规格写在上方。"""
    from ezdxf.enums import TextEntityAlignment as TA

    from .dxf_io import APPID, _text

    k = scale(d)
    lay = LY["valve"]
    for pl in placements(d, c):
        ux, uy, yx, yy = _frame(pl)
        px, py = pl["p"]

        def T(x, y):
            return (px + (x * ux + y * yx) * k, py + (x * uy + y * yy) * k)

        xd = [(1000, "fitting"), (1000, c.id), (1000, str(pl["i"])), (1000, pl["type"])]
        rot = math.degrees(math.atan2(uy, ux))
        mirror = (ux * yy - uy * yx) < 0  # corner 的局部坐标可能是镜像
        for g in library()[pl["type"]]["graphics"]:
            t = g["type"]
            if t == "polyline":
                msp.add_lwpolyline([T(x, y) for x, y in g["points"]], close=g.get("closed", False), dxfattribs={"layer": lay}).set_xdata(APPID, xd)
            elif t == "circle":
                cc, r = T(*g["c"]), g["r"] * k
                if g.get("fill"):
                    h = msp.add_hatch(color=256, dxfattribs={"layer": lay})
                    h.set_solid_fill(color=256)
                    h.paths.add_edge_path().add_arc(cc, r, 0, 360)
                    h.set_xdata(APPID, xd)
                msp.add_circle(cc, r, dxfattribs={"layer": lay}).set_xdata(APPID, xd)
            elif t == "arc":
                a0, a1 = g["start"], g["end"]
                if mirror:
                    a0, a1 = -a1, -a0
                msp.add_arc(T(*g["c"]), g["r"] * k, a0 + rot, a1 + rot, dxfattribs={"layer": lay}).set_xdata(APPID, xd)
            elif t == "fill":
                h = msp.add_hatch(color=256, dxfattribs={"layer": lay})
                h.set_solid_fill(color=256)
                h.paths.add_polyline_path([T(x, y) for x, y in g["points"]], is_closed=True)
                h.set_xdata(APPID, xd)
        f = c.fittings[pl["i"]]
        if f.size_class:  # 异径管：「大端公称直径×小端公称直径」（文章 3.2 节），写在符号上方
            p = T(0, 2.2)
            _text(msp, f.size_class, p, text_h(d, "pipe_code", f.size_class), LY["pipe_text"], TA.BOTTOM_CENTER).set_xdata(APPID, xd)


def check(f: dict) -> Fitting:
    """校验并规范一项管件输入；类型不认识或法兰没标 for_maintenance 时抛 ValueError（调用方转成 E_BAD_ARG）。"""
    fid = resolve(f.get("type", ""))
    if fid is None:
        raise ValueError(f"管件类型 {f.get('type')!r} 不认识；可选：" + "、".join(f"{k}（{v['name_cn']}）" for k, v in library().items()))
    fd = library()[fid]
    if fd.get("maintenance_only") and not f.get("for_maintenance"):
        raise ValueError(f"{fd['name_cn']}是管道之间的连接件，一般不画；为安装、检修加的才画，请传 for_maintenance=true（文章 3.2.3 节）")
    at = f.get("at", 0.5)
    if isinstance(at, str):
        s = at.replace(" ", "")
        try:
            at = float(s)
        except ValueError:
            if not (s.startswith(("from", "to")) and s[4 if s.startswith("from") else 2:].lstrip("+-").replace(".", "", 1).isdigit()):
                raise ValueError(f"at={at!r} 不对：0～1 的比例，或「from+15」「to-15」（距端口的图纸 mm）")
            at = s
    return Fitting(type=fid, at=at, size_class=str(f.get("size_class") or "").strip()[:20], for_maintenance=bool(f.get("for_maintenance")))


def migrate_valves(d: Drawing) -> dict:
    """把当作设备的 valve 图元迁移成管件：一进一出的 valve 和两条连接合并成一条带闸阀的连接（前一条连接的 id、介质和标注保留）。"""
    before = {"topology": d.topology_hash(), "topology_folded": d.topology_hash(fold_valves=True)}
    merged, skipped = [], []
    for v in [e for e in d.equipment if e.symbol_id == "valve"]:
        ins = [c for c in d.connections if c.to_equip == v.id]
        outs = [c for c in d.connections if c.from_equip == v.id]
        if len(ins) != 1 or len(outs) != 1:
            skipped.append({"tag": v.tag, "reason": f"进 {len(ins)} 根、出 {len(outs)} 根管线，只迁移一进一出的阀门"})
            continue
        a, b = ins[0], outs[0]
        at = 0.5
        path = None
        if a.path and b.path:
            path = list(a.path) + list(b.path)
            la, lb = _cum(a.path)[-1], _cum(b.path)[-1]
            at = round((la + math.dist(a.path[-1], b.path[0]) / 2) / max(la + lb + math.dist(a.path[-1], b.path[0]), 1e-9), 4)
        from .routing import simplify

        new = a.model_copy(update={
            "to_equip": b.to_equip, "to_port": b.to_port, "path": simplify(path) if path else None,
            "medium": a.medium or b.medium, "fittings": [*a.fittings, Fitting(type="gate_valve", at=at), *b.fittings],
            **{k: getattr(b, k) for k in ("pipe_no", "od", "wall", "material", "grade", "code", "spec") if not getattr(a, k) and getattr(b, k)},
        })
        d.connections[d.connections.index(a)] = new
        d.connections.remove(b)
        d.equipment.remove(v)
        if v.id in d.auto_tags:
            d.auto_tags.remove(v.id)
        merged.append({"valve": v.tag, "name": v.name, "into": new.id, "removed_connection": b.id, "fitting": "gate_valve 闸阀"})
    after = {"topology": d.topology_hash(), "topology_folded": d.topology_hash(fold_valves=True)}
    return {"migrated": merged, "skipped": skipped, "before": before, "after": after,
            "folded_unchanged": before["topology_folded"] == after["topology_folded"]}
