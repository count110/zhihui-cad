"""几何约束与标注约束（函数 39，意图类别 block_xref_constraint）。求解器见 cad/geom/constraints.py。"""
from __future__ import annotations

import math
import re
from typing import Any, Literal, Optional

import numpy as np

from zhihui.cad.geom import constraints as C
from zhihui.cad.geom.entities import P, ang, dist, entity_bbox
from zhihui.cad.geom.errors import GeomError
from zhihui.tools.core import ToolError
from .ctx import Ctx, to_tool_error
from .registry import Args, Ids, tool


def _need(cond, msg: str, hint: str = "") -> None:
    if not cond:
        raise ToolError("MISSING_ARG", msg, hint or "向用户确认")


class ConstraintArgs(Args):
    constraint_category: Literal["geometric", "dimensional", "auto_constrain", "remove", "list", "solve"]
    geom_type: Optional[Literal["fix", "coincident", "collinear", "concentric", "parallel", "perpendicular", "horizontal", "vertical", "tangent", "equal", "symmetric", "smooth"]] = None
    entity_ids: Ids = None
    constraint_points: Optional[list[str]] = None
    dim_type: Optional[Literal["distance", "horizontal", "vertical", "angle", "radius", "diameter"]] = None
    dim_param_name: Optional[str] = None
    dim_expression: Any = None
    constraint_ids: Ids = None


# ------------------------------------------------------------------ 引用整理
def _line_ref(ctx: Ctx, ref: str) -> str:
    base = re.sub(r"\..*", "", ref)
    e = ctx.entity(base)
    if e.kind == "line" and "." not in ref:
        return e.id
    if e.kind == "polyline" and re.fullmatch(r"[A-Za-z]+\d+\.s\d+", ref):
        return f"{e.id}.{ref.split('.')[1].lower()}"
    raise ToolError("INVALID_ARGS", f"{ref} 不是直线（多段线某一段写成 PL1.s0）", "")


def _endpoints(ctx: Ctx, e) -> list[tuple[str, tuple]]:
    k, d = e.kind, e.data
    if k == "line":
        return [(f"{e.id}.start", P(d["p1"])), (f"{e.id}.end", P(d["p2"]))]
    if k == "arc":
        from zhihui.cad.geom.entities import polar
        return [(f"{e.id}.start", polar(P(d["c"]), d["r"], d["a0"])), (f"{e.id}.end", polar(P(d["c"]), d["r"], d["a1"]))]
    if k == "point":
        return [(e.id, P(d["p"]))]
    raise ToolError("INVALID_ARGS", f"{e.id}（{k}）没有端点", "")


def _nearest_endpoints(ctx: Ctx, e1, e2) -> tuple[str, str]:
    best = None
    for n1, p1 in _endpoints(ctx, e1):
        for n2, p2 in _endpoints(ctx, e2):
            dd = dist(p1, p2)
            if best is None or dd < best[0]:
                best = (dd, n1, n2)
    return best[1], best[2]


def _check_pt(ctx: Ctx, ref: str) -> str:
    base = re.sub(r"\..*", "", ref)
    ctx.entity(base)
    return ref


