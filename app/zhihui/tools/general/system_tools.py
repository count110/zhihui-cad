"""系统函数：cad_batch_execute（事务批量）、history_undo_redo（撤销/重做）。"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import Field

from zhihui.cad.geom.errors import GeomError
from zhihui.ir import Drawing
from zhihui.tools.core import ToolError
from .ctx import Ctx, to_tool_error
from .refs import resolve_refs
from .registry import REGISTRY, Args, Ids, tool, validate


class StepArgs(Args):
    tool: str
    args: dict = Field(default_factory=dict)


class BatchArgs(Args):
    transaction_name: str = ""
    steps: list[StepArgs] = Field(min_length=1, max_length=40)


def _batch_quiet(a: BatchArgs) -> bool:
    """所有子步骤都是确定“不改图”的函数（视图、选择、绘图辅助开关…）时，整批也不落版本。参数里有变量引用、拿不准的一律按“会改图”处理。"""
    for st in a.steps:
        t = REGISTRY.get(st.tool)
        if t is None:
            return False
        if t.quiet:
            continue
        if t.quiet_if is not None:
            try:
                if t.quiet_if(validate(t, st.args)):
                    continue
            except Exception:                                     # noqa: BLE001  参数里有 $ 引用等，验证不了
                return False
        return False
    return True


@tool("cad_batch_execute", "system", BatchArgs, quiet_if=_batch_quiet)
def cad_batch_execute(ctx: Ctx, a: BatchArgs) -> dict:
    """事务批量执行：steps=[{tool, args}] 依次执行，全部成功才提交，只落一个版本；任何一步失败整体丢弃，返回 {failed_step, error_code, message, hint, completed_results}。批量内用 $this[i] 引用第 i 个子步骤的结果（如 $this[0].created[0]）。"""
    d = ctx.d
    results: list[dict] = []
    all_created: list[str] = []
    all_modified: list[str] = []
    all_deleted: list[str] = []
    warnings: list[str] = []
    notes: list[str] = []
    summaries: list[str] = []
    for i, st in enumerate(a.steps):
        t = REGISTRY.get(st.tool)
        try:
            if t is None:
                raise ToolError("UNKNOWN_TOOL", f"没有函数 {st.tool}", "从工具目录里选一个存在的函数")
            if st.tool == "cad_batch_execute":
                raise ToolError("NESTED_BATCH", "批量里不能再嵌套批量", "把子步骤摊平到同一个 steps 里")
            if not t.needs_drawing:
                raise ToolError("NOT_ALLOWED_IN_BATCH", f"{st.tool} 不能放在批量里", "文件操作请单独调用")
            args = resolve_refs(st.args, {}, results, d.session, leave=("steps",))
            model = validate(t, args)
            sub = Ctx(d, t.name, ctx.svc)
            try:
                res = t.impl(sub, model)
            except GeomError as ex:
                raise to_tool_error(ex)
            if not t.readonly:
                sub.record_session()
            results.append(res)
            all_created += [x for x in res.get("created", []) if x not in all_created]
            all_modified += [x for x in res.get("modified", []) if x not in all_modified and x not in all_created]
            all_deleted += res.get("deleted", [])
            warnings += res.get("warnings", [])
            notes += res.get("defaults_used", [])
            summaries.append(f"{i + 1}. {res.get('summary', t.name)}")
        except ToolError as ex:
            raise ToolError(ex.code, f"批量「{a.transaction_name or '未命名'}」第 {i + 1} 步（{st.tool}）失败：{ex.message}", ex.hint, failed_step=i, failed_tool=st.tool,
                            completed_results=[{"step": j, "summary": r.get("summary"), "created": r.get("created", [])} for j, r in enumerate(results)], **ex.extra)
    ctx.created = [x for x in all_created if x not in all_deleted]
    ctx.modified = [x for x in all_modified if x not in all_deleted]
    ctx.deleted = all_deleted
    ctx.warnings = warnings
    ctx.notes = list(dict.fromkeys(notes))
    kinds = {e.id: e.kind for e in d.entities if e.id in ctx.created}
    out = ctx.result(f"事务「{a.transaction_name or '批量'}」完成 {len(a.steps)} 步：" + "；".join(summaries), steps=[{"created": r.get("created", []), "modified": r.get("modified", []), "data": r.get("data")} for r in results])
    out["step_results"] = results
    return out


class UndoArgs(Args):
    action: Literal["undo", "redo", "list"]
    steps: int = Field(1, ge=1, le=50)


@tool("history_undo_redo", "system", UndoArgs, no_history=True, readonly=True)
def history_undo_redo(ctx: Ctx, a: UndoArgs) -> dict:
    """撤销 / 重做 / 列出历史。撤销把更早一版的内容恢复为一个新版本（审计链完整）；重做取回被撤销的那一版。"""
    d = ctx.d
    s = d.session
    if a.action == "list":
        return ctx.result(f"历史 {len(s.history)} 步，可重做 {len(s.redo)} 步", history=[{"rev": h["rev"], "tool": h["tool"], "utterance": h.get("utterance", ""), "args": h.get("args")} for h in reversed(s.history[-30:])],
                          redo=[{"rev": h["rev"], "tool": h["tool"]} for h in reversed(s.redo)])
    raise ToolError("INTERNAL", "undo/redo 由运行器处理（registry.run_tool）", "")


def undo_redo(svc, drawing_id: str, action: str, steps: int = 1) -> dict:
    """运行器：需要访问版本库，所以不走普通的 impl。"""
    try:
        with svc.store.lock(drawing_id):
            try:
                cur = svc.store.load(drawing_id)
            except KeyError:
                raise ToolError("E_DRAWING_NOT_FOUND", f"没有图纸 {drawing_id}", "")
            s = cur.session
            if action == "undo":
                if not s.history:
                    raise ToolError("NOTHING_TO_UNDO", "没有可撤销的操作", "")
                n = min(steps, len(s.history))
                undone = s.history[-n:]
                target_rev = undone[0]["prev_rev"]
                prev = svc.store.load(drawing_id, target_rev)
                keep_history = s.history[:-n]
                redo = s.redo + [{**h} for h in reversed(undone)]
                restored = prev
                restored.session.history = keep_history
                restored.session.redo = redo
                restored.session.selection = []
                restored.revision = cur.revision
                svc.store.save(restored)
                return {"ok": True, "drawing_id": drawing_id, "revision": restored.revision, "summary": f"已撤销 {n} 步（{'、'.join(h['tool'] for h in reversed(undone))}），恢复到第 {target_rev} 版内容",
                        "created": [], "modified": [], "deleted": [], "data": {"restored_from_rev": target_rev, "undone": [h["tool"] for h in undone]}}
            if not s.redo:
                raise ToolError("NOTHING_TO_REDO", "没有可重做的操作", "")
            n = min(steps, len(s.redo))
            redo = s.redo[:]
            take = [redo.pop() for _ in range(n)]           # 先被撤销的先重做
            target = take[-1]
            nxt = svc.store.load(drawing_id, target["rev"])
            nxt.session.history = (s.history + [{**h} for h in take])
            nxt.session.redo = redo
            nxt.session.selection = []
            nxt.revision = cur.revision
            svc.store.save(nxt)
            return {"ok": True, "drawing_id": drawing_id, "revision": nxt.revision, "summary": f"已重做 {n} 步（{'、'.join(h['tool'] for h in take)}）",
                    "created": [], "modified": [], "deleted": [], "data": {"redone": [h["tool"] for h in take]}}
    except ToolError as ex:
        return ex.to_dict()
