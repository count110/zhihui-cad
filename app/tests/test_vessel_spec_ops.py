"""设备参数操作层单测（任务书 10.1 第一条 spec_ops 部分）：合并补丁、路径白名单、选择器、add、用户值/规则值、来源、缺项、单位、用途同义词、管口符号稳定、count 展开。

纯函数测试，直接调 zhihui.cad.equipment.spec_ops；演示口令的参数照 prompts.EQUIP_FEW_SHOTS[0]、[5]。
"""
import copy
import re

import pytest
from pydantic import ValidationError

from zhihui.cad.equipment import spec_ops as S
from zhihui.cad.geom.errors import GeomError
from zhihui.ir.vessel import DesignData, VesselPatchIn, VesselSpec

DEMO1 = {"kind": "vertical_vessel", "shell": {"inner_diameter_mm": 1600, "length_mm": 2400, "thickness_mm": 8, "material": "Q345R"}, "heads": [{"kind": "EHA"}],
         "design": {"design_pressure_mpa": 0.6, "design_temp_c": 80},
         "nozzles": [{"dn": 500, "kind": "manhole", "purpose_text": "人孔"}, {"dn": 80, "purpose_text": "进料口"}, {"dn": 80, "purpose_text": "出料口"},
                     {"dn": 25, "kind": "level_gauge", "purpose_text": "液位计口", "count": 2}],
         "supports": [{"kind": "lug", "count": 4}]}
DEMO8 = {"kind": "horizontal_vessel", "shell": {"inner_diameter_mm": 2000, "tangent_length_mm": 6000, "thickness_mm": 10},
         "nozzles": [{"dn": 100, "purpose_text": "进料"}, {"dn": 100, "purpose_text": "出料"}, {"dn": 500, "kind": "manhole", "purpose_text": "人孔"}],
         "supports": [{"kind": "saddle", "count": 2}]}


def create(d: dict, turn: str = "t1", fill: bool = True) -> VesselSpec:
    spec, _, _ = S.apply_create(VesselPatchIn.model_validate(copy.deepcopy(d)), turn)
    if fill:
        S.fill_defaults(spec)
    return spec


def patch(spec: VesselSpec, turn: str = "t2", fill: bool = True, **ops) -> VesselSpec:
    sp, _, _ = S.apply_patch(spec, copy.deepcopy(ops), turn)
    if fill:
        S.fill_defaults(sp)
    return sp


def raises(code: str, fn, *a, **kw) -> GeomError:
    with pytest.raises(GeomError) as ei:
        fn(*a, **kw)
    assert ei.value.code == code, f"期望 {code}，实际 {ei.value.code}：{ei.value.message}"
    return ei.value


def nz(spec: VesselSpec, mark: str):
    hit = [n for n in spec.nozzles if n.mark == mark]
    assert len(hit) == 1, (mark, [n.mark for n in spec.nozzles])
    return hit[0]


def marks(spec: VesselSpec) -> dict[str, str]:
    return {n.uid: n.mark for n in spec.nozzles}


# ================================================================== apply_create：只合并给了的值，来源 user:<turn>
def test_create_merges_only_given_values():
    spec, changed, warns = S.apply_create(VesselPatchIn.model_validate({"kind": "vertical_vessel", "shell": {"inner_diameter_mm": 1600, "thickness_mm": 8}}), "t1")
    assert (spec.shell.inner_diameter_mm, spec.shell.thickness_mm) == (1600.0, 8.0)
    assert spec.shell.length_mm is None and spec.shell.tangent_length_mm is None and spec.shell.material is None
    assert spec.design == DesignData()                                           # 没说的设计数据一个都不填
    assert spec.nozzles == [] and spec.supports == []
    assert [h.position for h in spec.heads] == ["bottom", "top"]
    assert all(h.kind is None and h.thickness_mm is None for h in spec.heads)    # 封头字段留给规则，不是“用户值”
    assert spec.sources == {"kind": "user:t1", "shell.inner_diameter_mm": "user:t1", "shell.thickness_mm": "user:t1"}
    assert changed == ["kind", "shell.inner_diameter_mm", "shell.thickness_mm"]
    assert warns == []


def test_create_every_given_value_has_source_user_turn_and_nothing_else():
    spec, changed, _ = S.apply_create(VesselPatchIn.model_validate(DEMO1), "t7")
    assert changed and set(changed) == set(spec.sources)
    assert set(spec.sources.values()) == {"user:t7"}                              # 补默认值之前没有任何规则来源
    assert spec.sources["shell.length_mm"] == "user:t7"
    assert spec.sources["heads[top].kind"] == "user:t7" and spec.sources["heads[bottom].kind"] == "user:t7"
    assert spec.sources["design.design_temp_c"] == "user:t7"
    assert spec.sources["supports[s1].count"] == "user:t7"
    assert spec.sources["nozzles[n2].dn"] == "user:t7"
    assert "nozzles[n2].mark" not in spec.sources                                 # 符号没人给，不是用户值


def test_create_tag_name_and_drawing_values_are_user_and_survive_defaults():
    d = {**DEMO1, "tag": "V-101", "name": "进料缓冲罐", "drawing": {"sheet": "A2", "scale": "1:20"}}
    spec, changed, _ = S.apply_create(VesselPatchIn.model_validate(d), "t3")
    assert (spec.tag, spec.name) == ("V-101", "进料缓冲罐")
    assert spec.sources["tag"] == spec.sources["name"] == spec.sources["drawing.sheet"] == spec.sources["drawing.scale"] == "user:t3"
    assert {"tag", "name", "drawing.sheet", "drawing.scale"} <= set(changed)
    S.fill_defaults(spec)
    assert spec.drawing["sheet"] == "A2" and spec.sources["drawing.sheet"] == "user:t3"   # R-SHEET-01 不覆盖用户的幅面


def test_create_without_kind_asks_everything_at_once():
    e = raises("SPEC_INCOMPLETE", S.apply_create, VesselPatchIn.model_validate({"shell": {"inner_diameter_mm": 1600}}), "t1")
    assert [p["path"] for p in e.extra["pending"]] == ["kind", "shell.length", "shell.thickness_mm", "nozzles"]      # 给过的内径不再问
    assert all(S.path_allowed(p["path"]) for p in e.extra["pending"])
    e = raises("SPEC_INCOMPLETE", S.apply_create, VesselPatchIn(), "t1")                                              # 边界：什么都没给
    assert [p["path"] for p in e.extra["pending"]] == ["kind", "shell.inner_diameter_mm", "shell.length", "shell.thickness_mm", "nozzles"]


def test_create_count_expands_into_independent_nozzles_with_stable_uids():
    spec = create(DEMO1, fill=False)
    assert [n.uid for n in spec.nozzles] == ["n1", "n2", "n3", "n4", "n5"]
    gauges = [n for n in spec.nozzles if n.kind == "level_gauge"]
    assert [n.uid for n in gauges] == ["n4", "n5"] and all(n.dn == 25.0 and n.purpose == "液位计口" for n in gauges)
    assert spec.sources["nozzles[n4].dn"] == spec.sources["nozzles[n5].dn"] == "user:t1"           # 展开后每个管口各有自己的来源键
    assert all(n.mark is None for n in spec.nozzles)                                               # 展开不会复制符号
    twenty = create({**DEMO1, "nozzles": [{"dn": 25, "purpose_text": "温度计口", "count": 20}]}, fill=False)   # 边界：count 上限
    assert len(twenty.nozzles) == 20 and len({n.uid for n in twenty.nozzles}) == 20


