"""主视图的尺寸链（任务书 5.1 dims.py）。

尺寸的定义点一律取**参数值算出的真实位置**（壁厚夸大只改图形），所以测量值 = 参数值，尺寸文字就是参数值，尺寸链按参数闭合：
总高/总长 = 下（左）封头总高 + 筒体长度 + 上（右）封头总高；筒体管口按基准线分段；管口伸出长度、封头管口高度 H 与径向位置、耳座高度、鞍座位置。
"""
from __future__ import annotations

from . import std_tables as T
from .rules import flange_dims
from .views import Builder, side_of


def dim_style_name(den: float) -> str:
    return f"EQ_1_{den:g}"                           # DXF 表名不能含冒号


def _dim(dtype: str, p1, p2, loc, style: str, text: str = "") -> dict:
    if dtype == "linear_vertical":
        meas = abs(p2[1] - p1[1])
    else:
        meas = abs(p2[0] - p1[0])
    return {"kind": "dimension", "role": "dim", "tags": [], "data": {"dtype": dtype, "p1": [p1[0], p1[1], 0.0], "p2": [p2[0], p2[1], 0.0], "loc": [loc[0], loc[1], 0.0],
                                                                    "text": text, "style": style, "measurement": round(meas, 6)}}


def main_view_dims(spec, view) -> tuple[list[dict], dict]:
    """返回 (ops, info)。info["chain"] 尺寸链各段的值、info["overall"] 总高/总长、info["expected"] 按参数算的总高/总长。"""
    den = view.den
    st = dim_style_name(den)
    info = view.info
    ri, ro, L = info["ri"], info["ro_true"], info["L"]
    bot, top = info["bottom_true"], info["top_true"]
    vert = spec.vertical
    b = Builder("M", None)
    gap = 10.0 * den
    step = 8.0 * den

    def M(u, v):                                         # 局部（轴向 u，径向 v）→ 模型
        return (v, u) if vert else (u, v)

    along = "linear_vertical" if vert else "linear_horizontal"
    across = "linear_horizontal" if vert else "linear_vertical"
    x0, y0, x1, y1 = view.bbox
    # ---- 总高 / 总长 与 分段链（立式在左侧，卧式在上方）
    shell_noz = sorted({round(n.axial_mm, 6) for n in spec.nozzles if n.location == "shell" and n.axial_mm is not None and n.mark})
    us = [bot, 0.0] + [u for u in shell_noz if 0.0 < u < L] + [L, top]
    if vert:
        v_line = x0 - gap
    else:
        v_line = y1 + gap
    chain_ids, chain_vals = [], []
    for i in range(len(us) - 1):
        ua, ub = us[i], us[i + 1]
        va = 0.0 if i == 0 else -ro if vert else ro
        vb = 0.0 if i == len(us) - 2 else -ro if vert else ro
        pa, pb = M(ua, va), M(ub, vb)
        loc = (v_line, (ua + ub) / 2) if vert else ((ua + ub) / 2, v_line)
        b.add("DIM-CHAIN", [_dim(along, pa, pb, loc, st)], local=False)
        chain_ids.append(b.ops[-1]["id"])
        chain_vals.append(round(ub - ua, 6))
    v2 = v_line - step if vert else v_line + step
    b.add("DIM-TOTAL", [_dim(along, M(bot, 0.0), M(top, 0.0), (v2, (bot + top) / 2) if vert else ((bot + top) / 2, v2), st)], local=False)
    overall_id = b.ops[-1]["id"]
    # ---- 内径
    u_di = L * 0.82
    p1, p2 = M(u_di, -ri), M(u_di, ri)
    b.add("DIM-DI", [_dim(across, p1, p2, M(u_di, 0.0), st, "Φ<>")], local=False)
    # ---- 壁厚 δ（文字 + 指引线；文字用参数值）
    u_t = L * 0.45
    q0 = M(u_t, -(ri + info["tw"] / 2))
    q1 = M(u_t + 4 * den, -(ri - 6 * den))
    b.add("DIM-T", [{"kind": "line", "role": "thin", "tags": ["leader"], "data": {"p1": [q0[0], q0[1], 0.0], "p2": [q1[0], q1[1], 0.0]}}], local=False)
    b.text("DIM-T", q1, f"δ={_f(spec.shell.thickness_mm)}", 3.5 * den, "left", local=False, tags=["thickness_label"])
    # ---- 管口：伸出长度、封头管口 H 与径向位置
    for n in spec.nozzles:
        if n.mark is None or n.projection_mm is None:
            continue
        sd = side_of(spec, n)
        D = flange_dims(n)["D"]
        part = f"DIM-NZ-{n.mark}"
        if n.location == "shell":
            pa, pb = M(n.axial_mm, sd * ro), M(n.axial_mm, sd * (ro + n.projection_mm))
            uloc = n.axial_mm + D / 2 + 2.5 * den
            loc = M(uloc, sd * (ro + n.projection_mm / 2))
            b.add(part, [_dim(across, pa, pb, loc, st)], local=False)
        else:
            pos = n.location in ("top_head", "right_head")
            dr = 1.0 if pos else -1.0
            us_ = L if pos else 0.0
            v = sd * float(n.radial_mm or 0.0)
            face = us_ + dr * n.projection_mm
            vloc = v + sd * (D / 2 + 3.0 * den) if v != 0 else D / 2 + 3.0 * den
            pa, pb = M(us_, v), M(face, v)
            loc = M((us_ + face) / 2, vloc)
            b.add(part, [_dim(along, pa, pb, loc, st)], local=False)
            if abs(v) > 1e-9:
                uu = face + dr * 3.0 * den
                b.add(part, [_dim(across, M(face, 0.0), M(face, v), M(uu, v / 2), st)], local=False)
    # ---- 支座
    if "lug_elev" in info:
        e = info["lug_elev"]

        def reach(sd: float) -> float:             # 这一侧在 [基准线, 耳座] 轴向范围内伸得最远的东西（耳座、管口法兰、人孔盖）到轴线的距离
            r = info["ro_drawn"] + info["lug_l1"]
            for n in spec.nozzles:
                if n.location == "shell" and n.mark and n.axial_mm is not None and side_of(spec, n) == sd and n.axial_mm - flange_dims(n)["D"] / 2 < e and n.axial_mm + flange_dims(n)["D"] / 2 > 0.0:
                    cover = float(T.lookup("manhole", dn=n.dn)["row"]["cover_thk"]) if n.kind == "manhole" else 0.0
                    r = max(r, ro + float(n.projection_mm or 0.0) + cover)
            return r
        sd = min(info["lug_sides"], key=reach)     # 两侧都有耳座时标在伸出少的一侧，尺寸线不压管口法兰
        vloc = sd * (reach(sd) + 4 * den)
        b.add("DIM-LUG", [_dim(along, M(0.0, sd * ro), M(e, sd * (info["ro_drawn"] + info["lug_l1"])), (vloc, e / 2) if vert else (e / 2, vloc), st)], local=False)
    if "saddles" in info:
        xs = [0.0] + sorted(info["saddles"]) + [L]
        vb = info["saddle_bottom"] - 6 * den
        for i in range(len(xs) - 1):
            ua, ub = xs[i], xs[i + 1]
            pa = M(ua, -ro if i == 0 else info["saddle_bottom"])
            pb = M(ub, -ro if i == len(xs) - 2 else info["saddle_bottom"])
            b.add("DIM-SADDLE", [_dim(along, pa, pb, M((ua + ub) / 2, vb), st)], local=False)
    for o in b.ops:
        if o["kind"] == "dimension":
            o["tags"].append("equip_dim")
    meta = {"chain": chain_vals, "chain_ids": chain_ids, "overall_id": overall_id, "overall": round(top - bot, 6), "expected": round(top - bot, 6), "style": st}
    return b.ops, meta


def _f(v) -> str:
    from .spec_ops import fmt_value
    return fmt_value(v)
