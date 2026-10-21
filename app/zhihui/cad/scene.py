"""前端交互视图用的场景 JSON：把通用图纸的图元变成浏览器能直接画的几何（SVG 路径 / 圆 / 文字 / 填充），
附带每个图元的捕捉点、夹点和包围盒，图层、绘图辅助、会话（选择集、上一点）、布局和约束。

坐标系：模型坐标（y 向上）。前端根节点用 scale(1,-1) 翻转，文字单独再翻回来。
"""
from __future__ import annotations

import math
import re

from ezdxf import colors as ezcolors

from zhihui.ir import Drawing
from zhihui.cad.geom import blocks as B
from zhihui.cad.geom import dims as D
from zhihui.cad.geom import grips as G
from zhihui.cad.geom import snap as S
from zhihui.cad.geom.entities import Arc, Seg, ang, dist, entity_bbox, polar, primitives, sample, transform_data

MAX_SNAP_ENTITIES = 3000
LT_DASH = {"DASHED": [6, 3], "HIDDEN": [3, 2], "CENTER": [14, 3, 3, 3], "DASHDOT": [8, 3, 1, 3], "DOT": [0.5, 3], "PHANTOM": [14, 3, 3, 3, 3, 3], "DIVIDE": [10, 3, 1, 3, 1, 3], "BORDER": [10, 3, 10, 3, 1, 3]}


def aci_hex(aci: int) -> str:
    if aci == 7:
        return "fg"                                            # 黑底显示白、白底显示黑，前端按背景决定
    try:
        r, g, b = ezcolors.aci2rgb(abs(int(aci))).rgb
        return f"#{r:02X}{g:02X}{b:02X}"
    except Exception:
        return "fg"


def resolve_color(d: Drawing, e) -> str:
    if e.color is not None:
        if isinstance(e.color, str) and e.color.startswith("#"):
            return e.color.upper()
        if isinstance(e.color, int) and e.color not in (0, 256):
            return aci_hex(e.color)
    l = d.layer_of(e)
    if l is not None:
        return l.true_color.upper() if l.true_color else aci_hex(l.color)
    return "fg"


def resolve_lw(d: Drawing, e) -> float:
    if e.lineweight_mm is not None:
        return e.lineweight_mm
    l = d.layer_of(e)
    return l.lineweight_mm if l is not None and l.lineweight_mm else 0.25


def resolve_lt(d: Drawing, e) -> str:
    lt = e.linetype
    if not lt:
        l = d.layer_of(e)
        lt = l.linetype if l else "Continuous"
    return (lt or "Continuous").upper()


def _f(v: float) -> str:
    return f"{v:.4f}".rstrip("0").rstrip(".") if abs(v) < 1e7 else f"{v:.0f}"


def path_of_prims(prims) -> str:
    out = []
    last = None
    for pr in prims:
        if isinstance(pr, Seg):
            a, b = pr.a, pr.b
            if last is None or dist(last, a) > 1e-6:
                out.append(f"M{_f(a[0])} {_f(a[1])}")
            out.append(f"L{_f(b[0])} {_f(b[1])}")
            last = b
        else:
            s, e_ = pr.start(), pr.end()
            if last is None or dist(last, s) > 1e-6:
                out.append(f"M{_f(s[0])} {_f(s[1])}")
            if pr.span >= 359.999:                              # 整圆拆成两个半圆（SVG 的 A 不能起点等于终点）
                m = polar(pr.c, pr.r, pr.a0 + 180)
                out.append(f"A{_f(pr.r)} {_f(pr.r)} 0 1 1 {_f(m[0])} {_f(m[1])}A{_f(pr.r)} {_f(pr.r)} 0 1 1 {_f(s[0])} {_f(s[1])}")
            else:
                out.append(f"A{_f(pr.r)} {_f(pr.r)} 0 {1 if pr.span > 180 else 0} 1 {_f(e_[0])} {_f(e_[1])}")
            last = e_
    return "".join(out)


