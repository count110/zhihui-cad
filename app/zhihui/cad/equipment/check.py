"""设备图自检（任务书第 7 节）：参数、视图、表格、布局之间是否一致。返回 {"errors": [...], "warnings": [...]}，每项 {code, message}。

不检查“值是不是用户给的”（E_INVENTED_VALUE 在引擎侧的 check_vessel_patch 里做，只有那里看得到原话）。
流程图专用的 standards_check.py 不用于设备图。
"""
from __future__ import annotations

import math

from zhihui.cad import standards as STD
from zhihui.cad.geom import layout as LYT

from . import std_tables as T
from .profile import head_profile
from .rules import _angle_diff, footprint, pad_od
from .spec_ops import fmt_value, missing


def _e(code: str, msg: str) -> dict:
    return {"code": code, "message": msg}


def _overlap(a, b, tol: float = 0.05) -> bool:
    return a[0] < b[2] - tol and b[0] < a[2] - tol and a[1] < b[3] - tol and b[1] < a[3] - tol


def run(spec, d=None) -> dict:
    errors: list[dict] = []
    warnings: list[dict] = []
    pend = missing(spec)
    if pend:
        errors.append(_e("E_SPEC_PENDING", "必问参数没填：" + "、".join(p["label"] for p in pend)))
        return {"errors": errors, "warnings": warnings}
    sh = spec.shell
    L, di = float(sh.length_mm), float(sh.inner_diameter_mm)
    ro = di / 2 + float(sh.thickness_mm)
    edge = float(T.rule_value("seam_min_edge_mm"))
    limit = float(T.rule_value("head_radial_limit_ratio")) * di
    # ---- 管口符号
    marks = [n.mark for n in spec.nozzles if n.mark]
    dup = sorted({m for m in marks if marks.count(m) > 1})
    if dup:
        errors.append(_e("E_MARK_DUP", f"管口符号重复：{'、'.join(dup)}"))
    # ---- 管口位置
    shell = [n for n in spec.nozzles if n.location == "shell" and n.axial_mm is not None]
    for n in shell:
        r = float(n.pipe_od_mm or 0) / 2
        if n.axial_mm - r < 0 or n.axial_mm + r > L:
            errors.append(_e("E_NOZZLE_OUT_OF_RANGE", f"管口 {n.mark} 的开孔不在筒体段内（轴向 {fmt_value(n.axial_mm)}，筒体长度 {fmt_value(L)}）"))
            continue
        pr = pad_od(n) / 2
        if n.axial_mm - pr < edge - 1e-6 or n.axial_mm + pr > L - edge + 1e-6:
            errors.append(_e("E_NOZZLE_ON_SEAM", f"管口 {n.mark} 的开孔（含补强圈估计外径）离环焊缝小于 {fmt_value(edge)} mm"))
    for i, a in enumerate(shell):
        for b in shell[i + 1:]:
            if a.orientation_deg is None or b.orientation_deg is None:
                continue
            fa, fb = footprint(a), footprint(b)
            arc = math.radians(_angle_diff(a.orientation_deg, b.orientation_deg)) * ro
            if arc < (fa + fb) / 2 - 1e-6 and abs(a.axial_mm - b.axial_mm) < (fa + fb) / 2 - 1e-6:
                errors.append(_e("E_NOZZLE_OVERLAP", f"管口 {a.mark} 与 {b.mark} 在筒体上重叠（含法兰外缘和补强圈估计外径）"))
    for loc in ("top_head", "bottom_head", "left_head", "right_head"):
        hs = [n for n in spec.nozzles if n.location == loc and n.radial_mm is not None]
        for n in hs:
            if float(n.radial_mm) + pad_od(n) / 2 > limit + 1e-6:
                errors.append(_e("E_NOZZLE_OUT_OF_RANGE", f"封头管口 {n.mark} 的开孔外边缘超出 0.4Di（{fmt_value(limit)} mm）"))
        for i, a in enumerate(hs):
            for b in hs[i + 1:]:
                pa = (a.radial_mm * math.sin(math.radians(a.orientation_deg or 0)), a.radial_mm * math.cos(math.radians(a.orientation_deg or 0)))
                pb = (b.radial_mm * math.sin(math.radians(b.orientation_deg or 0)), b.radial_mm * math.cos(math.radians(b.orientation_deg or 0)))
                if math.dist(pa, pb) < (footprint(a) + footprint(b)) / 2 - 1e-6:
                    errors.append(_e("E_NOZZLE_OVERLAP", f"封头管口 {a.mark} 与 {b.mark} 重叠"))
    # ---- 管口与支座
    for sup in spec.supports:
        if sup.kind != "lug" or not sup.positions:
            continue
        row = T.lookup("lug_support_nbt47065_3", dn=di)["row"]
        for n in shell:
            for p in sup.positions:
                e = p.get("elevation_mm")
                if e is None or n.orientation_deg is None:
                    continue
                arc = math.radians(_angle_diff(n.orientation_deg, p["orientation_deg"])) * ro
                fp = footprint(n)
                if arc < row["b1"] / 2 + fp / 2 and e - fp / 2 < n.axial_mm < e + row["H"] + fp / 2:
                    errors.append(_e("E_NOZZLE_SUPPORT_CLASH", f"管口 {n.mark} 与 {fmt_value(p['orientation_deg'])}° 的耳式支座冲突"))
    for sup in spec.supports:
        if sup.kind != "saddle" or not sup.positions:
            continue
        row = T.lookup("saddle_nbt47065_1", dn=di)["row"]
        for n in shell:
            th = (n.orientation_deg or 0.0) % 360
            if 90 < th < 270:
                for p in sup.positions:
                    if abs(n.axial_mm - p["axial_mm"]) < row["b1"] / 2 + footprint(n) / 2:
                        errors.append(_e("E_NOZZLE_SUPPORT_CLASH", f"底部管口 {n.mark} 与鞍座（轴向 {fmt_value(p['axial_mm'])}）冲突"))
    rec = spec.drawing.get("record")
    if rec:
        # ---- 表格一致性
        want = sorted(marks)
        if sorted(rec.get("nozzle_rows", [])) != want:
            errors.append(_e("E_TABLE_MISMATCH", f"管口表的行 {sorted(rec.get('nozzle_rows', []))} 与参数里的管口 {want} 不一致"))
        if sorted(rec.get("main_marks", [])) != want:
            errors.append(_e("E_TABLE_MISMATCH", f"主视图上的管口符号 {sorted(rec.get('main_marks', []))} 与参数 {want} 不一致"))
        if sorted(rec.get("end_marks", [])) != want:
            errors.append(_e("E_TABLE_MISMATCH", f"{'管口方位图' if spec.vertical else '左视图'}上的管口符号与参数 {want} 不一致"))
        if d is not None:
            drawn = sorted(int(t.split(":")[1]) for e in d.entities if e.space == "Model" for t in e.tags if t.startswith("balloon:"))
            nos = sorted(p["no"] for p in rec.get("parts", []))
            if drawn != nos:
                errors.append(_e("E_TABLE_MISMATCH", f"件号 {drawn} 与明细栏 {nos} 不一致"))
            on_drawing = set()
            for e in d.entities:
                for t in e.tags:
                    if t.startswith("equip_mark:") and "equip_view:M" in e.tags:
                        on_drawing.add(t.split(":", 1)[1])
            if on_drawing and sorted(on_drawing) != want:
                errors.append(_e("E_TABLE_MISMATCH", f"主视图图元上的管口符号 {sorted(on_drawing)} 与参数 {want} 不一致"))
        # ---- 尺寸链（按参数值）
        dm = rec.get("dim") or {}
        hs = {h.position: head_profile(spec, h) for h in spec.heads}
        expect = L + sum(p.total_height for p in hs.values())
        if dm and (abs(sum(dm.get("chain", [])) - dm.get("overall", 0)) > 1e-6 or abs(dm.get("overall", 0) - expect) > 1e-6):
            errors.append(_e("E_DIM_CHAIN", f"尺寸链不闭合：分段和 {fmt_value(sum(dm.get('chain', [])))}，总尺寸 {fmt_value(dm.get('overall'))}，按参数应为 {fmt_value(expect)}"))
        # ---- 布局
        ix0, iy0, ix1, iy1 = rec["inner"]
        rects = [(v["id"], v["rect"]) for v in rec.get("viewports", [])] + [(t["name"], t["rect"]) for t in rec.get("tables", [])] + [("标题栏", rec["title_block"])]
        for name, r in rects:
            if r[0] < ix0 - 0.05 or r[1] < iy0 - 0.05 or r[2] > ix1 + 0.05 or r[3] > iy1 + 0.05:
                errors.append(_e("E_VIEW_OVERLAP", f"{name} 超出图框"))
        for i, (n1, r1) in enumerate(rects):
            for n2, r2 in rects[i + 1:]:
                if _overlap(r1, r2):
                    errors.append(_e("E_VIEW_OVERLAP", f"{n1} 与 {n2} 重叠"))
        for v in rec.get("viewports", []):
            if not LYT.is_standard_scale(v["scale"]):
                errors.append(_e("E_SCALE_NONSTANDARD", f"视口 {v['id']} 的比例 {v['scale']} 不在 GB/T 14690 系列里"))
        # ---- 字高
        if d is not None:
            std = STD.load("gb_general")
            dens = {"M": rec.get("den_main", 1.0), "O": rec.get("den_end", 1.0), "L": rec.get("den_end", 1.0)}
            bad = []
            for e in d.entities:
                if e.kind != "text" or "equip:auto" not in e.tags:
                    continue
                h = float(e.data.get("height", 0))
                if e.space == "Model":
                    view = next((t.split(":")[1] for t in e.tags if t.startswith("equip_view:")), "M")
                    h = h / float(dens.get(view, 1.0))
                if h < std.min_height(e.data.get("text", "")) - 1e-6:
                    bad.append(f"{e.id}（{fmt_value(h)} mm）")
            if bad:
                errors.append(_e("E_TEXT_HEIGHT", f"{len(bad)} 处文字字高低于 GB/T 14691 的最小值：" + "、".join(bad[:5])))
        if rec.get("unverified"):
            warnings.append(_e("W_UNVERIFIED_TABLE", "用到了未核对的标准数据（图上技术要求已注明）：" + "、".join(rec["unverified"][:6])))
    if any(v.startswith("rule:") for v in spec.sources.values()):
        warnings.append(_e("W_DEFAULT_USED", "有参数按默认规则取值，待确认（管口表备注①、技术要求）"))
    for w in spec.warnings:
        code = w.split("：", 1)[0] if w.startswith("W_") else "W_NOTE"
        warnings.append(_e(code, w))
    return {"errors": errors, "warnings": warnings}
