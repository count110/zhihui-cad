"""化工设备图基准（E 类，任务书 10.2–10.4）：用例集本身的完整性、断言 DSL（每种一个正例一个反例）、运行器的判定、scripted 全部通过、mock 只判路由其余如实 SKIP。

scripted 的 12 条要走完整的出图和导出，每条约 8 秒；其余测试都很快。真实模型模式（--llm real）需要 API Key，这里不跑。
"""
import asyncio
import copy
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import zhihui.tools.general  # noqa: F401
from zhihui.agent.general.guards import check_vessel_patch
from zhihui.agent.general.plan import validate_plan
from zhihui.agent.speech_normalize import normalize
from zhihui.bench import general_cases as gc
from zhihui.bench.general_cases import EQUIP_KINDS, check, expected_calls, run_vessel_case, steps_to_plan
from zhihui.bench.run import load_cases, run_case
from zhihui.tools.core import ToolService
from zhihui.tools.general.registry import REGISTRY, run_tool
from zhihui.tools.store import DrawingStore

CASES = {k: v for k, v in load_cases().items() if v["category"] == "E"}
IDS = [f"E{n:03d}" for n in range(1, 13)]
ROUTE_ONLY = [i for i in IDS if CASES[i]["expect_route"] != "equipment"]
KNOWN_KINDS = EQUIP_KINDS | {"revisions_added", "artifact", "layout", "text"}


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ 用例集本身
def test_case_ids_and_fields():
    assert sorted(CASES) == IDS
    assert ROUTE_ONLY == ["E011"]                                       # P0 里只有 E011 是纯路由用例（期望 pfd）
    assert {CASES[i]["expect_route"] for i in IDS} == {"equipment", "pfd"}


@pytest.mark.parametrize("cid", IDS)
def test_case_is_well_formed(cid):
    c = CASES[cid]
    assert c["prompt"] and c["prompt_asr"] and c["prompt_asr"] != c["prompt"] and c["assert"] and c["intents"] is not None and c["note"]
    assert c["mock_ok"] is False                                        # P0：规则 Planner 没有设备图规则，如实标 false
    assert c["setup"]["template"] == "hg_equipment_a1"                  # 每条 E 用例都必须有 setup（否则自动新建图纸会被算成一个新版本）
    assert "steps" in c
    for a in c["assert"]:
        ((kind, _),) = [(k, v) for k, v in a.items() if k not in ("scripted_only", "note")]
        assert kind in KNOWN_KINDS, f"{cid}: 不认识的断言 {kind}"
    for call in c["setup"].get("calls", []):
        assert call["tool"] == "vessel_define" and call["args"]["op"] == "create"      # 预置用确定性的 vessel_define
    if cid in ROUTE_ONLY:
        assert c["steps"] == [] and c["intents"] == []
        return
    assert c["steps"] and c["intents"] == ["chem_equipment"]
    plan = steps_to_plan(c, {})
    assert validate_plan(plan) == [], validate_plan(plan)
    for t in expected_calls(c):
        assert t in REGISTRY and REGISTRY[t].category == "chem_equipment"
    for f in c["functions"]:
        assert f in REGISTRY
    if any(st["args"].get("op") == "patch" or "views" in st["args"] or "export" in st["args"] for st in c["steps"]):
        assert any(call["tool"] == "vessel_define" for call in c["setup"].get("calls", [])), f"{cid}: 改图 / 出图类用例必须用 setup.calls 预置参数"
    if c.get("expect_clarify"):
        assert c["expect_clarify"]["paths"] and not any(k == "revisions_added" and v != 0 for a in c["assert"] for k, v in a.items())


def test_cases_cover_the_p0_demo_prompts_and_both_functions():
    fns = {t for i in IDS for t in expected_calls(CASES[i])}
    assert fns == {"vessel_define", "vessel_generate_drawing"}
    assert CASES["E001"]["prompt"].startswith("画一台立式储罐的设备图，内径1600，筒体高2400") and CASES["E003"]["prompt"] == "画一个立式储罐"
    assert CASES["E011"]["prompt"] == "画精馏工段的工艺流程图" and CASES["E011"]["expect_route"] == "pfd"
    kinds = {k for i in IDS for a in CASES[i]["assert"] for k in a}
    assert EQUIP_KINDS <= kinds, f"没有用例用到这些断言：{sorted(EQUIP_KINDS - kinds)}"


