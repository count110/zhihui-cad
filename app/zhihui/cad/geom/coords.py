"""坐标表达式：绝对/相对直角和极坐标、方向加距离、图元特征点、交点/垂足/切点/最近点/延伸点/两点中点。

  100,50  100,50,10     绝对直角        80<30        绝对极坐标
  @30,0   @30,0,5       相对上一点      @50<45       相对极坐标
  right 100 / 向右100 / up 30            方向加距离（受正交、极轴设置影响）
  C8.center L2.start L2.end L2.mid PL6.v0 PL6.edge_mid(1) C8.quad(90) PL6.center
  int(L1,L2) aint(L1,L2) per(L1) tan(C8) nea(L1,30%) ext(L1,20) m2p(P1,P2)
  $LAST_POINT

解析结果 {point, source, warnings}；调用方负责在成功后更新 session.last_point。
"""
from __future__ import annotations

import math
import re

from . import curves, snap
from .entities import P, Pt, Seg, ang, dist, polar, primitives
from .errors import GeomError

_NUM = r"[-+]?\d+(?:\.\d+)?"
DIRS = {"right": 0, "east": 0, "向右": 0, "右": 0, "left": 180, "west": 180, "向左": 180, "左": 180, "up": 90, "north": 90, "向上": 90, "上": 90,
        "down": 270, "south": 270, "向下": 270, "下": 270,
        "upright": 45, "up-right": 45, "右上": 45, "向右上": 45, "upleft": 135, "up-left": 135, "左上": 135, "向左上": 135,
        "downleft": 225, "down-left": 225, "左下": 225, "向左下": 225, "downright": 315, "down-right": 315, "右下": 315, "向右下": 315}
_DIR_RE = re.compile(r"^(?P<d>" + "|".join(sorted(map(re.escape, DIRS), key=len, reverse=True)) + r")\s*(?P<n>" + _NUM + r")(?:\s*(?:mm|毫米))?$", re.I)
_ANG_DIR_RE = re.compile(r"^(?P<a>" + _NUM + r")\s*(?:度|°|deg)\s*(?:方向)?\s*(?P<n>" + _NUM + r")(?:\s*(?:mm|毫米))?$", re.I)
_FEATURE_RE = re.compile(r"^(?P<id>[A-Za-z]+\d+)\.(?P<f>[a-z_][a-z_0-9]*)(?:\((?P<arg>[^)]*)\))?$", re.I)
_FUNC_RE = re.compile(r"^(?P<fn>int|aint|per|tan|nea|ext|m2p)\((?P<args>.*)\)$", re.I)


class CoordContext:
    """解析所需的最小上下文：图纸（取图元、绘图设置、最后一点）。"""

    def __init__(self, drawing):
        self.d = drawing

    @property
    def last(self) -> Pt | None:
        lp = self.d.session.last_point
        return tuple(lp) if lp else None

    def entity(self, ref: str):
        e = self.d.get_entity(ref)
        if e is None:
            raise GeomError("ENTITY_NOT_FOUND", f"没有图元 {ref}", "先用 query_entities_by_filter 查找，或检查 id")
        return e


def _split_args(s: str) -> list[str]:
    out, depth, cur = [], 0, ""
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


def _quantize(a: float, ctx: CoordContext, warnings: list[str]) -> float:
    dr = ctx.d.draft
    if dr.ortho_on:
        q = (round(a / 90.0) * 90) % 360
        if abs(((a - q + 180) % 360) - 180) > 1e-6:
            warnings.append(f"正交已开启，角度 {a:g}° 按最近的 {q:g}° 处理")
        return q
    if dr.polar_on and dr.polar_angle_deg > 0:
        step = dr.polar_angle_deg
        q = (round(a / step) * step) % 360
        if abs(((a - q + 180) % 360) - 180) > 1e-6:
            warnings.append(f"极轴已开启（增量 {step:g}°），角度 {a:g}° 按最近的 {q:g}° 处理")
        return q
    return a % 360


