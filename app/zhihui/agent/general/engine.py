"""通用 CAD 智能体入口：口述 → 规范化 → 命令快速通道 / Planner → 护栏 → Executor（含识别）→ 校验 → 出图。

和 Orchestrator / Coordinator 接口一致：run(prompt, file_ref, resume, answer) → Outcome，事件通过 on_event 推给后端。
反问时把执行状态存进 Outcome.messages（一条 role=general_state 的消息），用户回答后从断点继续，已经完成的步骤不重做。
"""
from __future__ import annotations

import json
import os
import re
import time
from contextlib import asynccontextmanager
from typing import Any

from zhihui.agent import command_line as CL
from zhihui.agent.orchestrator import Outcome, mcp_client
from zhihui.agent.speech_normalize import normalize

from .backend import Backend, McpBackend
from .executor import ExecResult, Executor, MAX_TOOL_CALLS
from .guards import GuardIssue, blocking, check_plan, check_vessel_patch, invented_values, utterance_numbers
from .plan import Plan, plan_dict
from .planner import LLMUnavailable, Planner
from .recognizer import Recognizer
from .router import categories_of

MAX_SECONDS = 180
MAX_REPLAN = 2
EQUIP_CAT = "chem_equipment"
# 设备图函数返回这些错误码时，引擎直接转成反问（mock 和 real 一样；问题由代码按 hint/pending 生成，不调用模型，也不消耗重规划次数）
VESSEL_CLARIFY = {"SPEC_INCOMPLETE", "STD_SIZE_NOT_FOUND", "NOZZLE_NO_ROOM", "SPEC_CONFLICT", "NOT_FOUND", "AMBIGUOUS", "INVALID_VALUE", "NOT_SUPPORTED", "HEAD_UNSUPPORTED"}
ID_RX = re.compile(r"(?<![A-Za-z])(PT|ML|RY|XL|MS|TL|LD|PL|SP|EL|MT|[LCABDHTS])\s*(\d+)(?!\d)", re.I)


def _state_msg(state: dict) -> list[dict]:
    return [{"role": "general_state", "content": json.dumps(state, ensure_ascii=False, default=str)}]


