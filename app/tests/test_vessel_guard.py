"""设备补丁护栏（任务书 9.3 第 5 条）：编造类全部被拦或被剔除，用户写法全部放行；另测引擎侧复核 invented_values。

每条探针都先用 speech_normalize.normalize 规范化口令，再调 check_vessel_patch(plan, 规范化文本, 原话)。
"""
import copy
import json

import pytest

from zhihui.agent.general.guards import check_plan, check_vessel_patch, invented_values
from zhihui.agent.general.plan import Plan
from zhihui.agent.speech_normalize import normalize
from zhihui.cad.equipment import spec_ops as S
from zhihui.ir.vessel import VesselPatchIn

DEMO1 = "画一台立式储罐的设备图，内径1600，筒体高2400，标准椭圆封头，壁厚8，材料Q345R，设计压力0.6MPa，设计温度80℃，人孔DN500，进料口DN80，出料口DN80，两个液位计口DN25，四个耳式支座"
SPEC1 = {"kind": "vertical_vessel", "shell": {"inner_diameter_mm": 1600, "length_mm": 2400, "thickness_mm": 8, "material": "Q345R"}, "heads": [{"kind": "EHA"}],
         "design": {"design_pressure_mpa": 0.6, "design_temp_c": 80},
         "nozzles": [{"dn": 500, "kind": "manhole", "purpose_text": "人孔"}, {"dn": 80, "purpose_text": "进料口"}, {"dn": 80, "purpose_text": "出料口"},
                     {"dn": 25, "kind": "level_gauge", "purpose_text": "液位计口", "count": 2}],
         "supports": [{"kind": "lug", "count": 4}]}


def VP(tool: str, args: dict) -> Plan:
    return Plan.model_validate({"thought": "t", "intent_categories": ["chem_equipment"], "workflow_steps": [
        {"step_id": "s1", "target_agent": "tool_execution_agent", "depends_on": [], "action_summary": tool, "payload": {"tool": tool, "args": copy.deepcopy(args)}}]})


def probe(utt: str, args: dict, tool: str = "vessel_define"):
    """规范化口令 → check_vessel_patch。返回 (护栏结果, 护栏处理之后的 args)。"""
    norm = normalize(utt)
    plan = VP(tool, args)
    out = check_vessel_patch(plan, norm.text, utt)
    return out, plan.workflow_steps[0].payload["args"]


def create(spec: dict) -> dict:
    return {"op": "create", "spec": spec}


def spec1(**over) -> dict:
    s = copy.deepcopy(SPEC1)
    for k, v in over.items():
        s[k] = v
    return s


def codes(issues) -> list[str]:
    return [i.code for i in issues]


def clean(out) -> bool:
    return not out["block"] and not out["warn"] and not out["clarify"]


# ------------------------------------------------------------------ 基线：演示 1 的完整写法全部放行
def test_demo1_full_spec_passes_untouched():
    out, args = probe(DEMO1, create(SPEC1))
    assert clean(out), out
    assert args["spec"] == SPEC1                                                     # 一个字段都没被剔除


# ------------------------------------------------------------------ 编造的可默认字段：剔除 + E_DEFAULTABLE_FIELD，不拦截（不重规划）
def test_invented_orientation_is_stripped():
    s = spec1()
    s["nozzles"][1]["orientation_deg"] = 180                                          # 原话没说方位
    out, args = probe(DEMO1, create(s))
    assert not out["block"] and codes(out["warn"]) == ["E_DEFAULTABLE_FIELD"]
    assert "orientation_deg" not in args["spec"]["nozzles"][1] and args["spec"]["nozzles"][1]["dn"] == 80


def test_orientation_borrowed_from_design_temperature_is_stripped():
    """“设计温度80℃”里的“度”不是方位的说法：方位 80 不能借设计温度的数混过去。"""
    s = spec1()
    s["nozzles"][1]["orientation_deg"] = 80
    out, args = probe(DEMO1, create(s))
    assert not out["block"] and codes(out["warn"]) == ["E_DEFAULTABLE_FIELD"]
    assert "orientation_deg" not in args["spec"]["nozzles"][1]