def test_create_head_position_filter_and_wrong_vessel_type():
    spec = create({**DEMO1, "heads": [{"position": "top", "kind": "HHA"}]})
    assert spec.heads[1].position == "top" and spec.heads[1].kind == "HHA" and spec.sources["heads[top].kind"] == "user:t1"
    assert spec.heads[0].kind == "EHA" and spec.sources["heads[bottom].kind"] == "rule:R-HEAD-01"          # 没点名的那个封头由规则补 EHA
    raises("INVALID_ARGS", S.apply_create, VesselPatchIn.model_validate({**DEMO1, "heads": [{"position": "left", "kind": "EHA"}]}), "t1")          # 立式没有 left
    h = copy.deepcopy(DEMO8)
    h["heads"] = [{"position": "top", "kind": "EHA"}]
    raises("INVALID_ARGS", S.apply_create, VesselPatchIn.model_validate(h), "t1")                                                                 # 卧式没有 top


@pytest.mark.parametrize("base,loc", [(DEMO1, "left_head"), (DEMO1, "right_head"), (DEMO8, "top_head"), (DEMO8, "bottom_head")])
def test_nozzle_location_must_match_vessel_kind_in_create_patch_and_upsert(base, loc):
    bad = {**base, "nozzles": [{"dn": 80, "purpose_text": "进料口", "location": loc}]}
    e = raises("INVALID_ARGS", S.apply_create, VesselPatchIn.model_validate(bad), "t1")
    assert "没有" in e.message
    spec = create(base)
    raises("INVALID_ARGS", S.apply_patch, spec, {"upsert_nozzles": [{"dn": 80, "purpose_text": "放空口", "location": loc}]}, "t2")
    mark = spec.nozzles[0].mark
    raises("INVALID_ARGS", S.apply_patch, spec, {"set": {f"nozzles[{mark}].location": loc}}, "t2")


# ================================================================== 路径白名单
@pytest.mark.parametrize("path", [
    "shell.foo", "nozzles.dn", "nozzles[a]", "design", "design[0].design_pressure_mpa", "supports", "supports[0].foo", "foo.bar", "tag.x", "drawing.orientation",
    "heads[left].kind", "Shell.length_mm", "", "shell.length_mm ; drop"])
def test_set_rejects_paths_outside_whitelist_with_bad_path_and_hint_lists_allowed(path):
    spec = create(DEMO1)
    e = raises("BAD_PATH", S.apply_patch, spec, {"set": {path: 1}}, "t2")
    assert "shell.length_mm" in e.extra["allowed"] and "design.design_pressure_mpa" in e.extra["allowed"]
    assert "heads[top].kind" in e.extra["allowed"] and "heads[left].kind" not in e.extra["allowed"]            # 立式的白名单没有 left
    assert "可用路径" in e.hint


def test_kind_and_logical_length_cannot_be_set_by_patch():
    spec = create(DEMO1)
    e = raises("BAD_PATH", S.apply_patch, spec, {"set": {"kind": "horizontal_vessel"}}, "t2")
    assert "重新 create" in e.hint
    e = raises("BAD_PATH", S.apply_patch, spec, {"set": {"shell.length": 3000}}, "t2")                         # shell.length 只能用在 add
    assert "add" in e.message and "shell.length_mm" in e.hint
    raises("BAD_PATH", S.apply_patch, spec, {"add": {"kind": 1}}, "t2")


@pytest.mark.parametrize("path,kind,ok", [
    ("shell.length_mm", None, True), ("shell.length", None, True), ("shell.tangent_length_mm", None, True), ("design.design_pressure_mpa", None, True),
    ("heads.kind", None, True), ("heads[top].kind", "vertical_vessel", True), ("heads[left].kind", "horizontal_vessel", True),
    ("heads[left].kind", "vertical_vessel", False), ("heads[top].kind", "horizontal_vessel", False), ("heads[middle].kind", None, False),
    ("nozzles[b].dn", None, True), ("nozzles[purpose=进料口].orientation_deg", None, True), ("nozzles[location=top_head].projection_mm", None, True),
    ("nozzles", None, True), ("nozzles.dn", None, False), ("nozzles[b]", None, False), ("nozzles[b].bogus", None, False),
    ("supports[0].count", None, True), ("supports.count", None, True), ("supports[0].positions", None, False),
    ("drawing.sheet", None, True), ("drawing.mark_style", None, True), ("drawing.record", None, False), ("tag", None, True), ("name", None, True),
    ("drawing_type", None, True), ("kind", None, True), ("sources", None, False), ("pending", None, False), ("internals.tubes", None, False)])
def test_path_allowed_table(path, kind, ok):
    assert S.path_allowed(path, kind) is ok


def test_allowed_paths_depend_on_vessel_kind():
    v, h = S.allowed_paths("vertical_vessel"), S.allowed_paths("horizontal_vessel")
    assert "heads[top].kind" in v and "heads[bottom].kind" in v and "heads[left].kind" not in v
    assert "heads[left].kind" in h and "heads[right].kind" in h and "heads[top].kind" not in h
    for p in ("shell.length", "shell.length_mm", "nozzles", "drawing.sheet"):
        assert p in v and p in h
    assert not any(p.startswith(("sources", "pending", "warnings", "internals")) for p in v)


# ================================================================== 选择器：0 / 1 / 多个匹配
def test_selector_single_match_by_mark_purpose_synonym_location_and_combination():
    spec = create(DEMO1)
    a = nz(spec, "a")
    assert a.purpose == "进料口" and a.location == "top_head"
    assert S.resolve_selector(spec, "a") == [a]
    assert S.resolve_selector(spec, "A") == [a]                                                                  # 符号不分大小写
    assert S.resolve_selector(spec, "mark=a") == S.resolve_selector(spec, "符号=a") == [a]
    assert S.resolve_selector(spec, "purpose=进料口") == [a]
    assert S.resolve_selector(spec, "purpose=进料") == S.resolve_selector(spec, "用途=入料口") == [a]            # 同义词也命中
    assert S.resolve_selector(spec, "location=top_head") == S.resolve_selector(spec, "位置=top_head") == [a]
    assert S.resolve_selector(spec, "kind=manhole") == [nz(spec, "M1")]
    assert S.resolve_selector(spec, "dn=500") == [nz(spec, "M1")]
    assert S.resolve_selector(spec, "dn=80,location=top_head") == [a]                                            # 多个条件取交集
    assert S.resolve_selector(spec, a.uid) == [a]


def test_selector_no_match_is_not_found_with_candidates():
    spec = create(DEMO1)
    e = raises("NOT_FOUND", S.resolve_selector, spec, "purpose=搅拌口")
    assert sorted(e.extra["candidates"]) == ["M1", "a", "b1", "b2", "c"]
    assert "现有管口" in e.hint and len(e.extra["options"]) == 4                                                 # 反问选项最多 4 个
    raises("NOT_FOUND", S.resolve_selector, spec, "z9")
    raises("NOT_FOUND", S.resolve_selector, spec, "dn=80,location=shell")                                       # 条件各自有解但交集为空
    e = raises("NOT_FOUND", S.apply_patch, spec, {"set": {"nozzles[purpose=搅拌口].dn": 50}}, "t2")             # 从 patch 里走也一样
    assert e.extra["candidates"]


def test_selector_multiple_matches_is_ambiguous_with_candidates_and_all_option():
    spec = create(DEMO1)
    e = raises("AMBIGUOUS", S.resolve_selector, spec, "purpose=液位计口")
    assert sorted(e.extra["candidates"]) == ["b1", "b2"]
    assert e.extra["options"][-1] == "全部" and len(e.extra["options"]) == 3
    assert "要改哪一个" in e.message and "b1" in e.message and "b2" in e.message
    assert sorted(raises("AMBIGUOUS", S.resolve_selector, spec, "dn=80").extra["candidates"]) == ["a", "c"]
    assert sorted(raises("AMBIGUOUS", S.resolve_selector, spec, "location=shell").extra["candidates"]) == ["M1", "b1", "b2"]
    got = S.resolve_selector(spec, "purpose=液位计口", allow_many=True)                                         # 查询类调用允许多个
    assert sorted(n.mark for n in got) == ["b1", "b2"]
    e = raises("AMBIGUOUS", S.apply_patch, spec, {"set": {"nozzles[purpose=液位计口].orientation_deg": 180}}, "t2")
    assert sorted(e.extra["candidates"]) == ["b1", "b2"]
    sp2, _, _ = S.apply_patch(spec, {"set": {"nozzles[b1].orientation_deg": 180}}, "t2")                           # 用符号就唯一了
    assert nz(sp2, "b1").orientation_deg == 180.0


