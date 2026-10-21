"""设备图提示词与 few-shot（任务书 9.1、9.2）：每条示例三重校验，提示词按意图类别拼装，计划协议里和设备图有关的部分。

三重校验 = ① parse_plan 通过（含“函数在 chem_equipment 子集里”）② 每个工具步骤过函数的参数模型 ③ check_plan 与 check_vessel_patch 对示例口述（先规范化）一个问题都不报。
再加一道“示例能真的跑通”：把示例按顺序在真实的设备图上执行（Executor，不经 Planner）。
"""
import asyncio
import copy
import json
import re

import pytest

import zhihui.tools.general  # noqa: F401  注册函数
from gen_helpers import Env
from zhihui.agent.general import planner as planner_mod, prompts as P
from zhihui.agent.general.backend import LocalBackend
from zhihui.agent.general.executor import Executor
from zhihui.agent.general.guards import check_plan, check_vessel_patch
from zhihui.agent.general.plan import Plan, PlanError, parse_plan, plan_dict, strip_engine_fields, validate_plan
from zhihui.agent.general.planner import allowed_tools
from zhihui.agent.speech_normalize import normalize
from zhihui.tools.general.registry import CATEGORIES, REGISTRY, tools_of
from zhihui.tools.general.view_tools import build_context

SHOTS = P.EQUIP_FEW_SHOTS
EQUIP_TOOLS = {t.name for t in REGISTRY.values() if t.category == "chem_equipment"}


def run(coro):
    return asyncio.run(coro)


def sid(i: int) -> str:
    return f"shot{i}"


def tool_calls(plan_json: dict):
    """示例里的所有工具步骤 → (工具名, args)。"""
    for st in plan_json.get("workflow_steps", []):
        if st["target_agent"] == "tool_execution_agent":
            yield st["payload"]["tool"], st["payload"].get("args", {})


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


# ------------------------------------------------------------------ few-shot 三重校验
def test_equipment_few_shots_are_at_most_eight_and_cover_the_planned_scenarios():
    assert 6 <= len(SHOTS) <= 8
    ops = {(tool, a.get("op")) for _, p in SHOTS for tool, a in tool_calls(p)}
    assert ("vessel_define", "create") in ops and ("vessel_define", "patch") in ops
    kinds = {a["spec"]["kind"] for _, p in SHOTS for tool, a in tool_calls(p) if tool == "vessel_define" and a.get("op") == "create"}
    assert kinds == {"vertical_vessel", "horizontal_vessel"}
    patch_keys = {k for _, p in SHOTS for tool, a in tool_calls(p) if a.get("op") == "patch" for k in a}
    assert {"set", "add", "upsert_nozzles"} <= patch_keys                                         # 转方位、加长、加管口各有示例
    asks = [p for _, p in SHOTS if p["clarification_needed"]]
    assert any(it["path"] == "drawing_type" for p in asks for it in p["clarification"]["items"])  # 只有设备名：先问图种
    assert any(it["path"] == "shell.thickness_mm" for p in asks for it in p["clarification"]["items"])   # 壁厚计算：如实说明，请用户给厚度


@pytest.mark.parametrize("i", range(len(SHOTS)), ids=sid)
def test_few_shot_parses_as_a_plan_inside_the_equipment_tool_subset(i):
    utt, plan = SHOTS[i]
    parsed = parse_plan(json.dumps(plan, ensure_ascii=False), allowed_tools(["chem_equipment"]))
    assert parsed.intent_categories == ["chem_equipment"]
    assert bool(parsed.workflow_steps) != parsed.clarification_needed                              # 要么出步骤，要么反问
    assert all(c["tool"] in EQUIP_TOOLS for c in (st.payload for st in parsed.workflow_steps))     # 示例不教模型用通用函数拼设备


@pytest.mark.parametrize("i", range(len(SHOTS)), ids=sid)
def test_few_shot_args_pass_the_tool_argument_models(i):
    _, plan = SHOTS[i]
    for tool, args in tool_calls(plan):
        REGISTRY[tool].model.model_validate(args)
        assert not {"turn_id", "confirmed"} & set(args), "turn_id / confirmed 不归模型写，示例里也不能出现"


@pytest.mark.parametrize("i", range(len(SHOTS)), ids=sid)
def test_few_shot_passes_both_guards_on_its_own_utterance(i):
    utt, plan = SHOTS[i]
    norm = normalize(utt)
    parsed = parse_plan(json.dumps(plan, ensure_ascii=False))
    before = plan_dict(parsed)
    assert check_plan(parsed, norm.text, utt) == []
    out = check_vessel_patch(parsed, norm.text, utt)
    assert out == {"block": [], "warn": [], "clarify": []}, out
    assert plan_dict(parsed) == before, "护栏不该改动示例里的任何字段（改了就说明示例里有编造的值）"


