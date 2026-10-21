"""设备图在引擎里的行为（任务书 6.1、10.1“引擎”）：反问不落版本也不消耗重规划、护栏接入、turn_id 注入、续接、上下文、导出文件。

写法参照 test_plan_executor.py：GeneralAgent + LocalBackend，run_plan 直接执行写好的计划（跳过 Planner）；需要看 Planner 入参的用桩。
带“出图/导出”的用例要 7 秒左右（_finish 会导出 DXF/PNG/SVG），所以只留必要的几条走到 done；反问类用例都在执行前后就停下，很快。
"""
import asyncio
import json

import pytest

import zhihui.tools.general  # noqa: F401  注册函数
from gen_helpers import Env
from zhihui.agent.general import planner as planner_mod
from zhihui.agent.general.backend import Backend, LocalBackend
from zhihui.agent.general.engine import GeneralAgent, VESSEL_CLARIFY
from zhihui.agent.general.executor import Executor
from zhihui.agent.general.guards import invented_values
from zhihui.agent.general.plan import Plan
from zhihui.agent.orchestrator import Outcome
from zhihui.agent.speech_normalize import normalize
from zhihui.tools.general.view_tools import build_context

DEMO1 = "画一台立式储罐的设备图，内径1600，筒体高2400，标准椭圆封头，壁厚8，材料Q345R，设计压力0.6MPa，设计温度80℃，人孔DN500，进料口DN80，出料口DN80，两个液位计口DN25，四个耳式支座"
SPEC1 = {"kind": "vertical_vessel", "shell": {"inner_diameter_mm": 1600, "length_mm": 2400, "thickness_mm": 8, "material": "Q345R"}, "heads": [{"kind": "EHA"}],
         "design": {"design_pressure_mpa": 0.6, "design_temp_c": 80},
         "nozzles": [{"dn": 500, "kind": "manhole", "purpose_text": "人孔"}, {"dn": 80, "purpose_text": "进料口"}, {"dn": 80, "purpose_text": "出料口"},
                     {"dn": 25, "kind": "level_gauge", "purpose_text": "液位计口", "count": 2}],
         "supports": [{"kind": "lug", "count": 4}]}
# 两个进料口（选择器 purpose=进料口 会有歧义）；其余最小
UT_TWO = "画一台立式储罐的设备图，内径1600，筒体高2400，壁厚8，进料口DN80，进料口DN50，出料口DN80，四个耳式支座"
SPEC_TWO = {"kind": "vertical_vessel", "shell": {"inner_diameter_mm": 1600, "length_mm": 2400, "thickness_mm": 8},
            "nozzles": [{"dn": 80, "purpose_text": "进料口"}, {"dn": 50, "purpose_text": "进料口"}, {"dn": 80, "purpose_text": "出料口"}], "supports": [{"kind": "lug", "count": 4}]}
SPEC_HORIZ = {"kind": "horizontal_vessel", "shell": {"inner_diameter_mm": 2000, "tangent_length_mm": 6000, "thickness_mm": 10},
              "nozzles": [{"dn": 100, "purpose_text": "进料"}, {"dn": 100, "purpose_text": "出料"}, {"dn": 500, "kind": "manhole", "purpose_text": "人孔"}], "supports": [{"kind": "saddle", "count": 2}]}
MIN_SPEC = {"kind": "vertical_vessel", "shell": {"inner_diameter_mm": 1600, "length_mm": 2400, "thickness_mm": 8},
            "nozzles": [{"dn": 80, "purpose_text": "进料口"}, {"dn": 80, "purpose_text": "出料口"}], "supports": [{"kind": "lug", "count": 4}]}
REQUIRED_ITEMS = ["shell.inner_diameter_mm", "shell.length", "shell.thickness_mm", "supports.kind", "nozzles"]


def run(coro):
    return asyncio.run(coro)


def VP(args: dict, tool: str = "vessel_define") -> Plan:
    return Plan.model_validate({"thought": "t", "intent_categories": ["chem_equipment"], "workflow_steps": [
        {"step_id": "s1", "target_agent": "tool_execution_agent", "depends_on": [], "action_summary": tool, "payload": {"tool": tool, "args": args}}]})


def ASK(items: list[dict], question: str = "") -> Plan:
    return Plan.model_validate({"thought": "t", "intent_categories": ["chem_equipment"], "workflow_steps": [], "clarification_needed": True,
                                "clarification": {"question": question, "items": items}})


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def new_eq(env) -> str:
    return env.new("hg_equipment_a1")


def with_vessel(env, spec=None, turn="t0") -> str:
    """新建设备图并直接调函数建好参数（不走 Agent，不导出，很快）。"""
    did = new_eq(env)
    env.call("vessel_define", did, op="create", spec=spec or SPEC1, turn_id=turn)
    return did


def agent(env, did=None, mock=True, planner="forbid", **kw) -> GeneralAgent:
    """planner="forbid"：Planner 被调用就报错（run_plan 本来就不该调用）；"keep" 保留原来的。"""
    ev = []
    ag = GeneralAgent(on_event=lambda k, p: ev.append((k, p)), mock=mock, backend=LocalBackend(env.svc), drawing_id=did, **kw)
    ag.events = ev

    async def forbidden(*a, **k):
        raise AssertionError("不该调用 Planner")
    if planner == "forbid":
        ag.planner.plan = forbidden
    return ag


