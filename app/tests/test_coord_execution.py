"""Execution policy contracts; exercise real storage/MCP for delivery and replay."""
import asyncio
import json
import math
import sqlite3
from pathlib import Path

import ezdxf
import pytest

from zhihui.agent.execution import ExecutionConfig, ExecutionControl, compliant_paper, issue_score, signature, usable
from zhihui.agent.llm import DEMO_PLAN, ScriptedLLM, plan_to_steps
from zhihui.agent.orchestrator import Orchestrator, _openai_tools
from zhihui.agent.replay import CASE_DIR, export_cases, run_case, sha256
from zhihui.tools.core import ToolService


def validation(errors=(), warnings=()):
    return {"ok": True, "passed": not errors, "errors": [{"code": c, "message": c} for c in errors],
            "warnings": [{"code": c, "message": c} for c in warnings], "stats": {"equipment": 2}}


def build(svc):
    did = svc.create_drawing(title=DEMO_PLAN["title"])["drawing_id"]
    assert svc.place_equipment(did, DEMO_PLAN["equipment"])["ok"]
    assert svc.connect_ports(did, DEMO_PLAN["connections"])["ok"]
    assert svc.auto_layout(did)["ok"]
    return did


def test_canonical_signature_ignores_order_and_float_noise():
    assert signature("move", {"x": 1.01, "y": [2.04, 3]}) == signature("move", {"y": [2, 3.0], "x": 1})
    assert signature("move", {"x": 1.1}) != signature("move", {"x": 1})
    assert signature("move", {"x": True}) != signature("move", {"x": 1})


def test_non_finite_arguments_are_rejected():
    c = ExecutionControl()
    assert c.before("move_equipment", {"dx": math.nan})["error_code"] == "E_BAD_JSON"
    with pytest.raises(ValueError):
        signature("move", {"x": math.inf})
    c.observe("move_equipment", {"dx": math.nan}, {"ok": False, "error_code": "E_BAD_JSON"}, executed=False)
    assert c.attempt_log[-1]["signature"] == "move_equipment:invalid_json"


def test_first_failed_signature_then_two_blocks_escalate_without_execution():
    c = ExecutionControl()
    args = {"drawing_id": "dwg_x", "pick": {"x": 1762.5, "y": 6381.2}}
    failure = {"ok": False, "error_code": "E_HATCH_NO_BOUNDARY", "message": "not closed"}
    assert c.before("add_hatch", args) is None
    c.observe("add_hatch", args, failure)
    blocked = c.before("add_hatch", args)
    assert blocked["error_code"] == "E_REPEATED_CALL"
    assert c.observe("add_hatch", args, blocked, executed=False) is None
    blocked = c.before("add_hatch", args)
    assert c.observe("add_hatch", args, blocked, executed=False) == "escalate"
    assert c.duplicates == 2 and c.issues[c.issue_key("add_hatch", args)].attempts == 1
    assert c.attempt_log[-1]["executed"] is False


def test_hatch_retry_cannot_evade_dedup_by_changing_pick_point():
    c = ExecutionControl()
    args = {"drawing_id": "dwg_x", "pick": {"x": 1762.5, "y": 6381.2}}
    c.observe("add_hatch", args, {"ok": False, "error_code": "E_HATCH_NO_BOUNDARY"})
    assert c.before("add_hatch", {**args, "pick": {"x": 2000, "y": 7000}})["error_code"] == "E_REPEATED_CALL"


def test_playbook_actions_are_ordered_and_bounded():
    c = ExecutionControl()
    args = {"drawing_id": "dwg_x"}
    c.observe("add_hatch", args, {"ok": False, "error_code": "E_HATCH_NO_BOUNDARY"})
    issue = c.issues[c.issue_key("add_hatch", args)]
    assert c.action(issue)["action"] == "use_named_region"
    issue.blocked = 2
    assert c.action(issue) == {"action": "skip_hatch_and_record_gap", "escalate": "degrade", "issue_id": issue.id}


def test_invalid_port_allows_only_one_corrected_retry():
    c = ExecutionControl()
    args = {"drawing_id": "dwg_x", "items": [{"from_port": "P-101.bad", "to_port": "V-101.in"}]}
    c.observe("connect_ports", args, {"ok": False, "error_code": "E_PORT_INVALID", "hint": "ports: in,out"})
    changed = {**args, "items": [{"from_port": "P-101.also_bad", "to_port": "V-101.in"}]}
    assert c.before("connect_ports", changed) is None
    c.observe("connect_ports", changed, {"ok": False, "error_code": "E_PORT_INVALID"})
    assert c.before("connect_ports", {**args, "items": []})["error_code"] == "E_ISSUE_EXHAUSTED"


