"""标题栏：JSON 模板驱动（《CATIA V5-6 R2014 工程图教程》6.5 节「创建与插入标题栏」；GB/T 10609.1-2008）。

内置模板在 zhihui/cad/titleblocks/：gb10609（流程图默认）、water_institute（高程图默认）；标准配置的 title_block 指定默认模板，
create_drawing(title_template=...) 可以换内置模板，也可以直接传用户自己公司的模板（JSON 对象，格式同内置模板）。

模板格式：size [宽, 高]（图纸 mm）；cells 每格 x [左, 右]、y [下, 上]（原点在标题栏左下角），写 text（固定文字）或 field（字段，
取 Drawing.title_block[field]）；字段为空时显示 placeholder（如「（设计单位名称）」），也可给 default；auto 写由程序生成的字段
（图名 = 图纸标题、图号、日期、「不按比例」）；sign_area 是签字区（角色 / 姓名 / 日期），角色可用 title_block.sign_roles 换；
required 是必填字段，S9 自检里没填（显示占位符）的给警告。
"""

from __future__ import annotations

import json
from datetime import date
from functools import lru_cache
from pathlib import Path

from zhihui.ir import Drawing

from .standards import LY, for_drawing
from .table import Table, draw as draw_table, from_template

BUILTIN_DIR = Path(__file__).parent / "titleblocks"


@lru_cache(maxsize=None)
def load(tid: str) -> dict:
    p = BUILTIN_DIR / f"{tid}.json"
    if not p.exists():
        raise KeyError(f"没有标题栏模板 {tid}")
    return json.loads(p.read_text(encoding="utf-8"))


def available() -> list[str]:
    return sorted(p.stem for p in BUILTIN_DIR.glob("*.json"))


def template(d: Drawing) -> dict:
    t = d.title_template
    if isinstance(t, dict) and t:
        return t
    return load(t or for_drawing(d).raw.get("title_block", "gb10609"))


def expand(tpl: dict, tb: dict) -> dict:
    """把签字区展开成单元格：每个角色一行（角色名 / 姓名 / 可选日期）；「证号」的值写 cert_no。"""
    sa = tpl.get("sign_area")
    if not sa:
        return tpl
    roles = tb.get("sign_roles") if isinstance(tb.get("sign_roles"), list) and tb.get("sign_roles") else sa["roles"]
    xs = [float(v) for v in sa["x"]]
    y0, y1 = (float(v) for v in sa["y"])
    rh = (y1 - y0) / len(roles)
    cells = []
    for i, role in enumerate(roles):
        top, bot = round(y1 - i * rh, 4), round(y1 - (i + 1) * rh, 4)
        cells.append({"x": xs[0:2], "y": [bot, top], "text": str(role), "role": "title_block_label"})
        cells.append({"x": xs[1:3], "y": [bot, top], "field": "cert_no" if role == "证号" else f"sign_{role}", "role": "title_block_value"})
        if sa.get("date_column") and len(xs) > 3:
            cells.append({"x": xs[2:4], "y": [bot, top], "field": f"sign_{role}_date", "role": "title_block_value"})
    return {**tpl, "cells": cells + list(tpl["cells"])}


def auto_values(d: Drawing, tpl: dict) -> dict[str, str]:
    from zhihui.ir import STAGE_NAMES

    gen = {
        "stage_name": lambda: STAGE_NAMES.get(d.stage, ""),
        "sheet_no": lambda: str(d.sheet_no),
        "sheet_total": lambda: str(d.sheet_total),
        "title": lambda: d.title,
        "revision_no": lambda: f"{d.id}-R{d.revision}",
        "not_to_scale": lambda: "不按比例",
        "today": lambda: date.today().isoformat(),
    }
    return {k: gen[v]() for k, v in (tpl.get("auto") or {}).items() if v in gen}


def values(d: Drawing, tpl: dict | None = None) -> dict[str, str]:
    tpl = tpl or template(d)
    out = auto_values(d, tpl)
    out.update({k: v for k, v in d.title_block.items() if isinstance(v, str) and v})
    return out


def fields(tpl: dict) -> list[str]:
    return [c["field"] for c in expand(tpl, {})["cells"] if c.get("field")]


def size(d: Drawing) -> tuple[float, float]:
    w, h = template(d)["size"]
    return float(w), float(h)


def build(d: Drawing) -> Table:
    tpl = template(d)
    return from_template(expand(tpl, d.title_block), values(d, tpl))


def missing_required(d: Drawing) -> list[str]:
    """必填字段里没填的（图上显示的是占位符）。"""
    tpl = template(d)
    v = values(d, tpl)
    return [k for k in tpl.get("required", []) if not v.get(k)]


def draw(msp, d: Drawing) -> Table:
    """画在图框右下角；右、下两边与图框线重合不重复画。"""
    from .symbols import inner_frame

    t = build(d)
    _, fy0, fx1, _ = inner_frame(d)
    draw_table(msp, d, t, (fx1 - t.width * d.scale, fy0), "title_block", inner_layer=LY["frame"], outer_layer=LY["border"])
    return t
