"""通用 CAD 的 Web 接口：新建图、场景、ops（不经过大模型）、命令行、对话任务、反问续接、渲染、语音接口状态。"""
import time

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ZHIHUI_LLM", "mock")
    monkeypatch.setenv("ZHIHUI_GENERAL_BACKEND", "local")
    from zhihui.api.app import app

    with TestClient(app) as c:
        yield c


def wait(client, tid, timeout=90):
    t0 = time.time()
    while time.time() - t0 < timeout:
        t = client.get(f"/api/tasks/{tid}").json()
        if t["status"] in ("READY", "READY_WITH_WARNINGS", "FAILED", "WAITING_CONFIRMATION", "CANCELLED"):
            return t
        time.sleep(0.2)
    raise TimeoutError(tid)


def new(client, tpl="gb_a3_mech"):
    r = client.post("/api/drawings", json={"template_name": tpl, "title": "测试图"})
    assert r.status_code == 200, r.text
    return r.json()["drawing_id"]


def test_new_scene_ops_and_undo(client):
    did = new(client)
    sc = client.get(f"/api/drawings/{did}/scene").json()
    assert sc["mode"] == "general" and sc["current_layer"] == "粗实线" and sc["entities"] == [] and [l["name"] for l in sc["layers"]][:2] == ["0", "粗实线"]
    r = client.post(f"/api/drawings/{did}/ops", json={"tool": "draw_circular_primitives", "args": {"curve_type": "circle", "method": "center_radius", "center": "50,50", "radius": 20}}).json()
    assert r["ok"] and r["created"] == ["C1"]
    sc = client.get(f"/api/drawings/{did}/scene").json()
    c = sc["entities"][0]
    assert c["k"] == "circle" and c["items"][0]["t"] == "circle" and c["items"][0]["r"] == 20 and c["bb"] == [30.0, 30.0, 70.0, 70.0]
    assert any(s[0] == "cen" and s[1:] == [50.0, 50.0] for s in c["sn"])               # 圆心捕捉点
    assert sc["session"]["undo"] == 1
    # 没变化时返回 unchanged
    assert client.get(f"/api/drawings/{did}/scene", params={"since": sc["revision"]}).json()["unchanged"] is True
    # 撤销、重做
    assert client.post(f"/api/drawings/{did}/ops", json={"tool": "history_undo_redo", "args": {"action": "undo"}}).json()["ok"]
    assert client.get(f"/api/drawings/{did}/scene").json()["entities"] == []
    assert client.post(f"/api/drawings/{did}/ops", json={"tool": "history_undo_redo", "args": {"action": "redo"}}).json()["ok"]
    assert len(client.get(f"/api/drawings/{did}/scene").json()["entities"]) == 1
    # 未知函数 / 参数不对都是结构化错误，不是 500
    assert client.post(f"/api/drawings/{did}/ops", json={"tool": "nope", "args": {}}).status_code == 400
    bad = client.post(f"/api/drawings/{did}/ops", json={"tool": "draw_circular_primitives", "args": {"curve_type": "circle", "radius": -1}}).json()
    assert bad["ok"] is False and bad["error_code"] == "INVALID_ARGS"


def test_command_line_fast_path(client):
    did = new(client)
    r = client.post(f"/api/drawings/{did}/command", json={"text": "REC 0,0 @100,60"}).json()
    assert r["handled"] and r["ok"], r
    r = client.post(f"/api/drawings/{did}/command", json={"text": "打开正交"}).json()
    assert r["ok"] and client.get(f"/api/drawings/{did}/scene").json()["draft"]["ortho"] is True
    r = client.post(f"/api/drawings/{did}/command", json={"text": "C 50,50"}).json()            # 写法不全：给出怎么写
    assert r["handled"] and not r["ok"] and "C 50,50 20" in r["hint"]
    r = client.post(f"/api/drawings/{did}/command", json={"text": "帮我设计一个法兰盘"}).json()  # 不是命令：交给智能体
    assert r["handled"] is False
    r = client.post(f"/api/drawings/{did}/command", json={"text": "help"}).json()
    assert "REC" in r["help"]
    # 口语规范化：画一个半径二十的圆，圆心在 五十逗号五十
    r = client.post(f"/api/drawings/{did}/command", json={"text": "画一个半径二十的圆，圆心在50,50"}).json()
    assert r["handled"] and r["ok"], r


