"""化工设备图的 Web 接口：/api/route 的设备图判断、GET /api/drawings/{id}/vessel 的形状、POST /api/tasks 与 /chat 把入口路由写进任务状态、route_hint 传给智能体。"""
import time

import pytest
from fastapi.testclient import TestClient

EQUIP = "chem_equipment"
SPEC = {"kind": "vertical_vessel", "shell": {"inner_diameter_mm": 1600, "length_mm": 2400, "thickness_mm": 8, "material": "Q345R"}, "heads": [{"kind": "EHA"}],
        "design": {"design_pressure_mpa": 0.6, "design_temp_c": 80},
        "nozzles": [{"dn": 500, "kind": "manhole", "purpose_text": "人孔"}, {"dn": 80, "purpose_text": "进料口"}, {"dn": 80, "purpose_text": "出料口"},
                    {"dn": 25, "kind": "level_gauge", "purpose_text": "液位计口", "count": 2}],
        "supports": [{"kind": "lug", "count": 4}]}
DEMO1 = "画一台立式储罐的设备图，内径1600，筒体高2400，标准椭圆封头，壁厚8，材料Q345R，设计压力0.6MPa，设计温度80℃，人孔DN500，进料口DN80，出料口DN80，两个液位计口DN25，四个耳式支座"
DONE = ("READY", "READY_WITH_WARNINGS", "FAILED", "WAITING_CONFIRMATION", "CANCELLED")


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
        if t["status"] in DONE:
            return t
        time.sleep(0.2)
    raise TimeoutError(tid)


def new(client, tpl="gb_a3_mech"):
    r = client.post("/api/drawings", json={"template_name": tpl, "title": "测试图"})
    assert r.status_code == 200, r.text
    return r.json()["drawing_id"]


def tool(did, name, **args):
    from zhihui.api import general as G
    from zhihui.tools.general.registry import run_tool
    r = run_tool(G.general_svc(), did, name, args)
    assert r.get("ok"), r
    return r


def vessel_drawing(client):
    did = new(client, "hg_equipment_a1")
    tool(did, "vessel_define", op="create", spec=SPEC)
    return did


def task_state(tid):
    from zhihui.api import app as A
    return A.db.get_task(tid)["state"]


# ---------------------------------------------------------------- /api/route
def test_route_endpoint_returns_the_equipment_fields(client):
    r = client.post("/api/route", json={"prompt": DEMO1}).json()
    assert set(r) == {"mode", "confidence", "reason", "categories", "ask_kind", "has_vessel"}
    assert r["mode"] == "equipment" and EQUIP in r["categories"] and r["ask_kind"] is False and r["has_vessel"] is False and r["reason"]
    r = client.post("/api/route", json={"prompt": "画一个立式储罐"}).json()
    assert r["mode"] == "equipment" and r["ask_kind"] is True and EQUIP in r["categories"]
    r = client.post("/api/route", json={"prompt": "画精馏工段的工艺流程图"}).json()
    assert r["mode"] == "pfd" and r["ask_kind"] is False


def test_route_endpoint_reads_has_vessel_from_the_drawing(client):
    did = vessel_drawing(client)
    r = client.post("/api/route", json={"prompt": "把进料口转到90度", "drawing_id": did}).json()
    assert r["mode"] == "equipment" and r["has_vessel"] is True and r["ask_kind"] is False and EQUIP in r["categories"]
    r = client.post("/api/route", json={"prompt": "储罐的进料管道改成DN100", "drawing_id": did}).json()
    assert r["mode"] == "equipment" and r["has_vessel"] is True                                   # 当前是设备图：管道不计流程分
    r = client.post("/api/route", json={"prompt": "画精馏工段的工艺流程图", "drawing_id": did}).json()
    assert r["mode"] == "pfd" and r["has_vessel"] is True                                         # 明确要流程图才建议新建
    r = client.post("/api/route", json={"prompt": "导出 DXF 和 PDF", "drawing_id": did}).json()
    assert r["mode"] == "equipment"


