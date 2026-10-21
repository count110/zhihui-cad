"""大模型接入：国内模型统一走 OpenAI 兼容接口，一主一备自动切换；另有离线脚本模型用于测试和演示。

环境变量：
  ZHIHUI_LLM            real（默认）/ mock（离线演示，不需要 API Key）
  ZHIHUI_LLM_PRIMARY    主模型提供方：deepseek / qwen / glm / openai（默认 deepseek）
  ZHIHUI_LLM_BACKUP     备用提供方（默认 qwen）
  DEEPSEEK_API_KEY / DASHSCOPE_API_KEY / ZHIPUAI_API_KEY / OPENAI_API_KEY
  DEEPSEEK_MODEL / QWEN_MODEL / GLM_MODEL / OPENAI_MODEL   覆盖默认模型名（以各平台控制台当前可用的型号为准）
  GLM_THINKING          enabled / disabled（默认 disabled，关闭 GLM 深度思考以提速）
  <提供方>_BASE_URL     覆盖接口地址（如 OPENAI_BASE_URL 指向中转服务）
openai 仅用于开发期测试；比赛演示按方案书使用国内模型。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

PROVIDERS: dict[str, dict[str, str]] = {
    "deepseek": {"base_url": "https://api.deepseek.com", "key_env": "DEEPSEEK_API_KEY", "model_env": "DEEPSEEK_MODEL", "model": "deepseek-chat"},
    "qwen": {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "key_env": "DASHSCOPE_API_KEY", "model_env": "QWEN_MODEL", "model": "qwen-plus"},
    "glm": {"base_url": "https://open.bigmodel.cn/api/paas/v4/", "key_env": "ZHIPUAI_API_KEY", "model_env": "GLM_MODEL", "model": "glm-4.5-air"},
    "openai": {"base_url": "https://api.openai.com/v1", "key_env": "OPENAI_API_KEY", "model_env": "OPENAI_MODEL", "model": "gpt-4.1"},
}
# OpenAI 的推理类模型只接受默认 temperature，传了会报错，所以只对国内模型设低温度
LOW_TEMPERATURE = {"deepseek", "qwen", "glm"}


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str = ""


@dataclass
class Turn:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str = ""

    def as_message(self) -> dict:
        msg: dict[str, Any] = {"role": "assistant", "content": self.content or ""}
        if self.tool_calls:
            msg["tool_calls"] = [
                {"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": tc.raw_arguments or json.dumps(tc.arguments, ensure_ascii=False)}}
                for tc in self.tool_calls
            ]
        return msg


class LLM(Protocol):
    async def chat(self, messages: list[dict], tools: list[dict]) -> Turn: ...


class OpenAICompatLLM:
    def __init__(self, provider: str, timeout: float = 90):
        from openai import AsyncOpenAI

        cfg = PROVIDERS[provider]
        key = os.environ.get(cfg["key_env"])
        if not key:
            raise RuntimeError(f"缺少环境变量 {cfg['key_env']}（在项目根目录的 .env 里填写）")
        self.provider = provider
        self.model = os.environ.get(cfg["model_env"]) or cfg["model"]
        base_url = os.environ.get(f"{provider.upper()}_BASE_URL") or cfg["base_url"]
        self.client = AsyncOpenAI(api_key=key, base_url=base_url, timeout=timeout, max_retries=3)  # 429 限流时 SDK 会退避重试
        self.thinking_switch = True

    async def chat(self, messages: list[dict], tools: list[dict]) -> Turn:
        extra: dict[str, Any] = {"temperature": 0.1} if self.provider in LOW_TEMPERATURE else {}
        extra.update(getattr(self,'generation',{}))
        if self.provider == "glm" and self.thinking_switch and os.environ.get("GLM_THINKING", "disabled") == "disabled":
            # 制图任务不需要长链思考，关掉能明显提速（GLM_THINKING=enabled 可打开）
            extra["extra_body"] = {"thinking": {"type": "disabled"}}
        try:
            resp = await self.client.chat.completions.create(model=self.model, messages=messages, tools=tools, **extra)
        except Exception as ex:
            if "extra_body" in extra and "1210" in str(ex):  # 该型号始终思考，不接受关闭参数
                self.thinking_switch = False
                extra.pop("extra_body")
                resp = await self.client.chat.completions.create(model=self.model, messages=messages, tools=tools, **extra)
            else:
                raise
        msg = resp.choices[0].message
        calls = []
        for tc in msg.tool_calls or []:
            raw = tc.function.arguments or "{}"
            try:
                args = json.loads(raw)
            except json.JSONDecodeError:
                args = {"__invalid_json__": raw}
            calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args, raw_arguments=raw))
        usage = resp.usage
        return Turn(
            content=msg.content or "",
            tool_calls=calls,
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
            model=f"{self.provider}:{self.model}",
        )


class FailoverLLM:
    """主模型超时或报错时自动切换到备用模型。"""

    def __init__(self, primary: LLM, backup: LLM | None = None):
        self.primary, self.backup = primary, backup
        self.switched = False

    async def chat(self, messages: list[dict], tools: list[dict]) -> Turn:
        if not self.switched:
            try:
                return await self.primary.chat(messages, tools)
            except Exception:
                if self.backup is None:
                    raise
                self.switched = True
        return await self.backup.chat(messages, tools)


# ---------------------------------------------------------------- 离线脚本模型

class ScriptedLLM:
    """按预先写好的步骤「假装」调用工具，用于单测、评测脚本自检和前端联调。

    steps 中每一步是以下之一：
      [{"name": 工具名, "arguments": {...}}, ...]   一轮里的若干工具调用
      {"ask": "问题", "options": [...]}              调用 ask_user
      {"say": "文字"}                                 直接结束
    参数里的字符串 "$drawing_id" 会替换成最近一次工具结果里的 drawing_id。
    """

    def __init__(self, steps: list[Any]):
        self.steps = list(steps)
        self.i = 0
        self.n = 0

    async def chat(self, messages: list[dict], tools: list[dict]) -> Turn:
        did = None
        for m in messages:
            if m.get("role") == "tool":
                hit = re.search(r'"drawing_id":\s*"(dwg_\w+)"', m.get("content", ""))
                if hit:
                    did = hit.group(1)
        if self.i >= len(self.steps):
            return Turn(content="已完成。", model="scripted")
        step = self.steps[self.i]
        self.i += 1

        def sub(v):
            if v == "$drawing_id":
                return did
            if isinstance(v, dict):
                return {k: sub(x) for k, x in v.items()}
            if isinstance(v, list):
                return [sub(x) for x in v]
            return v

        if isinstance(step, dict) and "say" in step:
            return Turn(content=step["say"], model="scripted")
        if isinstance(step, dict) and "ask" in step:
            step = [{"name": "ask_user", "arguments": {"question": step["ask"], "options": step.get("options", [])}}]
        calls = []
        for c in step:
            self.n += 1
            calls.append(ToolCall(id=f"call_{self.n}", name=c["name"], arguments=sub(c.get("arguments", {}))))
        return Turn(tool_calls=calls, model="scripted")


def plan_to_steps(plan: dict) -> list[Any]:
    """把基准用例里的 plan（设备 + 连接）转成脚本步骤，模拟「一次批量调用」的理想智能体。"""
    create = {"title": plan.get("title", "工艺流程图")}
    for k in ("paper_size", "template_id", "title_block", "notes", "legend_codes", "code_names", "vscale",
              "stage", "section_no", "tag_style", "sheet_no", "sheet_total", "zones", "standard", "binding"):
        if plan.get(k):
            create[k] = plan[k]
    if plan.get("frame") is False:  # 方案流程图可以不画图框
        create["frame"] = False
    after = []  # 化工流程图：布局之后再挂相同系统方框、仪表、物料组成表（都要用到已布好的设备和管线）
    for r in plan.get("repeats", []):
        after.append({"name": "repeat_system", "arguments": {"drawing_id": "$drawing_id", **r}})
    for i in plan.get("instruments", []):
        after.append({"name": "add_instrument", "arguments": {"drawing_id": "$drawing_id", **i}})
    for t in plan.get("stream_tables", []):
        after.append({"name": "add_stream_table", "arguments": {"drawing_id": "$drawing_id", **t}})
    return [
        [{"name": "create_drawing", "arguments": create}],
        [
            {"name": "place_equipment", "arguments": {"drawing_id": "$drawing_id", "items": plan["equipment"]}},
            {"name": "connect_ports", "arguments": {"drawing_id": "$drawing_id", "items": plan["connections"]}},
        ],
        [{"name": "auto_layout", "arguments": {"drawing_id": "$drawing_id"}}],
        *([after] if after else []),
        {"say": f"已生成「{plan.get('title', '工艺流程图')}」。"},
    ]


DEMO_PLAN = {
    "title": "垃圾焚烧烟气处理流程",
    "equipment": [
        {"symbol_id": "pit", "tag": "V-101", "name": "垃圾储坑"},
        {"symbol_id": "grate_incinerator", "tag": "F-101", "name": "焚烧炉"},
        {"symbol_id": "boiler", "tag": "B-101", "name": "余热锅炉"},
        {"symbol_id": "spray_tower", "tag": "T-101", "name": "急冷塔"},
        {"symbol_id": "dust_collector", "tag": "D-101", "name": "布袋除尘器"},
        {"symbol_id": "fan", "tag": "F-201", "name": "引风机"},
        {"symbol_id": "stack", "tag": "S-101", "name": "烟囱"},
    ],
    "connections": [
        {"from_port": "V-101.out", "to_port": "F-101.in", "medium": "垃圾"},
        {"from_port": "F-101.out", "to_port": "B-101.in", "medium": "烟气"},
        {"from_port": "B-101.flue_out", "to_port": "T-101.feed", "medium": "烟气"},
        {"from_port": "T-101.top_out", "to_port": "D-101.in", "medium": "烟气"},
        {"from_port": "D-101.out", "to_port": "F-201.in", "medium": "烟气"},
        {"from_port": "F-201.out", "to_port": "S-101.in", "medium": "净烟气"},
    ],
}


TAG_RE = re.compile(r"(?<![A-Za-z0-9])([A-Z]{1,3}-\d{2,4}[A-Z]?)(?![A-Za-z0-9])")
PREFIX_SYMBOL = {"P": "pump", "V": "tank", "T": "tower", "E": "heat_exchanger", "R": "reactor", "C": "compressor",
                 "D": "dust_collector", "S": "stack", "B": "boiler", "XV": "valve", "K": "compressor"}
GAS_SOURCES = {"furnace", "grate_incinerator", "boiler", "tower", "spray_tower", "packed_tower", "reactor", "fixed_bed_reactor",
               "dust_collector", "esp", "cyclone", "fan", "mixer"}


def heuristic_plan(prompt: str) -> dict | None:
    """离线模式的规则解析：从需求里找出「位号 + 中文名」，按出现顺序串成主流程。

    只是没有大模型时的兜底，理解不了分支、回流和复杂描述；能识别出 ≥2 台设备才返回。
    """
    from zhihui.cad.symbols import load_symbols

    from zhihui.agent.coord.requirements import symbol_for, tag_names

    syms = load_symbols()
    found = tag_names(prompt, syms)  # 设备名取位号前紧邻的中文，去掉「进入/返回/分成」等动作词；仪表位号不当设备
    if len(found) < 2:
        return None
    equipment = [{"symbol_id": symbol_for(tag, name, syms), "tag": tag, "name": name[:20]} for tag, name in found.items()]
    gas = "烟气" in prompt
    connections, gas_started = [], False
    for a, b in zip(equipment, equipment[1:]):
        src = a["tag"]
        if gas and a["symbol_id"] in ("furnace", "boiler"):
            src += ".flue_out"
        gas_started = gas_started or (gas and a["symbol_id"] in GAS_SOURCES)
        connections.append({"from_port": src, "to_port": b["tag"], "medium": "烟气" if gas_started else ""})
    titles = re.findall(r"[“\"「]([^”\"」]{2,40})[”\"」]", prompt)
    named = re.search(r"图纸名称[^“\"「]*[“\"「]([^”\"」]{2,40})", prompt)
    title = (named.group(1) if named else titles[0] if titles else "工艺流程图").replace("（PFD）", "").replace("(PFD)", "")
    paper = re.search(r"(?<![A-Za-z0-9])(A[123])(?![0-9])", prompt)
    return {"title": title, "paper_size": paper.group(1) if paper else "A3", "equipment": equipment, "connections": connections}


def _bench_plan(case_id: str) -> dict:
    from pathlib import Path

    f = Path(__file__).resolve().parents[2] / "06_测试与基准用例" / "cases" / f"{case_id}.json"
    return json.loads(f.read_text(encoding="utf-8"))["plan"]


def demo_steps(prompt: str, file_ref: str | None) -> list[Any]:
    """mock 模式：不调用真实大模型，按简单规则走完整流程，供前端/后端联调和离线演示。"""
    if not file_ref and re.search(r"高程布置图|高程图|净水厂|水厂流程", prompt):
        # 离线规则解析读不懂水厂流程，演示时直接用基准用例 W001 的设计（给水处理 工艺流程及高程布置图）
        steps = plan_to_steps(_bench_plan("W001"))
        steps[-1] = {"say": "（离线演示）未调用大模型，按内置示例 W001 生成「工艺流程及高程布置图」；接入大模型后会按你的描述生成。"}
        return steps
    if not file_ref:
        plan = heuristic_plan(prompt)
        if plan is None:
            return [{"ask": "离线演示模式不调用大模型，只能识别「位号 + 名称」清单（如：V-101 原料罐、P-101 进料泵）。请按这种格式描述设备，或配置大模型 API Key 后再试。"}]
        steps = plan_to_steps(plan)
        steps[0][0]["arguments"]["paper_size"] = plan["paper_size"]
        steps[-1] = {"say": f"（离线规则解析）识别出 {len(plan['equipment'])} 台设备，按出现顺序串联：" + " → ".join(f"{e['tag']} {e['name']}" for e in plan["equipment"])}
        return steps
    m = re.search(r"([A-Za-z]{1,3}-?\d{2,4})\s*(?:向)?([左右上下])(?:移|挪|平移)\s*(\d+(?:\.\d+)?)\s*(?:mm|毫米)?", prompt)
    steps: list[Any] = [[{"name": "inspect_drawing", "arguments": {"file_ref": file_ref}}]]
    if not m:
        steps.append({"ask": "请说明要移动哪台设备（位号）、方向和距离，例如「把 F-201 右移 800 mm」。"})
        return steps
    tag, direction, dist = m.group(1).upper(), m.group(2), float(m.group(3))
    dx, dy = {"右": (dist, 0), "左": (-dist, 0), "上": (0, dist), "下": (0, -dist)}[direction]
    steps.append([{"name": "find_entities", "arguments": {"drawing_id": "$drawing_id", "tag": tag}}])
    steps.append([{"name": "move_equipment", "arguments": {"drawing_id": "$drawing_id", "equipment_ref": tag, "dx": dx, "dy": dy}}])
    steps.append({"say": f"已将 {tag} {direction}移 {dist:g} mm，相关管线已自动重连，其他设备未改动。"})
    return steps


def make_llm(prompt: str = "", file_ref: str | None = None) -> LLM:
    if os.environ.get("ZHIHUI_LLM", "real") == "mock":
        return ScriptedLLM(demo_steps(prompt, file_ref))
    primary = os.environ.get("ZHIHUI_LLM_PRIMARY", "deepseek")
    backup = os.environ.get("ZHIHUI_LLM_BACKUP", "qwen")
    p = OpenAICompatLLM(primary)
    try:
        b = OpenAICompatLLM(backup) if backup and backup != primary else None
    except RuntimeError:
        b = None
    return FailoverLLM(p, b)
