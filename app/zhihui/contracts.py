"""导出接口合同：python -m zhihui.contracts

生成到 01_项目文档/接口约定/：
  tools.schema.json   全部 MCP 工具的输入 Schema（直接取自 MCP Server，保证与代码一致）：流程图工具 profile=pfd，通用 CAD 函数 profile=general（带 category）
  cad_ir.schema.json  CAD-IR 数据模型
  symbols.json        图元清单（id、中文名、端口）
改了工具签名或 CAD-IR 之后重新跑一次并提交，前端/后端/测试以这里为准。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "01_项目文档" / "接口约定"


async def _tools() -> list[dict]:
    """流程图工具（profile=pfd，和以前完全一致）+ 通用 CAD 函数（profile=general，附 category）。"""
    from mcp import Client

    from zhihui.mcp_server.server import extended_mcp, general_mcp
    from zhihui.tools.general.registry import REGISTRY

    out: list[dict] = []
    async with Client(extended_mcp) as c:
        out = [{"name": t.name, "description": t.description, "input_schema": t.input_schema, "profile": "pfd"} for t in (await c.list_tools()).tools]
    have = {t["name"] for t in out}
    async with Client(general_mcp) as c:
        out += [{"name": t.name, "description": t.description, "input_schema": t.input_schema, "profile": "general", "category": REGISTRY[t.name].category}
                for t in (await c.list_tools()).tools if t.name not in have and t.name in REGISTRY]
    return out


def main() -> None:
    from zhihui.ir import Drawing
    from zhihui.tools.core import symbol_catalog

    OUT.mkdir(parents=True, exist_ok=True)
    dump = lambda obj: json.dumps(obj, ensure_ascii=False, indent=1)  # noqa: E731
    (OUT / "tools.schema.json").write_text(dump(asyncio.run(_tools())), encoding="utf-8")
    (OUT / "cad_ir.schema.json").write_text(dump(Drawing.model_json_schema()), encoding="utf-8")
    (OUT / "symbols.json").write_text(dump(symbol_catalog()), encoding="utf-8")
    print("已导出到", OUT)


if __name__ == "__main__":
    main()