def test_selector_unknown_condition_key_is_bad_path():
    spec = create(DEMO1)
    raises("BAD_PATH", S.resolve_selector, spec, "color=red")


def test_selector_heads_and_supports():
    spec = create(DEMO1)
    assert S.resolve_selector(spec, "top", "heads")[0].position == "top"
    e = raises("NOT_FOUND", S.resolve_selector, spec, "left", "heads")
    assert sorted(e.extra["candidates"]) == ["bottom", "top"]
    assert S.resolve_selector(spec, "0", "supports") == [spec.supports[0]]
    assert S.resolve_selector(spec, "kind=lug", "supports") == [spec.supports[0]]
    assert S.resolve_selector(spec, "s1", "supports") == [spec.supports[0]]
    e = raises("NOT_FOUND", S.resolve_selector, spec, "1", "supports")                                          # 边界：只有第 0 组
    assert e.extra["candidates"] == ["0"]
    raises("NOT_FOUND", S.resolve_selector, spec, "kind=leg", "supports")
    raises("NOT_FOUND", S.apply_patch, spec, {"set": {"supports[3].count": 3}}, "t2")


def test_support_path_without_selector_needs_exactly_one_support():
    spec = create(DEMO1)
    assert patch(spec, set={"supports.count": 3}).supports[0].count == 3                                         # 只有一组：不用选择器
    two = patch(spec, upsert_supports=[{"kind": "leg", "height_mm": 600}])
    assert [s.kind for s in two.supports] == ["lug", "leg"]
    e = raises("AMBIGUOUS", S.apply_patch, two, {"set": {"supports.count": 3}}, "t3")
    assert e.extra["candidates"] == ["0", "1"]
    assert patch(two, "t3", set={"supports[kind=leg].count": 5}).supports[1].count == 5


# ================================================================== set / 来源 / 不改原对象
def test_set_value_with_unit_records_user_turn_source_and_does_not_touch_original():
    spec = create(DEMO1)
    before = spec.model_dump()
    sp, changed, warns = S.apply_patch(spec, {"set": {"design.design_pressure_mpa": "1.0MPa", "design.medium": "液氨"}}, "t9")
    assert spec.model_dump() == before                                                                           # 原对象不动
    assert sp.design.design_pressure_mpa == 1.0 and sp.design.medium == "液氨"
    assert sp.sources["design.design_pressure_mpa"] == sp.sources["design.medium"] == "user:t9"
    assert changed == ["design.design_pressure_mpa", "design.medium"] and warns == []
    assert sp.design.design_temp_c == 80.0 and sp.sources["design.design_temp_c"] == "user:t1"                  # 没提到的保持原来的来源


def test_set_via_selector_is_user_value_and_survives_defaults():
    spec = create(DEMO1)
    assert S.source_of(spec, "nozzles[purpose=进料口].orientation_deg") == "rule:R-NOZ-ORI-01"
    sp = patch(spec, set={"nozzles[purpose=进料口].orientation_deg": 90})
    assert nz(sp, "a").orientation_deg == 90.0
    assert S.source_of(sp, "nozzles[purpose=进料口].orientation_deg") == "user:t2"
    assert S.source_of(sp, "nozzles[a].orientation_deg") == "user:t2"
    assert marks(sp) == marks(spec)                                                                              # 改方位不动符号


def test_set_length_switches_the_length_basis_the_user_gave():
    h = create(DEMO8)
    sp, _, _ = S.apply_patch(h, {"set": {"shell.length_mm": 5000}}, "t2")
    assert sp.shell.length_mm == 5000.0 and sp.shell.tangent_length_mm is None                                   # 用户改用筒体长度：原来的切线长作废
    assert "shell.tangent_length_mm" not in sp.sources
    S.fill_defaults(sp)
    assert sp.shell.length_mm == 5000.0 and sp.shell.tangent_length_mm is None
    v = create(DEMO1)
    sp, _, _ = S.apply_patch(v, {"set": {"shell.tangent_length_mm": 2600}}, "t2")
    assert sp.shell.tangent_length_mm == 2600.0 and sp.shell.length_mm is None
    S.fill_defaults(sp)
    assert sp.shell.length_mm == 2550.0 and sp.sources["shell.length_mm"] == "calc:R-LEN-01"                     # 2600 − 2×25（head_eha 直边）


def test_set_mark_conflict_and_free_mark():
    spec = create(DEMO1)
    raises("SPEC_CONFLICT", S.apply_patch, spec, {"set": {"nozzles[a].mark": "c"}}, "t2")
    raises("SPEC_CONFLICT", S.apply_patch, spec, {"set": {"nozzles[a].mark": "C"}}, "t2")                       # 不分大小写
    sp = patch(spec, set={"nozzles[a].mark": "N1"})
    assert sp.nozzles[1].mark == "N1" and sp.sources["nozzles[n2].mark"] == "user:t2"
    sp = patch(spec, set={"nozzles[a].mark": "a"})                                                              # 改成自己现在的符号不算冲突
    assert nz(sp, "a").uid == "n2"


@pytest.mark.parametrize("path,value", [
    ("shell.inner_diameter_mm", 0), ("shell.inner_diameter_mm", -1600), ("shell.length_mm", 0), ("shell.thickness_mm", 0), ("shell.thickness_mm", -8),
    ("nozzles[a].dn", 0), ("nozzles[a].dn", -80), ("nozzles[a].projection_mm", 0), ("supports[0].height_mm", -1), ("supports[0].count", 0)])
def test_set_non_positive_numbers_are_invalid_value(path, value):
    raises("INVALID_VALUE", S.apply_patch, create(DEMO1), {"set": {path: value}}, "t2")


def test_set_value_that_is_not_a_number_is_invalid_args():
    spec = create(DEMO1)
    raises("INVALID_ARGS", S.apply_patch, spec, {"set": {"shell.length_mm": "很长"}}, "t2")
    raises("INVALID_ARGS", S.apply_patch, spec, {"set": {"nozzles[a].dn": True}}, "t2")                          # bool 不当 1
    raises("INVALID_ARGS", S.apply_patch, spec, {"set": {"design.design_pressure_mpa": {"unit": "MPa"}}}, "t2")


def test_negative_design_pressure_is_a_legal_value_not_an_error():
    sp = patch(create(DEMO1), set={"design.design_pressure_mpa": "-0.05MPa"})
    assert sp.design.design_pressure_mpa == pytest.approx(-0.05)                                                 # 负压 = 真空，不是错误


@pytest.mark.parametrize("raw,expect", [(90, 90.0), (450, 90.0), (-90, 270.0), (360, 0.0), (0, 0.0), ("270", 270.0)])
def test_set_orientation_is_wrapped_into_0_360(raw, expect):
    sp = patch(create(DEMO1), set={"nozzles[a].orientation_deg": raw})
    assert nz(sp, "a").orientation_deg == expect


@pytest.mark.parametrize("raw,expect", [("90°", 90.0), ("90度", 90.0), ("45 deg", 45.0), ({"value": 180, "unit": "°"}, 180.0)])
def test_orientation_accepts_degree_units(raw, expect):
    sp = patch(create(DEMO1), set={"nozzles[a].orientation_deg": raw})
    assert nz(sp, "a").orientation_deg == expect


