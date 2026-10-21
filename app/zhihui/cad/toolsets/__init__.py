"""工程制图专业工具集（函数 44 invoke_engineering_toolset 调用）：机械、建筑；化工 / 给水切换到流程图模式，不在这里重写。"""
from __future__ import annotations

from . import architectural, mechanical

DOMAIN_TOOLS: dict[str, dict] = {
    "mechanical": {**mechanical.TOOLS, "bom_balloon_and_table": architectural.TOOLS["bom_balloon_and_table"]},
    "architectural": {**architectural.TOOLS},
}
ALL_NAMES = sorted({n for d in DOMAIN_TOOLS.values() for n in d})
PFD_DOMAINS = ("chem", "water")


def describe() -> dict:
    return {dom: {name: req for name, (_, req) in tools.items()} for dom, tools in DOMAIN_TOOLS.items()}