def items_of(out: Outcome) -> list[str]:
    return [it["path"] for it in out.question["items"]]


def state_of(out: Outcome) -> dict:
    return json.loads(out.messages[0]["content"])


# ------------------------------------------------------------------ 缺必问项：整批反问，图纸不变，不消耗重规划（mock 与 real 一样）
@pytest.mark.parametrize("mock", [True, False], ids=["mock", "real"])
def test_create_with_only_kind_asks_every_required_item_and_keeps_the_version(env, mock):
    did = new_eq(env)
    rev = env.rev(did)
    ag = agent(env, did, mock=mock, task_id="T1")
    out = run(ag.run_plan(VP({"op": "create", "spec": {"kind": "vertical_vessel"}}), "画一个立式储罐"))
    assert out.status == "WAITING_CONFIRMATION"
    assert items_of(out) == REQUIRED_ITEMS                                                     # 一次问完：内径、长度口径、壁厚、支座型式、管口
    assert all(it["label"] for it in out.question["items"]) and out.question["error_code"] == "SPEC_INCOMPLETE"
    assert "还缺这些参数" in out.message and "内径" in out.message and out.message == out.question["question"]
    assert env.rev(did) == rev and env.d(did).vessel is None and ag.replans == 0
    kinds = [k for k, _ in ag.events]
    assert "clarify" in kinds and "step_failed" in kinds and "tool_result" not in kinds
    st = state_of(out)
    assert st["kind"] == "ask" and st["utterance"] == "画一个立式储罐" and st["drawing_id"] == did and items_of_state(st) == REQUIRED_ITEMS
    assert ag.last_question["items"] == out.question["items"]


def items_of_state(st: dict) -> list[str]:
    return [it["path"] for it in st["question"]["items"]]


@pytest.mark.parametrize("mock", [True, False], ids=["mock", "real"])
def test_ambiguous_selector_asks_which_nozzle_with_both_marks_as_candidates(env, mock):
    did = with_vessel(env, SPEC_TWO)
    rev, before = env.rev(did), env.d(did).vessel.model_dump()
    ag = agent(env, did, mock=mock, task_id="T2")
    out = run(ag.run_plan(VP({"op": "patch", "set": {"nozzles[purpose=进料口].orientation_deg": 90}}), "把进料口转到90度"))
    assert out.status == "WAITING_CONFIRMATION" and out.question["error_code"] == "AMBIGUOUS"
    assert out.question["candidates"] == ["a", "b"]                                            # 两个进料口：符号 a、b
    assert "2 个管口" in out.message and "a（进料口，DN80" in out.message and "b（进料口，DN50" in out.message
    assert out.question["options"][-1] == "全部" and len(out.question["options"]) == 3
    assert env.rev(did) == rev and env.d(did).vessel.model_dump() == before and ag.replans == 0   # 参数、版本都没动


@pytest.mark.parametrize("mock", [True, False], ids=["mock", "real"])
def test_pipe_size_missing_from_the_standard_table_asks_instead_of_interpolating(env, mock):
    did = with_vessel(env)
    rev = env.rev(did)
    ag = agent(env, did, mock=mock)
    out = run(ag.run_plan(VP({"op": "patch", "set": {"nozzles[purpose=出料口].dn": 85}}), "出料口改成DN85"))
    assert out.status == "WAITING_CONFIRMATION" and out.question["error_code"] == "STD_SIZE_NOT_FOUND"
    assert "dn=85" in out.message and "不插值" in out.question["items"][0]["label"]               # 说明缺的是哪张表哪一行，并交代覆盖范围
    assert env.rev(did) == rev and ag.replans == 0
    assert next(n.dn for n in env.d(did).vessel.nozzles if n.purpose == "出料口") == 80


@pytest.mark.parametrize("mock", [True, False], ids=["mock", "real"])
def test_nozzle_that_does_not_fit_on_the_head_asks_instead_of_overlapping(env, mock):
    did = with_vessel(env)
    rev = env.rev(did)
    ag = agent(env, did, mock=mock)
    out = run(ag.run_plan(VP({"op": "patch", "upsert_nozzles": [{"dn": 500, "kind": "manhole", "purpose_text": "人孔", "location": "top_head"}]}), "上封头再加一个DN500的人孔"))
    assert out.status == "WAITING_CONFIRMATION" and out.question["error_code"] == "NOZZLE_NO_ROOM"
    assert "放不下" in out.message and "0.4Di" in out.message
    assert env.rev(did) == rev and ag.replans == 0 and len(env.d(did).vessel.nozzles) == 5


