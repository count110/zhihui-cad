"""几何约束与标注约束的求解器（numpy，阻尼最小二乘 / Levenberg–Marquardt）。

变量：每个相关图元的几何参数（线 4 个、圆 3 个、弧 5 个、点 2 个、多段线每顶点 2 个）。
残差：每个约束若干个方程，满足时全为 0。求解从当前几何出发，所以解是「离原图形最近」的一个，不会乱跳。
过约束（方程冗余且互相矛盾）报 OVER_CONSTRAINED，无解报 UNSOLVABLE；自由度 = 变量数 − 雅可比矩阵的秩。
标注约束的数值表达式用安全解析器（只许 + - * / ** 括号、数字、参数名），不使用 eval。
"""
from __future__ import annotations

import ast
import math
import operator
import re
from typing import Callable

import numpy as np

from .entities import P, ang, pl
from .errors import GeomError

TOL = 1e-7
GEOM_TYPES = ("fix", "coincident", "collinear", "concentric", "parallel", "perpendicular", "horizontal", "vertical", "tangent", "equal", "symmetric", "smooth")
DIM_TYPES = ("distance", "horizontal", "vertical", "angle", "radius", "diameter")
SUPPORTED = ("point", "line", "circle", "arc", "polyline")


# ------------------------------------------------------------------ 安全表达式
_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv, ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos}


def eval_expr(expr: str, names: dict[str, float]) -> float:
    """四则运算 + 乘方 + 括号；名字只能是已有的标注参数。"""
    try:
        tree = ast.parse(str(expr).strip(), mode="eval")
    except SyntaxError:
        raise GeomError("BAD_EXPRESSION", f"表达式写法不对：{expr}", "只能用数字、参数名和 + - * / ** 括号")

    def walk(n) -> float:
        if isinstance(n, ast.Expression):
            return walk(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool):
            return float(n.value)
        if isinstance(n, ast.Name):
            if n.id not in names:
                raise GeomError("PARAM_NOT_FOUND", f"表达式里的参数「{n.id}」不存在", f"已有参数：{sorted(names)}")
            return float(names[n.id])
        if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
            a, b = walk(n.left), walk(n.right)
            if isinstance(n.op, ast.Div) and abs(b) < 1e-12:
                raise GeomError("BAD_EXPRESSION", "表达式里有除以 0", "")
            if isinstance(n.op, ast.Pow) and abs(b) > 10:
                raise GeomError("BAD_EXPRESSION", "乘方指数太大", "")
            return _OPS[type(n.op)](a, b)
        if isinstance(n, ast.UnaryOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](walk(n.operand))
        raise GeomError("BAD_EXPRESSION", f"表达式里有不允许的内容：{ast.dump(n)[:40]}", "只能用数字、参数名和 + - * / ** 括号")

    return walk(tree)


def expr_names(expr: str) -> set[str]:
    try:
        return {n.id for n in ast.walk(ast.parse(str(expr).strip(), mode="eval")) if isinstance(n, ast.Name)}
    except SyntaxError:
        return set()


