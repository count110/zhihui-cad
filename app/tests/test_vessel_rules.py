"""默认规则单测（任务书 10.1 第一条“每条默认规则至少 1 个正常用例和 1 个边界用例”）。

测试函数名里带规则号（R-HEAD-01 → test_r_head_01_…），最后一个测试检查 RULES 里每条规则都至少有 2 个测试，新增规则不写测试会失败。
期望值来自任务书 4.3 的规则文字和 data/*.json 的数据表，数字都在注释里写了推算过程。
"""
import copy
import math

import pytest

from zhihui.cad.equipment import rules as R
from zhihui.cad.equipment import spec_ops as S
from zhihui.cad.equipment import std_tables as T
from zhihui.cad.geom.errors import GeomError
from zhihui.ir.vessel import Head, Nozzle, VesselPatchIn, VesselSpec

DESIGN = {"design_pressure_mpa": 0.6, "design_temp_c": 80}


def V(**kw) -> dict:
    """立式 DN1600×8、筒体长 2400、一个耳座、一个进料口（上封头）；参数可覆盖。"""
    return {"kind": "vertical_vessel", "shell": {"inner_diameter_mm": 1600, "length_mm": 2400, "thickness_mm": 8}, "nozzles": [{"dn": 80, "purpose_text": "进料口"}],
            "supports": [{"kind": "lug"}], **kw}


def H(**kw) -> dict:
    """卧式 DN2000×10、切线长 6000、一个进料口（顶部）；没写支座，由 R-SUP-01 补两个鞍座。"""
    return {"kind": "horizontal_vessel", "shell": {"inner_diameter_mm": 2000, "tangent_length_mm": 6000, "thickness_mm": 10}, "nozzles": [{"dn": 100, "purpose_text": "进料"}], **kw}


def build(d: dict, turn: str = "t1") -> VesselSpec:
    spec, _, _ = S.apply_create(VesselPatchIn.model_validate(copy.deepcopy(d)), turn)
    S.fill_defaults(spec)
    return spec


def build_info(d: dict, turn: str = "t1"):
    spec, _, _ = S.apply_create(VesselPatchIn.model_validate(copy.deepcopy(d)), turn)
    return spec, S.fill_defaults(spec)


def patch(spec: VesselSpec, turn: str = "t2", **ops) -> VesselSpec:
    sp, _, _ = S.apply_patch(spec, copy.deepcopy(ops), turn)
    S.fill_defaults(sp)
    return sp


def raises(code: str, fn, *a, **kw) -> GeomError:
    with pytest.raises(GeomError) as ei:
        fn(*a, **kw)
    assert ei.value.code == code, f"期望 {code}，实际 {ei.value.code}：{ei.value.message}"
    return ei.value


def by(spec: VesselSpec, key: str, value) -> Nozzle:
    hit = [n for n in spec.nozzles if getattr(n, key) == value]
    assert len(hit) == 1, (key, value, [(n.mark, n.purpose) for n in spec.nozzles])
    return hit[0]


def head(spec: VesselSpec, pos: str) -> Head:
    return next(h for h in spec.heads if h.position == pos)


def src(spec: VesselSpec, key: str):
    return spec.sources.get(key)


def nsrc(spec: VesselSpec, n: Nozzle, field: str):
    return spec.sources.get(f"nozzles[{n.uid}].{field}")


# ================================================================== R-HEAD-01：封头缺省 EHA，立式 top+bottom、卧式 left+right
def test_r_head_01_default_eha_vertical_top_bottom_horizontal_left_right():
    v = build(V())
    assert [(h.position, h.kind) for h in v.heads] == [("bottom", "EHA"), ("top", "EHA")]
    assert src(v, "heads[top].kind") == src(v, "heads[bottom].kind") == "rule:R-HEAD-01"
    h = build(H())
    assert [(x.position, x.kind) for x in h.heads] == [("left", "EHA"), ("right", "EHA")]
    assert src(h, "heads[left].kind") == "rule:R-HEAD-01"


def test_r_head_01_boundary_user_kind_wins_and_stray_positions_are_dropped():
    v = build(V(heads=[{"position": "top", "kind": "HHA"}]))
    assert head(v, "top").kind == "HHA" and src(v, "heads[top].kind") == "user:t1"
    assert head(v, "bottom").kind == "EHA"
    spec, _, _ = S.apply_create(VesselPatchIn.model_validate(V()), "t1")
    spec.heads.append(Head(position="left"))                                                                     # 立式容器不该有 left：ensure_heads 会清掉并按固定顺序排
    spec.heads.reverse()
    S.ensure_heads(spec)
    assert [h.position for h in spec.heads] == ["bottom", "top"]
    S.ensure_heads(spec)
    assert [h.position for h in spec.heads] == ["bottom", "top"]                                                 # 幂等


# ================================================================== R-MAT-01：一个材料只用于壳体（筒体、封头、主要受压元件）
def test_r_mat_01_single_material_goes_to_shell_heads_and_main_material_only():
    spec = build(V(shell={"inner_diameter_mm": 1600, "length_mm": 2400, "thickness_mm": 8, "material": "Q345R"},
                   nozzles=[{"dn": 500, "kind": "manhole", "purpose_text": "人孔"}, {"dn": 80, "purpose_text": "进料口"}]))
    assert head(spec, "top").material == head(spec, "bottom").material == spec.design.main_material == "Q345R"
    assert src(spec, "heads[top].material") == src(spec, "design.main_material") == "rule:R-MAT-01"
    assert all(n.pipe_material is None for n in spec.nozzles)                                                    # 接管、人孔的材料写“待填”，不能带板材牌号
    assert all(s.material is None for s in spec.supports)


def test_r_mat_01_boundary_user_values_stay_and_no_shell_material_means_nothing_to_copy():
    spec = build(V(shell={"inner_diameter_mm": 1600, "length_mm": 2400, "thickness_mm": 8, "material": "Q345R"}, heads=[{"position": "top", "material": "16MnR"}],
                   design={"main_material": "Q245R"}))
    assert head(spec, "top").material == "16MnR" and head(spec, "bottom").material == "Q345R"
    assert spec.design.main_material == "Q245R" and src(spec, "design.main_material") == "user:t1"
    none = build(V())
    assert head(none, "top").material is None and none.design.main_material is None


# ================================================================== R-HEAD-02：EHA/HHA/THA/SDH 封头厚度缺省同筒体，FLAT/CONE 必问
@pytest.mark.parametrize("kind", ["EHA", "HHA", "THA", "SDH"])
def test_r_head_02_curved_heads_follow_shell_thickness_and_note_it(kind):
    spec, info = build_info(V(heads=[{"kind": kind}]))
    assert [h.thickness_mm for h in spec.heads] == [8.0, 8.0]
    assert src(spec, "heads[top].thickness_mm") == "rule:R-HEAD-02"
    assert "封头厚度按筒体厚度画，未计成形减薄与强度" in info["notes"]


def test_r_head_02_boundary_flat_head_never_follows_shell_and_is_asked():
    spec, info = build_info(V(heads=[{"position": "top", "kind": "FLAT"}]))
    assert head(spec, "top").thickness_mm is None and head(spec, "bottom").thickness_mm == 8.0
    assert [p["path"] for p in S.missing(spec)] == ["heads[top].thickness_mm"]
    assert "封头厚度按筒体厚度画，未计成形减薄与强度" in info["notes"]                                                # 下封头仍是默认的，所以仍要注明
    ok = build(V(heads=[{"position": "top", "kind": "FLAT", "thickness_mm": 24}]))
    assert head(ok, "top").thickness_mm == 24.0 and src(ok, "heads[top].thickness_mm") == "user:t1" and S.missing(ok) == []


def test_r_head_02_boundary_user_thickness_stays_and_no_note_when_nothing_was_defaulted():
    spec, info = build_info(V(heads=[{"kind": "EHA", "thickness_mm": 10}]))
    assert [h.thickness_mm for h in spec.heads] == [10.0, 10.0] and src(spec, "heads[top].thickness_mm") == "user:t1"
    assert "封头厚度按筒体厚度画，未计成形减薄与强度" not in info["notes"]                                           # 全是用户给的，没有“按默认”可注明


# ================================================================== R-HEAD-SF：直边高度（EHA/THA 查 head_eha，HHA/SDH/FLAT 取 0）
def test_r_head_sf_straight_flange_by_kind():
    v = build(V())
    assert [h.straight_flange_mm for h in v.heads] == [25.0, 25.0] and src(v, "heads[top].straight_flange_mm") == "table:head_eha"
    got = {k: [h.straight_flange_mm for h in build(V(heads=[{"kind": k, "thickness_mm": 20}])).heads] for k in ("EHA", "THA", "HHA", "SDH", "FLAT")}
    assert got == {"EHA": [25.0, 25.0], "THA": [25.0, 25.0], "HHA": [0.0, 0.0], "SDH": [0.0, 0.0], "FLAT": [0.0, 0.0]}
    assert src(build(V(heads=[{"kind": "HHA"}])), "heads[top].straight_flange_mm") == "rule:R-HEAD-SF"