def test_asr_phrasings_include_pressure_units():
    """任务书 10.4-5：语音写法里要有千帕 / kPa（kW、kg 在 P0 的参数里没有对应字段，没有用例用到）。"""
    hits = [i for i in IDS if any(u in CASES[i]["prompt_asr"] for u in ("千帕", "kPa", "kpa"))]
    assert len(hits) >= 2, hits


@pytest.mark.parametrize("cid", [i for i in IDS if i not in ROUTE_ONLY])
def test_scripted_steps_pass_the_vessel_patch_guard_for_both_phrasings(cid):
    """步骤里的数值在口令里都有出处：口令和语音误识别写法（规范化之后）都不被设备补丁护栏拦下或剔除。"""
    c = CASES[cid]
    for text in (c["prompt"], c["prompt_asr"]):
        plan = steps_to_plan(c, {})
        before = json.dumps([s.payload for s in plan.workflow_steps], ensure_ascii=False, sort_keys=True)
        out = check_vessel_patch(plan, normalize(text).text, text)
        assert not out["block"] and not out["warn"] and not out["clarify"], (text, out)
        assert json.dumps([s.payload for s in plan.workflow_steps], ensure_ascii=False, sort_keys=True) == before        # 没有字段被剔除


# ------------------------------------------------------------------ scripted：12 条全部通过
@pytest.mark.parametrize("cid", IDS)
def test_scripted_mode_passes(cid, tmp_path):
    r = run(run_vessel_case(CASES[cid], "scripted", tmp_path))
    assert r["passed"], f"{cid}: {r['reason']}"
    if cid in ROUTE_ONLY:
        assert r["status"] == "ROUTED"
    elif cid in ("E003", "E012"):
        assert r["status"] == "WAITING_CONFIRMATION"
    else:
        assert r["status"] in ("READY", "READY_WITH_WARNINGS")


# ------------------------------------------------------------------ 断言 DSL：每种一个正例一个反例
@pytest.fixture(scope="module")
def e1(tmp_path_factory):
    svc = ToolService(DrawingStore(tmp_path_factory.mktemp("e1")))
    did = run_tool(svc, None, "manage_document_file", {"action": "new", "template_name": "hg_equipment_a1"})["drawing_id"]
    base = svc.store.load(did).revision
    r = run_tool(svc, did, "vessel_define", {**CASES["E001"]["steps"][0]["args"], "turn_id": "T1"})
    assert r["ok"], r
    return SimpleNamespace(svc=svc, did=did, base=base, d=svc.store.load(did), agent=SimpleNamespace(turn_id="T1", results={}), prompt=CASES["E001"]["prompt"])


def ck(e1, a, d=None, out=None, arts=None, agent=None, route=None, prompt=None, base=None):
    return check(a, e1.d if d is None else d, out, agent or e1.agent, e1.base if base is None else base, arts or [], "scripted",
                 {"prompt": prompt or e1.prompt, "route": route or {"mode": "equipment"}, "svc": e1.svc})


def mutated(e1):
    return e1.d.model_copy(deep=True)


def test_dsl_equip_spec(e1):
    assert ck(e1, {"equip_spec": {"shell.inner_diameter_mm": 1600, "shell.length": 2400, "design.design_pressure_mpa": 0.6, "design.design_temp_c": 80.0,
                                   "heads[top].kind": "EHA", "nozzles[purpose=进料口].dn": 80, "nozzles[M1].dn": 500, "supports[0].count": 4,
                                   "supports[0].positions.orientation_deg": [45, 135, 225, 315]}})[0]
    ok, why = ck(e1, {"equip_spec": {"shell.inner_diameter_mm": 1500}})
    assert not ok and "shell.inner_diameter_mm" in why and "1600" in why
    assert not ck(e1, {"equip_spec": {"supports[0].positions.orientation_deg": [0, 90, 180, 270]}})[0]
    ok, why = ck(e1, {"equip_spec": {"nozzles[purpose=液位计口].dn": 25}})            # 选择器不唯一：失败并写清原因，不是异常
    assert not ok and "液位计口" in why
    assert not ck(e1, {"equip_spec": {"nozzles[purpose=不存在的用途].dn": 25}})[0]