@pytest.mark.parametrize("path,value", [
    ("nozzles[a].location", "bogus"), ("nozzles[a].location", "上封头"), ("nozzles[a].kind", "bogus"), ("nozzles[a].pipe_series", "C"), ("heads.kind", "XYZ"),
    ("heads[top].kind", "CONE2"), ("supports[0].kind", "foo"), ("drawing.sheet", "A5"), ("drawing.scale", "1:7")])
def test_set_enum_fields_reject_values_outside_the_model(path, value):
    """枚举字段写了模型不认的值，存进去会让整张图的 JSON 再也加载不了（VesselSpec 校验失败），必须在写入时拦下。"""
    spec = create(DEMO1)
    raises("INVALID_VALUE", S.apply_patch, spec, {"set": {path: value}}, "t2")


@pytest.mark.parametrize("path,value,check,expect", [
    ("heads.kind", "hha", "heads[top].kind", "HHA"), ("heads[top].kind", " Eha ", "heads[top].kind", "EHA"), ("nozzles[a].pipe_series", "a", "nozzles[a].pipe_series", "A"),
    ("nozzles[a].location", "SHELL", "nozzles[a].location", "shell"), ("supports[0].kind", "Leg", "supports[0].kind", "leg"), ("drawing.sheet", "a2", "drawing.sheet", "A2"),
    ("drawing.scale", "1:20", "drawing.scale", "1:20")])
def test_set_enum_fields_accept_case_variants_and_store_canonical_value(path, value, check, expect):
    sp, _, _ = S.apply_patch(create(DEMO1), {"set": {path: value}}, "t2")
    assert S.value_of(sp, check) == expect
    VesselSpec.model_validate(sp.model_dump())                                                                   # 写进去的值能重新加载


# ================================================================== add：加到用户给过的长度口径
def test_add_shell_length_goes_to_the_basis_the_user_gave():
    v = create(DEMO1)                                                                                            # 用户给的是筒体长度
    sp = patch(v, add={"shell.length": 500})
    assert sp.shell.length_mm == 2900.0 and sp.shell.tangent_length_mm is None
    assert sp.sources["shell.length_mm"] == "user:t2:add"
    h = create(DEMO8)                                                                                            # 用户给的是切线长
    sp, _, _ = S.apply_patch(h, {"add": {"shell.length": 500}}, "t2")
    assert sp.shell.tangent_length_mm == 6500.0 and sp.sources["shell.tangent_length_mm"] == "user:t2:add"
    S.fill_defaults(sp)
    assert sp.shell.length_mm == 6450.0 and sp.sources["shell.length_mm"] == "calc:R-LEN-01"                    # 筒体长度由程序算：6500 − 2×25
    assert sp.sources["shell.tangent_length_mm"] == "user:t2:add"


def test_add_twice_accumulates_and_keeps_add_source():
    sp = patch(create(DEMO1), add={"shell.length": 500})
    sp = patch(sp, "t3", add={"shell.length": 500})
    assert sp.shell.length_mm == 3400.0 and sp.sources["shell.length_mm"] == "user:t3:add"


def test_add_when_user_gave_both_length_bases_moves_both_so_they_stay_consistent():
    both = create({**DEMO1, "shell": {**DEMO1["shell"], "tangent_length_mm": 2450}})                             # 2400 + 2×25，两个口径一致
    assert both.shell.length_mm == 2400.0
    sp = patch(both, add={"shell.length": 500})                                                                  # 不能因为只加了一边就报 SPEC_CONFLICT
    assert (sp.shell.length_mm, sp.shell.tangent_length_mm) == (2900.0, 2950.0)
    assert sp.sources["shell.length_mm"] == sp.sources["shell.tangent_length_mm"] == "user:t2:add"


def test_add_accepts_units_negative_delta_and_rejects_non_positive_result():
    spec = create(DEMO1)
    assert patch(spec, add={"shell.length": {"value": 0.5, "unit": "m"}}).shell.length_mm == 2900.0
    assert patch(spec, add={"shell.length": -400}).shell.length_mm == 2000.0
    assert patch(spec, add={"shell.length": "300mm"}).shell.length_mm == 2700.0
    raises("INVALID_VALUE", S.apply_patch, spec, {"add": {"shell.length": -2400}}, "t2")                         # 边界：加到 0
    raises("INVALID_VALUE", S.apply_patch, spec, {"add": {"shell.length": -3000}}, "t2")


def test_add_on_missing_value_or_text_field_is_refused():
    bare, _, _ = S.apply_create(VesselPatchIn.model_validate({"kind": "horizontal_vessel", "shell": {"inner_diameter_mm": 2000}}), "t1")
    raises("MISSING_ARG", S.apply_patch, bare, {"add": {"shell.length": 500}}, "t2")                             # 没有长度可加
    raises("MISSING_ARG", S.apply_patch, bare, {"add": {"design.design_pressure_mpa": 0.1}}, "t2")
    raises("INVALID_ARGS", S.apply_patch, create(DEMO1), {"add": {"shell.material": "Q345R"}}, "t2")             # 文本字段不能“加”
    raises("INVALID_ARGS", S.apply_patch, create(DEMO1), {"add": {"shell.length": "很多"}}, "t2")


def test_add_to_count_stays_integer_and_to_rule_value_becomes_user_value():
    sp = patch(create(DEMO1), add={"supports[0].count": 1})
    assert sp.supports[0].count == 5 and isinstance(sp.supports[0].count, int)
    assert sp.sources["supports[s1].count"] == "user:t2:add"
    spec = create(DEMO1)
    assert nz(spec, "b2").axial_mm == 160.0 and spec.sources["nozzles[n4].axial_mm"] == "rule:R-NOZ-POS-01"
    sp = patch(spec, add={"nozzles[b2].axial_mm": 100})                                                          # 在规则值上加：结果是用户值
    assert nz(sp, "b2").axial_mm == 260.0 and sp.sources["nozzles[n4].axial_mm"] == "user:t2:add"
    sp = patch(sp, "t3", set={"shell.thickness_mm": 10})
    assert nz(sp, "b2").axial_mm == 260.0                                                                        # 之后规则不再覆盖
    sp = patch(create(DEMO1), set={"nozzles[a].orientation_deg": 300}, add={"nozzles[a].orientation_deg": 90})
    assert nz(sp, "a").orientation_deg == 30.0                                                                   # 加完回绕到 0–360