class GeneralAgent:
    def __init__(self, on_event=None, task_id: str | None = None, db=None, mock: bool = False, backend: Backend | None = None, drawing_id: str | None = None,
                 route_hint: str | None = None):
        self.on_event_fn = on_event
        self.task_id = task_id
        self.db = db
        self.mock = mock
        self._backend = backend
        self.drawing_id = drawing_id
        self.usage_d: dict = {"prompt_tokens": 0, "completion_tokens": 0, "models": set()}
        self.planner = Planner(mock, self.usage_d)
        self.tool_calls = 0
        self.t0 = time.monotonic()
        self.be: Backend | None = None
        self.replans = 0
        self.defaults: list[str] = []
        self.warnings: list[str] = []
        self.log: list[dict] = []
        self.numbers: set[float] = set()
        self.results: dict = {}                 # 最近一次执行的各步结果（基准断言和排错用）
        self.route_hint = route_hint            # 入口路由结果：equipment 时新建化工设备图样板、类别并入 chem_equipment
        self.turn_id = task_id or f"t{int(time.time() * 1000) % 10 ** 9}"     # 本轮编号：设备参数里用户值的来源记为 user:<turn_id>
        self.last_question: dict | None = None

    # ------------------------------------------------------------ 事件与用量
    async def emit(self, kind: str, payload: dict) -> None:
        if self.on_event_fn:
            r = self.on_event_fn(kind, {"agent": "general", **payload})
            if hasattr(r, "__await__"):
                await r

    def usage(self) -> dict:
        u = {k: v for k, v in self.usage_d.items() if k != "models"}
        u.update({"tool_calls": self.tool_calls, "seconds": round(time.monotonic() - self.t0, 1), "models": sorted(self.usage_d.get("models", [])), "planner": self.planner.source})
        return u

    @asynccontextmanager
    async def _backend_ctx(self):
        if self._backend is not None:
            yield self._backend
            return
        async with mcp_client(general=True) as client:
            yield McpBackend(client)

    # ------------------------------------------------------------ 入口
    async def run(self, prompt: str, file_ref: str | None = None, resume: list[dict] | None = None, answer: str | None = None) -> Outcome:
        async with self._backend_ctx() as be:
            self.be = be
            self.recognizer = Recognizer(be, self.planner.recognizer_calls if not self.mock else None)
            state = None
            if resume:
                for m in resume:
                    if m.get("role") == "general_state":
                        state = json.loads(m["content"])
            if state:
                self.drawing_id = state.get("drawing_id") or self.drawing_id
            try:
                if state and answer is not None:
                    return await self._resume(state, answer)
                return await self._fresh(prompt, file_ref)
            except CL.CmdError as ex:
                return Outcome("FAILED", ex.message + (f"（{ex.hint}）" if ex.hint else ""), self.drawing_id, usage=self.usage())
            except LLMUnavailable as ex:                         # 没接入大模型 / 接口失败：明确告诉用户，不退回规则
                await self.emit("llm_unavailable", {"reason": ex.reason, "message": str(ex)})
                return Outcome("FAILED", str(ex), self.drawing_id, usage=self.usage())

    async def run_plan(self, plan: Plan, prompt: str = "") -> Outcome:
        """跳过 Planner，直接执行一份写好的计划（基准 scripted 模式、回放）。仍然过护栏：计划里的数值必须在口令里有出处。"""
        async with self._backend_ctx() as be:
            self.be = be
            self.recognizer = Recognizer(be, None)
            norm = normalize(prompt)
            self.numbers = utterance_numbers(norm.text) | utterance_numbers(prompt)
            self.planner.source = "scripted"
            return await self._after_plan(plan, norm.text, prompt, [], {})

    # ------------------------------------------------------------ 新任务
    async def _fresh(self, prompt: str, file_ref: str | None) -> Outcome:
        be = self.be
        await self.emit("status", {"status": "PLANNING"})
        if file_ref and file_ref.lower().endswith((".dxf", ".json")):
            r = await be.call("manage_document_file", {"action": "open", "file_ref": file_ref}, None)
            self.tool_calls += 1
            if not r.get("ok"):
                return Outcome("FAILED", f"打开文件失败：{r.get('message')}", None, usage=self.usage())
            self.drawing_id = r["drawing_id"]
            await self.emit("drawing", {"drawing_id": self.drawing_id, "reason": "open"})
        norm = normalize(prompt)
        self.numbers = utterance_numbers(norm.text) | utterance_numbers(prompt)
        if norm.changed:
            await self.emit("normalized", {"original": prompt, "normalized": norm.text, "changes": norm.changes[:12]})
        # ---- 命令快速通道（不经过模型）----
        cmd = CL.parse(norm.text) if "\n" not in norm.text else None
        if cmd is not None:
            if cmd.help:
                return Outcome("READY", cmd.help, self.drawing_id, usage=self.usage())
            await self._ensure_drawing()
            plan = Plan.model_validate({"thought": "命令快速通道", "intent_categories": [], "normalized_utterance": norm.text, "workflow_steps": [{"step_id": "s1", "target_agent": "tool_execution_agent", "depends_on": [], "action_summary": cmd.summary,
                                                                                                                                                   "payload": cmd.as_call()}], "user_response_preview": ""})
            self.planner.source = "command"
            return await self._execute(plan, norm.text, prompt, None)
        # ---- Planner ----
        await self._ensure_drawing(only_if_needed=norm.text)
        ctx = await be.context(self.drawing_id) if self.drawing_id else {}
        cats = self._with_equipment(categories_of(norm.text), ctx)
        plan = await self.planner.plan(norm.text, prompt, ctx, cats)
        return await self._after_plan(plan, norm.text, prompt, cats, ctx)

    def _with_equipment(self, cats: list[str], ctx: dict) -> list[str]:
        """入口路由是设备图，或当前图纸已有设备参数 / 是设备图样板：chem_equipment 始终在工具子集里（任务书 6.2-5、6.2-7）。"""
        if self.route_hint == "equipment" or (ctx or {}).get("vessel") or (ctx or {}).get("workspace") == EQUIP_CAT:
            return [EQUIP_CAT] + [c for c in cats if c != EQUIP_CAT]
        return cats

    async def _ensure_drawing(self, only_if_needed: str | None = None) -> None:
        """用户没有先新建图就直接画：自动建一张空白 A3（计划里自己有“新建”就不建）。"""
        if self.drawing_id:
            return
        if self.route_hint == "equipment":                         # 设备图：新建化工设备图样板，不画进别的图
            r = await self.be.call("manage_document_file", {"action": "new", "template_name": "hg_equipment_a1"}, None)
            self.tool_calls += 1
            if r.get("ok"):
                self.drawing_id = r["drawing_id"]
                await self.emit("drawing", {"drawing_id": self.drawing_id, "reason": "auto"})
            return
        if only_if_needed is not None and re.search(r"新建.{0,8}图|新建.{0,4}(?:文件|文档)", only_if_needed):
            return
        tpl = "gb_a3_mech" if (only_if_needed and re.search(r"机械", only_if_needed)) else "gb_a3_arch" if (only_if_needed and re.search(r"建筑", only_if_needed)) else "blank_a3"
        r = await self.be.call("manage_document_file", {"action": "new", "template_name": tpl}, None)
        self.tool_calls += 1
        if r.get("ok"):
            self.drawing_id = r["drawing_id"]
            self.defaults.append(f"还没有图纸，自动新建了一张 {tpl}")
            await self.emit("drawing", {"drawing_id": self.drawing_id, "reason": "auto"})

    def _guard(self, plan: Plan, norm_text: str, prompt: str) -> tuple[list[GuardIssue], dict]:
        vg = check_vessel_patch(plan, norm_text, prompt)               # 设备补丁护栏：先剔除编出来的可默认字段，再查数值出处
        return blocking(check_plan(plan, norm_text, prompt, self.numbers)) + vg["block"], vg

    async def _after_plan(self, plan: Plan, norm_text: str, prompt: str, cats: list[str], ctx: dict) -> Outcome:
        blk, vg = self._guard(plan, norm_text, prompt)
        if blk and not self.mock and self.replans < MAX_REPLAN:
            self.replans += 1
            await self.emit("guard", {"issues": [i.as_dict() for i in blk]})
            plan = await self.planner.plan(norm_text, prompt, ctx, cats, None, "护栏拦截：" + "；".join(i.message for i in blk))
            blk, vg = self._guard(plan, norm_text, prompt)
        if blk:
            await self.emit("guard", {"issues": [i.as_dict() for i in blk]})
            return Outcome("FAILED", "计划没有通过护栏检查：" + "；".join(i.message for i in blk[:3]), self.drawing_id, usage=self.usage())
        if vg["warn"]:
            await self.emit("guard", {"issues": [i.as_dict() for i in vg["warn"]]})
            self.warnings += [i.message for i in vg["warn"] if i.message not in self.warnings]
        if vg["clarify"]:                                          # “高/长”口径不清：确定性反问，不让模型猜
            return await self._ask(vg["clarify"], prompt, "你说的长度口径需要确认")
        if plan.clarification_needed:
            items = [it.model_dump() for it in plan.clarification.items]
            q = {"question": plan.clarification.question, "options": plan.clarification.options[:4], "candidates": plan.clarification.candidates, **({"items": items} if items else {})}
            await self.emit("clarify", q)
            self.last_question = q
            return Outcome("WAITING_CONFIRMATION", q["question"], self.drawing_id, question=q, usage=self.usage(), messages=_state_msg({"kind": "ask", "utterance": prompt, "drawing_id": self.drawing_id, "question": q}))
        return await self._execute(plan, norm_text, prompt, None, cats, ctx)

    async def _ask(self, items: list[dict], prompt: str, lead: str = "") -> Outcome:
        """由代码根据待问项拼出一个问题（一次问完），不调用模型。"""
        from zhihui.cad.equipment.spec_ops import question_text
        text = question_text(items) if len(items) > 1 or not lead else f"{items[0]['label']}"
        q = {"question": text, "options": (items[0].get("options") or [])[:4] if len(items) == 1 else [], "candidates": [], "items": items}
        await self.emit("clarify", q)
        self.last_question = q
        return Outcome("WAITING_CONFIRMATION", text, self.drawing_id, question=q, usage=self.usage(), messages=_state_msg({"kind": "ask", "utterance": prompt, "drawing_id": self.drawing_id, "question": q}))

    # ------------------------------------------------------------ 执行
    async def _execute(self, plan: Plan, norm_text: str, prompt: str, prior: dict | None, cats: list[str] | None = None, ctx: dict | None = None) -> Outcome:
        be = self.be
        await self.emit("status", {"status": "EXECUTING"})
        if plan.user_response_preview:
            self.defaults.append(plan.user_response_preview)
        results: dict = dict(prior or {})
        steps_done: list[dict] = []
        self._inject_turn(plan)
        while True:
            ex = Executor(be, self.drawing_id, self.emit, self.recognizer, self.planner.repair_args if os.environ.get("ZHIHUI_EXECUTOR_LLM") == "1" else None, max_calls=MAX_TOOL_CALLS - self.tool_calls)
            res = await ex.run(plan, results)
            self.drawing_id = ex.drawing_id or self.drawing_id
            self.tool_calls += res.calls
            results = res.results
            self.results = res.results
            steps_done += res.log
            self.defaults += [n for n in res.notes if n not in self.defaults]
            self.warnings += [w for w in res.warnings if w not in self.warnings]
            if res.status == "done":
                await self._recheck_vessel(plan, norm_text + "；" + prompt)
                return await self._finish(plan, res, steps_done)
            if res.status == "clarify":
                cl = res.clarification or {}
                q = {"question": cl.get("question", ""), "options": cl.get("options", [])[:4], "candidates": cl.get("candidates", [])}
                await self.emit("clarify", q)
                state = {"kind": "pick", "plan": plan_dict(plan), "results": results, "step": res.failed_step, "drawing_id": self.drawing_id, "options": cl.get("options", []), "candidates": cl.get("candidates", []),
                         "utterance": prompt, "normalized": norm_text, "steps_done": steps_done}
                return Outcome("WAITING_CONFIRMATION", q["question"], self.drawing_id, question=q, usage=self.usage(), messages=_state_msg(state))
            if res.status == "replan":                                       # 计划里写了 replan_after：看过前面的结果再规划后面的
                if self.replans >= MAX_REPLAN:
                    return Outcome("FAILED", "重新规划次数超过上限", self.drawing_id, usage=self.usage())
                self.replans += 1
                prior_summary = [{"step": k, "summary": v.get("summary"), "entity_ids": v.get("entity_ids"), "created": v.get("created")} for k, v in results.items()]
                ctx2 = await be.context(self.drawing_id) if self.drawing_id else {}
                plan = await self.planner.plan(norm_text, prompt, ctx2, cats or [], prior_summary)
                results = {}
                if plan.clarification_needed:
                    q = {"question": plan.clarification.question, "options": plan.clarification.options[:4], "candidates": plan.clarification.candidates}
                    return Outcome("WAITING_CONFIRMATION", q["question"], self.drawing_id, question=q, usage=self.usage(), messages=_state_msg({"kind": "ask", "utterance": prompt, "drawing_id": self.drawing_id, "question": q}))
                continue
            # ---- 失败 ----
            err = res.error or {}
            code = err.get("error_code", "")
            fstep = next((s for s in plan.workflow_steps if s.step_id == res.failed_step), None)
            ftool = (fstep.payload.get("tool") if fstep else "") or ""
            if ftool == "cad_batch_execute":                                 # 设备函数被模型塞进了批量：批量的错误里带着失败的子函数名
                ftool = err.get("failed_tool") or ftool
            if ftool.startswith("vessel_") and code in VESSEL_CLARIFY:
                items = err.get("pending") or []
                if not items:
                    opts = err.get("options") or err.get("candidates") or []
                    items = [{"path": "", "label": err.get("message", "需要确认") + (f"（{err['hint']}）" if err.get("hint") and code != "AMBIGUOUS" else ""), "options": [str(o) for o in opts][:4]}]
                q = {"question": err.get("message", ""), "options": items[0].get("options", [])[:4] if len(items) == 1 else [],
                     "candidates": err.get("candidates", []), "items": items, "error_code": code}
                await self.emit("clarify", q)
                self.last_question = q
                state = {"kind": "ask", "utterance": prompt, "drawing_id": self.drawing_id, "question": q}
                return Outcome("WAITING_CONFIRMATION", q["question"], self.drawing_id, question=q, usage=self.usage(), messages=_state_msg(state))
            if code == "CONFIRM_REQUIRED":
                q = {"question": err.get("message", "需要确认") + "。确认执行吗？", "options": ["确认", "取消"], "candidates": []}
                state = {"kind": "confirm", "plan": plan_dict(plan), "results": results, "step": res.failed_step, "drawing_id": self.drawing_id, "utterance": prompt, "normalized": norm_text, "steps_done": steps_done}
                return Outcome("WAITING_CONFIRMATION", q["question"], self.drawing_id, question=q, usage=self.usage(), messages=_state_msg(state))
            if code == "LAYER_LOCKED":
                q = {"question": err.get("message", "图层已锁定") + "。要先解锁这个图层再继续吗？", "options": ["解锁并继续", "取消"], "candidates": []}
                m = re.search(r"「([^」]+)」", err.get("message", ""))
                state = {"kind": "unlock", "plan": plan_dict(plan), "results": results, "step": res.failed_step, "layer": m.group(1) if m else None, "drawing_id": self.drawing_id, "utterance": prompt, "normalized": norm_text, "steps_done": steps_done}
                return Outcome("WAITING_CONFIRMATION", q["question"], self.drawing_id, question=q, usage=self.usage(), messages=_state_msg(state))
            if code in ("MISSING_ARG", "DEVIATION_UNKNOWN", "SIZE_UNKNOWN", "KEYWAY_UNKNOWN") and not self.mock and self.replans < MAX_REPLAN:
                self.replans += 1
                q = {"question": err.get("message", "缺少参数") + ("；" + err["hint"] if err.get("hint") else ""), "options": [], "candidates": []}
                state = {"kind": "ask_resume", "plan": plan_dict(plan), "results": results, "step": res.failed_step, "drawing_id": self.drawing_id, "utterance": prompt, "normalized": norm_text, "question": q, "steps_done": steps_done}
                return Outcome("WAITING_CONFIRMATION", q["question"], self.drawing_id, question=q, usage=self.usage(), messages=_state_msg(state))
            if not self.mock and self.replans < MAX_REPLAN and code not in ("NOT_FOUND", "BUDGET_EXCEEDED", "OVER_CONSTRAINED", "UNSOLVABLE"):
                self.replans += 1
                ctx2 = await be.context(self.drawing_id) if self.drawing_id else {}
                fb = f"步骤 {res.failed_step} 失败：[{code}] {err.get('message')}。提示：{err.get('hint', '')}"
                await self.emit("replan", {"reason": fb})
                prior_summary = [{"step": k, "summary": v.get("summary")} for k, v in results.items()]
                plan = await self.planner.plan(norm_text, prompt, ctx2, cats or [], prior_summary, fb)
                results = {}
                if plan.clarification_needed:
                    q = {"question": plan.clarification.question, "options": plan.clarification.options[:4], "candidates": plan.clarification.candidates}
                    return Outcome("WAITING_CONFIRMATION", q["question"], self.drawing_id, question=q, usage=self.usage(), messages=_state_msg({"kind": "ask", "utterance": prompt, "drawing_id": self.drawing_id, "question": q}))
                continue
            return await self._failed(plan, res, steps_done)

    async def _failed(self, plan: Plan, res: ExecResult, steps_done: list[dict]) -> Outcome:
        err = res.error or {}
        step = next((s for s in plan.workflow_steps if s.step_id == res.failed_step), None)
        msg = f"第 {res.failed_step} 步「{step.action_summary if step else ''}」没有完成：{err.get('message', '未知错误')}"
        if err.get("hint"):
            msg += f"（{err['hint']}）"
        done = [f"{s['step_id']} {s['summary']}" for s in steps_done if s["ok"]]
        if done:
            msg += f"。已完成：{'；'.join(done)}"
        arts = await self._artifacts() if res.created or res.modified else []
        return Outcome("FAILED", msg, self.drawing_id, artifacts=arts, usage=self.usage(), report=json.dumps({"steps": steps_done, "error": err}, ensure_ascii=False))

    # ------------------------------------------------------------ 收尾
    async def _finish(self, plan: Plan, res: ExecResult, steps_done: list[dict]) -> Outcome:
        be = self.be
        await self.emit("status", {"status": "VALIDATING"})
        validation = None
        if self.drawing_id:
            v = await be.call("validate_drawing", {"drawing_id": self.drawing_id}, self.drawing_id)
            if v.get("ok"):
                validation = {"passed": v["passed"], "errors": v.get("errors", []), "warnings": v.get("warnings", []), "stats": v.get("stats", {})}
        arts = await self._artifacts()
        if any(st.payload.get("tool") == "vessel_generate_drawing" for st in plan.workflow_steps):
            asked = {f.get("format") for f in res.files}                                 # 设备图的导出文件名带“_设备图”，和自动导出的同格式文件是同一张图：以口令要的为准，不给两份
            arts = [a for a in arts if a.get("format") not in asked or a.get("preview")]  # 模型空间预览图（preview=model）照给
        have = {a.get("file_ref") for a in arts}
        arts += [f for f in res.files if f.get("file_ref") not in have]                 # 口令里要求输出的文件（PDF、STL…）也交给用户
        parts = [s["summary"] for s in steps_done if s["ok"] and s["summary"]]
        msg = ("已完成：" + "；".join(parts[:8]) + ("…" if len(parts) > 8 else "")) if parts else "已执行"
        notes = list(dict.fromkeys(self.defaults))
        if notes:
            msg += "。说明：" + "；".join(notes[:5])
        warns = list(self.warnings)
        if validation:
            warns += [w["message"] for w in validation["warnings"][:5]]
        status = "READY"
        if validation and not validation["passed"]:
            status = "READY_WITH_WARNINGS"
            warns = [e["message"] for e in validation["errors"][:5]] + warns
        elif warns:
            status = "READY_WITH_WARNINGS"
        if warns:
            msg += "。注意：" + "；".join(dict.fromkeys(warns))[:300]
        return Outcome(status, msg, self.drawing_id, artifacts=arts, validation=validation, usage=self.usage(), report=json.dumps({"steps": steps_done, "created": res.created, "modified": res.modified, "deleted": res.deleted}, ensure_ascii=False))

    async def _artifacts(self) -> list[dict]:
        if not self.drawing_id:
            return []
        r = await self.be.call("export_artifacts", {"drawing_id": self.drawing_id, "formats": ["dxf", "png", "svg"], "allow_warnings": True}, self.drawing_id)
        if not r.get("ok"):
            self.warnings.append(f"出图失败：{r.get('message')}")
            return []
        return [f for f in r.get("files", []) if f.get("format") in ("dxf", "png", "svg", "pdf")]

    # ------------------------------------------------------------ 反问之后
    async def _resume(self, state: dict, answer: str) -> Outcome:
        kind = state["kind"]
        norm = normalize(answer)
        await self.emit("status", {"status": "PLANNING"})
        if kind == "ask":
            items = ((state.get("question") or {}).get("items")) or []
            if any(it.get("path") == "drawing_type" for it in items) and re.search(r"流程图|工艺流程|PFD|P&ID", answer, re.I) and not re.search(r"设备图|装配图", answer):
                await self.emit("switch", {"mode": "pfd", "prompt": state["utterance"], "reason": "用户选择工艺流程图"})
                return Outcome("READY", "按工艺流程图处理：已转到「化工流程图」工作空间（原话已带过去）。", self.drawing_id, usage=self.usage())
            return await self._fresh(state["utterance"] + "；" + norm.text, None)
        plan = Plan.model_validate(state["plan"])
        results = state.get("results", {})
        prompt, norm_text = state.get("utterance", ""), state.get("normalized", "")
        self.numbers = utterance_numbers(norm_text) | utterance_numbers(prompt) | utterance_numbers(norm.text)
        if kind == "pick":
            step = next(s for s in plan.workflow_steps if s.step_id == state["step"])
            ids = self._parse_pick(answer, state.get("candidates", []), state.get("options", []))
            if not ids:
                q = {"question": "没有理解你的选择，请点图上的候选图元，或回答其中一个编号：" + "、".join(state.get("candidates", [])), "options": state.get("options", [])[:4], "candidates": state.get("candidates", [])}
                return Outcome("WAITING_CONFIRMATION", q["question"], self.drawing_id, question=q, usage=self.usage(), messages=_state_msg(state))
            rr = await self.recognizer.from_ids(step.payload.get("query", ""), ids, step.payload.get("need"), self.drawing_id)
            await self.emit("recognize", {"step_id": step.step_id, "method": "user", "status": "resolved", "entity_ids": ids, "message": rr["log"]})
            results[step.step_id] = rr
            return await self._execute(plan, norm_text, prompt, results)
        if kind == "confirm":
            if not re.search(r"确认|是|好|可以|同意|继续|y|yes|ok", answer, re.I) or re.search(r"不|取消|别|否", answer):
                return Outcome("READY", "已取消，没有改动。", self.drawing_id, usage=self.usage())
            for st in plan.workflow_steps:
                if st.step_id == state["step"]:
                    self._set_confirmed(st.payload)
            return await self._execute(plan, norm_text, prompt, results)
        if kind == "unlock":
            if not re.search(r"解锁|确认|是|好|可以|继续|y|yes", answer, re.I) or re.search(r"取消|不", answer):
                return Outcome("READY", "已取消，没有改动。", self.drawing_id, usage=self.usage())
            if state.get("layer"):
                await self.be.call("set_layer_state", {"layer_name": state["layer"], "is_locked": False}, self.drawing_id)
                self.tool_calls += 1
            return await self._execute(plan, norm_text, prompt, results)
        if kind == "ask_resume":
            ctx = await self.be.context(self.drawing_id) if self.drawing_id else {}
            prior = [{"step": k, "summary": v.get("summary")} for k, v in results.items()]
            new = await self.planner.plan(norm_text + "；" + norm.text, prompt + "；" + answer, ctx, [], prior, f"上一次缺少参数：{state['question']['question']}；用户的回答：{answer}")
            return await self._after_plan(new, norm_text + "；" + norm.text, prompt + "；" + answer, [], ctx)
        return Outcome("FAILED", f"不认识的续接状态 {kind}", self.drawing_id, usage=self.usage())

    @staticmethod
    def _vessel_defines(plan: Plan) -> list[dict]:
        """计划里所有 vessel_define 的 args（含被放进 cad_batch_execute 的）。"""
        out: list[dict] = []
        for st in plan.workflow_steps:
            p = st.payload
            if p.get("tool") == "vessel_define":
                out.append(p.setdefault("args", {}))
            elif p.get("tool") == "cad_batch_execute":
                out += [s.setdefault("args", {}) for s in (p.get("args") or {}).get("steps", []) if isinstance(s, dict) and s.get("tool") == "vessel_define"]
        return out

    def _inject_turn(self, plan: Plan) -> None:
        """vessel_define 的 turn_id 由引擎注入（模型不写；Planner 解析时已丢掉模型写的）：用户值的来源记为 user:<turn_id>。"""
        for args in self._vessel_defines(plan):
            args.setdefault("turn_id", self.turn_id)

    async def _recheck_vessel(self, plan: Plan, text: str) -> None:
        defines = self._vessel_defines(plan)
        if not defines or not self.drawing_id:
            return
        try:
            r = await self.be.call("vessel_query", {"what": "summary"}, self.drawing_id)
        except Exception:
            return
        if not r.get("ok"):
            return
        svc = getattr(self.be, "svc", None)
        if svc is None:
            return
        try:
            d = svc.store.load(self.drawing_id)
        except Exception:
            return
        if d.vessel is not None:
            bad: list[str] = []
            for turn in dict.fromkeys(a.get("turn_id") or self.turn_id for a in defines):        # 脚本计划自带 turn_id 时按它查
                bad += [b for b in invented_values(d.vessel, turn, text) if b not in bad]
            if bad:
                self.warnings.append("E_INVENTED_VALUE：这些值标为用户给的，但原话里没有：" + "、".join(bad[:5]))

    @staticmethod
    def _set_confirmed(payload: dict) -> None:
        args = payload.get("args", {})
        if payload.get("tool") == "cad_batch_execute":
            for sub in args.get("steps", []):
                if sub.get("tool") in ("erase_entities", "explode_compound_entities", "vessel_define"):
                    sub.setdefault("args", {})["confirmed"] = True
        else:
            args["confirmed"] = True
            payload["args"] = args

    @staticmethod
    def _parse_pick(answer: str, candidates: list[str], options: list[str]) -> list[str]:
        cand = [c.upper() for c in candidates]
        if re.search(r"全部|都|两个都|所有", answer) and cand:
            return list(cand)
        ids = [(m.group(1) + m.group(2)).upper() for m in ID_RX.finditer(answer)]
        ids = [i for i in ids if i in cand] or ([i for i in ids] if not cand else [])
        if ids:
            return list(dict.fromkeys(ids))
        m = re.fullmatch(r"\s*(?:第)?(\d+)\s*(?:个|项|条)?\s*", answer)
        if m and 1 <= int(m.group(1)) <= len(options):
            opt = options[int(m.group(1)) - 1]
            if "全部" in opt:
                return list(cand)
            mm = ID_RX.search(opt)
            if mm:
                return [(mm.group(1) + mm.group(2)).upper()]
        for opt in options:
            if answer.strip() and answer.strip() in opt:
                mm = ID_RX.search(opt)
                if mm:
                    return [(mm.group(1) + mm.group(2)).upper()]
        return []