def test_dsl_equip_sources(e1):
    assert ck(e1, {"equip_sources": {"shell.inner_diameter_mm": "user", "heads[top].straight_flange_mm": "table", "nozzles[purpose=进料口].pn": "rule", "supports[0].count": "user",
                                      "nozzles[b1].mark": "rule", "kind": "user"}})[0]
    ok, why = ck(e1, {"equip_sources": {"shell.inner_diameter_mm": "rule"}})
    assert not ok and "user:T1" in why
    assert not ck(e1, {"equip_sources": {"nozzles[purpose=进料口].pn": "user"}})[0]
    assert not ck(e1, {"equip_sources": {"nozzles[purpose=进料口].no_such_field": "rule"}})[0]


def test_dsl_equip_sources_calc_for_tangent_length(tmp_path):
    svc = ToolService(DrawingStore(tmp_path))
    did = run_tool(svc, None, "manage_document_file", {"action": "new", "template_name": "hg_equipment_a1"})["drawing_id"]
    assert run_tool(svc, did, "vessel_define", {**CASES["E002"]["steps"][0]["args"], "turn_id": "T2"})["ok"]
    d = svc.store.load(did)
    ag = SimpleNamespace(turn_id="T2", results={})
    assert check({"equip_sources": {"shell.length_mm": "calc", "shell.tangent_length_mm": "user"}}, d, None, ag, 0, [], "scripted", {})[0]
    assert not check({"equip_sources": {"shell.length_mm": "user"}}, d, None, ag, 0, [], "scripted", {})[0]


def test_dsl_equip_values_from_user(e1):
    assert ck(e1, {"equip_values_from_user": True})[0]
    ok, why = ck(e1, {"equip_values_from_user": True}, prompt=e1.prompt.replace("内径1600", "内径1700"))      # 口令里没有 1600 → 来源标成用户却对不上
    assert not ok and "1600" in why
    ok, why = ck(e1, {"equip_values_from_user": True}, prompt=e1.prompt.replace("设计压力0.6MPa", "设计压力0.3MPa"))
    assert not ok and "0.6" in why
    ok, why = ck(e1, {"equip_values_from_user": True}, agent=SimpleNamespace(turn_id="另一轮", results={}))      # 本轮没有用户数值：空转的检查不放行
    assert not ok and "空转" in why
    assert not ck(e1, {"equip_values_from_user": True}, agent=SimpleNamespace(results={}))[0]


def test_dsl_equip_nozzles(e1):
    assert ck(e1, {"equip_nozzles": {"count": 5, "marks": ["c", "b2", "b1", "a", "M1"], "dn": {"M1": 500, "a": 80, "b1": 25}, "location": {"a": "top_head", "c": "bottom_head", "M1": "shell"},
                                      "purpose": {"a": "进料口"}}})[0]
    assert not ck(e1, {"equip_nozzles": {"count": 6}})[0]
    ok, why = ck(e1, {"equip_nozzles": {"marks": ["a", "b", "c"]}})
    assert not ok and "管口符号" in why
    assert not ck(e1, {"equip_nozzles": {"dn": {"a": 100}}})[0]
    assert not ck(e1, {"equip_nozzles": {"dn": {"zz": 100}}})[0]
    assert not ck(e1, {"equip_nozzles": {"location": {"a": "bottom_head"}}})[0]


def test_dsl_equip_views(e1):
    assert ck(e1, {"equip_views": ["O", "M"]})[0]
    ok, why = ck(e1, {"equip_views": ["M", "L"]})
    assert not ok and "['M', 'O']" in why
    assert not ck(e1, {"equip_views": {"layout": "管口方位图", "views": ["O"]}})[0]               # 没有这张布局
    d2 = mutated(e1)
    d2.vessel.drawing["record"]["viewports"][1]["view"] = "L"                                    # 自检记录和布局对不上
    assert not ck(e1, {"equip_views": ["M", "O"]}, d=d2)[0]


def test_dsl_equip_table(e1):
    assert ck(e1, {"equip_table": ["设计数据表", "管口表", "明细栏", "技术要求"]})[0]
    assert not ck(e1, {"equip_table": ["不存在的表"]})[0]
    d2 = mutated(e1)
    d2.vessel.drawing["record"]["tables"] = [t for t in d2.vessel.drawing["record"]["tables"] if t["name"] != "管口表"]
    ok, why = ck(e1, {"equip_table": ["管口表"]}, d=d2)
    assert not ok and "管口表" in why
    d3 = mutated(e1)
    d3.entities = [e for e in d3.entities if "equip_part:TECH" not in e.tags]                     # 记录里有、图纸空间里却读不到文字
    ok, why = ck(e1, {"equip_table": ["技术要求"]}, d=d3)
    assert not ok and "技术要求" in why
    d4 = mutated(e1)
    d4.vessel.drawing["record"]["title_block"] = None
    ok, why = ck(e1, {"equip_table": ["管口表"]}, d=d4)
    assert not ok and "标题栏" in why