def test_radial_half_of_diameter_is_stripped():
    s = spec1()
    s["nozzles"][1]["radial_mm"] = 800                                                # = 1600 / 2
    out, args = probe(DEMO1, create(s))
    assert not out["block"] and codes(out["warn"]) == ["E_DEFAULTABLE_FIELD"] and "radial_mm" not in args["spec"]["nozzles"][1]


@pytest.mark.parametrize("pn", [16, "PN16"])
def test_pn_twice_thickness_is_stripped(pn):
    s = spec1()
    s["nozzles"][1]["pn"] = pn                                                        # = 8 × 2，原话没有 PN
    out, args = probe(DEMO1, create(s))
    assert not out["block"] and codes(out["warn"]) == ["E_DEFAULTABLE_FIELD"] and "pn" not in args["spec"]["nozzles"][1]


def test_straight_flange_borrowed_from_dn25_is_stripped_but_head_kind_kept():
    s = spec1(heads=[{"kind": "EHA", "straight_flange_mm": 25}])                      # 25 借自“液位计口DN25”
    out, args = probe(DEMO1, create(s))
    assert not out["block"] and codes(out["warn"]) == ["E_DEFAULTABLE_FIELD"]
    assert args["spec"]["heads"] == [{"kind": "EHA"}]


def test_user_said_orientation_passes():
    out, args = probe("把进料口转到90度", {"op": "patch", "set": {"nozzles[purpose=进料口].orientation_deg": 90}})
    assert clean(out) and args["set"] == {"nozzles[purpose=进料口].orientation_deg": 90}


def test_orientation_number_from_another_clause_is_stripped():
    """90 出现在原话里，但不在“方位/度/转到”所在的分句里：可默认字段照样剔除。"""
    out, args = probe("进料口改成DN90，出料口不变", {"op": "patch", "set": {"nozzles[purpose=进料口].dn": 90, "nozzles[purpose=进料口].orientation_deg": 90}})
    assert not out["block"] and codes(out["warn"]) == ["E_DEFAULTABLE_FIELD"] and args["set"] == {"nozzles[purpose=进料口].dn": 90}


# ------------------------------------------------------------------ 单位：原样写数 + 单位放行；单位没换算（1.6 写成裸数）拦截
UTT_M = "画一台立式储罐的设备图，内径1.6米，筒体高2400，壁厚8，进料口DN80，四个耳式支座"


@pytest.mark.parametrize("di", [{"value": 1.6, "unit": "m"}, 1600, "1.6m"])
def test_diameter_in_meters_written_with_unit_or_converted_passes(di):
    s = {"kind": "vertical_vessel", "shell": {"inner_diameter_mm": di, "length_mm": 2400, "thickness_mm": 8}, "nozzles": [{"dn": 80, "purpose_text": "进料口"}],
         "supports": [{"kind": "lug", "count": 4}]}
    out, args = probe(UTT_M, create(s))
    assert clean(out), out
    assert args["spec"]["shell"]["inner_diameter_mm"] == di


def test_diameter_in_meters_written_as_bare_number_is_blocked():
    s = {"kind": "vertical_vessel", "shell": {"inner_diameter_mm": 1.6, "length_mm": 2400, "thickness_mm": 8}, "nozzles": [{"dn": 80, "purpose_text": "进料口"}],
         "supports": [{"kind": "lug", "count": 4}]}
    out, _ = probe(UTT_M, create(s))
    assert codes(out["block"]) == ["E_INVENTED_VALUE"] and "unit" in out["block"][0].message


