"""默认规则（任务书 4.3）：每条规则一个函数，只算参数、不画几何；汇总为 RULES（按计算顺序）。

规则只填“还没有值”的字段，来源记 rule:<规则号>（查表得到的记 table:<表>，换算得到的记 calc:<规则号>），用户值永远不动。
经验数值都在 data/rules.json（带 source / verified）。PN、法兰型式、人孔标准、支座型号只在设计压力和设计温度都给了时才“初选”，否则写“待定”。
与任务书顺序的唯一差异：R-MARK-01 放在最后（“主视图左下方起顺时针”需要管口位置），已分配的符号不重排。
"""
from __future__ import annotations

import math
import string
from collections import OrderedDict

from zhihui.cad.geom.errors import GeomError

from . import std_tables as T
from .profile import head_profile, inner_depth_of
from .spec_ops import fmt_value, get_key, hkey, nkey, purpose_row, set_key, skey

PENDING = "待定"


class RuleCtx:
    def __init__(self, spec):
        self.spec = spec
        self.defaults: list[dict] = []
        self.warnings: list[str] = []
        self.notes: list[str] = []
        self.tables: list[str] = []
        self.unverified: list[str] = []

    def set(self, key: str, value, rid: str, src_kind: str = "rule") -> bool:
        if value is None or get_key(self.spec, key) is not None:
            return False
        set_key(self.spec, key, value, f"{src_kind}:{rid}")
        if src_kind == "rule":
            self.defaults.append({"path": key, "rule_id": rid, "value": value})
        return True

    def lookup(self, table_id: str, **keys) -> dict:
        r = T.lookup(table_id, **keys)
        self.tables.append(table_id)
        if not r["verified"]:
            self.unverified.append(table_id)
        return r["row"]

    def rule(self, key: str):
        self.tables.append("rules")
        if not T.rule_meta(key)["verified"]:
            self.unverified.append("rules")
        return T.rule_value(key)


def _di(spec) -> float | None:
    return spec.shell.inner_diameter_mm


def design_complete(spec) -> bool:
    d = spec.design
    return d.design_pressure_mpa is not None and d.design_temp_c is not None


def _band(table: list[dict], v: float, key: str):
    for row in table:
        if v <= row["dn_max"] + 1e-9:
            return row[key]
    return table[-1][key]


# ------------------------------------------------------------------ 封头、材料、长度
def r_head_01(c: RuleCtx) -> None:
    from .spec_ops import ensure_heads
    ensure_heads(c.spec)
    for h in c.spec.heads:
        c.set(hkey(h, "kind"), "EHA", "R-HEAD-01")


def r_mat_01(c: RuleCtx) -> None:
    s = c.spec
    m = s.shell.material
    if not m:
        return
    for h in s.heads:
        c.set(hkey(h, "material"), m, "R-MAT-01")
    c.set("design.main_material", m, "R-MAT-01")


def r_head_02(c: RuleCtx) -> None:
    s = c.spec
    t = s.shell.thickness_mm
    if t is None:
        return
    hit = False
    for h in s.heads:
        if h.kind in ("EHA", "HHA", "THA", "SDH"):
            hit |= c.set(hkey(h, "thickness_mm"), t, "R-HEAD-02")
    if hit:
        c.notes.append("封头厚度按筒体厚度画，未计成形减薄与强度")


def r_head_sf(c: RuleCtx) -> None:
    """直边高度：EHA/THA 查 head_eha 表；HHA/SDH/FLAT 按 rules.json 取 0。"""
    s = c.spec
    di = _di(s)
    if di is None:
        return
    for h in s.heads:
        if h.straight_flange_mm is not None:
            continue
        if h.kind in ("EHA", "THA"):
            row = c.lookup("head_eha", dn=di)
            c.set(hkey(h, "straight_flange_mm"), float(row["h_straight"]), "head_eha", "table")
        elif h.kind in ("HHA", "SDH", "FLAT"):
            c.set(hkey(h, "straight_flange_mm"), float(c.rule("straight_flange_by_kind")[h.kind]), "R-HEAD-SF")
        elif h.kind == "CONE":
            raise GeomError("HEAD_UNSUPPORTED", "锥形封头放在 P2，P0 不支持", "改用 EHA（标准椭圆封头）等")


