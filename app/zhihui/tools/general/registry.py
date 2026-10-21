"""通用 CAD 函数注册表：函数名 → (实现, pydantic 参数模型, 意图类别, 是否只读)。

同一份注册表同时服务：
  · MCP Server（批量注册成工具，动态签名）；
  · Planner / Orchestrator（按意图类别取子集，生成工具目录）；
  · 前端 /api/drawings/{id}/ops（按钮、夹点、面板直接调用，不经过大模型）。
"""
from __future__ import annotations

import inspect
import json
from dataclasses import dataclass
from typing import Annotated, Any, Callable, Optional

from pydantic import BaseModel, BeforeValidator, ConfigDict, ValidationError

from zhihui.cad.geom.errors import GeomError
from zhihui.ir import Drawing
from zhihui.tools.core import ToolError
from .ctx import Ctx, to_tool_error


def _flat(v):
    for x in v:
        if isinstance(x, (list, tuple)):
            yield from _flat(x)
        else:
            yield x


def _ids(v):
    if v is None:
        return None
    return [v] if isinstance(v, str) else list(dict.fromkeys(_flat(v)))        # 嵌套列表（如 [$s1.boundary.outer, $s1.boundary.islands]）摊平、去重


Ids = Annotated[Optional[list[str]], BeforeValidator(_ids)]
Pt = Any                     # [x,y] / [x,y,z] / 坐标表达式字符串 / 变量引用（Executor 先解析）


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


CATEGORIES = ("environment_setup", "2d_drafting", "precision_snap", "view_select", "2d_editing", "annotation_dimension", "layout_plotting",
              "block_xref_constraint", "3d_modeling", "engineering_specialty", "chem_equipment", "system", "perception")


@dataclass
class Tool:
    name: str
    category: str
    model: type[Args]
    impl: Callable[[Ctx, Any], dict]
    doc: str
    readonly: bool = False
    needs_drawing: bool = True
    quiet: bool = False           # 只改视图/会话，不产生新版本
    no_history: bool = False      # 不进撤销栈（撤销重做自身、纯设置类）
    quiet_if: Callable[[Any], bool] | None = None   # 按参数决定是否 quiet（如只切换空间）


REGISTRY: dict[str, Tool] = {}


def tool(name: str, category: str, model: type[Args], readonly: bool = False, needs_drawing: bool = True, quiet: bool = False, no_history: bool = False, quiet_if=None):
    def deco(fn):
        REGISTRY[name] = Tool(name, category, model, fn, (fn.__doc__ or "").strip(), readonly, needs_drawing, quiet, no_history, quiet_if)
        return fn
    return deco


def tools_of(categories: list[str] | None = None, include_system: bool = True) -> list[Tool]:
    cats = set(categories or CATEGORIES)
    if include_system:
        cats |= {"system", "perception"}
    return [t for t in REGISTRY.values() if t.category in cats]


def validate(t: Tool, args: dict) -> Args:
    try:
        return t.model.model_validate(args)
    except ValidationError as ex:
        errs = [{"field": ".".join(str(x) for x in e["loc"]), "message": e["msg"]} for e in ex.errors()[:6]]
        raise ToolError("INVALID_ARGS", f"{t.name} 参数不对：" + "；".join(f"{e['field'] or '(整体)'}：{e['message']}" for e in errs),
                        "按函数参数说明改字段名、枚举拼写和类型；缺少必需的数值要向用户确认", errors=errs)


def _record_history(d: Drawing, t: Tool, args: dict, utterance: str) -> None:
    if t.no_history or t.readonly:
        return
    s = d.session
    s.history.append({"rev": d.revision + 1, "prev_rev": d.revision, "tool": t.name, "args": _brief(args), "utterance": utterance})
    s.history = s.history[-200:]
    s.redo = []


def _brief(args: dict) -> dict:
    out = {}
    for k, v in args.items():
        s = json.dumps(v, ensure_ascii=False, default=str)
        out[k] = v if len(s) <= 120 else s[:117] + "..."
    return out