def _geom_refs(ctx: Ctx, gt: str, a: ConstraintArgs) -> list[str]:
    ids = list(a.entity_ids or [])
    pts = [_check_pt(ctx, p) for p in (a.constraint_points or [])]
    ents = [ctx.entity(i) for i in ids]
    if gt == "fix":
        _need(pts or ids, "固定需要 entity_ids 或 constraint_points")
        for e in ents:
            if e.kind not in C.SUPPORTED:
                raise ToolError("INVALID_ARGS", f"{e.id}（{e.kind}）不能加约束", f"支持：{C.SUPPORTED}")
        return pts or [e.id for e in ents]
    if gt == "coincident":
        if len(pts) == 2:
            return pts
        _need(len(ents) == 2, "重合需要两个点（constraint_points）或两个图元（entity_ids，取最近的端点）")
        return list(_nearest_endpoints(ctx, ents[0], ents[1]))
    if gt in ("collinear", "parallel", "perpendicular"):
        refs = [_line_ref(ctx, r) for r in (ids + (a.constraint_points or []))]
        _need(len(refs) == 2, f"{gt} 需要两条直线")
        return refs
    if gt == "concentric":
        _need(len(ents) == 2 and all(e.kind in ("circle", "arc") for e in ents), "同心需要两个圆或圆弧")
        return [e.id for e in ents]
    if gt in ("horizontal", "vertical"):
        if len(pts) == 2:
            return pts
        refs = [_line_ref(ctx, r) for r in (ids + (a.constraint_points or []))]
        _need(len(refs) == 1, f"{gt} 需要一条直线或两个点")
        return refs
    if gt == "tangent":
        _need(len(ents) == 2, "相切需要两个图元（直线+圆/圆弧，或两个圆/圆弧）")
        e1, e2 = ents
        if e2.kind == "line":
            e1, e2 = e2, e1
        if e1.kind == "line" and e2.kind in ("circle", "arc") or e1.kind in ("circle", "arc") and e2.kind in ("circle", "arc"):
            return [e1.id, e2.id]
        raise ToolError("INVALID_ARGS", f"{e1.kind} 与 {e2.kind} 不能做相切约束", "")
    if gt == "equal":
        _need(len(ents) == 2 and ents[0].kind == ents[1].kind and ents[0].kind in ("line", "circle", "arc"), "相等需要两个同类图元（线与线、圆与圆）")
        return [e.id for e in ents]
    if gt == "symmetric":
        if pts and len(ents) == 1 and len(pts) % 2 == 0:
            return pts + [_line_ref(ctx, ents[0].id)]
        _need(len(ents) == 3 and ents[0].kind == ents[1].kind, "对称需要「两个点 + 一条对称轴」或「两个同类图元 + 对称轴」")
        axis = _line_ref(ctx, ents[2].id)
        e1, e2 = ents[0], ents[1]
        if e1.kind == "line":
            return [f"{e1.id}.start", f"{e2.id}.start", f"{e1.id}.end", f"{e2.id}.end", axis]
        if e1.kind in ("circle", "arc"):
            return [f"{e1.id}.center", f"{e2.id}.center", axis]
        raise ToolError("INVALID_ARGS", f"{e1.kind} 不支持对称约束", "")
    if gt == "smooth":
        _need(len(ents) == 2 and all(e.kind in ("line", "arc") for e in ents), "平滑需要两个相接的直线/圆弧")
        n1, n2 = _nearest_endpoints(ctx, ents[0], ents[1])
        return [n1, n2, ents[0].id, ents[1].id]
    raise ToolError("INVALID_ARGS", f"不支持的几何约束 {gt}", f"可用：{list(C.GEOM_TYPES)}")


def _entities_of(ctx: Ctx, refs: list[str]) -> list:
    out, seen = [], set()
    for r in refs:
        e = ctx.entity(re.sub(r"\..*", "", r))
        if e.id not in seen:
            seen.add(e.id)
            out.append(e)
    return out


# ------------------------------------------------------------------ 应用求解结果
def _apply(ctx: Ctx, changes: dict) -> list[str]:
    moved = []
    for eid, data in changes.items():
        e = ctx.entity(eid)
        if e.data != data:
            ctx.update(e, data)
            moved.append(eid)
    return moved


def _try_add(ctx: Ctx, c: dict) -> dict:
    """加进约束表并求解；失败就撤回，抛出带诊断的错误。返回求解信息。"""
    d = ctx.d
    d.constraints.append(c)
    # 和 AutoCAD 一致：两个对象之间的几何约束，后选的对象向先选的对象靠拢（先选的钉住）；钉住无解时求解器会放开
    pins: set[str] = set()
    if c["category"] == "geometric":
        owners = [re.sub(r"\..*", "", r) for r in c["refs"]]
        if len({o.upper() for o in owners}) >= 2:
            pins = {owners[0]}
    try:
        sol = C.solve_drawing(d, pin_ids=pins)
    except GeomError:
        d.constraints.remove(c)
        raise
    return sol