# ------------------------------------------------------------------ 模型：变量与引用
class Model:
    def __init__(self, ents: dict):
        self.ents = ents
        self.idx: dict[str, int] = {}                   # 图元 id → 变量起始下标
        self.n: dict[str, int] = {}
        x: list[float] = []
        for eid, e in ents.items():
            vals = self._values(e)
            self.idx[eid] = len(x)
            self.n[eid] = len(vals)
            x += vals
        self.x0 = np.array(x, dtype=float)

    @staticmethod
    def _values(e) -> list[float]:
        k, d = e.kind, e.data
        if k == "point":
            return [float(d["p"][0]), float(d["p"][1])]
        if k == "line":
            return [float(d["p1"][0]), float(d["p1"][1]), float(d["p2"][0]), float(d["p2"][1])]
        if k == "circle":
            return [float(d["c"][0]), float(d["c"][1]), float(d["r"])]
        if k == "arc":
            return [float(d["c"][0]), float(d["c"][1]), float(d["r"]), float(d["a0"]), float(d["a1"])]
        if k == "polyline":
            return [float(c) for v in d["vertices"] for c in v["p"][:2]]
        raise GeomError("UNSUPPORTED", f"{e.id}（{k}）不能加约束", f"支持：{SUPPORTED}")

    # ---- 点引用 ----
    def point(self, spec: str) -> Callable[[np.ndarray], np.ndarray]:
        m = re.fullmatch(r"\s*([A-Za-z]+\d+)(?:\.(\w+))?\s*", spec)
        if not m:
            raise GeomError("INVALID_ARGS", f"点引用写法不对：{spec}", "写成 L1.start / L1.end / C2.center / A3.start / PL4.v2 / PT5")
        eid, part = m.group(1).upper(), (m.group(2) or "").lower()
        e = self._ent(eid)
        i = self.idx[e.id]
        k = e.kind
        if k == "point" and part in ("", "p"):
            return lambda x, i=i: x[i:i + 2]
        if k == "line" and part in ("start", "p1", "s"):
            return lambda x, i=i: x[i:i + 2]
        if k == "line" and part in ("end", "p2", "e"):
            return lambda x, i=i: x[i + 2:i + 4]
        if k == "line" and part == "mid":
            return lambda x, i=i: (x[i:i + 2] + x[i + 2:i + 4]) / 2
        if k in ("circle", "arc") and part == "center":
            return lambda x, i=i: x[i:i + 2]
        if k == "arc" and part in ("start", "end"):
            j = 3 if part == "start" else 4
            return lambda x, i=i, j=j: x[i:i + 2] + x[i + 2] * np.array([math.cos(math.radians(x[i + j])), math.sin(math.radians(x[i + j]))])
        if k == "polyline" and re.fullmatch(r"v\d+", part):
            v = int(part[1:])
            if not 0 <= v < self.n[e.id] // 2:
                raise GeomError("INVALID_ARGS", f"{e.id} 没有顶点 {part}", f"顶点 v0…v{self.n[e.id] // 2 - 1}")
            return lambda x, i=i, v=v: x[i + 2 * v:i + 2 * v + 2]
        raise GeomError("INVALID_ARGS", f"{e.id} 没有点「{part or '默认'}」", "线：start/end/mid；圆、弧：center；弧：start/end；多段线：v0、v1…；点：直接写 id")

    def _ent(self, eid: str):
        for k, e in self.ents.items():
            if k.upper() == eid.upper():
                return e
        raise GeomError("ENTITY_NOT_FOUND", f"约束引用的图元 {eid} 不在求解范围内", "")

    # ---- 直线（线或多段线某一段）----
    def line(self, spec: str) -> Callable[[np.ndarray], tuple[np.ndarray, np.ndarray]]:
        m = re.fullmatch(r"\s*([A-Za-z]+\d+)(?:\.s(\d+))?\s*", spec)
        if not m:
            raise GeomError("INVALID_ARGS", f"直线引用写法不对：{spec}", "L1 或 PL2.s0（多段线第 0 段）")
        e = self._ent(m.group(1))
        i = self.idx[e.id]
        if e.kind == "line" and m.group(2) is None:
            return lambda x, i=i: (x[i:i + 2], x[i + 2:i + 4])
        if e.kind == "polyline" and m.group(2) is not None:
            s = int(m.group(2))
            nv = self.n[e.id] // 2
            if not 0 <= s < (nv if e.data.get("closed") else nv - 1):
                raise GeomError("INVALID_ARGS", f"{e.id} 没有第 {s} 段", "")
            a, b = i + 2 * s, i + 2 * ((s + 1) % nv)
            return lambda x, a=a, b=b: (x[a:a + 2], x[b:b + 2])
        raise GeomError("INVALID_ARGS", f"{spec} 不是直线", "")

    def curve(self, spec: str):
        """圆或弧 → (center, radius)。"""
        e = self._ent(re.sub(r"\..*", "", spec.strip()))
        if e.kind not in ("circle", "arc"):
            raise GeomError("INVALID_ARGS", f"{e.id} 不是圆或圆弧", "")
        i = self.idx[e.id]
        return lambda x, i=i: (x[i:i + 2], x[i + 2])

    def kind(self, spec: str) -> str:
        return self._ent(re.sub(r"\..*", "", spec.strip())).kind