def execute(d: Drawing, t: Tool, args: dict, utterance: str = "", quiet: bool = False, svc=None) -> dict:
    """在已加载的 Drawing 上执行一个函数（已在事务里）：校验 → 实现 → 回写会话 → 记历史。"""
    model = validate(t, args)
    ctx = Ctx(d, t.name, svc)
    auto_ids = {e.id for e in d.entities if "equip:auto" in e.tags} if (d.vessel is not None and t.category != "chem_equipment" and not t.readonly) else set()
    try:
        res = t.impl(ctx, model)
    except GeomError as ex:
        raise to_tool_error(ex)
    if auto_ids:
        _detach_generated(d, ctx, res, auto_ids)
    if not t.readonly:
        if t.name != "apply_parametric_constraints" and (ctx.created or ctx.modified or ctx.deleted):
            from .constraint_tools import after_edit      # 约束跟着改动走
            after_edit(ctx)
            res["modified"] = list(dict.fromkeys(list(res.get("modified", [])) + [m for m in ctx.modified if m not in ctx.created]))
            if ctx.warnings:
                res["warnings"] = list(dict.fromkeys(list(res.get("warnings", [])) + ctx.warnings))
        if ctx.created or ctx.modified:
            from .draw_tools import refresh_hatches        # 关联填充跟着边界走
            changed = [h for h in refresh_hatches(d) if h not in ctx.created and h not in ctx.modified]
            ctx.modified += changed
            if changed:
                res.setdefault("modified", [])
                res["modified"] = list(dict.fromkeys(list(res["modified"]) + changed))
        ctx.record_session()
        if not quiet:
            _record_history(d, t, args, utterance)
    return res


def _detach_generated(d: Drawing, ctx: Ctx, res: dict, auto_ids: set[str]) -> None:
    """通用编辑函数改动或删除了设备图的生成图元（equip:auto）：改标为 equip:detached（删除的记进 vessel.drawing.detached），
    重新生成时不再覆盖它；复制出来的新图元去掉生成标签，算用户自己的图元。"""
    hit = []
    for e in d.entities:
        if e.id in ctx.modified and e.id in auto_ids:
            e.tags = [t for t in e.tags if t != "equip:auto"] + ["equip:detached"]
            hit.append(e.id)
        elif e.id in ctx.created and "equip:auto" in e.tags:
            e.tags = [t for t in e.tags if not t.startswith("equip")]
    gone = [i for i in ctx.deleted if i in auto_ids]
    if gone:
        det = list(d.vessel.drawing.get("detached") or [])
        d.vessel.drawing["detached"] = det + [i for i in gone if i not in det]
        hit += gone
    if hit:
        w = f"图元 {'、'.join(hit[:6])}{'…' if len(hit) > 6 else ''} 是设备图按参数生成的，现在已脱离参数，重新生成不会再覆盖它（要改设备请改参数）"
        res["warnings"] = list(dict.fromkeys(list(res.get("warnings", [])) + [w]))
        ctx.warnings.append(w)


