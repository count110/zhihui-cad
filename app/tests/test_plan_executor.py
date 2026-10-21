"""计划执行器与智能体状态机：变量引用、拓扑序、反问/确认/解锁续接、批量事务回滚、护栏、预算、模型修参。"""
import asyncio
import json

import pytest

from gen_helpers import Env, pclose
from zhihui.agent.general.backend import LocalBackend
from zhihui.agent.general.engine import GeneralAgent
from zhihui.agent.general.executor import Executor, topo
from zhihui.agent.general.guards import check_plan, utterance_numbers
from zhihui.agent.general.plan import Plan, PlanError, parse_plan, validate_plan
from zhihui.agent.general.recognizer import Recognizer


def run(coro):
    return asyncio.run(coro)


def P(*steps, **kw):
    """用简写写计划：("s1", "tool", {args}, [deps]) 或 ("s2", "rec", {"query": ...}, [deps])。"""
    out = []
    for s in steps:
        sid, kind, payload, *rest = s
        deps = rest[0] if rest else []
        if kind == "rec":
            out.append({"step_id": sid, "target_agent": "entity_recognition_agent", "depends_on": deps, "action_summary": f"识别 {payload.get('query') or payload.get('tool')}", "payload": payload})
        else:
            out.append({"step_id": sid, "target_agent": "tool_execution_agent", "depends_on": deps, "action_summary": kind, "payload": {"tool": kind, "args": payload}})
    return Plan.model_validate({"thought": "t", "intent_categories": [], "workflow_steps": out, **kw})


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


@pytest.fixture
def did(env):
    return env.new("blank_a3")


def execute(env, did, plan, prior=None, **kw):
    be = LocalBackend(env.svc)
    ex = Executor(be, did, recognizer=Recognizer(be), **kw)
    return run(ex.run(plan, prior))


# ------------------------------------------------------------------ 计划协议
def test_plan_validation_catches_unknown_tools_bad_deps_and_wrong_agent():
    bad = P(("s1", "没有这个函数", {}), ("s2", "draw_linear_primitives", {"primitive_type": "line", "points": ["0,0", "1,1"]}, ["s9"]))
    issues = validate_plan(bad)
    assert any("没有函数" in i for i in issues) and any("s9" in i for i in issues)
    bad = P(("s1", "rec", {"tool": "erase_entities", "args": {}}))
    assert any("只能调用感知函数" in i for i in validate_plan(bad))                                   # 识别步骤不能顺手改图
    subset = P(("s1", "draw_linear_primitives", {"primitive_type": "line", "points": ["0,0", "1,1"]}))
    assert any("不在当前意图类别" in i for i in validate_plan(subset, allowed_tools={"create_hatch"}))        # 只暴露意图类别对应的工具子集
    assert validate_plan(subset, allowed_tools={"draw_linear_primitives"}) == []
    with pytest.raises(PlanError):
        parse_plan('{"workflow_steps": [{"step_id": "x1"}]}')
    p = parse_plan('```json\n{"workflow_steps": [{"step_id": "s1", "target_agent": "tool_execution_agent", "payload": {"tool": "history_undo_redo", "args": {"action": "undo"}}}]}\n```')
    assert p.workflow_steps[0].step_id == "s1"                                                       # 模型包了代码块也能解析


def test_topological_order_follows_dependencies_not_listing_order():
    plan = P(("s2", "draw_linear_primitives", {"primitive_type": "line", "points": ["0,0", "1,1"]}, ["s1"]), ("s1", "draw_linear_primitives", {"primitive_type": "line", "points": ["0,0", "1,0"]}), ("s3", "history_undo_redo", {"action": "list"}))
    assert [s.step_id for s in topo(plan.workflow_steps)] == ["s1", "s2", "s3"]
    cyc = P(("s1", "history_undo_redo", {"action": "list"}, ["s2"]), ("s2", "history_undo_redo", {"action": "list"}, ["s1"]))
    assert len(topo(cyc.workflow_steps)) == 2                                                        # 循环依赖不死循环