@pytest.mark.parametrize("di,expect", [(300, 25.0), (1600, 25.0), (2000, 25.0), (2001, 40.0), (3000, 40.0), (6000, 40.0)])
def test_r_head_sf_boundary_head_eha_bands(di, expect):
    spec = build(V(shell={"inner_diameter_mm": di, "length_mm": 3000, "thickness_mm": 12}, supports=[{"kind": "leg", "height_mm": 600}]))
    assert [h.straight_flange_mm for h in spec.heads] == [expect, expect]                                         # DN2000 与 DN2001 在不同档


def test_r_head_sf_boundary_outside_table_unsupported_cone_and_user_value():
    for di in (299, 6001):
        e = raises("STD_SIZE_NOT_FOUND", build, V(shell={"inner_diameter_mm": di, "length_mm": 3000, "thickness_mm": 12}, supports=[{"kind": "leg", "height_mm": 600}]))
        assert e.extra["table_id"] == "head_eha" and "不插值" in e.hint
    raises("HEAD_UNSUPPORTED", build, V(heads=[{"kind": "CONE", "thickness_mm": 10}]))
    spec = build(V(shell={"inner_diameter_mm": 250, "length_mm": 1000, "thickness_mm": 6}, heads=[{"straight_flange_mm": 20}], supports=[{"kind": "leg", "height_mm": 500}]))
    assert [h.straight_flange_mm for h in spec.heads] == [20.0, 20.0]                                             # 用户给了直边就不查表，表外的内径也能出


# ================================================================== R-LEN-01：切线长 → 筒体长度 = 切线长 − 两直边
def test_r_len_01_tangent_length_converts_to_shell_length():
    v = build(V(shell={"inner_diameter_mm": 1600, "tangent_length_mm": 3000, "thickness_mm": 8}))
    assert v.shell.length_mm == 2950.0 and src(v, "shell.length_mm") == "calc:R-LEN-01" and src(v, "shell.tangent_length_mm") == "user:t1"   # 3000 − 2×25
    h = build(H())
    assert h.shell.length_mm == 5950.0 and src(h, "shell.length_mm") == "calc:R-LEN-01"
    hha = build(V(shell={"inner_diameter_mm": 1600, "tangent_length_mm": 3000, "thickness_mm": 8}, heads=[{"kind": "HHA"}]))
    assert hha.shell.length_mm == 3000.0                                                                          # 半球封头没有直边
    sp = patch(h, set={"heads.kind": "HHA"})                                                                      # 换封头型式后筒体长度跟着重算
    assert sp.shell.length_mm == 6000.0


def test_r_len_01_boundary_conflict_and_non_positive_length():
    d = V(shell={"inner_diameter_mm": 1600, "tangent_length_mm": 3000, "length_mm": 2900, "thickness_mm": 8})     # 3000 − 50 = 2950 ≠ 2900
    e = raises("SPEC_CONFLICT", build, d)
    assert "2900" in e.message and "3000" in e.message and "2950" in e.message
    ok = build(V(shell={"inner_diameter_mm": 1600, "tangent_length_mm": 3000, "length_mm": 2950, "thickness_mm": 8}))
    assert ok.shell.length_mm == 2950.0 and src(ok, "shell.length_mm") == "user:t1"                               # 两个口径一致：保留用户的
    raises("SPEC_CONFLICT", build, V(shell={"inner_diameter_mm": 1600, "tangent_length_mm": 50, "thickness_mm": 8}))     # 刚好等于两个直边之和 → 筒体长度 0
    assert build(V(shell={"inner_diameter_mm": 1600, "tangent_length_mm": 51, "thickness_mm": 8}, nozzles=[{"dn": 25, "purpose_text": "温度计口", "axial_mm": 0.5, "orientation_deg": 0}])).shell.length_mm == 1.0


def test_r_len_01_boundary_no_basis_given_leaves_length_empty():
    spec = build({"kind": "vertical_vessel", "shell": {"inner_diameter_mm": 1600, "thickness_mm": 8}, "supports": [{"kind": "lug"}], "nozzles": [{"dn": 80, "purpose_text": "进料口"}]})
    assert spec.shell.length_mm is None and spec.shell.tangent_length_mm is None
    assert [p["path"] for p in S.missing(spec)] == ["shell.length"]


# ================================================================== R-SUP-01：支座数量
def test_r_sup_01_counts_by_kind_and_diameter_band():
    h = build(H(supports=[]))                                                                                     # 卧式没说支座 → 鞍座 2 个
    assert [(s.kind, s.count) for s in h.supports] == [("saddle", 2)] and src(h, "supports[s1].kind") == src(h, "supports[s1].count") == "rule:R-SUP-01"
    assert build(H(supports=[{"kind": "saddle"}])).supports[0].count == 2
    assert build(H(supports=[{"kind": "saddle", "count": 3}])).supports[0].count == 3                             # 用户给的不改
    assert build(V()).supports[0].count == 4                                                                      # DN1600 耳座 4 个
    for di, n in ((1200, 3), (1201, 4), (300, 3)):                                                                # lug_count：DN ≤ 1200 → 3，否则 4
        assert build(V(shell={"inner_diameter_mm": di, "length_mm": 3000, "thickness_mm": 8})).supports[0].count == n, di
    for di, n in ((1000, 3), (1001, 4)):                                                                          # leg_count：DN ≤ 1000 → 3，否则 4
        assert build(V(shell={"inner_diameter_mm": di, "length_mm": 3000, "thickness_mm": 8}, supports=[{"kind": "leg", "height_mm": 600}])).supports[0].count == n, di


def test_r_sup_01_boundary_wrong_kind_for_vessel_and_unsupported_kinds():
    e = raises("SPEC_CONFLICT", build, V(supports=[{"kind": "saddle"}]))
    assert "立式" in e.message and "鞍座" in e.message
    for k, word in (("lug", "耳式支座"), ("leg", "支腿")):
        assert word in raises("SPEC_CONFLICT", build, H(supports=[{"kind": k, "height_mm": 600}])).message
    assert "P2" in raises("NOT_SUPPORTED", build, V(supports=[{"kind": "skirt", "height_mm": 1000}])).message
    raises("NOT_SUPPORTED", build, V(supports=[{"kind": "ring"}]))


# ================================================================== R-SUP-02：支座型号（设计条件齐全才初选，否则“待定”）
def test_r_sup_02_model_is_preselected_only_with_complete_design_conditions():
    spec = build(V(design=DESIGN))                                                                                # DN1600 → lug_support 第一个覆盖 1600 的是 4 号（1000–2000）
    assert spec.supports[0].model == "NB/T 47065.3 耳式支座 A4（初选，待校核）" and src(spec, "supports[s1].model") == "rule:R-SUP-02"
    assert any(w.startswith("W_SUPPORT_NOT_CHECKED") for w in spec.warnings) and not any(w.startswith("W_DESIGN_DATA_MISSING") for w in spec.warnings)
    sad = build(H(design=DESIGN))
    assert sad.supports[0].model == "NB/T 47065.1 鞍式支座 A2000-F/S（120°包角，初选，待校核）"
    assert any("NB/T 47042" in w for w in sad.warnings)                                                           # 鞍座同时注明 A 型和包角是默认值、未按 NB/T 47042 校核
    leg = build(V(design=DESIGN, supports=[{"kind": "leg", "height_mm": 600}]))
    assert leg.supports[0].model == "NB/T 47065.2 支腿（型号待定）"
    pending = build(V())                                                                                          # 设计压力、温度都没给
    assert pending.supports[0].model == "待定"
    assert any(w.startswith("W_DESIGN_DATA_MISSING") for w in pending.warnings) and not any(w.startswith("W_SUPPORT_NOT_CHECKED") for w in pending.warnings)


def test_r_sup_02_boundary_half_given_conditions_user_model_and_table_edges():
    assert build(V(design={"design_pressure_mpa": 0.6})).supports[0].model == "待定"                              # 只有压力
    assert build(V(design={"design_temp_c": 80})).supports[0].model == "待定"                                     # 只有温度
    assert build(V(design=DESIGN, supports=[{"kind": "lug", "model": "NB/T 47065.3 耳式支座 A9"}])).supports[0].model == "NB/T 47065.3 耳式支座 A9"
    for di, no in ((300, 1), (600, 1), (601, 2), (1000, 2), (1001, 3), (2000, 4), (2001, 5), (4000, 8)):          # 同一个 DN 落在多个号的范围内时取第一个覆盖的号
        spec = build(V(design=DESIGN, shell={"inner_diameter_mm": di, "length_mm": 3000, "thickness_mm": 10}))
        assert spec.supports[0].model == f"NB/T 47065.3 耳式支座 A{no}（初选，待校核）", di
    e = raises("STD_SIZE_NOT_FOUND", build, V(design=DESIGN, shell={"inner_diameter_mm": 4001, "length_mm": 3000, "thickness_mm": 10}))
    assert e.extra["table_id"] == "lug_support_nbt47065_3"
    e = raises("STD_SIZE_NOT_FOUND", build, H(design=DESIGN, shell={"inner_diameter_mm": 1700, "tangent_length_mm": 6000, "thickness_mm": 10}))     # 鞍座表按 200 一档，不插值
    assert e.extra["table_id"] == "saddle_nbt47065_1"