def test_dsl_equip_check(e1):
    assert ck(e1, {"equip_check": {"errors": 0}})[0]
    d2 = mutated(e1)
    d2.vessel.nozzles[0].mark = d2.vessel.nozzles[1].mark                                         # 管口符号重复 → E_MARK_DUP
    ok, why = ck(e1, {"equip_check": {"errors": 0}}, d=d2)
    assert not ok and "E_MARK_DUP" in why
    from zhihui.cad.equipment import check as CHK
    n = len(CHK.run(d2.vessel, d2)["errors"])
    assert n >= 1 and ck(e1, {"equip_check": {"errors": n}}, d=d2)[0]                           # 写成实际的错误数就通过：数的是错误个数，不是“有没有错误”
    d3 = mutated(e1)
    d3.vessel.shell.inner_diameter_mm = None                                                      # 必问字段没填 → E_SPEC_PENDING
    ok, why = ck(e1, {"equip_check": {"errors": 0}}, d=d3)
    assert not ok and "E_SPEC_PENDING" in why


def test_dsl_equip_dim(e1):
    assert ck(e1, {"equip_dim": {"overall": 3266}})[0]
    ok, why = ck(e1, {"equip_dim": {"overall": 3000}})
    assert not ok and "3266" in why
    d2 = mutated(e1)
    d2.vessel.shell.length_mm = 2500                                                              # 参数变了、图没跟着变：按参数值算的总高对不上
    ok, why = ck(e1, {"equip_dim": {"overall": 3266}}, d=d2)
    assert not ok and "按参数值算" in why
    d3 = mutated(e1)
    d3.vessel.drawing["record"]["dim"]["chain"][0] += 10                                          # 尺寸链不闭合
    ok, why = ck(e1, {"equip_dim": {"overall": 3266}}, d=d3)
    assert not ok and "尺寸链" in why


def test_dsl_clarify_covers(e1):
    out = SimpleNamespace(status="WAITING_CONFIRMATION", question={"items": [{"path": "shell.length_mm"}, {"path": "nozzles[n1].dn"}, {"path": "supports.kind"}, {"path": "drawing_type"}]})
    assert ck(e1, {"clarify_covers": {"paths": ["shell.length", "nozzles.dn", "supports.kind", "drawing_type"]}}, out=out, base=e1.d.revision)[0]
    assert check({"clarify_covers": {"paths": ["shell.tangent_length_mm"]}}, None, out, None, 0, [], "scripted", {})[0]       # 无图纸也行；tangent_length_mm 和 length_mm 都算 shell.length
    ok, why = ck(e1, {"clarify_covers": {"paths": ["shell.thickness_mm"]}}, out=out, base=e1.d.revision)
    assert not ok and "shell.thickness_mm" in why
    ok, why = ck(e1, {"clarify_covers": {"paths": ["shell.length"]}}, out=SimpleNamespace(status="READY", question=None), base=e1.d.revision)
    assert not ok and "WAITING_CONFIRMATION" in why
    ok, why = ck(e1, {"clarify_covers": {"paths": ["shell.length"]}}, out=SimpleNamespace(status="WAITING_CONFIRMATION", question={"question": "没看懂"}), base=e1.d.revision)
    assert not ok and "待问项" in why                                                             # 只有一句话、没有结构化 items 不算覆盖
    ok, why = ck(e1, {"clarify_covers": {"paths": ["shell.length"]}}, out=out, base=e1.d.revision - 1)           # 反问的同时落了版本
    assert not ok and "版本" in why


