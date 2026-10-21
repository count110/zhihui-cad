"""化工流程图规范化 第 5 步：规范自检扩展（规范化要求 12）、基准用例 C001～C003（文章图 3-1、3-2、3-4）。"""

import pytest

from zhihui.agent.llm import plan_to_steps
from zhihui.bench.run import load_cases
from zhihui.cad.dxf_io import APPID, build_doc
from zhihui.cad.standards_check import _verified, run, to_markdown
from zhihui.ir import Connection, Drawing, Equipment
from zhihui.tools.core import ToolService


def _build(cid):
    plan = load_cases()[cid]["plan"]
    svc = ToolService()
    steps = plan_to_steps(plan)
    did = svc.create_drawing(**steps[0][0]["arguments"])["drawing_id"]
    for step in steps[1:]:
        if isinstance(step, list):
            for call in step:
                r = getattr(svc, call["name"])(did, **{k: v for k, v in call["arguments"].items() if k != "drawing_id"})
                assert r["ok"], (call["name"], r)
    return svc, did, svc.store.load(did)


@pytest.mark.parametrize("cid", ["C001", "C002", "C003"])
def test_article_cases_self_check_clean(cid):
    """三条用例按文章图 3-1、3-2、3-4 建图：规范自检 0 错误；报告分开统计核对过原文和没核对的规则。"""
    svc, did, d = _build(cid)
    rep = run(d)
    assert rep["errors"] == 0, [(x["name"], x["details"][:2]) for x in rep["items"] if x["status"] == "error"]
    ids = {x["id"] for x in rep["items"]}
    assert {"stage", "sheets", "dash_join", "thin", "family", "tags", "pipes", "pipe_label", "codes"} <= ids
    v = rep["verified"]
    assert v["items"] >= 3 and v["unverified_items"] >= 10 and v["items"] + v["unverified_items"] == len(rep["items"])
    assert all(isinstance(x["verified"], bool) for x in rep["items"])
    md = to_markdown(rep, cid)
    assert "原文核对" in md and "已核对原文的规则" in md
    exp = load_cases()[cid]["expected"]
    assert d.stage == exp["stage"]
    if cid == "C003":
        assert {"nozzles", "crossing", "fittings", "instruments", "legend"} <= ids
        warn = {x["id"]: x for x in rep["items"] if x["status"] == "warning"}
        # 文章图 3-4 上 CW、HW、LS、PL0606、SC 这几根本身就没标材料或管径：只能来自用户，记警告
        assert set(warn) <= {"title_block", "pipe_label"}


def test_stage_differences_in_check():
    """同一张图，施工流程图要查管口、管道标注（缺管道号是错误）；方案流程图不查。"""
    svc, did, d = _build("C001")
    scheme_ids = {x["id"] for x in run(d)["items"]}
    assert "nozzles" not in scheme_ids
    d.stage = "construction"
    assert _item(run(d), "stage")["status"] == "error"  # 施工流程图必须有图框（C001 是省略图框的方案图）
    d.frame = True
    rep = run(d)
    assert "nozzles" in {x["id"] for x in rep["items"]}
    pl = next(x for x in rep["items"] if x["id"] == "pipe_label")
    assert pl["status"] == "error" and any("没有管道号" in m for m in pl["details"])
    st = next(x for x in rep["items"] if x["id"] == "stage")
    assert st["status"] == "warning" and any("阀门" in m for m in st["details"]) and any("仪表" in m for m in st["details"])


def _crossing():
    d = Drawing(id="x")
    for i, (x, y) in enumerate([(5000, 8000), (15000, 8000), (10000, 11000), (10000, 5000)], 1):
        d.equipment.append(Equipment(id=f"E{i}", tag=f"V-10{i}", symbol_id="tank", x=x, y=y))
    h_path = [(5000 + 13 * 1.25 * 50, 8000 - 9 * 1.25 * 50), (15000 - 13 * 1.25 * 50, 8000 - 9 * 1.25 * 50)]
    v_path = [(10000 + 13 * 1.25 * 50, 11000 - 9 * 1.25 * 50), (10000 + 13 * 1.25 * 50, 5000 + 5 * 1.25 * 50)]
    d.connections.append(Connection(id="C1", from_equip="E1", from_port="out", to_equip="E2", to_port="in", path=h_path))
    d.connections.append(Connection(id="C2", from_equip="E3", from_port="out", to_equip="E4", to_port="in", path=v_path))
    return d, v_path


def _item(rep, iid):
    return next(x for x in rep["items"] if x["id"] == iid)


def test_check_catches_violations():
    d, v_path = _crossing()
    doc = build_doc(d)
    assert _item(run(d, doc), "crossing")["status"] == "pass"
    msp = doc.modelspace()
    msp.add_lwpolyline(v_path, dxfattribs={"layer": "ZH-管道-主物料"}).set_xdata(APPID, [(1000, "conn_part"), (1000, "C2"), (1000, "9")])  # 交叉处没断开
    msp.add_lwpolyline([(1000, 1000), (3000, 1000), (3000, 3000)], dxfattribs={"layer": "ZH-虚线"})  # 带拐角的整条虚线
    msp.add_text("XX  多余", dxfattribs={"layer": "ZH-图例"}).set_xdata(APPID, [(1000, "legend"), (1000, "code:XX")])  # 图例多余条目
    rep = run(d, doc)
    assert _item(rep, "crossing")["status"] == "error"
    assert _item(rep, "dash_join")["status"] == "error" and _item(rep, "dash_join")["verified"] is True
    leg = _item(rep, "legend")
    assert leg["status"] == "error" and any("已经没用到" in m for m in leg["details"])


def test_verified_flag_from_source():
    assert _verified("GB/T 14691-1993 第 3.2 条（字高系列）") is True
    assert _verified("GB/T 17450-1998 第 5.1 条") is True
    assert _verified("文章 表 2-5") is False
    assert _verified("GB/T 17450-1998 第 4.1 条；HG/T 20519.1-2009 表 6.1.3") is False
    assert _verified("GB/T 14689-2008 第 3.1 条") is False  # 原文未取得
