"""真实大模型基准（2026-10-20）暴露出来的问题的回归测试：不调用真实模型，用桩模型和确定性数据复现。"""
import asyncio
import json

import pytest

from gen_helpers import Env
from zhihui.agent.general import planner as planner_mod, prompts
from zhihui.agent.general.backend import LocalBackend
from zhihui.agent.general.router import categories_of


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


# ---------------------------------------------------------------- 路由：关键词路由漏类别会让模型拿不到该用的函数
@pytest.mark.parametrize("phrase,cat", [
    ("把横线L1超出竖线L2的部分剪掉，拾取点在100,0", "2d_editing"),
    ("把C2的图层和颜色刷成跟C1一样", "2d_editing"),
    ("把圆右边那个点拖到80,30", "2d_editing"),
    ("切换到三维建模空间", "environment_setup"),
    ("切换到二维草图空间", "environment_setup"),
])
def test_router_keeps_the_category_that_has_the_needed_tool(phrase, cat):
    assert cat in categories_of(phrase)


# ---------------------------------------------------------------- 选择集：模型写的筛选条件键名不精确，不能变成“内部错误”
def test_select_entities_filter_accepts_common_aliases(env):
    did = env.new("blank_a3")
    env.call("draw_circular_primitives", did, curve_type="circle", method="center_radius", center="0,0", radius=5)
    env.call("draw_linear_primitives", did, primitive_type="line", points=["0,0", "10,0"])
    r = env.call("select_entities", did, selection_mode="filter", filter_criteria={"type": "circle"})
    assert r["data"]["count"] == 1 and env.d(did).session.selection == ["C1"]
    r = env.call("select_entities", did, selection_mode="filter", filter_criteria={"entity_type": ["line"]})
    assert r["data"]["count"] == 1 and env.d(did).session.selection == ["L2"]


def test_select_entities_filter_with_unknown_key_is_a_clear_bad_args(env):
    did = env.new("blank_a3")
    e = env.err("select_entities", did, selection_mode="filter", filter_criteria={"colour_name": "red"})
    assert e["error_code"] == "BAD_ARGS" and "可用条件" in e["hint"] and "entity_types" in e["hint"]


# ---------------------------------------------------------------- 动态块：拉伸参数没写要拉伸的图元，不能悄悄成为摆设
def test_linear_stretch_without_geometry_refs_is_refused_with_the_block_entity_ids(env):
    did = env.new("blank_a3")
    env.call("draw_polygons_and_meshes", did, shape_type="rectangle", corner1="0,0", width=100, height=60)
    env.call("create_and_insert_block", did, action="define_block", block_name="PLATE", source_entity_ids=["PL1"], base_point="0,0", keep_source="convert")
    e = env.err("manage_dynamic_block", did, action="create_dynamic_def", block_name="PLATE",
                parameters_and_actions=[{"param_name": "宽度", "param_type": "linear_stretch", "default_value": 100}])
    assert e["error_code"] == "INVALID_ARGS" and "geometry_refs" in e["message"] and "E1" in e["hint"]


# ---------------------------------------------------------------- 提示词：模型要看得见嵌套参数的字段名和自由字典的键名
def test_catalog_expands_nested_parameter_models_and_toolset_keys():
    import zhihui.tools.general  # noqa: F401
    line = prompts.tool_line("manage_dynamic_block")
    assert "param_name*" in line and "param_type*" in line and "geometry_refs" in line
    t = prompts.tool_line("invoke_engineering_toolset")
    assert "standard_fastener={type,size,length" in t and "keyway={shaft_diameter}" in t


def test_few_shots_do_not_teach_the_wrong_island_style():
    for _, plan in prompts.FEW_SHOTS:
        s = json.dumps(plan, ensure_ascii=False)
        assert '"island_style": "outer"' not in s


# ---------------------------------------------------------------- Planner：类别不是硬限制
def _plan(tool, args, cats):
    return json.dumps({"thought": "", "intent_categories": cats, "normalized_utterance": "x", "workflow_steps": [
        {"step_id": "s1", "target_agent": "tool_execution_agent", "depends_on": [], "action_summary": "x", "payload": {"tool": tool, "args": args}}]}, ensure_ascii=False)


def _circle(cats):
    return _plan("draw_circular_primitives", {"curve_type": "circle", "method": "center_radius", "center": "0,0", "radius": 5}, cats)