def r_len_01(c: RuleCtx) -> None:
    s = c.spec
    sh = s.shell
    if sh.tangent_length_mm is None or any(h.straight_flange_mm is None for h in s.heads):
        if sh.length_mm is not None and sh.length_mm <= 0:
            raise GeomError("SPEC_CONFLICT", "筒体长度必须大于 0", "")
        return
    calc = sh.tangent_length_mm - sum(h.straight_flange_mm for h in s.heads)
    if calc <= 0:
        raise GeomError("SPEC_CONFLICT", f"切线长 {fmt_value(sh.tangent_length_mm)} 小于两个封头直边之和，筒体长度算出来不是正数", "请确认切线长")
    if sh.length_mm is not None and s.sources.get("shell.length_mm", "").startswith("user:"):
        if abs(sh.length_mm - calc) > 1e-6:
            raise GeomError("SPEC_CONFLICT", f"筒体长度 {fmt_value(sh.length_mm)} 与切线长 {fmt_value(sh.tangent_length_mm)}（换算后筒体长度应为 {fmt_value(calc)}）矛盾", "只保留其中一个口径")
        return
    c.set("shell.length_mm", calc, "R-LEN-01", "calc")


# ------------------------------------------------------------------ 支座
def r_sup_01(c: RuleCtx) -> None:
    from zhihui.ir.vessel import Support
    from .spec_ops import _next_uid
    s = c.spec
    if not s.vertical and not s.supports:
        sup = Support(uid=_next_uid(s.supports, "s"))
        s.supports.append(sup)
        c.set(skey(sup, "kind"), "saddle", "R-SUP-01")
    di = _di(s)
    for sup in s.supports:
        if not s.vertical and sup.kind is None:
            c.set(skey(sup, "kind"), "saddle", "R-SUP-01")
        if sup.kind == "saddle":
            c.set(skey(sup, "count"), 2, "R-SUP-01")
        elif sup.kind == "lug" and di is not None:
            c.set(skey(sup, "count"), int(_band(c.rule("lug_count"), di, "count")), "R-SUP-01")
        elif sup.kind == "leg" and di is not None:
            c.set(skey(sup, "count"), int(_band(c.rule("leg_count"), di, "count")), "R-SUP-01")
        elif sup.kind in ("skirt", "ring"):
            c.set(skey(sup, "count"), 1, "R-SUP-01")
        if sup.kind == "saddle" and s.vertical:
            raise GeomError("SPEC_CONFLICT", "立式容器不能用鞍座", "立式容器用耳式支座、支腿或裙座")
        if sup.kind in ("lug", "leg", "skirt") and not s.vertical:
            raise GeomError("SPEC_CONFLICT", f"卧式容器不能用{ {'lug': '耳式支座', 'leg': '支腿', 'skirt': '裙座'}[sup.kind] }", "卧式容器用鞍座")
        if sup.kind == "skirt":
            raise GeomError("NOT_SUPPORTED", "裙座放在 P2（塔器），P0 只画耳式支座、支腿和鞍座", "改用耳式支座或支腿")
        if sup.kind == "ring":
            raise GeomError("NOT_SUPPORTED", "支承式（环形）支座 P0 不支持", "改用耳式支座或支腿")


def r_sup_02(c: RuleCtx) -> None:
    """支座型号：设计条件齐全才按 DN 初选（备注待校核）；否则“待定”。外形尺寸（画图用）照样查表。"""
    s = c.spec
    di = _di(s)
    if di is None:
        return
    for sup in s.supports:
        if sup.kind == "lug":
            row = c.lookup("lug_support_nbt47065_3", dn=di)
            model = f"NB/T 47065.3 耳式支座 A{row['no']}（初选，待校核）"
        elif sup.kind == "saddle":
            row = c.lookup("saddle_nbt47065_1", dn=di)
            model = f"NB/T 47065.1 鞍式支座 A{fmt_value(float(row['dn']))}-F/S（120°包角，初选，待校核）"
        elif sup.kind == "leg":
            model = "NB/T 47065.2 支腿（型号待定）"
        else:
            continue
        if design_complete(s):
            if c.set(skey(sup, "model"), model, "R-SUP-02"):
                c.warnings.append("W_SUPPORT_NOT_CHECKED：支座型号按 DN 初选，待按 NB/T 47065.x 校核（需要操作/充水质量和风、地震载荷）" + ("；鞍座 A 型和 120° 包角为默认值，未按 NB/T 47042 校核" if sup.kind == "saddle" else ""))
        else:
            c.set(skey(sup, "model"), PENDING, "R-SUP-02")