def parse_point(expr, ctx: CoordContext, allow_relative: bool = True) -> dict:
    """任意写法的点 → {point:[x,y,z], source, warnings}。"""
    warnings: list[str] = []
    if isinstance(expr, dict):
        expr = expr.get("point", expr.get("p", expr.get("coord")))
    if isinstance(expr, (list, tuple)):
        if len(expr) < 2 or not all(isinstance(v, (int, float)) for v in expr[:3]):
            raise GeomError("INVALID_POINT", f"点写法不对：{expr}", "写成 [x, y] 或 [x, y, z]，或坐标表达式字符串")
        return {"point": list(P(expr)), "source": "array", "warnings": warnings}
    if not isinstance(expr, str):
        raise GeomError("INVALID_POINT", f"点写法不对：{expr!r}", "写成 [x, y]、'100,50' 或 '@30,0'")
    s = expr.strip().replace("，", ",").replace("＜", "<").replace("＠", "@")
    if s.upper() in ("$LAST_POINT", "LAST_POINT"):
        if ctx.last is None:
            raise GeomError("REF_UNRESOLVED", "还没有“上一点”", "先给出一个绝对坐标")
        return {"point": list(ctx.last), "source": "last_point", "warnings": warnings}
    m = re.fullmatch(r"(?P<rel>@)?\s*(?P<a>" + _NUM + r")\s*<\s*(?P<b>" + _NUM + r")", s)
    if m:                                         # 极坐标
        r, a = float(m["a"]), float(m["b"])
        if m["rel"]:
            base = _need_last(ctx)
            a = _quantize(a, ctx, warnings)
            pt = polar(base, r, a)
            return {"point": [round(v, 9) for v in pt], "source": "relative_polar", "warnings": warnings}
        pt = polar((0, 0, 0), r, a)
        return {"point": [round(v, 9) for v in pt], "source": "absolute_polar", "warnings": warnings}
    m = re.fullmatch(r"(?P<rel>@)?\s*(?P<x>" + _NUM + r")\s*,\s*(?P<y>" + _NUM + r")(?:\s*,\s*(?P<z>" + _NUM + r"))?", s)
    if m:                                         # 直角坐标
        x, y, z = float(m["x"]), float(m["y"]), float(m["z"] or 0)
        if m["rel"]:
            b = _need_last(ctx)
            return {"point": [b[0] + x, b[1] + y, b[2] + z], "source": "relative_xy", "warnings": warnings}
        return {"point": [x, y, z], "source": "absolute_xy", "warnings": warnings}
    m = _DIR_RE.match(s)
    if m:                                         # 方向 + 距离
        base = _need_last(ctx)
        a = _quantize(float(DIRS[m["d"].lower() if m["d"].lower() in DIRS else m["d"]]), ctx, warnings)
        pt = polar(base, float(m["n"]), a)
        return {"point": [round(v, 9) for v in pt], "source": "direction", "warnings": warnings}
    m = _ANG_DIR_RE.match(s)
    if m:
        base = _need_last(ctx)
        a = _quantize(float(m["a"]), ctx, warnings)
        pt = polar(base, float(m["n"]), a)
        return {"point": [round(v, 9) for v in pt], "source": "angle_distance", "warnings": warnings}
    m = _FUNC_RE.match(s)
    if m:
        return _function(m["fn"].lower(), _split_args(m["args"]), ctx)
    m = _FEATURE_RE.match(s)
    if m:
        return _feature(m["id"], m["f"].lower(), m["arg"], ctx)
    if re.fullmatch(r"[A-Za-z]+\d+", s):          # 光秃秃的点图元 id
        e = ctx.entity(s)
        if e.kind == "point":
            return {"point": list(P(e.data["p"])), "source": "node", "warnings": warnings}
        raise GeomError("INVALID_POINT", f"{s} 不是点图元，请写成 {s}.center / {s}.start 等特征点", "")
    raise GeomError("INVALID_POINT", f"无法识别的坐标写法：{expr}", "支持 100,50 / @30,0 / @50<45 / right 100 / C8.center / int(L1,L2) 等")


def _need_last(ctx: CoordContext) -> Pt:
    if ctx.last is None:
        raise GeomError("NO_LAST_POINT", "相对坐标需要“上一点”，但还没有", "先给出一个绝对坐标，例如 0,0", )
    return ctx.last