def test_planner_widens_to_all_tools_when_the_model_names_a_tool_outside_the_subset(env, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    systems = []
    ws = _plan("manage_workspace_ui", {"workspace": "3d_modeling"}, ["environment_setup"])
    replies = iter([ws] * 5)                                      # 第一次：函数不在 2d_drafting 子集里，重来时目录换成全部函数

    async def fake(system, user, usage, role="planner", max_tokens=4000, timeout=60, strict=False, json_mode=False):
        systems.append(system)
        return next(replies)
    monkeypatch.setattr(planner_mod, "chat_text", fake)
    ctx = run(LocalBackend(env.svc).context(env.new("blank_a3")))
    plan = run(planner_mod.Planner(False).plan("x", "x", ctx, ["2d_drafting"]))
    assert plan.workflow_steps[0].payload["tool"] == "manage_workspace_ui"
    assert "- manage_workspace_ui(" not in systems[0] and "- manage_workspace_ui(" in systems[1]


def test_planner_asks_again_with_the_categories_the_model_says_it_needs(env, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    systems = []

    async def fake(system, user, usage, role="planner", max_tokens=4000, timeout=60, strict=False, json_mode=False):
        systems.append(system)
        return _circle(["2d_drafting", "2d_editing"])
    monkeypatch.setattr(planner_mod, "chat_text", fake)
    ctx = run(LocalBackend(env.svc).context(env.new("blank_a3")))
    run(planner_mod.Planner(False).plan("x", "x", ctx, ["2d_drafting"]))
    assert len(systems) == 2 and "- grip_edit_entity(" not in systems[0] and "- grip_edit_entity(" in systems[1]


def test_planner_does_not_loop_when_declared_categories_are_already_covered(env, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    n = []

    async def fake(system, user, usage, role="planner", max_tokens=4000, timeout=60, strict=False, json_mode=False):
        n.append(1)
        return _circle(["2d_drafting"])
    monkeypatch.setattr(planner_mod, "chat_text", fake)
    ctx = run(LocalBackend(env.svc).context(env.new("blank_a3")))
    run(planner_mod.Planner(False).plan("x", "x", ctx, ["2d_drafting"]))
    assert len(n) == 1


# ---------------------------------------------------------------- 基准检查器
def test_bench_checker_new_assertions(env):
    from zhihui.bench import general_cases as gc
    did = env.new("blank_a3")
    env.call("manage_dim_style", did, style_name="机械", standard_preset="GB_MECH_MM", text_height=3.5, precision=1, set_current=True)
    d = env.d(did)

    class A:
        results = {"s1": {"points": {"primary": [50.0, 30.0, 0.0]}}}
    assert gc.check({"drawing": {"current_dim_style": {"ne": "ISO-25"}}}, d, None, A, 0, [])[0]
    assert not gc.check({"drawing": {"current_dim_style": {"ne": "机械"}}}, d, None, A, 0, [])[0]
    assert gc.check({"dim_style": {"text_height": 3.5, "precision": 1}}, d, None, A, 0, [])[0]
    assert not gc.check({"dim_style": {"text_height": 5}}, d, None, A, 0, [])[0]
    assert gc.check({"result_has": [50, 30, 0]}, d, None, A, 0, [])[0] and not gc.check({"result_has": [1, 2, 3]}, d, None, A, 0, [])[0]
    spec = {"result": {"step": "s2", "path": "points.primary", "value": [50, 30, 0]}}
    assert not gc.check(spec, d, None, A, 0, [], "scripted")[0] and gc.check(spec, d, None, A, 0, [], "real")[0]


# ---------------------------------------------------------------- 第三轮真实模型基准里出现的
def test_pure_rgb_of_a_basic_color_is_stored_as_aci(env):
    did = env.new("gb_a3_mech")
    env.call("set_layer_properties", did, layer_name="中心线", color="#FF0000")
    l = env.d(did).get_layer("中心线")
    assert l.color == 1 and l.true_color is None
    env.call("set_layer_properties", did, layer_name="中心线", color=[0, 0, 255])
    assert env.d(did).get_layer("中心线").color == 5
    env.call("set_layer_properties", did, layer_name="中心线", color="#FF8000")                 # 不是标准色：仍按真彩色存
    l = env.d(did).get_layer("中心线")
    assert l.true_color == "#FF8000"


def test_corner_point_phrasing_with_fang_is_a_point_not_a_region():
    from zhihui.agent.general.recognizer_rules import parse_phrase as parse_query
    for q in ("矩形的左下方顶点", "矩形左下方的角点", "矩形的左下角"):
        spec = parse_query(q)
        assert "endpoint" in spec.features and spec.region is None, q
    assert parse_query("左下方的小圆").region == "bottom_left"


def test_arabic_number_followed_by_qian_is_not_rewritten():
    from zhihui.agent.speech_normalize import normalize
    assert normalize("设计压力600千帕").text == "设计压力600千帕"
    assert normalize("电机功率15千瓦").text == "电机功率15千瓦"
    assert normalize("设备重量1200千克").text == "设备重量1200千克"
    assert normalize("长1.5千米").text == "长1.5千米"
    assert normalize("画一个半径一千的圆").text == "画1个半径1000的圆"                     # 纯中文数字照常转换
    assert normalize("一千二百").text == "1200"