def test_resolved_issue_does_not_permanently_disable_later_valid_operations():
    c = ExecutionControl()
    first = {"drawing_id": "dwg_x", "items": [{"from_port": "P-101.wrong", "to_port": "V-101.in"}]}
    retry = {"drawing_id": "dwg_x", "items": [{"from_port": "P-101.wrong2", "to_port": "V-101.in"}]}
    c.observe("connect_ports", first, {"ok": False, "error_code": "E_PORT_INVALID"})
    c.observe("connect_ports", retry, {"ok": False, "error_code": "E_PORT_INVALID"})
    corrected = {"drawing_id": "dwg_x", "items": [{"from_port": "P-101.out", "to_port": "V-101.in"}]}
    c.observe("connect_ports", corrected, {"ok": True, "drawing_id": "dwg_x", "revision": 2})
    later = {"drawing_id": "dwg_x", "items": [{"from_port": "V-101.out", "to_port": "T-101.in"}]}
    assert c.before("connect_ports", later) is None
    assert c.before("connect_ports", first)["error_code"] == "E_REPEATED_CALL"


def test_self_loop_is_blocked_before_tool_execution():
    c = ExecutionControl()
    r = c.before("connect_ports", {"items": [{"from_port": "F-101.air_in", "to_port": "F-101.air_in"}]})
    assert r["error_code"] == "E_SELF_LOOP" and "terminal" in r["message"]


def test_empty_search_is_a_failed_signature_even_when_tool_ok():
    c = ExecutionControl()
    args = {"drawing_id": "dwg_x", "query": "nothing"}
    c.observe("find_entities", args, {"ok": True, "candidates": []})
    assert c.before("find_entities", args)["error_code"] == "E_REPEATED_CALL"


def test_blind_title_block_search_is_capability_gap():
    c = ExecutionControl()
    assert c.before("find_entities", {"layer": "TITLE_BLOCK_TEXT_21"})["error_code"] == "E_CAPABILITY_MISSING"


def test_a_b_a_freezes_only_the_oscillating_object():
    c = ExecutionControl()
    args = {"drawing_id": "dwg_x", "equipment_ref": "P-101"}
    c.observe("move_equipment", args, {"ok": True, "drawing_id": "dwg_x", "revision": 2, "old_position": [0, 0], "new_position": [0, 500]})
    decision = c.observe("move_equipment", args, {"ok": True, "drawing_id": "dwg_x", "revision": 3, "old_position": [0, 500], "new_position": [0, 0]})
    assert decision == "oscillation" and c.oscillations == 1
    assert c.before("move_equipment", args)["error_code"] == "E_OSCILLATION"
    assert c.before("move_equipment", {**args, "equipment_ref": "P-102"}) is None


def test_three_modifications_without_score_improvement_escalate():
    c = ExecutionControl()
    for revision in range(1, 4):
        assert c.record_validation("dwg_x", revision, validation(), compare=True) is None
    assert c.record_validation("dwg_x", 4, validation(), compare=True) == "layout_fixer"


def test_worse_score_requests_immediate_best_revision_rollback():
    c = ExecutionControl()
    c.revisions["dwg_x"] = 2
    c.record_validation("dwg_x", 2, validation(), compare=True)
    c.revisions["dwg_x"] = 3
    assert c.record_validation("dwg_x", 3, validation(["E_OVERLAP"]), compare=True) == "rollback"
    assert c.best["dwg_x"] == {"revision": 2, "score": 0} and c.needs_rollback("dwg_x")
    assert issue_score(validation(["a", "b"], ["w"])) == 21


def test_initial_empty_or_unplaced_revision_can_never_be_the_best():
    c = ExecutionControl()
    c.record_validation("dwg_x", 1, {**validation(), "stats": {"equipment": 0}})
    c.record_validation("dwg_x", 2, validation(["E_UNPLACED"]))
    assert "dwg_x" not in c.best