# ================================================================== 用户值不被规则覆盖；规则值随参数重算
def test_user_values_are_never_overwritten_by_rules():
    d = copy.deepcopy(DEMO1)
    d["heads"] = [{"position": "top", "kind": "EHA", "thickness_mm": 12, "straight_flange_mm": 40, "material": "16MnR"}]
    d["nozzles"] = [{"dn": 80, "purpose_text": "进料口", "pn": 25, "orientation_deg": 180, "axial_mm": 700, "projection_mm": 300, "pipe_od_mm": 100, "pipe_thk_mm": 5,
                     "flange_type": "WN", "facing": "FF", "flange_std": "HG/T 20615", "location": "shell"},
                    {"dn": 500, "kind": "manhole", "purpose_text": "人孔", "std_part": "HG/T 21516 人孔"}]
    d["supports"] = [{"kind": "lug", "count": 3, "model": "NB/T 47065.3 耳式支座 A9"}]
    spec = create(d)
    top = next(h for h in spec.heads if h.position == "top")
    assert (top.thickness_mm, top.straight_flange_mm, top.material) == (12.0, 40.0, "16MnR")
    assert spec.sources["heads[top].thickness_mm"] == "user:t1"
    a = nz(spec, "a")
    assert (a.pn, a.orientation_deg, a.axial_mm, a.projection_mm, a.pipe_od_mm, a.pipe_thk_mm) == (25.0, 180.0, 700.0, 300.0, 100.0, 5.0)
    assert (a.flange_type, a.facing, a.flange_std, a.location) == ("WN", "FF", "HG/T 20615", "shell")
    assert all(spec.sources[f"nozzles[n1].{f}"] == "user:t1" for f in ("pn", "orientation_deg", "axial_mm", "projection_mm", "pipe_od_mm", "flange_type", "location"))
    assert nz(spec, "M1").std_part == "HG/T 21516 人孔"
    assert (spec.supports[0].count, spec.supports[0].model) == (3, "NB/T 47065.3 耳式支座 A9")
    bottom = next(h for h in spec.heads if h.position == "bottom")
    assert bottom.thickness_mm == 8.0 and spec.sources["heads[bottom].thickness_mm"] == "rule:R-HEAD-02"       # 另一个封头照样走规则
    sp = patch(spec, set={"shell.thickness_mm": 10, "design.design_pressure_mpa": 1.6, "shell.inner_diameter_mm": 2000})   # 参数全变一遍，用户值仍然在
    top = next(h for h in sp.heads if h.position == "top")
    assert (top.thickness_mm, top.straight_flange_mm) == (12.0, 40.0)
    a = nz(sp, "a")
    assert (a.pn, a.orientation_deg, a.axial_mm, a.projection_mm, a.pipe_od_mm, a.flange_type) == (25.0, 180.0, 700.0, 300.0, 100.0, "WN")
    assert next(h for h in sp.heads if h.position == "bottom").thickness_mm == 10.0                              # 规则值跟着新壁厚走


def test_rule_defaults_are_recomputed_when_pressure_changes_and_user_pn_stays():
    d = copy.deepcopy(DEMO1)
    d["nozzles"][1]["pn"] = 25                                                                                   # 进料口 PN 由用户指定
    spec = create(d)
    assert {n.mark: n.pn for n in spec.nozzles} == {"M1": 10.0, "a": 25.0, "b1": 10.0, "b2": 10.0, "c": 10.0}   # 0.6 MPa / 80 ℃ → PN10
    sp = patch(spec, set={"design.design_pressure_mpa": 1.0})                                                    # “设计压力改成1.0MPa”
    assert {n.mark: n.pn for n in sp.nozzles} == {"M1": 16.0, "a": 25.0, "b1": 16.0, "b2": 16.0, "c": 16.0}    # 默认的重新取档到 PN16，用户的 25 不动
    assert sp.sources["nozzles[n3].pn"] == "rule:R-PN-01" and sp.sources["nozzles[n2].pn"] == "user:t1"
    assert nz(sp, "M1").std_part.endswith("PN16（初选，待校核）")                                                   # 依赖 PN 的人孔型号也跟着重算
    assert nz(sp, "a").flange_type is None or sp.sources["nozzles[n2].flange_type"].startswith("rule:")        # 法兰型式仍是规则值（不会被固化成“用户值”）
    sp = patch(sp, "t3", set={"design.design_pressure_mpa": 0.6})                                                # 再改回去，也跟着回去
    assert {n.mark: n.pn for n in sp.nozzles} == {"M1": 10.0, "a": 25.0, "b1": 10.0, "b2": 10.0, "c": 10.0}


def test_rule_defaults_follow_standard_pressure_diameter_and_dn_changes():
    spec = create(DEMO1)
    assert spec.design.design_standard == "GB/T 150.1~150.4-2024"
    sp = patch(spec, set={"design.design_pressure_mpa": "50kPa"})
    assert sp.design.design_pressure_mpa == pytest.approx(0.05) and sp.design.design_standard == "NB/T 47003.1-2022"
    sp = patch(sp, "t3", set={"design.design_standard": "企业标准 Q/XX"})
    sp = patch(sp, "t4", set={"design.design_pressure_mpa": 1.0})
    assert sp.design.design_standard == "企业标准 Q/XX" and sp.sources["design.design_standard"] == "user:t3"   # 用户写过的标准不被规则改
    heads = lambda s: [(h.position, h.straight_flange_mm) for h in s.heads]
    assert heads(spec) == [("bottom", 25.0), ("top", 25.0)]
    assert heads(patch(spec, set={"shell.inner_diameter_mm": 2000})) == [("bottom", 25.0), ("top", 25.0)]       # head_eha 表：DN2000 仍是 25
    assert heads(patch(spec, set={"shell.inner_diameter_mm": 2400})) == [("bottom", 40.0), ("top", 40.0)]       # DN2400 → 40
    sp = patch(spec, set={"nozzles[a].dn": 100})
    a = nz(sp, "a")
    assert (a.dn, a.pipe_od_mm, a.pipe_thk_mm) == (100.0, 108.0, 4.0) and sp.sources["nozzles[n2].pipe_od_mm"] == "table:pipe_dims"
    assert (nz(spec, "a").pipe_od_mm, nz(spec, "c").pipe_od_mm) == (89.0, 89.0)                                 # DN80 → 89


def test_changing_purpose_recomputes_location_but_not_the_mark():
    spec = create(DEMO1)
    assert nz(spec, "a").location == "top_head"
    sp = patch(spec, upsert_nozzles=[{"mark": "a", "purpose_text": "排污"}])                                      # 进料口 → 排净口：位置跟着用途走
    a = nz(sp, "a")
    assert (a.purpose, a.purpose_text, a.location) == ("排净口", "排污", "bottom_head")
    assert sp.sources["nozzles[n2].location"] == "rule:R-NOZ-LOC-01" and marks(sp) == marks(spec)


def test_fill_defaults_is_idempotent_and_deterministic():
    spec = create(DEMO1)
    once = spec.model_dump()
    S.fill_defaults(spec)
    assert spec.model_dump() == once
    again = create(DEMO1)
    assert again.model_dump() == once                                                                            # 同样的输入，同样的参数（含符号、来源）


# ================================================================== upsert / remove 管口与支座
def test_upsert_new_nozzle_on_top_head_keeps_existing_marks():
    spec = create(DEMO1)
    sp = patch(spec, upsert_nozzles=[{"dn": 50, "purpose_text": "放空口", "location": "top_head"}])                 # “上封头再加一个DN50的放空口”
    assert len(sp.nozzles) == 6 and sp.nozzles[-1].uid == "n6"
    new = sp.nozzles[-1]
    assert (new.dn, new.purpose, new.location, new.pipe_od_mm) == (50.0, "放空口", "top_head", 57.0)
    assert sp.sources["nozzles[n6].dn"] == sp.sources["nozzles[n6].location"] == "user:t2"
    assert new.mark and new.mark not in {n.mark for n in spec.nozzles}                                           # 新符号不与旧的重复
    assert {n.uid: n.mark for n in sp.nozzles if n.uid != "n6"} == marks(spec)                                   # 旧符号一个都没变


def test_upsert_with_count_adds_that_many_and_with_existing_mark_updates_in_place():
    spec = create(DEMO1)
    sp = patch(spec, upsert_nozzles=[{"dn": 25, "purpose_text": "温度计口", "count": 3}])
    assert len(sp.nozzles) == 8 and [n.uid for n in sp.nozzles[-3:]] == ["n6", "n7", "n8"]
    sp = patch(spec, upsert_nozzles=[{"mark": "A", "dn": 100}])                                                   # 符号不分大小写，按符号找到就是改它
    assert len(sp.nozzles) == 5
    a = nz(sp, "a")
    assert a.dn == 100.0 and a.purpose == "进料口" and a.location == "top_head" and sp.sources["nozzles[n2].dn"] == "user:t2"
    assert sp.sources["nozzles[n2].purpose"] == "user:t1"                                                        # 没提到的字段来源不变


