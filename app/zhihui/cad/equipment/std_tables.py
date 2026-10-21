"""标准数据表（任务书 5.4）：zhihui/cad/equipment/data/*.json，每一行都带 source 和 verified。

lookup(table_id, **keys) → {"row", "source", "verified", "table_id", "standard"}；查不到报 STD_SIZE_NOT_FOUND（带覆盖范围），**不插值、不外推**。
行里的键可以是精确值（"dn": 80），也可以是分档（"dn_min"/"dn_gt" 下限、"dn_max" 上限，标准里本来就是按档给的）。
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from zhihui.cad.geom.errors import GeomError

DATA_DIR = Path(__file__).resolve().parent / "data"


@lru_cache(maxsize=None)
def table(table_id: str) -> dict:
    p = DATA_DIR / f"{table_id}.json"
    if not p.exists():
        raise GeomError("STD_TABLE_UNKNOWN", f"没有标准表 {table_id}", f"可用：{', '.join(available())}")
    return json.loads(p.read_text(encoding="utf-8"))


def available() -> list[str]:
    return sorted(p.stem for p in DATA_DIR.glob("*.json") if p.stem != "rules")


@lru_cache(maxsize=None)
def rules() -> dict:
    return json.loads((DATA_DIR / "rules.json").read_text(encoding="utf-8"))


def rule_value(key: str):
    """rules.json 里的经验数值（值本身；来源和是否核对用 rule_meta 取）。"""
    return rules()[key]["value"]


def rule_meta(key: str) -> dict:
    r = rules()[key]
    return {"source": r.get("source", ""), "verified": bool(r.get("verified"))}


def _match(row: dict, key: str, v) -> bool:
    if key in row:
        a = row[key]
        if isinstance(a, (int, float)) and isinstance(v, (int, float)):
            return abs(float(a) - float(v)) < 1e-9
        return str(a) == str(v)
    lo, gt, hi = row.get(f"{key}_min"), row.get(f"{key}_gt"), row.get(f"{key}_max")
    if lo is None and gt is None and hi is None:
        return False
    try:
        x = float(v)
    except (TypeError, ValueError):
        return False
    if lo is not None and x < float(lo) - 1e-9:
        return False
    if gt is not None and x <= float(gt) + 1e-9:
        return False
    if hi is not None and x > float(hi) + 1e-9:
        return False
    return True


def coverage(table_id: str) -> str:
    t = table(table_id)
    return t.get("coverage", "")


def lookup(table_id: str, **keys) -> dict:
    t = table(table_id)
    for row in t["rows"]:
        if all(_match(row, k, v) for k, v in keys.items()):
            return {"row": row, "source": row.get("source", ""), "verified": bool(row.get("verified")), "table_id": table_id, "standard": t.get("standard", "")}
    ks = "，".join(f"{k}={v:g}" if isinstance(v, (int, float)) else f"{k}={v}" for k, v in keys.items())
    raise GeomError("STD_SIZE_NOT_FOUND", f"标准表 {table_id}（{t.get('title', '')}）里没有 {ks} 这一行", f"本表覆盖：{coverage(table_id)}；不插值、不外推，请给出这个规格的尺寸或换一个规格",
                    table_id=table_id, keys=keys, coverage=coverage(table_id))


def rows(table_id: str) -> list[dict]:
    return list(table(table_id)["rows"])


def unverified_summary(used: list[str]) -> list[str]:
    """用到的未核对表（去重），给图上技术要求 ⑦ 和回复用。"""
    out = []
    for tid in dict.fromkeys(used):
        try:
            t = table(tid)
        except GeomError:
            continue
        out.append(f"{t.get('standard', tid)} {t.get('title', '')}")
    return out