@pytest.mark.parametrize("i", range(len(SHOTS)), ids=sid)
def test_few_shot_clarification_items_use_allowed_paths(i):
    _, plan = SHOTS[i]
    parsed = Plan.model_validate(plan)
    assert validate_plan(parsed) == []
    for it in parsed.clarification.items:
        assert it.label and it.path


def test_few_shot_guards_really_catch_a_corrupted_copy():
    """防止三重校验空转：把示例 1 里的内径改成口述里没有的数，护栏必须拦下；加一个口述没说的方位，必须被剔除。"""
    utt, plan = SHOTS[0]
    bad = copy.deepcopy(plan)
    bad["workflow_steps"][0]["payload"]["args"]["spec"]["shell"]["inner_diameter_mm"] = 1500
    out = check_vessel_patch(parse_plan(json.dumps(bad, ensure_ascii=False)), normalize(utt).text, utt)
    assert [i.code for i in out["block"]] == ["E_INVENTED_VALUE"]
    bad = copy.deepcopy(plan)
    bad["workflow_steps"][0]["payload"]["args"]["spec"]["nozzles"][1]["orientation_deg"] = 180
    out = check_vessel_patch(parse_plan(json.dumps(bad, ensure_ascii=False)), normalize(utt).text, utt)
    assert not out["block"] and [i.code for i in out["warn"]] == ["E_DEFAULTABLE_FIELD"]


def test_few_shots_execute_on_a_real_equipment_drawing(env):
    """示例教的调用要真的能跑：立式储罐上依次执行 新建、转方位、加长、加管口、改设计数据；卧式示例另开一张图。"""
    did = env.new("hg_equipment_a1")
    be = LocalBackend(env.svc)
    for i in (0, 2, 3, 4, 6):
        res = run(Executor(be, did).run(Plan.model_validate(SHOTS[i][1])))
        assert res.status == "done", (i, res.error)
    v = env.d(did).vessel
    assert v.shell.length_mm == 2900                                                            # 2400 + 500
    assert {n.purpose for n in v.nozzles} >= {"人孔", "进料口", "出料口", "液位计口", "放空口"}
    assert [n.orientation_deg for n in v.nozzles if n.purpose == "进料口"] == [90.0]
    assert v.design.medium == "液氨" and v.design.design_pressure_mpa == 2.16
    assert sum(1 for n in v.nozzles if n.purpose == "液位计口") == 2                             # “两个液位计口DN25”写一个管口 count=2
    did2 = env.new("hg_equipment_a1")
    res = run(Executor(be, did2).run(Plan.model_validate(SHOTS[5][1])))
    assert res.status == "done", res.error
    v = env.d(did2).vessel
    assert v.kind == "horizontal_vessel" and v.shell.tangent_length_mm == 6000 and v.shell.length_mm and v.shell.length_mm < 6000   # 切线长由程序换算筒体长度
    assert sorted(n.dn for n in v.nozzles) == [100, 100, 500]


# ------------------------------------------------------------------ 系统提示词拼装
def ctx_of(env, template="hg_equipment_a1"):
    return build_context(env.d(env.new(template)))


def test_prompt_has_equipment_section_and_shots_only_for_the_equipment_category(env):
    ctx = ctx_of(env)
    on = P.build_planner_prompt(ctx, ["chem_equipment"])
    assert "# 化工设备图" in on and on.count("# 化工设备图") == 1
    assert "- vessel_define(" in on and "- vessel_generate_drawing(" in on
    for utt, _ in SHOTS:
        assert utt in on, utt                                                                   # 8 条设备示例都在
    assert "把它往左移 10" in on and "刚才那步不要了" in on                                        # 另带两条通用示例（指代、撤销）
    off = P.build_planner_prompt(ctx, ["2d_drafting"])
    assert "# 化工设备图" not in off and "vessel_" not in off
    assert not any(utt in off for utt, _ in SHOTS)
    assert "示例 1" in off and "建一套国标机械图层" in off                                        # 通用示例照旧
    assert "# 化工设备图" in P.build_planner_prompt(ctx, ["2d_editing", "chem_equipment"])        # 和别的类别并用时也拼进去
    assert "# 化工设备图" not in P.build_planner_prompt(ctx, None)


def test_prompt_is_fully_formatted_and_has_no_stray_braces(env):
    """EQUIP_SECTION 是 format 的参数不是模板：写成 {{ 会原样漏进提示词（缺陷回归）。"""
    ctx = ctx_of(env)
    for cats in (["chem_equipment"], ["2d_drafting"], None, list(CATEGORIES)):
        p = P.build_planner_prompt(ctx, cats)
        assert "{{" not in p, (cats, p[p.find("{{") - 60: p.find("{{") + 60])
        for ph in ("{context}", "{tool_catalog}", "{category_list}", "{equipment_section}", "{few_shots}"):
            assert ph not in p
    sec = P.build_planner_prompt(ctx, ["chem_equipment"])
    assert '{"value": 1.6, "unit": "m"}' in sec and '{"shell.length": 500}' in sec               # 单位写法和 add 写法原样给出