def test_route_endpoint_on_blank_equipment_sheet_and_on_a_plain_sketch(client):
    sheet = new(client, "hg_equipment_a1")                                                         # 刚新建，还没有 vessel
    r = client.post("/api/route", json={"prompt": "画一个立式储罐", "drawing_id": sheet}).json()
    assert r["mode"] == "equipment" and r["has_vessel"] is False and r["ask_kind"] is True
    sketch = new(client)
    r = client.post("/api/route", json={"prompt": "画一个矩形，左下角在零零，长120宽80", "drawing_id": sketch}).json()
    assert r["mode"] == "general" and r["has_vessel"] is False and "2d_drafting" in r["categories"]
    r = client.post("/api/route", json={"prompt": DEMO1, "drawing_id": sketch}).json()            # 草图里说设备图：返回 equipment，前端据此新建设备图而不是画进草图
    assert r["mode"] == "equipment" and r["has_vessel"] is False
    assert client.post("/api/route", json={"prompt": "画个圆", "drawing_id": "dwg_不存在"}).status_code == 200


def test_route_endpoint_normalizes_speech_errors(client):
    r = client.post("/api/route", json={"prompt": "画一台立式储罐的设备途，内径一千六，桶体高两千四，椭圆风头，壁厚八"}).json()
    assert r["mode"] == "equipment" and r["ask_kind"] is False


# ---------------------------------------------------------------- GET /api/drawings/{id}/vessel
def test_vessel_endpoint_404_and_null(client):
    assert client.get("/api/drawings/dwg_不存在/vessel").status_code == 404
    sketch = new(client)
    r = client.get(f"/api/drawings/{sketch}/vessel").json()
    assert r == {"drawing_id": sketch, "revision": r["revision"], "vessel": None}
    sheet = new(client, "hg_equipment_a1")
    assert client.get(f"/api/drawings/{sheet}/vessel").json()["vessel"] is None                    # 样板新建、还没有参数


def test_vessel_endpoint_shape_and_sources(client):
    did = vessel_drawing(client)
    r = client.get(f"/api/drawings/{did}/vessel").json()
    assert r["drawing_id"] == did and isinstance(r["revision"], int) and r["revision"] >= 2
    v = r["vessel"]
    assert set(v) == {"summary", "kind", "tag", "name", "fields", "nozzles", "supports", "pending", "warnings", "check", "record", "layouts"}
    assert v["kind"] == "vertical_vessel" and v["summary"].startswith("立式容器") and v["pending"] == []
    fields = {f["path"]: f for f in v["fields"]}
    for f in v["fields"]:
        assert set(f) == {"path", "label", "value", "source", "unverified"}
        assert f["source"] in ("user", "rule", "table", "calc", "pending", "") and isinstance(f["unverified"], bool) and isinstance(f["value"], str) and f["label"]
    # 用户说的值：来源 user，值已按 fmt_value 格式化（整数不带小数点）
    assert (fields["shell.inner_diameter_mm"]["value"], fields["shell.inner_diameter_mm"]["source"], fields["shell.inner_diameter_mm"]["unverified"]) == ("1600", "user", False)
    assert fields["shell.length_mm"]["value"] == "2400" and fields["shell.thickness_mm"]["value"] == "8" and fields["shell.material"]["value"] == "Q345R"
    assert fields["design.design_pressure_mpa"]["value"] == "0.6" and fields["design.design_temp_c"]["value"] == "80"
    assert fields["kind"]["value"] == "vertical_vessel" and fields["kind"]["source"] == "user"
    # 规则 / 查表来的值：未核对
    assert fields["heads[top].thickness_mm"]["source"] == "rule" and fields["heads[top].thickness_mm"]["unverified"] is True
    assert fields["heads[top].straight_flange_mm"]["source"] == "table" and fields["heads[top].straight_flange_mm"]["unverified"] is True
    assert fields["design.design_standard"]["source"] == "rule" and fields["design.design_standard"]["unverified"] is True
    assert {"heads[top].kind", "heads[bottom].kind", "heads[top].material", "drawing.sheet", "supports[0].kind", "supports[0].count", "supports[0].model", "supports[0].positions"} <= set(fields)
    assert fields["supports[0].count"]["value"] == "4" and fields["supports[0].count"]["source"] == "user"
    assert fields["supports[0].positions"]["value"].count("°") == 4
    # 必显但没给的：待填 + pending
    assert fields["design.medium"]["value"] == "待填" and fields["design.medium"]["source"] == "pending" and fields["design.medium"]["unverified"] is False
    # 管口
    nz = {n["mark"]: n for n in v["nozzles"]}
    assert len(v["nozzles"]) == 5 and "M1" in nz and {"a", "b1", "b2", "c"} <= set(nz)
    for n in v["nozzles"]:
        assert {"mark", "dn", "purpose", "kind", "location", "axial_mm", "radial_mm", "orientation_deg", "projection_mm", "pn", "flange_type", "facing", "std_part", "sources"} == set(n)
        assert n["sources"]["dn"] == "user" and n["sources"]["mark"] == "rule" and n["sources"]["orientation_deg"] == "rule" and set(n["sources"].values()) <= {"user", "rule", "table", "calc", ""}
    assert nz["M1"]["kind"] == "manhole" and nz["M1"]["dn"] == 500 and nz["a"]["purpose"] == "进料口" and nz["a"]["location"] == "top_head"
    assert len(v["supports"]) == 1 and v["supports"][0]["count"] == 4 and v["supports"][0]["sources"]["count"] == "user"
    # 自检、记录、布局
    assert set(v["check"]) == {"errors", "warnings"} and v["check"]["errors"] == []
    assert any(w["code"] == "W_UNVERIFIED_TABLE" for w in v["check"]["warnings"])
    assert v["record"]["den_main"] == 10 and v["record"]["den_end"] == 10 and len(v["record"]["viewports"]) == 2
    assert v["layouts"] == ["设备图"] and v["warnings"]