def test_stall_counter_resets_only_on_revision_progress():
    c = ExecutionControl(ExecutionConfig(stall_calls=3))
    c.observe("inspect_drawing", {}, {"ok": True, "drawing_id": "dwg_x", "revision": 1})
    for i in range(2):
        assert c.observe("find_entities", {"query": str(i)}, {"ok": True, "candidates": []}) is None
    assert c.observe("find_entities", {"query": "last"}, {"ok": True, "candidates": []}) == "stall"
    c.observe("move_equipment", {}, {"ok": True, "drawing_id": "dwg_x", "revision": 2})
    assert c.no_revision == 0


def test_budget_reserves_delivery_calls_and_enforces_global_limit():
    now = [0.]
    c = ExecutionControl(ExecutionConfig(max_calls=8, finish_reserve=2, max_seconds=180), clock=lambda: now[0])
    for _ in range(6):
        c.consume()
    assert c.budget_reason() and c.budget_reason(finishing=True) is None
    c.consume(); c.consume()
    with pytest.raises(TimeoutError):
        c.consume()
    now[0] = 181
    assert c.remaining_seconds == 0


def test_restore_retains_history_and_uses_a_monotonically_new_revision():
    svc = ToolService()
    did = build(svc)
    original = svc.store.load(did)
    historical = svc.store._dir(did)/"revs"/f"{original.revision:04d}.json"
    old_hash = sha256(historical)
    assert svc.move_equipment(did, "F-201", dx=200)["ok"]
    moved = svc.store.load(did)
    restored = svc.inspect_drawing(drawing_id=did, restore_revision=original.revision)
    assert restored["ok"] and restored["revision"] == moved.revision+1
    assert svc.store.load(did).get_equipment("F-201").x == original.get_equipment("F-201").x
    assert sha256(historical) == old_hash and svc.store.load(did, moved.revision).get_equipment("F-201").x != original.get_equipment("F-201").x


def test_coordinator_rollback_works_through_mcp_without_local_store_access():
    svc = ToolService()
    did = build(svc)
    before = svc.store.load(did)
    orch = Orchestrator(ScriptedLLM([]))
    orch.control.revisions[did] = before.revision
    orch.control.record_validation(did, before.revision, svc.validate_drawing(did))
    assert svc.move_equipment(did, "F-201", dx=200)["ok"]
    after = svc.store.load(did)
    orch.control.revisions[did] = after.revision
    orch.control.record_validation(did, after.revision, validation(["E_OVERLAP"]))
    async def check():
        from zhihui.agent.orchestrator import mcp_client
        async with mcp_client() as client:
            await orch._rollback(client, did)
    asyncio.run(check())
    assert orch.control.rollbacks == 1
    assert svc.store.load(did).get_equipment("F-201").x == before.get_equipment("F-201").x


def test_validation_cache_is_per_revision_and_returns_independent_objects():
    svc = ToolService()
    did = build(svc)
    v = svc.validate_drawing(did)
    v["errors"].append({"code": "fake_test_error"})
    assert svc.validate_drawing(did)["passed"] and not svc.validate_drawing(did)["errors"]
    assert svc.move_equipment(did, "F-201", dx=200)["ok"]
    assert svc.validate_drawing(did)["revision"] == v["revision"]+1


def test_validation_cache_invalidates_on_standard_rule_changes(monkeypatch):
    svc = ToolService()
    did = build(svc)
    assert svc.validate_drawing(did)["passed"]
    from zhihui.cad import standards
    std = standards.for_drawing(svc.store.load(did))
    changed = json.loads(json.dumps(std.raw))
    changed["cache_version_test"] = "revised"
    monkeypatch.setattr(std, "raw", changed)
    size = len(svc._validation_cache)
    assert svc.validate_drawing(did)["passed"] and len(svc._validation_cache) == size+1


def test_models_cannot_see_validation_export_or_restore_parameters():
    from zhihui.agent.orchestrator import mcp_client
    async def check():
        async with mcp_client() as client:
            return _openai_tools((await client.list_tools()).tools)
    functions = {t["function"]["name"]: t["function"] for t in asyncio.run(check())}
    assert "validate_drawing" not in functions and "export_artifacts" not in functions
    assert "restore_revision" not in functions["inspect_drawing"]["parameters"]["properties"]
    assert "include_state" not in functions["inspect_drawing"]["parameters"]["properties"]