def test_prompt_category_list_comes_from_registry_categories(env):
    p = P.build_planner_prompt(ctx_of(env), ["chem_equipment"])
    listed = re.search(r"intent_categories 只能从这几个里选，不要自造：([^。]+)。", p).group(1).split("、")
    assert listed == [c for c in CATEGORIES if c not in ("system", "perception")]
    assert "chem_equipment" in listed and "chem_equipment" in CATEGORIES


def test_equipment_section_states_the_hard_rules():
    s = P.EQUIP_SECTION
    for must in ("vessel_define", "不要写用户没说的", "不要计算任何数", "必问字段", "P0 不支持壁厚计算", "drawing_type", "vessel_generate_drawing",
                 "tangent_length_mm", "nozzles[purpose=进料口]", "count=2"):
        assert must in s, must
    assert "turn_id" not in s and "confirmed" not in s                                           # 引擎字段不让模型知道


def test_vessel_define_catalog_line_is_short_and_does_not_unfold_the_spec():
    line = P.tool_line("vessel_define")
    assert line.startswith("- vessel_define(") and "\n" not in line and len(line) <= 1000, len(line)
    for hidden in ("weld_joint_coeff", "reinforcement", "cone_half_angle_deg", "pipe_od_mm", "uid", "sources", "internals", "shell_side", "insulation_thickness_mm"):
        assert hidden not in line, hidden                                                       # 展开 VesselSpec 会把可默认字段当参数展示，等于引导模型编数
    for shown in ("op*", "nozzles[purpose=进料口]", "tangent_length_mm", "可默认字段只有用户明确说了才写", "orientation_deg", "straight_flange_mm"):
        assert shown in line, shown


def test_every_equipment_tool_has_a_one_line_catalog_entry():
    cat = P.compact_catalog(["chem_equipment"]).splitlines()
    for name in sorted(EQUIP_TOOLS):
        lines = [l for l in cat if l.startswith(f"- {name}(")]
        assert len(lines) == 1, name
    assert all(l.startswith("- ") for l in cat), "每个函数一行"
    assert not any(l.startswith("- draw_") for l in P.compact_catalog(["chem_equipment"]).splitlines()), "设备图子集里没有画线画弧"
    assert {"vessel_define", "vessel_generate_drawing", "vessel_query", "vessel_lookup_standard", "vessel_check"} == EQUIP_TOOLS


def test_few_shot_text_renders_every_shot_as_valid_json():
    text = P.few_shot_text(10, equipment=True)
    blocks = re.findall(r"```json\n(.*?)\n```", text, re.S)
    assert len(blocks) == len(SHOTS) + 2
    for b in blocks:
        Plan.model_validate(json.loads(b))
    assert "vessel_define" not in P.few_shot_text(10, equipment=False)
    assert text.count("**示例 ") == len(SHOTS) + 2


def test_equipment_prompt_stays_in_a_reasonable_size(env):
    on = P.build_planner_prompt(ctx_of(env), ["chem_equipment"])
    assert len(on) < 30000, len(on)
    assert len(P.EQUIP_SECTION) < 2500 and len(P.EQUIP_SECTION) > 500