def r_sup_pos(c: RuleCtx) -> None:
    """R-SUP-POS-02（耳座/支腿方位从 45° 起均布，耳座高度按经验比例）；R-SUP-POS-01（鞍座距封头切线 A）。"""
    s = c.spec
    sh = s.shell
    for sup in s.supports:
        if sup.positions is not None or not sup.count:
            continue
        if sup.kind in ("lug", "leg"):
            start = float(c.rule("lug_start_deg"))
            n = int(sup.count)
            elev = None
            if sup.kind == "lug" and sh.length_mm is not None:
                elev = round(sh.length_mm * float(c.rule("lug_elevation_ratio")) / 10.0) * 10.0
            sup.positions = [{"orientation_deg": (start + 360.0 * i / n) % 360.0, **({"elevation_mm": elev} if elev is not None else {})} for i in range(n)]
            s.sources[skey(sup, "positions")] = "rule:R-SUP-POS-02"
            c.defaults.append({"path": skey(sup, "positions"), "rule_id": "R-SUP-POS-02", "value": sup.positions})
            if sup.kind == "lug":
                c.notes.append("耳座高度位置按默认，待确认（需结合设备重心和支承梁标高）")
        elif sup.kind == "saddle" and sh.length_mm is not None and sh.inner_diameter_mm is not None and all(h.straight_flange_mm is not None for h in s.heads):
            hl, hr = s.heads[0].straight_flange_mm, s.heads[1].straight_flange_mm
            ltan = sh.length_mm + hl + hr
            rm = (sh.inner_diameter_mm + (sh.thickness_mm or 0.0)) / 2
            a = math.floor(min(float(c.rule("saddle_a_ratio_L")) * ltan, float(c.rule("saddle_a_ratio_Rm")) * rm) / 10.0) * 10.0
            n = int(sup.count)
            pos = [{"axial_mm": a - hl, "fixed": True}]
            if n >= 2:
                for i in range(1, n - 1):
                    pos.append({"axial_mm": round((a - hl) + (ltan - 2 * a) * i / (n - 1), 1), "fixed": False})
                pos.append({"axial_mm": sh.length_mm + hr - a, "fixed": False})
            sup.positions = pos
            s.sources[skey(sup, "positions")] = "rule:R-SUP-POS-01"
            c.defaults.append({"path": skey(sup, "positions"), "rule_id": "R-SUP-POS-01", "value": pos})
            c.notes.append(f"鞍座距封头切线 A={fmt_value(a)}（按默认，待确认），左为固定鞍座 F、右为活动鞍座 S")


# ------------------------------------------------------------------ 管口
def r_noz_kind(c: RuleCtx) -> None:
    for n in c.spec.nozzles:
        if nkey(n, "kind") in c.spec.sources:
            continue
        row = purpose_row(n.purpose)
        if row and row["kind"] != "pipe" and n.kind == "pipe":
            n.kind = row["kind"]
            c.spec.sources[nkey(n, "kind")] = "calc:purpose"


def r_noz_loc(c: RuleCtx) -> None:
    s = c.spec
    for n in s.nozzles:
        row = purpose_row(n.purpose)
        cls = row["loc"] if row else "shell"
        if n.kind == "manhole":
            cls = "shell"
        if s.vertical:
            loc = {"top": "top_head", "bottom": "bottom_head"}.get(cls, "shell")
            c.set(nkey(n, "location"), loc, "R-NOZ-LOC-01")
        else:
            c.set(nkey(n, "location"), "shell", "R-NOZ-LOC-02")


def pipe_series(c: RuleCtx) -> str:
    return c.spec.drawing.get("pipe_series") or "B"


def r_pipe_01(c: RuleCtx) -> None:
    s = c.spec
    ser_default = pipe_series(c)
    for n in s.nozzles:
        if n.dn is None:
            continue
        c.set(nkey(n, "pipe_series"), ser_default, "R-PIPE-01")
        if n.kind == "manhole":
            row = c.lookup("manhole", dn=n.dn)
            c.set(nkey(n, "pipe_od_mm"), float(row["neck_od"]), "manhole", "table")
            c.set(nkey(n, "pipe_thk_mm"), float(row["neck_thk"]), "manhole", "table")
            continue
        row = c.lookup("pipe_dims", series=n.pipe_series, dn=n.dn)
        c.set(nkey(n, "pipe_od_mm"), float(row["od"]), "pipe_dims", "table")
        if c.set(nkey(n, "pipe_thk_mm"), float(row["thk"]), "pipe_dims", "table"):
            c.notes.append("接管壁厚按默认，未校核")


def sketch_pn(c: RuleCtx | None, n) -> float:
    """画法兰外形用的 PN：PN 待定时按 PN 下限示意（技术要求里说明），不代表选型。"""
    if isinstance(n.pn, (int, float)):
        return float(n.pn)
    return float(T.rule_value("pn_min"))


def flange_dims(n, c: RuleCtx | None = None) -> dict:
    """法兰外径 D、厚度 C（画图与间距判断用）。"""
    ser = n.pipe_series or "B"
    keys = {"series": ser, "pn": sketch_pn(c, n), "dn": n.dn}
    row = c.lookup("pipe_flange_hg20592", **keys) if c else T.lookup("pipe_flange_hg20592", **keys)["row"]
    return {"D": float(row["D"]), "C": float(row["C"])}


def footprint(n, c: RuleCtx | None = None) -> float:
    """开孔在壳体上的占位直径：max(法兰外径, 补强圈估计外径)。"""
    od = float(n.pipe_od_mm or 0.0)
    thr = T.rule_value("reinf_dn_threshold")
    pad = od * float(T.rule_value("reinf_pad_od_ratio")) if (n.dn or 0) >= thr else od
    return max(flange_dims(n, c)["D"], pad)