def test_compliant_paper_is_computed_on_copy_with_default_hg_a1_minimum():
    svc = ToolService()
    did = build(svc)
    drawing = svc.store.load(did)
    old = drawing.to_json()
    from zhihui.ir import PAPER_SIZES
    paper = compliant_paper(drawing)
    assert paper in PAPER_SIZES and PAPER_SIZES[paper][0]*PAPER_SIZES[paper][1] >= 841*594
    assert drawing.to_json() == old and paper != "A1x3"


def test_warning_export_has_visible_watermark_report_and_original_ir_unchanged():
    svc = ToolService()
    did = build(svc)
    original = svc.store.load(did).to_json()
    result = svc.export_artifacts(did, ["dxf"], allow_warnings=True, delivery_notes=["填充边界未定义，已跳过"])
    assert result["ok"]
    doc = ezdxf.readfile(svc.store.resolve_ref(next(a["file_ref"] for a in result["files"] if a["format"] == "dxf")))
    assert any("未完全通过校验" in e.dxf.text for e in doc.modelspace().query("TEXT"))
    report = svc.store.resolve_ref(next(a["file_ref"] for a in result["files"] if a["format"] == "delivery_report"))
    assert "填充边界未定义" in report.read_text(encoding="utf-8")
    assert svc.store.load(did).to_json() == original


def test_warning_export_cannot_bypass_unplaced_equipment_guard():
    svc = ToolService()
    did = svc.create_drawing()["drawing_id"]
    svc.place_equipment(did, DEMO_PLAN["equipment"])
    assert not svc.export_artifacts(did, ["dxf"], allow_warnings=True)["ok"]
    assert not usable(validation(["E_SELF_LOOP"]))


def test_model_failure_after_build_delivers_existing_drawing_with_warning():
    class ModelOutage:
        def __init__(self):
            self.script = ScriptedLLM(plan_to_steps(DEMO_PLAN)[:-1])
        async def chat(self, messages, tools):
            if self.script.i >= len(self.script.steps):
                raise ConnectionError("simulated model outage")
            return await self.script.chat(messages, tools)
    out = asyncio.run(Orchestrator(ModelOutage()).run("画垃圾焚烧流程"))
    assert out.status == "READY_WITH_WARNINGS" and any(a["format"] == "dxf" for a in out.artifacts)


def test_model_budget_exhaustion_delivers_before_global_limit():
    steps = plan_to_steps(DEMO_PLAN)[:-1]
    steps.extend([[{"name": "inspect_drawing", "arguments": {"drawing_id": "$drawing_id"}}]]*20)
    out = asyncio.run(Orchestrator(ScriptedLLM(steps), config=ExecutionConfig(max_calls=14)).run("画垃圾焚烧流程"))
    assert out.status == "READY_WITH_WARNINGS" and out.usage["tool_calls"] <= 14


def test_repeated_bad_hatch_calls_execute_once_and_report_original_error(data_dir):
    steps = plan_to_steps(DEMO_PLAN)[:-1]
    call = [{"name": "add_hatch", "arguments": {"drawing_id": "$drawing_id", "pick": {"x": -9999, "y": -9999}}}]
    steps.extend([call]*30)
    events = []
    out = asyncio.run(Orchestrator(ScriptedLLM(steps), lambda k,p: events.append((k,p))).run("垃圾焚烧流程加填充"))
    assert out.status == "READY_WITH_WARNINGS" and out.usage["repeated_calls_blocked"] == 2
    assert sum(k == "tool_call" and p["name"] == "add_hatch" for k,p in events) == 1
    report = data_dir/next(a["file_ref"] for a in out.artifacts if a["format"] == "delivery_report")
    assert "E_HATCH_NO_BOUNDARY" in report.read_text(encoding="utf-8")


def test_failed_hatch_cannot_be_silently_reported_as_ready():
    steps = plan_to_steps(DEMO_PLAN)[:-1]
    steps.append([{"name": "add_hatch", "arguments": {"drawing_id": "$drawing_id", "pick": {"x": -9999, "y": -9999}}}])
    steps.append({"say": "任务已完成"})
    out = asyncio.run(Orchestrator(ScriptedLLM(steps)).run("垃圾焚烧流程加填充"))
    assert out.status == "READY_WITH_WARNINGS" and any(a["format"] == "delivery_report" for a in out.artifacts)