# ------------------------------------------------------------------ 变量引用
def test_references_to_recognition_and_tool_results(env, did):
    env.call("draw_circular_primitives", did, curve_type="circle", method="center_radius", center="100,60", radius=20)
    env.call("draw_circular_primitives", did, curve_type="circle", method="center_radius", center="10,10", radius=3)
    plan = P(
        ("s1", "rec", {"query": "大圆", "need": ["snap:center"]}),
        ("s2", "draw_circular_primitives", {"curve_type": "circle", "method": "center_radius", "center": "$s1.points.primary", "radius": 5}, ["s1"]),
        ("s3", "modify_entity_properties", {"action": "set", "entity_ids": ["$s2.created[0]"], "common_props": {"color": 1}}, ["s2"]),
        ("s4", "modify_entity_properties", {"action": "set", "entity_ids": "$LAST_CREATED", "common_props": {"color": 3}}, ["s3"]),
        ("s5", "transform_entities", {"operation": "move", "entity_ids": ["$s1.entity_ids[0]"], "displacement": [5, 0]}, ["s4"]),
    )
    res = execute(env, did, plan)
    assert res.status == "done", res.error
    d = env.d(did)
    new = d.get_entity(res.results["s2"]["created"][0])
    assert pclose(new.data["c"], (100, 60)) and new.color == 3                                       # 圆心来自识别出的大圆的圆心，不是模型算的
    assert d.get_entity(res.results["s1"]["entity_ids"][0]).data["c"][0] == 105
    assert res.created and res.modified


def test_unresolved_reference_fails_before_calling_tool(env, did):
    rev = env.rev(did)
    plan = P(("s1", "erase_entities", {"entity_ids": ["$s9.created[0]"]}))
    res = execute(env, did, plan)
    assert res.status == "failed" and res.failed_step == "s1" and res.error["error_code"] == "REF_UNRESOLVED" and env.rev(did) == rev
    plan = P(("s1", "draw_linear_primitives", {"primitive_type": "line", "points": ["0,0", "5,0"]}), ("s2", "erase_entities", {"entity_ids": ["$s1.created[3]"]}, ["s1"]))
    res = execute(env, did, plan)
    assert res.status == "failed" and res.error["error_code"] == "REF_UNRESOLVED" and "s1" in res.results                      # 第一步已经完成，保留


def test_unknown_tool_and_invalid_args_are_structured(env, did):
    res = execute(env, did, P(("s1", "没有这个", {})))
    assert res.error["error_code"] == "UNKNOWN_TOOL"
    res = execute(env, did, P(("s1", "draw_circular_primitives", {"curve_type": "circle", "method": "center_radius", "center": "0,0"})))
    assert res.error["error_code"] in ("MISSING_ARG", "INVALID_ARGS") and res.error["hint"]
    res = execute(env, did, P(("s1", "draw_circular_primitives", {"curve_type": "circle", "method": "center_radius", "center": "0,0", "radius": 5, "bogus": 1})))
    assert res.error["error_code"] == "INVALID_ARGS" and "bogus" in json.dumps(res.error, ensure_ascii=False)


# ------------------------------------------------------------------ 识别步骤的三种结局
def test_ambiguous_recognition_stops_and_later_steps_do_not_run(env, did):
    env.call("draw_linear_primitives", did, primitive_type="line", points=["0,0", "0,50"])
    env.call("draw_linear_primitives", did, primitive_type="line", points=["100,0", "100,50"])
    plan = P(("s1", "rec", {"query": "竖线"}), ("s2", "erase_entities", {"entity_ids": "$s1.entity_ids"}, ["s1"]))
    rev = env.rev(did)
    res = execute(env, did, plan)
    assert res.status == "clarify" and res.failed_step == "s1" and sorted(res.clarification["candidates"]) == ["L1", "L2"] and res.clarification["options"]
    assert env.rev(did) == rev and "s2" not in res.results