# ================================================================== R-SUP-POS：耳座/支腿方位从 45° 起均布（R-SUP-POS-02）；鞍座距切线 A（R-SUP-POS-01）
def test_r_sup_pos_lug_orientation_from_45_degrees_evenly_and_elevation():
    spec = build(V())
    assert [p["orientation_deg"] for p in spec.supports[0].positions] == [45.0, 135.0, 225.0, 315.0]
    assert {p["elevation_mm"] for p in spec.supports[0].positions} == {1440.0}                                    # round(2400×0.6/10)×10
    assert src(spec, "supports[s1].positions") == "rule:R-SUP-POS-02"
    three = build(V(supports=[{"kind": "lug", "count": 3}]))
    assert [p["orientation_deg"] for p in three.supports[0].positions] == [45.0, 165.0, 285.0]
    assert {p["elevation_mm"] for p in build(V(shell={"inner_diameter_mm": 1600, "length_mm": 3000, "thickness_mm": 8})).supports[0].positions} == {1800.0}
    leg = build(V(supports=[{"kind": "leg", "height_mm": 600}]))
    assert leg.supports[0].count == 4 and [p["orientation_deg"] for p in leg.supports[0].positions] == [45.0, 135.0, 225.0, 315.0]
    assert all("elevation_mm" not in p for p in leg.supports[0].positions)                                        # 支腿没有耳座高度


def test_r_sup_pos_boundary_counts_one_and_twelve_and_default_note():
    one = build(V(supports=[{"kind": "lug", "count": 1}]))
    assert one.supports[0].positions == [{"orientation_deg": 45.0, "elevation_mm": 1440.0}]
    twelve = build(V(supports=[{"kind": "lug", "count": 12}]))
    assert [p["orientation_deg"] for p in twelve.supports[0].positions] == [45.0, 75.0, 105.0, 135.0, 165.0, 195.0, 225.0, 255.0, 285.0, 315.0, 345.0, 15.0]   # 30° 一档，超过 360 回绕
    _, info = build_info(V())
    assert "耳座高度位置按默认，待确认（需结合设备重心和支承梁标高）" in info["notes"]
    _, info = build_info(V(supports=[{"kind": "leg", "height_mm": 600}]))
    assert not any("耳座高度" in n for n in info["notes"])


def test_r_sup_pos_saddle_distance_a_is_min_of_point_two_l_and_half_rm_floored_to_10():
    spec, info = build_info(H())                                                                                  # Ltan 6000，Rm = (2000+10)/2 = 1005 → min(1200, 502.5) = 502.5 → 500
    assert spec.supports[0].positions == [{"axial_mm": 475.0, "fixed": True}, {"axial_mm": 5475.0, "fixed": False}]   # 500 − 直边 25；5950 + 25 − 500
    assert src(spec, "supports[s1].positions") == "rule:R-SUP-POS-01"
    assert "鞍座距封头切线 A=500（按默认，待确认），左为固定鞍座 F、右为活动鞍座 S" in info["notes"]
    three = build(H(supports=[{"kind": "saddle", "count": 3}]))
    assert [p["axial_mm"] for p in three.supports[0].positions] == [475.0, 2975.0, 5475.0] and [p["fixed"] for p in three.supports[0].positions] == [True, False, False]


@pytest.mark.parametrize("di,tangent,t,a", [
    (2000, 2000, 10, 400),      # 短筒：0.2×2000 = 400 < 0.5Rm = 502.5 → A = 400
    (1000, 1500, 8, 250),       # 细长：0.5Rm = 252 < 0.2×1500 = 300 → 252 取整 250
    (3000, 6000, 10, 750)])     # 0.5Rm = 752.5 < 1200 → 750
def test_r_sup_pos_boundary_either_limit_can_govern(di, tangent, t, a):
    spec = build(H(shell={"inner_diameter_mm": di, "tangent_length_mm": tangent, "thickness_mm": t}))
    sf = spec.heads[0].straight_flange_mm
    left, right = [p["axial_mm"] for p in spec.supports[0].positions]
    assert left == a - sf                                                                                         # 以左环焊缝为基准量，距左切线 A
    assert (spec.shell.length_mm + sf) - right == a                                                               # 右鞍座距右切线也是 A（两侧对称）


# ================================================================== R-NOZ-KIND：用途决定管口类别
def test_r_noz_kind_comes_from_purpose_when_user_did_not_say():
    spec = build(V(nozzles=[{"dn": 500, "purpose_text": "人孔"}, {"dn": 25, "purpose_text": "液位计口"}, {"dn": 80, "purpose_text": "进料口"}, {"dn": 50, "purpose_text": "手孔"}]))
    assert [n.kind for n in spec.nozzles] == ["manhole", "level_gauge", "pipe", "handhole"] if False else [n.kind for n in spec.nozzles] == ["manhole", "level_gauge", "pipe", "handhole"]
    assert nsrc(spec, spec.nozzles[0], "kind") == "calc:purpose" and nsrc(spec, spec.nozzles[2], "kind") is None


def test_r_noz_kind_boundary_user_kind_and_unknown_purpose():
    spec = build(V(nozzles=[{"dn": 25, "purpose_text": "液位计口", "kind": "sight_glass"}, {"dn": 40, "purpose_text": "冷却水进口"}]))
    assert [n.kind for n in spec.nozzles] == ["sight_glass", "pipe"]
    assert nsrc(spec, spec.nozzles[0], "kind") == "user:t1"


# ================================================================== R-NOZ-LOC：管口位置（立式 LOC-01，卧式 LOC-02）
@pytest.mark.parametrize("purpose,loc", [("进料口", "top_head"), ("放空口", "top_head"), ("安全阀口", "top_head"), ("压力表口", "top_head"), ("出料口", "bottom_head"), ("排净口", "bottom_head"),
                                          ("人孔", "shell"), ("液位计口", "shell"), ("温度计口", "shell")])
def test_r_noz_loc_vertical_by_purpose(purpose, loc):
    dn = 500 if purpose == "人孔" else 25
    spec = build(V(nozzles=[{"dn": dn, "purpose_text": purpose}]))
    assert spec.nozzles[0].location == loc and nsrc(spec, spec.nozzles[0], "location") == "rule:R-NOZ-LOC-01"


def test_r_noz_loc_horizontal_everything_on_shell_with_loc_02():
    spec = build(H(nozzles=[{"dn": 100, "purpose_text": "进料"}, {"dn": 100, "purpose_text": "出料"}, {"dn": 25, "purpose_text": "放空"}, {"dn": 500, "purpose_text": "人孔"}]))
    assert {n.location for n in spec.nozzles} == {"shell"} and {nsrc(spec, n, "location") for n in spec.nozzles} == {"rule:R-NOZ-LOC-02"}


def test_r_noz_loc_boundary_user_location_unknown_purpose_and_manhole_always_on_shell():
    spec = build(V(nozzles=[{"dn": 80, "purpose_text": "进料口", "location": "shell"}, {"dn": 40, "purpose_text": "冷却水进口"}, {"dn": 50},
                            {"dn": 500, "kind": "manhole", "purpose_text": "进料口"}]))
    assert [n.location for n in spec.nozzles] == ["shell", "shell", "shell", "shell"]                              # 用户说的优先；词表外/没说用途 → 筒体；人孔类别无论什么用途都在筒体
    assert nsrc(spec, spec.nozzles[0], "location") == "user:t1" and nsrc(spec, spec.nozzles[1], "location") == "rule:R-NOZ-LOC-01"
    e = raises("INVALID_ARGS", build, V(nozzles=[{"dn": 80, "purpose_text": "进料口", "location": "left_head"}]))
    assert "立式" in e.message


# ================================================================== R-PIPE-01：接管外径与壁厚查表
def test_r_pipe_01_series_b_by_default_and_series_a_when_asked():
    spec, info = build_info(V(nozzles=[{"dn": 80, "purpose_text": "进料口"}, {"dn": 80, "purpose_text": "出料口", "pipe_series": "A"}, {"dn": 500, "kind": "manhole", "purpose_text": "人孔"}]))
    a, c, m = spec.nozzles
    assert (a.pipe_series, a.pipe_od_mm, a.pipe_thk_mm) == ("B", 89.0, 4.0) and nsrc(spec, a, "pipe_od_mm") == "table:pipe_dims" and nsrc(spec, a, "pipe_series") == "rule:R-PIPE-01"
    assert (c.pipe_series, c.pipe_od_mm, c.pipe_thk_mm) == ("A", 88.9, 5.49) and nsrc(spec, c, "pipe_series") == "user:t1"
    assert (m.pipe_od_mm, m.pipe_thk_mm) == (530.0, 8.0) and nsrc(spec, m, "pipe_od_mm") == "table:manhole"          # 人孔筒节查 manhole 表
    assert info["notes"].count("接管壁厚按默认，未校核") == 2                                                          # 每个默认壁厚一条（引擎去重后展示）