def pad_od(n) -> float:
    od = float(n.pipe_od_mm or 0.0)
    return od * float(T.rule_value("reinf_pad_od_ratio")) if (n.dn or 0) >= T.rule_value("reinf_dn_threshold") else od


def _round_up(v: float, step: float = 10.0) -> float:
    return math.ceil(v / step - 1e-9) * step


def r_noz_pos_01(c: RuleCtx) -> None:
    """筒体管口的轴向位置：人孔、液位计按经验位置；其余在筒体可用段内均布。"""
    s = c.spec
    L = s.shell.length_mm
    if L is None:
        return
    edge = float(c.rule("seam_min_edge_mm"))
    shell = [n for n in s.nozzles if n.location == "shell" and n.dn is not None and n.pipe_od_mm is not None]
    todo = [n for n in shell if n.axial_mm is None]
    if not todo:
        return

    def lo_hi(n):
        r = footprint(n, c) / 2
        return edge + r, L - edge - r

    if s.vertical:
        lgs = [n for n in todo if n.kind == "level_gauge"]
        if lgs:
            lo, hi = lo_hi(lgs[0])
            lo = _round_up(lo)
            if len(lgs) == 1:
                c.set(nkey(lgs[0], "axial_mm"), lo, "R-NOZ-POS-01")
            else:
                user_cc = None
                cc_opts = [x for x in c.rule("level_gauge_cc_mm") if lo + x * (len(lgs) - 1) <= hi + 1e-9]
                if not cc_opts and user_cc is None:
                    raise GeomError("NOZZLE_NO_ROOM", f"筒体长度 {fmt_value(L)} 放不下 {len(lgs)} 个液位计接口（最小中心距 {c.rule('level_gauge_cc_mm')[0]}）", "加长筒体，或把液位计接口放到封头上")
                cc = max(cc_opts)
                for i, n in enumerate(lgs):
                    c.set(nkey(n, "axial_mm"), lo + cc * i, "R-NOZ-POS-01")
                c.notes.append(f"液位计接口中心距 {fmt_value(float(cc))} 按默认，待仪表专业确认")
        for n in [x for x in todo if x.kind in ("manhole", "handhole")]:
            lo, hi = lo_hi(n)
            if lo > hi:
                raise GeomError("NOZZLE_NO_ROOM", f"筒体长度 {fmt_value(L)} 放不下 {n.purpose or '人孔'} DN{fmt_value(n.dn)}", "加长筒体，或把人孔放到封头上")
            want = float(c.rule("manhole_center_mm"))
            c.set(nkey(n, "axial_mm"), min(max(want, _round_up(lo)), math.floor(hi / 10) * 10), "R-NOZ-POS-01")
        rest = [x for x in todo if x.axial_mm is None]
    else:
        rest = todo
    if not rest:
        return
    # 卧式：顶部一组、底部一组、侧面（液位计）一组，各自在可用段内均布；底部避开鞍座
    groups: dict[str, list] = {}
    for n in rest:
        if s.vertical:
            g = "all"
        else:
            row = purpose_row(n.purpose)
            g = "lg" if n.kind == "level_gauge" else ("bottom" if row and row["loc"] == "bottom" else "top")
        groups.setdefault(g, []).append(n)
    sad = next((x for x in s.supports if x.kind == "saddle" and x.positions), None)
    for g, items in groups.items():
        rmax = max(footprint(n, c) for n in items) / 2
        lo, hi = edge + rmax, L - edge - rmax
        if g == "lg":
            ax = _round_up((lo + hi) / 2 - (hi - lo) * 0.25)
            for n in items:
                c.set(nkey(n, "axial_mm"), ax, "R-NOZ-POS-01")
            continue
        if g == "bottom" and sad:
            row = c.lookup("saddle_nbt47065_1", dn=s.shell.inner_diameter_mm)
            xs = sorted(p["axial_mm"] for p in sad.positions)
            lo, hi = max(lo, xs[0] + row["b1"] / 2 + rmax + edge), min(hi, xs[-1] - row["b1"] / 2 - rmax - edge)
        k = len(items)
        if lo > hi or (k > 1 and (hi - lo) / (k - 1) < 2 * rmax):
            raise GeomError("NOZZLE_NO_ROOM", f"筒体上放不下这 {k} 个管口（可用段 {fmt_value(max(0.0, hi - lo))} mm）", "加长筒体、减少管口，或把部分管口放到封头上")
        for i, n in enumerate(items):
            u = lo + (hi - lo) * (i + 1) / (k + 1) if k > 0 else (lo + hi) / 2
            c.set(nkey(n, "axial_mm"), round(u / 10.0) * 10.0, "R-NOZ-POS-01")