def test_upsert_with_unknown_mark_creates_a_nozzle_with_that_user_mark():
    spec = create(DEMO1)
    sp = patch(spec, upsert_nozzles=[{"mark": "X1", "dn": 40, "purpose_text": "温度计口"}])
    new = sp.nozzles[-1]
    assert new.mark == "X1" and sp.sources[f"nozzles[{new.uid}].mark"] == "user:t2"
    assert marks(sp) == {**marks(spec), new.uid: "X1"}


def test_upsert_nozzle_with_misspelled_field_is_rejected():
    with pytest.raises(ValidationError):
        S.apply_patch(create(DEMO1), {"upsert_nozzles": [{"diameter": 50}]}, "t2")


def test_purpose_not_in_vocabulary_is_kept_as_is_with_warning():
    spec = create(DEMO1)
    sp, _, warns = S.apply_patch(spec, {"upsert_nozzles": [{"dn": 40, "purpose_text": "冷却水进口"}]}, "t2")
    new = sp.nozzles[-1]
    assert new.purpose == "冷却水进口" and new.purpose_text == "冷却水进口"
    assert any("不在受控词表" in w for w in warns)


def test_remove_nozzle_by_mark_selector_and_purpose_clears_sources_and_keeps_other_marks():
    spec = create(DEMO1)
    for sel in ("a", "nozzles[a]", "purpose=进料口", "purpose=进料"):
        sp = patch(spec, remove_nozzles=[sel])
        assert sorted(n.mark for n in sp.nozzles) == ["M1", "b1", "b2", "c"], sel
        assert not any(k.startswith("nozzles[n2].") for k in sp.sources)                                         # 被删管口的来源一并清掉
        assert {n.uid: n.mark for n in sp.nozzles} == {u: m for u, m in marks(spec).items() if u != "n2"}      # 其余符号不变
    sp = patch(spec, remove_nozzles=["b1", "nozzles[b2]"])
    assert sorted(n.mark for n in sp.nozzles) == ["M1", "a", "c"]


def test_remove_nozzle_errors_not_found_and_ambiguous_and_removing_all_makes_nozzles_pending():
    spec = create(DEMO1)
    raises("NOT_FOUND", S.apply_patch, spec, {"remove_nozzles": ["z"]}, "t2")
    e = raises("AMBIGUOUS", S.apply_patch, spec, {"remove_nozzles": ["purpose=液位计口"]}, "t2")
    assert sorted(e.extra["candidates"]) == ["b1", "b2"]
    sp = patch(spec, remove_nozzles=["a", "b1", "b2", "c", "M1"])
    assert sp.nozzles == []
    assert [p["path"] for p in S.missing(sp)] == ["nozzles"]


def test_support_upsert_updates_same_kind_or_adds_and_remove_clears_sources():
    spec = create(DEMO1)
    sp = patch(spec, upsert_supports=[{"kind": "lug", "count": 3}])                                              # “支座改成3个”
    assert len(sp.supports) == 1 and sp.supports[0].count == 3 and sp.sources["supports[s1].count"] == "user:t2"
    assert [p["orientation_deg"] for p in sp.supports[0].positions] == [45.0, 165.0, 285.0]                       # 方位随数量重算
    sp = patch(spec, upsert_supports=[{"count": 6}])                                                             # 只有一组且没说型式 → 就是改它
    assert len(sp.supports) == 1 and sp.supports[0].kind == "lug" and sp.supports[0].count == 6
    sp = patch(spec, upsert_supports=[{"kind": "leg", "height_mm": 600}])
    assert [(s.uid, s.kind) for s in sp.supports] == [("s1", "lug"), ("s2", "leg")] and sp.supports[1].count == 4   # DN1600 支腿 → 4 条
    for sel in ("0", "supports[0]", "kind=lug"):
        sp = patch(spec, remove_supports=[sel])
        assert sp.supports == [] and not any(k.startswith("supports[s1].") for k in sp.sources), sel
        assert [p["path"] for p in S.missing(sp)] == ["supports.kind"]                                           # 立式容器没有支座 → 必问
    raises("NOT_FOUND", S.apply_patch, spec, {"remove_supports": ["kind=leg"]}, "t2")


# ================================================================== 管口符号：稳定，renumber 才重排
def test_marks_are_stable_across_add_remove_and_parameter_changes():
    spec = create(DEMO1)
    assert {n.mark: (n.purpose, n.axial_mm) for n in spec.nozzles} == {"a": ("进料口", None), "b1": ("液位计口", 2160.0), "b2": ("液位计口", 160.0), "c": ("出料口", None), "M1": ("人孔", 900.0)}
    assert all(spec.sources[f"nozzles[{n.uid}].mark"] == "rule:R-MARK-01" for n in spec.nozzles)
    base = marks(spec)
    sp = patch(spec, remove_nozzles=["a"])                                                                       # 删一个
    assert marks(sp) == {u: m for u, m in base.items() if u != "n2"}
    sp2 = patch(sp, "t3", upsert_nozzles=[{"dn": 50, "purpose_text": "放空口", "location": "top_head"}])         # 再加一个：新的占空位，老的不动
    assert {u: m for u, m in marks(sp2).items() if u in marks(sp)} == marks(sp)
    assert len(set(marks(sp2).values())) == len(sp2.nozzles)
    sp3 = patch(sp2, "t4", set={"shell.length_mm": 3600, "shell.inner_diameter_mm": 2000, "nozzles[c].orientation_deg": 180})   # 位置全变了，符号不变
    assert marks(sp3) == marks(sp2)
    assert all(sp3.sources[f"nozzles[{n.uid}].mark"] == "rule:R-MARK-01" for n in sp3.nozzles)


def test_renumber_reassigns_marks_in_view_order():
    spec = create(DEMO1)
    sp = patch(spec, remove_nozzles=["a"])                                                                       # 删掉 a 之后，剩下的符号 b1 b2 c M1 不会自己补位
    assert sorted(marks(sp).values()) == ["M1", "b1", "b2", "c"]
    sp2 = patch(sp, "t3", renumber=True)                                                                         # “重新编号”才重排：主视图左下起顺时针，同规格同用途的 a1/a2
    assert {n.purpose: n.mark for n in sp2.nozzles if n.kind != "level_gauge"} == {"人孔": "M1", "出料口": "b"}
    assert sorted(n.mark for n in sp2.nozzles if n.kind == "level_gauge") == ["a1", "a2"]
    assert all(sp2.sources[f"nozzles[{n.uid}].mark"] == "rule:R-MARK-01" for n in sp2.nozzles)
    sp3 = patch(sp2, "t4", set={"shell.length_mm": 3000})                                                        # 重排之后又稳定了
    assert marks(sp3) == marks(sp2)
    again = patch(sp, "t3", renumber=True)
    assert marks(again) == marks(sp2)                                                                            # 同样的输入，同样的重排结果


def test_mark_style_n_numbers_all_nozzles_n1_n2():
    sp = patch(create(DEMO1), set={"drawing.mark_style": "N"}, renumber=True)
    assert sorted(marks(sp).values()) == ["N1", "N2", "N3", "N4", "N5"]


# ================================================================== 缺项 / pending
def test_missing_lists_everything_at_once_in_fixed_order():
    spec, _, _ = S.apply_create(VesselPatchIn.model_validate({"kind": "vertical_vessel"}), "t1")
    p = S.missing(spec)
    assert [x["path"] for x in p] == ["shell.inner_diameter_mm", "shell.length", "shell.thickness_mm", "supports.kind", "nozzles"]
    assert all(set(x) == {"path", "label", "options"} and 2 <= len(x["options"]) <= 4 and x["label"] for x in p)
    assert all(S.path_allowed(x["path"], spec.kind) for x in p)
    h, _, _ = S.apply_create(VesselPatchIn.model_validate({"kind": "horizontal_vessel"}), "t1")
    assert [x["path"] for x in S.missing(h)] == ["shell.inner_diameter_mm", "shell.length", "shell.thickness_mm", "nozzles"]   # 卧式不问支座型式


