"""Executor（5.3）：确定性地执行计划。

1. 按 depends_on 拓扑排序，同一张图上顺序执行；
2. 每一步：解析变量引用 → 用注册表的 pydantic 模型校验参数 → 调用函数（经 MCP 或本地后端）；
3. 参数格式错误（INVALID_ARGS）可以让模型修一次（ZHIHUI_EXECUTOR_LLM=1），其他错误原样返回给 Planner；
4. 识别步骤交给 Recognizer；ambiguous 就停下来反问，not_found 当作失败；
5. 每一步推送事件：plan / step_start / recognize / tool_result / step_failed。
cad_batch_execute 保证失败不留半成品（版本号不变）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from zhihui.tools.core import ToolError
from zhihui.tools.general.refs import has_refs, resolve_refs
from zhihui.tools.general.registry import REGISTRY, validate

from .backend import Backend
from .plan import Plan, Step

MAX_TOOL_CALLS = 60
EventFn = Callable[[str, dict], Awaitable[None] | None]
RepairFn = Callable[[str, dict, dict, dict], Awaitable[dict | None]]


@dataclass
class ExecResult:
    status: str                                    # done / failed / clarify / replan
    results: dict[str, dict] = field(default_factory=dict)
    failed_step: str | None = None
    error: dict | None = None
    clarification: dict | None = None
    drawing_id: str | None = None
    created: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    log: list[dict] = field(default_factory=list)
    calls: int = 0
    files: list[dict] = field(default_factory=list)


def topo(steps: list[Step]) -> list[Step]:
    """稳定拓扑排序：依赖先行，其余保持计划里的顺序。"""
    done: set[str] = set()
    out: list[Step] = []
    pending = list(steps)
    while pending:
        progressed = False
        for st in list(pending):
            if all(d in done for d in st.depends_on):
                out.append(st)
                done.add(st.step_id)
                pending.remove(st)
                progressed = True
                break
        if not progressed:
            out.append(pending.pop(0))                 # 循环依赖：按原序放行，REF 解析会报错
    return out


def _brief(result: dict) -> str:
    if not result.get("ok"):
        return f"{result.get('error_code')}: {result.get('message', '')}"
    return str(result.get("summary") or "ok")[:160]


class Executor:
    def __init__(self, backend: Backend, drawing_id: str | None, on_event: EventFn | None = None, recognizer=None, repair: RepairFn | None = None, max_calls: int = MAX_TOOL_CALLS):
        self.backend = backend
        self.drawing_id = drawing_id
        self.on_event = on_event
        self.recognizer = recognizer
        self.repair = repair if repair is not None else None
        self.max_calls = max_calls
        self.calls = 0

    async def emit(self, kind: str, payload: dict) -> None:
        if self.on_event:
            r = self.on_event(kind, payload)
            if hasattr(r, "__await__"):
                await r

    # ------------------------------------------------------------ 主流程
    async def run(self, plan: Plan, prior: dict[str, dict] | None = None) -> ExecResult:
        res = ExecResult("done", results=dict(prior or {}), drawing_id=self.drawing_id)
        await self.emit("plan", {"thought": plan.thought, "intent_categories": plan.intent_categories, "preview": plan.user_response_preview,
                                 "steps": [{"step_id": s.step_id, "summary": s.action_summary, "agent": s.target_agent, "tool": s.payload.get("tool") or "识别", "args": s.payload.get("args")} for s in plan.workflow_steps]})
        for st in topo(plan.workflow_steps):
            if prior and st.step_id in prior:
                continue                                  # 恢复执行：已经完成（或用户已经替识别步骤作了选择）的步骤不再重跑
            if self.calls >= self.max_calls:
                res.status, res.failed_step = "failed", st.step_id
                res.error = {"ok": False, "error_code": "BUDGET_EXCEEDED", "message": f"这个任务的函数调用次数超过上限 {self.max_calls}", "hint": "把任务拆小"}
                break
            await self.emit("step_start", {"step_id": st.step_id, "summary": st.action_summary, "agent": st.target_agent})
            if st.target_agent == "entity_recognition_agent":
                out = await self._recognize(st, res)
            else:
                out = await self._tool(st, res)
            res.log.append({"step_id": st.step_id, "agent": st.target_agent, "summary": st.action_summary, "ok": out["ok"], "brief": out["brief"]})
            if out["kind"] == "clarify":
                res.status, res.failed_step, res.clarification = "clarify", st.step_id, out["clarification"]
                break
            if not out["ok"]:
                res.status, res.failed_step, res.error = "failed", st.step_id, out["error"]
                await self.emit("step_failed", {"step_id": st.step_id, "error": out["error"], "summary": st.action_summary})
                break
            if plan.replan_after == st.step_id:
                res.status = "replan"
                break
        res.drawing_id = self.drawing_id
        res.calls = self.calls
        return res

    # ------------------------------------------------------------ 工具步骤
    async def _tool(self, st: Step, res: ExecResult) -> dict:
        tool, args = st.payload.get("tool"), dict(st.payload.get("args") or {})
        t = REGISTRY.get(tool)
        if t is None:
            err = {"ok": False, "error_code": "UNKNOWN_TOOL", "message": f"没有函数 {tool}", "hint": "从工具目录里选一个存在的函数"}
            return {"ok": False, "kind": "tool", "error": err, "brief": _brief(err)}
        try:
            args = await self._resolve(args, res, batch=(tool == "cad_batch_execute"))
            model = validate(t, args) if tool != "cad_batch_execute" else None
        except ToolError as ex:
            if ex.code == "INVALID_ARGS" and self.repair is not None and os.environ.get("ZHIHUI_EXECUTOR_LLM") == "1":
                fixed = await self.repair(tool, t.model.model_json_schema(), args, ex.to_dict())
                if fixed is not None:
                    try:
                        validate(t, fixed)
                        args = fixed
                        ex = None
                    except ToolError as ex2:
                        ex = ex2
            if ex is not None:
                err = ex.to_dict()
                return {"ok": False, "kind": "tool", "error": err, "brief": _brief(err)}
        out = await self._call(tool, args, st, res)
        if not out["ok"] and out["error"].get("error_code") == "INVALID_ARGS" and self.repair is not None and os.environ.get("ZHIHUI_EXECUTOR_LLM") == "1":
            fixed = await self.repair(tool, t.model.model_json_schema(), args, out["error"])
            if fixed is not None:
                out = await self._call(tool, fixed, st, res)
        return out

    async def _call(self, tool: str, args: dict, st: Step, res: ExecResult) -> dict:
        self.calls += 1
        if tool == "manage_document_file" and args.get("action") in ("save", "save_as", "close") and not args.get("drawing_id") and self.drawing_id:
            args = {**args, "drawing_id": self.drawing_id}                    # “新建一张图，导出成 DXF”：保存的就是刚新建/当前的那张
        r = await self.backend.call(tool, args, self.drawing_id)
        if not r.get("ok"):
            return {"ok": False, "kind": "tool", "error": r, "brief": _brief(r)}
        res.results[st.step_id] = r
        for key, bucket in (("created", res.created), ("modified", res.modified), ("deleted", res.deleted)):
            for x in r.get(key, []) or []:
                if x not in bucket:
                    bucket.append(x)
        res.warnings += [w for w in r.get("warnings", []) if w not in res.warnings]
        res.notes += [n for n in r.get("defaults_used", []) if n not in res.notes]
        data = r.get("data") or {}
        f = data.get("file")
        if isinstance(f, dict):
            res.files.append(f)
        for f in data.get("files") or []:                                   # 一次导出多个文件（设备图 DXF + PDF）
            if isinstance(f, dict):
                res.files.append(f)
        if tool == "manage_document_file" and r.get("drawing_id") and args.get("action") in ("new", "open"):
            self.drawing_id = r["drawing_id"]
            await self.emit("drawing", {"drawing_id": self.drawing_id, "reason": "new" if args.get("action") == "new" else "open"})
        if isinstance(data.get("switch_to"), dict):
            await self.emit("switch", data["switch_to"])
        await self.emit("tool_result", {"step_id": st.step_id, "tool": tool, "summary": r.get("summary", ""), "revision": r.get("revision"), "created": r.get("created", []), "modified": r.get("modified", []),
                                        "deleted": r.get("deleted", []), "warnings": r.get("warnings", []), "drawing_id": self.drawing_id})
        return {"ok": True, "kind": "tool", "result": r, "brief": _brief(r)}

    # ------------------------------------------------------------ 识别步骤
    async def _recognize(self, st: Step, res: ExecResult) -> dict:
        p = st.payload
        try:
            if "tool" in p:                                                    # 直接调用感知函数
                args = await self._resolve(dict(p.get("args") or {}), res)
                self.calls += 1
                r = await self.backend.call(p["tool"], args, self.drawing_id)
                if not r.get("ok"):
                    return {"ok": False, "kind": "recognize", "error": r, "brief": _brief(r)}
                norm = normalize_perception(p["tool"], r)
                res.results[st.step_id] = norm
                await self.emit("recognize", {"step_id": st.step_id, "method": "tool", "tool": p["tool"], "status": norm.get("status"), "entity_ids": norm.get("entity_ids", []), "message": r.get("summary", "")})
                return {"ok": True, "kind": "recognize", "result": norm, "brief": _brief(r)}
            if self.recognizer is None:
                err = {"ok": False, "error_code": "NO_RECOGNIZER", "message": "没有配置识别器", "hint": ""}
                return {"ok": False, "kind": "recognize", "error": err, "brief": _brief(err)}
            rr = await self.recognizer.resolve(p["query"], p.get("expect"), p.get("need"), self.drawing_id, resolved_ref=lambda a: self._resolve(a, res))
            self.calls += max(1, rr.get("calls", 1))
            await self.emit("recognize", {"step_id": st.step_id, "method": rr.get("method"), "query": p["query"], "status": rr["status"], "entity_ids": rr.get("entity_ids", []),
                                          "message": rr.get("log") or rr.get("ambiguity_message") or "", "candidates": rr.get("candidates", [])})
            if rr["status"] == "ambiguous":
                cl = {"question": rr["ambiguity_message"], "options": rr.get("options", []), "candidates": rr.get("entity_ids", []), "step_id": st.step_id}
                return {"ok": False, "kind": "clarify", "clarification": cl, "brief": "ambiguous", "error": {"ok": False, "error_code": "AMBIGUOUS", "message": rr["ambiguity_message"]}}
            if rr["status"] == "not_found":
                err = {"ok": False, "error_code": "NOT_FOUND", "message": rr.get("ambiguity_message") or f"没有找到「{p['query']}」", "hint": rr.get("hint", "换一种说法，或直接给出图元编号")}
                return {"ok": False, "kind": "recognize", "error": err, "brief": _brief(err)}
            res.results[st.step_id] = rr
            return {"ok": True, "kind": "recognize", "result": rr, "brief": rr.get("log", "resolved")}
        except ToolError as ex:
            return {"ok": False, "kind": "recognize", "error": ex.to_dict(), "brief": _brief(ex.to_dict())}

    # ------------------------------------------------------------ 引用
    async def _resolve(self, args: Any, res: ExecResult, batch: bool = False) -> Any:
        if not has_refs(args):
            return args
        session = await self.backend.session(self.drawing_id) if self.drawing_id else None
        leave = ("this", "session") if batch else ("this",)
        return resolve_refs(args, res.results, None, session, leave=leave)


def normalize_perception(tool: str, r: dict) -> dict:
    """感知函数的原始结果 → 统一的引用友好结构：entity_ids / points（按类型和序号都能取）/ boundary。"""
    data = r.get("data") or {}
    out: dict = {"ok": True, "status": "resolved", "method": "tool", "tool": tool, "summary": r.get("summary", "")}
    if tool == "query_entities_by_filter":
        cands = data.get("candidates", [])
        out["entity_ids"] = [c["entity_id"] for c in cands]
        out["matched_entities"] = cands
        out["status"] = "resolved" if len(cands) == 1 else ("not_found" if not cands else "ambiguous")
    elif tool == "resolve_contextual_reference":
        out["entity_ids"] = data.get("entity_ids", [])
        out["matched_entities"] = data.get("entities", [])
    elif tool == "get_entity_snap_points":
        pts = data.get("snap_points", [])
        by: dict = {}
        for p in pts:
            by.setdefault(p["type"], p["coord"])
        out["points"] = {**by, "_list": [p["coord"] for p in pts]}
        if pts:
            out["points"]["primary"] = pts[0]["coord"]                    # 和规则识别器一致：$sN.points.primary 取第一个求出来的点
        out["entity_ids"] = list(dict.fromkeys(p.get("entity_id") for p in pts if p.get("entity_id")))
    elif tool == "find_closed_boundary":
        out["boundary"] = {"outer": data.get("outer_loop_entity_ids", []), "islands": [i["entity_ids"] for i in data.get("island_loops", [])], "outer_points": data.get("outer_loop_points", []),
                           "is_closed": data.get("is_closed", False), "area": data.get("enclosed_area")}
        out["entity_ids"] = data.get("outer_loop_entity_ids", [])
    else:
        out["data"] = data
    out["created"], out["modified"], out["deleted"] = [], [], []
    return out
