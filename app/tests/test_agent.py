"""闭环：校验不过 → 发回修复；白名单；评测脚本不能空判。"""

import asyncio
import os
from pathlib import Path

from zhihui.agent.llm import DEMO_PLAN, ScriptedLLM, plan_to_steps
from zhihui.agent.orchestrator import Orchestrator

LAYOUT = [{"name": "auto_layout", "arguments": {"drawing_id": "$drawing_id"}}]


def run(steps, prompt="x", file_ref=None):
    events = []
    out = asyncio.run(Orchestrator(ScriptedLLM(steps), lambda k, p: events.append((k, p))).run(prompt, file_ref))
    return out, events


def statuses(events):
    return [p["status"] for k, p in events if k == "status"]


def test_generate_ready_with_artifacts():
    out, events = run(plan_to_steps(DEMO_PLAN))
    assert out.status == "READY"
    assert {a["format"] for a in out.artifacts} == {"dxf", "png", "pdf", "check_json", "check_md"}  # 图纸 + 规范自检报告
    s = statuses(events)
    assert s[:2] == ["PLANNING", "RUNNING"] and s[-1] == "READY"


def test_validation_failure_fails_after_two_repair_rounds():
    # 只登记设备不布局：模型说完成 → 校验 E_UNPLACED → 两轮修复都没修 → FAILED
    steps = plan_to_steps(DEMO_PLAN)
    out, events = run([steps[0], steps[1], {"say": "完成"}, {"say": "完成"}, {"say": "完成"}])
    assert out.status == "FAILED"
    assert statuses(events).count("REPAIRING") == 2


def test_repair_round_succeeds():
    steps = plan_to_steps(DEMO_PLAN)
    out, events = run([steps[0], steps[1], {"say": "完成"}, LAYOUT, {"say": "修好了"}])
    assert out.status == "READY"
    assert "REPAIRING" in statuses(events)


def test_non_whitelisted_tool_is_refused():
    out, events = run([[{"name": "os_system", "arguments": {"cmd": "whoami"}}], {"say": "好的"}])
    assert out.status == "FAILED"  # 没有产出图纸
    assert not any(k == "tool_call" for k, _ in events)


def test_bench_judge_detects_wrong_target():
    """评测脚本自检：移动了错误的设备，M 类判定必须失败。"""
    from zhihui.bench.run import load_cases, run_case

    cases = load_cases()
    case = dict(cases["M001"])
    case["steps"] = [
        [{"name": "inspect_drawing", "arguments": {"file_ref": "$file_ref"}}],
        [{"name": "move_equipment", "arguments": {"drawing_id": "$drawing_id", "equipment_ref": "V-101", "dx": 0, "dy": 800}}],
        {"say": "ok"},
    ]
    r = asyncio.run(run_case(case, cases, "scripted", Path(os.environ["ZHIHUI_DATA_DIR"])))
    assert not r["passed"]