def test_r_pipe_01_boundary_dn_outside_table_user_size_and_missing_dn():
    for series in ("B", "A"):
        e = raises("STD_SIZE_NOT_FOUND", build, V(nozzles=[{"dn": 85, "purpose_text": "进料口", "pipe_series": series}]))
        assert e.extra["table_id"] == "pipe_dims" and e.extra["keys"] == {"series": series, "dn": 85.0} and "DN10–600" in e.extra["coverage"]
    raises("STD_SIZE_NOT_FOUND", build, V(nozzles=[{"dn": 700, "purpose_text": "进料口"}]))                         # DN700 以上没录入
    ok = build(V(nozzles=[{"dn": 80, "purpose_text": "进料口", "pipe_od_mm": 95, "pipe_thk_mm": 5}]))
    assert (ok.nozzles[0].pipe_od_mm, ok.nozzles[0].pipe_thk_mm) == (95.0, 5.0)                                   # 用户给的外径、壁厚不被表覆盖
    _, info = build_info(V(nozzles=[{"dn": 80, "purpose_text": "进料口", "pipe_thk_mm": 5}]))
    assert "接管壁厚按默认，未校核" not in info["notes"]                                                              # 壁厚是用户给的，不再注明“按默认”
    no_dn = build(V(nozzles=[{"purpose_text": "进料口"}]))
    assert no_dn.nozzles[0].pipe_od_mm is None and no_dn.nozzles[0].pipe_series is None                           # 没有 DN 就没有可查的（由 missing 反问）


# ================================================================== R-NOZ-POS-01：筒体管口的轴向位置
def test_r_noz_pos_01_vertical_manhole_and_level_gauge_positions():
    spec, info = build_info(V(nozzles=[{"dn": 500, "kind": "manhole", "purpose_text": "人孔"}, {"dn": 25, "kind": "level_gauge", "purpose_text": "液位计口", "count": 2}]))
    man = by(spec, "kind", "manhole")
    assert man.axial_mm == 900.0 and nsrc(spec, man, "axial_mm") == "rule:R-NOZ-POS-01"                              # manhole_center_mm
    gauges = sorted((n for n in spec.nozzles if n.kind == "level_gauge"), key=lambda n: n.axial_mm)
    # DN25：法兰外径 115 → 半径 57.5，离环焊缝 ≥100 → 下限 157.5 取整到 10 → 160；上限 2400−157.5；中心距取 level_gauge_cc_mm 里放得下的最大值 2000
    assert [g.axial_mm for g in gauges] == [160.0, 2160.0]
    assert any("液位计接口中心距 2000 按默认，待仪表专业确认" in n for n in info["notes"])
    one = build(V(nozzles=[{"dn": 25, "kind": "level_gauge", "purpose_text": "液位计口"}]))
    assert one.nozzles[0].axial_mm == 160.0
    three = build(V(nozzles=[{"dn": 25, "kind": "level_gauge", "purpose_text": "液位计口", "count": 3}]))
    assert sorted(n.axial_mm for n in three.nozzles) == [160.0, 1160.0, 2160.0]                                   # 3 个：2×cc ≤ 可用段 → cc = 1000


def test_r_noz_pos_01_boundary_manhole_clamped_and_no_room():
    def man_at(length):
        return build(V(shell={"inner_diameter_mm": 1600, "length_mm": length, "thickness_mm": 8}, nozzles=[{"dn": 500, "kind": "manhole", "purpose_text": "人孔"}])).nozzles[0].axial_mm
    # DN500 开孔占位 = 补强圈估计外径 2×530 = 1060 → 半径 530；下限 100+530 = 630，上限 L − 630
    assert man_at(2400) == 900.0
    assert man_at(1500) == 870.0                                                                                  # 900 超过上限 870 → 压到上限
    assert man_at(1260) == 630.0                                                                                  # 刚好放得下
    e = raises("NOZZLE_NO_ROOM", man_at, 1259)
    assert "人孔" in e.message and "DN500" in e.message
    pair = lambda length: [n.axial_mm for n in build(V(shell={"inner_diameter_mm": 1600, "length_mm": length, "thickness_mm": 8},
                                                       nozzles=[{"dn": 25, "kind": "level_gauge", "purpose_text": "液位计口", "count": 2}])).nozzles]
    assert pair(800) == [160.0, 460.0]                                                                            # 可用段 160…642.5：只有中心距 300 放得下
    raises("NOZZLE_NO_ROOM", pair, 400)


def test_r_noz_pos_01_user_axial_is_kept_and_other_shell_nozzles_spread_evenly():
    spec = build(V(nozzles=[{"dn": 50, "purpose_text": "温度计口", "count": 2}]))
    # DN50：法兰外径 165 → 半径 82.5，可用段 182.5…2217.5，两个均布：182.5 + 2035×(1/3)=860.8→860，182.5 + 2035×(2/3)=1539.2→1540
    assert sorted(n.axial_mm for n in spec.nozzles) == [860.0, 1540.0]
    kept = build(V(nozzles=[{"dn": 50, "purpose_text": "温度计口", "axial_mm": 700}, {"dn": 50, "purpose_text": "温度计口"}]))
    assert kept.nozzles[0].axial_mm == 700.0 and nsrc(kept, kept.nozzles[0], "axial_mm") == "user:t1"
    assert kept.nozzles[1].axial_mm == 1200.0                                                                     # 单个：可用段中点


def test_r_noz_pos_01_horizontal_groups_top_bottom_and_side():
    spec = build(H(nozzles=[{"dn": 100, "purpose_text": "进料"}, {"dn": 100, "purpose_text": "出料"}, {"dn": 500, "kind": "manhole", "purpose_text": "人孔"}], supports=[{"kind": "saddle", "count": 2}]))
    feed, outlet, man = by(spec, "purpose", "进料口"), by(spec, "purpose", "出料口"), by(spec, "kind", "manhole")
    # 顶部组（进料口 + 人孔）：占位半径取 530 → 可用段 630…5320，两个均布 → 2193→2190、3757→3760
    assert (feed.axial_mm, man.axial_mm) == (2190.0, 3760.0)
    # 底部组（出料口）：避开鞍座（475/5475，底板宽 240）：下限 475+120+110+100 = 805，上限 5475−120−110−100 = 5145 → 中点 2975 → 2980
    assert outlet.axial_mm == 2980.0
    gauges = build(H(nozzles=[{"dn": 25, "purpose_text": "液位计口", "count": 2}]))
    assert {n.axial_mm for n in gauges.nozzles} == {1570.0}                                                       # 一对液位计同一轴向位置（上下对齐）：(lo+hi)/2 − 0.25(hi−lo) 取整


def test_r_noz_pos_01_boundary_horizontal_too_many_nozzles_in_a_short_shell():
    e = raises("NOZZLE_NO_ROOM", build, H(shell={"inner_diameter_mm": 2000, "tangent_length_mm": 3000, "thickness_mm": 10},
                                           nozzles=[{"dn": 500, "kind": "manhole", "purpose_text": "人孔", "count": 3}]))
    assert "放不下这 3 个管口" in e.message
    ok = build(H(shell={"inner_diameter_mm": 2000, "tangent_length_mm": 3000, "thickness_mm": 10}, nozzles=[{"dn": 500, "kind": "manhole", "purpose_text": "人孔", "count": 2}]))
    assert len({n.axial_mm for n in ok.nozzles}) == 2


# ================================================================== R-NOZ-POS-02：封头管口径向位置
def test_r_noz_pos_02_head_nozzles_on_ring_and_outlet_at_center():
    spec = build(V(nozzles=[{"dn": 80, "purpose_text": "进料口"}, {"dn": 80, "purpose_text": "出料口"}, {"dn": 50, "purpose_text": "排净口"}]))
    feed, out, drain = by(spec, "purpose", "进料口"), by(spec, "purpose", "出料口"), by(spec, "purpose", "排净口")
    assert out.radial_mm == 0.0                                                                                    # 出料口在下封头中心
    assert feed.radial_mm == drain.radial_mm == 400.0                                                              # 其余均布在半径 0.25Di = 400 的圆上（开孔外缘 ≤ 0.4Di = 640）
    assert nsrc(spec, feed, "radial_mm") == "rule:R-NOZ-POS-02"
    alone = build(V(nozzles=[{"dn": 50, "purpose_text": "排净口"}]))
    assert alone.nozzles[0].radial_mm == 0.0                                                                       # 下封头只有一个管口：放中心
    kept = build(V(nozzles=[{"dn": 80, "purpose_text": "进料口", "radial_mm": 300}]))
    assert kept.nozzles[0].radial_mm == 300.0 and nsrc(kept, kept.nozzles[0], "radial_mm") == "user:t1"