def run_tool(svc, drawing_id: str | None, name: str, args: dict | None = None, utterance: str = "") -> dict:
    """统一入口：校验 + 事务 + 版本。返回 {"ok": true, ...} 或 {"ok": false, error_code, message, hint}。"""
    args = dict(args or {})
    t = REGISTRY.get(name)
    if t is None:
        return {"ok": False, "error_code": "UNKNOWN_TOOL", "message": f"没有函数 {name}", "hint": "从工具目录里选一个存在的函数"}
    try:
        if not t.needs_drawing:
            model = validate(t, args)
            try:
                return {"ok": True, **t.impl(svc, model)}
            except GeomError as ex:
                raise to_tool_error(ex)
        if not drawing_id:
            return {"ok": False, "error_code": "E_DRAWING_NOT_FOUND", "message": "缺少 drawing_id", "hint": "先 manage_document_file(action=new) 新建一张图"}
        if name == "history_undo_redo":
            model = validate(t, args)
            if model.action != "list":
                from .system_tools import undo_redo
                return undo_redo(svc, drawing_id, model.action, model.steps)
        if t.readonly:
            d = svc._read(drawing_id)
            if d.mode != "general":
                raise ToolError("WRONG_MODE", "这是流程图（pfd）模式的图纸，不能用通用 CAD 函数", "新建一张通用图，或使用流程图工具")
            return {"ok": True, "drawing_id": d.id, "revision": d.revision, **execute(d, t, args, utterance, svc=svc)}

        is_quiet = t.quiet or (t.quiet_if is not None and t.quiet_if(validate(t, args)))

        def fn(d: Drawing) -> dict:
            if d.mode != "general":
                raise ToolError("WRONG_MODE", "这是流程图（pfd）模式的图纸，不能用通用 CAD 函数", "新建一张通用图，或使用流程图工具")
            return execute(d, t, args, utterance, quiet=is_quiet, svc=svc)
        if is_quiet:
            return svc_mutate_quiet(svc, drawing_id, fn)
        return svc._mutate(drawing_id, fn)
    except ToolError as ex:
        return ex.to_dict()
    except Exception as ex:                                      # 未预料的错误：事务已中止（没有保存），返回结构化错误，不让 MCP 进程崩
        import logging
        logging.getLogger("zhihui.general").exception("%s failed", name)
        return {"ok": False, "error_code": "INTERNAL", "message": f"{name} 内部错误：{type(ex).__name__}: {ex}", "hint": "这是系统缺陷，图纸没有被修改；换一种方式重试，或把这条错误报告给开发者"}


def svc_mutate_quiet(svc, drawing_id: str, fn) -> dict:
    """视图/会话类操作：写 current.json，但不新增版本、不进撤销栈。"""
    import os
    try:
        with svc.store.lock(drawing_id):
            try:
                d = svc.store.load(drawing_id)
            except KeyError:
                raise ToolError("E_DRAWING_NOT_FOUND", f"没有图纸 {drawing_id}", "先新建或打开图纸")
            result = fn(d)
            base = svc.store._dir(d.id)
            tmp = base / "current.json.tmp"
            tmp.write_text(d.to_json(), encoding="utf-8")
            os.replace(tmp, base / "current.json")
            return {"ok": True, "drawing_id": d.id, "revision": d.revision, **result}
    except ToolError as ex:
        return ex.to_dict()


# ------------------------------------------------------------------ MCP 注册
def mcp_function(svc, t: Tool) -> Callable[..., str]:
    """为注册表里的函数生成带真实签名的 MCP 工具函数（参数来自 pydantic 字段）。"""
    params = []
    ann: dict[str, Any] = {}
    if t.needs_drawing:
        params.append(inspect.Parameter("drawing_id", inspect.Parameter.KEYWORD_ONLY, annotation=str))
        ann["drawing_id"] = str
    for n, f in t.model.model_fields.items():
        a = Annotated[(f.annotation, *f.metadata)] if f.metadata else f.annotation
        default = f.default if not f.is_required() else inspect.Parameter.empty
        if f.default_factory is not None:
            default = f.default_factory()
        params.append(inspect.Parameter(n, inspect.Parameter.KEYWORD_ONLY, default=default, annotation=a))
        ann[n] = a
    ann["return"] = str
    # 必需参数不能排在有默认值的参数后面（全是 keyword-only，顺序无所谓）
    def fn(**kw):
        did = kw.pop("drawing_id", None)
        res = run_tool(svc, did, t.name, kw)
        return json.dumps(res, ensure_ascii=False, separators=(",", ":"), default=str)
    fn.__name__ = t.name
    fn.__doc__ = t.doc or t.name
    fn.__signature__ = inspect.Signature(params, return_annotation=str)
    fn.__annotations__ = ann
    return fn


def register_mcp(server, svc) -> list[str]:
    names = []
    for t in REGISTRY.values():
        server.tool()(mcp_function(svc, t))
        names.append(t.name)
    return names


def catalog(categories: list[str] | None = None, include_system: bool = True) -> list[dict]:
    """给大模型看的工具目录：函数名 + 说明 + JSON Schema。"""
    out = []
    for t in tools_of(categories, include_system):
        schema = t.model.model_json_schema()
        schema.pop("title", None)
        out.append({"name": t.name, "category": t.category, "readonly": t.readonly, "description": t.doc, "parameters": schema})
    return out