@pytest.mark.parametrize("utt", ["介质是水，设计压力600千帕", "介质是水，设计压力600kPa"])
@pytest.mark.parametrize("p", [{"value": 600, "unit": "kPa"}, "600kPa", 0.6])
def test_pressure_in_kpa_passes(utt, p):
    out, args = probe(utt, {"op": "patch", "set": {"design.medium": "水", "design.design_pressure_mpa": p}})
    assert clean(out), out
    assert args["set"]["design.design_pressure_mpa"] == p


def test_pressure_in_kpa_written_as_bare_600_is_blocked():
    out, _ = probe("设计压力600千帕", {"op": "patch", "set": {"design.design_pressure_mpa": 600}})
    assert codes(out["block"]) == ["E_INVENTED_VALUE"]


# ------------------------------------------------------------------ 必问字段编造：拦截 E_INVENTED_VALUE（重规划）
@pytest.mark.parametrize("di", [1500, 3200, 800])                                     # 编造、倍数、一半
def test_invented_or_derived_diameter_is_blocked(di):
    s = spec1()
    s["shell"] = {**s["shell"], "inner_diameter_mm": di}
    out, _ = probe(DEMO1, create(s))
    assert codes(out["block"]) == ["E_INVENTED_VALUE"] and not out["warn"]


def test_doubled_length_is_blocked():
    s = spec1()
    s["shell"] = {**s["shell"], "length_mm": 4800}
    out, _ = probe(DEMO1, create(s))
    assert codes(out["block"]) == ["E_INVENTED_VALUE"]


def test_add_increment_must_be_user_number():
    out, args = probe("筒体加长500", {"op": "patch", "add": {"shell.length": 500}})
    assert clean(out) and args["add"] == {"shell.length": 500}
    out, _ = probe("筒体加长500", {"op": "patch", "add": {"shell.length": 1000}})
    assert codes(out["block"]) == ["E_INVENTED_VALUE"]


def test_nozzle_dn_must_be_user_number():
    out, _ = probe("上封头再加一个DN50的放空口", {"op": "patch", "upsert_nozzles": [{"dn": 65, "purpose_text": "放空口", "location": "top_head"}]})
    assert codes(out["block"]) == ["E_INVENTED_VALUE"]


# ------------------------------------------------------------------ 管口符号
def test_invented_mark_is_stripped():
    out, args = probe("上封头再加一个DN50的放空口", {"op": "patch", "upsert_nozzles": [{"mark": "a", "dn": 50, "purpose_text": "放空口", "location": "top_head"}]})
    assert not out["block"] and codes(out["warn"]) == ["E_DEFAULTABLE_FIELD"]
    assert args["upsert_nozzles"] == [{"dn": 50, "purpose_text": "放空口", "location": "top_head"}]


def test_user_said_mark_passes():
    out, args = probe("把b口改成DN100", {"op": "patch", "upsert_nozzles": [{"mark": "b", "dn": 100}]})
    assert clean(out) and args["upsert_nozzles"] == [{"mark": "b", "dn": 100}]
    out, args = probe("把b口改成DN100", {"op": "patch", "set": {"nozzles[b].dn": 100}})
    assert clean(out) and args["set"] == {"nozzles[b].dn": 100}


def test_mark_letter_inside_unit_does_not_count():
    """“0.6MPa”里的 a 不是用户说的管口符号。"""
    out, args = probe("设计压力0.6MPa，上封头加一个DN50的放空口", {"op": "patch", "upsert_nozzles": [{"mark": "a", "dn": 50, "purpose_text": "放空口", "location": "top_head"}]})
    assert codes(out["warn"]) == ["E_DEFAULTABLE_FIELD"] and "mark" not in args["upsert_nozzles"][0]


# ------------------------------------------------------------------ 照抄字段：材料、用途
def test_material_substring_passes_and_invented_material_is_blocked():
    out, _ = probe(DEMO1, create(SPEC1))
    assert clean(out)
    s = spec1()
    s["shell"] = {**s["shell"], "material": "Q235B"}
    out, _ = probe(DEMO1, create(s))
    assert codes(out["block"]) == ["E_INVENTED_VALUE"]


