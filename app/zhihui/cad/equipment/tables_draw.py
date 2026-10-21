"""设备图的表格与技术要求（任务书 5.1 tables_draw.py、5.2 节）：设计数据表、管口表、明细栏、技术要求 → 图纸空间的线和文字 ops。

表头和列宽在 templates/vessel_tables.json；单元格文字放不下就按字宽估算自动换行（字高不缩到 3.5 mm 以下，汉字最小字高见 GB/T 14691），行高随行数增加。
ezdxf 写不出 AutoCAD 原生的 TABLE 对象，这里按“线 + 文字”画表（等价实现，写进功能对照表）。
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from zhihui.cad.standards import text_width
from zhihui.cad.toolsets import common as c

from . import std_tables as T
from .spec_ops import fmt_value

TPL = Path(__file__).resolve().parent / "templates" / "vessel_tables.json"
PENDING = "待填"


@lru_cache(maxsize=1)
def tpl() -> dict:
    return json.loads(TPL.read_text(encoding="utf-8"))


def wrap(s: str, width: float, h: float) -> list[str]:
    """按估算字宽换行（汉字 = 字高×0.7×1.5，字母数字 = 字高×0.7）。"""
    s = str(s)
    lines: list[str] = []
    for para in s.split("\n"):
        cur = ""
        for ch in para:
            if cur and text_width(cur + ch, h) > width:
                lines.append(cur)
                cur = ch.lstrip() if ch == " " else ch
            else:
                cur += ch
        lines.append(cur)
    return lines or [""]


def draw_table(name: str, x0: float, y_anchor: float, cols: list[tuple[str, float]], rows: list[list[str]], *, header_at_bottom: bool = False, title: str | None = None,
               h: float = 3.5, row_h: float = 7.0) -> tuple[list[dict], dict]:
    """画一张表。header_at_bottom=False：y_anchor 是表的上边，表头在上、行往下排；True（明细栏）：y_anchor 是下边，表头在下、行往上排（件号 1 在最下面）。
    返回 (ops, info)，info = {name, rect, first_col, cells}。"""
    pad = 1.0
    pitch = h * 1.5
    ws = [w for _, w in cols]
    W = sum(ws)

    def lay(cells: list[str], hh: float):
        wrapped = [wrap(cl, w - 2 * pad, hh) for cl, w in zip(cells, ws)]
        n = max(len(x) for x in wrapped)
        return wrapped, max(row_h, n * pitch + 2 * pad - (pitch - hh))

    header = lay([t for t, _ in cols], h)
    body = [lay([str(x) for x in r], h) for r in rows]
    order = ([header] + body) if not header_at_bottom else ([header] + body)
    heights = [hh for _, hh in order]
    total = sum(heights)
    title_h = (h * 1.6 + 2.0) if title else 0.0
    ops: list[dict] = []
    if header_at_bottom:
        y_bottom = y_anchor
        ys = []
        y = y_bottom
        for hh in heights:                                   # 表头在最下，往上排
            ys.append((y, y + hh))
            y += hh
        top = y
    else:
        top = y_anchor - title_h
        ys = []
        y = top
        for hh in heights:
            ys.append((y - hh, y))
            y -= hh
        y_bottom = y
    if title:
        ty = (top + 1.5) if header_at_bottom else (top + 1.5)
        ops.append(c.text((x0 + W / 2, ty), title, h * 1.4 if h * 1.4 >= 5 else 5.0, "text", ["table_title"], 0.0, "center"))
    ops.append(c.rect(x0, y_bottom, x0 + W, top, "frame", ["table_outer"]))
    for (ya, yb) in ys[:-1] if not header_at_bottom else ys[:-1]:
        yline = ya if not header_at_bottom else yb
        ops.append(c.line((x0, yline), (x0 + W, yline), "thin", ["table_rule"]))
    xx = x0
    for w in ws[:-1]:
        xx += w
        ops.append(c.line((xx, y_bottom), (xx, top), "thin", ["table_rule"]))
    cells_info = []
    for (wrapped, hh), (ya, yb) in zip(order, ys):
        xx = x0
        row_txt = []
        for lines, w in zip(wrapped, ws):
            n = len(lines)
            block = n * pitch - (pitch - h)
            y_first = (ya + yb) / 2 + block / 2 - h
            for i, ln in enumerate(lines):
                if ln:
                    ops.append(c.text((xx + pad, y_first - i * pitch), ln, h, "text", ["table_cell"]))
            row_txt.append("".join(lines))
            xx += w
        cells_info.append(row_txt)
    # 外框矩形含表名那一条（表名文字在 top 之上、title_h 之内）：表 + 表名占的整个高度，自检的重叠、出框判断才不漏掉表名
    info = {"name": name, "rect": (x0, y_bottom, x0 + W, top + title_h), "header": cells_info[0], "rows": cells_info[1:], "first_col": [r[0] for r in cells_info[1:]]}
    return ops, info


# ------------------------------------------------------------------ 各表的内容
def _v(x) -> str:
    return PENDING if x is None or x == "" else fmt_value(x)


def design_rows(spec) -> list[list[str]]:
    d = spec.design
    items = []
    for label, key in tpl()["design_table"]["rows"]:
        if key == "test":
            v = PENDING if not d.test_type and d.test_pressure_mpa is None else f"{d.test_type or PENDING} / {_v(d.test_pressure_mpa)}"
        elif key == "wind_seismic":
            v = f"{_v(d.wind_pressure_kpa)} / {d.seismic or PENDING}"
        elif key == "_volume":
            v = "待填（P1 计算）"
        elif key == "_mass":
            v = "待填（P1 计算）"
        elif key == "design_standard":
            v = d.design_standard or PENDING
            if d.design_standard and d.design_standard.startswith("GB/T 150"):
                v += "、TSG 21"
        else:
            v = _v(getattr(d, key))
        items.append((label, v))
    rows = []
    for i in range(0, len(items), 2):
        a = items[i]
        b = items[i + 1] if i + 1 < len(items) else ("", "")
        rows.append([a[0], a[1], b[0], b[1]])
    return rows


def nozzle_rows(spec) -> list[list[str]]:
    rows = []
    thr = T.rule_value("reinf_dn_threshold")
    sv = spec.vertical
    ro = spec.shell.inner_diameter_mm / 2 + float(spec.shell.thickness_mm)
    for n in sorted([n for n in spec.nozzles if n.mark], key=_mark_key):
        notes = []
        src = lambda f: spec.sources.get(f"nozzles[{n.uid}].{f}", "")       # noqa: E731
        for code, hit in NOTE_CODES(spec, n, src, thr):
            if hit:
                notes.append(code)
        if n.location == "shell":
            dist = ro + float(n.projection_mm or 0.0)
        else:
            dist = float(n.projection_mm or 0.0)
        purpose = n.purpose or n.purpose_text or PENDING
        if n.kind == "manhole":
            std = (n.std_part or "待定").replace("（初选，待校核）", "")
            rows.append([n.mark, fmt_value(n.dn), _pn(n), " ".join(std.split(" ")[:2]) if std != "待定" else std, "—", "—", purpose, fmt_value(dist), "".join(notes)])
        else:
            rows.append([n.mark, fmt_value(n.dn), _pn(n), n.flange_std or "待定", n.flange_type or "待定", n.facing or "待定", purpose, fmt_value(dist), "".join(notes)])
    return rows


NOTE_TEXT = {"①": "位置、方位或伸出长度按默认，待确认", "②": "PN 按设计条件初选，待校核", "③": "接管壁厚按默认，未校核", "④": "开孔补强待计算", "⑤": "PN 待设计压力和设计温度给出后初选"}


def NOTE_CODES(spec, n, src, thr):
    return [("①", any(src(f).startswith("rule:") for f in ("location", "axial_mm", "radial_mm", "orientation_deg", "projection_mm"))),
            ("②", isinstance(n.pn, (int, float)) and src("pn").startswith("rule:")),
            ("③", src("pipe_thk_mm").startswith("table:")),
            ("④", (n.dn or 0) >= thr and n.kind not in ("level_gauge",)),
            ("⑤", n.pn is None)]


def _pn(n) -> str:
    return fmt_value(n.pn) if isinstance(n.pn, (int, float)) else "待定"


def _mark_key(n):
    """管口表排序：小写字母符号（a、b1、b2…）在前，人孔 M1、M2 在后。"""
    import re
    m = re.fullmatch(r"([A-Za-z]+)(\d*)", n.mark or "")
    if not m:
        return (2, n.mark or "", 0)
    return (1 if m.group(1).isupper() else 0, m.group(1), int(m.group(2) or 0))


def parts_rows(numbered: list[dict]) -> list[list[str]]:
    rows = []
    for r in sorted(numbered, key=lambda x: x["no"]):
        rows.append([str(r["no"]), r["std"], r["name"], str(r["qty"]), r["material"], "—", "—", "；".join(x for x in ["P1 计算", r.get("remark", "")] if x)])
    return rows


def tech_requirements(spec, wall_exaggerated: bool, unverified: list[str], pending_flange_sketch: bool) -> list[str]:
    d = spec.design
    items = []
    std = d.design_standard
    if std and std.startswith("GB/T 150"):
        items.append(f"本设备按 {std} 进行设计、制造、检验和验收，并应符合 TSG 21《固定式压力容器安全技术监察规程》（是否属于监管范围由用户确认）。")
    elif std:
        items.append(f"本设备按 {std} 进行设计、制造、检验和验收。")
    else:
        items.append("设计、制造、检验和验收标准：待填。")
    items.append("焊接接头型式除注明外按 HG/T 20583-2020 的规定；焊条、焊丝等焊接材料：待填。")
    items.append("无损检测方法、检测比例和合格级别：待填。")
    items.append(f"耐压试验型式和试验压力：{d.test_type or '待填'}" + (f"，{fmt_value(d.test_pressure_mpa)} MPa" if d.test_pressure_mpa is not None else "") + f"；泄漏试验：{d.leak_test or '待填'}。")
    items.append("管口方位以" + ("管口方位图" if spec.vertical else "左视图") + "为准；主视图中的管口按旋转画法绘制。")
    if wall_exaggerated:
        items.append("图中壁厚夸大画出，尺寸以标注为准。")
    if pending_flange_sketch:
        items.append(f"PN 待定的管口法兰按 PN{fmt_value(float(T.rule_value('pn_min')))} 的外形示意画出，不代表选型。")
    defaults = [k for k, v in spec.sources.items() if v.startswith("rule:")]
    if defaults:
        items.append("管口位置、方位、伸出长度、支座位置等按默认规则确定，待设计人员确认。")
    used = sorted({code for r in nozzle_rows(spec) for code in r[-1]})
    if used:
        items.append("管口表备注：" + "；".join(f"{k}{NOTE_TEXT[k]}" for k in used if k in NOTE_TEXT) + "。")
    if unverified:
        items.append("本图标准件尺寸部分未经核对，仅供方案阶段使用。")
    items.append("本图未进行强度计算、开孔补强计算和支座校核，不得直接用于制造。")
    return [f"{i}. {s}" for i, s in enumerate(items, 1)]


def tech_ops(lines: list[str], x0: float, y_top: float, width: float, h: float = 3.5) -> tuple[list[dict], dict]:
    ops = [c.text((x0 + width / 2, y_top - 6.0), "技术要求", 5.0, "text", ["tech_title"], 0.0, "center")]
    pitch = h * 1.5
    y = y_top - 6.0 - 2.5 - h
    for ln in lines:
        wl = wrap(ln, width - 2.0, h)
        for i, s in enumerate(wl):
            ops.append(c.text((x0 + (0.0 if i == 0 else text_width("1. ", h)), y), s, h, "text", ["tech_req"]))
            y -= pitch
    return ops, {"name": "技术要求", "rect": (x0, y + pitch - 1.0, x0 + width, y_top), "lines": lines}


def tech_height(lines: list[str], width: float, h: float = 3.5) -> float:
    n = sum(len(wrap(ln, width - 2.0, h)) for ln in lines)
    return 6.0 + 2.5 + n * h * 1.5 + 2.0


def table_height(cols, rows, title: bool, h: float = 3.5, row_h: float = 7.0) -> float:
    _, info = draw_table("x", 0.0, 1000.0, cols, rows, title="t" if title else None, h=h, row_h=row_h)
    return 1000.0 - info["rect"][1]


def uses_pending_sketch(spec) -> bool:
    return any(n.pn is None and n.dn is not None for n in spec.nozzles)