def test_dsl_route_mode_and_no_vessel(e1, tmp_path):
    assert ck(e1, {"route_mode": "equipment"})[0] and ck(e1, {"route_mode": "pfd"}, route={"mode": "pfd"})[0]
    ok, why = ck(e1, {"route_mode": "pfd"})
    assert not ok and "equipment" in why
    assert not ck(e1, {"route_mode": "general"}, route={"mode": None})[0]
    svc = ToolService(DrawingStore(tmp_path))
    did = run_tool(svc, None, "manage_document_file", {"action": "new", "template_name": "hg_equipment_a1"})["drawing_id"]
    empty = svc.store.load(did)
    assert check({"no_vessel": True}, empty, None, None, 0, [], "scripted", {})[0] and check({"no_vessel": True}, None, None, None, 0, [], "scripted", {})[0]
    assert not check({"no_vessel": True}, e1.d, None, None, 0, [], "scripted", {})[0]
    assert check({"no_vessel": False}, e1.d, None, None, 0, [], "scripted", {})[0]
    assert not check({"equip_spec": {"kind": "vertical_vessel"}}, empty, None, None, 0, [], "scripted", {})[0]       # 没有设备参数的图上，设备断言都失败
    assert not check({"equip_check": {"errors": 0}}, None, None, None, 0, [], "scripted", {})[0]


def test_dsl_artifact_source_without_files(e1):
    arts = [{"format": "pdf", "source": "layout:设备图"}, {"format": "dxf", "source": "model+layouts"}]
    assert ck(e1, {"artifact_source": {"format": "pdf", "source": "layout:设备图"}}, arts=arts)[0]
    ok, why = ck(e1, {"artifact_source": {"format": "pdf", "source": "layout:设备图"}}, arts=[{"format": "pdf", "source": "model"}])      # PDF 不是从布局渲染的
    assert not ok and "model" in why
    ok, why = ck(e1, {"artifact_source": {"format": "pdf", "source": "layout:设备图"}}, arts=[{"format": "dxf", "source": "model+layouts"}])
    assert not ok and "pdf" in why
    assert ck(e1, {"artifact_source": {"format": "dxf"}}, arts=arts)[0]                          # 不写 source 就只查有这种文件


def test_dsl_artifact_source_reads_the_exported_files_back(tmp_path):
    svc = ToolService(DrawingStore(tmp_path))
    did = run_tool(svc, None, "manage_document_file", {"action": "new", "template_name": "hg_equipment_a1"})["drawing_id"]
    assert run_tool(svc, did, "vessel_define", {**CASES["E001"]["steps"][0]["args"], "turn_id": "T1"})["ok"]
    r = run_tool(svc, did, "vessel_generate_drawing", {"export": ["dxf", "pdf"]})
    assert r["ok"], r
    files = r["data"]["files"]
    extra = {"svc": svc}

    def chk(spec):
        return check({"artifact_source": spec}, svc.store.load(did), None, None, 0, files, "scripted", extra)
    assert chk({"format": "pdf", "source": "layout:设备图"})[0]
    assert chk({"format": "dxf", "source": "model+layouts", "layouts_has": ["设备图"], "texts_has": ["管口表", "设计数据表", "技术要求", "方案图"]})[0]
    ok, why = chk({"format": "dxf", "layouts_has": ["设备图", "没有这张布局"]})
    assert not ok and "没有这张布局" in why
    ok, why = chk({"format": "dxf", "texts_has": ["图上根本没有的文字"]})
    assert not ok and "图上根本没有的文字" in why
    pdf = next(f for f in files if f["format"] == "pdf")
    (tmp_path / pdf["file_ref"]).write_bytes(b"hello")                                           # 文件头不是 PDF
    ok, why = chk({"format": "pdf", "source": "layout:设备图"})
    assert not ok and "PDF" in why
    (tmp_path / pdf["file_ref"]).write_bytes(b"")
    ok, why = chk({"format": "pdf", "source": "layout:设备图"})
    assert not ok and "空" in why


def test_old_assertions_still_work_without_a_drawing():
    assert check({"revisions_added": 0}, None, None, None, 0, [])[0]
    ok, why = check({"count": {"kind": "line", "n": 0}}, None, None, None, 0, [])
    assert not ok and "没有图纸" in why
    assert check({"artifact": "pdf"}, None, None, None, 0, [{"format": "pdf"}])[0]


def test_unknown_equip_assertion_is_a_failure_not_a_pass(e1):
    ok, why = gc.check_equip("equip_nonexistent", {}, e1.d, None, e1.agent, e1.base, [], {})
    assert not ok and "未知" in why
    ok, why = check({"no_such_kind": 1}, e1.d, None, e1.agent, e1.base, [])
    assert not ok and "未知" in why


# ------------------------------------------------------------------ 运行器的判定
def variant(cid, **over):
    c = copy.deepcopy(CASES[cid])
    c.update(over)
    return c


