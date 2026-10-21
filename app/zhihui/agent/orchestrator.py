"""Orchestrator：智能体闭环 —— 规划 → 执行 → 校验 → 修复（≤2 轮）→ 导出。

- 大模型通过 MCP 调用白名单工具；白名单外的调用直接拒绝。
- 图纸 revision 变化后由程序校验并缓存结果；修复最多两轮。
- 重复、振荡、无进展或预算熔断后，回滚最优版本并降级交付或询问用户。
- 每个任务最多 40 次工具调用、总时长 180 s。
- 护栏（guards.py）在执行前拦下「猜改图目标」和「编造数值」。
- 每一步都通过 on_event 回调推给后端，前端据此实时展示工具调用日志。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .extended_guards import ExtendedGuard as Guard
from .execution import ExecutionConfig, ExecutionControl, compliant_paper, signature, usable
from .llm import LLM, ToolCall, Turn
from .prompts import ASK_USER_TOOL, SYSTEM_PROMPT, repair_message, user_message

MAX_TOOL_CALLS = 40
MAX_SECONDS = 180
MAX_REPAIR_ROUNDS = 2
WHITELIST = {
    "create_drawing", "place_equipment", "connect_ports", "auto_layout", "add_annotation", "inspect_drawing",
    "find_entities", "move_equipment", "edit_equipment", "add_hatch", "edit_hatch", "add_dimension", "add_detail_view", "add_schedule",
    "repeat_system", "add_instrument", "migrate_valves", "add_stream_table",
    "edit_title_block", "edit_annotation", "delete_annotation",
}

EventFn = Callable[[str, dict], Awaitable[None] | None]


@dataclass
class Outcome:
    status: str  # READY / READY_WITH_WARNINGS / FAILED / WAITING_CONFIRMATION
    message: str = ""
    drawing_id: str | None = None
    question: dict | None = None
    artifacts: list[dict] = field(default_factory=list)
    validation: dict | None = None
    diff: dict | None = None
    usage: dict = field(default_factory=dict)
    messages: list[dict] = field(default_factory=list)  # 用于 WAITING_CONFIRMATION 后续接
    spec: dict | None = None
    report: str = ''
    requirements: list = field(default_factory=list)   # per-requirement verification (multi-agent only)
    metrics: dict = field(default_factory=dict)
    generated_symbols: list = field(default_factory=list)


@asynccontextmanager
async def mcp_client(general: bool = False):
    """按 ZHIHUI_MCP 连接 MCP Server：stdio（默认，子进程）/ inproc（测试用）/ http(s)://...（云端内网）。general=True 连带通用 CAD 函数的那个服务。"""
    from mcp import Client, StdioServerParameters

    target = os.environ.get("ZHIHUI_MCP", "stdio")
    if target == "inproc":
        if general:
            from zhihui.mcp_server.server import general_mcp as mcp
        else:
            from zhihui.mcp_server.server import extended_mcp as mcp

        server: Any = mcp
    elif target.startswith("http"):
        import httpx2  # mcp 2.x 自带
        from mcp.client.streamable_http import streamable_http_client

        http = httpx2.AsyncClient(headers={"X-Zhihui-Token": os.environ.get("ZHIHUI_MCP_TOKEN", "")}, timeout=60)
        server = streamable_http_client(target, http_client=http)
    else:
        # MCP Server 用不到模型密钥，不传给子进程
        env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")}
        env['ZHIHUI_MCP_EXTENDED'] = '1'
        if general:
            env['ZHIHUI_MCP_GENERAL'] = '1'
        server = StdioServerParameters(command=sys.executable, args=["-m", "zhihui.mcp_server"], env=env)
    async with Client(server) as client:
        yield client


def _openai_tools(mcp_tools) -> list[dict]:
    out = []
    for t in mcp_tools:
        if t.name in WHITELIST:
            # Recovery fields are for program use only, including over remote MCP.
            schema = json.loads(json.dumps(t.input_schema))
            for key in ("restore_revision", "include_state"):
                schema.get("properties", {}).pop(key, None)
            out.append({"type": "function", "function": {"name": t.name, "description": t.description or "", "parameters": schema}})
    return out + [ASK_USER_TOOL]