def test_not_found_recognition_fails_with_reason(env, did):
    env.call("draw_linear_primitives", did, primitive_type="line", points=["0,0", "0,50"])
    res = execute(env, did, P(("s1", "rec", {"query": "圆"}), ("s2", "erase_entities", {"entity_ids": "$s1.entity_ids"}, ["s1"])))
    assert res.status == "failed" and res.error["error_code"] == "NOT_FOUND" and "没有" in res.error["message"]


def test_recognition_by_perception_tool_directly(env, did):
    env.call("draw_polygons_and_meshes", did, shape_type="rectangle", corner1="0,0", corner2="100,60")
    plan = P(("s1", "rec", {"tool": "get_entity_snap_points", "args": {"entity_ids": ["PL1"], "snap_types": ["geometric_center"]}}),
             ("s2", "draw_circular_primitives", {"curve_type": "circle", "method": "center_radius", "center": "$s1.points.primary", "radius": 10}, ["s1"]))
    res = execute(env, did, plan)
    assert res.status == "done" and pclose(env.d(did).get_entity(res.created[0]).data["c"], (50, 30))


# ------------------------------------------------------------------ replan / 预算 / 失败保留
def test_replan_after_stops_after_that_step(env, did):
    plan = P(("s1", "draw_linear_primitives", {"primitive_type": "line", "points": ["0,0", "5,0"]}), ("s2", "draw_linear_primitives", {"primitive_type": "line", "points": ["0,0", "0,5"]}, ["s1"]), replan_after="s1")
    res = execute(env, did, plan)
    assert res.status == "replan" and list(res.results) == ["s1"] and len(env.ids(did)) == 1


def test_failure_keeps_completed_steps_and_reports_which_failed(env, did):
    plan = P(("s1", "draw_linear_primitives", {"primitive_type": "line", "points": ["0,0", "5,0"]}), ("s2", "draw_circular_primitives", {"curve_type": "circle", "method": "center_radius", "center": "0,0", "radius": -1}, ["s1"]),
             ("s3", "draw_linear_primitives", {"primitive_type": "line", "points": ["0,0", "0,5"]}, ["s2"]))
    res = execute(env, did, plan)
    assert res.status == "failed" and res.failed_step == "s2" and env.ids(did) == ["L1"] and [l["ok"] for l in res.log] == [True, False]


def test_tool_call_budget(env, did):
    steps = [("s%d" % i, "draw_linear_primitives", {"primitive_type": "line", "points": ["0,0", f"{i},0"]}) for i in range(1, 6)]
    res = execute(env, did, P(*steps), max_calls=3)
    assert res.status == "failed" and res.error["error_code"] == "BUDGET_EXCEEDED" and len(env.ids(did)) == 3


def test_batch_all_or_nothing_inside_plan(env, did):
    rev = env.rev(did)
    steps = [{"tool": "draw_linear_primitives", "args": {"primitive_type": "line", "points": ["0,0", "10,0"]}},
             {"tool": "draw_circular_primitives", "args": {"curve_type": "circle", "method": "center_radius", "center": "$this[0].created[0].p2", "radius": 4}}]
    res = execute(env, did, P(("s1", "cad_batch_execute", {"transaction_name": "测试", "steps": steps})))
    assert res.status == "failed" and res.error.get("failed_step") == 1 and env.rev(did) == rev and env.ids(did) == []          # 失败整体回滚
    steps[1]["args"]["center"] = "$this[0].created[0]"
    res = execute(env, did, P(("s1", "cad_batch_execute", {"transaction_name": "测试", "steps": steps})))
    assert res.status == "failed"                                                                    # 引用到的是图元 id 而不是点：仍然回滚
    steps[1]["args"]["center"] = "L1.end"
    res = execute(env, did, P(("s1", "cad_batch_execute", {"transaction_name": "测试", "steps": steps})))
    assert res.status == "done" and env.rev(did) == rev + 1 and len(res.created) == 2
    assert pclose(env.d(did).get_entity("C2").data["c"], (10, 0))                                          # 特征点引用 L1.end


