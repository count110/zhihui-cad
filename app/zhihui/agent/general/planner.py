"""Planner：口述 → 计划 JSON。

· 产品形态只有 real：按意图类别取工具子集，写系统提示词，调用大模型；JSON 不合格最多修一次；**模型不可用（没填 API Key、接口失败、两次都给不出合格计划）就明确失败，不会悄悄退回规则 Planner**；
· mock：规则 Planner（mock_planner.py）——只给自动化测试和开发用（ZHIHUI_LLM=mock），不是产品形态；
· 拿不准意图类别时，第一次用默认子集，模型在计划里给出 intent_categories 后，用对应子集重新请求一次。
模型调用都在 zhihui/agent/ 下完成（MCP 子进程拿不到 *_API_KEY）。
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from zhihui.tools.general.registry import CATEGORIES, tools_of

from . import prompts
from zhihui.agent.llm_required import NO_KEY_MESSAGE, LLMUnavailable
from .mock_planner import plan_offline
from .plan import Plan, PlanError, parse_plan

DEFAULT_CATEGORIES = ["environment_setup", "2d_drafting", "2d_editing", "annotation_dimension"]


def _json_fmt(provider: str, on: bool) -> dict:
    """要求接口只返回合法 JSON（DeepSeek / OpenAI 兼容接口支持 response_format=json_object；提示词里已经写了“只输出 JSON”）。GLM 不一定支持，不加。"""
    return {"response_format": {"type": "json_object"}} if on and provider in ("deepseek", "openai", "qwen") else {}


async def chat_text(system: str, user: str, usage: dict, role: str = "planner", max_tokens: int = 4000, timeout: float = 60, strict: bool = False, json_mode: bool = False) -> str | None:
    """调用一次大模型，返回文本。strict=True（Planner 用）：没有密钥或接口失败时抛 LLMUnavailable；strict=False（识别兜底、修参）：失败返回 None，由规则结果继续。"""
    from zhihui.agent.llm import PROVIDERS, FailoverLLM, OpenAICompatLLM

    provider = os.environ.get("ZHIHUI_LLM_PRIMARY", "deepseek")
    if provider not in PROVIDERS or not os.environ.get(PROVIDERS[provider]["key_env"]):
        if strict:
            raise LLMUnavailable(NO_KEY_MESSAGE, "no_key")
        return None
    primary = backup = None
    try:
        primary = OpenAICompatLLM(provider, timeout=timeout)
        primary.generation = {"temperature": 0.1, "max_tokens": max_tokens, **_json_fmt(provider, json_mode)}
        bp = os.environ.get("ZHIHUI_LLM_BACKUP", "qwen")
        if bp in PROVIDERS and bp != provider and os.environ.get(PROVIDERS[bp]["key_env"]):
            backup = OpenAICompatLLM(bp, timeout=timeout)
            backup.generation = {"temperature": 0.1, "max_tokens": max_tokens, **_json_fmt(bp, json_mode)}
        llm = FailoverLLM(primary, backup)
        t0 = time.monotonic()
        turn = await llm.chat([{"role": "system", "content": system}, {"role": "user", "content": user}], [])
        usage["prompt_tokens"] = usage.get("prompt_tokens", 0) + turn.prompt_tokens
        usage["completion_tokens"] = usage.get("completion_tokens", 0) + turn.completion_tokens
        usage.setdefault("models", set()).add(turn.model)
        usage.setdefault("role_attempts", []).append({"role": role, "model": turn.model, "seconds": round(time.monotonic() - t0, 2)})
        return turn.content
    except Exception as ex:
        if strict:
            raise LLMUnavailable(f"调用大模型失败（{type(ex).__name__}: {str(ex)[:160]}）。请检查网络、API Key、模型名和接口地址（⚙ 设置里可以「测试连接」）。", "api_error") from ex
        return None
    finally:
        for c in (primary, backup):
            if c is not None:
                try:
                    await c.client.close()
                except Exception:
                    pass


def allowed_tools(categories: list[str] | None) -> set[str]:
    cats = [c for c in (categories or DEFAULT_CATEGORIES) if c in CATEGORIES]
    return {t.name for t in tools_of(cats, include_system=True)}


class Planner:
    def __init__(self, mock: bool, usage: dict | None = None):
        self.mock = mock
        self.usage = usage if usage is not None else {"prompt_tokens": 0, "completion_tokens": 0, "models": set()}
        self.source = "rules"

    async def plan(self, normalized: str, original: str, context: dict, categories: list[str], prior_summary: list[dict] | None = None, feedback: str | None = None) -> Plan:
        if self.mock:
            self.source = "rules"
            return plan_offline(normalized, context)
        cats = list(categories) or list(DEFAULT_CATEGORIES)
        plan = await self._ask_llm(normalized, original, context, cats, prior_summary, feedback)
        if plan is None:
            raise LLMUnavailable("大模型连续 3 次都没有给出格式合格的计划。请换个说法或把需求拆小一点再试；也可以直接输入 AutoCAD 命令（如 L 0,0 @100,0）。", "bad_plan")
        declared = [c for c in plan.intent_categories if c in CATEGORIES]
        if not categories and declared and set(declared) - set(cats):                                   # 拿不准类别：按模型给的类别换子集重新请求一次
            again = await self._ask_llm(normalized, original, context, declared, prior_summary, feedback)
            if again is not None:
                plan = again
        elif categories and set(declared) - set(cats) - {"perception"}:                                  # 关键词路由漏了类别：模型自己说还需要别的类别，把它们并进子集再请求一次（只放宽，不缩小）
            again = await self._ask_llm(normalized, original, context, cats + [c for c in declared if c not in cats], prior_summary, feedback)
            if again is not None:
                plan = again
        return plan

    async def _ask_llm(self, normalized: str, original: str, context: dict, cats: list[str], prior: list[dict] | None, feedback: str | None) -> Plan | None:
        system = prompts.build_planner_prompt(context, cats)
        user = {"原话": original, "规范化后": normalized}
        if prior:
            user["已执行的步骤结果"] = prior
        if feedback:
            user["上一次的问题（请修正）"] = feedback
        text = await chat_text(system, json.dumps(user, ensure_ascii=False), self.usage, "planner", 4000, 90, strict=True, json_mode=True)
        self.source = "llm"
        allowed = allowed_tools(cats)
        widened = False
        for attempt in range(3):                                   # 第一次 + 最多两次“带着错误重来”
            try:
                return parse_plan(text, allowed)
            except PlanError as ex:
                if attempt == 2:
                    return None
                extra: dict = {}
                if not widened and any(("没有函数" in i or "工具子集" in i) for i in ex.issues):
                    widened = True                                  # 模型要的函数不在按关键词选出的子集里：换成全部函数的目录再让它改
                    cats = [c for c in CATEGORIES if c != "perception"]
                    system = prompts.build_planner_prompt(context, cats)
                    allowed = allowed_tools(cats)
                    extra = {"提示": "函数名必须从系统提示词的函数目录里原样复制，不要自己编名字；目录已经换成全部函数。"}
                text = await chat_text(system, json.dumps({**user, **extra, "上一次的输出不合格": str(ex)[:600], "上一次的输出": text[:1500]}, ensure_ascii=False), self.usage, "planner", 4000, 90, strict=True, json_mode=True)
        return None

    # ---- 供 Recognizer / Executor 用的模型兜底 ----
    async def recognizer_calls(self, query: str, context: dict) -> dict | None:
        if self.mock:
            return None
        text = await chat_text(prompts.build_recognizer_prompt(context), json.dumps({"指代": query}, ensure_ascii=False), self.usage, "recognizer", 1200, 45, json_mode=True)
        if not text:
            return None
        try:
            from .plan import strip_fence
            d = json.loads(strip_fence(text))
            return d if isinstance(d, dict) and isinstance(d.get("calls"), list) else None
        except ValueError:
            return None

    async def repair_args(self, tool: str, schema: dict, args: dict, error: dict) -> dict | None:
        if self.mock:
            return None
        text = await chat_text(prompts.build_repair_prompt(schema, args, error), json.dumps({"tool": tool}, ensure_ascii=False), self.usage, "repair", 800, 30, json_mode=True)
        if not text:
            return None
        try:
            from .plan import strip_fence
            d = json.loads(strip_fence(text))
        except ValueError:
            return None
        if not isinstance(d, dict) or d.get("give_up") or not isinstance(d.get("args"), dict):
            return None
        return d["args"]