def _new_id(d) -> str:
    n = 1 + max([int(re.sub(r"\D", "", c["id"]) or 0) for c in d.constraints] + [0])
    return f"K{n}"


def _describe(c: dict) -> str:
    if c["category"] == "dimensional":
        dm = c["dim"]
        rhs = dm["expr"] if dm.get("expr") is not None else f"{dm['value']:g}"
        return f"{dm['name']}：{dm['dim_type']} {' '.join(c['refs'])} = {rhs}"
    return f"{c['type']} {' '.join(c['refs'])}"


# ------------------------------------------------------------------ 标注约束
def _dim_refs(ctx: Ctx, a: ConstraintArgs) -> tuple[list[str], str]:
    dt = a.dim_type
    ids, pts = list(a.entity_ids or []), list(a.constraint_points or [])
    if dt in ("radius", "diameter"):
        _need(len(ids) == 1, f"{dt} 约束需要一个圆或圆弧（entity_ids）")
        e = ctx.entity(ids[0])
        if e.kind not in ("circle", "arc"):
            raise ToolError("INVALID_ARGS", f"{e.id} 不是圆或圆弧", "")
        return [e.id], "c"
    if dt == "angle":
        refs = [_line_ref(ctx, r) for r in ids + pts]
        _need(len(refs) == 2, "角度约束需要两条直线")
        return refs, "ll"
    if len(pts) == 2:
        return [_check_pt(ctx, p) for p in pts], "pp"
    if len(ids) == 1 and ctx.entity(ids[0]).kind in ("line", "polyline") or (len(ids) == 0 and len(pts) == 1):
        return [_line_ref(ctx, (ids or pts)[0])], "l"
    if len(ids) == 2:
        e1, e2 = ctx.entity(ids[0]), ctx.entity(ids[1])
        if e1.kind == "point" and e2.kind == "line" and dt == "distance":
            return [e1.id, e2.id], "pl"
        if e1.kind == "line" and e2.kind == "point" and dt == "distance":
            return [e2.id, e1.id], "pl"
        return list(_nearest_endpoints(ctx, e1, e2)), "pp"
    raise ToolError("MISSING_ARG", f"{dt} 约束需要两个点（constraint_points）、一条直线，或一个点和一条直线（距离）", "")


def _dimensional(ctx: Ctx, a: ConstraintArgs) -> dict:
    d = ctx.d
    names = {c["dim"]["name"] for c in d.constraints if c["category"] == "dimensional"}
    existing = next((c for c in d.constraints if c["category"] == "dimensional" and c["dim"]["name"] == a.dim_param_name), None) if a.dim_param_name else None
    if existing and not (a.entity_ids or a.constraint_points):
        _need(a.dim_expression is not None, f"修改参数 {a.dim_param_name} 需要 dim_expression")
        old = dict(existing["dim"])
        _set_expr(existing, a.dim_expression, names)
        try:
            sol = C.solve_drawing(d)
        except GeomError:
            existing["dim"] = old
            raise
        moved = _apply(ctx, sol["changes"])
        return ctx.result(f"参数 {a.dim_param_name} 改为 {a.dim_expression}，重新求解，移动了 {len(moved)} 个图元", dof=sol["info"]["dof"], moved=moved)
    _need(a.dim_type, "标注约束需要 dim_type")
    refs, form = _dim_refs(ctx, a)
    name = a.dim_param_name
    if name is None:
        k = 1
        while f"d{k}" in names:
            k += 1
        name = f"d{k}"
        ctx.notes.append(f"参数名未指定，自动取 {name}")
    if not re.fullmatch(r"[A-Za-z_]\w*", name):
        raise ToolError("INVALID_ARGS", f"参数名「{name}」不合法", "字母或下划线开头，只含字母数字下划线")
    if name in names:
        raise ToolError("PARAM_EXISTS", f"参数 {name} 已存在", "换一个名字，或不给 entity_ids 直接改它的表达式")
    c = {"id": _new_id(d), "category": "dimensional", "type": a.dim_type, "refs": refs, "dim": {"name": name, "dim_type": a.dim_type, "form": form, "expr": None, "value": 0.0}}
    ents = {e.id: e for e in _entities_of(ctx, refs)}
    cur = C.measure(C.Model(ents), c, C.Model(ents).x0)
    if a.dim_expression is None:
        v = abs(cur)
        c["dim"]["value"] = round(v, 9)
        ctx.notes.append(f"数值未指定，取当前测量值 {v:g}")
    else:
        _set_expr(c, a.dim_expression, names)
    sol = _try_add(ctx, c)
    moved = _apply(ctx, sol["changes"])
    ex = c["dim"]["expr"] if c["dim"]["expr"] is not None else c["dim"]["value"]
    return ctx.result(f"添加标注约束 {c['id']}：{name}（{a.dim_type}）= {ex}，移动了 {len(moved)} 个图元，剩余自由度 {sol['info']['dof']}", constraint_id=c["id"], param=name, value=C.resolve_params(d.constraints)[name], dof=sol["info"]["dof"], moved=moved)