def test_material_copied_to_nozzles_is_blocked_when_not_said():
    s = spec1()
    s["nozzles"][1]["pipe_material"] = "20"                                          # 原话里没有“20”号钢
    out, _ = probe(DEMO1, create(s))
    assert out["block"] or out["warn"], "编造的接管材料必须被拦下或剔除"


def test_purpose_synonym_passes():
    utt = "画一个卧式储罐装配图，内径2000，切线长6000，壁厚10，两个鞍座，管口有进料DN100、出料DN100、人孔DN500"
    s = {"kind": "horizontal_vessel", "shell": {"inner_diameter_mm": 2000, "tangent_length_mm": 6000, "thickness_mm": 10},
         "nozzles": [{"dn": 100, "purpose_text": "进料口"}, {"dn": 100, "purpose": "出料口"}, {"dn": 500, "kind": "manhole", "purpose_text": "人孔"}],
         "supports": [{"kind": "saddle", "count": 2}]}
    out, args = probe(utt, create(s))
    assert clean(out), out
    assert args["spec"]["nozzles"][0]["purpose_text"] == "进料口"


def test_invented_purpose_is_blocked():
    out, _ = probe("上封头再加一个DN50的放空口", {"op": "patch", "upsert_nozzles": [{"dn": 50, "purpose_text": "排净口", "location": "top_head"}]})
    assert codes(out["block"]) == ["E_INVENTED_VALUE"]


# ------------------------------------------------------------------ “高/长”口径
def _vert(length_key: str, length: float, utt_len: str) -> tuple:
    utt = f"画一台立式储罐的设备图，内径1600，{utt_len}，壁厚8，进料口DN80，四个耳式支座"
    s = {"kind": "vertical_vessel", "shell": {"inner_diameter_mm": 1600, length_key: length, "thickness_mm": 8}, "nozzles": [{"dn": 80, "purpose_text": "进料口"}],
         "supports": [{"kind": "lug", "count": 4}]}
    return probe(utt, create(s))


@pytest.mark.parametrize("utt_len,n", [("高3000", 3000), ("总高3600", 3600), ("高度3000", 3000)])
def test_bare_height_turns_into_clarify(utt_len, n):
    out, args = _vert("length_mm", n, utt_len)
    assert not out["block"] and [c["path"] for c in out["clarify"]] == ["shell.length"]
    assert "length_mm" not in args["spec"]["shell"]                                    # 剔除后转成反问，不让模型猜
    assert any(str(n) in o for o in out["clarify"][0]["options"])


def test_shell_height_passes():
    out, args = _vert("length_mm", 3000, "筒体高3000")
    assert clean(out) and args["spec"]["shell"]["length_mm"] == 3000


def test_tangent_length_passes_and_bare_length_clarifies():
    out, _ = _vert("tangent_length_mm", 6000, "切线长6000")
    assert clean(out)
    out, args = _vert("tangent_length_mm", 6000, "长6000")
    assert [c["path"] for c in out["clarify"]] == ["shell.length"] and "tangent_length_mm" not in args["spec"]["shell"]


