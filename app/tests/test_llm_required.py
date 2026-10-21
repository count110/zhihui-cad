"""产品里没有离线版：自然语言口令必须经过大模型。没接入（没填 API Key / 接口失败 / 给不出合格计划）就明确失败，不会悄悄退回规则 Planner。
（ZHIHUI_LLM=mock 的规则 Planner 只给自动化测试和开发用。）"""
import asyncio
import json

import pytest

from gen_helpers import Env
from zhihui.agent.general import planner as planner_mod
from zhihui.agent.general.backend import LocalBackend
from zhihui.agent.general.engine import GeneralAgent


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def real_agent(env, did=None):
    events = []
    ag = GeneralAgent(on_event=lambda k, p: events.append((k, p)), mock=False, backend=LocalBackend(env.svc), drawing_id=did)
    ag.events = events
    return ag


@pytest.fixture
def no_key(monkeypatch):
    for k in ("DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY", "ZHIPUAI_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ZHIHUI_LLM_PRIMARY", "deepseek")
    monkeypatch.setenv("ZHIHUI_LLM_BACKUP", "")


def test_without_an_api_key_natural_language_fails_clearly_and_draws_nothing(env, no_key):
    did = env.new("blank_a3")
    out = run(real_agent(env, did).run("画一个半径10的圆，圆心在原点"))
    assert out.status == "FAILED" and "还没有接入大模型" in out.message and "API Key" in out.message
    assert env.ids(did) == []                                                                              # 没有悄悄用规则画出来


