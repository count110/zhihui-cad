"""Minimal full-stack support needed for M1 warning deliveries and resume."""
import json
import time
import pytest

@pytest.fixture(autouse=True)
def single_fault_injection(monkeypatch):
    # These M1 API tests deliberately inject a single-model tool sequence.
    monkeypatch.setenv('ZHIHUI_AGENT_MODE','single')

from fastapi.testclient import TestClient

from zhihui.agent.llm import DEMO_PLAN, ScriptedLLM, plan_to_steps


def wait(client, task_id):
    started = time.monotonic()
    while time.monotonic()-started < 30:
        task = client.get(f"/api/tasks/{task_id}").json()
        if task["status"] in {"READY", "READY_WITH_WARNINGS", "WAITING_CONFIRMATION", "FAILED"}:
            return task
        time.sleep(.02)
    raise TimeoutError(task_id)


def test_warning_status_persists_and_all_artifacts_download(monkeypatch):
    import zhihui.api.app as api
    monkeypatch.setenv("ZHIHUI_LLM", "mock")
    steps = plan_to_steps(DEMO_PLAN)[:-1]
    call = [{"name": "add_hatch", "arguments": {"drawing_id": "$drawing_id", "pick": {"x": -9999, "y": -9999}}}]
    monkeypatch.setattr(api, "make_llm", lambda *a: ScriptedLLM(steps+[call]*30))
    with TestClient(api.app) as client:
        tid = client.post("/api/tasks", data={"prompt": "画垃圾焚烧流程并填充"}).json()["id"]
        task = wait(client, tid)
        assert task["status"] == "READY_WITH_WARNINGS", task
        assert "READY_WITH_WARNINGS" in api.FINAL
        formats = {a["format"] for a in task["result"]["artifacts"]}
        assert {"dxf", "png", "pdf", "delivery_report"} <= formats
        for artifact in task["result"]["artifacts"]:
            assert client.get(artifact["url"]).status_code == 200
        assert any(e.get("result", {}).get("error_code") == "E_REPEATED_CALL" for e in task["events"])
        assert all({"agent", "stage", "budget_remaining"} <= set(e) for e in task["events"] if e["kind"] == "tool_call")


def test_program_question_closes_pending_batch_and_preserves_resume(monkeypatch):
    import zhihui.api.app as api
    monkeypatch.setenv("ZHIHUI_LLM", "real")
    steps = plan_to_steps(DEMO_PLAN)[:-1]
    steps.append([
        {"name": "move_equipment", "arguments": {"drawing_id": "$drawing_id", "equipment_ref": "P-999", "dx": 100}},
        {"name": "add_annotation", "arguments": {"drawing_id": "$drawing_id", "anchor_ref": "title", "text": "must be skipped"}},
    ])
    models = iter([ScriptedLLM(steps), ScriptedLLM([{"say": "保留当前图纸"}])])
    monkeypatch.setattr(api, "make_llm", lambda *a: next(models))
    with TestClient(api.app) as client:
        tid = client.post("/api/tasks", data={"prompt": "画垃圾焚烧流程"}).json()["id"]
        task = wait(client, tid)
        assert task["status"] == "WAITING_CONFIRMATION" and len(task["result"]["question"]["options"]) == 2
        messages = api.db.get_task(tid)["messages"]
        assert messages[-1]["tool_calls"][0]["function"]["name"] == "ask_user"
        assert any(m.get("role") == "tool" and json.loads(m["content"]).get("error_code") == "E_SKIPPED" for m in messages)
        assert client.post(f"/api/tasks/{tid}/reply", json={"answer": "保留当前图纸"}).status_code == 200
        assert wait(client, tid)["status"] == "READY"