# ------------------------------------------------------------------ 翻译类文字字段
@pytest.mark.parametrize("utt,kind,ok", [
    ("画一个储罐的设备图，内径1600，筒体高2400，壁厚8，进料口DN80", "vertical_vessel", False),        # 没说立式/卧式：设备类型是必问字段
    ("画一个储罐的设备图，内径1600，筒体高2400，壁厚8，进料口DN80", "horizontal_vessel", False),
    ("画一个卧式储罐的设备图，内径1600，切线长2400，壁厚8，进料口DN80", "vertical_vessel", False),
    ("画一个立式储罐的设备图，内径1600，筒体高2400，壁厚8，进料口DN80", "vertical_vessel", True),
    ("画一个储罐的设备图，内径1600，筒体高2400，壁厚8，进料口DN80，四个耳式支座", "vertical_vessel", True),   # 耳座只用于立式
    ("画一个储罐的设备图，内径1600，切线长2400，壁厚8，进料口DN80，两个鞍座", "horizontal_vessel", True),
])
def test_vessel_kind_must_come_from_the_words(utt, kind, ok):
    s = {"kind": kind, "shell": {"inner_diameter_mm": 1600, "thickness_mm": 8}, "nozzles": [{"dn": 80, "purpose_text": "进料口"}]}
    out, args = probe(utt, create(s))
    if ok:
        assert clean(out), out
    else:
        assert codes(out["block"]) == ["E_INVENTED_VALUE"] and "设备类型" in out["block"][0].message


def test_lug_without_saying_lug_is_blocked():
    utt = "画一台立式储罐的设备图，内径1600，筒体高2400，壁厚8，进料口DN80，四个支座"
    s = {"kind": "vertical_vessel", "shell": {"inner_diameter_mm": 1600, "length_mm": 2400, "thickness_mm": 8}, "nozzles": [{"dn": 80, "purpose_text": "进料口"}],
         "supports": [{"kind": "lug", "count": 4}]}
    out, _ = probe(utt, create(s))
    assert codes(out["block"]) == ["E_INVENTED_VALUE"] and "支座" in out["block"][0].message
    out, _ = probe(utt.replace("四个支座", "四个耳座"), create(s))
    assert clean(out)


def test_head_kind_translation_needs_the_words():
    out, args = probe(DEMO1, create(SPEC1))
    assert clean(out) and args["spec"]["heads"] == [{"kind": "EHA"}]
    out, args = probe(DEMO1.replace("标准椭圆封头，", ""), create(SPEC1))
    assert not out["block"] and codes(out["warn"]) == ["E_DEFAULTABLE_FIELD"] and args["spec"]["heads"] == [{}]


def test_head_thickness_copied_from_shell_is_stripped():
    s = spec1(heads=[{"kind": "EHA", "thickness_mm": 8}])                            # 封头厚度同筒体是默认规则 R-HEAD-02，不是用户说的
    out, args = probe(DEMO1, create(s))
    assert not out["block"] and codes(out["warn"]) == ["E_DEFAULTABLE_FIELD"] and args["spec"]["heads"] == [{"kind": "EHA"}]
    out, args = probe(DEMO1.replace("标准椭圆封头", "标准椭圆封头厚10"), create(spec1(heads=[{"kind": "EHA", "thickness_mm": 10}])))
    assert clean(out) and args["spec"]["heads"] == [{"kind": "EHA", "thickness_mm": 10}]


def test_location_translation_needs_the_words():
    out, args = probe("再加一个DN50的放空口", {"op": "patch", "upsert_nozzles": [{"dn": 50, "purpose_text": "放空口", "location": "top_head"}]})
    assert not out["block"] and codes(out["warn"]) == ["E_DEFAULTABLE_FIELD"] and "location" not in args["upsert_nozzles"][0]
    out, args = probe("上封头再加一个DN50的放空口", {"op": "patch", "upsert_nozzles": [{"dn": 50, "purpose_text": "放空口", "location": "top_head"}]})
    assert clean(out) and args["upsert_nozzles"][0]["location"] == "top_head"


def test_count_from_chinese_numeral_passes():
    out, args = probe(DEMO1, create(SPEC1))
    assert clean(out) and args["spec"]["nozzles"][3]["count"] == 2 and args["spec"]["supports"][0]["count"] == 4
    s = spec1()
    s["nozzles"][3]["count"] = 3                                                      # 原话是“两个”
    out, _ = probe(DEMO1, create(s))
    assert out["block"] or out["warn"]