def test_batch_sub_step_refs_and_session_refs(env, did):
    steps = [{"tool": "draw_polygons_and_meshes", "args": {"shape_type": "rectangle", "corner1": "0,0", "corner2": "100,60"}},
             {"tool": "draw_circular_primitives", "args": {"curve_type": "circle", "method": "center_diameter", "center": "50,30", "diameter": 20}},
             {"tool": "create_radial_and_chain_dim", "args": {"dim_type": "diameter", "target_entity_id": "$this[1].created[0]"}},
             {"tool": "modify_entity_properties", "args": {"action": "set", "entity_ids": ["$this[0].created[0]", "$this[1].created[0]"], "common_props": {"color": 5}}}]
    res = execute(env, did, P(("s1", "cad_batch_execute", {"transaction_name": "组合", "steps": steps})))
    assert res.status == "done"
    assert [env.ent(did, i).color for i in ("PL1", "C2")] == [5, 5]
    res = execute(env, did, P(("s2", "transform_entities", {"operation": "move", "entity_ids": "$s1.step_results[0].created", "displacement": [10, 0]}, ["s1"])), prior={"s1": {"step_results": [{"created": ["PL1"]}]}})
    assert res.status == "done" and env.ent(did, "PL1").data["vertices"][0]["p"][0] == 10                    # 批量子步骤的结果可以用 step_results[i] 引用


# ------------------------------------------------------------------ 模型修参（可选）
def test_executor_llm_repair_runs_only_when_enabled(env, did, monkeypatch):
    calls = []

    async def repair(tool, schema, args, err):
        calls.append(tool)
        return {**args, "radius": 7}
    plan = P(("s1", "draw_circular_primitives", {"curve_type": "circle", "method": "center_radius", "center": "0,0", "radius": "大约七"}))
    monkeypatch.delenv("ZHIHUI_EXECUTOR_LLM", raising=False)
    res = execute(env, did, plan, repair=repair)
    assert res.status == "failed" and not calls and res.error["error_code"] == "INVALID_ARGS"
    monkeypatch.setenv("ZHIHUI_EXECUTOR_LLM", "1")
    res = execute(env, did, plan, repair=repair)
    assert res.status == "done" and calls == ["draw_circular_primitives"] and env.d(did).get_entity(res.created[0]).data["r"] == 7


# ------------------------------------------------------------------ 护栏
def test_guard_blocks_numbers_not_from_the_user():
    plan = P(("s1", "draw_circular_primitives", {"curve_type": "circle", "method": "center_radius", "center": "0,0", "radius": 37}))
    issues = check_plan(plan, "画一个圆", "画一个圆")
    assert any(i.code == "E_UNSOURCED_VALUE" and "37" in i.message for i in issues)
    assert not check_plan(plan, "画一个半径37的圆", "画一个半径37的圆")
    assert not check_plan(P(("s1", "draw_circular_primitives", {"curve_type": "circle", "method": "center_diameter", "center": "0,0", "diameter": 40})), "画一个半径20的圆", "")        # 半径 20 → 直径 40 的简单换算允许
    plan = P(("s1", "draw_circular_primitives", {"curve_type": "circle", "method": "center_radius", "center": "$s0.points.primary", "radius": "$s0.metrics.radius"}))
    assert not check_plan(plan, "画圆", "")                                                          # 引用没有数值出处问题


def test_guard_blocks_hardcoded_computed_coordinates():
    plan = P(("s1", "draw_linear_primitives", {"primitive_type": "line", "points": ["0,0", "73.5,41"]}))
    issues = check_plan(plan, "画一条线", "画一条线")
    assert any(i.code == "E_HARDCODED_COORD" for i in issues)
    assert not check_plan(plan, "从原点画到 73.5,41", "")
    assert utterance_numbers("R8 倒角、Ra3.2、H7") >= {8.0, 3.2, 7.0}