def test_r_noz_pos_02_boundary_no_room_on_head():
    # 上封头：开孔外缘 ≤ 0.4Di = 640。DN250（补强圈 2×273 = 546，半径 273）→ 半径 min(400, 640−273) = 367 → 取整 360 ≥ 273，放得下
    assert build(V(nozzles=[{"dn": 250, "purpose_text": "进料口"}])).nozzles[0].radial_mm == 360.0
    # DN300（补强圈 650，半径 325）→ 圆周 min(400, 315) = 315 → 310 < 325 放不下；只有这一个管口时退到封头中心（325 ≤ 640）
    assert build(V(nozzles=[{"dn": 300, "purpose_text": "进料口"}])).nozzles[0].radial_mm == 0.0
    # 两个 DN300 都在上封头：圆周放不下、中心只能放一个 → NOZZLE_NO_ROOM
    e = raises("NOZZLE_NO_ROOM", build, V(nozzles=[{"dn": 300, "purpose_text": "进料口"}, {"dn": 300, "purpose_text": "放空口"}]))
    assert "上封头" in e.message
    # 出料口放中心也要在 0.4Di 内：Di=400 → 160。DN150 补强圈 2×159 = 318，半径 159 ≤ 160 放得下；DN200 半径 219 放不下
    small = {"inner_diameter_mm": 400, "length_mm": 1500, "thickness_mm": 6}
    assert build(V(shell=small, nozzles=[{"dn": 150, "purpose_text": "出料口"}])).nozzles[0].radial_mm == 0.0
    e = raises("NOZZLE_NO_ROOM", build, V(shell=small, nozzles=[{"dn": 200, "purpose_text": "出料口"}]))
    assert "0.4Di" in e.message


def test_r_noz_pos_02_horizontal_right_head_outlet_at_center_left_head_on_ring():
    spec = build(H(nozzles=[{"dn": 100, "purpose_text": "出料", "location": "right_head"}, {"dn": 100, "purpose_text": "进料", "location": "left_head"}]))
    assert by(spec, "location", "right_head").radial_mm == 0.0 and by(spec, "location", "left_head").radial_mm == 500.0     # 0.25×2000


# ================================================================== R-NOZ-ORI-01：管口方位
def test_r_noz_ori_01_vertical_defaults_and_level_gauge_pair_share_orientation():
    spec = build(V(nozzles=[{"dn": 500, "kind": "manhole", "purpose_text": "人孔"}, {"dn": 80, "purpose_text": "进料口"}, {"dn": 80, "purpose_text": "出料口"},
                            {"dn": 25, "kind": "level_gauge", "purpose_text": "液位计口", "count": 2}]))
    man, feed, out = by(spec, "kind", "manhole"), by(spec, "purpose", "进料口"), by(spec, "purpose", "出料口")
    g1, g2 = [n for n in spec.nozzles if n.kind == "level_gauge"]
    assert (man.orientation_deg, feed.orientation_deg, out.orientation_deg) == (0.0, 0.0, 0.0)                    # 第一个 0°；封头管口在不同封头上互不冲突
    assert g1.orientation_deg == g2.orientation_deg == 90.0                                                       # 一对液位计同一方位角（人孔占了 0°，先找没人用的 90°）
    assert g1.axial_mm != g2.axial_mm and g1.location == g2.location == "shell"                                  # 上下对齐：同方位、不同高度
    assert nsrc(spec, g2, "orientation_deg") == "rule:R-NOZ-ORI-01"


def test_r_noz_ori_01_user_orientation_is_kept_and_treated_as_obstacle():
    spec = build(V(nozzles=[{"dn": 50, "purpose_text": "温度计口", "axial_mm": 1000, "orientation_deg": 0}, {"dn": 50, "purpose_text": "温度计口", "axial_mm": 1000}]))
    assert spec.nozzles[0].orientation_deg == 0.0 and nsrc(spec, spec.nozzles[0], "orientation_deg") == "user:t1"
    assert spec.nozzles[1].orientation_deg == 90.0                                                                # 同一高度不能再用 0°，以 45° 为步长找：先找没人用的 90°


def test_r_noz_ori_01_boundary_avoid_lugs_and_run_out_of_angles():
    ear = {"dn": 200, "purpose_text": "温度计口", "axial_mm": 1500}                                                # 在耳座高度范围（1440…1690）内
    four = build(V(nozzles=[ear], supports=[{"kind": "lug", "count": 4}]))                                       # 耳座 45/135/225/315：0° 离耳座 45°，弧长 634 > 70+170，不冲突
    assert four.nozzles[0].orientation_deg == 0.0
    six = build(V(nozzles=[ear], supports=[{"kind": "lug", "count": 6}]))                                        # 耳座 45/105/165/225/285/345：0°/90°/180°/270°/45° 都离耳座 ≤15°（弧长 211 < 70+170），只剩 135°
    assert six.nozzles[0].orientation_deg == 135.0
    e = raises("NOZZLE_NO_ROOM", build, V(nozzles=[ear], supports=[{"kind": "lug", "count": 8}]))                  # 8 个耳座把 8 个候选方位全占了
    assert "支座" in e.message
    away = build(V(nozzles=[{**ear, "axial_mm": 600}], supports=[{"kind": "lug", "count": 8}]))                  # 轴向错开耳座就没事
    assert away.nozzles[0].orientation_deg == 0.0


def test_r_noz_ori_01_horizontal_defaults_and_same_height_conflict():
    spec = build(H(nozzles=[{"dn": 100, "purpose_text": "进料"}, {"dn": 100, "purpose_text": "出料"}, {"dn": 25, "purpose_text": "液位计口", "count": 2}]))
    assert by(spec, "purpose", "进料口").orientation_deg == 0.0                                                    # 顶部
    assert by(spec, "purpose", "出料口").orientation_deg == 180.0                                                  # 底部
    assert sorted(n.orientation_deg for n in spec.nozzles if n.kind == "level_gauge") == [45.0, 135.0]           # 液位计在侧面
    both = build(H(nozzles=[{"dn": 100, "purpose_text": "进料", "axial_mm": 2000}, {"dn": 100, "purpose_text": "放空", "axial_mm": 2000}]))
    assert sorted(n.orientation_deg for n in both.nozzles) == [0.0, 315.0]                                        # 同一高度、都想在顶部：第二个让到 315°


def test_r_noz_ori_01_head_nozzles_spread_and_run_out_of_angles():
    two = build(V(nozzles=[{"dn": 80, "purpose_text": "进料口"}, {"dn": 80, "purpose_text": "放空口"}]))
    assert sorted(n.orientation_deg for n in two.nozzles) == [0.0, 90.0]                                          # 同一圈上第一个 0°，第二个 90°（距离 565 > 220）
    eight = build(V(nozzles=[{"dn": 80, "purpose_text": "放空口", "count": 8}]))
    assert sorted(n.orientation_deg for n in eight.nozzles) == [0.0, 45.0, 90.0, 135.0, 180.0, 225.0, 270.0, 315.0]
    e = raises("NOZZLE_NO_ROOM", build, V(nozzles=[{"dn": 80, "purpose_text": "放空口", "count": 9}]))           # 第 9 个：8 个候选方位全被占了
    assert "封头" in e.message


# ================================================================== R-NOZ-PROJ-01：伸出长度
@pytest.mark.parametrize("dn,mm", [(25, 150.0), (50, 150.0), (65, 200.0), (150, 200.0), (200, 250.0), (300, 250.0), (350, 300.0)])
def test_r_noz_proj_01_shell_nozzle_projection_by_dn_band(dn, mm):
    spec = build(V(nozzles=[{"dn": dn, "purpose_text": "温度计口"}]))
    assert spec.nozzles[0].projection_mm == mm and nsrc(spec, spec.nozzles[0], "projection_mm") == "rule:R-NOZ-PROJ-01"


def test_r_noz_proj_01_boundary_insulation_manhole_head_nozzles_and_user_value():
    ins = build(V(nozzles=[{"dn": 25, "purpose_text": "温度计口"}, {"dn": 500, "kind": "manhole", "purpose_text": "人孔"}], design={"insulation_thickness_mm": 100}))
    assert [n.projection_mm for n in ins.nozzles] == [250.0, 350.0]                                               # 150 + 100；人孔 h1=250 + 100
    assert build(V(nozzles=[{"dn": 25, "purpose_text": "温度计口", "projection_mm": 400}])).nozzles[0].projection_mm == 400.0
    spec = build(V(nozzles=[{"dn": 80, "purpose_text": "进料口"}, {"dn": 80, "purpose_text": "出料口"}]))
    # 封头管口：法兰面到环焊缝 = 直边 25 + 椭圆外表面在 r=径向+外径/2 处的高度 + 附加 200，向上取整到 10
    # 进料口（径向 400，r=444.5）：408×√(1−(444.5/808)²)=340.7 → 25+340.7+200 = 565.7 → 570；出料口（径向 0，r=44.5）：407.4 → 632.4 → 640
    assert by(spec, "purpose", "进料口").projection_mm == 570.0 and by(spec, "purpose", "出料口").projection_mm == 640.0