def test_vessel_endpoint_follows_changes_and_marks_new_user_values(client):
    did = vessel_drawing(client)
    before = client.get(f"/api/drawings/{did}/vessel").json()
    assert {n["mark"]: n for n in before["vessel"]["nozzles"]}["a"]["sources"]["orientation_deg"] == "rule"
    tool(did, "vessel_define", op="patch", set={"nozzles[purpose=进料口].orientation_deg": 90, "design.design_pressure_mpa": 1.0})
    after = client.get(f"/api/drawings/{did}/vessel").json()
    assert after["revision"] == before["revision"] + 1
    a = {n["mark"]: n for n in after["vessel"]["nozzles"]}["a"]
    assert a["orientation_deg"] == 90 and a["sources"]["orientation_deg"] == "user"
    f = {x["path"]: x for x in after["vessel"]["fields"]}["design.design_pressure_mpa"]
    assert (f["value"], f["source"]) == ("1", "user")                                              # 1.0 → fmt_value → "1"
    assert all(n["pn"] is not None for n in after["vessel"]["nozzles"])                            # 默认 PN 随设计压力重取档


def test_vessel_endpoint_does_not_break_when_the_check_crashes(client, monkeypatch):
    did = vessel_drawing(client)
    from zhihui.cad.equipment import check as CHK

    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(CHK, "run", boom)
    r = client.get(f"/api/drawings/{did}/vessel")
    assert r.status_code == 200
    assert r.json()["vessel"]["check"]["errors"][0]["code"] == "E_CHECK_CRASH"


# ---------------------------------------------------------------- POST /api/tasks、/chat：入口路由写进任务状态
def test_create_task_stores_route_and_categories_and_passes_route_hint(client, monkeypatch):
    from zhihui.agent.general import engine as E
    seen = []
    orig = E.GeneralAgent.__init__

    def spy(self, *a, **kw):
        seen.append(kw.get("route_hint"))
        orig(self, *a, **kw)
    monkeypatch.setattr(E.GeneralAgent, "__init__", spy)
    r = client.post("/api/tasks", data={"prompt": DEMO1}).json()
    assert r["mode"] == "general" and r["route"] == "equipment" and r["drawing_id"] is None      # 设备图口令也由通用智能体处理
    st = task_state(r["id"])
    assert st["general"] is True and st["route"] == "equipment" and EQUIP in st["categories"] and st["drawing_id"] is None
    t = wait(client, r["id"])
    assert seen == ["equipment"]
    assert t["status"] in DONE and t["status"] != "CANCELLED"
    # 没有图纸：新建的是化工设备图样板，不是空白通用图
    did = t["result"]["drawing_id"]
    assert did
    from zhihui.api import general as G
    d = G.general_svc().store.load(did)
    assert d.workspace == EQUIP and d.title == "化工设备图"
    assert task_state(r["id"])["drawing_id"] == did and task_state(r["id"])["route"] == "equipment"      # 反问回答接着用这张图


def test_create_task_ask_kind_prompt_is_also_equipment(client):
    r = client.post("/api/tasks", data={"prompt": "画一个立式储罐"}).json()
    assert r["route"] == "equipment" and task_state(r["id"])["route"] == "equipment"
    assert wait(client, r["id"])["status"] in DONE