@pytest.mark.parametrize("mock", [True, False], ids=["mock", "real"])
@pytest.mark.parametrize("utt,args,code", [
    ("支座改成鞍座", {"op": "patch", "set": {"supports[0].kind": "saddle"}}, "SPEC_CONFLICT"),
    ("排污口转到90度", {"op": "patch", "set": {"nozzles[purpose=排污口].orientation_deg": 90}}, "NOT_FOUND"),
    ("进料口改成DN-5", {"op": "patch", "set": {"nozzles[purpose=进料口].dn": -5}}, "INVALID_VALUE"),
    ("封头改成锥形封头", {"op": "patch", "set": {"heads.kind": "CONE"}}, "HEAD_UNSUPPORTED"),
    ("支座改成裙座", {"op": "patch", "set": {"supports[0].kind": "skirt"}}, "NOT_SUPPORTED"),
    ("设计压力改成20MPa，设计温度改成80℃", {"op": "patch", "set": {"design.design_pressure_mpa": 20, "design.design_temp_c": 80}}, "STD_SIZE_NOT_FOUND"),      # 额定值表里选不出 PN
])
def test_every_clarifiable_vessel_error_becomes_a_question_without_touching_the_drawing(env, mock, utt, args, code):
    assert code in VESSEL_CLARIFY
    did = with_vessel(env, MIN_SPEC)
    rev, before = env.rev(did), env.d(did).vessel.model_dump()
    ag = agent(env, did, mock=mock)
    out = run(ag.run_plan(VP(args), utt))
    assert out.status == "WAITING_CONFIRMATION" and out.question["error_code"] == code, (out.status, out.message)
    assert out.question["items"] and out.question["items"][0]["label"] and ag.replans == 0
    assert env.rev(did) == rev and env.d(did).vessel.model_dump() == before


def test_vessel_clarify_set_matches_the_error_codes_the_tools_raise():
    assert VESSEL_CLARIFY == {"SPEC_INCOMPLETE", "STD_SIZE_NOT_FOUND", "NOZZLE_NO_ROOM", "SPEC_CONFLICT", "NOT_FOUND", "AMBIGUOUS", "INVALID_VALUE", "NOT_SUPPORTED", "HEAD_UNSUPPORTED"}


def test_errors_the_user_cannot_answer_are_not_turned_into_questions(env):
    """路径写错这类是模型的错：mock 下直接失败（图不变）；真实模型下才消耗一次重规划，带着错误让模型改。"""
    did = with_vessel(env, MIN_SPEC)
    rev = env.rev(did)
    bad = {"op": "patch", "set": {"nozzles[purpose=进料口].bogus": 80}}
    out = run(agent(env, did).run_plan(VP(bad), "进料口改成DN80的"))
    assert out.status == "FAILED" and "不在可用路径里" in out.message and env.rev(did) == rev
    feedback = []
    ag = agent(env, did, mock=False, planner="keep")

    async def replan(norm, original, ctx, cats, prior=None, feedback_text=None):
        feedback.append(feedback_text)
        return ASK([{"path": "nozzles", "label": "是哪个管口？"}])
    ag.planner.plan = replan
    out = run(ag.run_plan(VP(bad), "进料口改成DN80的"))
    assert ag.replans == 1 and out.status == "WAITING_CONFIRMATION" and "BAD_PATH" in feedback[0] and "步骤 s1 失败" in feedback[0]
    assert env.rev(did) == rev


def test_vessel_define_inside_a_batch_still_becomes_a_question(env):
    did = new_eq(env)
    rev = env.rev(did)
    plan = Plan.model_validate({"thought": "t", "intent_categories": ["chem_equipment"], "workflow_steps": [{"step_id": "s1", "target_agent": "tool_execution_agent", "payload": {
        "tool": "cad_batch_execute", "args": {"transaction_name": "x", "steps": [{"tool": "vessel_define", "args": {"op": "create", "spec": {"kind": "vertical_vessel"}}}]}}}]})
    out = run(agent(env, did, mock=False).run_plan(plan, "画一个立式储罐"))
    assert out.status == "WAITING_CONFIRMATION" and out.question["error_code"] == "SPEC_INCOMPLETE" and items_of(out) == REQUIRED_ITEMS
    assert env.rev(did) == rev and env.d(did).vessel is None


# ------------------------------------------------------------------ 护栏接入引擎
def test_guard_block_in_mock_mode_fails_without_touching_the_drawing(env):
    did = new_eq(env)
    rev = env.rev(did)
    s = {**SPEC1, "shell": {**SPEC1["shell"], "inner_diameter_mm": 1500}}                       # 原话说的是 1600
    ag = agent(env, did)
    out = run(ag.run_plan(VP({"op": "create", "spec": s}), DEMO1))
    assert out.status == "FAILED" and "没有通过护栏检查" in out.message and "1500" in out.message
    guard = [p for k, p in ag.events if k == "guard"]
    assert guard and guard[0]["issues"][0]["code"] == "E_INVENTED_VALUE"
    assert env.rev(did) == rev and env.d(did).vessel is None and ag.tool_calls == 0


def test_guard_block_in_real_mode_replans_once_with_the_message_then_gives_up_or_continues(env):
    did = new_eq(env)
    rev = env.rev(did)
    s = {**SPEC1, "shell": {**SPEC1["shell"], "inner_diameter_mm": 1500}}
    seen = []

    async def replan(norm, original, ctx, cats, prior=None, feedback=None):
        seen.append(feedback)
        return ASK([{"path": "shell.inner_diameter_mm", "label": "内径是多少？"}])
    ag = agent(env, did, mock=False, planner="keep")
    ag.planner.plan = replan
    out = run(ag.run_plan(VP({"op": "create", "spec": s}), DEMO1))
    assert ag.replans == 1 and out.status == "WAITING_CONFIRMATION" and "护栏拦截" in seen[0] and "1500" in seen[0]
    assert env.rev(did) == rev
    # 重规划之后还是编造：不再重试，直接失败
    ag = agent(env, did, mock=False, planner="keep")

    async def again(norm, original, ctx, cats, prior=None, feedback=None):
        return VP({"op": "create", "spec": s})
    ag.planner.plan = again
    out = run(ag.run_plan(VP({"op": "create", "spec": s}), DEMO1))
    assert out.status == "FAILED" and "没有通过护栏检查" in out.message and ag.replans == 1 and env.rev(did) == rev