def _poly_d(pts, close=False) -> str:
    if not pts:
        return ""
    s = f"M{_f(pts[0][0])} {_f(pts[0][1])}" + "".join(f"L{_f(p[0])} {_f(p[1])}" for p in pts[1:])
    return s + ("Z" if close else "")


def _arrow(tip, direction, size):
    """实心箭头：三角形（尖在 tip，朝 direction 方向指）。"""
    ux, uy = direction
    L = math.hypot(ux, uy) or 1.0
    ux, uy = ux / L, uy / L
    bx, by = tip[0] - ux * size, tip[1] - uy * size
    nx, ny = -uy * size / 6, ux * size / 6
    return [[tip[0], tip[1]], [bx + nx, by + ny], [bx - nx, by - ny]]


def _clean_dim_text(txt: str, meas_text: str) -> str:
    t = (txt or "<>").replace("<>", meas_text)
    t = t.replace("%%c", "⌀").replace("%%C", "⌀").replace("%%d", "°").replace("%%p", "±")
    t = re.sub(r"\{\\S([^^;]*)\^([^;]*);\}", lambda m: f" {m.group(1).strip()}/{m.group(2).strip() or '0'}", t)
    return re.sub(r"\\[A-Za-z][^;]*;|[{}]", "", t).strip()