def test_planner_in_real_mode_sends_the_equipment_prompt_for_the_equipment_category(env, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    systems = []
    utt, plan = SHOTS[0]

    async def fake(system, user, usage, role="planner", max_tokens=4000, timeout=60, strict=False, json_mode=False):
        systems.append(system)
        return json.dumps(plan, ensure_ascii=False)
    monkeypatch.setattr(planner_mod, "chat_text", fake)
    ctx = run(LocalBackend(env.svc).context(env.new("hg_equipment_a1")))
    got = run(planner_mod.Planner(False).plan(normalize(utt).text, utt, ctx, ["chem_equipment", "2d_drafting"]))
    assert len(systems) == 1 and "# 化工设备图" in systems[0] and "- vessel_define(" in systems[0]
    assert got.workflow_steps[0].payload["tool"] == "vessel_define"
    # 没有设备类别时模型要的 vessel_define 不在子集里：重来时目录换成全部函数（含设备函数）
    systems.clear()
    got = run(planner_mod.Planner(False).plan(normalize(utt).text, utt, ctx, ["2d_drafting"]))
    assert len(systems) >= 2 and "- vessel_define(" not in systems[0] and "- vessel_define(" in systems[1]


# ------------------------------------------------------------------ 计划协议里和设备图有关的部分
def test_intent_category_names_made_up_for_equipment_map_to_chem_equipment():
    for name in ("chem_equipment", "vessel_design", "equipment_drawing", "chemical_equipment_layout", "tank_drawing", "pressure_vessel"):
        p = Plan.model_validate({"intent_categories": [name], "workflow_steps": [], "clarification_needed": True, "clarification": {"question": "q"}})
        assert p.intent_categories == ["chem_equipment"], name
    p = Plan.model_validate({"intent_categories": ["layout_and_print", "pfd", "胡说八道"], "workflow_steps": [], "clarification_needed": True, "clarification": {"question": "q"}})
    assert p.intent_categories == ["layout_plotting", "pfd"]                                    # 别的类别的映射不受影响


def test_all_registry_categories_are_accepted_as_intent_categories():
    p = Plan.model_validate({"intent_categories": list(CATEGORIES), "workflow_steps": [], "clarification_needed": True, "clarification": {"question": "q"}})
    assert p.intent_categories == list(CATEGORIES)


def test_clarification_items_are_strict_and_paths_are_checked():
    with pytest.raises(Exception):
        Plan.model_validate({"clarification_needed": True, "clarification": {"items": [{"path": "shell.length", "label": "x", "bogus": 1}]}})
    ok = {"clarification_needed": True, "clarification": {"items": [{"path": p, "label": p} for p in
                                                                    ("drawing_type", "nozzles", "shell.length", "shell.inner_diameter_mm", "supports.kind", "nozzles[b].dn", "heads[top].kind")]}}
    assert validate_plan(Plan.model_validate(ok)) == []
    bad = Plan.model_validate({"clarification_needed": True, "clarification": {"items": [{"path": "shell.volume", "label": "容积"}, {"path": "../etc", "label": "x"}]}})
    issues = validate_plan(bad)
    assert len(issues) == 1 and "shell.volume" in issues[0] and "待问项路径" in issues[0]
    assert validate_plan(Plan.model_validate({"clarification_needed": True})) != []              # 既没有问题也没有待问项
    assert validate_plan(Plan.model_validate({"clarification_needed": True, "clarification": {"items": [{"path": "drawing_type", "label": "图种"}]}})) == []


def test_vessel_tools_are_only_allowed_with_the_equipment_category():
    plan = Plan.model_validate(SHOTS[0][1])
    assert validate_plan(plan, allowed_tools(["chem_equipment"])) == []
    assert any("不在当前意图类别" in i for i in validate_plan(plan, allowed_tools(["2d_drafting"])))
    assert {t.name for t in tools_of(["chem_equipment"], include_system=False)} == EQUIP_TOOLS


def test_parse_plan_drops_engine_owned_fields_the_model_wrote():
    """turn_id 由引擎注入；confirmed 只能在用户确认之后由引擎置 true——模型写了也丢掉（否则能绕过“替换现有设备参数”的确认）。"""
    plan = copy.deepcopy(SHOTS[0][1])
    plan["workflow_steps"][0]["payload"]["args"].update({"turn_id": "t-evil", "confirmed": True})
    parsed = parse_plan(json.dumps(plan, ensure_ascii=False))
    args = parsed.workflow_steps[0].payload["args"]
    assert "turn_id" not in args and "confirmed" not in args and args["op"] == "create"
    batch = {"thought": "", "intent_categories": ["chem_equipment"], "workflow_steps": [{"step_id": "s1", "target_agent": "tool_execution_agent", "payload": {"tool": "cad_batch_execute", "args": {"steps": [
        {"tool": "vessel_define", "args": {"op": "delete", "confirmed": True, "turn_id": "x"}}, {"tool": "erase_entities", "args": {"entity_ids": ["L1"], "confirmed": True}}]}}}]}
    parsed = parse_plan(json.dumps(batch), allowed_tools(["chem_equipment", "2d_editing"]))
    steps = parsed.workflow_steps[0].payload["args"]["steps"]
    assert steps[0]["args"] == {"op": "delete"}
    assert steps[1]["args"]["confirmed"] is True                                                 # 通用函数的 confirmed 不动
    assert strip_engine_fields(parsed) == []                                                     # 再剥一次没有东西了


def test_strip_engine_fields_does_not_touch_a_confirmed_plan_restored_from_state():
    """用户确认之后恢复执行的计划是 Plan.model_validate(state["plan"])，不走 parse_plan：confirmed 要留着。"""
    plan = copy.deepcopy(SHOTS[0][1])
    plan["workflow_steps"][0]["payload"]["args"]["confirmed"] = True
    restored = Plan.model_validate(plan)
    assert restored.workflow_steps[0].payload["args"]["confirmed"] is True
    with pytest.raises(PlanError):
        parse_plan("{}")                                                                         # 既没有步骤也没有反问