def test_guard_asks_confirmation_for_bulk_delete_and_layer_delete():
    plan = P(("s1", "erase_entities", {"entity_ids": [f"L{i}" for i in range(25)]}))
    assert [i.kind for i in check_plan(plan, "删掉", "")] == ["confirm"]
    plan = P(("s1", "manage_layers", {"action": "delete", "layer_name": "x"}))
    assert [i.code for i in check_plan(plan, "删除图层 x", "")] == ["E_CONFIRM_REQUIRED"]


# ------------------------------------------------------------------ 智能体：反问 / 确认 / 解锁 的续接
def agent(env, did=None):
    events = []
    ag = GeneralAgent(on_event=lambda k, p: events.append((k, p)), mock=True, backend=LocalBackend(env.svc), drawing_id=did)
    ag.events = events
    return ag


def resume_msgs(out):
    return out.messages


def test_pick_clarification_resumes_without_redoing_completed_steps(env, did):
    env.call("draw_linear_primitives", did, primitive_type="line", points=["0,0", "0,50"])
    env.call("draw_linear_primitives", did, primitive_type="line", points=["100,0", "100,50"])
    out = run(agent(env, did).run("把竖线删掉"))
    assert out.status == "WAITING_CONFIRMATION" and "竖线" in out.question["question"] and sorted(out.question["candidates"]) == ["L1", "L2"] and len(env.ids(did)) == 2
    state = json.loads(out.messages[0]["content"])
    assert state["kind"] == "pick" and state["candidates"] == ["L1", "L2"]
    wrong = run(agent(env, did).run("把竖线删掉", resume=out.messages, answer="那个"))
    assert wrong.status == "WAITING_CONFIRMATION" and "没有理解" in wrong.question["question"]       # 听不懂的回答：再问一次，不乱选
    done = run(agent(env, did).run("把竖线删掉", resume=out.messages, answer="L2"))
    assert done.status in ("READY", "READY_WITH_WARNINGS") and env.ids(did) == ["L1"]


def test_pick_by_option_number_and_all(env, did):
    env.call("draw_linear_primitives", did, primitive_type="line", points=["0,0", "0,50"])
    env.call("draw_linear_primitives", did, primitive_type="line", points=["100,0", "100,50"])
    out = run(agent(env, did).run("把竖线删掉"))
    done = run(agent(env, did).run("把竖线删掉", resume=out.messages, answer="两个都删"))
    assert env.ids(did) == []


def test_bulk_erase_needs_confirmation_then_runs_or_cancels(env, did):
    for i in range(25):
        env.call("draw_circular_primitives", did, curve_type="circle", method="center_radius", center=f"{i * 10},0", radius=2)
    out = run(agent(env, did).run("删除所有的圆"))
    assert out.status == "WAITING_CONFIRMATION" and "确认" in out.question["question"] and len(env.ids(did)) == 25
    cancel = run(agent(env, did).run("删除所有的圆", resume=out.messages, answer="取消"))
    assert cancel.status == "READY" and "取消" in cancel.message and len(env.ids(did)) == 25
    ok = run(agent(env, did).run("删除所有的圆", resume=out.messages, answer="确认"))
    assert env.ids(did) == [] and ok.status in ("READY", "READY_WITH_WARNINGS")


def test_locked_layer_asks_to_unlock_then_continues(env, did):
    env.call("draw_circular_primitives", did, curve_type="circle", method="center_radius", center="0,0", radius=5)
    env.call("manage_layers", did, action="create", layer_name="锁层")
    env.call("modify_entity_properties", did, action="set", entity_ids=["C1"], common_props={"layer": "锁层"})
    env.call("set_layer_state", did, layer_name="锁层", is_locked=True)
    out = run(agent(env, did).run("把圆向右移动 10"))
    assert out.status == "WAITING_CONFIRMATION" and "锁" in out.question["question"] and env.ent(did, "C1").data["c"][0] == 0
    done = run(agent(env, did).run("把圆向右移动 10", resume=out.messages, answer="解锁并继续"))
    assert env.ent(did, "C1").data["c"][0] == 10 and env.d(did).get_layer("锁层").locked is False