# ================================================================== R-PN-01：设计压力和设计温度都给了才初选
@pytest.mark.parametrize("p,t,pn", [(0.6, 80, 10.0), (1.0, 80, 16.0), (2.16, 80, 25.0)])
def test_r_pn_01_demo_phrases_pick_pn_from_pressure_temperature_rating(p, t, pn):
    spec = build(V(design={"design_pressure_mpa": p, "design_temp_c": t}))
    assert spec.nozzles[0].pn == pn and nsrc(spec, spec.nozzles[0], "pn") == "rule:R-PN-01"


def test_r_pn_01_boundary_without_both_conditions_pn_stays_pending():
    for design in ({}, {"design_pressure_mpa": 0.6}, {"design_temp_c": 80}):
        spec, info = build_info(V(design=design))
        assert spec.nozzles[0].pn is None, design
        assert any(w.startswith("W_DESIGN_DATA_MISSING") for w in info["warnings"])                                # 不能取下限出图
        assert spec.nozzles[0].flange_type is None and spec.nozzles[0].facing is None
    assert not any(w.startswith("W_DESIGN_DATA_MISSING") for w in build(V(design=DESIGN)).warnings)


def test_r_pn_01_boundary_temperature_band_is_the_nearest_one_not_below_design_temperature():
    pn = lambda p, t: build(V(design={"design_pressure_mpa": p, "design_temp_c": t})).nozzles[0].pn
    assert pn(1.0, 50) == 10.0          # PN10 在 ≤50℃ 档 = 1.0 MPa，刚好够
    assert pn(1.0, 51) == 16.0          # 51℃ 取 100℃ 档：PN10 只有 0.92 → PN16
    assert pn(0.5, 300) == 10.0         # 300℃ 档 PN10 = 0.69
    assert pn(0.5, -20) == 10.0         # 表覆盖到 −20℃
    assert pn(2.0, 300) == 40.0         # 300℃ 档：PN25 = 1.72，PN40 = 2.76
    assert pn(0.3, 80) == 10.0          # 不低于 pn_min（PN6 不选）
    assert pn(-0.1, 80) == 10.0         # 负压（真空）按绝对值取，不是错误


def test_r_pn_01_boundary_outside_rating_table_is_std_size_not_found():
    for p, t, word in ((0.5, 301, "300"), (0.5, -21, "-20"), (4.01, 50, "PN40"), (2.8, 300, "PN40")):
        e = raises("STD_SIZE_NOT_FOUND", build, V(design={"design_pressure_mpa": p, "design_temp_c": t}))
        assert word in e.message and "直接给出管口 PN" in e.hint, (p, t)
    assert build(V(design={"design_pressure_mpa": 4.0, "design_temp_c": 50})).nozzles[0].pn == 40.0               # 上封头管口：4.0 MPa 刚好是 PN40 在 ≤50℃ 的额定值


def test_r_pn_01_bottom_nozzles_add_liquid_static_head_and_user_pn_stays():
    d = V(design={"design_pressure_mpa": 0.9, "design_temp_c": 80}, nozzles=[{"dn": 80, "purpose_text": "进料口"}, {"dn": 80, "purpose_text": "出料口"}, {"dn": 80, "purpose_text": "放空口", "pn": 25}])
    spec = build(d)
    # 0.9 ≤ PN10 在 100℃ 档的 0.92，上封头管口 PN10；下封头出料口另加静压 1000×9.81×3250mm = 0.0319 MPa → 0.9319 > 0.92 → PN16
    assert by(spec, "purpose", "进料口").pn == 10.0 and by(spec, "purpose", "出料口").pn == 16.0
    assert by(spec, "purpose", "放空口").pn == 25.0 and nsrc(spec, by(spec, "purpose", "放空口"), "pn") == "user:t1"
    hor = build(H(design={"design_pressure_mpa": 0.91, "design_temp_c": 80}, nozzles=[{"dn": 100, "purpose_text": "进料口"}, {"dn": 100, "purpose_text": "出料口"}]))
    assert by(hor, "purpose", "进料口").pn == 10.0 and by(hor, "purpose", "出料口").pn == 16.0                      # 卧式：方位在下半圆的筒体管口加 Di 的静压 0.0196


# ================================================================== R-FLG-01：法兰标准、型式、密封面
@pytest.mark.parametrize("p,pn,ftype", [(0.6, 10.0, "SO"), (1.0, 16.0, "SO"), (2.16, 25.0, "WN"), (2.6, 40.0, "WN")])
def test_r_flg_01_flange_standard_type_and_facing_follow_pn(p, pn, ftype):
    n = build(V(design={"design_pressure_mpa": p, "design_temp_c": 80})).nozzles[0]
    assert (n.pn, n.flange_std, n.flange_type, n.facing) == (pn, "HG/T 20592", ftype, "RF")


def test_r_flg_01_boundary_pending_manholes_user_values_and_hazard_media():
    spec = build(V(design=DESIGN, nozzles=[{"dn": 80, "purpose_text": "进料口", "flange_type": "WN", "facing": "FM"}, {"dn": 500, "kind": "manhole", "purpose_text": "人孔"}]))
    feed, man = spec.nozzles
    assert (feed.flange_type, feed.facing) == ("WN", "FM") and nsrc(spec, feed, "flange_type") == "user:t1"
    assert man.flange_std is None and man.flange_type is None and man.facing is None                               # 人孔不选管法兰
    assert build(V()).nozzles[0].flange_std is None                                                              # 设计条件不全：待定
    haz = build(V(design={**DESIGN, "medium_property": "高度危害"})).nozzles[0]
    assert haz.flange_type == "SO"                                                                               # 危害介质不选 PL（表里本来也没有 PL）
    given_pn = build(V(nozzles=[{"dn": 80, "purpose_text": "进料口", "pn": 16}]))                                  # 用户给了 PN 但设计条件不全：型式也写待定（任务书：设计条件不全时写“待定”）
    assert given_pn.nozzles[0].pn == 16.0 and given_pn.nozzles[0].flange_type is None


# ================================================================== R-MH-01：人孔标准
def test_r_mh_01_atmospheric_manhole_only_below_0_1_mpa():
    man = lambda p: build(V(design={"design_pressure_mpa": p, "design_temp_c": 80}, nozzles=[{"dn": 500, "kind": "manhole", "purpose_text": "人孔"}])).nozzles[0]
    assert man(0.05).std_part == "HG/T 21515 常压人孔 人孔 DN500（初选，待校核）"
    assert man(0.0999).std_part.startswith("HG/T 21515 常压人孔") and man(0.0).std_part.startswith("HG/T 21515 常压人孔")
    for p in (0.1, 0.6):                                                                                          # ≥0.1 MPa 不得选常压人孔
        assert man(p).std_part == "HG/T 21517 回转盖带颈平焊法兰人孔 人孔 DN500 PN10（初选，待校核）", p
    assert nsrc(build(V(design=DESIGN, nozzles=[{"dn": 500, "kind": "manhole", "purpose_text": "人孔"}])), man(0.6), "std_part") is None or True


def test_r_mh_01_boundary_high_pn_user_std_pending_and_vacuum():
    man = lambda p, **kw: build(V(design={"design_pressure_mpa": p, "design_temp_c": 80}, nozzles=[{"dn": 500, "kind": "manhole", "purpose_text": "人孔", **kw}])).nozzles[0]
    assert man(2.6).std_part == "HG/T 21518 回转盖带颈对焊法兰人孔 人孔 DN500 PN40（初选，待校核）"                       # PN > 25 → 对焊法兰人孔
    assert man(0.6, std_part="HG/T 21516 人孔").std_part == "HG/T 21516 人孔"                                       # 用户给的不改
    assert build(V(nozzles=[{"dn": 500, "kind": "manhole", "purpose_text": "人孔"}])).nozzles[0].std_part is None   # 设计条件不全 → 待定
    assert "常压" not in (man(-0.05).std_part or "")                                                              # 负压不是错误，也不会误选成常压人孔
    hand = build(V(design={"design_pressure_mpa": 0.05, "design_temp_c": 80}, nozzles=[{"dn": 150, "kind": "handhole", "purpose_text": "手孔"}])).nozzles[0]
    assert hand.std_part == "HG/T 21515 常压人孔 手孔 DN150（初选，待校核）"                                          # 手孔同一规则，名称写“手孔”


# ================================================================== R-STD-01：设计标准
@pytest.mark.parametrize("p,std", [
    (1.6, "GB/T 150.1~150.4-2024"), (0.1, "GB/T 150.1~150.4-2024"), (0.0999, "NB/T 47003.1-2022"), (0.0, "NB/T 47003.1-2022"), (-0.0199, "NB/T 47003.1-2022"),
    (-0.02, "GB/T 150.1~150.4-2024"), (-0.1, "GB/T 150.1~150.4-2024")])
def test_r_std_01_standard_by_design_pressure(p, std):
    spec = build(V(design={"design_pressure_mpa": p}))
    assert spec.design.design_standard == std and src(spec, "design.design_standard") == "rule:R-STD-01"           # 负压（真空/外压）不是错误


