"""化工流程图代号表（文章表 2-6～2-10 转录，未与标准原文核对，verified=false）：JSON 数据 + 查询。"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

DIR = Path(__file__).parent


@lru_cache(maxsize=None)
def table(name: str) -> dict:
    """读一张代号表（material_codes / pipe_material / temp_range / insulation / heat_tracing）。"""
    return json.loads((DIR / f"{name}.json").read_text(encoding="utf-8"))


def lookup(name: str, code: str) -> dict | None:
    """按代号查一项，查不到返回 None。"""
    return table(name).get("codes", {}).get(str(code).strip().upper())


def medium_code(text: str) -> tuple[str, list[str]]:
    """介质名 → 物料代号（文章表 2-6）。返回 (代号, 候选)：唯一对上时代号非空；
    说法有歧义（「循环冷却水」可能是上水 CW 或回水 HW）时代号为空、候选列出来，调用方应 ask_user 或保留中文并提示。"""
    t = str(text or "").strip()
    if not t:
        return "", []
    tab = table("material_codes")
    codes = tab["codes"]
    if t.upper() in codes:
        return t.upper(), [t.upper()]
    exact = [c for c, v in codes.items() if t == v["name"] or t in v.get("aliases", []) or t in v["name"].split("、")]
    if len(exact) == 1:
        return exact[0], exact
    if t in tab.get("ambiguous", {}):
        return "", list(tab["ambiguous"][t])
    # 包含关系：「去冷凝器的工艺气体」→ PG；取最长的名称，长度相同的有几个就算歧义
    hits = []
    for c, v in codes.items():
        for n in [v["name"], *v["name"].split("、"), *v.get("aliases", [])]:
            if len(n) >= 2 and n in t:
                hits.append((len(n), c))
    if not hits:
        return "", []
    best = max(n for n, _ in hits)
    cands = sorted({c for n, c in hits if n == best})
    return (cands[0], cands) if len(cands) == 1 else ("", cands)


def pipe_label(c, size_only: bool = False) -> str:
    """管道标注（文章图 2-7）：管道号-外径×壁厚材料代号/等级代号-温度范围代号隔热隔音代号，如 CW0601-32×3.5A/A-C6。
    温度范围和隔热隔音代号可省略；省略时只在管道尺寸后标材料代号。size_only：同一管段号只是管径不同时只标管径（文章 3.2 节）。"""
    size = f"{c.od}×{c.wall}" if c.od and c.wall else (c.od or "")
    if size_only:
        return size + c.material
    if not c.pipe_no:
        return ""
    s = c.pipe_no
    if size or c.material:
        s += "-" + size + c.material
    if c.grade:
        s += "/" + c.grade
    if c.temp_code or c.insulation_code:
        s += "-" + c.temp_code + c.insulation_code
    return s