def _wrap(a: float) -> float:
    return (a + 180.0) % 360.0 - 180.0


def _dirn(a, b):
    v = b - a
    n = np.linalg.norm(v)
    return v / n if n > 1e-12 else np.array([1.0, 0.0])


# ------------------------------------------------------------------ 约束 → 残差函数
def build_residual(model: Model, c: dict, values: dict[str, float]) -> Callable[[np.ndarray], np.ndarray]:
    t = c["type"]
    refs = c["refs"]
    if c["category"] == "dimensional":
        return _dim_residual(model, c, values)
    if t == "fix":
        fs = [model.point(r) if "." in r or model.kind(r) == "point" else None for r in refs]
        if all(f is not None for f in fs):
            p0 = [f(model.x0).copy() for f in fs]
            return lambda x: np.concatenate([f(x) - q for f, q in zip(fs, p0)])
        lo = [(model.idx[model._ent(r).id], model.n[model._ent(r).id]) for r in refs]
        sel = np.concatenate([np.arange(i, i + n) for i, n in lo])
        base = model.x0[sel].copy()
        return lambda x: x[sel] - base
    if t == "coincident":
        a, b = model.point(refs[0]), model.point(refs[1])
        return lambda x: a(x) - b(x)
    if t == "collinear":
        l1, l2 = model.line(refs[0]), model.line(refs[1])

        def f(x):
            a, b = l1(x)
            u = _dirn(a, b)
            c1, d1 = l2(x)
            return np.array([_cr(u, c1 - a), _cr(u, d1 - a)])
        return f
    if t == "concentric":
        a, b = model.curve(refs[0]), model.curve(refs[1])
        return lambda x: a(x)[0] - b(x)[0]
    if t in ("parallel", "perpendicular"):
        l1, l2 = model.line(refs[0]), model.line(refs[1])

        def f(x):
            u, v = _dirn(*l1(x)), _dirn(*l2(x))
            return np.array([_cr(u, v)]) if t == "parallel" else np.array([float(np.dot(u, v))])
        return f
    if t in ("horizontal", "vertical"):
        k = 1 if t == "horizontal" else 0
        if len(refs) == 1:
            l1 = model.line(refs[0])
            return lambda x: np.array([l1(x)[0][k] - l1(x)[1][k]])
        a, b = model.point(refs[0]), model.point(refs[1])
        return lambda x: np.array([a(x)[k] - b(x)[k]])
    if t == "tangent":
        k0, k1 = model.kind(refs[0]), model.kind(refs[1])
        if k0 == "line" or "." in refs[0] and k0 == "polyline":
            ln, ci = model.line(refs[0]), model.curve(refs[1])

            def f(x):
                a, b = ln(x)
                u = _dirn(a, b)
                ctr, r = ci(x)
                return np.array([abs(_cr(u, ctr - a)) - r])
            return f
        c1, c2 = model.curve(refs[0]), model.curve(refs[1])
        ctr1, r1 = c1(model.x0)
        ctr2, r2 = c2(model.x0)
        d0 = np.linalg.norm(ctr1 - ctr2)
        external = abs(d0 - (r1 + r2)) <= abs(d0 - abs(r1 - r2))            # 按当前形态选外切或内切

        def g(x):
            p1, q1 = c1(x)
            p2, q2 = c2(x)
            return np.array([np.linalg.norm(p1 - p2) - ((q1 + q2) if external else abs(q1 - q2))])
        return g
    if t == "equal":
        if model.kind(refs[0]) in ("circle", "arc"):
            a, b = model.curve(refs[0]), model.curve(refs[1])
            return lambda x: np.array([a(x)[1] - b(x)[1]])
        l1, l2 = model.line(refs[0]), model.line(refs[1])
        return lambda x: np.array([np.linalg.norm(l1(x)[1] - l1(x)[0]) - np.linalg.norm(l2(x)[1] - l2(x)[0])])
    if t == "symmetric":
        axis = model.line(refs[-1])
        pairs = [(model.point(refs[i]), model.point(refs[i + 1])) for i in range(0, len(refs) - 1, 2)]

        def f(x):
            a, b = axis(x)
            u = _dirn(a, b)
            out = []
            for p, q in pairs:
                pp, qq = p(x), q(x)
                out.append(_cr(u, (pp + qq) / 2 - a))             # 中点在轴上
                out.append(float(np.dot(u, qq - pp)))                  # 连线垂直于轴
            return np.array(out)
        return f
    if t == "smooth":
        k0, k1 = model.kind(refs[0]), model.kind(refs[1])
        # 约定 refs = [端点引用1, 端点引用2, 图元1, 图元2]：两端点重合且在该点处相切
        p1, p2 = model.point(refs[0]), model.point(refs[1])

        def tangent_of(spec_e: str, ptf):
            kind = model.kind(spec_e)
            if kind == "line":
                ln = model.line(spec_e)
                return lambda x: _dirn(*ln(x))
            ci = model.curve(spec_e)
            return lambda x: np.array([-(ptf(x) - ci(x)[0])[1], (ptf(x) - ci(x)[0])[0]]) / max(ci(x)[1], 1e-12)

        t1, t2 = tangent_of(refs[2], p1), tangent_of(refs[3], p2)
        return lambda x: np.concatenate([p1(x) - p2(x), [_cr(t1(x), t2(x))]])
    raise GeomError("INVALID_ARGS", f"不支持的几何约束 {t}", f"可用：{GEOM_TYPES}")