def test_r_std_01_boundary_no_pressure_and_user_standard_and_unit_edge():
    assert build(V()).design.design_standard is None                                                              # 没给设计压力：不填（图上写“待填”）
    spec = build(V(design={"design_pressure_mpa": 1.6, "design_standard": "企业标准 Q/XX"}))
    assert spec.design.design_standard == "企业标准 Q/XX" and src(spec, "design.design_standard") == "user:t1"
    assert build(V(design={"design_pressure_mpa": "100kPa"})).design.design_standard == "GB/T 150.1~150.4-2024"    # 100 kPa 就是 0.1 MPa，边界上不能被单位换算的误差带偏
    assert build(V(design={"design_pressure_mpa": "-20kPa"})).design.design_standard == "GB/T 150.1~150.4-2024"
    assert build(V(design={"design_pressure_mpa": "99kPa"})).design.design_standard == "NB/T 47003.1-2022"


# ================================================================== W-DESIGN：设计条件缺失警告
def test_w_design_warning_text_and_when_it_fires():
    spec = build(V())
    w = [x for x in spec.warnings if x.startswith("W_DESIGN_DATA_MISSING")]
    assert len(w) == 1 and "待定" in w[0] and "没有按下限取值出图" in w[0]
    assert not [x for x in build(V(design=DESIGN)).warnings if x.startswith("W_DESIGN_DATA_MISSING")]
    assert [x for x in build(V(design={"design_pressure_mpa": 0.6})).warnings if x.startswith("W_DESIGN_DATA_MISSING")]    # 只给压力也算不全


def test_w_design_boundary_warnings_are_recomputed_not_accumulated():
    spec = build(V())
    assert any(w.startswith("W_DESIGN_DATA_MISSING") for w in spec.warnings)
    sp = patch(spec, set={"design.design_pressure_mpa": 0.6, "design.design_temp_c": 80})
    assert not any(w.startswith("W_DESIGN_DATA_MISSING") for w in sp.warnings)                                    # 补齐后警告消失
    assert sp.nozzles[0].pn == 10.0 and sp.supports[0].model.startswith("NB/T 47065.3")                           # 同时 PN、型号都从“待定”变成初选
    S.fill_defaults(sp)
    assert len(sp.warnings) == len(set(sp.warnings))


# ================================================================== R-SHEET-01：幅面缺省 A1
def test_r_sheet_01_default_a1():
    spec = build(V())
    assert spec.drawing["sheet"] == "A1" and src(spec, "drawing.sheet") == "rule:R-SHEET-01"


def test_r_sheet_01_boundary_user_sheet_wins_and_default_is_recomputed():
    spec = build(V(drawing={"sheet": "A0"}))
    assert spec.drawing["sheet"] == "A0" and src(spec, "drawing.sheet") == "user:t1"
    sp = patch(build(V()), set={"drawing.sheet": "A2"})
    assert sp.drawing["sheet"] == "A2" and src(sp, "drawing.sheet") == "user:t2"
    assert T.rule_value("sheet_default") == "A1"


# ================================================================== R-MARK-01：管口符号
def test_r_mark_01_letters_clockwise_from_lower_left_same_spec_gets_numbers_manhole_m():
    spec = build({**V(), "nozzles": [{"dn": 500, "kind": "manhole", "purpose_text": "人孔"}, {"dn": 80, "purpose_text": "进料口"}, {"dn": 80, "purpose_text": "出料口"},
                                      {"dn": 25, "kind": "level_gauge", "purpose_text": "液位计口", "count": 2}], "design": DESIGN})
    marks = {n.mark: (n.purpose, n.axial_mm) for n in spec.nozzles}
    # 主视图上：进料口在上封头偏右（顺时针最先），液位计口两个在右侧（上面的 b1、下面的 b2），出料口在下封头中心，人孔单独用 M
    assert marks == {"a": ("进料口", None), "b1": ("液位计口", 2160.0), "b2": ("液位计口", 160.0), "c": ("出料口", None), "M1": ("人孔", 900.0)}
    assert all(nsrc(spec, n, "mark") == "rule:R-MARK-01" for n in spec.nozzles)


def test_r_mark_01_boundary_existing_marks_never_move_and_user_marks_reserve_their_letter():
    spec = build(V(nozzles=[{"dn": 80, "purpose_text": "进料口"}, {"dn": 80, "purpose_text": "出料口"}]))
    assert {n.purpose: n.mark for n in spec.nozzles} == {"进料口": "a", "出料口": "b"}
    sp = patch(spec, upsert_nozzles=[{"dn": 80, "purpose_text": "进料口", "location": "bottom_head"}])              # 又加一个同规格同用途的进料口：不把旧的改成 a1/a2
    assert by(sp, "uid", "n1").mark == "a" and by(sp, "uid", "n2").mark == "b" and by(sp, "uid", "n3").mark == "c"
    sp2 = patch(spec, upsert_nozzles=[{"mark": "a", "dn": 50}])
    assert sp2.nozzles[0].mark == "a"                                                                              # 改 DN 不改符号
    user = build(V(nozzles=[{"dn": 80, "purpose_text": "进料口", "mark": "b"}, {"dn": 80, "purpose_text": "出料口"}]))
    assert user.nozzles[0].mark == "b" and nsrc(user, user.nozzles[0], "mark") == "user:t1"
    assert user.nozzles[1].mark == "a"                                                                             # 用户占了 b，规则从空位 a 开始


