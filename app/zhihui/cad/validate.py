"""validate_drawing 的规则。每条错误都带 error_code、涉及对象和一句给大模型看的修复提示。"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import asdict, dataclass, field

from zhihui.ir import Drawing

from .diff import changed_ids, signatures
from .routing import EPS, collect_segments, endpoint_ok, path_segments
from .symbols import boxes_overlap, footprint, get_symbol, label_box, load_symbols, port_point, symbol_box, usable_area


@dataclass
class Issue:
    code: str
    message: str
    refs: list[str] = field(default_factory=list)
    hint: str = ""


GEO_TOL = 0.01  # mm：坐标会四舍五入到 0.001 mm，端口恰好在边框上时不能算「穿过」


def _seg_hits_box(a, b, box) -> bool:
    x0, y0, x1, y1 = box
    t = GEO_TOL
    lo_x, hi_x = min(a[0], b[0]), max(a[0], b[0])
    lo_y, hi_y = min(a[1], b[1]), max(a[1], b[1])
    return lo_x < x1 - t and hi_x > x0 + t and lo_y < y1 - t and hi_y > y0 + t or (
        x0 + t < a[0] < x1 - t and y0 + t < a[1] < y1 - t
    )


def crossings(d: Drawing) -> int:
    segs = collect_segments(d.connections)
    n = 0
    for y, x0, x1 in segs.horizontal:
        for x, y0, y1 in segs.vertical:
            if x0 + EPS < x < x1 - EPS and y0 + EPS < y < y1 - EPS:
                n += 1
    return n


def validate(
    d: Drawing,
    profile: str = "generate",
    expected_changes: list[str] | None = None,
    base: Drawing | None = None,
) -> dict:
    errors: list[Issue] = []
    warnings: list[Issue] = []
    s = d.unit  # 图元尺度
    k = d.scale  # 图框尺度（真实图纸 mm）
    syms = load_symbols()
    tagmap = {e.id: e.tag for e in d.equipment}

    # 1. 位号唯一、图元存在、已布局
    for tag, n in Counter(e.tag.lower() for e in d.equipment).items():
        if n > 1:
            errors.append(Issue("E_TAG_DUP", f"位号 {tag} 重复 {n} 次", [tag], "用 edit_equipment 的 new_tag 改成唯一位号"))
    for e in d.equipment:
        if e.symbol_id not in syms:
            errors.append(Issue("E_SYMBOL_UNKNOWN", f"{e.tag} 的图元 {e.symbol_id} 不存在", [e.tag], f"可用图元：{sorted(syms)}"))
    unplaced = [e.tag for e in d.equipment if not e.placed]
    if unplaced:
        errors.append(Issue("E_UNPLACED", f"{len(unplaced)} 台设备还没有坐标", unplaced, "调用 auto_layout"))
    placed = [e for e in d.equipment if e.placed and e.symbol_id in syms]

    # 2. 重叠、超幅
    area = usable_area(d)
    fps = {e.id: footprint(e, s, d.template_id) for e in placed}
    for i, a in enumerate(placed):
        for b in placed[i + 1 :]:
            if boxes_overlap(fps[a.id], fps[b.id]):
                errors.append(
                    Issue("E_OVERLAP", f"{a.tag} 与 {b.tag} 重叠", [a.tag, b.tag], f"用 move_equipment 移开其中一台，或 auto_layout(mode='full') 重排")
                )
    tol = 0.5 * k
    for e in placed:
        x0, y0, x1, y1 = fps[e.id]
        if x0 < area[0] - tol or y0 < area[1] - tol or x1 > area[2] + tol or y1 > area[3] + tol:
            errors.append(
                Issue("E_OUT_OF_FRAME", f"{e.tag} 超出设备区（压到图框、上方位号栏或标题栏）", [e.tag], "移回设备区，或 auto_layout(paper_size='A2')")
            )

    # 3. 连接合法性
    eq = {e.id: e for e in d.equipment}
    used_ports: Counter = Counter()
    for c in d.connections:
        refs = [c.id]
        bad = False
        for eid, port in ((c.from_equip, c.from_port), (c.to_equip, c.to_port)):
            e = eq.get(eid)
            if e is None:
                errors.append(Issue("E_DANGLING", f"连接 {c.id} 指向不存在的设备 {eid}", refs, "用 connect_ports 的 remove 删除该连接"))
                bad = True
            elif e.symbol_id in syms and get_symbol(e.symbol_id).port(port) is None:
                ports = [p.name for p in get_symbol(e.symbol_id).ports]
                errors.append(Issue("E_PORT_INVALID", f"{e.tag} 没有端口 {port}", refs, f"{e.tag} 的端口有 {ports}"))
                bad = True
            else:
                used_ports[(eid, port)] += 1
        if bad:
            continue
        if c.from_equip == c.to_equip:
            errors.append(Issue("E_SELF_LOOP", f"连接 {c.id} 首尾是同一台设备", refs, "删除该连接"))
        if not c.path:
            if eq[c.from_equip].placed and eq[c.to_equip].placed:
                errors.append(Issue("E_UNROUTED", f"{tagmap[c.from_equip]}→{tagmap[c.to_equip]} 未能布线", refs, "auto_layout(spacing='loose') 或换 A2 图幅"))
            continue
        for a, b in path_segments(c.path):
            if abs(a[0] - b[0]) > EPS and abs(a[1] - b[1]) > EPS:
                errors.append(Issue("E_PIPE_NOT_ORTHOGONAL", f"管线 {c.id} 有斜线段", refs, "auto_layout 重新布线"))
                break
        (p0, _), (p1, _) = port_point(eq[c.from_equip], c.from_port, s), port_point(eq[c.to_equip], c.to_port, s)
        if not endpoint_ok(d, c, "from", p0, s) or not endpoint_ok(d, c, "to", p1, s):
            errors.append(Issue("E_PIPE_DETACHED", f"管线 {c.id} 端点没接在端口上", refs, "auto_layout 重新布线"))
        for e in placed:
            box = symbol_box(e, s)
            if any(_seg_hits_box(a, b, box) for a, b in path_segments(c.path)):
                errors.append(
                    Issue("E_PIPE_THROUGH_EQUIP", f"管线 {tagmap[c.from_equip]}→{tagmap[c.to_equip]} 穿过 {e.tag}", [c.id, e.tag], "移开挡路的设备后重新布线")
                )
            elif any(_seg_hits_box(a, b, label_box(e, s, d.template_id)) for a, b in path_segments(c.path)):
                warnings.append(Issue("W_PIPE_OVER_TEXT", f"管线 {c.id} 压到 {e.tag} 的文字", [c.id, e.tag]))
    for (eid, port), n in used_ports.items():
        if n > 1:
            warnings.append(Issue("W_PORT_SHARED", f"{tagmap.get(eid, eid)}.{port} 接了 {n} 条管线", [tagmap.get(eid, eid)]))

    connected = {c.from_equip for c in d.connections} | {c.to_equip for c in d.connections}
    for e in d.equipment:
        if e.id not in connected and len(d.equipment) > 1:
            warnings.append(Issue("W_ISOLATED", f"{e.tag} 没有任何连接", [e.tag], "确认是否漏了 connect_ports"))

    # 4. 改图：声明影响集之外不得有变化
    if profile == "modify" and base is not None:
        allowed: set[str] = set()
        for ref in expected_changes or []:
            for dd in (d, base):
                e = dd.get_equipment(ref)
                if e:
                    allowed.add(e.id)
            if d.get_connection(ref) or base.get_connection(ref):
                allowed.add(ref)
        for dd in (d, base):
            for c in dd.connections:
                if c.from_equip in allowed or c.to_equip in allowed:
                    allowed.add(c.id)
        unexpected = sorted(set(changed_ids(signatures(base), signatures(d))) - allowed)
        if unexpected:
            names = [tagmap.get(i, base.get_equipment(i).tag if base.get_equipment(i) else i) for i in unexpected]
            errors.append(
                Issue("E_UNEXPECTED_CHANGE", f"声明影响集之外的对象被改动：{names}", names, "撤销这些改动，或把它们加入 expected_changes 并向用户说明")
            )

    # 5. 填充：失去关联、压住文字（警告）
    if d.hatches:
        from shapely.geometry import box

        from .hatch_ir import Scene, region_from_rings

        lost = [h.id for h in d.hatches if h.disassociated]
        if lost:
            warnings.append(Issue("W_HATCH_DISASSOCIATED", f"填充 {lost} 的边界已求不出（失去关联），沿用上一次的边界", lost,
                                  "确认边界是否被改开了口；需要的话用 edit_hatch 改 gap_tol，或删掉重新 add_hatch"))
        if all(e.placed for e in d.equipment):
            texts = [box(*b) for b in Scene(d).texts]
            for h in d.hatches:
                r = region_from_rings(h)
                if r is not None and any(r.geometry.intersection(t).area > 1e-6 * max(t.area, 1.0) for t in texts):
                    warnings.append(Issue("W_HATCH_OVER_TEXT", f"填充 {h.id} 压住了文字", [h.id], "用 edit_hatch 微调或重新拾取，让剖面线避开文字"))

    # 6. 尺寸（GB/T 4458.4 五条）和标注关联（自由文字给警告）
    from .anchors import free_texts

    free = free_texts(d)
    if free:
        warnings.append(Issue("W_FREE_TEXT", f"标注 {free} 没有关联到对象，对象移动后不会跟着走", free,
                              "用 add_annotation 把文字锚在设备位号或管线 id 上"))
    if d.dimensions and all(e.placed for e in d.equipment):
        from .dims import check as check_dims
        from .dxf_io import build_doc

        for it in check_dims(build_doc(d), d):
            errors.append(Issue(it["code"], it["message"], it["refs"], "调整尺寸所在一侧（side），或删除重复的尺寸"))
    if d.equipment and all(e.placed for e in d.equipment):  # 7. 规范自检（S9）：错误项进入「校验 → 发回模型修复」循环
        from .standards_check import errors_for_validate, run as std_run

        for it in errors_for_validate(std_run(d)):
            if not it["code"].startswith("E_STD_DIM_"):
                errors.append(Issue(it["code"], it["message"], it["refs"], it["hint"]))
    if any(s.placement == "inline" for s in d.schedules) and all(e.placed for e in d.equipment):
        from .dxf_io import build_doc

        moved = build_doc(d).zh_overflow
        if moved:
            warnings.append(Issue("W_SCHEDULE_SHEET", f"{moved} 一览表本图放不下，导出时改为单独一页", moved,
                                  "需要放在本图就换大图幅；单独一页也符合要求"))

    stats = {
        "equipment": len(d.equipment),
        "connections": len(d.connections),
        "crossings": crossings(d),
        "bends": sum(max(len(c.path) - 2, 0) for c in d.connections if c.path),
    }
    return {
        "passed": not errors,
        "errors": [asdict(i) for i in errors],
        "warnings": [asdict(i) for i in warnings],
        "stats": stats,
    }