def measure(model: Model, c: dict, x: np.ndarray) -> float:
    """标注约束当前的测量值（有符号的量取原符号）。"""
    dt, refs = c["dim"]["dim_type"], c["refs"]
    if dt in ("distance", "horizontal", "vertical"):
        form = c["dim"].get("form", "pp")              # l 线长 / pp 点到点 / pl 点到直线
        if form == "l":
            a, b = model.line(refs[0])(x)
        elif form == "pl":
            pt = model.point(refs[0])(x)
            a, b = model.line(refs[1])(x)
            return float(abs(_cr(_dirn(a, b), pt - a)))
        else:
            a, b = model.point(refs[0])(x), model.point(refs[1])(x)
        v = b - a
        return float(np.linalg.norm(v)) if dt == "distance" else float(v[0] if dt == "horizontal" else v[1])
    if dt == "angle":
        u, v = _dirn(*model.line(refs[0])(x)), _dirn(*model.line(refs[1])(x))
        return math.degrees(math.atan2(_cr(u, v), float(np.dot(u, v))))
    if dt == "radius":
        return float(model.curve(refs[0])(x)[1])
    if dt == "diameter":
        return float(2 * model.curve(refs[0])(x)[1])
    raise GeomError("INVALID_ARGS", f"不支持的标注约束 {dt}", f"可用：{DIM_TYPES}")


def _dim_residual(model: Model, c: dict, values: dict[str, float]):
    target = float(values[c["dim"]["name"]])
    cur = measure(model, c, model.x0)
    sign = -1.0 if cur < 0 and c["dim"]["dim_type"] in ("horizontal", "vertical", "angle") else 1.0
    if c["dim"]["dim_type"] == "angle":
        return lambda x: np.array([_wrap(measure(model, c, x) - sign * target)])
    return lambda x: np.array([measure(model, c, x) - sign * target])


# ------------------------------------------------------------------ 参数（标注约束的名字和表达式）
def resolve_params(constraints: list[dict]) -> dict[str, float]:
    """按依赖顺序求每个标注约束的目标值；循环引用报错。"""
    dims = {c["dim"]["name"]: c for c in constraints if c["category"] == "dimensional"}
    done: dict[str, float] = {}
    visiting: set[str] = set()

    def get(name: str) -> float:
        if name in done:
            return done[name]
        if name in visiting:
            raise GeomError("CYCLIC_PARAMS", f"标注参数循环引用：{name}", "改掉互相引用的表达式")
        visiting.add(name)
        c = dims[name]
        expr = c["dim"].get("expr")
        if expr is None:
            done[name] = float(c["dim"]["value"])
        else:
            deps = {n for n in expr_names(expr)}
            for dn in deps:
                if dn not in dims:
                    raise GeomError("PARAM_NOT_FOUND", f"表达式「{expr}」引用了不存在的参数 {dn}", f"已有参数：{sorted(dims)}")
                get(dn)
            done[name] = eval_expr(expr, done)
        visiting.discard(name)
        return done[name]

    for n in dims:
        get(n)
    return done