def r_noz_pos_02(c: RuleCtx) -> None:
    """封头管口：下封头出料口在中心、其余（含上封头全部）均布在半径 0.25Di 的圆上；开孔外边缘距中心 ≤ 0.4Di。"""
    s = c.spec
    di = _di(s)
    if di is None:
        return
    limit = float(c.rule("head_radial_limit_ratio")) * di
    for loc in ("top_head", "bottom_head", "left_head", "right_head"):
        items = [n for n in s.nozzles if n.location == loc and n.dn is not None and n.pipe_od_mm is not None]
        todo = [n for n in items if n.radial_mm is None]
        if not todo:
            continue
        center = None
        if loc in ("bottom_head", "right_head"):
            center = next((n for n in todo if n.purpose == "出料口"), None)
            if center is None and len(items) == 1:
                center = todo[0]
        if center is not None:
            c.set(nkey(center, "radial_mm"), 0.0, "R-NOZ-POS-02")
            if pad_od(center) / 2 > limit:
                raise GeomError("NOZZLE_NO_ROOM", f"封头开孔 DN{fmt_value(center.dn)} 超出 0.4Di 的范围", "改放到筒体上")
        ring = [n for n in todo if n is not center]
        if not ring:
            continue
        rmax = max(pad_od(n) for n in ring) / 2
        r = min(float(c.rule("head_ring_ratio")) * di, limit - rmax)
        r = math.floor(r / 10.0) * 10.0
        if r < rmax and len(ring) == 1 and center is None and not any(n.radial_mm == 0 for n in items) and pad_od(ring[0]) / 2 <= limit:
            c.set(nkey(ring[0], "radial_mm"), 0.0, "R-NOZ-POS-02")          # 小封头上只有一个管口、圆周上放不下：放在封头中心
            continue
        if r < rmax:
            raise GeomError("NOZZLE_NO_ROOM", f"{'上' if loc == 'top_head' else '下' if loc == 'bottom_head' else '左' if loc == 'left_head' else '右'}封头放不下这些管口（0.4Di = {fmt_value(limit)}）", "把部分管口放到筒体上，或减小 DN")
        for n in ring:
            c.set(nkey(n, "radial_mm"), float(r), "R-NOZ-POS-02")


def _angle_diff(a: float, b: float) -> float:
    d = abs((a - b) % 360.0)
    return min(d, 360.0 - d)


def _ro(s) -> float:
    return s.shell.inner_diameter_mm / 2 + (s.shell.thickness_mm or 0.0)


def shell_conflict(s, n, ang: float, others: list[tuple], c: RuleCtx | None = None) -> str | None:
    """筒体管口在方位 ang 是否与已定的管口 / 支座冲突：方位上弧长重叠且轴向范围重叠。"""
    ro = _ro(s)
    fp = footprint(n, c)
    for m, a2 in others:
        if m.axial_mm is None:
            continue
        fp2 = footprint(m, c)
        arc = math.radians(_angle_diff(ang, a2)) * ro
        if arc < (fp + fp2) / 2 and abs(n.axial_mm - m.axial_mm) < (fp + fp2) / 2:
            return f"管口 {m.mark or m.uid}"
    for sup in s.supports:
        if sup.kind not in ("lug", "leg") or not sup.positions:
            continue
        if sup.kind == "lug":
            row = T.lookup("lug_support_nbt47065_3", dn=s.shell.inner_diameter_mm)["row"]
            half_w = row["b1"] / 2
            for p in sup.positions:
                e = p.get("elevation_mm")
                if e is None:
                    continue
                z0, z1 = e, e + row["H"]
                arc = math.radians(_angle_diff(ang, p["orientation_deg"])) * ro
                if arc < half_w + fp / 2 and z0 - fp / 2 < n.axial_mm < z1 + fp / 2:
                    return "耳式支座"
        else:
            for p in sup.positions:
                arc = math.radians(_angle_diff(ang, p["orientation_deg"])) * ro
                if arc < 100.0 + fp / 2 and n.axial_mm < float(T.rule_value("leg_overlap_mm")) + fp / 2:
                    return "支腿"
    return None


def head_conflict(n, ang: float, others: list[tuple], c: RuleCtx | None = None) -> str | None:
    fp = footprint(n, c)
    p = (n.radial_mm * math.sin(math.radians(ang)), n.radial_mm * math.cos(math.radians(ang)))
    for m, a2 in others:
        if m.radial_mm is None:
            continue
        q = (m.radial_mm * math.sin(math.radians(a2)), m.radial_mm * math.cos(math.radians(a2)))
        if math.dist(p, q) < (fp + footprint(m, c)) / 2 + 20.0:
            return f"管口 {m.mark or m.uid}"
    return None


