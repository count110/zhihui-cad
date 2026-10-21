"""命令快速通道（离线，不经过大模型）：AutoCAD 风格命令和常见中文口令 → 函数调用。

匹配不上返回 None，交给 Planner；写法不全（缺参数）抛 CmdError，带一句怎么写。
支持：L C A REC POL PL PO H T DLI DAL DRA DDI DAN O F CHA TR EX M CO RO SC MI ARR ARP E X LA LTS Z P U REDO
ORTHO SNAP GRID POLAR OSNAP OTRACK DYN LIMITS LIST EXT REV VIEW NEW SAVE HELP；以及“打开正交”“把当前图层设为中心线”“放大”“全部显示”“撤销”等中文模板。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


class CmdError(Exception):
    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.message, self.hint = message, hint


@dataclass
class Cmd:
    calls: list[dict]
    summary: str
    help: str = ""
    name: str = ""

    def as_call(self) -> dict:
        """多个调用合成一个批量（只落一个版本）；单个调用原样返回。"""
        if len(self.calls) == 1:
            return self.calls[0]
        return {"tool": "cad_batch_execute", "args": {"transaction_name": self.summary[:30], "steps": self.calls}}


_NUM = r"-?\d+(?:\.\d+)?"
_COORD = re.compile(rf"^(?:@)?{_NUM},{_NUM}(?:,{_NUM})?$|^(?:@)?{_NUM}<{_NUM}$|^\$LAST_POINT$|^[A-Za-z]+\d+\.[A-Za-z_]\w*(?:\([^)]*\))?$|^(?:int|aint|per|tan|nea|ext|m2p)\(.*\)$", re.I)
_ENT = re.compile(r"^(?:PT|ML|RY|XL|MS|TL|LD|PL|SP|EL|MT|[LCABDHT])\d+$", re.I)
_DIR = {"right": "right", "left": "left", "up": "up", "down": "down", "向右": "right", "向左": "left", "向上": "up", "向下": "down", "右": "right", "左": "left", "上": "up", "下": "down"}
COLORS = {"红": 1, "红色": 1, "red": 1, "黄": 2, "黄色": 2, "yellow": 2, "绿": 3, "绿色": 3, "green": 3, "青": 4, "青色": 4, "cyan": 4, "蓝": 5, "蓝色": 5, "blue": 5,
          "洋红": 6, "品红": 6, "magenta": 6, "白": 7, "白色": 7, "white": 7, "灰": 8, "灰色": 8, "黑": 7, "黑色": 7}


def is_point(tok: str) -> bool:
    return bool(_COORD.match(tok)) or bool(re.fullmatch(rf"(?:right|left|up|down)\s+{_NUM}", tok, re.I))


def is_ent(tok: str) -> bool:
    return bool(_ENT.match(tok)) and not is_point(tok)


def _num(tok: str, what: str) -> float:
    try:
        return float(tok)
    except (TypeError, ValueError):
        raise CmdError(f"{what}必须是数字：{tok!r}", "")


def _tokens(text: str) -> list[str]:
    toks = text.replace("，", " ").replace("；", " ").split()
    out: list[str] = []
    i = 0
    while i < len(toks):
        t = toks[i]
        if t.lower() in _DIR and i + 1 < len(toks) and re.fullmatch(_NUM, toks[i + 1]):
            out.append(f"{_DIR[t.lower()]} {toks[i + 1]}")
            i += 2
            continue
        out.append(t)
        i += 1
    return out


def _call(tool: str, **args) -> dict:
    return {"tool": tool, "args": args}


def _pts(toks: list[str]) -> tuple[list[str], list[str]]:
    pts = []
    i = 0
    while i < len(toks) and is_point(toks[i]):
        pts.append(toks[i])
        i += 1
    return pts, toks[i:]


# ------------------------------------------------------------------ AutoCAD 风格命令
def _cmd_line(a, raw):
    close = bool(a) and a[-1].upper() in ("C", "CLOSE")
    pts, rest = _pts(a[:-1] if close else a)
    if len(pts) < 2 and not (close and len(pts) >= 2):
        raise CmdError("直线至少需要两个点", "写法：L 0,0 @100,0 @0,50 C（最后的 C 表示闭合）")
    if rest:
        raise CmdError(f"看不懂 {rest[0]}", "写法：L 0,0 @100,0 @0,50 C")
    return Cmd([_call("draw_linear_primitives", primitive_type="line", points=pts, close_loop=close)], f"画直线 {len(pts)} 点" + ("并闭合" if close else ""))


def _cmd_pline(a, raw):
    close = bool(a) and a[-1].upper() in ("C", "CLOSE")
    pts, rest = _pts(a[:-1] if close else a)
    if len(pts) < 2 or rest:
        raise CmdError("多段线至少需要两个点", "写法：PL 0,0 @50,0 @0,30 C")
    return Cmd([_call("draw_complex_curves", curve_type="lwpolyline", vertices=pts, closed=close)], f"画多段线 {len(pts)} 点")


def _cmd_circle(a, raw):
    if not a:
        raise CmdError("圆需要圆心和半径", "写法：C 50,50 20 或 C 50,50 D 30 或 C 2P p1 p2 或 C 3P p1 p2 p3")
    k = a[0].upper()
    if k in ("2P", "3P"):
        pts = a[1:]
        n = int(k[0])
        if len(pts) != n or not all(is_point(p) for p in pts):
            raise CmdError(f"{k} 画圆需要 {n} 个点", f"写法：C {k} " + " ".join(f"p{i + 1}" for i in range(n)))
        return Cmd([_call("draw_circular_primitives", curve_type="circle", method=k.lower(), points=pts)], f"{k} 画圆")
    if k == "TTR":
        if len(a) != 4 or not (is_ent(a[1]) and is_ent(a[2])):
            raise CmdError("相切相切半径画圆需要两个图元和半径", "写法：C TTR L1 L2 10")
        return Cmd([_call("draw_circular_primitives", curve_type="circle", method="ttr", tangent_entities=[a[1], a[2]], radius=_num(a[3], "半径"))], "TTR 画圆")
    if not is_point(a[0]) or len(a) < 2:
        raise CmdError("圆需要圆心和半径", "写法：C 50,50 20 或 C 50,50 D 30")
    if a[1].upper() in ("D", "DIA") and len(a) == 3:
        return Cmd([_call("draw_circular_primitives", curve_type="circle", method="center_diameter", center=a[0], diameter=_num(a[2], "直径"))], f"画圆 ⌀{a[2]}")
    if len(a) == 2:
        return Cmd([_call("draw_circular_primitives", curve_type="circle", method="center_radius", center=a[0], radius=_num(a[1], "半径"))], f"画圆 R{a[1]}")
    raise CmdError("圆的写法不对", "C 50,50 20 或 C 50,50 D 30")


def _cmd_arc(a, raw):
    if len(a) == 3 and all(is_point(p) for p in a):
        return Cmd([_call("draw_circular_primitives", curve_type="arc", method="3p", points=a)], "三点画弧")
    raise CmdError("圆弧默认用三点画", "写法：A 起点 中间点 终点，例如 A 0,0 10,10 20,0")


def _cmd_rect(a, raw):
    pts, rest = _pts(a)
    if len(pts) != 2 or rest:
        raise CmdError("矩形需要两个角点", "写法：REC 0,0 @100,60（第二点可以是相对坐标）")
    return Cmd([_call("draw_polygons_and_meshes", shape_type="rectangle", corner1=pts[0], corner2=pts[1])], "画矩形")


def _cmd_poly(a, raw):
    if len(a) != 4 or a[2].upper() not in ("I", "C") or not is_point(a[1]):
        raise CmdError("正多边形需要边数、中心、内接/外切和半径", "写法：POL 6 0,0 I 50（I 内接于圆，C 外切于圆）")
    n = int(_num(a[0], "边数"))
    return Cmd([_call("draw_polygons_and_meshes", shape_type="polygon", num_sides=n, center=a[1], radius=_num(a[3], "半径"), inscribed=a[2].upper() == "I")], f"画正 {n} 边形")


def _cmd_point(a, raw):
    pts, _ = _pts(a)
    if not pts:
        raise CmdError("点需要坐标", "写法：PO 10,10")
    return Cmd([_call("draw_linear_primitives", primitive_type="point", points=pts)], "画点")


def _cmd_hatch(a, raw):
    pat = "ANSI31"
    if a and not is_point(a[0]) and not is_ent(a[0]):
        pat, a = a[0].upper(), a[1:]
    scale = 1.0
    if len(a) == 2 and is_point(a[0]) and re.fullmatch(_NUM, a[1]):
        scale = float(a[1])
        a = a[:1]
    if len(a) == 1 and is_point(a[0]):
        return Cmd([_call("create_hatch", boundary_mode="pick_internal_point", pick_point=a[0], pattern_name=pat, scale=scale)], f"拾取点填充 {pat}")
    ents = [t for t in a if is_ent(t)]
    if ents and len(ents) == len(a):
        return Cmd([_call("create_hatch", boundary_mode="entity_ids", entity_ids=ents, pattern_name=pat, scale=scale)], f"按图元填充 {pat}")
    raise CmdError("填充需要边界内部的一点或闭合图元", "写法：H 50,50 或 H ANSI31 50,50 2 或 H C8 PL3")


def _cmd_text(a, raw):
    if len(a) < 3 or not is_point(a[0]) or not re.fullmatch(_NUM, a[1]):
        raise CmdError("文字需要位置、字高和内容", "写法：T 10,10 3.5 技术要求")
    return Cmd([_call("create_text_annotation", text_type="single_line", content=" ".join(a[2:]), insert_point=a[0], height=float(a[1]))], "写文字")


def _cmd_dim_linear(kind):
    def f(a, raw):
        pts, rest = _pts(a)
        if len(pts) not in (2, 3) or rest:
            raise CmdError("线性标注需要两个界线点（可再给一个尺寸线位置）", "写法：DLI 0,0 100,0 [50,20]")
        args = dict(dim_type=kind, ext_line1_point=pts[0], ext_line2_point=pts[1])
        if len(pts) == 3:
            args["dim_line_location"] = pts[2]
        return Cmd([_call("create_linear_and_angular_dim", **args)], "线性标注")
    return f


def _cmd_dim_radial(kind):
    def f(a, raw):
        if len(a) < 1 or not is_ent(a[0]):
            raise CmdError(f"{'半径' if kind == 'radius' else '直径'}标注需要一个圆或圆弧", f"写法：{'DRA' if kind == 'radius' else 'DDI'} C8")
        args = dict(dim_type=kind, target_entity_id=a[0].upper())
        if len(a) > 1 and is_point(a[1]):
            args["leader_point"] = a[1]
        return Cmd([_call("create_radial_and_chain_dim", **args)], f"{'半径' if kind == 'radius' else '直径'}标注 {a[0].upper()}")
    return f


def _cmd_dim_angle(a, raw):
    ents = [t.upper() for t in a if is_ent(t)]
    if len(ents) != 2:
        raise CmdError("角度标注需要两条直线", "写法：DAN L1 L2")
    return Cmd([_call("create_linear_and_angular_dim", dim_type="angular", angle_entity_ids=ents)], "角度标注")


def _cmd_offset(a, raw):
    if len(a) < 2 or not re.fullmatch(_NUM, a[0]) or not is_ent(a[1]):
        raise CmdError("偏移需要距离和图元", "写法：O 10 C8 外（外/内/左/右，或给一个侧边点）")
    args = dict(operation="offset", entity_ids=[a[1].upper()], offset_distance=float(a[0]))
    side = a[2] if len(a) > 2 else None
    if side is None:
        raise CmdError("偏移需要说明偏移到哪一侧", "加上 外/内/左/右，或一个点：O 10 C8 外")
    m = {"外": "outside", "out": "outside", "outside": "outside", "o": "outside", "内": "inside", "in": "inside", "inside": "inside", "i": "inside", "左": "left", "left": "left", "l": "left", "右": "right", "right": "right", "r": "right"}
    if side.lower() in m:
        args["offset_side"] = m[side.lower()]
    elif is_point(side):
        args["offset_side_point"] = side
    else:
        raise CmdError(f"看不懂偏移方向 {side}", "外/内/左/右 或一个点")
    return Cmd([_call("replicate_entities", **args)], f"偏移 {a[1].upper()} {a[0]}")


def _cmd_fillet(a, raw):
    m = re.fullmatch(r"[Rr]?(-?\d+(?:\.\d+)?)", a[0]) if a else None
    if not m:
        raise CmdError("圆角需要半径", "写法：F R5 L1 L2（两条线）或 F R5 PL6（多段线所有拐角）")
    r = float(m.group(1))
    ents = [t.upper() for t in a[1:] if is_ent(t)]
    if len(ents) == 1:
        return Cmd([_call("modify_corners_and_breaks", operation="fillet", entity_id_1=ents[0], fillet_radius=r, apply_to_entire_polyline=True)], f"多段线圆角 R{r:g}")
    if len(ents) == 2:
        return Cmd([_call("modify_corners_and_breaks", operation="fillet", entity_id_1=ents[0], entity_id_2=ents[1], fillet_radius=r)], f"圆角 R{r:g}")
    raise CmdError("圆角需要一条多段线或两个图元", "F R5 L1 L2")


def _cmd_chamfer(a, raw):
    if not a or not re.fullmatch(rf"{_NUM}(?:,{_NUM})?", a[0]):
        raise CmdError("倒角需要距离", "写法：CHA 5 L1 L2 或 CHA 5,3 L1 L2")
    ds = [float(x) for x in a[0].split(",")]
    ents = [t.upper() for t in a[1:] if is_ent(t)]
    args = dict(operation="chamfer", chamfer_dist1=ds[0], chamfer_dist2=ds[-1])
    if len(ents) == 1:
        return Cmd([_call("modify_corners_and_breaks", entity_id_1=ents[0], apply_to_entire_polyline=True, **args)], "多段线倒角")
    if len(ents) == 2:
        return Cmd([_call("modify_corners_and_breaks", entity_id_1=ents[0], entity_id_2=ents[1], **args)], "倒角")
    raise CmdError("倒角需要一条多段线或两个图元", "CHA 5 L1 L2")


def _cmd_trim(op):
    def f(a, raw):
        ents = [t.upper() for t in a if is_ent(t)]
        pts = [t for t in a if is_point(t)]
        if not ents:
            raise CmdError(f"{'修剪' if op == 'trim' else '延伸'}需要目标图元", f"写法：{'TR' if op == 'trim' else 'EX'} L3 [拾取点]")
        tgt = [{"entity_id": ents[0]}]
        if pts:
            tgt[0]["pick_point"] = pts[0]
        else:
            tgt[0]["pick_point"] = f"{ents[0]}.mid"
        bnd = ents[1:] or None
        return Cmd([_call("adjust_line_lengths", operation=op, target_entities=tgt, boundary_entity_ids=bnd)], f"{'修剪' if op == 'trim' else '延伸'} {ents[0]}" + ("（拾取点默认取中点）" if not pts else ""))
    return f


def _split_ents(a):
    ents = []
    i = 0
    while i < len(a) and is_ent(a[i]):
        ents.append(a[i].upper())
        i += 1
    return ents, a[i:]


def _cmd_move(a, raw):
    ents, rest = _split_ents(a)
    if not ents:
        raise CmdError("移动需要图元", "写法：M C8 @10,0 或 M C8 0,0 20,10")
    if len(rest) == 1 and rest[0].startswith("@"):
        return Cmd([_call("transform_entities", operation="move", entity_ids=ents, displacement=rest[0])], "移动")
    if len(rest) == 2 and all(is_point(p) for p in rest):
        return Cmd([_call("transform_entities", operation="move", entity_ids=ents, base_point=rest[0], target_point=rest[1])], "移动")
    raise CmdError("移动需要位移（@dx,dy）或基点+目标点", "M C8 @10,0")


def _cmd_copy(a, raw):
    ents, rest = _split_ents(a)
    if not ents:
        raise CmdError("复制需要图元", "写法：CO C8 0,0 @10,0 @20,0")
    if len(rest) >= 2 and all(is_point(p) for p in rest):
        return Cmd([_call("replicate_entities", operation="copy", entity_ids=ents, base_point=rest[0], target_points=rest[1:])], "复制")
    raise CmdError("复制需要基点和至少一个目标点", "CO C8 0,0 @10,0")


def _cmd_rotate(a, raw):
    ents, rest = _split_ents(a)
    if not ents or len(rest) != 2 or not is_point(rest[0]) or not re.fullmatch(_NUM, rest[1]):
        raise CmdError("旋转需要图元、基点和角度", "写法：RO PL6 0,0 30")
    return Cmd([_call("transform_entities", operation="rotate", entity_ids=ents, base_point=rest[0], rotation_angle_deg=float(rest[1]))], f"旋转 {rest[1]}°")


def _cmd_scale(a, raw):
    ents, rest = _split_ents(a)
    if not ents or len(rest) != 2 or not is_point(rest[0]) or not re.fullmatch(_NUM, rest[1]):
        raise CmdError("缩放需要图元、基点和比例", "写法：SC C8 0,0 2")
    return Cmd([_call("transform_entities", operation="scale", entity_ids=ents, base_point=rest[0], scale_factor=float(rest[1]))], f"缩放 {rest[1]} 倍")


def _cmd_mirror(a, raw):
    ents, rest = _split_ents(a)
    erase = bool(rest) and rest[-1].upper() in ("Y", "YES")
    rest = rest[:-1] if erase or (rest and rest[-1].upper() in ("N", "NO")) else rest
    if not ents or len(rest) != 2 or not all(is_point(p) for p in rest):
        raise CmdError("镜像需要图元和镜像线两点", "写法：MI C8 0,0 10,0 [Y 删除源对象]")
    return Cmd([_call("replicate_entities", operation="mirror", entity_ids=ents, mirror_line=rest, erase_source=erase)], "镜像")


def _cmd_array_rect(a, raw):
    ents, rest = _split_ents(a)
    if not ents or len(rest) != 4:
        raise CmdError("矩形阵列需要图元、行数、列数、行距、列距", "写法：ARR L1 3 4 20 30")
    r, c, rs, cs = (float(x) for x in rest)
    return Cmd([_call("replicate_entities", operation="array_rect", entity_ids=ents, array_params={"rows": int(r), "cols": int(c), "row_spacing": rs, "col_spacing": cs})], f"{int(r)}×{int(c)} 矩形阵列")


def _cmd_array_polar(a, raw):
    ents, rest = _split_ents(a)
    if not ents or len(rest) not in (2, 3) or not is_point(rest[0]):
        raise CmdError("环形阵列需要图元、中心、数量（可加填充角度）", "写法：ARP C8 0,0 6 360")
    ap = {"center": rest[0], "count": int(float(rest[1])), "fill_angle_deg": float(rest[2]) if len(rest) == 3 else 360.0, "rotate_items": True}
    return Cmd([_call("replicate_entities", operation="array_polar", entity_ids=ents, array_params=ap)], f"环形阵列 {ap['count']} 个")


def _cmd_erase(a, raw):
    ents = [t.upper() for t in a if is_ent(t)]
    if not ents or len(ents) != len(a):
        raise CmdError("删除需要图元编号", "写法：E L3 C8")
    return Cmd([_call("erase_entities", entity_ids=ents)], f"删除 {len(ents)} 个")


def _cmd_explode(a, raw):
    ents = [t.upper() for t in a if is_ent(t)]
    if not ents:
        raise CmdError("分解需要图元编号", "写法：X PL6")
    return Cmd([_call("explode_compound_entities", entity_ids=ents)], "分解")


def _color(tok: str):
    if tok in COLORS or tok.lower() in COLORS:
        return COLORS.get(tok, COLORS.get(tok.lower()))
    if re.fullmatch(r"\d+", tok):
        return int(tok)
    if re.fullmatch(r"#[0-9a-fA-F]{6}", tok):
        return tok.upper()
    return None


def _cmd_layer(a, raw):
    if not a:
        return Cmd([_call("manage_layers", action="list")], "列出图层")
    if a[0] in ("新建", "N", "NEW", "n", "new"):
        if len(a) < 2:
            raise CmdError("新建图层需要名称", "写法：LA 新建 中心线 红 CENTER")
        name, rest = a[1], a[2:]
        props: dict = {}
        for t in rest:
            c = _color(t)
            if c is not None and "color" not in props:
                props["color"] = c
            elif re.fullmatch(r"[A-Za-z_]+", t):
                props["linetype"] = t.upper()
            elif re.fullmatch(rf"{_NUM}mm", t):
                props["lineweight_mm"] = float(t[:-2])
            else:
                raise CmdError(f"看不懂 {t}", "颜色（红/黄/绿/青/蓝/洋红/白 或数字）、线型（CENTER/DASHED…）、线宽（0.5mm）")
        calls = [_call("manage_layers", action="create", layer_name=name)]
        if props:
            calls.append(_call("set_layer_properties", layer_name=name, **props))
        return Cmd(calls, f"新建图层 {name}")
    name = a[0]
    act = a[1] if len(a) > 1 else "设为当前"
    states = {"设为当前": dict(set_current=True), "当前": dict(set_current=True), "S": dict(set_current=True), "SET": dict(set_current=True), "关": dict(is_on=False), "OFF": dict(is_on=False), "开": dict(is_on=True), "ON": dict(is_on=True),
              "冻结": dict(is_frozen=True), "F": dict(is_frozen=True), "解冻": dict(is_frozen=False), "T": dict(is_frozen=False), "锁定": dict(is_locked=True), "LOCK": dict(is_locked=True), "解锁": dict(is_locked=False), "UNLOCK": dict(is_locked=False)}
    if act.upper() in states or act in states:
        return Cmd([_call("set_layer_state", layer_name=name, **states.get(act, states.get(act.upper())))], f"图层 {name} {act}")
    raise CmdError(f"看不懂图层操作 {act}", "LA 新建 名称 [颜色] [线型]；LA 名称 设为当前/关/开/冻结/解冻/锁定/解锁")


def _cmd_lts(a, raw):
    if not a:
        return Cmd([_call("set_linetype_scale", auto=True)], "线型比例自动")
    if a[0].upper() == "AUTO":
        return Cmd([_call("set_linetype_scale", auto=True)], "线型比例自动")
    return Cmd([_call("set_linetype_scale", ltscale=_num(a[0], "线型比例"))], f"线型比例 {a[0]}")


def _cmd_zoom(a, raw):
    k = (a[0].upper() if a else "E")
    if k in ("E", "EXTENTS"):
        return Cmd([_call("control_viewport_view", action="zoom_extents")], "范围缩放")
    if k in ("A", "ALL"):
        return Cmd([_call("control_viewport_view", action="zoom_all")], "全部缩放")
    if k in ("P", "PREVIOUS"):
        return Cmd([_call("control_viewport_view", action="zoom_previous")], "返回上一视图")
    if k in ("W", "WINDOW") and len(a) == 3:
        return Cmd([_call("control_viewport_view", action="zoom_window", window_min=a[1], window_max=a[2])], "窗口缩放")
    if k in ("O", "OBJECT"):
        ents = [t.upper() for t in a[1:] if is_ent(t)]
        if ents:
            return Cmd([_call("control_viewport_view", action="zoom_object", target_entity_ids=ents)], "缩放到对象")
    m = re.fullmatch(rf"({_NUM})(?:X)?", k)
    if m:
        return Cmd([_call("control_viewport_view", action="zoom_scale", scale_factor=float(m.group(1)))], f"缩放 {m.group(1)} 倍")
    raise CmdError("缩放写法不对", "Z E 范围；Z A 全部；Z W p1 p2 窗口；Z 2 放大 2 倍；Z P 上一视图；Z O C8 对象")


def _cmd_pan(a, raw):
    if len(a) == 1 and is_point(a[0]):
        return Cmd([_call("control_viewport_view", action="pan", pan_delta=a[0])], "平移")
    raise CmdError("平移需要位移", "写法：P @100,0")


def _cmd_undo(a, raw):
    n = int(float(a[0])) if a and re.fullmatch(r"\d+", a[0]) else 1
    return Cmd([_call("history_undo_redo", action="undo", steps=n)], f"撤销 {n} 步")


def _cmd_redo(a, raw):
    n = int(float(a[0])) if a and re.fullmatch(r"\d+", a[0]) else 1
    return Cmd([_call("history_undo_redo", action="redo", steps=n)], f"重做 {n} 步")


def _onoff(tok: str | None) -> bool:
    if tok is None:
        raise CmdError("需要 ON 或 OFF", "例如：ORTHO ON")
    t = tok.upper()
    if t in ("ON", "开", "打开", "1", "开启"):
        return True
    if t in ("OFF", "关", "关闭", "0"):
        return False
    raise CmdError(f"看不懂 {tok}", "ON 或 OFF")


def _mode(tool, key, label):
    def f(a, raw):
        return Cmd([_call(tool, **{key: _onoff(a[0] if a else None)})], f"{label}{'开' if _onoff(a[0]) else '关'}")
    return f


def _cmd_polar(a, raw):
    args: dict = {"polar_on": _onoff(a[0] if a else None)}
    if len(a) > 1:
        args["polar_angle_deg"] = _num(a[1], "极轴角度")
    return Cmd([_call("configure_drafting_modes", **args)], f"极轴{'开' if args['polar_on'] else '关'}")


def _cmd_limits(a, raw):
    pts, _ = _pts(a)
    if len(pts) != 2:
        raise CmdError("图形界限需要左下角和右上角", "LIMITS 0,0 420,297")
    return Cmd([_call("configure_canvas_and_limits", limits_lower_left=pts[0], limits_upper_right=pts[1])], "设置图形界限")


def _cmd_list(a, raw):
    ents = [t.upper() for t in a if is_ent(t)]
    if not ents:
        raise CmdError("LIST 需要图元编号", "LIST C8")
    return Cmd([_call("modify_entity_properties", action="get", entity_ids=ents)], "查看特性")


def _cmd_extrude(a, raw):
    ents, rest = _split_ents(a)
    if not ents or len(rest) != 1 or not re.fullmatch(_NUM, rest[0]):
        raise CmdError("拉伸需要闭合轮廓和高度", "写法：EXT PL1 C2 30")
    return Cmd([_call("extrude_2d_to_3d", profile_entity_ids=ents, height=float(rest[0]))], f"拉伸 {rest[0]}")


def _cmd_view(a, raw):
    m = {"TOP": "top", "BOTTOM": "bottom", "FRONT": "front", "BACK": "back", "LEFT": "left", "RIGHT": "right", "SW": "sw_isometric", "SE": "se_isometric", "NE": "ne_isometric", "NW": "nw_isometric"}
    if not a or a[0].upper() not in m:
        raise CmdError("视图方向不对", "VIEW TOP/BOTTOM/FRONT/BACK/LEFT/RIGHT/SW/SE/NE/NW")
    return Cmd([_call("switch_3d_view_and_visual_style", standard_view=m[a[0].upper()])], f"切换到 {a[0].upper()} 视图")


def _cmd_new(a, raw):
    return Cmd([_call("manage_document_file", action="new", template_name=(a[0] if a else "gb_a3_mech"))], "新建图纸")


def _cmd_save(a, raw):
    return Cmd([_call("manage_document_file", action="save", format="dxf")], "保存 DXF")


def _cmd_help(a, raw):
    return Cmd([], "命令帮助", help=HELP_TEXT)


HELP_TEXT = """常用命令（空格分隔，坐标写 x,y 或 @相对 或 长<角度）：
L 0,0 @100,0 @0,50 C          直线（C 闭合）        PL 0,0 @50,0 @0,30   多段线
C 50,50 20 / C 50,50 D 30      圆（半径/直径）        A p1 p2 p3             三点圆弧
REC 0,0 @100,60                矩形                   POL 6 0,0 I 50         正多边形（I 内接）
O 10 C8 外                      偏移（外/内/左/右）    F R5 L1 L2 / F R5 PL6  圆角
CHA 5 L1 L2                     倒角                   TR L3 / EX L3          修剪 / 延伸
M C8 @10,0                      移动                   CO C8 0,0 @10,0        复制
RO PL6 0,0 30                   旋转                   SC C8 0,0 2            缩放
MI C8 0,0 10,0                  镜像                   ARR L1 3 4 20 30       矩形阵列
ARP C8 0,0 6 360                环形阵列               E L3 / X PL6           删除 / 分解
LA 新建 中心线 红 CENTER        新建图层               LA 中心线 设为当前     图层状态
LTS 2                           线型比例（LTS 自动）   Z E / Z A / Z W p1 p2  缩放
U / REDO                        撤销 / 重做            ORTHO ON / GRID OFF    绘图辅助
中文口令：打开正交、关闭栅格、把当前图层设为中心线、放大、缩小、全部显示、撤销、重做"""

ALIASES = {
    "L": _cmd_line, "LINE": _cmd_line, "PL": _cmd_pline, "PLINE": _cmd_pline, "C": _cmd_circle, "CIRCLE": _cmd_circle, "A": _cmd_arc, "ARC": _cmd_arc,
    "REC": _cmd_rect, "RECTANG": _cmd_rect, "RECTANGLE": _cmd_rect, "POL": _cmd_poly, "POLYGON": _cmd_poly, "PO": _cmd_point, "POINT": _cmd_point,
    "H": _cmd_hatch, "HATCH": _cmd_hatch, "T": _cmd_text, "TEXT": _cmd_text, "DT": _cmd_text,
    "DLI": _cmd_dim_linear("linear_auto"), "DIMLINEAR": _cmd_dim_linear("linear_auto"), "DAL": _cmd_dim_linear("aligned"), "DIMALIGNED": _cmd_dim_linear("aligned"),
    "DRA": _cmd_dim_radial("radius"), "DIMRADIUS": _cmd_dim_radial("radius"), "DDI": _cmd_dim_radial("diameter"), "DIMDIAMETER": _cmd_dim_radial("diameter"), "DAN": _cmd_dim_angle, "DIMANGULAR": _cmd_dim_angle,
    "O": _cmd_offset, "OFFSET": _cmd_offset, "F": _cmd_fillet, "FILLET": _cmd_fillet, "CHA": _cmd_chamfer, "CHAMFER": _cmd_chamfer,
    "TR": _cmd_trim("trim"), "TRIM": _cmd_trim("trim"), "EX": _cmd_trim("extend"), "EXTEND": _cmd_trim("extend"),
    "M": _cmd_move, "MOVE": _cmd_move, "CO": _cmd_copy, "COPY": _cmd_copy, "RO": _cmd_rotate, "ROTATE": _cmd_rotate, "SC": _cmd_scale, "SCALE": _cmd_scale,
    "MI": _cmd_mirror, "MIRROR": _cmd_mirror, "ARR": _cmd_array_rect, "ARRAYRECT": _cmd_array_rect, "ARP": _cmd_array_polar, "ARRAYPOLAR": _cmd_array_polar,
    "E": _cmd_erase, "ERASE": _cmd_erase, "DEL": _cmd_erase, "X": _cmd_explode, "EXPLODE": _cmd_explode,
    "LA": _cmd_layer, "LAYER": _cmd_layer, "LTS": _cmd_lts, "LTSCALE": _cmd_lts, "Z": _cmd_zoom, "ZOOM": _cmd_zoom, "P": _cmd_pan, "PAN": _cmd_pan,
    "U": _cmd_undo, "UNDO": _cmd_undo, "REDO": _cmd_redo, "MREDO": _cmd_redo,
    "ORTHO": _mode("configure_drafting_modes", "ortho_on", "正交"), "SNAP": _mode("configure_drafting_modes", "snap_on", "捕捉"), "GRID": _mode("configure_drafting_modes", "grid_on", "栅格"),
    "POLAR": _cmd_polar, "OSNAP": _mode("configure_osnap_and_otracking", "osnap_on", "对象捕捉"), "OTRACK": _mode("configure_osnap_and_otracking", "otrack_on", "对象追踪"), "DYN": _mode("configure_dynamic_input_and_coords", "dynmode_on", "动态输入"),
    "LIMITS": _cmd_limits, "LIST": _cmd_list, "EXT": _cmd_extrude, "EXTRUDE": _cmd_extrude, "VIEW": _cmd_view, "NEW": _cmd_new, "SAVE": _cmd_save, "QSAVE": _cmd_save, "HELP": _cmd_help, "?": _cmd_help,
}

# ------------------------------------------------------------------ 中文模板
_SWITCH = {"正交": ("configure_drafting_modes", "ortho_on"), "栅格": ("configure_drafting_modes", "grid_on"), "捕捉": ("configure_drafting_modes", "snap_on"), "极轴": ("configure_drafting_modes", "polar_on"),
           "对象捕捉": ("configure_osnap_and_otracking", "osnap_on"), "对象追踪": ("configure_osnap_and_otracking", "otrack_on"), "动态输入": ("configure_dynamic_input_and_coords", "dynmode_on")}
_ON = r"(?:打开|开启|开|启用)"
_OFF = r"(?:关闭|关掉|关|禁用)"


def _cn_switches(text: str) -> Cmd | None:
    t = text.strip("。 ")
    m = re.fullmatch(rf"({_ON}|{_OFF})\s*((?:正交|栅格|捕捉|极轴|对象捕捉|对象追踪|动态输入)(?:\s*[和与及、,，]\s*(?:正交|栅格|捕捉|极轴|对象捕捉|对象追踪|动态输入))*)", t)
    if not m:
        return None
    on = bool(re.fullmatch(_ON, m.group(1)))
    names = re.findall(r"对象捕捉|对象追踪|动态输入|正交|栅格|捕捉|极轴", m.group(2))
    calls = []
    for n in names:
        tool, key = _SWITCH[n]
        calls.append(_call(tool, **{key: on}))
    return Cmd(calls, f"{'打开' if on else '关闭'}{'、'.join(names)}")


def _cn_templates(text: str) -> Cmd | None:
    t = text.strip("。 ！!")
    c = _cn_switches(t)
    if c:
        return c
    m = re.fullmatch(r"(?:把)?当前图层(?:设为|设置为|切换到|改为)\s*[「\"“]?([^\s「」\"”]+?)(?:图层|层)?[」\"”]?", t) or re.fullmatch(r"(?:切换到|切到)\s*([^\s]+?)\s*图层", t) or re.fullmatch(r"设\s*([^\s]+?)\s*为当前(?:图层)?", t)
    if m:
        return Cmd([_call("set_layer_state", layer_name=m.group(1), set_current=True)], f"当前图层设为 {m.group(1)}")
    if re.fullmatch(r"(?:全部显示|显示全部|范围缩放|缩放到全部|显示所有|全图显示)", t):
        return Cmd([_call("control_viewport_view", action="zoom_extents")], "全部显示")
    m = re.fullmatch(r"放大(?:\s*(\d+(?:\.\d+)?)\s*倍)?", t)
    if m:
        return Cmd([_call("control_viewport_view", action="zoom_scale", scale_factor=float(m.group(1))) if m.group(1) else _call("control_viewport_view", action="zoom_in")], "放大")
    m = re.fullmatch(r"缩小(?:\s*(\d+(?:\.\d+)?)\s*倍)?", t)
    if m:
        return Cmd([_call("control_viewport_view", action="zoom_scale", scale_factor=1 / float(m.group(1))) if m.group(1) else _call("control_viewport_view", action="zoom_out")], "缩小")
    m = re.fullmatch(r"(?:撤销|撤回|回退)(?:上[一1]步|刚才那步|[一1]步)?(?:\s*(\d+)\s*步)?", t) or re.fullmatch(r"刚才那步不要了|上一步不要了|刚才那个不要了", t)
    if m:
        n = int(m.group(1)) if m.lastindex and m.group(1) else 1
        return Cmd([_call("history_undo_redo", action="undo", steps=n)], f"撤销 {n} 步")
    if re.fullmatch(r"(?:重做|恢复|还原)(?:上一步)?", t):
        return Cmd([_call("history_undo_redo", action="redo", steps=1)], "重做")
    m = re.fullmatch(r"(?:调整|调一下|设置)?线型比例(?:设为|改为|为)?\s*(\d+(?:\.\d+)?)", t)
    if m:
        return Cmd([_call("set_linetype_scale", ltscale=float(m.group(1)))], f"线型比例 {m.group(1)}")
    m = re.fullmatch(r"画(?:一个)?半径(?:为|是)?\s*(\d+(?:\.\d+)?)\s*(?:mm)?的圆[，,]?\s*圆心(?:在|于)\s*(-?\d+(?:\.\d+)?,-?\d+(?:\.\d+)?)", t)
    if m:
        return Cmd([_call("draw_circular_primitives", curve_type="circle", method="center_radius", center=m.group(2), radius=float(m.group(1)))], f"画圆 R{m.group(1)}")
    m = re.fullmatch(r"画(?:一个)?直径(?:为|是)?\s*(\d+(?:\.\d+)?)\s*(?:mm)?的圆[，,]?\s*圆心(?:在|于)\s*(-?\d+(?:\.\d+)?,-?\d+(?:\.\d+)?)", t)
    if m:
        return Cmd([_call("draw_circular_primitives", curve_type="circle", method="center_diameter", center=m.group(2), diameter=float(m.group(1)))], f"画圆 ⌀{m.group(1)}")
    m = re.fullmatch(r"画(?:一条)?(?:直线|线)(?:从)\s*(-?\d+(?:\.\d+)?,-?\d+(?:\.\d+)?)\s*(?:到|至)\s*(@?-?\d+(?:\.\d+)?[,<]-?\d+(?:\.\d+)?)", t)
    if m:
        return Cmd([_call("draw_linear_primitives", primitive_type="line", points=[m.group(1), m.group(2)])], "画直线")
    if t in ("帮助", "命令帮助", "有哪些命令", "怎么用命令行"):
        return Cmd([], "命令帮助", help=HELP_TEXT)
    return None


# ------------------------------------------------------------------ 入口
def parse(text: str) -> Cmd | None:
    """能直接映射就返回 Cmd；不是命令（交给 Planner）返回 None；是命令但写法不全抛 CmdError。"""
    raw = re.sub(r"(?<![\d.第])1\s*(?=[张个条份套根段行])", "一", text.strip())     # 规范化把“一个”变成了“1个”，模板里按原来的说法写
    if not raw:
        return None
    cn = _cn_templates(raw)
    if cn is not None:
        return cn
    toks = _tokens(raw)
    if not toks:
        return None
    head = toks[0]
    fn = ALIASES.get(head.upper()) if re.fullmatch(r"[A-Za-z?]+", head) else None
    if fn is None:
        return None
    # 英文单词开头但后面是整句中文 → 不是命令（如 "C 语言…"），交给 Planner
    if any(re.search(r"[一-鿿]{3,}", t) for t in toks[1:]) and head.upper() not in ("LA", "LAYER", "T", "TEXT", "DT"):
        return None
    cmd = fn(toks[1:], raw)
    cmd.name = head.upper()
    return cmd
