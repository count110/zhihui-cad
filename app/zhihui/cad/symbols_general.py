"""通用图纸的符号库：按类别索引。

· mech_weld / mech_surface / mech_gdt / arch_annotation / electrical / hydraulic_pid：内置的程序化符号（简化画法，单位 mm，原点是插入基点）。
· chem_equipment / water：复用流程图图元库 zhihui/cad/symbols/*.json（SymbolDef.graphics 转成通用图元）。
· 用户自定义：<data>/symbols/custom/<id>.json（由现有块注册，或从 JSON 导入）。
符号落到图纸上是一个块 SYM_<id>（首次插入时生成），所以可以整体移动、缩放、炸开。
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

from .geom import mech
from .geom.errors import GeomError
from .toolsets import common as c

CATEGORIES = {
    "mech_weld": "机械：焊接符号", "mech_surface": "机械：表面结构", "mech_gdt": "机械：形位公差与基准", "arch_annotation": "建筑：标注符号",
    "electrical": "电气图形符号", "hydraulic_pid": "液压/气动原理图", "chem_equipment": "化工设备（流程图图元库）", "water": "给水处理构筑物（流程图图元库）",
}
WATER_IDS = {"clear_well", "intake_head", "pit", "floc_sed_tank", "valveless_filter", "dosing_point", "clo2_generator", "retaining_wall", "submerged_pump"}


# ------------------------------------------------------------------ 内置符号构造
def _sym(id_, name, cat, aliases, ops, desc="", source="简化画法"):
    return {"id": id_, "name_cn": name, "category": cat, "aliases": aliases, "ops": ops, "desc": desc, "source": source, "builtin": True}


def _weld():
    def fillet():
        return [c.poly([(0, 0), (6, 0), (0, 6)], True, "outline"), c.line((-4, 0), (10, 0), "thin")]

    def butt_i():
        return [c.line((-0.7, 0), (-0.7, 6), "outline"), c.line((0.7, 0), (0.7, 6), "outline"), c.line((-6, 0), (6, 0), "thin")]

    def butt_v():
        return [c.poly([(-3, 6), (0, 0), (3, 6)], False, "outline"), c.line((-6, 0), (6, 0), "thin")]

    def spot():
        return [c.circle((0, 3), 2.5, "outline"), c.line((-6, 0), (6, 0), "thin")]

    def arrow_line():
        return [c.line((0, 0), (20, 0), "thin"), c.poly([(0, 0), (-4, 1.7)], False, "outline"), c.poly([(0, 0), (-4, -1.7)], False, "outline"), c.line((0, 0), (-8, 6), "outline")]
    return [_sym("weld_fillet", "角焊缝符号", "mech_weld", ["角焊", "三角焊缝"], fillet(), "GB/T 324 基本符号：直角三角形，画在基准线上"),
            _sym("weld_butt_i", "I 形焊缝符号", "mech_weld", ["I形焊缝", "对接焊缝"], butt_i(), "GB/T 324 基本符号"),
            _sym("weld_butt_v", "V 形焊缝符号", "mech_weld", ["V形焊缝", "V型坡口"], butt_v(), "GB/T 324 基本符号"),
            _sym("weld_spot", "点焊符号", "mech_weld", ["点焊"], spot(), "GB/T 324 基本符号"),
            _sym("weld_leader", "焊缝指引线", "mech_weld", ["焊接指引线"], arrow_line(), "箭头线 + 基准线（实线），符号画在基准线上侧/下侧")]


def _surface():
    out = []
    for proc, nm, al in (("any", "表面粗糙度（任意方法）", ["粗糙度", "表面结构"]), ("remove", "表面粗糙度（去除材料）", ["去除材料", "粗糙度去除"]), ("no_remove", "表面粗糙度（不去除材料）", ["不去除材料"])):
        out.append(_sym(f"roughness_{proc}", nm, "mech_surface", al, mech.roughness_ops((0, 0), "Ra", "", proc), "GB/T 131；数值请在插入后用 create_mechanical_tolerances 标注"))
    return out


def _gdt():
    out = [_sym("datum_triangle", "基准符号", "mech_gdt", ["基准", "基准三角"], mech.datum_symbol_ops((0, 0), "A"), "GB/T 1182 基准符号：涂黑三角 + 方框字母")]
    for s in mech.GDT_SYMBOLS:
        out.append(_sym(f"gdt_{s}", f"{mech.GDT_NAMES[s]}符号", "mech_gdt", [mech.GDT_NAMES[s]],
                        [c.poly([(-3.5, -3.5), (3.5, -3.5), (3.5, 3.5), (-3.5, 3.5)], True, "thin")] + [{**o, "role": "outline"} for o in mech.gdt_glyph(s, 0, 0, 1.0)], "GB/T 1182 形位公差特征符号（带 7×7 框）"))
    return out


def _arch():
    north = [c.circle((0, 0), 12, "thin"), c.poly([(0, 14), (-4, -10), (0, -4), (4, -10)], True, "outline"), c.text((-2.5, 16), "N", 5, "text")]
    elev = [c.poly([(0, 0), (-2, 3.5), (2, 3.5)], True, "outline"), c.line((-10, 3.5), (10, 3.5), "thin")]
    section = [c.line((0, 0), (0, 10), "outline"), c.line((0, 10), (-5, 10), "outline"), c.line((0, 0), (-5, 0), "outline")]
    bubble = [c.circle((0, 0), 4, "thin")]
    return [_sym("north_arrow", "指北针", "arch_annotation", ["指北", "北向"], north, "GB/T 50104"), _sym("elevation_mark", "标高符号", "arch_annotation", ["标高", "高程符号"], elev, "GB/T 50001 等腰直角三角形（简化）"),
            _sym("section_mark", "剖切符号", "arch_annotation", ["剖切", "剖视符号"], section, "粗实线剖切位置线"), _sym("axis_bubble", "轴线编号圆", "arch_annotation", ["轴号圆", "定位轴线圈"], bubble, "直径 8 mm")]


def _elec():
    lead = lambda a, b: c.line(a, b, "outline")
    res = [lead((-10, 0), (-5, 0)), c.rect(-5, -2, 5, 2, "outline"), lead((5, 0), (10, 0))]
    cap = [lead((-10, 0), (-1, 0)), c.line((-1, -4), (-1, 4), "outline"), c.line((1, -4), (1, 4), "outline"), lead((1, 0), (10, 0))]
    ind = [lead((-10, 0), (-6, 0))] + [c.arc((-3 + 4 * i, 0), 2, 0, 180) for i in range(3)] + [lead((6, 0), (10, 0))]
    gnd = [lead((0, 4), (0, 0)), c.line((-5, 0), (5, 0), "outline"), c.line((-3.3, -2), (3.3, -2), "outline"), c.line((-1.6, -4), (1.6, -4), "outline")]
    bat = [lead((-10, 0), (-2, 0)), c.line((-2, -5), (-2, 5), "outline"), c.line((2, -3), (2, 3), "outline"), lead((2, 0), (10, 0))]
    sw = [lead((-10, 0), (-4, 0)), c.line((-4, 0), (4, 5), "outline"), lead((4, 0), (10, 0))]
    lamp = [lead((-10, 0), (-4, 0)), c.circle((0, 0), 4, "outline"), c.line((-2.8, -2.8), (2.8, 2.8), "outline"), c.line((-2.8, 2.8), (2.8, -2.8), "outline"), lead((4, 0), (10, 0))]
    diode = [lead((-10, 0), (-3, 0)), c.poly([(-3, -4), (-3, 4), (3, 0)], True, "outline"), c.line((3, -4), (3, 4), "outline"), lead((3, 0), (10, 0))]
    src = [lead((-10, 0), (-5, 0)), c.circle((0, 0), 5, "outline"), c.poly([(-3, 0), (-1.5, 2), (0, 0), (1.5, -2), (3, 0)], False, "outline"), lead((5, 0), (10, 0))]
    mk = lambda i, n, a, o: _sym(i, n, "electrical", a, o, "GB/T 4728 简化画法")
    return [mk("resistor", "电阻", ["电阻器"], res), mk("capacitor", "电容", ["电容器"], cap), mk("inductor", "电感", ["电感器", "线圈"], ind), mk("ground", "接地", ["地线", "接地符号"], gnd),
            mk("battery", "电池", ["电源", "电池组"], bat), mk("switch", "开关", ["单刀开关"], sw), mk("lamp", "灯", ["指示灯", "灯泡"], lamp), mk("diode", "二极管", ["二极管符号"], diode), mk("ac_source", "交流电源", ["交流源"], src)]


def _hyd():
    lead = lambda a, b: c.line(a, b, "outline")
    pump = [c.circle((0, 0), 6, "outline"), {**c.poly([(-3, -3), (3, -3), (0, 3)], True, "outline"), "tags": ["filled"]}, lead((0, 6), (0, 12))]
    cyl = [c.rect(-12, -4, 12, 4, "outline"), c.line((0, -4), (0, 4), "outline"), c.line((0, 0), (20, 0), "outline"), lead((-9, 4), (-9, 9)), lead((9, 4), (9, 9))]
    chk = [lead((-10, 0), (-4, 0)), c.circle((0, 0), 3, "outline"), c.poly([(4, -4), (0, 0), (4, 4)], False, "outline"), lead((3, 0), (10, 0))]
    rel = [c.rect(-6, -6, 6, 6, "outline"), c.line((-2, -4), (-2, 4), "outline"), c.poly([(-2, 4), (-3.5, 1.5), (-0.5, 1.5)], True, "outline"), lead((0, -6), (0, -10)), lead((0, 6), (0, 10))]
    tank = [c.poly([(-8, 0), (-8, -8), (8, -8), (8, 0)], False, "outline")]
    flt = [c.poly([(-6, 0), (0, 6), (6, 0), (0, -6)], True, "outline"), c.line((-4, -4), (4, 4), "hidden"), lead((-10, 0), (-6, 0)), lead((6, 0), (10, 0))]
    mk = lambda i, n, a, o: _sym(i, n, "hydraulic_pid", a, o, "GB/T 786.1 简化画法")
    return [mk("hyd_pump", "液压泵（单向定量）", ["液压泵", "油泵"], pump), mk("hyd_cylinder_double", "双作用液压缸", ["液压缸", "油缸", "双作用缸"], cyl), mk("hyd_check_valve", "单向阀", ["止回阀", "液压单向阀"], chk),
            mk("hyd_relief_valve", "溢流阀", ["安全阀", "液压溢流阀"], rel), mk("hyd_tank", "油箱", ["油箱符号", "回油箱"], tank), mk("hyd_filter", "过滤器", ["滤油器"], flt)]


def builtins() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for s in _weld() + _surface() + _gdt() + _arch() + _elec() + _hyd():
        out[s["id"]] = s
    return out


# ------------------------------------------------------------------ 流程图图元库转换
def _from_pfd(sd) -> dict:
    ops = []
    for g in sd.graphics:
        t = g["type"]
        if t == "polyline":
            ops.append(c.poly([tuple(p) for p in g["points"]], bool(g.get("closed")), "outline"))
        elif t == "circle":
            ops.append(c.circle(g["c"], g["r"], "outline"))
        elif t == "arc":
            ops.append(c.arc(g["c"], g["r"], g["start"], g["end"], "outline"))
        elif t == "ellipse":
            rx, ry = g["rx"], g["ry"]
            major = (rx, 0.0) if rx >= ry else (0.0, ry)
            ratio = (ry / rx) if rx >= ry else (rx / ry)
            ops.append({"kind": "ellipse", "role": "outline", "tags": [], "data": {"c": [g["c"][0], g["c"][1], 0.0], "major": [major[0], major[1], 0.0], "ratio": ratio, "t0": float(g.get("start", 0)), "t1": float(g.get("end", 360))}})
        elif t == "text":
            ops.append(c.text(g["p"], g["text"], g.get("h", 2.5), "text"))
        elif t == "hatch":
            loops = [[(x, y) for x, y in g["boundary"]]] + [[(x, y) for x, y in h] for h in g.get("holes", [])]
            ops.append({"kind": "hatch", "role": "thin", "tags": [], "data": {"pattern": g.get("pattern", "ANSI31"), "scale": max(g.get("paper_spacing", 3.0) / 3.0, 0.1), "angle": float(g.get("angle", 0)),
                                                                          "loops": [[[x, y, 0.0] for x, y in lp] for lp in loops], "island_style": "normal", "associative": False, "source_ids": [], "gradient": None}})
    return {"id": sd.id, "name_cn": sd.name_cn, "category": "water" if sd.id in WATER_IDS else "chem_equipment", "aliases": list(sd.aliases), "ops": ops, "desc": sd.source, "source": sd.source,
            "builtin": True, "ports": [{"name": p.name, "x": p.x, "y": p.y, "dir": p.dir} for p in sd.ports], "pfd": True}


def pfd_symbols() -> dict[str, dict]:
    from . import symbols
    return {sid: _from_pfd(sd) for sid, sd in symbols.load_symbols().items()}


# ------------------------------------------------------------------ 自定义符号
def custom_dir() -> Path:
    from zhihui.tools.store import data_dir
    return data_dir() / "symbols" / "custom"


def custom_symbols() -> dict[str, dict]:
    d = custom_dir()
    out = {}
    if d.is_dir():
        for f in sorted(d.glob("*.json")):
            try:
                r = json.loads(f.read_text(encoding="utf-8"))
                out[r["id"]] = {**r, "builtin": False}
            except (ValueError, KeyError):
                continue
    return out


def all_symbols() -> dict[str, dict]:
    out = {**pfd_symbols(), **builtins()}
    out.update({k: v for k, v in custom_symbols().items() if k not in out})        # 自定义的不能覆盖内置
    return out


def valid_id(sid: str) -> str:
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{1,40}", sid or ""):
        raise GeomError("INVALID_ARGS", f"符号 id「{sid}」不合法", "字母开头，只含字母数字下划线，2–41 个字符")
    return sid


def save_custom(sym: dict) -> Path:
    valid_id(sym["id"])
    d = custom_dir()
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{sym['id']}.json"
    p.write_text(json.dumps({k: v for k, v in sym.items() if k not in ("builtin", "pfd")}, ensure_ascii=False, indent=1), encoding="utf-8")
    return p


def search(keyword: str, category: str | None = None, limit: int = 30) -> list[dict]:
    kw = (keyword or "").strip().lower()
    rows = []
    for s in all_symbols().values():
        if category and s["category"] != category:
            continue
        hay = " ".join([s["id"], s["name_cn"], " ".join(s.get("aliases", [])), s.get("desc", "")]).lower()
        if not kw or kw in hay:
            score = 3 if kw == s["id"].lower() or kw == s["name_cn"].lower() else 2 if kw in s["name_cn"].lower() or any(kw in a.lower() for a in s.get("aliases", [])) else 1
            rows.append((score, s))
    rows.sort(key=lambda t: (-t[0], t[1]["id"]))
    return [{"id": s["id"], "name": s["name_cn"], "category": s["category"], "aliases": s.get("aliases", []), "builtin": s.get("builtin", False), "desc": s.get("desc", "")} for _, s in rows[:limit]]
