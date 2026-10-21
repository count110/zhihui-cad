"""大模型连通性自检：python -m zhihui.agent.check_llm [--provider openai]

依次检查：密钥是否读到 → 网络能否连上 → 账号可用的模型 → 工具调用（function calling）是否正常。
不打印密钥本身。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import time

from zhihui.env import load_env


async def main() -> int:
    load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default=os.environ.get("ZHIHUI_LLM_PRIMARY", "deepseek"))
    args = ap.parse_args()

    from zhihui.agent.llm import PROVIDERS, OpenAICompatLLM

    cfg = PROVIDERS[args.provider]
    print(f"提供方：{args.provider}")
    if not os.environ.get(cfg["key_env"]):
        print(f"✗ 没有读到 {cfg['key_env']}：请在项目根目录的 .env 里填写")
        return 1
    print(f"✓ 已读到 {cfg['key_env']}")
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    print(f"  代理：{proxy or '未设置'}")
    llm = OpenAICompatLLM(args.provider, timeout=30)
    print(f"  接口：{llm.client.base_url}  模型：{llm.model}")

    try:
        models = [m.id async for m in llm.client.models.list()]
        hint = [m for m in models if any(k in m for k in ("gpt", "deepseek", "qwen", "glm"))]
        print(f"✓ 网络连通，账号可用模型 {len(models)} 个，例如：{sorted(hint)[:12]}")
        if llm.model not in models:
            print(f"⚠ 当前模型 {llm.model} 不在列表里，请在 .env 里把 {cfg['model_env']} 改成上面列出的某个型号")
    except Exception as ex:
        print(f"✗ 列模型失败：{type(ex).__name__}: {str(ex)[:200]}")
        print("  常见原因：网络不通（检查 HTTPS_PROXY）、密钥错误、中转服务不支持列模型（可忽略，看下一步）")

    tools = [{
        "type": "function",
        "function": {
            "name": "place_equipment",
            "description": "登记设备",
            "parameters": {"type": "object", "properties": {"tag": {"type": "string"}, "symbol_id": {"type": "string"}}, "required": ["tag", "symbol_id"]},
        },
    }]
    t0 = time.monotonic()
    try:
        turn = await llm.chat([{"role": "user", "content": "请调用工具登记一台离心泵，位号 P-101，symbol_id 用 pump。"}], tools)
    except Exception as ex:
        print(f"✗ 调用失败：{type(ex).__name__}: {str(ex)[:300]}")
        return 1
    dt = time.monotonic() - t0
    if turn.tool_calls:
        tc = turn.tool_calls[0]
        print(f"✓ 工具调用正常（{dt:.1f} s，{turn.prompt_tokens}+{turn.completion_tokens} token）：{tc.name}({tc.arguments})")
        return 0
    print(f"✗ 模型没有调用工具，回复：{turn.content[:200]}")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