def test_chat_task_runs_general_agent_and_resumes_after_question(client):
    did = new(client)
    tid = client.post(f"/api/drawings/{did}/chat", json={"prompt": "画个圆"}).json()["id"]
    t = wait(client, tid)
    assert t["status"] == "WAITING_CONFIRMATION" and "圆心" in t["result"]["question"]["question"]
    client.post(f"/api/tasks/{tid}/reply", json={"answer": "圆心在 0,0，半径 10"})
    t = wait(client, tid)
    assert t["status"] in ("READY", "READY_WITH_WARNINGS"), t["result"]
    sc = client.get(f"/api/drawings/{did}/scene").json()
    assert [e["k"] for e in sc["entities"]] == ["circle"] and sc["entities"][0]["items"][0]["r"] == 10
    assert any(a["format"] == "dxf" for a in t["result"]["artifacts"])
    kinds = {e["kind"] for e in t["events"]}
    assert {"plan", "step_start", "tool_result"} <= kinds


def test_task_without_drawing_is_routed_to_general_and_creates_one(client):
    r = client.post("/api/tasks", data={"prompt": "新建一张 A3 机械图，画一个矩形，左下角在零零，长 120 宽 80"}).json()
    assert r["mode"] == "general"
    t = wait(client, r["id"])
    assert t["status"] in ("READY", "READY_WITH_WARNINGS"), t["result"]
    did = t["result"]["drawing_id"]
    sc = client.get(f"/api/drawings/{did}/scene").json()
    assert [e["k"] for e in sc["entities"]] == ["polyline"] and sc["entities"][0]["bb"] == [0.0, 0.0, 120.0, 80.0]


def test_flow_chart_prompt_still_goes_to_pfd(client):
    r = client.post("/api/tasks", data={"prompt": "画一张工艺流程图：原料罐 V-101 → 进料泵 P-101 → 反应釜 R-101"}).json()
    assert r["mode"] == "pfd"
    wait(client, r["id"])


def test_render_list_export_and_asr_status(client):
    did = new(client)
    client.post(f"/api/drawings/{did}/ops", json={"tool": "draw_polygons_and_meshes", "args": {"shape_type": "rectangle", "corner1": "10,10", "corner2": "@80,50"}})
    png = client.get(f"/api/drawings/{did}/render", params={"fmt": "png"})
    assert png.status_code == 200 and png.content[:4] == b"\x89PNG"
    assert any(i["drawing_id"] == did for i in client.get("/api/drawings").json()["items"])
    ex = client.post(f"/api/drawings/{did}/export", json={"formats": ["dxf"]}).json()
    assert ex["files"][0]["format"] == "dxf" and client.get(ex["files"][0]["url"]).status_code == 200
    assert client.get("/api/asr/status").json()["available"] in (True, False)
    cat = client.get("/api/tools", params={"categories": "3d_modeling"}).json()["tools"]
    assert {"extrude_2d_to_3d", "cad_batch_execute"} <= {t["name"] for t in cat}