def _set_expr(c: dict, expr, names: set[str]) -> None:
    dm = c["dim"]
    if isinstance(expr, (int, float)) and not isinstance(expr, bool):
        dm["expr"], dm["value"] = None, float(expr)
    else:
        s = str(expr).strip()
        s = s.split("=", 1)[1].strip() if "=" in s else s            # 兼容 "d2 = d1 * 2"
        try:
            dm["expr"], dm["value"] = None, float(s)
        except ValueError:
            allv = {n: 1.0 for n in names | {dm["name"]}}
            C.eval_expr(s, allv)                                       # 语法与参数名先验一遍
            dm["expr"] = s
            dm["value"] = 0.0
    if dm["expr"] is None and dm["dim_type"] in ("distance", "radius", "diameter") and dm["value"] <= 0:
        raise ToolError("INVALID_ARGS", f"{dm['dim_type']} 的数值必须大于 0", "")


# ------------------------------------------------------------------ 自动约束
def _auto_candidates(ctx: Ctx, ents: list) -> list[dict]:
    d = ctx.d
    bb = [b for b in (entity_bbox(e) for e in ents) if b]
    diag = math.hypot(max(b[2] for b in bb) - min(b[0] for b in bb), max(b[3] for b in bb) - min(b[1] for b in bb)) if bb else 1.0
    tol = max(1e-3 * diag, 1e-4)
    atol = 0.5
    out: list[dict] = []
    lines = [e for e in ents if e.kind == "line"]
    horiz, vert = set(), set()
    for e in lines:
        p1, p2 = P(e.data["p1"]), P(e.data["p2"])
        if dist(p1, p2) < 5 * tol:
            continue
        a_ = ang(p1, p2) % 180
        if min(a_, 180 - a_) < atol:
            horiz.add(e.id)
            out.append({"category": "geometric", "type": "horizontal", "refs": [e.id]})
        elif abs(a_ - 90) < atol:
            vert.add(e.id)
            out.append({"category": "geometric", "type": "vertical", "refs": [e.id]})
    pts = [(n, p) for e in ents if e.kind in ("line", "arc") for n, p in _endpoints(ctx, e)]
    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            if pts[i][0].split(".")[0] != pts[j][0].split(".")[0] and dist(pts[i][1], pts[j][1]) < tol:
                out.append({"category": "geometric", "type": "coincident", "refs": [pts[i][0], pts[j][0]]})
    for i in range(len(lines)):
        for j in range(i + 1, len(lines)):
            e1, e2 = lines[i], lines[j]
            a1, a2 = ang(P(e1.data["p1"]), P(e1.data["p2"])) % 180, ang(P(e2.data["p1"]), P(e2.data["p2"])) % 180
            df = abs(a1 - a2)
            df = min(df, 180 - df)
            if df < atol and not (e1.id in horiz and e2.id in horiz) and not (e1.id in vert and e2.id in vert):
                out.append({"category": "geometric", "type": "parallel", "refs": [e1.id, e2.id]})
            elif abs(df - 90) < atol and not ({e1.id, e2.id} & horiz and {e1.id, e2.id} & vert):
                out.append({"category": "geometric", "type": "perpendicular", "refs": [e1.id, e2.id]})
    circles = [e for e in ents if e.kind in ("circle", "arc")]
    for i in range(len(circles)):
        for j in range(i + 1, len(circles)):
            if dist(P(circles[i].data["c"]), P(circles[j].data["c"])) < tol:
                out.append({"category": "geometric", "type": "concentric", "refs": [circles[i].id, circles[j].id]})
    for ln in lines:
        a, b = P(ln.data["p1"]), P(ln.data["p2"])
        L = dist(a, b)
        if L < 5 * tol:
            continue
        u = ((b[0] - a[0]) / L, (b[1] - a[1]) / L)
        for ci in circles:
            c = P(ci.data["c"])
            t = (c[0] - a[0]) * u[0] + (c[1] - a[1]) * u[1]
            dd = abs(u[0] * (c[1] - a[1]) - u[1] * (c[0] - a[0]))
            if abs(dd - ci.data["r"]) < tol and -tol <= t <= L + tol:
                out.append({"category": "geometric", "type": "tangent", "refs": [ln.id, ci.id]})
    return out