def _feature(eid: str, feat: str, arg: str | None, ctx: CoordContext) -> dict:
    e = ctx.entity(eid)
    prims = primitives(e)
    pt: Pt | None = None
    if feat in ("start", "end"):
        if e.kind == "polyline":
            vs = e.data["vertices"]
            pt = P(vs[0]["p"] if feat == "start" else vs[-1]["p"])
        elif prims:
            a, b = curves.endpoints(prims[0] if feat == "start" else prims[-1])
            pt = a if feat == "start" else b
    elif feat in ("mid", "midpoint"):
        if e.kind == "polyline" and arg is None:
            pts = [P(v["p"]) for v in e.data["vertices"]]
            pt = ((pts[0][0] + pts[-1][0]) / 2, (pts[0][1] + pts[-1][1]) / 2, 0.0) if not e.data.get("closed") else None
        if pt is None and prims:
            pt = curves.midpoint(prims[len(prims) // 2] if len(prims) % 2 else prims[len(prims) // 2 - 1]) if e.kind != "polyline" else curves.midpoint(prims[0])
    elif feat in ("center", "geometric_center"):
        pt = snap.geometric_center(e) if feat == "geometric_center" or e.kind in ("polyline", "hatch", "spline", "mline", "line") else (tuple(e.data["c"]) + (0.0,) * (3 - len(e.data["c"])) if "c" in e.data else snap.geometric_center(e))
    elif feat in ("v", "vertex") or re.fullmatch(r"v\d+", feat):
        idx = int(arg) if arg not in (None, "") else int(feat[1:])
        vs = e.data.get("vertices") or e.data.get("points") or e.data.get("fit") or e.data.get("cv")
        if not vs or not (0 <= idx < len(vs)):
            raise GeomError("INVALID_POINT", f"{eid} 没有第 {idx} 个顶点", f"顶点编号 0–{(len(vs) or 1) - 1}")
        pt = P(vs[idx]["p"] if isinstance(vs[idx], dict) else vs[idx])
    elif feat == "edge_mid":
        i = int(arg or 0)
        if not (0 <= i < len(prims)):
            raise GeomError("INVALID_POINT", f"{eid} 没有第 {i} 条边", f"边编号 0–{len(prims) - 1}")
        pt = curves.midpoint(prims[i])
    elif feat in ("quad", "quadrant"):
        q = float(arg or 0)
        if e.kind in ("circle", "arc"):
            pt = polar(P(e.data["c"]), float(e.data["r"]), q)
        else:
            raise GeomError("INVALID_POINT", f"{eid} 没有象限点", "象限点只对圆、圆弧、椭圆有效")
    elif feat in ("insert", "insertion", "p"):
        pt = P(e.data["p"]) if "p" in e.data else None
    if pt is None:
        raise GeomError("INVALID_POINT", f"{eid}.{feat} 求不出点", "检查特征点名称：start/end/mid/center/v0/edge_mid(i)/quad(90)")
    return {"point": [round(v, 9) for v in pt], "source": f"{eid}.{feat}", "warnings": []}


def _function(fn: str, args: list[str], ctx: CoordContext) -> dict:
    def ent(i: int):
        if i >= len(args):
            raise GeomError("INVALID_POINT", f"{fn}() 缺少参数", "")
        return ctx.entity(args[i])
    if fn in ("int", "aint"):
        a, b = ent(0), ent(1)
        pts = snap.intersections([a, b], apparent=(fn == "aint"))
        if not pts:
            raise GeomError("NO_INTERSECTION", f"{a.id} 与 {b.id} 没有{'延伸' if fn == 'aint' else ''}交点", "检查两个图元是否真的相交" + ("" if fn == "aint" else "；延伸才相交请用 aint"))
        ref = ctx.last
        chosen, _ = snap.choose(pts, ref)
        pt = (chosen or pts[0])["coord"]
        return {"point": list(pt), "source": f"{fn}({a.id},{b.id})", "warnings": ([f"有 {len(pts)} 个交点，取离上一点最近的"] if len(pts) > 1 else [])}
    if fn == "per":
        e = ent(0)
        base = _need_last(ctx)
        feet = [f for pr in primitives(e) for f in curves.perpendicular_feet(pr, base)]
        if not feet:
            raise GeomError("NO_PERPENDICULAR", f"从上一点到 {e.id} 没有落在图元上的垂足", "")
        return {"point": [round(v, 9) for v in min(feet, key=lambda p: dist(p, base))], "source": f"per({e.id})", "warnings": []}
    if fn == "tan":
        e = ent(0)
        base = _need_last(ctx)
        ts = [t for pr in primitives(e) for t in curves.tangent_points(pr, base)]
        if not ts:
            raise GeomError("NO_TANGENT", f"从上一点到 {e.id} 没有切点", "上一点在圆内，或图元不是圆/弧")
        return {"point": [round(v, 9) for v in min(ts, key=lambda p: dist(p, base))], "source": f"tan({e.id})", "warnings": ([] if len(ts) == 1 else ["有两个切点，取离上一点较近的"])}
    if fn == "nea":
        e = ent(0)
        prims = primitives(e)
        if len(args) > 1 and args[1].endswith("%"):
            frac = float(args[1][:-1]) / 100
            total = sum(p.length for p in prims)
            run = frac * total
            for pr in prims:
                if run <= pr.length + 1e-9:
                    return {"point": [round(v, 9) for v in curves.at_length(pr, run)], "source": f"nea({e.id},{args[1]})", "warnings": []}
                run -= pr.length
        base = _need_last(ctx)
        return {"point": [round(v, 9) for v in min((curves.closest(pr, base) for pr in prims), key=lambda p: dist(p, base))], "source": f"nea({e.id})", "warnings": []}
    if fn == "ext":
        e = ent(0)
        d = float(args[1]) if len(args) > 1 else 0.0
        pt = snap.extension_point(e, d, "end")
        if pt is None:
            raise GeomError("INVALID_POINT", f"{e.id} 不能延伸", "")
        return {"point": [round(v, 9) for v in pt], "source": f"ext({e.id},{d:g})", "warnings": []}
    if fn == "m2p":
        a = parse_point(args[0], ctx)["point"]
        b = parse_point(args[1], ctx)["point"]
        return {"point": [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2, (a[2] + b[2]) / 2], "source": f"m2p({args[0]},{args[1]})", "warnings": []}
    raise GeomError("INVALID_POINT", f"未知函数 {fn}", "")


def parse_points(exprs, ctx: CoordContext, update_last: bool = True) -> list[dict]:
    """一串点表达式依次解析；相对坐标以前一个结果为基准。"""
    out = []
    for ex in exprs:
        r = parse_point(ex, ctx)
        out.append(r)
        if update_last:
            ctx.d.session.last_point = tuple(r["point"])
    return out