def test_create_task_flowchart_prompt_is_not_a_general_task(client):
    r = client.post("/api/tasks", data={"prompt": "画一张工艺流程图：原料罐 V-101 → 进料泵 P-101 → 反应釜 R-101"}).json()
    assert r["mode"] == "pfd" and "route" not in r
    assert not (task_state(r["id"]) or {}).get("general")
    wait(client, r["id"])
    r = client.post("/api/tasks", data={"prompt": "画一个矩形，左下角在零零，长120宽80"}).json()
    assert r["mode"] == "general" and r["route"] == "general" and task_state(r["id"])["route"] == "general"
    assert "2d_drafting" in task_state(r["id"])["categories"]
    wait(client, r["id"])


def test_create_task_with_a_drawing_id_routes_by_that_drawing(client):
    did = vessel_drawing(client)
    r = client.post("/api/tasks", data={"prompt": "把进料口转到90度", "drawing_id": did}).json()
    assert r["route"] == "equipment" and r["drawing_id"] == did
    st = task_state(r["id"])
    assert st["route"] == "equipment" and st["drawing_id"] == did and st["categories"][0] == EQUIP
    wait(client, r["id"])
    r = client.post("/api/tasks", data={"prompt": "画精馏工段的工艺流程图", "drawing_id": did}).json()
    assert task_state(r["id"])["route"] == "pfd" and task_state(r["id"])["drawing_id"] == did   # 路由结果如实记下（任务仍绑定这张图）
    wait(client, r["id"])
    sketch = new(client)
    r = client.post("/api/tasks", data={"prompt": "画一个圆，圆心在原点，半径10", "drawing_id": sketch}).json()
    assert task_state(r["id"])["route"] == "general" and task_state(r["id"])["drawing_id"] == sketch
    wait(client, r["id"])


def test_equipment_prompt_never_lands_in_the_users_sketch(client):
    sketch = new(client)
    r = client.post("/api/tasks", data={"prompt": DEMO1, "drawing_id": sketch}).json()
    assert r["route"] == "equipment" and r["drawing_id"] is None and task_state(r["id"])["drawing_id"] is None     # 草图 id 被放下，引擎新建设备图样板
    t = wait(client, r["id"])
    assert t["result"]["drawing_id"] and t["result"]["drawing_id"] != sketch
    from zhihui.api import general as G
    assert G.general_svc().store.load(sketch).entities == []


def test_chat_stores_the_route_too(client):
    did = vessel_drawing(client)
    tid = client.post(f"/api/drawings/{did}/chat", json={"prompt": "把进料口转到90度"}).json()["id"]
    st = task_state(tid)
    assert st["general"] is True and st["drawing_id"] == did and st["route"] == "equipment" and st["categories"][0] == EQUIP
    wait(client, tid)
    sketch = new(client)
    tid = client.post(f"/api/drawings/{sketch}/chat", json={"prompt": "画一个圆，圆心在原点，半径10"}).json()["id"]
    assert task_state(tid)["route"] == "general" and task_state(tid)["drawing_id"] == sketch
    wait(client, tid)
    tid = client.post(f"/api/drawings/{sketch}/chat", json={"prompt": "画一个立式储罐"}).json()["id"]
    assert task_state(tid)["route"] == "equipment" and task_state(tid)["drawing_id"] is None
    wait(client, tid)


def test_follow_up_task_keeps_following_the_parent_general_task(client):
    from zhihui.api import app as A
    did = vessel_drawing(client)
    A.db.create_task("t_parent", "把进料口转到90度", None, None)                                      # 上一个任务：通用任务，结果里有 DXF
    A.db.set_state("t_parent", {"general": True, "drawing_id": did, "route": "equipment", "categories": [EQUIP]})
    A.db.set_result("t_parent", "READY", {"drawing_id": did, "artifacts": [{"format": "dxf", "file_ref": "exports/x.dxf"}]})
    r = client.post("/api/tasks", data={"prompt": "导出 DXF 和 PDF", "parent_id": "t_parent"}).json()
    assert r["mode"] == "general" and r["drawing_id"] == did and r["route"] == "equipment"
    assert task_state(r["id"])["route"] == "equipment" and task_state(r["id"])["drawing_id"] == did
    wait(client, r["id"])