def dim_items(d: Drawing, dd: dict) -> list[dict]:
    """尺寸的显示几何：界线、尺寸线、箭头、文字。"""
    st = next((s for s in d.dim_styles if s["name"] == dd.get("style")), None) or (d.dim_styles[0] if d.dim_styles else {"arrow_size": 2.5, "text_height": 2.5, "ext_offset": 0.625, "ext_beyond": 1.25, "precision": 2})
    k = float(st.get("dimscale", 1.0) or 1.0)                                   # 标注样式的全局比例（DIMSCALE）：1:10 视图的设备图 dimscale=10，预览里才看得见
    asz, th, eo, eb = st["arrow_size"] * k, st["text_height"] * k, st["ext_offset"] * k, st["ext_beyond"] * k
    prec = st.get("precision", 2)
    t = dd["dtype"]
    meas = dd.get("measurement", 0.0)
    prefix = "⌀" if t == "diameter" else "R" if t == "radius" else ""
    mt = prefix + D.fmt_measure(meas, prec) + ("°" if t == "angular" else "")
    text = _clean_dim_text(dd.get("text") or "", mt)
    lines: list[list] = []
    fills: list[list] = []
    items: list[dict] = []
    if t in ("linear_horizontal", "linear_vertical", "aligned"):
        p1, p2, loc = dd["p1"], dd["p2"], dd["loc"]
        if t == "linear_horizontal":
            a, b = (p1[0], loc[1]), (p2[0], loc[1])
            ext = [((p1[0], p1[1]), (p1[0], loc[1] + math.copysign(eb, loc[1] - p1[1]))), ((p2[0], p2[1]), (p2[0], loc[1] + math.copysign(eb, loc[1] - p2[1])))]
        elif t == "linear_vertical":
            a, b = (loc[0], p1[1]), (loc[0], p2[1])
            ext = [((p1[0], p1[1]), (loc[0] + math.copysign(eb, loc[0] - p1[0]), p1[1])), ((p2[0], p2[1]), (loc[0] + math.copysign(eb, loc[0] - p2[0]), p2[1]))]
        else:
            ux, uy = p2[0] - p1[0], p2[1] - p1[1]
            L = math.hypot(ux, uy) or 1.0
            nx, ny = -uy / L, ux / L
            off = (loc[0] - p1[0]) * nx + (loc[1] - p1[1]) * ny
            a, b = (p1[0] + nx * off, p1[1] + ny * off), (p2[0] + nx * off, p2[1] + ny * off)
            sg = 1 if off >= 0 else -1
            ext = [((p1[0], p1[1]), (a[0] + nx * eb * sg, a[1] + ny * eb * sg)), ((p2[0], p2[1]), (b[0] + nx * eb * sg, b[1] + ny * eb * sg))]
        lines += [[list(a), list(b)]] + [[list(e0), list(e1)] for e0, e1 in ext]
        u = (b[0] - a[0], b[1] - a[1])
        fills += [_arrow(a, (-u[0], -u[1]), asz), _arrow(b, u, asz)]
        mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        rot = math.degrees(math.atan2(u[1], u[0])) if t != "linear_vertical" else 90.0
        if rot > 90 or rot < -90:
            rot += 180
        items.append({"t": "text", "p": [mid[0], mid[1]], "s": text, "h": th, "rot": rot, "align": "center", "dy": th * 0.4})
    elif t in ("radius", "diameter"):
        c, r, loc = dd["center"], dd["radius"], dd["loc"]
        a = ang(c, loc)
        tip = polar(c, r, a)
        start = polar(c, r, a + 180) if t == "diameter" else c
        lines.append([[start[0], start[1]], [loc[0], loc[1]]])
        fills.append(_arrow(tip, (math.cos(math.radians(a)), math.sin(math.radians(a))), asz))
        if t == "diameter":
            fills.append(_arrow(start, (-math.cos(math.radians(a)), -math.sin(math.radians(a))), asz))
        items.append({"t": "text", "p": [loc[0], loc[1]], "s": text, "h": th, "rot": 0, "align": "left", "dy": th * 0.4})
    elif t == "angular":
        cx, cy = dd["center"][0], dd["center"][1]
        loc = dd["loc"]
        r = dist((cx, cy, 0), loc)
        a0, a1 = ang((cx, cy, 0), dd["l1"][1]), ang((cx, cy, 0), dd["l2"][1])
        span = (a1 - a0) % 360
        if span > 180:
            a0, a1 = a1, a0
            span = (a1 - a0) % 360
        arc = Arc((cx, cy, 0.0), r, a0, a0 + span)
        items.append({"t": "path", "d": path_of_prims([arc]), "fill": False})
        s0, s1 = math.radians(a0), math.radians(a0 + span)
        fills.append(_arrow(polar((cx, cy, 0), r, a0), (math.sin(s0), -math.cos(s0)), asz))
        fills.append(_arrow(polar((cx, cy, 0), r, a0 + span), (-math.sin(s1), math.cos(s1)), asz))
        m = polar((cx, cy, 0), r, a0 + span / 2)
        items.append({"t": "text", "p": [m[0], m[1]], "s": text, "h": th, "rot": 0, "align": "center", "dy": th * 0.6})
    else:
        loc = dd.get("loc") or dd.get("p1")
        items.append({"t": "text", "p": [loc[0], loc[1]], "s": text or mt, "h": th, "rot": 0, "align": "left", "dy": 0})
    if lines:
        items.insert(0, {"t": "path", "d": "".join(_poly_d(l) for l in lines), "fill": False})
    for f in fills:
        items.append({"t": "path", "d": _poly_d(f, True), "fill": True})
    return items