# ------------------------------------------------------------------ 求解
def _jac(fun, x: np.ndarray, r0: np.ndarray) -> np.ndarray:
    J = np.zeros((len(r0), len(x)))
    for j in range(len(x)):
        h = 1e-6 * max(1.0, abs(x[j]))
        xp, xm = x.copy(), x.copy()
        xp[j] += h
        xm[j] -= h
        J[:, j] = (fun(xp) - fun(xm)) / (2 * h)
    return J


def solve_system(model: Model, cons: list[dict], values: dict[str, float], max_iter: int = 200, pinned: set[str] | None = None) -> dict:
    """pinned：这些图元的变量当作常量（用户刚改过的图元，别让求解器把它们拉走）。"""
    funcs = [(c, build_residual(model, c, values)) for c in cons]
    sizes = [len(f(model.x0)) for _, f in funcs]
    pin_idx: set[int] = set()
    for eid in pinned or ():
        if eid in model.idx:
            pin_idx |= set(range(model.idx[eid], model.idx[eid] + model.n[eid]))
    free = np.array([i for i in range(len(model.x0)) if i not in pin_idx], dtype=int)
    base = model.x0.copy()

    def full(z):
        x = base.copy()
        x[free] = z
        return x

    def res(z):
        x = full(z)
        return np.concatenate([f(x) for _, f in funcs]) if funcs else np.zeros(0)

    z = model.x0[free].copy()
    lam = 1e-3
    r = res(z)
    cost = float(r @ r)
    for _ in range(max_iter):
        if math.sqrt(cost) < TOL or len(z) == 0:
            break
        J = _jac(res, z, r)
        A = J.T @ J + lam * np.eye(len(z))
        try:
            dx = -np.linalg.solve(A, J.T @ r)
        except np.linalg.LinAlgError:
            dx = -np.linalg.lstsq(A, J.T @ r, rcond=None)[0]
        zn = z + dx
        rn = res(zn)
        cn = float(rn @ rn)
        if cn < cost:
            z, r, cost, lam = zn, rn, cn, max(lam / 3, 1e-12)
            if np.linalg.norm(dx) < 1e-12:
                break
        else:
            lam *= 4
            if lam > 1e12:
                break
    J = _jac(res, z, r) if len(r) and len(z) else np.zeros((len(r), len(z)))
    rank = int(np.linalg.matrix_rank(J, tol=1e-6)) if J.size else 0
    ok = math.sqrt(cost) < 1e-5
    return {"ok": ok, "x": full(z), "residual": math.sqrt(cost), "rank": rank, "n_eq": int(sum(sizes)), "n_var": len(z), "dof": len(z) - rank, "sizes": sizes, "funcs": funcs, "per": r}


def diagnose(model: Model, cons: list[dict], values: dict[str, float]) -> dict:
    """求解失败时找可疑的约束：逐个拿掉，看哪个能让系统可解。"""
    sus = []
    if len(cons) <= 30:
        for c in cons:
            rest = [k for k in cons if k is not c]
            try:
                if solve_system(model, rest, values)["ok"]:
                    sus.append(c["id"])
            except GeomError:
                continue
    return {"suspects": sus}