# ------------------------------------------------------------------ 出图参数
def test_generate_drawing_sheet_and_scale():
    out, args = probe("换成A2图幅重新排版", {"sheet": "A2"}, tool="vessel_generate_drawing")
    assert clean(out) and args == {"sheet": "A2"}
    out, args = probe("出一张管口方位图", {"views": ["orientation"], "scale": "1:10"}, tool="vessel_generate_drawing")
    assert not out["block"] and codes(out["warn"]) == ["E_DEFAULTABLE_FIELD"] and args == {"views": ["orientation"]}


def test_engine_injected_fields_are_ignored():
    out, _ = probe("把进料口转到90度", {"op": "patch", "set": {"nozzles[purpose=进料口].orientation_deg": 90}, "turn_id": "t12345", "regenerate": True, "confirmed": False})
    assert clean(out)


def test_general_check_plan_skips_vessel_tools():
    """通用护栏的常量表/倍数放行不再作用于设备补丁（任务书 9.3 开头）。"""
    s = spec1()
    s["nozzles"][1]["orientation_deg"] = 180
    plan = VP("vessel_define", create(s))
    assert check_plan(plan, normalize(DEMO1).text, DEMO1) == []
    out = check_vessel_patch(plan, normalize(DEMO1).text, DEMO1)
    assert codes(out["warn"]) == ["E_DEFAULTABLE_FIELD"]


# ------------------------------------------------------------------ 引擎侧复核：invented_values
def _spec(patch: dict, turn: str = "t1"):
    spec, _, _ = S.apply_create(VesselPatchIn.model_validate(patch), turn)
    return spec


def test_invented_values_accepts_user_numbers_and_unit_conversion():
    assert invented_values(_spec(SPEC1), "t1", DEMO1) == []
    sp = _spec({"kind": "vertical_vessel", "shell": {"inner_diameter_mm": {"value": 1.6, "unit": "m"}, "length_mm": 2400, "thickness_mm": 8}})
    assert sp.shell.inner_diameter_mm == 1600
    assert invented_values(sp, "t1", UTT_M) == []


def test_invented_values_reports_numbers_not_in_utterance():
    s = spec1()
    s["shell"] = {**s["shell"], "inner_diameter_mm": 1500}
    bad = invented_values(_spec(s), "t1", DEMO1)
    assert len(bad) == 1 and "1500" in bad[0] and "inner_diameter" in bad[0]
    s = spec1()
    s["shell"] = {**s["shell"], "inner_diameter_mm": 3200}                          # 倍数也不放行
    assert len(invented_values(_spec(s), "t1", DEMO1)) == 1


def test_invented_values_only_checks_this_turn_and_skips_count_one():
    sp = _spec({"kind": "vertical_vessel", "shell": {"inner_diameter_mm": 1500, "length_mm": 2400, "thickness_mm": 8}, "nozzles": [{"dn": 80, "purpose_text": "进料口", "count": 1}]}, "t0")
    assert invented_values(sp, "t1", "进料口DN80") == []                              # 上一轮的用户值不在本轮复核范围
    assert any("1500" in b for b in invented_values(sp, "t0", "内径1600，筒体高2400，壁厚8，进料口DN80"))


def test_invented_values_orientation_modulo_360():
    sp = _spec({"kind": "vertical_vessel", "shell": {"inner_diameter_mm": 1600, "length_mm": 2400, "thickness_mm": 8},
                "nozzles": [{"dn": 80, "purpose_text": "进料口", "orientation_deg": 270}]})
    assert invented_values(sp, "t1", "内径1600，筒体高2400，壁厚8，进料口DN80，方位-90度") == []
    assert invented_values(sp, "t1", "内径1600，筒体高2400，壁厚8，进料口DN80，方位90度") != []