def items_of(d: Drawing, e, blocks: dict, depth: int = 0, transform=None) -> list[dict]:
    """一个图元 → 一组可画的项（path / circle / text / hatch）。块参照递归展开（最多 4 层）。"""
    k, dd = e.kind, e.data
    if transform is not None:
        dd = transform_data(k, dd, transform)
    class_ = type("E", (), {})()
    class_.id, class_.kind, class_.data, class_.layer, class_.tags = e.id, k, dd, e.layer, e.tags
    if k in ("line", "polyline", "arc", "mline", "spline", "ellipse", "ray", "xline"):
        if k in ("ray", "xline"):
            p, u = dd["p"], dd["dir"]
            L = 1e5
            a = (p[0] - (u[0] * L if k == "xline" else 0), p[1] - (u[1] * L if k == "xline" else 0))
            b = (p[0] + u[0] * L, p[1] + u[1] * L)
            return [{"t": "path", "d": _poly_d([a, b]), "fill": False, "infinite": True}]
        prs = primitives(class_)
        return [{"t": "path", "d": path_of_prims(prs), "fill": False}] if prs else []
    if k == "circle":
        return [{"t": "circle", "c": [dd["c"][0], dd["c"][1]], "r": dd["r"]}]
    if k == "point":
        return [{"t": "point", "p": [dd["p"][0], dd["p"][1]]}]
    if k in ("text", "mtext"):
        s = re.sub(r"\\[A-Za-z][^;]*;|[{}]", "", str(dd.get("text", ""))).replace("\\P", "\n")
        return [{"t": "text", "p": [dd["p"][0], dd["p"][1]], "s": s, "h": float(dd.get("height", 3.5)), "rot": float(dd.get("rotation", 0)), "align": dd.get("align", "left"), "width": dd.get("width") or 0}]
    if k == "dimension":
        if dd.get("dtype") == "foreign":
            return [{"t": "text", "p": [dd["loc"][0], dd["loc"][1]], "s": dd.get("text") or D.fmt_measure(dd.get("measurement", 0)), "h": 2.5, "rot": 0, "align": "left", "dy": 0}]
        return dim_items(d, dd)
    if k == "hatch":
        return [{"t": "hatch", "loops": [[[p[0], p[1]] for p in lp] for lp in dd.get("loops", [])], "pattern": dd.get("pattern", "ANSI31"), "angle": dd.get("angle", 0), "scale": dd.get("scale", 1.0), "solid": dd.get("pattern", "").upper() == "SOLID"}]
    if k in ("mesh", "solid3d"):
        from zhihui.cad.geom.solid import feature_edges
        V = dd.get("vertices", [])
        edges = feature_edges([tuple(v) for v in V], dd.get("faces", [])) if V else []                  # 只画棱线，不画三角剖分的对角线
        return [{"t": "path", "d": "".join(f"M{_f(V[a][0])} {_f(V[a][1])}L{_f(V[b][0])} {_f(V[b][1])}" for a, b in edges), "fill": False, "mesh": True}] if edges else []
    if k == "insert":
        blk = blocks.get(dd.get("block"))
        out: list[dict] = []
        if blk is None or depth >= 4:
            return [{"t": "point", "p": [dd["p"][0], dd["p"][1]]}]
        T = B.instance_transform(blk.get("base_point", (0, 0, 0)), dd["p"], dd.get("scale", (1, 1, 1)), float(dd.get("rotation", 0)))
        for be in blk.get("entities", []):
            sub = type("E", (), {})()
            sub.id, sub.kind, sub.data, sub.layer, sub.tags = f"{e.id}/{be.get('id', '')}", be["kind"], be["data"], be.get("layer", "0"), be.get("tags", [])
            out += items_of(d, sub, blocks, depth + 1, T)
        for ad in blk.get("attdefs", []):                                  # 属性值显示在属性定义的位置
            if ad.get("invisible"):
                continue
            val = dd.get("attribs", {}).get(ad["tag"], ad.get("default_val", ""))
            if val:
                pt = T(tuple(ad["position"]))
                out.append({"t": "text", "p": [pt[0], pt[1]], "s": str(val), "h": float(ad.get("height", 2.5)), "rot": float(dd.get("rotation", 0)), "align": "left"})
        return out
    if k in ("leader", "tolerance"):
        pts = dd.get("points") or [dd.get("p")]
        return [{"t": "path", "d": _poly_d([(p[0], p[1]) for p in pts]), "fill": False}]
    return []


def _snaps(e) -> list[list]:
    try:
        pts = S.entity_snap_points(e, ["endpoint", "midpoint", "center", "quadrant", "node", "insertion", "geometric_center"], None)
    except Exception:
        return []
    short = {"endpoint": "end", "midpoint": "mid", "center": "cen", "quadrant": "qua", "node": "nod", "insertion": "ins", "geometric_center": "gce"}
    out = []
    for p in pts:
        c = p["coord"]
        out.append([short.get(p["type"], p["type"]), round(c[0], 4), round(c[1], 4)])
    return out