def test_different_empty_searches_are_bounded_without_exceeding_budget():
    steps = plan_to_steps(DEMO_PLAN)[:-1]
    steps.extend([[{"name": "find_entities", "arguments": {"drawing_id": "$drawing_id", "layer": "nonexistent_"+str(n)}}] for n in range(40)])
    out = asyncio.run(Orchestrator(ScriptedLLM(steps)).run("画垃圾焚烧流程"))
    assert out.status == "READY_WITH_WARNINGS" and out.usage["tool_calls"] < 40


def test_successful_read_only_calls_also_trip_no_progress_stall():
    steps = plan_to_steps(DEMO_PLAN)[:-1]
    steps.extend([[{"name": "inspect_drawing", "arguments": {"drawing_id": "$drawing_id"}}]]*20)
    out = asyncio.run(Orchestrator(ScriptedLLM(steps)).run("画垃圾焚烧流程"))
    assert out.status == "READY_WITH_WARNINGS" and out.usage["stalls"] == 1 and out.usage["tool_calls"] < 40


def test_model_validation_requests_never_execute_a_validation_tool_call():
    steps = plan_to_steps(DEMO_PLAN)[:-1]
    steps.extend([[{"name": "validate_drawing", "arguments": {"drawing_id": "$drawing_id"}}]]*2)
    steps.append({"say": "完成"})
    events = []
    out = asyncio.run(Orchestrator(ScriptedLLM(steps), lambda k,p: events.append((k,p))).run("画垃圾焚烧流程"))
    assert out.status == "READY"
    calls = [p for k,p in events if k == "tool_call" and p["name"] == "validate_drawing"]
    assert len(calls) == 4  # four mutations, exactly one check for each revision
    assert sum(k == "execution_control" and p.get("action") == "blocked" for k,p in events) == 2


def test_unconfirmed_numeric_parameter_immediately_asks_with_choices():
    steps = plan_to_steps(DEMO_PLAN)[:-1]
    steps.append([{"name": "add_annotation", "arguments": {"drawing_id": "$drawing_id", "anchor_ref": "V-101", "text": "容积 500 m3"}}])
    out = asyncio.run(Orchestrator(ScriptedLLM(steps)).run("画垃圾焚烧流程，标容积"))
    assert out.status == "WAITING_CONFIRMATION" and len(out.question["options"]) == 2
    assert "不是用户给出" in out.message


@pytest.mark.parametrize("sid", ["S001", "S002", "S003", "S004", "S005"])
def test_real_failure_trace_replay_ends_with_delivery_or_explicit_choices(sid, data_dir):
    case = json.loads((CASE_DIR/f"{sid}.json").read_text(encoding="utf-8"))
    assert case["source"]["type"] == "sqlite_events" and case["historical_events"]
    result = asyncio.run(run_case(case, data_dir/sid))
    assert result["passed"], result
    assert result["usage"]["tool_calls"] <= 40 and result["status"] != "FAILED"
    if sid == "S001":
        assert result["executed_tool_counts"].get("find_entities", 0) == 0
    if sid == "S002":
        assert result["usage"]["repeated_calls_blocked"] >= 1
        assert result["executed_tool_counts"].get("add_hatch", 0) < case["source"]["tool_counts"]["add_hatch"]
    if sid == "S004":
        assert result["executed_tool_counts"].get("place_equipment", 0) == 0


def test_export_is_read_only_and_preserves_exact_event_payloads(tmp_path):
    from zhihui.api.db import DB
    path = tmp_path/"source.db"
    db = DB(path)
    db.create_task("real_task", "设计单位写某学院", None)
    payload = {"name": "find_entities", "arguments": {"drawing_id": "dwg_abc", "layer": "TITLE_BLOCK_TEXT_1"}, "ok": True, "summary": "candidates × 0", "ms": 3}
    db.add_event("real_task", "tool_call", payload)
    db.set_result("real_task", "FAILED", {"message": "limit"})
    db.conn.close()
    before = sha256(path)
    paths = export_cases(path, tmp_path/"exported")
    assert len(paths) == 1 and sha256(path) == before
    case = json.loads(paths[0].read_text(encoding="utf-8"))
    event = next(e for e in case["historical_events"] if e["kind"] == "tool_call")
    assert event["payload"] == payload
    assert case["steps"][0][0]["arguments"]["drawing_id"] == "$drawing_id"
    assert "result" not in event["payload"]


def test_export_missing_database_does_not_create_one(tmp_path):
    missing = tmp_path/"missing.db"
    with pytest.raises(FileNotFoundError):
        export_cases(missing, tmp_path/"output")
    assert not missing.exists()
