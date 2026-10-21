"""规范自检的化工流程图专项（规范化要求 12）：每项写明出处和 verified（只有出自 GB/T 14691、GB/T 17450 原文的算核对过）。

检查的仍是最终 DXF（build_doc 的结果）加 IR：图上画了什么以 DXF 为准，「应该有什么」以 IR 和阶段要求（standards/stages.json）为准。
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

from zhihui.ir import Drawing

from .standards import LY

LABEL_RE = re.compile(r"^[A-Z]{1,3}\d{4,6}(-((\d+(\.\d+)?×\d+(\.\d+)?)?[A-H]?))?(/[A-Z0-9]+)?(-[A-L]?[0-9A-U]?)?$")
SIZE_RE = re.compile(r"^\d+(\.\d+)?×\d+(\.\d+)?[A-H]?$")
STAGES = json.loads((Path(__file__).with_name("standards") / "stages.json").read_text(encoding="utf-8"))


def _xd(e) -> list[str]:
    try:
        return [v for _, v in e.get_xdata("ZHIHUI")] if e.has_xdata("ZHIHUI") else []
    except Exception:  # noqa: BLE001
        return []


def items(d: Drawing, doc, std, Item, PASS, WARN, ERROR) -> list:  # noqa: C901
    from . import chem, chem_tables, fittings, instruments
    from .codes import lookup
    from .symbols import get_symbol, inflate, symbol_box
    from .tagrow import GAP_BAND, content_top, tag_band
    from .tags import HG_RE

    s = d.scale
    msp = doc.modelspace()
    ents = list(msp)
    out = []
    stage = STAGES.get(d.stage, {})

    # ---- 阶段内容（文章 3.1 节）
    it = Item("stage", f"设计阶段内容（{stage.get('name', d.stage)}）", f"文章 {stage.get('source', '3.1 节')}；standards/stages.json")
    eqs = [e for e in d.equipment if get_symbol(e.symbol_id).category != "X"]
    if stage.get("frame") == "required" and not d.frame:
        it.fail(ERROR, f"{stage['name']}必须画图框和标题栏")
    if stage.get("features") and d.stage != "scheme":
        miss = [e.tag for e in eqs if not e.params.get("feature")]
        if miss:
            it.fail(WARN, f"没有特性参数（params.feature，只能来自用户）：{'、'.join(miss[:8])}")
    if stage.get("stream_tables") is True and not d.stream_tables:
        it.fail(WARN, "物料流程图应在流程起点和使物料发生变化的设备后画物料组成表（add_stream_table），数据只能来自用户")
    if stage.get("fittings") == "required" and not any(c.fittings for c in d.connections):
        it.fail(WARN, "施工流程图应画出阀门和管件（connect_ports 的 fittings）")
    if stage.get("instruments") == "required" and not d.instruments:
        it.fail(WARN, "施工流程图应画出仪表控制点（add_instrument）")
    if d.stage == "scheme" and (d.stream_tables or any(c.params for c in d.connections)):
        it.fail(WARN, "方案流程图不画物料组成表、不标工艺参数（这些只在物料流程图及以上显示）")
    it.verified = False
    out.append(it)

    # ---- 分张、第 张 共 张
    it = Item("sheets", "分张与「第 张 共 张」", "文章第 5 页（过长时分张）、图 2-3 标题栏")
    if not 1 <= d.sheet_no <= d.sheet_total:
        it.fail(ERROR, f"第 {d.sheet_no} 张 共 {d.sheet_total} 张不合理")
    texts = {t.dxf.text for t in msp.query("TEXT")}
    if d.frame and d.template_id != "water_elevation" and std.raw.get("title_block") == "hg_flow":
        for want in (f"第 {d.sheet_no} 张", f"共 {d.sheet_total} 张"):
            if want not in texts:
                it.fail(ERROR, f"标题栏里没有「{want}」")
    it.verified = False
    out.append(it)

    # ---- 虚线、点画线相交在画线处
    it = Item("dash_join", "虚线、点画线相交于画线处", "GB/T 17450-1998 第 5.2.1 条")
    dashed = {name for name, a in _layers(doc).items() if a and a.upper() not in ("CONTINUOUS", "BYLAYER", "BYBLOCK") and not name.endswith("-分段")}
    for e in msp.query("LWPOLYLINE"):
        if e.dxf.layer in dashed and (e.closed or len(e) > 2):
            it.fail(ERROR, f"{e.dxf.layer} 上有带拐角的整条虚线（{_xd(e)[:2]}），拐角不一定落在画上；应按线素相位拆开", (_xd(e) or [""])[1:2][0] if len(_xd(e)) > 1 else "")
    it.verified = True
    out.append(it)

    # ---- 设备、阀门、仪表、表格细实线
    it = Item("thin", "设备、阀门管件、仪表、表格用细实线", "文章第 4 页、3.2.3 节、3.2.4 节、3.1.2 节；GB/T 17450-1998 第 4.1 条")
    thin = std.lw100("thin")
    for role in ("equip", "valve", "instrument", "table"):
        lay = doc.layers.get(LY[role]) if LY[role] in doc.layers else None
        if lay is not None and lay.dxf.lineweight != thin:
            it.fail(ERROR, f"{LY[role]} 线宽 {lay.dxf.lineweight / 100:g} mm，应为细线 {thin / 100:g} mm")
    it.verified = False
    out.append(it)

    # ---- 同类设备外形一致
    it = Item("family", "同类设备外形一致", "规范化要求 5.1（同一 family 用同一套比例）")
    for e in msp.query("INSERT"):
        if _xd(e)[:1] == ["equip"] and (abs(e.dxf.xscale - 1) > 1e-9 or abs(e.dxf.yscale - 1) > 1e-9):
            it.fail(ERROR, f"{_xd(e)[1]} 单独缩放了（{e.dxf.xscale:g}×{e.dxf.yscale:g}），同类设备比例应一致")
    fams: dict[str, set] = {}
    for e in eqs:
        fams.setdefault(get_symbol(e.symbol_id).family, set()).add(e.symbol_id)
    if it.status == PASS:
        it.details.append("图元统一按图元库比例插入，不单独缩放" + (f"；同类：{'、'.join(f'{k}({len(v)} 种)' for k, v in fams.items())}" if fams else ""))
    it.verified = False
    out.append(it)

    # ---- 管口（施工流程图）
    if d.stage == "construction":
        it = Item("nozzles", "设备管口（施工流程图）", "文章 3.1.3 节（施工流程图画出与配管和外界有关的管口）")
        drawn: dict[str, int] = {}
        for e in ents:
            x = _xd(e)
            if x[:1] == ["nozzle"]:
                drawn[x[1]] = drawn.get(x[1], 0) + 1
        for e in eqs:
            want = len(get_symbol(e.symbol_id).nozzles)
            if want and drawn.get(e.id, 0) < want:
                it.fail(ERROR, f"{e.tag} 的管口只画了 {drawn.get(e.id, 0)} / {want} 个", e.tag)
            elif not want and get_symbol(e.symbol_id).category in "VTR":
                it.fail(WARN, f"{e.tag}（{e.symbol_id}）的图元没有定义排液、放空、仪表管口，只画了接管端口", e.tag)
        it.verified = False
        out.append(it)

    # ---- 位号格式、唯一
    it = Item("tags", "设备位号格式与唯一", "文章图 2-4（分类号 + 工段号 + 序号 + 尾号）；HG/T 20519.2 表 10.0.2")
    seen: dict[str, str] = {}
    for e in d.equipment:
        if e.tag.upper() in seen:
            it.fail(ERROR, f"位号 {e.tag} 重复", e.tag)
        seen[e.tag.upper()] = e.id
    if d.tag_style == "hg" and d.section_no:
        bad = [e.tag for e in eqs if not HG_RE.match(e.tag.upper()) or e.tag[0] != get_symbol(e.symbol_id).category]
        if bad:
            it.fail(WARN, f"不是「分类号 + 工段号 + 两位序号 + 尾号」格式（或分类号与图元类别不符）：{'、'.join(bad[:8])}")
    it.verified = False
    out.append(it)

    # ---- 位号栏贴着设备带
    band = tag_band(d) if d.template_id == "pfd_default" else None
    if band:
        it = Item("tag_row", "位号栏位置", "文章 2.2 节（位号、名称排在设备上方或下方，尽量正对设备）；规范化要求 5.7（贴近设备带 8～12 mm）")
        gap = (band[1] - content_top(d)) / s
        if not (8 - 0.5 <= gap <= 12 + 0.5):
            it.fail(WARN, f"位号栏离设备带 {gap:.1f} mm（应 8～12 mm；图框顶部放不下时退回顶部）")
        else:
            it.details.append(f"位号栏在设备带上方 {gap:.1f} mm")
        it.verified = False
        out.append(it)

    # ---- 管线：正交、不穿设备、每根有箭头
    it = Item("pipes", "管线正交、不穿设备、每根有箭头", "文章 3.1 节（管线画成水平或竖直，相交和拐弯是直角；尽量不穿过设备；每根管线以箭头表示流向）")
    arrows: dict[str, int] = {}
    for e in msp.query("SOLID"):
        x = _xd(e)
        if x[:1] == ["arrow"]:
            arrows[x[1]] = arrows.get(x[1], 0) + 1
    for c in d.connections:
        if not c.path:
            it.fail(ERROR, f"{c.id} 没有布线", c.id)
            continue
        for a, b in zip(c.path, c.path[1:]):
            if abs(a[0] - b[0]) > 1e-6 and abs(a[1] - b[1]) > 1e-6:
                it.fail(ERROR, f"{c.id} 有斜线段", c.id)
                break
        if not arrows.get(c.id):
            it.fail(ERROR, f"{c.id} 没有流向箭头", c.id)
        for e in d.equipment:
            if e.id in (c.from_equip, c.to_equip) or not e.placed:
                continue
            box = inflate(symbol_box(e, d.unit), -1.0 * s)
            from .water import _seg_in_box

            if any(_seg_in_box(a, b, box) for a, b in zip(c.path, c.path[1:])):
                it.fail(ERROR, f"{c.id} 穿过设备 {e.tag}", c.id)
    it.verified = False
    out.append(it)

    # ---- 交叉断开一根
    if chem.crossing_style(d) == "break":
        it = Item("crossing", "管线交叉断开一根", "文章 3.1 节、图 2-6（交叉时断开一根：横线连续、竖线断开；主物料与辅助物料交叉断辅助线）")
        pieces: dict[str, list] = {}
        for e in msp.query("LWPOLYLINE"):
            x = _xd(e)
            if x[:1] in (["conn"], ["conn_part"]):
                pieces.setdefault(x[1], []).append(list(e.get_points("xy")))
        for cid, pts in chem.break_points(d).items():
            for p in pts:
                if any(_on(p, q0, q1) for pl in pieces.get(cid, []) for q0, q1 in zip(pl, pl[1:])):
                    it.fail(ERROR, f"{cid} 在 ({p[0]:.0f}, {p[1]:.0f}) 处交叉没有断开", cid)
        n = sum(len(v) for v in chem.break_points(d).values())
        if it.status == PASS:
            it.details.append(f"{n} 处交叉都已断开" if n else "没有交叉")
        it.verified = False
        out.append(it)

    # ---- 管道标注（施工流程图）、标注位置、代号
    it = Item("pipe_label", "管道标注齐全、格式正确、位置（横上竖左）", "文章 3.2 节、图 2-7（物料代号 工段号 管段序号-外径×壁厚 材料/等级-温度 隔热；横向标在上方，竖向标在左侧）")
    labels: dict[str, list] = {}
    for t in msp.query("TEXT"):
        x = _xd(t)
        if x[:1] == ["conn_label"]:
            labels.setdefault(x[1], []).append(t)
    for c in d.connections:
        if d.stage == "construction":
            if not c.pipe_no:
                it.fail(ERROR, f"{c.id}（{c.medium or '无介质名'}）没有管道号：施工流程图每根管线都要标注（物料代号 + 工段号 + 管段序号）", c.id)
            elif not (c.od and c.wall) or not c.material:
                miss = "、".join(k for k, v in (("外径×壁厚", c.od and c.wall), ("材料代号", c.material)) if not v)
                it.fail(WARN, f"{c.pipe_no} 缺{miss}（只能来自用户，ask_user）", c.id)
            want = chem.label_text(d, c)
            if c.pipe_no and want and not (LABEL_RE.match(want) or SIZE_RE.match(want)):
                it.fail(ERROR, f"{c.id} 的管道标注「{want}」格式不对", c.id)
            if c.pipe_no and want and want not in [t.dxf.text for t in labels.get(c.id, [])]:
                it.fail(ERROR, f"{c.id} 的管道标注「{want}」没有画到图上", c.id)
        for t in labels.get(c.id, []):
            pos = _label_side(t, c)
            if pos == "below":
                it.fail(WARN, f"{c.id} 的标注「{t.dxf.text}」在横管下方（上方放不下）", c.id)
            elif pos == "right":
                it.fail(WARN, f"{c.id} 的标注「{t.dxf.text}」在竖管右侧（左侧放不下，字头仍向左）", c.id)
    it.verified = False
    out.append(it)

    it = Item("codes", "物料代号、管道材料代号合法", "文章表 2-6、表 2-7、表 2-8、表 2-9、表 2-10")
    chem_mode = d.stage != "scheme" or bool(d.section_no)
    for c in d.connections:
        if c.code and chem_mode and lookup("material_codes", c.code) is None:
            it.fail(WARN, f"{c.id} 的物料代号 {c.code} 不在表 2-6 里", c.id)
        for k, tab in (("material", "pipe_material"), ("temp_code", "temp_range"), ("insulation_code", "insulation"), ("tracing", "heat_tracing")):
            v = getattr(c, k)
            if v and lookup(tab, v) is None:
                it.fail(ERROR, f"{c.id} 的 {k}={v} 不在代号表里", c.id)
    it.verified = False
    out.append(it)

    # ---- 阀门管件
    if any(c.fittings for c in d.connections):
        it = Item("fittings", "阀门管件符号与尺寸", "文章 3.2.3 节（细实线；阀门长 6 宽 3 或长 8 宽 4）、表 2-11")
        vm = list(std.raw.get("fittings", {}).get("valve_mm", [6, 3]))
        if vm not in ([6, 3], [8, 4]):
            it.fail(ERROR, f"阀门尺寸 {vm[0]}×{vm[1]} 不是 6×3 或 8×4")
        drawn = {(x[1], x[2]) for x in (_xd(e) for e in ents) if x[:1] == ["fitting"]}
        for c in d.connections:
            for i, f in enumerate(c.fittings):
                if f.type not in fittings.library():
                    it.fail(ERROR, f"{c.id} 的管件类型 {f.type} 不在表 2-11 符号库里", c.id)
                elif (c.id, str(i)) not in drawn:
                    it.fail(WARN, f"{c.id} 的 {fittings.library()[f.type]['name_cn']} 没画上（直管段太短，放不下）", c.id)
        if it.status == PASS:
            it.details.append(f"阀门 {vm[0]}×{vm[1]} mm，{len(drawn)} 个管件")
        it.verified = False
        out.append(it)

    # ---- 仪表
    if d.instruments:
        it = Item("instruments", "仪表圆 10 mm、字母代号合法", "文章 3.2.4 节、表 2-12、表 2-13")
        for e in msp.query("CIRCLE"):
            x = _xd(e)
            if x[:1] == ["instrument"] and abs(e.dxf.radius - instruments.DIAMETER / 2 * s) > 1e-6:
                it.fail(ERROR, f"仪表 {x[1]} 的圆直径 {e.dxf.radius * 2 / s:g} mm，应为 10 mm", x[1])
        for ins in d.instruments:
            lvl, msg = instruments.check_letters(ins.letters)
            if lvl == "error":
                it.fail(ERROR, msg, ins.id)
            elif lvl == "warn":
                it.fail(WARN, msg, ins.id)
            if not ins.loop_no:
                it.fail(WARN, f"{ins.letters} 没有工段序号（只能来自用户）", ins.id)
        it.verified = False
        out.append(it)

    # ---- 图例完整、无多余
    want = {e["key"] for e in chem_tables.legend_entries(d)}
    have = {x[1] for x in (_xd(e) for e in ents) if x[:1] == ["legend"] and len(x) > 1} - {"_title"}
    if want or have or stage.get("legend") == "required":
        it = Item("legend", "图例及代号完整、没有多余条目", "文章「流程图上应配备图例」、图 3-4")
        if stage.get("legend") == "required" and not want:
            it.fail(WARN, "施工流程图应有图例及代号（图上还没有管件、代号或仪表）")
        for k in sorted(want - have):
            it.fail(ERROR, f"图上用到了「{k}」，图例里没有")
        for k in sorted(have - want):
            it.fail(ERROR, f"图例里有「{k}」，图上已经没用到")
        if it.status == PASS and want:
            it.details.append(f"{len(want)} 个条目，都在图上用到")
        it.verified = False
        out.append(it)
    return out


def _layers(doc) -> dict[str, str]:
    return {l.dxf.name: l.dxf.linetype for l in doc.layers}


def _on(p, a, b, eps=1e-6) -> bool:
    if abs(a[0] - b[0]) < eps:
        return abs(p[0] - a[0]) < eps and min(a[1], b[1]) - eps < p[1] < max(a[1], b[1]) + eps
    if abs(a[1] - b[1]) < eps:
        return abs(p[1] - a[1]) < eps and min(a[0], b[0]) - eps < p[0] < max(a[0], b[0]) + eps
    return False


def _label_side(t, c) -> str:
    """管线文字在所靠管段的哪一侧：above / below / left / right；引出线标注（离管线远）返回 leader。"""
    from .dxf_io import text_anchor

    x, y = text_anchor(t)
    rot = float(t.dxf.get("rotation", 0) or 0)
    best, side = None, "leader"
    for a, b in zip(c.path or [], (c.path or [])[1:]):
        if abs(a[1] - b[1]) < 1e-6 and abs(rot) < 1 and min(a[0], b[0]) <= x <= max(a[0], b[0]):
            dist = abs(y - a[1])
            if best is None or dist < best:
                best, side = dist, ("above" if y > a[1] else "below")
        elif abs(a[0] - b[0]) < 1e-6 and abs(rot - 90) < 1 and min(a[1], b[1]) <= y <= max(a[1], b[1]):
            dist = abs(x - a[0])
            if best is None or dist < best:
                best, side = dist, ("left" if x < a[0] else "right")
    h = float(t.dxf.height)
    return side if best is not None and best < 3 * h else "leader"