def test_route_endpoint_sends_flowchart_requests_to_the_flowchart_page_and_cad_requests_to_cad(client):
    did = new(client)
    r = client.post("/api/route", json={"prompt": "绘制乙醇水精馏工艺方案：原料罐 V-101 → 进料泵 P-101 → 精馏塔 T-101，塔底连接再沸器 E-102", "drawing_id": did}).json()
    assert r["mode"] == "pfd" and r["reason"]                                                           # 通用图上说“要流程图”：转到流程图页签
    r = client.post("/api/route", json={"prompt": "画一个矩形，左下角在零零，长120宽80", "drawing_id": did}).json()
    assert r["mode"] == "general" and "2d_drafting" in r["categories"]
    r = client.post("/api/route", json={"prompt": "把圆向右移动10"}).json()                                 # 没有图纸时也能判断
    assert r["mode"] in ("general", "pfd")
    assert client.post("/api/route", json={"prompt": "画个圆", "drawing_id": "dwg_不存在"}).status_code == 200   # 图纸不存在不报错，按话术判断


def test_flowchart_page_task_with_a_general_prompt_reports_mode_general_so_the_page_can_hand_over(client):
    r = client.post("/api/tasks", data={"prompt": "画一个矩形，左下角在零零，长120宽80，再在中心画一个直径40的圆，打开正交"}).json()
    assert r["mode"] == "general" and r["id"]                                                            # 流程图页据此跳到“通用绘图”页签接着看进度
    t = wait(client, r["id"])
    assert t["status"] in ("READY", "READY_WITH_WARNINGS") and t["result"]["drawing_id"]


def test_root_is_one_workspace_without_switching(client):
    """入口只有一个页面、一个工作空间（2026-10-21 用户要求）：不再分“化工流程图 / 化工设备图 / 通用绘图”三个页签。
    一个对话框：流程图在同页中间显示（pfdWrap），设备图有“设备参数”面板，通用绘图的工具和命令行一直都在。"""
    home = client.get("/")
    assert home.status_code == 200 and 'id="pfdWrap"' in home.text and 'data-p="vessel"' in home.text and 'id="cmdin"' in home.text and 'id="tools"' in home.text
    assert 'id="wsSeg"' not in home.text and 'data-ws="pfd"' not in home.text and 'id="pfdFrame"' not in home.text   # 没有三选一的工作空间切换
    assert "apptabs" not in home.text
    assert client.get("/cad.html").status_code == 200 and client.get("/index.html").status_code == 200       # 流程图的方案编辑器（逐项核对方案时在新标签页打开）仍可访问
    assert client.get("/shell.js").status_code == 200 and client.get("/shell.css").status_code == 200


def test_front_end_one_app_wiring():
    import pathlib
    web = pathlib.Path(__file__).resolve().parents[1] / "05_前端" / "web"
    idx, cad = (web / "index.html").read_text(encoding="utf-8"), (web / "cad.html").read_text(encoding="utf-8")
    appjs, cadjs, shell = (web / "app.js").read_text(encoding="utf-8"), (web / "cad.js").read_text(encoding="utf-8"), (web / "shell.js").read_text(encoding="utf-8")
    assert 'id="btnSettings"' in cad and "shell.js" in cad and 'id="settingsModal"' not in cad and 'id="settingsModal"' not in idx   # 设置对话框只有一份，在 shell.js
    assert "settingsBody" not in appjs and "/api/settings" in shell and "embed" in shell                       # 嵌入模式在 shell.js
    assert "pfdSend" in cadjs and '"/api/tasks"' in cadjs and "parent_id" in cadjs and "/confirm" in cadjs          # 流程图任务在同一个对话框里提交、续改、确认方案
    assert "#task=" in cadjs and "/api/route" in cadjs and 'context: context' in cadjs


def test_front_end_scripts_have_valid_syntax():
    import pathlib, shutil, subprocess
    node = shutil.which("node")
    if node is None:
        pytest.skip("没有 node，跳过前端脚本语法检查")
    web = pathlib.Path(__file__).resolve().parents[1] / "05_前端" / "web"
    for name in ("shell.js", "app.js", "cad.js"):
        p = subprocess.run([node, "--check", str(web / name)], capture_output=True, text=True)
        assert p.returncode == 0, f"{name}: {p.stderr[:400]}"