def r_noz_ori_01(c: RuleCtx) -> None:
    """方位：以 45° 为步长找不冲突的位置（先避开已定的支座，再避开已定的管口）；液位计一对共用同一方位。卧式顶部管口缺省 0°、底部 180°。"""
    s = c.spec
    if _di(s) is None:
        return
    cands = [0.0, 90.0, 180.0, 270.0, 45.0, 135.0, 225.0, 315.0]
    fixed = {loc: [(n, n.orientation_deg) for n in s.nozzles if n.location == loc and n.orientation_deg is not None] for loc in ("shell", "top_head", "bottom_head", "left_head", "right_head")}
    todo = [n for n in s.nozzles if n.orientation_deg is None and n.location and n.dn is not None and n.pipe_od_mm is not None]
    lgs = [n for n in todo if n.kind == "level_gauge" and n.location == "shell"]
    for n in todo:
        if n.orientation_deg is not None:
            continue
        loc = n.location
        if loc != "shell":
            if not n.radial_mm:
                c.set(nkey(n, "orientation_deg"), 0.0, "R-NOZ-ORI-01")
                fixed[loc].append((n, 0.0))
                continue
            for a in cands:
                if head_conflict(n, a, fixed[loc], c) is None:
                    c.set(nkey(n, "orientation_deg"), a, "R-NOZ-ORI-01")
                    fixed[loc].append((n, a))
                    break
            else:
                raise GeomError("NOZZLE_NO_ROOM", f"封头上找不到不冲突的方位放 {n.purpose or '管口'} DN{fmt_value(n.dn)}", "把它放到筒体上，或给出方位")
            continue
        if n.axial_mm is None:
            continue
        group = lgs if (n in lgs and s.vertical) else [n]
        if not s.vertical:
            row = purpose_row(n.purpose)
            if n.kind == "level_gauge":
                pref = [45.0, 135.0] if n is (lgs[0] if lgs else n) else [135.0, 45.0]
            elif row and row["loc"] == "bottom":
                pref = [180.0, 135.0, 225.0]
            else:
                pref = [0.0, 315.0, 45.0]
            order = pref + [a for a in cands if a not in pref]
        else:
            order = cands
        if s.vertical:                                                    # 立式：先找没有别的管口用过的方位（方位图更清楚），再退回只避开实际冲突
            taken = {round(a2 % 360.0, 6) for _, a2 in fixed["shell"]}
            order = [a for a in order if round(a, 6) not in taken] + [a for a in order if round(a, 6) in taken]
        for a in order:
            if all(m.axial_mm is not None and shell_conflict(s, m, a, fixed["shell"], c) is None for m in group):
                for m in group:
                    c.set(nkey(m, "orientation_deg"), a, "R-NOZ-ORI-01")
                    fixed["shell"].append((m, a))
                break
        else:
            raise GeomError("NOZZLE_NO_ROOM", f"筒体上找不到与其他管口和支座都不冲突的方位放 {n.purpose or '管口'} DN{fmt_value(n.dn)}", "给出方位或轴向位置，或减少管口")


def _extra(c: RuleCtx, key: str, dn: float) -> float:
    return float(_band(c.rule(key), dn, "mm"))


def r_noz_proj_01(c: RuleCtx) -> None:
    s = c.spec
    ins = float(s.design.insulation_thickness_mm or 0.0)
    for n in s.nozzles:
        if n.dn is None or n.location is None:
            continue
        if n.location == "shell":
            if n.kind == "manhole":
                v = float(c.lookup("manhole", dn=n.dn)["h1"]) + ins
            else:
                v = _extra(c, "projection_by_dn", n.dn) + ins
            c.set(nkey(n, "projection_mm"), v, "R-NOZ-PROJ-01")
        else:
            head = _head_for(s, n.location)
            if head is None or head.kind is None or head.straight_flange_mm is None or n.radial_mm is None or head.thickness_mm is None:
                continue
            pr = head_profile(s, head)
            r = n.radial_mm + float(n.pipe_od_mm or 0) / 2
            v = _round_up(pr.hsf + pr.outer_y(min(r, pr.a + pr.t)) + _extra(c, "head_projection_extra", n.dn) + ins)
            c.set(nkey(n, "projection_mm"), v, "R-NOZ-PROJ-01")


def _head_for(s, loc: str):
    pos = {"top_head": "top", "bottom_head": "bottom", "left_head": "left", "right_head": "right"}.get(loc)
    return next((h for h in s.heads if h.position == pos), None)


def static_head_mpa(c: RuleCtx) -> float:
    s = c.spec
    sh = s.shell
    if sh.inner_diameter_mm is None or sh.length_mm is None:
        return 0.0
    if s.vertical:
        h = sh.length_mm + sum((hd.straight_flange_mm or 0.0) + inner_depth_of(hd.kind or "EHA", sh.inner_diameter_mm)[0] for hd in s.heads)
    else:
        h = sh.inner_diameter_mm
    return float(c.rule("liquid_density_kg_m3")) * 9.81 * h / 1000.0 / 1e6