def _grips(e) -> list[list]:
    try:
        return [[g["index"], g["role"], round(g["coord"][0], 4), round(g["coord"][1], 4)] for g in G.grips_of(e)]
    except Exception:
        return []


def build_scene(d: Drawing, space: str | None = None, with_snaps: bool = True) -> dict:
    space = space or d.active_space
    B.set_context(d.blocks)
    blocks = {b["name"]: b for b in d.blocks}
    layer_use: dict[str, int] = {}
    ents = []
    snaps_on = with_snaps and len(d.entities) <= MAX_SNAP_ENTITIES
    for e in d.entities:
        layer_use[e.layer] = layer_use.get(e.layer, 0) + 1
        if e.space != space:
            continue
        l = d.layer_of(e)
        items = items_of(d, e, blocks)
        bb = entity_bbox(e)
        rec = {"id": e.id, "k": e.kind, "layer": e.layer, "col": resolve_color(d, e), "lw": resolve_lw(d, e), "lt": resolve_lt(d, e), "lts": e.linetype_scale, "tags": e.tags,
               "hidden": bool(l and (not l.on or l.frozen)), "locked": bool(l and l.locked), "plot": bool(l.plot) if l else True, "items": items, "bb": [round(v, 4) for v in bb] if bb else None}
        if snaps_on:
            rec["sn"] = _snaps(e)
            rec["gr"] = _grips(e)
        ents.append(rec)
    return {
        "drawing_id": d.id, "revision": d.revision, "title": d.title, "mode": d.mode, "workspace": d.workspace, "space": space, "limits": list(d.limits), "background": d.background,
        "ltscale": d.ltscale * d.celtscale, "current_layer": d.current_layer, "current_text_style": d.current_text_style, "current_dim_style": d.current_dim_style,
        "layers": [{"name": l.name, "color": l.true_color or aci_hex(l.color), "aci": l.color, "linetype": l.linetype, "lineweight_mm": l.lineweight_mm, "on": l.on, "frozen": l.frozen, "locked": l.locked,
                    "plot": l.plot, "transparency": l.transparency, "count": layer_use.get(l.name, 0), "current": l.name == d.current_layer} for l in d.layers],
        "view": {"center": list(d.view.center), "height": d.view.height, "view3d": d.view.view3d, "visual_style": d.view.visual_style, "projection": d.view.projection, "orbit": d.view.orbit,
                 "named_views": list(d.view.named_views), "panels": d.view.panels},
        "draft": {"snap": d.draft.snap_on, "snap_spacing": list(d.draft.snap_spacing), "grid": d.draft.grid_on, "grid_spacing": list(d.draft.grid_spacing), "ortho": d.draft.ortho_on, "polar": d.draft.polar_on,
                  "polar_angle": d.draft.polar_angle_deg, "osnap": d.draft.osnap_on, "osnap_modes": d.draft.osnap_modes, "otrack": d.draft.otrack_on, "dyn": d.draft.dynmode_on},
        "session": {"selection": d.session.selection, "last_point": list(d.session.last_point) if d.session.last_point else None, "undo": len(d.session.history), "redo": len(d.session.redo)},
        "entities": ents,
        "layouts": [{"name": l["name"], "page_setup": l["page_setup"], "viewports": l["viewports"], "frame": l.get("frame")} for l in d.layouts],
        "constraints": d.constraints, "blocks": [b["name"] for b in d.blocks], "entity_count": len(d.entities),
        "bbox": [round(v, 3) for v in d.bbox()] if d.bbox() else None, "text_styles": [s["name"] for s in d.text_styles], "dim_styles": [s["name"] for s in d.dim_styles],
    }