def _auto(ctx: Ctx, a: ConstraintArgs) -> dict:
    d = ctx.d
    ents = ctx.entities(a.entity_ids) if a.entity_ids else [e for e in d.entities if e.space == d.active_space and e.kind in C.SUPPORTED and e.kind != "polyline"]
    ents = [e for e in ents if e.kind in C.SUPPORTED and e.kind != "polyline"][:150]
    _need(ents, "没有可以自动约束的图元（支持直线、圆、圆弧）")
    have = {(c["type"], tuple(c["refs"])) for c in d.constraints}
    added, skipped = [], 0
    for cand in _auto_candidates(ctx, ents):
        key = (cand["type"], tuple(cand["refs"]))
        if key in have:
            continue
        c = {"id": _new_id(d), **cand}
        try:
            _try_add(ctx, c)
        except GeomError:
            skipped += 1
            continue
        have.add(key)
        added.append(c)
    sol = C.solve_drawing(d)
    moved = _apply(ctx, sol["changes"])
    by = {}
    for c in added:
        by[c["type"]] = by.get(c["type"], 0) + 1
    return ctx.result(f"自动约束：添加 {len(added)} 个（{by or '无'}），{skipped} 个因冲突跳过；对 {len(moved)} 个图元做了微调", added=[c["id"] for c in added], by_type=by, moved=moved, dof=sol["info"]["dof"])