def test_llm_failure_is_reported_not_papered_over_by_rules(env, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("ZHIHUI_LLM_PRIMARY", "deepseek")

    class Boom:
        def __init__(self, *a, **k):
            self.generation = {}

        async def chat(self, *a, **k):
            raise ConnectionError("network unreachable")

        class client:
            @staticmethod
            async def close():
                pass
    import zhihui.agent.llm as llm
    monkeypatch.setattr(llm, "OpenAICompatLLM", Boom)
    monkeypatch.setattr(llm, "FailoverLLM", lambda p, b: p)
    did = env.new("blank_a3")
    ag = real_agent(env, did)
    out = run(ag.run("画一个半径10的圆，圆心在原点"))
    assert out.status == "FAILED" and "调用大模型失败" in out.message and "ConnectionError" in out.message and env.ids(did) == []
    assert any(k == "llm_unavailable" for k, _ in ag.events)


def test_invalid_plans_twice_means_failure_not_rules(env, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    calls = []

    async def bad(system, user, usage, role="planner", max_tokens=4000, timeout=60, strict=False, json_mode=False):
        calls.append(role)
        return "这不是 JSON"
    monkeypatch.setattr(planner_mod, "chat_text", bad)
    did = env.new("blank_a3")
    out = run(real_agent(env, did).run("画一个半径10的圆，圆心在原点"))
    assert out.status == "FAILED" and "连续 3 次都没有给出格式合格的计划" in out.message and len(calls) == 3 and env.ids(did) == []


def test_a_valid_llm_plan_is_executed_through_the_same_executor(env, monkeypatch):
    """用桩模型返回一个合格的计划，验证真实路径（Planner → 护栏 → Executor）接得通。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    plan = {"thought": "画圆", "intent_categories": ["2d_drafting"], "normalized_utterance": "画1个半径10的圆，圆心在原点",
            "workflow_steps": [{"step_id": "s1", "target_agent": "tool_execution_agent", "depends_on": [], "action_summary": "画圆",
                                "payload": {"tool": "draw_circular_primitives", "args": {"curve_type": "circle", "method": "center_radius", "center": "0,0", "radius": 10}}}]}
    seen = {}

    async def fake(system, user, usage, role="planner", max_tokens=4000, timeout=60, strict=False, json_mode=False):
        seen["strict"], seen["role"] = strict, role
        usage["prompt_tokens"] = usage.get("prompt_tokens", 0) + 123
        return json.dumps(plan, ensure_ascii=False)
    monkeypatch.setattr(planner_mod, "chat_text", fake)
    did = env.new("blank_a3")
    ag = real_agent(env, did)
    out = run(ag.run("画一个半径十的圆，圆心在原点"))
    assert out.status in ("READY", "READY_WITH_WARNINGS") and seen == {"strict": True, "role": "planner"}
    c = env.ent(did, env.ids(did)[0])
    assert c.kind == "circle" and c.data["r"] == 10 and ag.usage()["planner"] == "llm" and ag.usage()["prompt_tokens"] == 123


def test_the_numeric_source_guard_still_applies_to_llm_plans(env, monkeypatch):
    """模型自己编了用户没说的数值：护栏拦下（真实模式下会让模型重来一次，再不行就失败），不会画出来。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    plan = {"thought": "", "intent_categories": ["2d_drafting"], "normalized_utterance": "画一个圆", "workflow_steps": [
        {"step_id": "s1", "target_agent": "tool_execution_agent", "depends_on": [], "action_summary": "画圆",
         "payload": {"tool": "draw_circular_primitives", "args": {"curve_type": "circle", "method": "center_radius", "center": "0,0", "radius": 37}}}]}

    async def fake(system, user, usage, role="planner", max_tokens=4000, timeout=60, strict=False, json_mode=False):
        return json.dumps(plan, ensure_ascii=False)
    monkeypatch.setattr(planner_mod, "chat_text", fake)
    did = env.new("blank_a3")
    out = run(real_agent(env, did).run("画一个圆"))
    assert out.status == "FAILED" and "护栏" in out.message and env.ids(did) == []


def test_typed_autocad_commands_still_work_without_a_key(env, no_key):
    """AutoCAD 命令是直接输入，不是自然语言口令：不需要大模型。"""
    did = env.new("blank_a3")
    out = run(real_agent(env, did).run("L 0,0 @100,0"))
    assert out.status in ("READY", "READY_WITH_WARNINGS") and env.ids(did) == ["L1"]


def test_health_reports_missing_key_for_the_front_end(monkeypatch):
    from fastapi.testclient import TestClient
    for k in ("DEEPSEEK_API_KEY",):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ZHIHUI_LLM", "real")
    monkeypatch.setenv("ZHIHUI_LLM_PRIMARY", "deepseek")
    from zhihui.api.app import app
    with TestClient(app) as c:
        h = c.get("/api/health").json()
    assert h["llm"] == "real" and h["key_missing"] is True


def test_front_end_has_no_offline_mode_left():
    import pathlib
    web = pathlib.Path(__file__).resolve().parents[1] / "05_前端" / "web"
    text = "".join((web / n).read_text(encoding="utf-8") for n in ("index.html", "cad.html", "app.js", "cad.js", "shell.js"))
    assert "离线演示" not in text and "离线演示（不调用大模型）" not in text
    assert "测试模式（未接入大模型）" in text and "还没有接入大模型" in text


@pytest.fixture
def real_client(monkeypatch):
    from fastapi.testclient import TestClient
    for k in ("DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY", "ZHIPUAI_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ZHIHUI_LLM", "real")
    monkeypatch.setenv("ZHIHUI_LLM_PRIMARY", "deepseek")
    monkeypatch.setenv("ZHIHUI_LLM_BACKUP", "")
    monkeypatch.setenv("ZHIHUI_GENERAL_BACKEND", "local")
    from zhihui.api.app import app
    with TestClient(app) as c:
        yield c


def _wait(c, tid, timeout=60):
    import time
    t0 = time.time()
    while time.time() - t0 < timeout:
        t = c.get(f"/api/tasks/{tid}").json()
        if t["status"] in ("READY", "READY_WITH_WARNINGS", "FAILED", "WAITING_CONFIRMATION", "CANCELLED"):
            return t
        time.sleep(0.2)
    raise TimeoutError(tid)


def test_real_mode_without_key_fails_for_flowchart_requests_too(real_client):
    r = real_client.post("/api/tasks", data={"prompt": "绘制乙醇水精馏工艺方案：原料罐 V-101 → 进料泵 P-101 → 精馏塔 T-101。不填写任何未提供的设计数值。"}).json()
    assert r["mode"] == "pfd"
    t = _wait(real_client, r["id"])
    assert t["status"] == "FAILED" and "还没有接入大模型" in t["result"]["message"] and "内部错误" not in t["result"]["message"]
    assert not t["result"].get("artifacts")                                                              # 没有悄悄用规则出一张图


def test_real_mode_without_key_fails_for_general_drawing_requests_too(real_client):
    did = real_client.post("/api/drawings", json={"template_name": "blank_a3"}).json()["drawing_id"]
    r = real_client.post(f"/api/drawings/{did}/chat", json={"prompt": "画一个半径10的圆，圆心在原点"}).json()
    t = _wait(real_client, r["id"])
    assert t["status"] == "FAILED" and "还没有接入大模型" in t["result"]["message"]
    assert real_client.get(f"/api/drawings/{did}/scene").json()["entities"] == []
    cmd = real_client.post(f"/api/drawings/{did}/command", json={"text": "C 0,0 10"}).json()                 # 直接输入的 AutoCAD 命令不需要大模型
    assert cmd["ok"] and cmd["handled"]


# ------------------------------------------------------------------ 真实模型下暴露出来的问题（2026-10-20 用 DeepSeek 实跑 14 条口令后加的）
def test_made_up_intent_categories_do_not_invalidate_a_plan():
    """模型自造的类别名（replication_array、entity_recognition、layout_and_print）曾让整份计划作废——类别只是路由提示。"""
    from zhihui.agent.general.plan import Plan
    p = Plan.model_validate({"intent_categories": ["replication_array", "entity_recognition", "layout_and_print", "annotation_dimension", "胡说八道"], "workflow_steps": [],
                             "clarification_needed": True, "clarification": {"question": "q"}})
    assert p.intent_categories == ["2d_editing", "view_select", "layout_plotting", "annotation_dimension"]
    assert Plan.model_validate({"intent_categories": "not a list", "workflow_steps": [], "clarification_needed": True, "clarification": {"question": "q"}}).intent_categories == []


def test_few_shot_examples_are_valid_plans_with_valid_tool_args():
    import json
    from zhihui.agent.general import prompts
    from zhihui.agent.general.plan import parse_plan, validate_plan
    from zhihui.tools.general.refs import has_refs
    from zhihui.tools.general.registry import REGISTRY, validate
    assert len(prompts.FEW_SHOTS) >= 10
    for utt, raw in prompts.FEW_SHOTS:
        plan = parse_plan(json.dumps(raw, ensure_ascii=False), None)
        assert validate_plan(plan) == [], utt
        for st in plan.workflow_steps:
            subs = st.payload.get("args", {}).get("steps", []) if st.payload.get("tool") == "cad_batch_execute" else [st.payload]
            for sub in subs:
                if sub.get("tool") in REGISTRY and "args" in sub and not has_refs(sub["args"]):
                    validate(REGISTRY[sub["tool"]], sub["args"])                                              # 没有变量引用的步骤，参数必须能通过函数自己的校验


def test_planner_asks_the_api_for_json_mode_and_gives_the_model_three_chances(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("ZHIHUI_LLM_PRIMARY", "deepseek")
    monkeypatch.setenv("ZHIHUI_LLM_BACKUP", "")
    seen = []

    class Fake:
        def __init__(self, *a, **k):
            self.generation = {}

        async def chat(self, messages, tools):
            seen.append(dict(self.generation))
            from zhihui.agent.llm import Turn
            return Turn(content="不是 JSON", model="fake")

        class client:
            @staticmethod
            async def close():
                pass
    import zhihui.agent.llm as llm
    monkeypatch.setattr(llm, "OpenAICompatLLM", Fake)
    monkeypatch.setattr(llm, "FailoverLLM", lambda p, b: p)
    from zhihui.agent.general.planner import Planner
    with pytest.raises(Exception) as ex:
        run(Planner(False).plan("画个圆", "画个圆", {}, ["2d_drafting"]))
    assert "连续 3 次都没有给出格式合格的计划" in str(ex.value) and len(seen) == 3
    assert all(g.get("response_format") == {"type": "json_object"} for g in seen)                              # DeepSeek 的 JSON 模式：不再出现语法错误的 JSON
