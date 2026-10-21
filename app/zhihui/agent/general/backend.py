"""工具后端：Executor 通过它调用函数。McpBackend 走 MCP 协议（生产路径）；LocalBackend 直接调注册表（测试、API 的 ops 接口、离线评测）。"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

from zhihui.tools.general.registry import REGISTRY, run_tool


class Backend:
    async def call(self, tool: str, args: dict, drawing_id: str | None) -> dict:           # pragma: no cover - 接口
        raise NotImplementedError

    async def context(self, drawing_id: str) -> dict:
        r = await self.call("inspect_drawing_context", {"include_tables": ["layers"]}, drawing_id)
        return (r.get("data") or {}).get("context") or {}

    async def session(self, drawing_id: str) -> SimpleNamespace:
        c = await self.context(drawing_id)
        return SimpleNamespace(last_point=tuple(c["last_point"]) if c.get("last_point") else None, selection=list(c.get("selection", [])), recent_entities=list(c.get("recent_entities", [])))


class LocalBackend(Backend):
    def __init__(self, svc):
        self.svc = svc

    SVC_TOOLS = ("validate_drawing", "export_artifacts", "inspect_drawing")

    async def call(self, tool: str, args: dict, drawing_id: str | None) -> dict:
        if tool in self.SVC_TOOLS:                                          # 流程图时代的 ToolService 方法（校验、导出）
            kw = dict(args)
            if drawing_id and "drawing_id" not in kw:
                kw["drawing_id"] = drawing_id
            return await asyncio.to_thread(getattr(self.svc, tool), **kw)
        return await asyncio.to_thread(run_tool, self.svc, drawing_id, tool, args)


class McpBackend(Backend):
    def __init__(self, client):
        self.client = client

    async def call(self, tool: str, args: dict, drawing_id: str | None) -> dict:
        t = REGISTRY.get(tool)
        payload: dict[str, Any] = dict(args)
        if t is None and tool in ("validate_drawing", "export_artifacts", "inspect_drawing") and drawing_id:
            payload.setdefault("drawing_id", drawing_id)
        if t is not None and t.needs_drawing:
            if not drawing_id:
                return {"ok": False, "error_code": "E_DRAWING_NOT_FOUND", "message": "缺少 drawing_id", "hint": "先新建或打开一张图"}
            payload["drawing_id"] = drawing_id
        r = await self.client.call_tool(tool, payload)
        text = r.content[0].text if getattr(r, "content", None) else ""
        if getattr(r, "is_error", False) or getattr(r, "isError", False):
            return {"ok": False, "error_code": "INVALID_ARGS", "message": f"{tool} 参数没通过 MCP 校验：{text[:300]}", "hint": "按函数参数说明改字段名、枚举拼写和类型"}
        try:
            return json.loads(text)
        except ValueError:
            return {"ok": False, "error_code": "BAD_RESPONSE", "message": f"{tool} 返回的不是 JSON：{text[:120]}", "hint": ""}