def test_missing_is_empty_for_complete_spec_and_tangent_length_counts_as_length():
    assert S.missing(create(DEMO1)) == []
    h = create(DEMO8)
    assert h.shell.tangent_length_mm == 6000.0 and S.missing(h) == []


def test_missing_per_object_items_leg_height_flat_head_thickness_and_nozzle_dn():
    d = {"kind": "vertical_vessel", "shell": {"inner_diameter_mm": 1000, "length_mm": 2000, "thickness_mm": 6},
         "heads": [{"position": "top", "kind": "FLAT"}], "supports": [{"kind": "leg"}], "nozzles": [{"purpose_text": "进料口"}, {"dn": 50, "purpose_text": "放空口"}]}
    spec = create(d)
    got = {x["path"]: x for x in S.missing(spec)}
    assert set(got) == {"heads[top].thickness_mm", "supports[s1].height_mm", "nozzles[n1].dn"}
    assert "平" in got["heads[top].thickness_mm"]["label"] or "FLAT" in got["heads[top].thickness_mm"]["label"]
    assert "进料口" in got["nozzles[n1].dn"]["label"] and got["nozzles[n1].dn"]["options"][0] == "DN25"
    assert all(S.path_allowed(p) for p in got)
    ok = patch(spec, set={"heads[top].thickness_mm": 20, "supports[0].height_mm": 800, "nozzles[purpose=进料口].dn": 80})
    assert S.missing(ok) == []
    spec_m = create({**DEMO1, "nozzles": [{"kind": "manhole", "purpose_text": "人孔"}]})
    assert S.missing(spec_m)[0]["options"] == ["DN450", "DN500", "DN600"]                                         # 人孔 DN 必须由用户给，选项也是人孔的


def test_pending_item_truncates_options_and_falls_back_to_path_as_label():
    p = S.pending_item("shell.inner_diameter_mm")
    assert p["label"] == "筒体内径 Di（mm）" and p["options"] == ["1000", "1600", "2000", "2400"]
    assert S.pending_item("foo.bar") == {"path": "foo.bar", "label": "foo.bar", "options": []}
    assert S.pending_item("x", "自定义", ["1", "2", "3", "4", "5", "6"])["options"] == ["1", "2", "3", "4"]       # 最多 4 个


def test_question_text_numbers_every_pending_item():
    q = S.question_text(S.missing(S.apply_create(VesselPatchIn.model_validate({"kind": "vertical_vessel"}), "t1")[0]))
    assert q.startswith("画设备图还缺这些参数，请一次告诉我：")
    for i in range(1, 6):
        assert f"{i}. " in q
    assert "筒体内径 Di（mm）（例如 1000 / 1600 / 2000）" in q and "支座型式" in q and "有哪些管口" in q


# ================================================================== 单位换算
@pytest.mark.parametrize("raw,field,expect", [
    ({"value": 1.6, "unit": "m"}, "length_mm", 1600.0), ({"value": 1.6, "unit": "米"}, "length_mm", 1600.0), ({"value": 24, "unit": "cm"}, "length_mm", 240.0),
    ({"value": 5, "unit": "mm"}, "thickness_mm", 5.0), ("1.6m", "inner_diameter_mm", 1600.0), ("1600mm", "inner_diameter_mm", 1600.0), ("1600", "inner_diameter_mm", 1600.0),
    (1600, "inner_diameter_mm", 1600.0), ("600kPa", "design_pressure_mpa", 0.6), ("0.6MPa", "design_pressure_mpa", 0.6), ("0.6 MPa", "design_pressure_mpa", 0.6),
    ("6bar", "design_pressure_mpa", 0.6), ("3bar", "design_pressure_mpa", 0.3), ("100kPa", "design_pressure_mpa", 0.1), ("-20kPa", "design_pressure_mpa", -0.02),
    ("1000Pa", "design_pressure_mpa", 0.001), ({"value": 600, "unit": "kPa"}, "design_pressure_mpa", 0.6), ("0.4kPa", "wind_pressure_kpa", 0.4), ("400Pa", "wind_pressure_kpa", 0.4),
    ("80℃", "design_temp_c", 80.0), ("80°C", "design_temp_c", 80.0), ("-196℃", "design_temp_c", -196.0), ("80 度", "design_temp_c", 80.0), ({"value": 80, "unit": "℃"}, "design_temp_c", 80.0),
    ("DN80", "dn", 80.0), ("PN16", "pn", 16.0), ("90°", "orientation_deg", 90.0), ("90度", "orientation_deg", 90.0)])
def test_to_number_converts_to_the_field_standard_unit_exactly(raw, field, expect):
    got = S.to_number(raw, field)
    assert got == expect, (raw, field, got)                                                                      # 严格相等：边界比较（p ≥ 0.1）不能被浮点误差带偏


def test_to_number_accepts_quantity_model_and_rejects_unknown_units_and_non_numbers():
    from zhihui.ir.vessel import Quantity
    assert S.to_number(Quantity(value=2, unit="m"), "length_mm") == 2000.0
    e = raises("BAD_UNIT", S.to_number, {"value": 1, "unit": "ft"}, "length_mm")
    assert "ft" in e.message and "mm" in e.hint
    raises("BAD_UNIT", S.to_number, "80K", "design_temp_c")
    raises("BAD_UNIT", S.to_number, "80mm", "dn")                                                                # dn 不带长度单位
    raises("INVALID_ARGS", S.to_number, "abc", "dn")
    raises("INVALID_ARGS", S.to_number, True, "dn")
    raises("INVALID_ARGS", S.to_number, None, "dn")
    raises("INVALID_ARGS", S.to_number, [1], "dn")


def test_units_are_converted_in_create_set_add_and_nested_models():
    d = {**DEMO1, "shell": {"inner_diameter_mm": {"value": 1.6, "unit": "m"}, "length_mm": "2.4m", "thickness_mm": "8mm"},
         "design": {"design_pressure_mpa": "600kPa", "design_temp_c": "80℃"}}
    spec = create(d)
    assert (spec.shell.inner_diameter_mm, spec.shell.length_mm, spec.shell.thickness_mm) == (1600.0, 2400.0, 8.0)
    assert (spec.design.design_pressure_mpa, spec.design.design_temp_c) == (0.6, 80.0)
    sp = patch(spec, set={"design.design_pressure_mpa": "1.0MPa", "shell.thickness_mm": {"value": 1, "unit": "cm"}}, add={"shell.length": {"value": 0.5, "unit": "m"}})
    assert (sp.design.design_pressure_mpa, sp.shell.thickness_mm, sp.shell.length_mm) == (1.0, 10.0, 2900.0)


def test_fmt_value_formats_for_the_drawing():
    assert S.fmt_value(None) == "待填"
    assert [S.fmt_value(v) for v in (2400.0, 0.6, 12.50, 1.23456, 1.00004, -0.00001, 0.0, "Q345R", 7)] == ["2400", "0.6", "12.5", "1.2346", "1", "0", "0", "Q345R", "7"]


# ================================================================== 用途同义词
@pytest.mark.parametrize("text,canon", [
    ("进料口", "进料口"), ("进料", "进料口"), ("入料口", "进料口"), ("进口", "进料口"), ("DN80进料口", "进料口"), ("  进料  ", "进料口"),
    ("出料", "出料口"), ("出口", "出料口"), ("放空", "放空口"), ("排气口", "放空口"), ("排污", "排净口"), ("排液口", "排净口"),
    ("两个液位计口DN25", "液位计口"), ("液面计", "液位计口"), ("液位计", "液位计口"), ("人孔", "人孔"), ("检修孔", "人孔"), ("手孔", "手孔"),
    ("安全阀", "安全阀口"), ("压力表", "压力表口"), ("温度计", "温度计口"), ("回流", "回流口"), ("备用口", "备用口"), ("视镜", "视镜"),
    ("上封头进料口", "进料口"), ("上封头进料", "进料口")])