def test_guard_strips_an_invented_defaultable_field_warns_and_does_not_replan(env):
    """编出来的方位 180 被剔除（E_DEFAULTABLE_FIELD 只警告，不重规划）；剩下的计划照常往下走（这里缺必问项，停在反问）。"""
    did = new_eq(env)
    spec = {"kind": "vertical_vessel", "nozzles": [{"dn": 80, "purpose_text": "进料口", "orientation_deg": 180}]}
    ag = agent(env, did, mock=False)
    out = run(ag.run_plan(VP({"op": "create", "spec": spec}), "画一个立式储罐，进料口DN80"))
    assert out.status == "WAITING_CONFIRMATION" and ag.replans == 0
    assert any("orientation_deg" in w and "已剔除" in w for w in ag.warnings)
    assert [i["code"] for k, p in ag.events if k == "guard" for i in p["issues"]] == ["E_DEFAULTABLE_FIELD"]
    sent = next(p for k, p in ag.events if k == "plan")["steps"][0]["args"]
    assert "orientation_deg" not in sent["spec"]["nozzles"][0] and sent["spec"]["nozzles"][0]["dn"] == 80          # 送去执行的参数里已经没有


def test_bare_height_is_asked_by_code_not_guessed(env):
    """用户只说“高3000”：护栏把 shell.length_mm 摘掉并转成确定性反问，一个函数都不调用。"""
    did = new_eq(env)
    rev = env.rev(did)
    utt = "画一台立式储罐的设备图，内径1600，高3000，壁厚8，进料口DN80，四个耳式支座"
    s = {"kind": "vertical_vessel", "shell": {"inner_diameter_mm": 1600, "length_mm": 3000, "thickness_mm": 8}, "nozzles": [{"dn": 80, "purpose_text": "进料口"}], "supports": [{"kind": "lug", "count": 4}]}
    ag = agent(env, did, mock=False)
    out = run(ag.run_plan(VP({"op": "create", "spec": s}), utt))
    assert out.status == "WAITING_CONFIRMATION" and items_of(out) == ["shell.length"]
    assert "3000" in out.message and "筒体长度" in out.message and "切线长" in out.message
    assert out.question["options"] == ["筒体长度 3000", "切线长 3000"]
    assert ag.tool_calls == 0 and ag.replans == 0 and env.rev(did) == rev and env.d(did).vessel is None
    assert state_of(out)["kind"] == "ask" and state_of(out)["utterance"] == utt