def _is_bottom(s, n) -> bool:
    if n.location == "bottom_head":
        return True
    if not s.vertical and n.location == "shell" and n.orientation_deg is not None:
        return 90.0 < n.orientation_deg % 360 < 270.0
    return False


def r_pn_01(c: RuleCtx) -> None:
    """PN：只有设计压力和设计温度都给了才按压力-温度额定值初选（底部管口加液柱静压，且不低于 PN 下限）；否则“待定”。"""
    s = c.spec
    if not design_complete(s):
        return
    p, t = abs(float(s.design.design_pressure_mpa)), float(s.design.design_temp_c)
    group = c.rule("flange_material_group_default")
    series = [x for x in c.rule("pn_series") if x >= c.rule("pn_min")]
    stat = static_head_mpa(c)
    for n in s.nozzles:
        if n.pn is not None or n.dn is None:
            continue
        need = p + (stat if _is_bottom(s, n) else 0.0)
        tbands = sorted({r["t_max"] for r in T.rows("flange_pt_rating") if r["group"] == group})
        tb = next((x for x in tbands if x >= t - 1e-9), None)
        if tb is None or t < -20:
            raise GeomError("STD_SIZE_NOT_FOUND", f"设计温度 {fmt_value(t)}℃ 超出法兰压力-温度额定值表的范围（{T.coverage('flange_pt_rating')}）", "请直接给出管口 PN")
        pick = None
        for pn in series:
            row = c.lookup("flange_pt_rating", group=group, pn=pn, t_max=tb)
            if row["p_mpa"] >= need - 1e-12:
                pick = pn
                break
        if pick is None:
            raise GeomError("STD_SIZE_NOT_FOUND", f"按设计压力 {fmt_value(p)} MPa、设计温度 {fmt_value(t)}℃，本系统的法兰额定值表（到 PN{series[-1]}）里选不出 PN", "请直接给出管口 PN")
        c.set(nkey(n, "pn"), float(pick), "R-PN-01")
    c.notes.append("管口 PN 按设计压力、设计温度和碳素钢材料组初选，待校核")


def r_flg_01(c: RuleCtx) -> None:
    s = c.spec
    hazard = any(w in (s.design.medium_property or "") for w in c.rule("hazard_words"))
    for n in s.nozzles:
        if n.kind in ("manhole", "handhole"):
            continue
        if not design_complete(s) or not isinstance(n.pn, (int, float)):
            continue
        c.set(nkey(n, "flange_std"), "HG/T 20592", "R-FLG-01")
        ft = next((r["type"] for r in c.rule("flange_type_by_pn") if n.pn <= r["pn_max"]), None)
        if ft == "PL" and hazard:
            ft = "SO"
        c.set(nkey(n, "flange_type"), ft, "R-FLG-01")
        c.set(nkey(n, "facing"), next((r["facing"] for r in c.rule("facing_by_pn") if n.pn <= r["pn_max"]), None), "R-FLG-01")


def r_mh_01(c: RuleCtx) -> None:
    s = c.spec
    for n in s.nozzles:
        if n.kind not in ("manhole", "handhole") or n.std_part is not None:
            continue
        if not design_complete(s):
            continue
        p = float(s.design.design_pressure_mpa)
        std = None
        for r in c.rule("manhole_std"):
            if "p_lt" in r and p < r["p_lt"] and p >= 0:
                std = r["std"]
                break
            if "pn_max" in r and isinstance(n.pn, (int, float)) and n.pn <= r["pn_max"] and p >= 0.1:
                std = r["std"]
                break
        if std:
            word = "人孔" if n.kind == "manhole" else "手孔"
            c.set(nkey(n, "std_part"), f"{std} {word} DN{fmt_value(n.dn)}" + (f" PN{fmt_value(n.pn)}" if isinstance(n.pn, (int, float)) and p >= 0.1 else "") + "（初选，待校核）", "R-MH-01")


def r_std_01(c: RuleCtx) -> None:
    d = c.spec.design
    p = d.design_pressure_mpa
    if p is None:
        return
    if p >= 0.1 or p <= -0.02:
        c.set("design.design_standard", "GB/T 150.1~150.4-2024", "R-STD-01")
    else:
        c.set("design.design_standard", "NB/T 47003.1-2022", "R-STD-01")


def r_design_missing(c: RuleCtx) -> None:
    if not design_complete(c.spec):
        c.warnings.append("W_DESIGN_DATA_MISSING：设计压力或设计温度没给，管口 PN、法兰型式、人孔标准和支座型号写“待定”，没有按下限取值出图")


def r_sheet_01(c: RuleCtx) -> None:
    c.set("drawing.sheet", c.rule("sheet_default"), "R-SHEET-01")


