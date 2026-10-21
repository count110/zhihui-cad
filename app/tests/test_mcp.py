"""真实 MCP 连接：stdio 子进程拉起 Server，确认 20 个工具都在、能调用。"""

import asyncio
import json
import sys

from mcp import Client, StdioServerParameters

from zhihui.mcp_server.server import TOOL_NAMES


def test_stdio_server_lists_20_tools_and_calls_one(data_dir):
    async def main():
        params = StdioServerParameters(command=sys.executable, args=["-m", "zhihui.mcp_server"], env={"ZHIHUI_DATA_DIR": str(data_dir)})
        async with Client(params) as c:
            names = sorted(t.name for t in (await c.list_tools()).tools)
            r = await c.call_tool("create_drawing", {"title": "测试"})
            return names, json.loads(r.content[0].text)

    names, created = asyncio.run(main())
    assert names == sorted(TOOL_NAMES) and len(names) == 20
    assert created["ok"] and created["drawing_id"].startswith("dwg_")
    assert (data_dir / "drawings" / created["drawing_id"] / "current.json").exists()