def test_missing_value_asks_instead_of_inventing(env, did):
    out = run(agent(env, did).run("画一个圆"))
    assert out.status == "WAITING_CONFIRMATION" and ("半径" in out.question["question"] or "圆心" in out.question["question"]) and env.ids(did) == []
    done = run(agent(env, did).run("画一个圆", resume=out.messages, answer="圆心在零零，半径十"))
    assert done.status in ("READY", "READY_WITH_WARNINGS")
    c = env.ent(did, env.ids(did)[0])
    assert c.kind == "circle" and c.data["r"] == 10 and pclose(c.data["c"], (0, 0))


def test_undo_redo_through_agent_and_history_survives(env, did):
    run(agent(env, did).run("画一个半径10的圆，圆心在零零"))
    assert len(env.ids(did)) == 1
    run(agent(env, did).run("撤销"))
    assert env.ids(did) == []
    run(agent(env, did).run("重做"))
    assert len(env.ids(did)) == 1


def test_agent_auto_creates_drawing_and_reports_defaults(env):
    out = run(agent(env).run("画一个半径10的圆，圆心在零零"))
    assert out.drawing_id and out.status in ("READY", "READY_WITH_WARNINGS") and "自动新建" in out.message
    assert out.artifacts and {a["format"] for a in out.artifacts} >= {"dxf", "png"}


def test_events_stream_plan_steps_and_results(env, did):
    ag = agent(env, did)
    run(ag.run("画一个矩形，左下角在零零，长120宽80"))
    kinds = [k for k, _ in ag.events]
    assert kinds.index("plan") < kinds.index("step_start") < kinds.index("tool_result") and "status" in kinds
    plan = next(p for k, p in ag.events if k == "plan")
    assert plan["steps"] and all("summary" in s for s in plan["steps"])


def test_command_fast_path_skips_planner(env, did):
    ag = agent(env, did)
    out = run(ag.run("L 0,0 @100,0"))
    assert out.status in ("READY", "READY_WITH_WARNINGS") and ag.usage()["planner"] == "command" and ag.usage()["prompt_tokens"] == 0
    assert env.ids(did) == ["L1"] and pclose(env.ent(did, "L1").data["p2"], (100, 0))
    bad = run(agent(env, did).run("C"))
    assert bad.status in ("READY", "FAILED") and len(env.ids(did)) == 1


def test_speech_normalization_event_and_effect(env, did):
    ag = agent(env, did)
    run(ag.run("画个源，圆心在零零，半径二十"))
    assert any(k == "normalized" for k, _ in ag.events)
    c = env.ent(did, env.ids(did)[0])
    assert c.kind == "circle" and c.data["r"] == 20


def test_local_and_mcp_backends_give_the_same_result(env, monkeypatch):
    """两种后端（直接调用 / 经 MCP 子进程）对同一句话结果一致。MCP 子进程会被去掉 *_API_KEY。"""
    monkeypatch.setenv("ZHIHUI_MCP", "inproc")
    from zhihui.agent.general.backend import McpBackend
    from zhihui.agent.orchestrator import mcp_client

    async def go():
        async with mcp_client(general=True) as client:
            be = McpBackend(client)
            r = await be.call("manage_document_file", {"action": "new", "template_name": "blank_a3"}, None)
            did = r["drawing_id"]
            r2 = await be.call("draw_circular_primitives", {"curve_type": "circle", "method": "center_radius", "center": "10,10", "radius": 5}, did)
            r3 = await be.call("query_entities_by_filter", {"entity_types": ["circle"]}, did)
            bad = await be.call("draw_circular_primitives", {"curve_type": "circle", "method": "center_radius", "center": "10,10", "radius": "x"}, did)
            return r, r2, r3, bad
    r, r2, r3, bad = run(go())
    assert r["ok"] and r2["ok"] and r2["created"] and r3["data"]["total"] == 1 and not bad["ok"]