def test_canonical_purpose_synonyms_hit_the_controlled_vocabulary(text, canon):
    assert S.canonical_purpose(text) == (canon, True)


@pytest.mark.parametrize("text", ["冷却水进口", "蒸汽出口", "氮封口", "取样口"])
def test_canonical_purpose_does_not_guess_for_words_outside_the_vocabulary(text):
    """词表外的用途原样保存（并警告），不能因为带“进口/出口”二字就当成进料口、出料口（会换了位置规则、写错图上的用途）。"""
    assert S.canonical_purpose(text) == (text, False)


def test_canonical_purpose_empty_input_and_purpose_rows():
    assert S.canonical_purpose(None) == (None, True) and S.canonical_purpose("") == (None, True)
    assert S.purpose_row("进料口")["loc"] == "top" and S.purpose_row("出料口")["loc"] == "bottom" and S.purpose_row("人孔")["kind"] == "manhole"
    assert S.purpose_row("不存在") is None and S.purpose_row(None) is None


def test_create_with_short_word_and_patch_with_full_word_hit_the_same_nozzle():
    d = {**DEMO1, "nozzles": [{"dn": 80, "purpose_text": "进料"}, {"dn": 80, "purpose_text": "出料"}]}
    spec = create(d)
    a = nz(spec, "a")
    assert (a.purpose, a.purpose_text) == ("进料口", "进料")                                                      # 存规范名，原话进 purpose_text
    sp = patch(spec, set={"nozzles[purpose=进料口].orientation_deg": 90})
    assert nz(sp, "a").orientation_deg == 90.0 and sp.sources["nozzles[n1].orientation_deg"] == "user:t2"
    assert sp.sources["nozzles[n2].orientation_deg"] == "rule:R-NOZ-ORI-01"                                      # 出料口没被连带改
    sp = patch(spec, set={"nozzles[purpose=进料].orientation_deg": 90})                                          # 反过来用同义词也命中
    assert nz(sp, "a").orientation_deg == 90.0
    spec2 = create({**DEMO1, "nozzles": [{"dn": 80, "purpose": "进口"}]})                                         # 直接给 purpose 也走同义词
    assert (spec2.nozzles[0].purpose, spec2.nozzles[0].purpose_text) == ("进料口", "进口")


def test_purpose_decides_nozzle_kind_unless_user_said_otherwise():
    spec = create({**DEMO1, "nozzles": [{"dn": 500, "purpose_text": "人孔"}, {"dn": 25, "purpose_text": "液位计口"}, {"dn": 80, "purpose_text": "进料口"}]})
    assert [n.kind for n in spec.nozzles] == ["manhole", "level_gauge", "pipe"]
    assert spec.sources["nozzles[n1].kind"] == "calc:purpose" and "nozzles[n3].kind" not in spec.sources
    spec = create({**DEMO1, "nozzles": [{"dn": 25, "purpose_text": "液位计口", "kind": "sight_glass"}, {"dn": 80, "purpose_text": "进料口"}]})
    assert spec.nozzles[0].kind == "sight_glass" and spec.sources["nozzles[n1].kind"] == "user:t1"


# ================================================================== 来源 / 取值 / 展示路径
def test_display_path_source_of_and_value_of_use_public_paths():
    spec = create(DEMO1)
    assert S.display_path(spec, "nozzles[n2].dn") == "nozzles[a].dn" and S.display_path(spec, "supports[s1].count") == "supports[0].count"
    assert S.display_path(spec, "shell.length_mm") == "shell.length_mm" and S.display_path(spec, "nozzles[n99].dn") == "nozzles[n99].dn"
    assert S.source_of(spec, "nozzles[purpose=进料口].dn") == "user:t1" and S.source_of(spec, "supports[0].count") == "user:t1"
    assert S.source_of(spec, "supports[0].model") == "rule:R-SUP-02"
    assert S.source_of(spec, "shell.thickness_mm") == "user:t1" and S.source_of(spec, "heads.kind") is None and S.source_of(spec, "bad path") is None
    assert S.source_of(spec, "shell.length") == "user:t1"                                                         # 逻辑路径：取用户给的那个口径的来源
    assert S.value_of(spec, "nozzles[purpose=进料口].dn") == 80.0 and S.value_of(spec, "heads[top].kind") == "EHA"
    assert S.value_of(spec, "design.design_pressure_mpa") == 0.6 and S.value_of(spec, "supports[0].count") == 4
    assert S.value_of(spec, "drawing.sheet") == "A1" and S.value_of(spec, "kind") == "vertical_vessel" and S.value_of(spec, "tag") is None
    raises("BAD_PATH", S.value_of, spec, "no path")
    raises("NOT_FOUND", S.value_of, spec, "nozzles[z].dn")


def test_value_and_source_of_logical_shell_length_follow_the_users_basis():
    h = create(DEMO8)
    assert S.value_of(h, "shell.length") == 6000.0 and S.source_of(h, "shell.length") == "user:t1"             # 用户给的是切线长
    v = create(DEMO1)
    assert S.value_of(v, "shell.length") == 2400.0


def test_get_set_key_roundtrip_and_clearing_resets_defaults():
    spec = create(DEMO1)
    assert S.get_key(spec, "nozzles[n2].dn") == 80.0 and S.get_key(spec, "nozzles[n99].dn") is None and S.get_key(spec, "bogus") is None
    S.set_key(spec, "nozzles[n2].kind", "level_gauge", "user:t5")
    assert spec.nozzles[1].kind == "level_gauge" and spec.sources["nozzles[n2].kind"] == "user:t5"
    S.set_key(spec, "nozzles[n2].kind", None, None)
    assert spec.nozzles[1].kind == "pipe" and "nozzles[n2].kind" not in spec.sources                              # 有缺省值的字段清回缺省值，不是 None
    S.set_key(spec, "nozzles[n2].reinforcement", None, None)
    assert spec.nozzles[1].reinforcement == "auto"
    VesselSpec.model_validate(spec.model_dump())


# ================================================================== 杂项：目录行、触发词表、摘要、拷贝
def test_catalog_line_is_short_and_lists_selector_syntax():
    line = S.catalog_line()
    assert len(line) < 1000
    for word in ("vessel_define", "nozzles[purpose=进料口]", "shell.length", "set:", "add:", "upsert_nozzles", "remove_nozzles"):
        assert word in line


def test_trigger_tables_are_valid_regexes_and_cover_the_catalog_keys():
    for k, pat in S.DEFAULTABLE_TRIGGERS.items():
        re.compile(pat)
    for field, mapping in S.TEXT_TRIGGERS.items():
        for pat in mapping.values():
            re.compile(pat)
    for k in ("orientation_deg", "projection_mm", "pn", "flange_type", "heads.kind", "straight_flange_mm", "axial_mm"):
        assert k in S.DEFAULTABLE_TRIGGERS
    assert re.search(S.DEFAULTABLE_TRIGGERS["orientation_deg"], "转到90度") and not re.search(S.DEFAULTABLE_TRIGGERS["orientation_deg"], "进料口DN80")
    assert re.search(S.TEXT_TRIGGERS["location"]["top_head"], "上封头加一个放空口")


def test_summary_and_copy_spec():
    assert S.summary(create(DEMO1)) == "立式容器 DN1600×δ8，L=2400，5 个管口，4 个支座"
    assert S.summary(create(DEMO8)) == "卧式容器 DN2000×δ10，L=5950，3 个管口，2 个鞍座"
    spec = create(DEMO1)
    cp = S.copy_spec(spec)
    assert cp == spec and cp is not spec
    cp.nozzles[0].dn = 1.0
    cp.sources["kind"] = "x"
    assert spec.nozzles[0].dn == 500.0 and spec.sources["kind"] == "user:t1"