# ------------------------------------------------------------------ 模型写出的畸形参数：护栏不能崩（崩了整条任务就是内部错误）
MALFORMED = [
    {"op": "create", "spec": None},
    {"op": "create", "spec": "kind=vertical"},
    {"op": "create", "spec": {"kind": "vertical_vessel", "shell": None, "nozzles": None, "supports": None, "heads": None, "design": None}},
    {"op": "create", "spec": {"kind": "vertical_vessel", "nozzles": [None, 3, {"dn": [80], "count": "两个", "purpose_text": None}, {"dn": {"value": "x", "unit": "mm"}}]}},
    {"op": "create", "spec": {"kind": "vertical_vessel", "shell": {"inner_diameter_mm": "abc"}}},
    {"op": "create", "spec": {"kind": ["vertical_vessel"]}},
    {"op": "patch", "set": {"": 1, "nozzles[": 2, "nozzles[purpose=进料口].dn": [1, 2], "shell.length": {"value": 1, "unit": "furlong"}}},
    {"op": "patch", "set": {"design.design_pressure_mpa": True, "design.medium": {"x": 1}}},
    {"op": "patch", "add": {"shell.length": "加500"}},
    {"op": "patch", "add": None, "set": None, "upsert_nozzles": "x", "remove_nozzles": ["a"]},
    {"op": "patch", "upsert_nozzles": [None, 3, {"dn": 50}], "upsert_supports": [None, {"kind": "lug", "count": [4]}]},
    {"op": "patch", "sheet": "A2", "scale": 5},
]


@pytest.mark.parametrize("args", MALFORMED, ids=lambda a: json.dumps(a, ensure_ascii=False)[:50])
def test_malformed_arguments_never_crash_the_guard(args):
    plan = VP("vessel_define", args)
    out = check_vessel_patch(plan, normalize("画一个立式储罐，进料口DN80").text, "画一个立式储罐，进料口DN80")
    assert set(out) == {"block", "warn", "clarify"}
    assert check_plan(plan, "画一个立式储罐", "") == []                                           # 通用护栏对设备函数直接跳过，也不崩


def test_malformed_generate_drawing_arguments_never_crash_the_guard():
    for args in ({"views": "orientation", "scale": 5}, {"views": None, "sheet": None}, {"export": ["dxf"], "sheet": ["A2"]}):
        out = check_vessel_patch(VP("vessel_generate_drawing", args), "出图", "出图")
        assert set(out) == {"block", "warn", "clarify"}


def test_guard_only_looks_at_the_two_writing_tools():
    """vessel_query / vessel_lookup_standard / vessel_check 是只读的：参数里的数不查来源，也不会被改。"""
    for tool, args in (("vessel_lookup_standard", {"table_id": "pipe_dims", "keys": {"dn": 65, "series": "B"}}), ("vessel_query", {"what": "nozzles", "filter": "purpose=进料口"}), ("vessel_check", {})):
        plan = VP(tool, args)
        out = check_vessel_patch(plan, "查一下", "查一下")
        assert clean(out) and plan.workflow_steps[0].payload["args"] == args


def test_guard_checks_vessel_define_wrapped_in_a_batch():
    """模型把 vessel_define 塞进 cad_batch_execute：护栏照样查，也照样剔除（原地修改子步骤的参数）。"""
    s = spec1()
    s["nozzles"][1]["orientation_deg"] = 180
    plan = Plan.model_validate({"thought": "t", "intent_categories": ["chem_equipment"], "workflow_steps": [{"step_id": "s1", "target_agent": "tool_execution_agent", "payload": {
        "tool": "cad_batch_execute", "args": {"steps": [{"tool": "vessel_define", "args": {"op": "create", "spec": s}}]}}}]})
    out = check_vessel_patch(plan, normalize(DEMO1).text, DEMO1)
    assert codes(out["warn"]) == ["E_DEFAULTABLE_FIELD"]
    assert "orientation_deg" not in plan.workflow_steps[0].payload["args"]["steps"][0]["args"]["spec"]["nozzles"][1]
