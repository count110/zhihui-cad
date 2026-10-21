"""件号与明细（任务书 5.1 balloons.py）：part_list 列出部件（同规格合并、数量相加），balloon_ops 在主视图旁排一列（卧式排一行）件号，
按指引线起点的位置排序（立式从上到下、卧式从左到右），指引线不交叉；件号与明细栏一一对应（GB/T 4458.2，件号按顺序排列）。
"""
from __future__ import annotations

from zhihui.cad.toolsets import common as c

from .rules import flange_dims
from .spec_ops import fmt_value
from .views import Builder

PENDING = "待填"
MASS_NOTE = "P1 计算"


def _mat(v) -> str:
    return v if v else PENDING


def part_list(spec, view) -> list[dict]:
    """部件 → [{key, name, std, qty, material, remark, anchor}]（还没编号）。"""
    s = spec
    sh = s.shell
    rows: list[dict] = []
    anchors = view.anchors
    hs = s.heads
    kinds = {"EHA": "椭圆形封头", "HHA": "半球形封头", "THA": "碟形封头", "SDH": "球冠形封头", "FLAT": "平盖"}

    def head_name(h):
        return f"{kinds.get(h.kind, h.kind)} {h.kind} {fmt_value(sh.inner_diameter_mm)}×{fmt_value(h.thickness_mm)}"

    same = len(hs) == 2 and (hs[0].kind, hs[0].thickness_mm, hs[0].material, hs[0].straight_flange_mm) == (hs[1].kind, hs[1].thickness_mm, hs[1].material, hs[1].straight_flange_mm)
    end = hs[-1].position
    if same:
        rows.append({"key": "head", "name": head_name(hs[0]), "std": "GB/T 25198", "qty": 2, "material": _mat(hs[0].material), "remark": "", "anchor": anchors.get("head_" + end)})
    else:
        for h in hs:
            rows.append({"key": f"head_{h.position}", "name": head_name(h), "std": "GB/T 25198" if h.kind != "FLAT" else "—", "qty": 1, "material": _mat(h.material), "remark": "",
                         "anchor": anchors.get("head_" + h.position)})
    rows.append({"key": "shell", "name": f"筒体 DN{fmt_value(sh.inner_diameter_mm)}×{fmt_value(sh.thickness_mm)} L={fmt_value(sh.length_mm)}", "std": "—", "qty": 1, "material": _mat(sh.material),
                 "remark": "", "anchor": anchors.get("shell")})
    groups: dict[tuple, list] = {}
    for n in s.nozzles:
        if n.mark is None or n.mark not in view.nozzles:
            continue
        key = (n.kind, n.dn, n.pn, n.pipe_series, n.flange_type, n.facing, n.pipe_od_mm, n.pipe_thk_mm, n.std_part, n.pipe_material)
        groups.setdefault(key, []).append(n)
    for key, ns in groups.items():
        n0 = ns[0]
        marks = "、".join(n.mark for n in ns)
        vn = view.nozzles[n0.mark]
        if n0.kind in ("manhole", "handhole"):
            word = "人孔" if n0.kind == "manhole" else "手孔"
            rows.append({"key": f"mh_{n0.mark}", "name": f"{word} DN{fmt_value(n0.dn)}", "std": n0.std_part.split(" ")[0] + " " + n0.std_part.split(" ")[1] if n0.std_part and len(n0.std_part.split(" ")) > 1 else "待定",
                         "qty": len(ns), "material": PENDING, "remark": f"管口 {marks}" + ("；初选待校核" if n0.std_part else "；型号待定"), "anchor": vn["face"]})
            continue
        pn = f"-{fmt_value(n0.pn)}" if isinstance(n0.pn, (int, float)) else "-PN待定"
        ft = f"{n0.flange_type} " if n0.flange_type else ""
        rows.append({"key": f"flg_{n0.mark}", "name": f"法兰 {ft}{fmt_value(n0.dn)}{pn} {n0.facing or ''}".strip(), "std": n0.flange_std or "HG/T 20592（待定）", "qty": len(ns), "material": PENDING,
                     "remark": f"管口 {marks}" + ("；PN初选待校核" if isinstance(n0.pn, (int, float)) and not str(spec.sources.get(f'nozzles[{n0.uid}].pn', '')).startswith('user:') else ""),
                     "anchor": vn["face"]})
        rows.append({"key": f"pipe_{n0.mark}", "name": f"接管 Φ{fmt_value(n0.pipe_od_mm)}×{fmt_value(n0.pipe_thk_mm)}", "std": "—", "qty": len(ns), "material": _mat(n0.pipe_material),
                     "remark": f"管口 {marks}；壁厚未校核", "anchor": vn["anchor"]})
    for sup in s.supports:
        if not sup.count:
            continue
        name = {"lug": "耳式支座", "leg": "支腿", "saddle": "鞍式支座"}.get(sup.kind or "", sup.kind or "支座")
        std = {"lug": "NB/T 47065.3", "leg": "NB/T 47065.2", "saddle": "NB/T 47065.1"}.get(sup.kind or "", "—")
        model = sup.model or "待定"
        rows.append({"key": f"sup_{sup.uid}", "name": name, "std": std, "qty": int(sup.count), "material": _mat(sup.material), "remark": "型号" + ("待定" if model == "待定" else "初选，待校核"),
                     "anchor": anchors.get(sup.kind or "")})
    return [r for r in rows if r["anchor"] is not None]


def balloon_ops(spec, view, rows: list[dict], x_or_y: float) -> tuple[list[dict], list[dict]]:
    """排件号：立式在 x = x_or_y 处排一列（从上到下），卧式在 y = x_or_y 处排一行（从左到右）。返回 (ops, 编好号的 rows)。"""
    den = view.den
    r = 5.0 * den                                         # 件号圆 ⌀10（图纸 mm）
    gap = 2.0 * den
    b = Builder("M", None)
    vert = spec.vertical
    order = sorted(rows, key=(lambda x: -x["anchor"][1]) if vert else (lambda x: x["anchor"][0]))
    pos: list[float] = []
    for row in order:
        want = row["anchor"][1] if vert else row["anchor"][0]
        if pos:
            want = min(want, pos[-1] - (2 * r + gap)) if vert else max(want, pos[-1] + (2 * r + gap))
        pos.append(want)
    if vert:
        lo = view.bbox[1]
        if pos and pos[-1] < lo:
            shift = lo - pos[-1]
            pos = [p + shift for p in pos]
    out_rows = []
    for i, (row, p) in enumerate(zip(order, pos), 1):
        cxy = (x_or_y, p) if vert else (p, x_or_y)
        ax, ay = row["anchor"]
        edge = (cxy[0] - r, cxy[1]) if vert else (cxy[0], cxy[1] - r)
        b.add(f"BAL-{i}", [c.line((ax, ay), edge, "thin", ["leader"]), c.circle((ax, ay), 0.6 * den, "thin", ["leader_dot"]), c.circle(cxy, r, "thin", ["balloon"])], local=False)
        b.text(f"BAL-{i}", (cxy[0], cxy[1] - 2.5 * den), str(i), 5.0 * den, "center", local=False, tags=["balloon_no", f"balloon:{i}"])
        out_rows.append({**row, "no": i})
    return b.ops, out_rows
