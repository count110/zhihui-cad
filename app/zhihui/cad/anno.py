"""专业符号块（symbols/annotation/*.json）：标高、流向、折断线、剖切符号、索引符号、指北针、坡度。

全部按标准尺寸（图纸 mm）定义成带属性的块，出图时插入，不在代码里临时拼图形。
块里的图元放 0 层、随插入层；属性字高取自标准配置的字高系列。
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from ezdxf.enums import TextEntityAlignment

from zhihui.ir import Drawing

ANNO_DIR = Path(__file__).with_name("symbols") / "annotation"
PREFIX = "ZH_ANNO_"


@lru_cache(maxsize=1)
def load() -> dict[str, dict]:
    return {f.stem: json.loads(f.read_text(encoding="utf-8")) for f in sorted(ANNO_DIR.glob("*.json"))}


def block_name(aid: str) -> str:
    return PREFIX + aid


def define(doc, aid: str, d: Drawing) -> str:
    """把专业符号定义成块（图纸 mm × 出图比例）；已定义过直接返回块名。"""
    from .dxf_io import TEXT_STYLE, WIDTH_FACTOR, _text
    from .hatch import SOLID, base_spacing, check_pattern
    from .standards import text_h

    name = block_name(aid)
    if name in doc.blocks:
        return name
    spec = load()[aid]
    k = d.scale
    blk = doc.blocks.new(name=name)
    for g in spec["graphics"]:
        t = g["type"]
        if t == "polyline":
            blk.add_lwpolyline([(x * k, y * k) for x, y in g["points"]], close=g.get("closed", False), dxfattribs={"layer": "0"})
        elif t == "circle":
            blk.add_circle((g["c"][0] * k, g["c"][1] * k), g["r"] * k, dxfattribs={"layer": "0"})
        elif t == "text":
            _text(blk, g["text"], (g["p"][0] * k, g["p"][1] * k), text_h(d, g.get("role", "annotation"), g["text"]), "0",
                  TextEntityAlignment.BOTTOM_CENTER)
        elif t == "hatch":
            h = blk.add_hatch(color=256, dxfattribs={"layer": "0"})
            pat = check_pattern(g.get("pattern", "SOLID"))
            if pat == SOLID:
                h.set_solid_fill(color=256)
            else:
                h.set_pattern_fill(pat, scale=g.get("paper_spacing", 2) * k / base_spacing(pat), color=256)
            h.paths.add_polyline_path([(x * k, y * k) for x, y in g["boundary"]], is_closed=True, flags=1)
    for a in spec.get("attribs", []):
        attrs = {"layer": "0", "style": TEXT_STYLE, "width": WIDTH_FACTOR, "height": text_h(d, a["role"], a.get("default", "") or "0")}
        blk.add_attdef(a["tag"], text=a.get("default", ""), dxfattribs=attrs).set_placement(
            (a["p"][0] * k, a["p"][1] * k), align=TextEntityAlignment[a["align"]])
    return name


def insert(msp, doc, d: Drawing, aid: str, pos, layer: str, values: dict | None = None, rotation: float = 0.0,
           mirror_y: bool = False, xdata: list | None = None):
    """插入专业符号；values 填属性（如 {"VALUE": "292.90"}），属性跟插入层，字高按内容（含汉字不小于 3.5）重算。"""
    from .standards import text_h

    name = define(doc, aid, d)
    ref = msp.add_blockref(name, pos, dxfattribs={"layer": layer, "rotation": rotation, "yscale": -1.0 if mirror_y else 1.0})
    if values:
        from .dxf_io import fix_insert, restyle

        ref.add_auto_attribs(values)
        roles = {a["tag"]: a["role"] for a in load()[aid].get("attribs", [])}
        for att in ref.attribs:
            att.dxf.layer = layer
            att.dxf.height = text_h(d, roles.get(att.dxf.tag, "annotation"), att.dxf.text)
            restyle(att)
            fix_insert(att)  # 不重算的查看器也能放对位置
    if xdata:
        from .dxf_io import APPID

        ref.set_xdata(APPID, xdata)
    return ref