def _short(result: dict) -> str:
    """给前端日志的一句话摘要。"""
    if not result.get("ok"):
        return f"{result.get('error_code')}: {result.get('message', '')}"
    for k in ("equipment", "connections", "candidates", "files"):
        if k in result and isinstance(result[k], list):
            return f"{k} × {len(result[k])}"
    if "passed" in result:
        return "通过" if result["passed"] else f"未通过：{len(result.get('errors', []))} 个错误"
    if "new_position" in result:
        return f"{result['equipment_ref']} → {result['new_position']}，重连 {len(result.get('rerouted', []))} 条"
    if "positions" in result:
        return f"{result['mode']} 布局 {len(result['positions'])} 台，布线 {result['routed']} 条"
    return "ok"


class Orchestrator:
    def __init__(self, llm: LLM, on_event: EventFn | None = None, config: ExecutionConfig | None = None):
        self.llm = llm
        self.on_event = on_event
        self.tool_calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.models: set[str] = set()
        self.t0 = time.monotonic()
        self.guard = Guard()
        self.control = ExecutionControl(config or ExecutionConfig(max_calls=MAX_TOOL_CALLS, max_seconds=MAX_SECONDS))
        self.warnings: list[str] = []
        self._layout_attempted: set[str] = set()

    async def emit(self, kind: str, payload: dict) -> None:
        payload = {"agent": "single", "stage": self.control.stage,
                   "budget_remaining": max(0, self.control.config.max_calls-self.control.calls), **payload}
        if self.on_event:
            r = self.on_event(kind, payload)
            if hasattr(r, "__await__"):
                await r

    def usage(self) -> dict:
        return {
            "tool_calls": self.tool_calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "seconds": round(time.monotonic() - self.t0, 1),
            "models": sorted(self.models),
            **self.control.metrics(),
        }

    async def run(self, prompt: str, file_ref: str | None = None, resume: list[dict] | None = None, answer: str | None = None) -> Outcome:
        async with mcp_client() as client:
            catalog = (await client.list_tools()).tools
            self.control.available_tools = {t.name for t in catalog}
            tools = _openai_tools(catalog)
            if resume:
                messages = list(resume)
                # 每个 tool_call 都要有结果：ask_user 的结果就是用户回答，同轮其他调用标记为未执行
                for tc in messages[-1].get("tool_calls", []):
                    if tc["function"]["name"] == "ask_user":
                        content = {"user_answer": answer or ""}
                    else:
                        content = {"ok": False, "error_code": "E_SKIPPED", "message": "等待用户回答期间未执行，如仍需要请重新调用"}
                    messages.append({"role": "tool", "tool_call_id": tc["id"], "content": json.dumps(content, ensure_ascii=False)})
            else:
                messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user_message(prompt, file_ref)}]
            return await self._loop(client, tools, messages)

    async def _call(self, client, name: str, args: dict) -> dict:
        self.control.consume()
        self.tool_calls = self.control.calls
        t = time.monotonic()
        try:
            res = await asyncio.wait_for(client.call_tool(name, args), timeout=self.control.remaining_seconds)
        except Exception as ex:
            data = {"ok": False, "error_code": "E_MCP_TRANSPORT", "message": f"{type(ex).__name__}: {ex}"}
            await self.emit("tool_call", {"name": name, "arguments": args, "ok": False, "summary": _short(data), "result": data, "ms": round((time.monotonic()-t)*1000)})
            return data
        text = "".join(getattr(c, "text", "") for c in res.content)
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = {"ok": not res.is_error, "error_code": "E_MCP" if res.is_error else None, "message": text[:300]}
        if not isinstance(data, dict):
            data = {"ok": False, "error_code": "E_MCP", "message": "工具未返回 JSON 对象"}
        await self.emit("tool_call", {"name": name, "arguments": args, "ok": bool(data.get("ok")), "summary": _short(data), "result": data, "ms": round((time.monotonic() - t) * 1000)})
        return data

    async def _program_call(self, client, name: str, args: dict) -> dict:
        data = await self._call(client, name, args)
        did = data.get("drawing_id") or args.get("drawing_id")
        if did and data.get("revision") is not None:
            self.control.revisions[did] = data["revision"]
        self.control.attempt_log.append({"tool": name, "signature": signature(name, args), "arguments": args,
            "ok": bool(data.get("ok")), "error_code": data.get("error_code"), "executed": True,
            "source": "scheduler", "revision": data.get("revision"), "stage": self.control.stage})
        return data

    async def _validate(self, client, did: str, *, compare: bool = False) -> tuple[dict, str | None]:
        cached = self.control.current_validation(did)
        if cached is not None:
            return cached, None
        v = await self._program_call(client, "validate_drawing", {"drawing_id": did})
        decision = None
        if v.get("ok"):
            decision = self.control.record_validation(did, v["revision"], v, compare=compare)
        return v, decision

    async def _rollback(self, client, did: str) -> None:
        if not self.control.needs_rollback(did):
            return
        best = self.control.best[did]
        r = await self._program_call(client, "inspect_drawing", {"drawing_id": did, "restore_revision": best["revision"]})
        if r.get("ok"):
            self.control.rollbacks += 1
            old = self.control.validations[did, best["revision"]]
            self.control.record_validation(did, r["revision"], {**old, "revision": r["revision"]})
            await self.emit("execution_control", {"action": "rollback", "drawing_id": did, "from_revision": best["revision"], "revision": r["revision"], "score": best["score"]})

    async def _layout_recovery(self, client, did: str, messages: list[dict], reason: str) -> bool:
        if did in self._layout_attempted or self.control.budget_reason():
            return False
        self._layout_attempted.add(did)
        await self.emit("execution_control", {"action": "layout_fixer", "reason": reason})
        snap = await self._program_call(client, "inspect_drawing", {"drawing_id": did, "include_state": True})
        if not snap.get("ok"):
            return False
        from zhihui.ir import Drawing
        d = Drawing.model_validate(snap["state"])
        args = {"drawing_id": did, "spacing": "compact", "mode": "incremental" if d.base_revision else "full"}
        if not d.base_revision:
            paper = compliant_paper(d)
            if paper is None:
                return False
            args["paper_size"] = paper
        blocked = self.guard.check("auto_layout", args, messages)
        if blocked:
            return False
        r = await self._program_call(client, "auto_layout", args)
        if not r.get("ok"):
            return False
        v, _ = await self._validate(client, did)
        return bool(v.get("passed"))

    async def _ask(self, message: str, did: str | None, messages: list[dict], options: list[str] | None = None) -> Outcome:
        options = options or ["提供明确对象及需要修改的内容", "保留当前图纸，暂不继续修改"]
        # Close any unfinished tool batch before inserting the program's question;
        # this keeps a resumed OpenAI-compatible conversation well formed.
        last = next((i for i in range(len(messages)-1, -1, -1) if messages[i].get("role") == "assistant"), None)
        if last is not None:
            answered = {m.get("tool_call_id") for m in messages[last+1:] if m.get("role") == "tool"}
            for tc in messages[last].get("tool_calls", []):
                if tc["id"] not in answered:
                    messages.append({"role": "tool", "tool_call_id": tc["id"], "content": json.dumps({"ok": False, "error_code": "E_SKIPPED", "message": "程序暂停执行，等待用户确认"}, ensure_ascii=False)})
        q = {"question": message, "options": options[:4]}
        messages.append(Turn(tool_calls=[ToolCall(f"control_ask_{len(messages)}", "ask_user", q)]).as_message())
        await self.emit("status", {"status": "WAITING_CONFIRMATION", **q})
        return Outcome("WAITING_CONFIRMATION", message, did, question=q, usage=self.usage(), messages=messages)

    async def _finish(self, client, did: str | None, message: str, messages: list[dict], *, warning: bool = False) -> Outcome:
        if did is None:
            return Outcome("FAILED", message or "未生成基础可用图纸", usage=self.usage())
        warning = warning or any(i.status != "resolved" and i.code != "E_SCHEDULER_ONLY" for i in self.control.issues.values())
        if warning and message not in self.warnings:
            self.warnings.append(message)
        if warning:
            for issue in self.control.issues.values():
                if issue.status != "resolved":
                    issue.status = "unresolved"
                    detail = f"[{issue.code}] {issue.message}；目标：{issue.target_id or issue.id}；已执行 {issue.attempts} 次，拦截 {issue.blocked} 次。"
                    if detail not in self.warnings:
                        self.warnings.append(detail)
        try:
            await self._rollback(client, did)
            v, _ = await self._validate(client, did)
            if not usable(v):
                return Outcome("FAILED", message or "未生成基础可用图纸", did, validation=v, usage=self.usage())
            warned = bool(self.warnings or not v.get("passed"))
            formats = ["dxf"] if self.control.remaining_seconds < 5 else ["dxf", "png", "pdf"]
            ex = await self._program_call(client, "export_artifacts", {"drawing_id": did, "formats": formats,
                "allow_warnings": warned, "delivery_notes": self.warnings})
            if not ex.get("ok"):
                return await self._ask("当前图纸已保留，但导出未完成："+ex.get("message", "请确认后重试"), did, messages,
                    ["保留图纸并重试导出", "提供补充要求后继续"])
            status = "READY_WITH_WARNINGS" if warned else "READY"
            await self.emit("status", {"status": status})
            return Outcome(status, message, did, artifacts=ex.get("files", []), validation={**v, "standards": ex.get("standards_check")},
                diff=ex.get("diff"), usage=self.usage())
        except TimeoutError:
            return await self._ask("执行预算已耗尽，当前工作版本已保留，请选择后续操作。", did, messages,
                ["保留工作版本，继续校验并导出", "补充要求后继续处理"])

    async def _loop(self, client, tools: list[dict], messages: list[dict]) -> Outcome:
        drawing_id = None
        for m in messages:  # 续接时从历史里找回 drawing_id
            if m.get("role") == "tool" and '"drawing_id"' in m.get("content", ""):
                try:
                    drawing_id = json.loads(m["content"]).get("drawing_id") or drawing_id
                except json.JSONDecodeError:
                    continue
        repair_round = 0
        await self.emit("status", {"status": "PLANNING"})
        from .guards import TAG_RE, user_text
        said = user_text(messages)
        if drawing_id is None and not TAG_RE.search(said) and re.search(r"菜市场|农贸市场|小型餐饮", said) and not any(m.get("role") == "tool" for m in messages):
            return await self._ask("该污水工艺尚无已确认的设备清单及知识来源，请先确认工艺方案。", None, messages,
                ["提供已确认的设备清单与连接", "提供参考工艺图后再绘制", "暂不生成图纸"])
        first = True
        while True:
            if reason := self.control.budget_reason():
                return await self._finish(client, drawing_id, reason, messages, warning=True)
            try:
                turn: Turn = await asyncio.wait_for(self.llm.chat(messages, tools),
                    timeout=max(.01, self.control.remaining_seconds-self.control.config.finish_seconds))
            except Exception as ex:  # 主备模型都失败
                return await self._finish(client, drawing_id, f"大模型调用未完成：{type(ex).__name__}: {ex}", messages, warning=True)
            self.prompt_tokens += turn.prompt_tokens
            self.completion_tokens += turn.completion_tokens
            if turn.model:
                self.models.add(turn.model)
            messages.append(turn.as_message())
            if turn.content.strip():
                await self.emit("assistant", {"text": turn.content.strip()})
            if first and turn.tool_calls:
                await self.emit("status", {"status": "RUNNING"})
                first = False

            if turn.tool_calls:
                for tc in turn.tool_calls:
                    if tc.name == "ask_user":
                        q = {"question": tc.arguments.get("question", ""), "options": tc.arguments.get("options", [])}
                        if len(q["options"]) < 2:
                            q["options"] = ["补充问题中要求的具体信息", "暂停当前任务，保留已有图纸"]
                        q["options"] = q["options"][:4]
                        await self.emit("status", {"status": "WAITING_CONFIRMATION", **q})
                        return Outcome("WAITING_CONFIRMATION", q["question"], drawing_id, question=q, usage=self.usage(), messages=messages)
                for tc in turn.tool_calls:
                    if reason := self.control.budget_reason():
                        return await self._finish(client, drawing_id, reason, messages, warning=True)
                    self.guard.normalize(tc.name, tc.arguments, messages)
                    blocked = self.control.before(tc.name, tc.arguments)
                    executed = False
                    t = time.monotonic()
                    if blocked is not None:
                        data = blocked
                    elif tc.name not in WHITELIST:
                        data = {"ok": False, "error_code": "E_NOT_ALLOWED", "message": f"工具 {tc.name} 不在白名单", "hint": "只能使用提供的工具"}
                    elif "__invalid_json__" in tc.arguments:
                        data = {"ok": False, "error_code": "E_BAD_JSON", "message": "参数不是合法 JSON", "hint": "重新生成合法的 JSON 参数"}
                    elif (blocked := self.guard.check(tc.name, tc.arguments, messages)) is not None:
                        data = blocked
                    else:
                        executed = True
                        data = await self._call(client, tc.name, tc.arguments)
                        self.guard.observe(tc.name, tc.arguments, data, messages)
                    if not executed:
                        await self.emit("execution_control", {"action": "blocked", "name": tc.name, "arguments": tc.arguments, "result": data})
                    if data.get("ok") and data.get("drawing_id"):
                        drawing_id = data["drawing_id"]
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(data, ensure_ascii=False, separators=(",", ":"))})
                    decision = self.control.observe(tc.name, tc.arguments, data, round((time.monotonic()-t)*1000), executed=executed)
                    code = data.get("error_code", "")
                    if code in {"E_TARGET_UNCONFIRMED", "E_UNSOURCED_VALUE", "E_PLACEHOLDER_VALUE", "E_RELAYOUT_UNCONFIRMED", "E_FULL_RELAYOUT", "E_CAPABILITY_MISSING", "E_SELF_LOOP"}:
                        return await self._ask(data["message"], drawing_id, messages)
                    if code == "E_NOT_ALLOWED" and tc.name in {"edit_title_block", "delete_annotation", "edit_annotation"}:
                        return await self._ask(f"当前工具目录尚未提供 {tc.name}。请确认是否保留图纸或补充修改目标。", drawing_id, messages)
                    if drawing_id and data.get("ok") and data.get("revision") is not None:
                        compare = tc.name in {"move_equipment", "edit_equipment", "edit_annotation", "delete_annotation", "edit_hatch"}
                        v, geometry = await self._validate(client, drawing_id, compare=compare)
                        if tc.name in {"auto_layout", "inspect_drawing"} and usable(v):
                            self.control.previous_score[drawing_id] = self.control.best.get(drawing_id, {}).get("score", 0)
                        if geometry == "rollback":
                            await self._rollback(client, drawing_id)
                            target = str(tc.arguments.get("equipment_ref") or tc.arguments.get("target") or "")
                            self.control.frozen.add((drawing_id, target))
                            self.control.oscillations += 1
                            self.warnings.append("修改后问题加权分升高，已恢复到历史最优版本。")
                            decision = "oscillation"
                        elif geometry == "layout_fixer":
                            self.control.frozen.add((drawing_id, str(tc.arguments.get("equipment_ref") or "")))
                            decision = geometry
                    if code in {"E_LAYOUT_OVERFLOW", "E_LAYOUT_TOO_LONG", "E_ROUTE_FAILED", "E_OVERLAP"} or (code == "E_BAD_ARG" and "图幅" in data.get("message", "")):
                        if drawing_id and await self._layout_recovery(client, drawing_id, messages, code):
                            self.warnings.append("执行过确定性布局恢复，请复核布局结果。")
                        else:
                            return await self._ask("确定性布局仍需补充条件，请确认是否更换图幅或拆分图纸。", drawing_id, messages,
                                ["同意程序选择更大合规图幅", "提供分张方案", "保留当前可用图纸"])
                    if decision:
                        await self.emit("execution_control", {"action": decision, "name": tc.name})
                        if drawing_id and decision in {"oscillation", "layout_fixer"}:
                            await self._layout_recovery(client, drawing_id, messages, decision)
                        return await self._finish(client, drawing_id, "执行控制已终止无进展或重复修复，未完成事项详见交付说明。", messages, warning=True)
                continue

            # —— 模型认为完成了：程序强制校验 ——
            if drawing_id is None:
                # 没有产出图纸：拒绝或纯文字回复
                status = "WAITING_CONFIRMATION" if turn.content.strip().endswith(("?", "？")) else "FAILED"
                q = {"question": turn.content, "options": []} if status == "WAITING_CONFIRMATION" else None
                return Outcome(status, turn.content or "未生成图纸", None, question=q, usage=self.usage(), messages=messages)
            await self.emit("status", {"status": "VALIDATING"})
            v, _ = await self._validate(client, drawing_id)
            if not v.get("passed"):
                repair_round += 1
                if repair_round > MAX_REPAIR_ROUNDS:
                    return await self._finish(client, drawing_id, "修复 2 轮后仍有问题：" + "；".join(e["message"] for e in v.get("errors", [])[:3]), messages, warning=True)
                self.control.stage = "repairing"
                await self.emit("status", {"status": "REPAIRING", "round": repair_round, "errors": v.get("errors", [])[:8]})
                messages.append({"role": "user", "content": repair_message(v.get("errors", []), repair_round)})
                continue
            return await self._finish(client, drawing_id, turn.content, messages)