def test_route_mismatch_fails_before_the_agent_runs(tmp_path):
    r = run(run_vessel_case(variant("E001", expect_route="pfd"), "scripted", tmp_path))              # 口令明明是设备图，用例却期望 pfd
    assert not r["passed"] and r["status"] == "ROUTE_MISMATCH" and "equipment" in r["reason"]
    r = run(run_vessel_case(variant("E011", expect_route="equipment"), "scripted", tmp_path))         # 流程图口令被期望成设备图：路由是 pfd → 失败
    assert not r["passed"] and r["status"] == "ROUTE_MISMATCH"
    r = run(run_vessel_case(variant("E003", expect_ask_kind=False), "scripted", tmp_path))
    assert not r["passed"] and "ask_kind" in r["reason"]


def test_route_is_checked_with_and_without_a_drawing(monkeypatch, tmp_path):
    seen = []
    real = gc.entry_route

    def spy(prompt, drawing=None):
        seen.append(None if drawing is None else getattr(drawing, "workspace", "?"))
        return real(prompt, drawing)
    monkeypatch.setattr(gc, "entry_route", spy)
    assert run(run_vessel_case(CASES["E011"], "scripted", tmp_path))["passed"]
    assert seen == [None, "chem_equipment"]                                                          # route_drawing=both：无图和设备图里各查一次
    seen.clear()
    assert run(run_vessel_case(CASES["E003"], "scripted", tmp_path))["passed"] and seen == [None]    # 新建场景不传图
    seen.clear()
    assert run(run_vessel_case(CASES["E012"], "scripted", tmp_path))["passed"] and seen == ["chem_equipment"]      # 改图场景传设备图


def test_the_agent_gets_the_route_as_route_hint(monkeypatch, tmp_path):
    seen = {}

    class Spy(gc.GeneralAgent):
        def __init__(self, *a, **kw):
            seen.update(kw)
            super().__init__(*a, **kw)
    monkeypatch.setattr(gc, "GeneralAgent", Spy)
    assert run(run_vessel_case(CASES["E003"], "scripted", tmp_path))["passed"]
    assert seen["route_hint"] == "equipment" and seen["drawing_id"]


def test_expect_error_must_really_appear(tmp_path):
    r = run(run_vessel_case(variant("E012", expect_error="SPEC_INCOMPLETE"), "scripted", tmp_path))
    assert not r["passed"] and "没有出现期望的错误码" in r["reason"]
    r = run(run_vessel_case(variant("E012", expect_error="STD_SIZE_NOT_FOUND"), "scripted", tmp_path))
    assert r["passed"] and r["status"] == "WAITING_CONFIRMATION"


def test_waiting_status_is_a_failure_unless_the_case_expects_it(tmp_path):
    c = variant("E012")
    del c["expect_error"]                                                                            # 同样的反问，用例没有声明期望 → 状态不对
    r = run(run_vessel_case(c, "scripted", tmp_path))
    assert not r["passed"] and "WAITING_CONFIRMATION" in r["reason"]


def test_clarify_must_cover_the_expected_paths(tmp_path):
    r = run(run_vessel_case(variant("E003", expect_clarify={"paths": ["shell.inner_diameter_mm", "design.medium"]}, **{"assert": []}), "scripted", tmp_path))
    assert not r["passed"] and "design.medium" in r["reason"]
    r = run(run_vessel_case(variant("E003", **{"assert": []}), "scripted", tmp_path))                # 没写 clarify_covers 断言时，expect_clarify 自动补上
    assert r["passed"]


def test_a_wrong_expected_value_fails_the_case(tmp_path):
    c = variant("E012", **{"assert": [{"equip_nozzles": {"count": 6}}]})
    r = run(run_vessel_case(c, "scripted", tmp_path))
    assert not r["passed"] and "断言失败" in r["reason"] and "equip_nozzles" in r["reason"]
    c = variant("E004")
    c["assert"] = [{"equip_spec": {"nozzles[purpose=进料口].orientation_deg": 180}}]
    r = run(run_vessel_case(c, "scripted", tmp_path))
    assert not r["passed"] and "orientation_deg" in r["reason"]


def test_setup_failure_is_reported_not_raised(tmp_path):
    c = variant("E004")
    c["setup"] = {"template": "hg_equipment_a1", "calls": [{"tool": "vessel_define", "args": {"op": "patch", "set": {"shell.length_mm": 1}}}]}      # 还没有设备参数，patch 会失败
    r = run(run_vessel_case(c, "scripted", tmp_path))
    assert not r["passed"] and r["status"] == "ERROR" and "预置失败" in r["reason"]