# ------------------------------------------------------------------ 管口符号
def main_view_pos(s, n) -> tuple[float, float] | None:
    """管口在主视图里的大致位置（旋转画法后），给 R-MARK-01 排顺序用。"""
    sh = s.shell
    if sh.length_mm is None or sh.inner_diameter_mm is None or n.orientation_deg is None:
        return None
    L, ro = sh.length_mm, _ro(s)
    th = n.orientation_deg % 360.0
    proj = float(n.projection_mm or 0.0)
    if s.vertical:
        side = 1.0 if th < 180.0 else -1.0
        if n.location == "shell":
            return side * (ro + proj), float(n.axial_mm or 0.0)
        r = side * float(n.radial_mm or 0.0)
        return (r, L + proj) if n.location == "top_head" else (r, -proj)
    side = 1.0 if (th >= 270.0 or th < 90.0) else -1.0
    if n.location == "shell":
        return float(n.axial_mm or 0.0), side * (ro + proj)
    r = side * float(n.radial_mm or 0.0)
    return (-proj, r) if n.location == "left_head" else (L + proj, r)


def clockwise_key(s, xy: tuple[float, float]) -> float:
    sh = s.shell
    cx, cy = (0.0, (sh.length_mm or 0) / 2) if s.vertical else ((sh.length_mm or 0) / 2, 0.0)
    a = math.degrees(math.atan2(xy[1] - cy, xy[0] - cx))
    return (225.0 - a) % 360.0


def r_mark_01(c: RuleCtx) -> None:
    """管口符号：首次生成时从主视图左下方起顺时针编 a、b、c…（同规格同用途的 b1、b2）；人孔用 M1、M2；已有符号不重排。"""
    s = c.spec
    todo = [n for n in s.nozzles if not n.mark]
    if not todo:
        return
    style = s.drawing.get("mark_style") or "letter"
    used = {n.mark for n in s.nozzles if n.mark}
    pos = {n.uid: main_view_pos(s, n) for n in todo}
    todo.sort(key=lambda n: (clockwise_key(s, pos[n.uid]) if pos[n.uid] else 999.0, n.uid))
    if style.upper().startswith("N"):
        k = 1
        for n in todo:
            while f"N{k}" in used:
                k += 1
            set_key(s, nkey(n, "mark"), f"N{k}", "rule:R-MARK-01")
            used.add(f"N{k}")
        return
    mp = c.rule("manhole_mark_prefix")
    skip = set(c.rule("mark_skip"))
    letters = [ch for ch in string.ascii_lowercase if ch not in skip]
    letters += [x + y for x in letters for y in letters]                 # 超过 23 组同规格同用途的管口时接着用 aa、ab…，不能崩
    used_letters = {m.rstrip(string.digits) for m in used}
    groups: list[list] = []
    for n in todo:
        if n.kind == "manhole":
            continue
        key = (n.purpose or n.purpose_text, n.dn, n.kind)
        g = next((g for g in groups if (g[0].purpose or g[0].purpose_text, g[0].dn, g[0].kind) == key), None)
        if g is None:
            groups.append([n])
        else:
            g.append(n)
    li = 0
    for g in groups:
        while letters[li] in used_letters:
            li += 1
        ch = letters[li]
        used_letters.add(ch)
        for i, n in enumerate(g, 1):
            set_key(s, nkey(n, "mark"), ch if len(g) == 1 else f"{ch}{i}", "rule:R-MARK-01")
    k = 1
    for n in [x for x in todo if x.kind == "manhole"]:
        while f"{mp}{k}" in used:
            k += 1
        set_key(s, nkey(n, "mark"), f"{mp}{k}", "rule:R-MARK-01")
        used.add(f"{mp}{k}")


RULES: "OrderedDict[str, callable]" = OrderedDict([
    ("R-HEAD-01", r_head_01), ("R-MAT-01", r_mat_01), ("R-HEAD-02", r_head_02), ("R-HEAD-SF", r_head_sf), ("R-LEN-01", r_len_01),
    ("R-SUP-01", r_sup_01), ("R-SUP-02", r_sup_02), ("R-SUP-POS", r_sup_pos),
    ("R-NOZ-KIND", r_noz_kind), ("R-NOZ-LOC", r_noz_loc), ("R-PIPE-01", r_pipe_01), ("R-NOZ-POS-01", r_noz_pos_01), ("R-NOZ-POS-02", r_noz_pos_02),
    ("R-NOZ-ORI-01", r_noz_ori_01), ("R-NOZ-PROJ-01", r_noz_proj_01), ("R-PN-01", r_pn_01), ("R-FLG-01", r_flg_01), ("R-MH-01", r_mh_01),
    ("R-STD-01", r_std_01), ("W-DESIGN", r_design_missing), ("R-SHEET-01", r_sheet_01), ("R-MARK-01", r_mark_01),
])