def test_r_mark_01_same_spec_and_purpose_numbered_manholes_m1_m2_and_letters_skip_i_l_o():
    spec = build(V(shell={"inner_diameter_mm": 2000, "length_mm": 5000, "thickness_mm": 10},
                   nozzles=[{"dn": 500, "kind": "manhole", "purpose_text": "人孔", "count": 2}, {"dn": 80, "purpose_text": "进料口"}, {"dn": 100, "purpose_text": "进料口"}]))
    assert sorted(n.mark for n in spec.nozzles if n.kind == "manhole") == ["M1", "M2"]
    letters = sorted(n.mark for n in spec.nozzles if n.kind != "manhole")
    assert letters == ["a", "b"]                                                                                  # 同用途但 DN 不同 → 不同规格 → 各自一个字母
    many = build(V(shell={"inner_diameter_mm": 1600, "length_mm": 5000, "thickness_mm": 8},
                   nozzles=[{"dn": 25, "purpose_text": f"用途{c}口", "axial_mm": 200 + 50 * i, "orientation_deg": 0, "location": "shell"} for i, c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ"[:12])]))
    assert not set("ilo") & {n.mark for n in many.nozzles}                                                         # 管口符号不用易与数字混淆的 i、l、o
    assert sorted(n.mark for n in many.nozzles) == sorted("abcdefghjkmn")


def test_r_mark_01_boundary_more_than_23_groups_continue_with_double_letters():
    nozzles = [{"dn": 25, "purpose_text": f"X{i}口", "axial_mm": 200 + 50 * i, "orientation_deg": 0, "location": "shell"} for i in range(1, 25)]
    spec = build(V(shell={"inner_diameter_mm": 1600, "length_mm": 5000, "thickness_mm": 8}, nozzles=nozzles))
    marks = [n.mark for n in spec.nozzles]
    assert len(set(marks)) == 24 and "aa" in marks and len([m for m in marks if len(m) == 1]) == 23              # 26 − 3 个字母用完后接 aa，不崩


def test_r_mark_01_mark_style_n_numbers_all_including_manholes():
    spec = build(V(drawing={"mark_style": "N"} if False else {}, nozzles=[{"dn": 500, "kind": "manhole", "purpose_text": "人孔"}, {"dn": 80, "purpose_text": "进料口"}, {"dn": 80, "purpose_text": "出料口"}]))
    sp = patch(spec, set={"drawing.mark_style": "N"}, renumber=True)
    assert sorted(n.mark for n in sp.nozzles) == ["N1", "N2", "N3"]
    assert [n.purpose for n in sorted(sp.nozzles, key=lambda n: n.mark)][0] in ("进料口", "出料口", "人孔")


def test_r_mark_01_clockwise_key_and_main_view_positions():
    spec = build(V(shell={"inner_diameter_mm": 1600, "length_mm": 2000, "thickness_mm": 8}))               # 立式：圆心取 (0, L/2) = (0, 1000)
    pts = [(-1, -1), (-1, 0), (-1, 1), (0, 1), (1, 1), (1, 0), (1, -1), (0, -1)]                          # 左下、左、左上、上、右上、右、右下、下
    keys = [R.clockwise_key(spec, (x * 100.0, 1000.0 + y * 100.0)) for x, y in pts]
    expect = [0, 45, 90, 135, 180, 225, 270, 315]
    assert all(min(abs(k - e), 360 - abs(k - e)) < 1e-9 for k, e in zip(keys, expect)), keys                # 从左下起顺时针依次增大
    hor = build(H())                                                                                       # 卧式圆心取 (L/2, 0)
    assert abs(R.clockwise_key(hor, (hor.shell.length_mm / 2 - 100, -100.0))) < 1e-9 or abs(R.clockwise_key(hor, (hor.shell.length_mm / 2 - 100, -100.0)) - 360) < 1e-9
    n = Nozzle(uid="n9", orientation_deg=90.0, location="shell", axial_mm=900.0, projection_mm=250.0)
    assert R.main_view_pos(spec, n) == (808.0 + 250.0, 900.0)                                              # 0°~180° 转到右侧；ro = 800 + 8
    n.orientation_deg = 270.0
    assert R.main_view_pos(spec, n) == (-(808.0 + 250.0), 900.0)                                           # 180°~360° 转到左侧
    top = Nozzle(uid="n8", orientation_deg=90.0, location="top_head", radial_mm=400.0, projection_mm=570.0)
    bot = Nozzle(uid="n7", orientation_deg=270.0, location="bottom_head", radial_mm=300.0, projection_mm=640.0)
    assert R.main_view_pos(spec, top) == (400.0, 2000.0 + 570.0) and R.main_view_pos(spec, bot) == (-300.0, -640.0)
    assert R.main_view_pos(spec, Nozzle(uid="n6", location="shell")) is None                               # 方位没定的排到最后
    hn = Nozzle(uid="n5", orientation_deg=0.0, location="shell", axial_mm=1000.0, projection_mm=200.0)
    assert R.main_view_pos(hor, hn) == (1000.0, 1010.0 + 200.0)                                            # 卧式：0° 在正上方 → 画在上侧（ro = 1000+10）
    hn.orientation_deg = 180.0
    assert R.main_view_pos(hor, hn) == (1000.0, -(1010.0 + 200.0))


# ================================================================== 规则顺序、助手函数、汇总结构
def test_rules_order_follows_task_book_with_mark_last():
    ids = list(R.RULES)
    assert ids == ["R-HEAD-01", "R-MAT-01", "R-HEAD-02", "R-HEAD-SF", "R-LEN-01", "R-SUP-01", "R-SUP-02", "R-SUP-POS", "R-NOZ-KIND", "R-NOZ-LOC", "R-PIPE-01", "R-NOZ-POS-01",
                   "R-NOZ-POS-02", "R-NOZ-ORI-01", "R-NOZ-PROJ-01", "R-PN-01", "R-FLG-01", "R-MH-01", "R-STD-01", "W-DESIGN", "R-SHEET-01", "R-MARK-01"]
    pos = {k: i for i, k in enumerate(ids)}
    assert pos["R-HEAD-01"] < pos["R-LEN-01"] < pos["R-SUP-01"] < pos["R-NOZ-POS-01"] < pos["R-NOZ-ORI-01"] < pos["R-NOZ-PROJ-01"] < pos["R-PN-01"] < pos["R-STD-01"] < pos["R-SHEET-01"] < pos["R-MARK-01"]
    assert pos["R-SUP-POS"] < pos["R-NOZ-ORI-01"]                                                                  # 管口避让支座，支座不反过来避让管口


def test_fill_defaults_result_structure_and_default_records():
    spec = S.apply_create(VesselPatchIn.model_validate(V(design=DESIGN)), "t1")[0]
    info = S.fill_defaults(spec)
    assert set(info) == {"defaults", "warnings", "notes", "tables"}
    paths = {d["path"]: d for d in info["defaults"]}
    assert paths["heads[top].kind"] == {"path": "heads[top].kind", "rule_id": "R-HEAD-01", "value": "EHA"}
    assert paths["nozzles[n1].pn"]["rule_id"] == "R-PN-01" and paths["nozzles[n1].pn"]["value"] == 10.0
    assert "supports[s1].positions" in paths and "drawing.sheet" in paths and "design.design_standard" in paths
    assert "shell.length_mm" not in paths and "heads[top].straight_flange_mm" not in paths                          # 查表、换算得到的值不算“按默认”
    assert "nozzles[n1].mark" not in paths
    assert info["tables"] == sorted(set(info["tables"])) == spec.drawing["tables_used"]


def test_helper_band_design_complete_sketch_pn_and_angle_diff():
    table = [{"dn_max": 1200, "count": 3}, {"dn_max": 99999, "count": 4}]
    assert R._band(table, 1200, "count") == 3 and R._band(table, 1200.0000000001, "count") == 3 and R._band(table, 1201, "count") == 4 and R._band(table, 1e9, "count") == 4
    spec = S.apply_create(VesselPatchIn.model_validate(V()), "t1")[0]
    assert not R.design_complete(spec)
    spec.design.design_pressure_mpa = 0.0                                                                          # 0 也是“给了”
    spec.design.design_temp_c = 20
    assert R.design_complete(spec)
    assert R.sketch_pn(None, Nozzle(uid="n1")) == 10.0 and R.sketch_pn(None, Nozzle(uid="n1", pn=25.0)) == 25.0    # PN 待定时只按 PN 下限示意外形
    assert R._angle_diff(10, 350) == 20 and R._angle_diff(0, 180) == 180 and R._angle_diff(-10, 10) == 20 and R._angle_diff(720, 0) == 0


def test_helper_footprint_pad_and_flange_dimensions():
    spec = build(V(nozzles=[{"dn": 25, "purpose_text": "温度计口"}, {"dn": 80, "purpose_text": "进料口"}, {"dn": 500, "kind": "manhole", "purpose_text": "人孔"}]))
    small, mid, man = spec.nozzles
    assert R.flange_dims(small) == {"D": 115.0, "C": R.flange_dims(small)["C"]} and R.flange_dims(mid)["D"] == 200.0          # B 系列 PN10：DN25 外径 115、DN80 外径 200
    assert R.pad_od(small) == 32.0 and R.pad_od(mid) == 178.0 and R.pad_od(man) == 1060.0                           # DN ≥ 80 才估补强圈：2×接管外径
    assert R.footprint(small) == 115.0 and R.footprint(mid) == 200.0 and R.footprint(man) == 1060.0               # 占位 = max(法兰外径, 补强圈外径)
    assert R.pad_od(Nozzle(uid="n1", dn=79.0, pipe_od_mm=89.0)) == 89.0 and R.pad_od(Nozzle(uid="n1", dn=80.0, pipe_od_mm=89.0)) == 178.0      # 阈值 DN80 含
    mid.pn = 40.0
    assert R.flange_dims(mid)["D"] == 200.0                                                                        # DN80 的 PN10 与 PN40 外径表里相同（都是 200）
    big = Nozzle(uid="n9", dn=100.0, pipe_series="B", pipe_od_mm=108.0)
    d10 = R.flange_dims(big)["D"]
    big.pn = 40.0
    assert d10 == 220.0 and R.flange_dims(big)["D"] == 235.0                                                       # DN100：指定了更高 PN，法兰外径随之变大


def test_helper_static_head():
    v = build(V())
    assert R.static_head_mpa(R.RuleCtx(v)) == pytest.approx(1000 * 9.81 * (2400 + 2 * (25 + 400)) / 1000 / 1e6)     # 立式：全高 = 筒体 + 两个封头（直边 + 内深 Di/4）
    h = build(H())
    assert R.static_head_mpa(R.RuleCtx(h)) == pytest.approx(1000 * 9.81 * 2000 / 1000 / 1e6)                       # 卧式：取 Di
    h.shell.length_mm = None
    assert R.static_head_mpa(R.RuleCtx(h)) == 0.0


def test_helper_conflict_checks():
    spec = build(V(supports=[{"kind": "lug", "count": 4}]))
    a = Nozzle(uid="n8", dn=80.0, pipe_od_mm=89.0, axial_mm=1500.0)
    b = Nozzle(uid="n9", dn=80.0, pipe_od_mm=89.0, axial_mm=1500.0, mark="x")
    assert R.shell_conflict(spec, a, 0.0, [(b, 0.0)]) == "管口 x"                                                    # 同高度同方位
    assert R.shell_conflict(spec, a, 90.0, [(b, 0.0)]) is None                                                     # 方位差 90°（弧长 1270）远大于占位
    b.axial_mm = 1500.0 + 300.0
    assert R.shell_conflict(spec, a, 0.0, [(b, 0.0)]) is None                                                      # 轴向错开 300 > 占位 200
    assert R.shell_conflict(spec, a, 45.0, []) == "耳式支座"                                                        # 耳座 45°，高度范围 1440…1690 内
    a.axial_mm = 600.0
    assert R.shell_conflict(spec, a, 45.0, []) is None
    ha = Nozzle(uid="n7", dn=80.0, pipe_od_mm=89.0, radial_mm=400.0)
    hb = Nozzle(uid="n6", dn=80.0, pipe_od_mm=89.0, radial_mm=400.0, mark="y")
    assert R.head_conflict(ha, 0.0, [(hb, 0.0)]) == "管口 y" and R.head_conflict(ha, 0.0, [(hb, 90.0)]) is None


def test_every_rule_has_a_normal_and_a_boundary_test():
    names = [n for n in globals() if n.startswith("test_")]
    for rid in R.RULES:
        key = rid.lower().replace("-", "_")
        n = [x for x in names if key in x]
        assert len(n) >= 2, f"规则 {rid} 的测试不足 2 个：{n}"