def test_a_step_that_changes_nothing_is_caught_by_revisions_added(tmp_path):
    c = variant("E012", expect_error="STD_SIZE_NOT_FOUND")
    c["assert"] = [{"revisions_added": 1}]                                                           # 反问时不该落版本；期望 1 个版本 → 失败
    r = run(run_vessel_case(c, "scripted", tmp_path))
    assert not r["passed"] and "新增版本 0" in r["reason"]


# ------------------------------------------------------------------ mock：只判路由，其余如实 SKIP
def test_mock_judges_route_only_cases_fully(tmp_path):
    r = run(run_vessel_case(CASES["E011"], "mock", tmp_path))
    assert r["passed"] and not r.get("skipped") and r["status"] == "ROUTED"


@pytest.mark.parametrize("cid", [i for i in IDS if i not in ROUTE_ONLY])
def test_mock_skips_the_rest_after_checking_both_routes(cid, tmp_path):
    r = run(run_vessel_case(CASES[cid], "mock", tmp_path))
    assert r["passed"] and r["skipped"] and r["status"] == "SKIPPED" and "SKIP" in r["reason"] and "路由" in r["reason"]


def test_mock_fails_when_the_route_is_wrong_instead_of_skipping(tmp_path):
    c = variant("E001", prompt="画精馏工段的工艺流程图", prompt_asr="画精馏工段的工艺流程图")
    r = run(run_vessel_case(c, "mock", tmp_path))
    assert not r["passed"] and not r.get("skipped") and r["status"] == "ROUTE_MISMATCH"
    c = variant("E001", prompt_asr="画精馏工段的工艺流程图")                                            # 只有语音写法的路由错了：也要失败
    r = run(run_vessel_case(c, "mock", tmp_path))
    assert not r["passed"] and "语音误识别" in r["reason"]


def test_mock_runs_the_agent_when_a_case_is_marked_mock_ok(tmp_path):
    """mock_ok=true 的 E 用例会真的走 GeneralAgent.run（规则 Planner 没有设备图规则，所以这里应当如实失败，而不是崩溃或冒充通过）。"""
    r = run(run_vessel_case(variant("E012", mock_ok=True), "mock", tmp_path))
    assert not r["passed"] and not r.get("skipped") and r["status"] in ("WAITING_CONFIRMATION", "FAILED") and r["reason"]
    assert (r.get("usage") or {}).get("prompt_tokens", 0) == 0                                      # 全程没有调用大模型


# ------------------------------------------------------------------ run.py 接入
def test_run_case_dispatches_category_e(tmp_path):
    cases = load_cases()
    r = run(run_case(cases["E003"], cases, "scripted", tmp_path))
    assert r["passed"] and r["id"] == "E003" and r["category"] == "E" and r["status"] == "WAITING_CONFIRMATION"
    r = run(run_case(cases["E001"], cases, "mock", tmp_path))
    assert r["passed"] and r["skipped"]


def test_bench_cli_runs_category_e_in_scripted_and_mock_modes(tmp_path):
    env = {**os.environ, "ZHIHUI_LLM": "mock", "PYTHONUTF8": "1"}
    cwd = str(Path(__file__).resolve().parents[1])
    out = tmp_path / "scripted.json"
    p = subprocess.run([sys.executable, "-m", "zhihui.bench.run", "--llm", "scripted", "--only", "E003,E011,E012", "--output", str(out)], cwd=cwd, capture_output=True, text=True, env=env, timeout=600)
    assert p.returncode == 0, p.stdout + p.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["summary"]["E"] == {"passed": 3, "total": 3, "rate": 1.0}
    out2 = tmp_path / "mock.json"
    p = subprocess.run([sys.executable, "-m", "zhihui.bench.run", "--llm", "mock", "--category", "E", "--output", str(out2)], cwd=cwd, capture_output=True, text=True, env=env, timeout=600)
    assert p.returncode == 0, p.stdout + p.stderr
    data = json.loads(out2.read_text(encoding="utf-8"))
    assert data["summary"]["E"] == {"passed": 1, "total": 1, "rate": 1.0, "skipped": 11}             # 只有 E011 真的判定了，其余 11 条如实 SKIP
    assert sum(1 for r in data["results"] if r.get("skipped")) == 11