# ------------------------------------------------------------------ 主函数
@tool("apply_parametric_constraints", "block_xref_constraint", ConstraintArgs)
def apply_parametric_constraints(ctx: Ctx, a: ConstraintArgs) -> dict:
    """几何约束（固定/重合/共线/同心/平行/垂直/水平/竖直/相切/相等/对称/平滑）和标注约束（距离/水平/垂直/角度/半径/直径，数值可写表达式如 d2 = d1 * 2，只支持四则运算和已有参数）。自研求解器：过约束报 OVER_CONSTRAINED，无解报 UNSOLVABLE。auto_constrain 自动识别近似水平/竖直/重合/平行/垂直/同心/相切；solve 重新求解；remove 删约束；list 列约束和自由度。约束只导出求解后的几何，约束本身写进 XDATA 备查，AutoCAD 不识别。"""
    d = ctx.d
    cat = a.constraint_category
    try:
        if cat == "list":
            rows = []
            for c in d.constraints:
                ok, res = C.satisfied(d, c)
                rows.append({"id": c["id"], "text": _describe(c), "category": c["category"], "satisfied": ok, "residual": None if res == float("inf") else round(res, 9)})
            info = {}
            if d.constraints:
                try:
                    info = C.solve_drawing(d)["info"]
                except GeomError as ex:
                    info = {"error": ex.code}
            return ctx.result(f"共 {len(rows)} 个约束" + (f"，自由度 {info['dof']}" if info.get("dof") is not None else ""), constraints=rows, params=C.resolve_params(d.constraints) if d.constraints else {}, solve=info)
        if cat == "solve":
            _need(d.constraints, "图中还没有约束")
            sol = C.solve_drawing(d)
            moved = _apply(ctx, sol["changes"])
            return ctx.result(f"重新求解：移动 {len(moved)} 个图元，自由度 {sol['info']['dof']}", moved=moved, **sol["info"])
        if cat == "remove":
            ids = [x.upper() for x in (a.constraint_ids or [])]
            if ids in (["ALL"], ["*"]):
                gone, d.constraints = list(d.constraints), []
            elif ids:
                gone = [c for c in d.constraints if c["id"].upper() in ids]
                miss = [i for i in ids if i not in {c["id"].upper() for c in d.constraints}]
                if miss:
                    raise ToolError("CONSTRAINT_NOT_FOUND", f"没有约束 {miss}", f"现有：{[c['id'] for c in d.constraints]}")
                d.constraints = [c for c in d.constraints if c not in gone]
            else:
                _need(a.entity_ids, "删除约束需要 constraint_ids（或 [\"all\"]）或 entity_ids（删除涉及这些图元的全部约束）")
                targets = {e.id.upper() for e in ctx.entities(a.entity_ids)}
                gone = [c for c in d.constraints if C.involved_ids(c) & targets]
                d.constraints = [c for c in d.constraints if c not in gone]
            return ctx.result(f"删除 {len(gone)} 个约束：{[c['id'] for c in gone]}（图形保持当前形状）")
        if cat == "auto_constrain":
            return _auto(ctx, a)
        if cat == "dimensional":
            return _dimensional(ctx, a)
        _need(a.geom_type, "几何约束需要 geom_type")
        refs = _geom_refs(ctx, a.geom_type, a)
        ents = _entities_of(ctx, refs)
        for e in ents:
            ctx.check_unlocked(e)
            if e.kind not in C.SUPPORTED:
                raise ToolError("INVALID_ARGS", f"{e.id}（{e.kind}）不能加约束", f"支持：{C.SUPPORTED}")
        if any(c["type"] == a.geom_type and c["refs"] == refs for c in d.constraints):
            raise ToolError("CONSTRAINT_EXISTS", "已有完全相同的约束", "")
        c = {"id": _new_id(d), "category": "geometric", "type": a.geom_type, "refs": refs}
        sol = _try_add(ctx, c)
        moved = _apply(ctx, sol["changes"])
        return ctx.result(f"添加约束 {c['id']}：{_describe(c)}；移动了 {len(moved)} 个图元，自由度 {sol['info']['dof']}", constraint_id=c["id"], moved=moved, dof=sol["info"]["dof"])
    except GeomError as ex:
        raise to_tool_error(ex)


# ------------------------------------------------------------------ 其他函数改了图之后：约束跟着走
def after_edit(ctx: Ctx) -> None:
    """其他工具改动图元后调用：删掉涉及已删除图元的约束；涉及被改图元的约束重新求解（用户刚改的图元先钉住，无解则放开让约束赢）。"""
    d = ctx.d
    if not d.constraints:
        return
    alive = {e.id.upper() for e in d.entities}
    gone = [c for c in d.constraints if not C.involved_ids(c) <= alive]
    if gone:
        d.constraints = [c for c in d.constraints if c not in gone]
        ctx.warnings.append(f"约束 {[c['id'] for c in gone]} 涉及已删除的图元，已一并移除")
    touched = {i.upper() for i in ctx.modified + ctx.created}
    if not d.constraints or not any(C.involved_ids(c) & touched for c in d.constraints):
        return
    try:
        sol = C.solve_drawing(d, pin_ids=touched)
    except GeomError as ex:
        raise ToolError("CONSTRAINT_VIOLATION", f"这个改动违反了已有的约束：{ex.message}", "先用 apply_parametric_constraints(remove) 去掉相关约束，或换一种改法", cause=ex.code, **ex.extra)
    moved = _apply(ctx, sol["changes"])
    if moved:
        ctx.notes.append(f"约束带动了 {len(moved)} 个图元跟着调整：{moved[:10]}")