def test_model_written_confirmed_does_not_skip_the_replace_confirmation(env, monkeypatch):
    """真实模型路径：模型在 vessel_define 里自己写 confirmed=true / turn_id，解析时被丢掉，替换现有设备参数照样要问用户。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    did = with_vessel(env, MIN_SPEC)
    rev, before = env.rev(did), env.d(did).vessel.model_dump()
    systems = []
    plan = {"thought": "", "intent_categories": ["chem_equipment"], "normalized_utterance": "x", "workflow_steps": [
        {"step_id": "s1", "target_agent": "tool_execution_agent", "depends_on": [], "action_summary": "新建",
         "payload": {"tool": "vessel_define", "args": {"op": "create", "spec": SPEC1, "confirmed": True, "turn_id": "evil"}}}]}

    async def fake(system, user, usage, role="planner", max_tokens=4000, timeout=60, strict=False, json_mode=False):
        systems.append(system)
        return json.dumps(plan, ensure_ascii=False)
    monkeypatch.setattr(planner_mod, "chat_text", fake)
    ag = agent(env, did, mock=False, planner="keep", task_id="T7")
    out = run(ag.run(DEMO1))
    assert len(systems) == 1 and "# 化工设备图" in systems[0] and "- vessel_define(" in systems[0]      # 图上有设备参数：设备类别并进子集
    assert out.status == "WAITING_CONFIRMATION" and "确认" in out.question["question"] and out.question["options"] == ["确认", "取消"]
    assert state_of(out)["kind"] == "confirm" and ag.replans == 0
    assert env.rev(did) == rev and env.d(did).vessel.model_dump() == before


# ------------------------------------------------------------------ 确认续接
def test_replacing_an_existing_vessel_needs_confirmation_cancel_keeps_it_and_yes_replaces_it(env):
    did = with_vessel(env, MIN_SPEC)
    rev = env.rev(did)
    plan = VP({"op": "create", "spec": SPEC1})
    out = run(agent(env, did, task_id="T5").run_plan(plan, DEMO1))
    assert out.status == "WAITING_CONFIRMATION" and "已有设备参数" in out.question["question"] and "确认" in out.question["question"]
    assert state_of(out)["kind"] == "confirm" and env.rev(did) == rev
    cancel = run(agent(env, did, task_id="T5").run(DEMO1, resume=out.messages, answer="取消"))
    assert cancel.status == "READY" and "取消" in cancel.message and env.rev(did) == rev and len(env.d(did).vessel.nozzles) == 2
    ok = run(agent(env, did, task_id="T5").run(DEMO1, resume=out.messages, answer="确认"))
    assert ok.status in ("READY", "READY_WITH_WARNINGS") and env.rev(did) > rev
    v = env.d(did).vessel
    assert len(v.nozzles) == 5 and v.shell.material == "Q345R" and v.sources["shell.inner_diameter_mm"] == "user:T5"


# ------------------------------------------------------------------ 反问之后（_resume）
def ask_state(env, did, items, utterance="画一个立式储罐"):
    out = run(agent(env, did, task_id="T3").run_plan(ASK(items), utterance))
    assert out.status == "WAITING_CONFIRMATION" and state_of(out)["kind"] == "ask"
    return out


DRAWING_TYPE = [{"path": "drawing_type", "label": "要画的图种", "options": ["工艺流程图", "化工设备图（装配图）"]},
                {"path": "shell.inner_diameter_mm", "label": "筒体内径 Di（mm）", "options": ["1000", "1600"]}]


@pytest.mark.parametrize("answer", ["工艺流程图", "画工艺流程图", "流程图", "PFD", "P&ID"])
def test_answering_process_flow_diagram_to_the_drawing_type_question_switches_mode(env, answer):
    did = new_eq(env)
    rev = env.rev(did)
    first = ask_state(env, did, DRAWING_TYPE)
    ag = agent(env, did, task_id="T3")
    out = run(ag.run("画一个立式储罐", resume=first.messages, answer=answer))
    assert out.status == "READY" and "化工流程图" in out.message and out.drawing_id == did
    sw = [p for k, p in ag.events if k == "switch"]
    assert len(sw) == 1 and sw[0]["mode"] == "pfd" and sw[0]["prompt"] == "画一个立式储罐" and sw[0]["agent"] == "general"
    assert ag.tool_calls == 0 and env.rev(did) == rev and env.d(did).vessel is None


@pytest.mark.parametrize("answer", ["化工设备图（装配图）", "设备图", "不要工艺流程图，画设备图", "装配图"])
def test_answering_equipment_drawing_does_not_switch_and_replans_with_the_combined_utterance(env, answer):
    did = new_eq(env)
    first = ask_state(env, did, DRAWING_TYPE)
    ag = agent(env, did, task_id="T3")
    called = []

    async def fresh(prompt, file_ref):
        called.append((prompt, file_ref))
        return Outcome("READY", "x", did)
    ag._fresh = fresh
    out = run(ag.run("画一个立式储罐", resume=first.messages, answer=answer))
    assert out.status == "READY" and not [1 for k, _ in ag.events if k == "switch"]
    assert called == [("画一个立式储罐；" + normalize(answer).text, None)]


def test_process_flow_answer_without_a_drawing_type_item_is_just_parameters(env):
    """待问项里没有 drawing_type 时，“工艺流程图”不触发切换（走正常续规划）。"""
    did = new_eq(env)
    first = ask_state(env, did, [DRAWING_TYPE[1]])
    ag = agent(env, did, task_id="T3")
    called = []

    async def fresh(prompt, file_ref):
        called.append(prompt)
        return Outcome("READY", "x", did)
    ag._fresh = fresh
    run(ag.run("画一个立式储罐", resume=first.messages, answer="工艺流程图"))
    assert called and not [1 for k, _ in ag.events if k == "switch"]


def test_answering_parameters_replans_from_the_original_utterance_plus_the_answer(env):
    did = new_eq(env)
    first = ask_state(env, did, DRAWING_TYPE)
    ag = agent(env, did, task_id="T3")
    called = []

    async def fresh(prompt, file_ref):
        called.append(prompt)
        return Outcome("READY", "x", did)
    ag._fresh = fresh
    run(ag.run("画一个立式储罐", resume=first.messages, answer="设备图，内径一千六，筒体高两千四百，壁厚八"))
    assert called == ["画一个立式储罐；" + normalize("设备图，内径一千六，筒体高两千四百，壁厚八").text] and "内径1600" in called[0] and "壁厚8" in called[0]      # 中文数字先规范化再拼


def test_answering_parameters_with_the_rule_planner_does_not_crash(env):
    """mock 下规则 Planner 给不出设备计划：不崩，状态合法（走 _fresh，设备图不被改动）。"""
    did = new_eq(env)
    first = ask_state(env, did, DRAWING_TYPE)
    ag = agent(env, did, task_id="T3", planner="keep")
    out = run(ag.run("画一个立式储罐", resume=first.messages, answer="设备图，内径1600，筒体高2400，壁厚8"))
    assert isinstance(out, Outcome) and out.status in ("WAITING_CONFIRMATION", "FAILED", "READY", "READY_WITH_WARNINGS")
    assert env.d(did).vessel is None                                                              # 规则 Planner 不会凭空建出设备参数


def test_resume_of_a_length_question_goes_through_fresh(env):
    did = new_eq(env)
    utt = "画一台立式储罐的设备图，内径1600，高3000，壁厚8，进料口DN80，四个耳式支座"
    s = {"kind": "vertical_vessel", "shell": {"inner_diameter_mm": 1600, "length_mm": 3000, "thickness_mm": 8}, "nozzles": [{"dn": 80, "purpose_text": "进料口"}], "supports": [{"kind": "lug", "count": 4}]}
    first = run(agent(env, did, mock=False).run_plan(VP({"op": "create", "spec": s}), utt))
    ag = agent(env, did, task_id="T3")
    called = []

    async def fresh(prompt, file_ref):
        called.append(prompt)
        return Outcome("READY", "x", did)
    ag._fresh = fresh
    run(ag.run(utt, resume=first.messages, answer="筒体长度3000"))
    assert called == [utt + "；筒体长度3000"]


# ------------------------------------------------------------------ 入口路由、turn_id、复核
def test_route_hint_equipment_without_a_drawing_builds_the_equipment_template_and_injects_the_turn_id(env):
    seen = {}
    ag = agent(env, None, route_hint="equipment", task_id="T1", planner="keep")

    async def plan(norm_text, original, ctx, cats, prior=None, feedback=None):
        seen.update(norm=norm_text, ctx=ctx, cats=cats)
        return VP({"op": "create", "spec": SPEC1})
    ag.planner.plan = plan
    out = run(ag.run(DEMO1))
    assert out.status in ("READY", "READY_WITH_WARNINGS"), out.message
    d = env.d(out.drawing_id)
    assert d.workspace == "chem_equipment" and d.title == "化工设备图" and "图框" in [l.name for l in d.layers]      # 自动新建的是设备图样板，不是 blank_a3
    assert [p["reason"] for k, p in ag.events if k == "drawing"] == ["auto"]
    assert not any("自动新建了一张" in x for x in ag.defaults)
    assert seen["cats"][0] == "chem_equipment" and seen["ctx"]["workspace"] == "chem_equipment" and "vessel" not in seen["ctx"]
    assert ag.turn_id == "T1"
    sent = next(p for k, p in ag.events if k == "plan")["steps"][0]["args"]
    assert sent["turn_id"] == "T1"                                                                # 引擎注入
    v = d.vessel
    assert v.sources["shell.inner_diameter_mm"] == "user:T1" and v.sources["nozzles[n2].dn"] == "user:T1"
    assert not v.sources.get("nozzles[n2].orientation_deg", "").startswith("user:")              # 没说的方位不是用户值
    assert invented_values(v, "T1", DEMO1) == [] and not any("E_INVENTED_VALUE" in w for w in ag.warnings)
    assert {a["format"] for a in out.artifacts} >= {"dxf", "png", "svg"}


def test_without_route_hint_a_plain_request_does_not_get_an_equipment_drawing(env):
    ag = agent(env, None, planner="keep")
    out = run(ag.run("画一个圆"))
    assert out.drawing_id and env.d(out.drawing_id).workspace != "chem_equipment"
    ag = agent(env, None, route_hint="pfd", planner="keep")                                       # 别的路由结果也不触发
    out = run(ag.run("画一个圆"))
    assert env.d(out.drawing_id).workspace != "chem_equipment"


def test_route_hint_equipment_keeps_an_existing_drawing(env):
    did = env.new("blank_a3")
    ag = agent(env, did, route_hint="equipment", planner="keep")
    out = run(ag.run("画一个圆"))
    assert out.drawing_id == did and not [1 for k, p in ag.events if k == "drawing" and p.get("reason") == "auto"]


def test_planner_gets_the_equipment_category_and_vessel_segment_when_the_drawing_has_a_vessel(env):
    did = with_vessel(env)
    seen = {}
    ag = agent(env, did, planner="keep")

    async def plan(norm_text, original, ctx, cats, prior=None, feedback=None):
        seen.update(ctx=ctx, cats=cats)
        return ASK([{"path": "nozzles", "label": "是哪个管口？"}])
    ag.planner.plan = plan
    out = run(ag.run("把进料口转到90度"))
    assert out.status == "WAITING_CONFIRMATION" and seen["cats"][0] == "chem_equipment"
    assert seen["ctx"]["vessel"]["kind"] == "vertical_vessel" and seen["ctx"]["workspace"] == "chem_equipment"


def test_with_equipment_merges_the_category_for_routes_and_equipment_drawings(env):
    be = LocalBackend(env.svc)
    plain = GeneralAgent(mock=True, backend=be)
    base = ["2d_drafting"]
    assert plain._with_equipment(base, {}) == base and plain._with_equipment(base, None) == base
    assert plain._with_equipment(base, {"workspace": "general"}) == base
    assert plain._with_equipment(base, {"vessel": {"kind": "vertical_vessel"}}) == ["chem_equipment", "2d_drafting"]
    assert plain._with_equipment(base, {"workspace": "chem_equipment"}) == ["chem_equipment", "2d_drafting"]
    hinted = GeneralAgent(mock=True, backend=be, route_hint="equipment")
    assert hinted._with_equipment(base, {}) == ["chem_equipment", "2d_drafting"]
    assert hinted._with_equipment(["2d_drafting", "chem_equipment"], {}) == ["chem_equipment", "2d_drafting"]     # 不重复，设备类别排第一
    assert hinted._with_equipment([], {}) == ["chem_equipment"]
    assert GeneralAgent(mock=True, backend=be, route_hint="pfd")._with_equipment(base, {}) == base
    # 真实上下文
    ctx = run(be.context(with_vessel(env)))
    assert plain._with_equipment(base, ctx)[0] == "chem_equipment"
    assert plain._with_equipment(base, run(be.context(env.new("blank_a3")))) == base
    assert plain._with_equipment(base, run(be.context(new_eq(env))))[0] == "chem_equipment"


def test_turn_id_defaults_and_is_the_task_id(env):
    be = LocalBackend(env.svc)
    assert GeneralAgent(mock=True, backend=be, task_id="abc123").turn_id == "abc123"
    a, b = GeneralAgent(mock=True, backend=be), GeneralAgent(mock=True, backend=be)
    assert a.turn_id.startswith("t") and len(a.turn_id) > 1 and b.turn_id.startswith("t")


def test_inject_turn_reaches_vessel_define_inside_batches_and_keeps_scripted_ids(env):
    ag = GeneralAgent(mock=True, backend=LocalBackend(env.svc), task_id="T8")
    plan = Plan.model_validate({"thought": "t", "workflow_steps": [
        {"step_id": "s1", "target_agent": "tool_execution_agent", "payload": {"tool": "vessel_define", "args": {"op": "patch", "set": {"design.medium": "水"}}}},
        {"step_id": "s2", "target_agent": "tool_execution_agent", "payload": {"tool": "vessel_define", "args": {"op": "delete", "turn_id": "scripted"}}},
        {"step_id": "s3", "target_agent": "tool_execution_agent", "payload": {"tool": "cad_batch_execute", "args": {"steps": [
            {"tool": "vessel_define", "args": {"op": "patch", "set": {"design.medium": "水"}}}, {"tool": "erase_entities", "args": {"entity_ids": ["L1"]}}]}}},
        {"step_id": "s4", "target_agent": "tool_execution_agent", "payload": {"tool": "vessel_query", "args": {"what": "summary"}}}]})
    ag._inject_turn(plan)
    got = [st.payload["args"].get("turn_id") for st in plan.workflow_steps]
    assert got == ["T8", "scripted", None, None]
    assert plan.workflow_steps[2].payload["args"]["steps"][0]["args"]["turn_id"] == "T8"
    assert "turn_id" not in plan.workflow_steps[2].payload["args"]["steps"][1]["args"]


def test_recheck_warns_about_user_values_that_are_not_in_the_utterance(env):
    did = with_vessel(env, SPEC1, turn="T9")
    ag = agent(env, did, task_id="T9")
    ag.be = LocalBackend(env.svc)
    plan = VP({"op": "create", "spec": SPEC1})
    run(ag._recheck_vessel(plan, DEMO1))
    assert ag.warnings == []
    run(ag._recheck_vessel(plan, DEMO1.replace("内径1600", "内径1500")))
    assert len(ag.warnings) == 1 and "E_INVENTED_VALUE" in ag.warnings[0] and "1600" in ag.warnings[0]
    # 脚本计划自带 turn_id：按它查，不因为和引擎的 turn_id 不同就漏查
    ag2 = agent(env, did, task_id="other")
    ag2.be = LocalBackend(env.svc)
    run(ag2._recheck_vessel(VP({"op": "create", "spec": SPEC1, "turn_id": "T9"}), DEMO1.replace("内径1600", "内径1500")))
    assert len(ag2.warnings) == 1
    # 计划里没有 vessel_define 或没有图：不查
    ag3 = agent(env, did, task_id="T9")
    ag3.be = LocalBackend(env.svc)
    run(ag3._recheck_vessel(VP({"what": "summary"}, "vessel_query"), "x"))
    assert ag3.warnings == []


# ------------------------------------------------------------------ 上下文里的 vessel 段
def test_context_of_a_vessel_drawing_has_a_compact_vessel_segment(env):
    did = with_vessel(env)
    ctx = run(LocalBackend(env.svc).context(did))
    v = ctx["vessel"]
    assert v["kind"] == "vertical_vessel" and v["shell"].startswith("Di1600×L2400×δ8") and "长度口径=筒体长度" in v["shell"]
    assert v["supports"] == ["lug×4"] and sorted(v["heads"]) == ["bottom:EHA", "top:EHA"]
    ns = {n["purpose"]: n for n in v["nozzles"]}
    assert set(ns) == {"人孔", "进料口", "出料口", "液位计口"} and ns["进料口"]["dn"] == 80 and ns["人孔"]["mark"] == "M1"
    assert all(n["src"]["orientation"] == "rule" for n in v["nozzles"])                          # 没说的方位来自规则
    assert v["design"]["design_pressure_mpa"] == 0.6 and v["design"]["design_temp_c"] == 80 and v["pending"] == []
    assert len(v["nozzles"]) <= 16 and len(json.dumps(v, ensure_ascii=False)) < 2500
    env.call("vessel_define", did, op="patch", set={"nozzles[purpose=进料口].orientation_deg": 90}, turn_id="t1")
    v = build_context(env.d(did))["vessel"]
    assert next(n for n in v["nozzles"] if n["purpose"] == "进料口")["src"]["orientation"] == "user"            # 用户说了的方位标 user


def test_context_marks_the_length_basis_for_horizontal_vessels(env):
    did = new_eq(env)
    env.call("vessel_define", did, op="create", spec=SPEC_HORIZ, turn_id="t0")
    v = build_context(env.d(did))["vessel"]
    assert v["kind"] == "horizontal_vessel" and "切线长 6000" in v["shell"] and "长度口径=切线长" in v["shell"]
    assert v["supports"] == ["saddle×2"] and sorted(v["heads"]) == ["left:EHA", "right:EHA"]


def test_context_without_vessel_has_no_vessel_segment_and_generated_entities_are_not_counted(env):
    assert "vessel" not in build_context(env.d(env.new("blank_a3")))
    assert "vessel" not in build_context(env.d(new_eq(env)))                                      # 设备图样板但还没有参数
    did = with_vessel(env, MIN_SPEC)
    ctx = build_context(env.d(did))
    assert ctx["entity_count"] > 100 and ctx["inventory"] == {} and "scene_digest" not in ctx      # 生成的图元（equip:auto）不进盘点
    env.call("draw_circular_primitives", did, curve_type="circle", method="center_radius", center="0,0", radius=5)
    assert build_context(env.d(did))["inventory"] == {"circle": 1}
    steps = [{"tool": "draw_circular_primitives", "args": {"curve_type": "circle", "method": "center_radius", "center": f"{i * 10},0", "radius": 2}} for i in range(31)]
    env.call("cad_batch_execute", did, steps=steps)
    ctx = build_context(env.d(did))
    assert ctx["inventory"] == {"circle": 32} and sum(sum(c["counts"].values()) for c in ctx["scene_digest"]) == 32     # 摘要也只数手工图元


# ------------------------------------------------------------------ 执行器与导出文件
def test_executor_returns_the_pending_items_of_an_incomplete_vessel(env):
    did = new_eq(env)
    rev = env.rev(did)
    res = run(Executor(LocalBackend(env.svc), did).run(VP({"op": "create", "spec": {"kind": "horizontal_vessel"}})))
    assert res.status == "failed" and res.failed_step == "s1" and res.error["error_code"] == "SPEC_INCOMPLETE"
    assert [p["path"] for p in res.error["pending"]] == ["shell.inner_diameter_mm", "shell.length", "shell.thickness_mm", "nozzles"]      # 卧式不问支座型式
    assert env.rev(did) == rev


def test_executor_reports_misspelled_vessel_fields_as_invalid_args(env):
    did = new_eq(env)
    res = run(Executor(LocalBackend(env.svc), did).run(VP({"op": "create", "spec": {"kind": "vertical_vessel", "shell": {"inner_diameter": 1600}}})))
    assert res.status == "failed" and res.error["error_code"] == "INVALID_ARGS" and "inner_diameter" in json.dumps(res.error, ensure_ascii=False)


class FakeBackend(Backend):
    def __init__(self, data):
        self.data = data

    async def call(self, tool, args, drawing_id):
        return {"ok": True, "summary": "ok", "data": self.data}


def test_executor_collects_single_and_multiple_exported_files_and_forwards_switch():
    files = [{"format": "dxf", "file_ref": "a.dxf"}, {"format": "pdf", "file_ref": "a.pdf"}, "not-a-dict", None]
    ev = []
    ex = Executor(FakeBackend({"file": {"format": "png", "file_ref": "a.png"}, "files": files, "switch_to": {"mode": "pfd"}}), "d1", lambda k, p: ev.append((k, p)))
    res = run(ex.run(VP({"export": ["dxf", "pdf"]}, "vessel_generate_drawing")))
    assert res.status == "done" and [f["file_ref"] for f in res.files] == ["a.png", "a.dxf", "a.pdf"]
    assert ("switch", {"mode": "pfd"}) in ev
    res = run(Executor(FakeBackend({}), "d1").run(VP({"what": "summary"}, "vessel_query")))
    assert res.status == "done" and res.files == []


def test_requested_vessel_exports_are_listed_once_per_format(env):
    """vessel_generate_drawing(export=[dxf,pdf]) 的结果文件和自动导出的 DXF/PNG/SVG 不重复：DXF 一份、PDF 一份，PNG 版面图和模型预览图各一份。"""
    did = with_vessel(env, MIN_SPEC)
    ag = agent(env, did)
    out = run(ag.run_plan(VP({"export": ["dxf", "pdf"]}, "vessel_generate_drawing"), "导出DXF和PDF"))
    assert out.status in ("READY", "READY_WITH_WARNINGS"), out.message
    fmts = [a["format"] for a in out.artifacts]
    assert fmts.count("dxf") == 1 and fmts.count("pdf") == 1 and fmts.count("svg") == 1, fmts
    pngs = [a for a in out.artifacts if a["format"] == "png"]
    assert len(pngs) == 2 and {a.get("source") for a in pngs} == {"layout:设备图", "model"}
    assert next(a for a in out.artifacts if a["format"] == "pdf")["source"] == "layout:设备图"
    assert next(a for a in out.artifacts if a["format"] == "dxf")["source"] == "model+layouts"
    assert len({a["file_ref"] for a in out.artifacts}) == len(out.artifacts)


def test_vessel_generate_drawing_without_a_vessel_fails_cleanly(env):
    did = new_eq(env)
    out = run(agent(env, did).run_plan(VP({"views": ["orientation"]}, "vessel_generate_drawing"), "出一张管口方位图"))
    assert out.status == "FAILED" and "还没有设备参数" in out.message