def write_back(model: Model, x: np.ndarray) -> dict[str, dict]:
    """把解写回成各图元的新 data（只返回有变化的）。"""
    out: dict[str, dict] = {}
    for eid, e in model.ents.items():
        i, n = model.idx[eid], model.n[eid]
        if np.allclose(x[i:i + n], model.x0[i:i + n], atol=1e-9):
            continue
        v = [round(float(t), 9) for t in x[i:i + n]]
        d = {**e.data}
        if e.kind == "point":
            d["p"] = [v[0], v[1], e.data["p"][2] if len(e.data["p"]) > 2 else 0.0]
        elif e.kind == "line":
            d["p1"] = [v[0], v[1], e.data["p1"][2] if len(e.data["p1"]) > 2 else 0.0]
            d["p2"] = [v[2], v[3], e.data["p2"][2] if len(e.data["p2"]) > 2 else 0.0]
        elif e.kind == "circle":
            d["c"] = [v[0], v[1], e.data["c"][2] if len(e.data["c"]) > 2 else 0.0]
            d["r"] = v[2]
        elif e.kind == "arc":
            d["c"] = [v[0], v[1], e.data["c"][2] if len(e.data["c"]) > 2 else 0.0]
            d["r"], d["a0"], d["a1"] = v[2], v[3] % 360.0, v[4] % 360.0
        elif e.kind == "polyline":
            d["vertices"] = [{**vt, "p": [v[2 * j], v[2 * j + 1], vt["p"][2] if len(vt["p"]) > 2 else 0.0]} for j, vt in enumerate(e.data["vertices"])]
        out[eid] = d
    return out


def involved_ids(c: dict) -> set[str]:
    return {re.sub(r"\..*", "", r).upper() for r in c["refs"]}


def solve_drawing(d, pin_ids: set[str] | None = None) -> dict:
    """对整张图里的约束求解；返回 {"changes": {id: data}, "info": ...}。不直接改图。"""
    cons = d.constraints
    if not cons:
        return {"changes": {}, "info": {"constraints": 0, "dof": None}}
    ids = {i for c in cons for i in involved_ids(c)}
    ents = {e.id: e for e in d.entities if e.id.upper() in ids}
    missing = ids - {k.upper() for k in ents}
    if missing:
        raise GeomError("ENTITY_NOT_FOUND", f"约束引用的图元已不存在：{sorted(missing)}", "删除相关约束")
    model = Model(ents)
    values = resolve_params(cons)
    pins = {k for k in ents if k.upper() in {p.upper() for p in (pin_ids or ())}}
    sol = solve_system(model, cons, values, pinned=pins) if pins else solve_system(model, cons, values)
    if pins and not sol["ok"]:
        sol = solve_system(model, cons, values)                    # 钉住用户改动后无解：放开，让约束赢
    if not sol["ok"]:
        redundant = sol["n_eq"] - sol["rank"]
        dg = diagnose(model, cons, values)
        code = "OVER_CONSTRAINED" if redundant > 0 and dg["suspects"] else "UNSOLVABLE"
        msg = "约束互相矛盾（过约束）" if code == "OVER_CONSTRAINED" else "约束无解"
        raise GeomError(code, f"{msg}：求解后仍有 {sol['residual']:.3g} mm 的偏差", "拿掉可疑的约束，或改动标注数值" + (f"；去掉其中任意一个即可求解：{dg['suspects']}" if dg["suspects"] else ""), suspects=dg["suspects"], residual=sol["residual"])
    return {"changes": write_back(model, sol["x"]), "info": {"constraints": len(cons), "equations": sol["n_eq"], "variables": sol["n_var"], "dof": sol["dof"], "redundant": max(sol["n_eq"] - sol["rank"], 0)}}


def satisfied(d, c: dict) -> tuple[bool, float]:
    """某个约束在当前几何下是否成立（用于校验与列表）。"""
    ids = involved_ids(c)
    ents = {e.id: e for e in d.entities if e.id.upper() in ids}
    if len(ents) < len(ids):
        return False, float("inf")
    model = Model(ents)
    values = resolve_params(d.constraints)
    r = build_residual(model, c, values)(model.x0)
    n = float(np.linalg.norm(r))
    return n < 1e-4, n


def _cr(a, b) -> float:
    """二维叉积（numpy 2.x 不再支持二维向量的 np.cross）。"""
    return float(a[0] * b[1] - a[1] * b[0])
