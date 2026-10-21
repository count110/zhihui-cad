"""夹点：每种图元的夹点定义，以及拖动夹点（拉伸/移动/旋转/缩放/镜像）后的新 data。"""
from __future__ import annotations

import copy
import math
import re

from . import curves
from .entities import EPS, P, Pt, Transform, ang, dist, pl, polar, primitives, transform_data
from .errors import GeomError


def _g(i: int, role: str, p) -> dict:
    return {"index": i, "role": role, "coord": [round(float(p[0]), 6), round(float(p[1]), 6), round(float(p[2]) if len(p) > 2 else 0.0, 6)]}


def grips_of(e) -> list[dict]:
    k, d = e.kind, e.data
    out: list[dict] = []
    if k == "line":
        s, t = P(d["p1"]), P(d["p2"])
        out = [_g(0, "start", s), _g(1, "end", t), _g(2, "mid", ((s[0] + t[0]) / 2, (s[1] + t[1]) / 2, 0.0))]
    elif k == "circle":
        c, r = P(d["c"]), float(d["r"])
        out = [_g(0, "center", c)] + [_g(i + 1, f"quadrant_{q}", polar(c, r, q)) for i, q in enumerate((0, 90, 180, 270))]
    elif k == "arc":
        c, r = P(d["c"]), float(d["r"])
        pr = primitives(e)[0]
        out = [_g(0, "start", pr.start()), _g(1, "end", pr.end()), _g(2, "mid", curves.midpoint(pr)), _g(3, "center", c)]
    elif k == "ellipse":
        c = P(d["c"])
        mx, my, rt = d["major"][0], d["major"][1], float(d.get("ratio", 1))
        out = [_g(0, "center", c), _g(1, "major_end", (c[0] + mx, c[1] + my, 0)), _g(2, "major_start", (c[0] - mx, c[1] - my, 0)),
               _g(3, "minor_end", (c[0] - my * rt, c[1] + mx * rt, 0)), _g(4, "minor_start", (c[0] + my * rt, c[1] - mx * rt, 0))]
    elif k == "polyline":
        vs = [P(v["p"]) for v in d["vertices"]]
        out = [_g(i, f"vertex_{i}", p) for i, p in enumerate(vs)]
        n = len(vs)
        for i in range(n if d.get("closed") else n - 1):
            a, b = vs[i], vs[(i + 1) % n]
            out.append(_g(n + i, f"mid_{i}", ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2, 0.0)))
    elif k in ("mline", "leader"):
        out = [_g(i, f"vertex_{i}", P(p)) for i, p in enumerate(d["points"])]
    elif k == "spline":
        src = d.get("fit") or d.get("cv") or []
        out = [_g(i, f"vertex_{i}", P(p)) for i, p in enumerate(src)]
    elif k in ("text", "mtext", "insert", "tolerance", "point"):
        out = [_g(0, "insertion" if k != "point" else "node", P(d["p"]))]
    elif k == "dimension":
        for i, key in enumerate(("p1", "p2", "loc", "center")):
            if d.get(key) is not None:
                out.append(_g(i, key, P(d[key])))
    elif k in ("ray", "xline"):
        out = [_g(0, "base", P(d["p"]))]
    return out


def find_grip(e, ref) -> dict:
    gs = grips_of(e)
    if isinstance(ref, int) or (isinstance(ref, str) and ref.strip().lstrip("-").isdigit()):
        i = int(ref)
        for g in gs:
            if g["index"] == i:
                return g
    key = str(ref).strip().lower()
    key = {"insert": "insertion", "ins": "insertion", "base": "insertion", "position": "insertion", "midpoint": "mid", "middle": "mid", "begin": "start", "p1": "start", "p2": "end", "centre": "center"}.get(key, key)
    for g in gs:
        if g["role"] == key:
            return g
    m = re.fullmatch(r"(vertex|v)_?(\d+)", key)
    if m:
        for g in gs:
            if g["role"] == f"vertex_{m.group(2)}":
                return g
    m = re.fullmatch(r"quad(?:rant)?_?(\d+)", key)
    if m:
        for g in gs:
            if g["role"] == f"quadrant_{m.group(1)}":
                return g
    raise GeomError("GRIP_NOT_FOUND", f"{e.id} 没有夹点 {ref}", f"可用夹点：{[g['role'] for g in gs]}", grips=gs)


def stretch_grip(e, grip: dict, new_pos: Pt) -> dict:
    """拖动夹点：线端点/多段线顶点移动；圆心整体移动；圆象限点改半径；圆弧端点改角度；文字插入点移动……返回新 data。"""
    k, d = e.kind, copy.deepcopy(e.data)
    role, idx = grip["role"], grip["index"]
    np_ = pl(new_pos)
    if k == "line":
        if role == "start":
            d["p1"] = np_
        elif role == "end":
            d["p2"] = np_
        else:                                                     # 中点：整体平移
            return transform_data(k, e.data, Transform.translation(new_pos[0] - grip["coord"][0], new_pos[1] - grip["coord"][1]))
    elif k == "circle":
        if role == "center":
            d["c"] = np_
        else:
            r = dist(P(d["c"]), new_pos)
            if r < EPS:
                raise GeomError("DEGENERATE", "半径不能为 0", "")
            d["r"] = r
    elif k == "arc":
        c = P(d["c"])
        if role == "start":
            d["a0"] = ang(c, new_pos)
        elif role == "end":
            d["a1"] = ang(c, new_pos)
        elif role == "center":
            d["c"] = np_
        else:                                                     # 中点：改半径
            d["r"] = dist(c, new_pos)
    elif k == "ellipse":
        c = P(d["c"])
        if role == "center":
            d["c"] = np_
        elif role.startswith("major"):
            d["major"] = [new_pos[0] - c[0], new_pos[1] - c[1], 0.0] if role == "major_end" else [c[0] - new_pos[0], c[1] - new_pos[1], 0.0]
        else:
            L = math.hypot(*d["major"][:2])
            d["ratio"] = max(1e-6, min(1.0, dist(c, new_pos) / L))
    elif k == "polyline":
        n = len(d["vertices"])
        if idx < n:
            d["vertices"][idx]["p"] = np_
        else:                                                     # 边中点：该边两端一起平移
            i = idx - n
            gx, gy = new_pos[0] - grip["coord"][0], new_pos[1] - grip["coord"][1]
            for j in (i, (i + 1) % n):
                p = d["vertices"][j]["p"]
                d["vertices"][j]["p"] = pl((p[0] + gx, p[1] + gy, p[2] if len(p) > 2 else 0.0))
    elif k in ("mline", "leader"):
        d["points"][idx] = np_
    elif k == "spline":
        key = "fit" if d.get("fit") else "cv"
        d[key][idx] = np_
    elif k in ("text", "mtext", "insert", "tolerance", "point", "ray", "xline"):
        d["p"] = np_
    elif k == "dimension":
        key = ("p1", "p2", "loc", "center")[idx]
        d[key] = np_
    else:
        raise GeomError("UNSUPPORTED", f"{k} 不支持夹点编辑", "")
    return d
