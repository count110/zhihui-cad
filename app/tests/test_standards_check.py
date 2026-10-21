"""S9 规范自检报告：条目齐全、标准出处、得分；错误进校验循环；导出 JSON + Markdown。"""

import json

from zhihui.agent.llm import plan_to_steps
from zhihui.bench.run import load_cases
from zhihui.cad.dxf_io import build_doc
from zhihui.cad.standards_check import errors_for_validate, run, to_markdown
from zhihui.tools.core import ToolService

REQUIRED = {"paper", "scale", "text_height", "cjk_height", "font", "lineweight", "ltscale", "gap", "overlap",
            "dim_overlap", "dim_on_outline", "dim_cross", "dim_duplicate", "dim_text_crossed",
            "text_line", "text_text", "title_block", "bylayer", "layer0", "anchored"}


def build(case: str):
    plan = load_cases()[case]["plan"]
    svc = ToolService()
    did = svc.create_drawing(**plan_to_steps(plan)[0][0]["arguments"])["drawing_id"]
    for name, args in (("place_equipment", {"items": plan["equipment"]}), ("connect_ports", {"items": plan["connections"]}), ("auto_layout", {})):
        assert getattr(svc, name)(did, **args)["ok"]
    return svc, did


def test_report_items_sources_and_score():
    """检查项覆盖要求的全部类别，每项有标准出处；G001、W001 错误 0 条，警告只有标题栏未填项（原因写明）。"""
    for case in ("G001", "W001"):
        svc, did = build(case)
        rep = run(svc.store.load(did))
        ids = {x["id"] for x in rep["items"]}
        assert REQUIRED <= ids
        assert all(x["source"] for x in rep["items"])
        assert rep["errors"] == 0, [x for x in rep["items"] if x["status"] == "error"]
        warns = [x for x in rep["items"] if x["status"] == "warning"]
        assert [x["id"] for x in warns] == ["title_block"] and "不编造" in warns[0]["details"][-1]
        assert rep["score"] == {"passed": len(rep["items"]) - 1, "total": len(rep["items"]), "text": f"{len(rep['items']) - 1}/{len(rep['items'])}"}
        md = to_markdown(rep, case)
        assert "符合项 / 总项" in md and "GB/T 14691" in md and "## 警告说明" in md


def test_errors_detected_and_fed_to_validation():
    """人为制造违规：非系列字高、汉字 < 3.5、0 层实体、实体不随层、文字压线 → 报告给错误，转成 validate 的 errors。"""
    svc, did = build("G001")
    d = svc.store.load(did)
    doc = build_doc(d)
    msp = doc.modelspace()
    msp.add_text("说明文字", height=3.0 * d.scale, dxfattribs={"layer": "ZH-文字", "style": "ZH_CN", "insert": (0, 0)})
    msp.add_line((0, 0), (1, 1), dxfattribs={"layer": "0"})
    msp.add_line((5, 5), (9, 9), dxfattribs={"layer": "ZH-细实线", "color": 1})
    tb = next(e for e in msp.query("TEXT") if e.dxf.text == "布袋除尘器")
    ext = __import__("ezdxf").bbox.extents([tb], fast=True)
    y = (ext.extmin.y + ext.extmax.y) / 2
    msp.add_line((ext.extmin.x - 100, y), (ext.extmax.x + 100, y), dxfattribs={"layer": "ZH-细实线"})
    rep = run(d, doc)
    bad = {x["id"] for x in rep["items"] if x["status"] == "error"}
    assert {"text_height", "cjk_height", "layer0", "bylayer", "text_line"} <= bad
    assert rep["score"]["passed"] == len(rep["items"]) - len(bad) - rep["warnings"]
    errs = errors_for_validate(rep)
    assert {"E_STD_TEXT_HEIGHT", "E_STD_LAYER0"} <= {e["code"] for e in errs}
    assert all("GB/T" in e["message"] or "通行做法" in e["message"] for e in errs)


def test_export_writes_report_and_validate_blocks_errors(monkeypatch):
    svc, did = build("G001")
    r = svc.export_artifacts(did)
    assert r["ok"] and r["standards_check"]["errors"] == 0
    files = {f["format"]: f for f in r["files"]}
    rep = json.loads(svc.store.resolve_ref(files["check_json"]["file_ref"]).read_text(encoding="utf-8"))
    assert rep["score"]["text"] == r["standards_check"]["score"]["text"]
    assert "规范自检报告" in svc.store.resolve_ref(files["check_md"]["file_ref"]).read_text(encoding="utf-8")
    # 错误项进入校验：伪造一条自检错误，validate 不通过、导出被拒
    from zhihui.cad import standards_check

    real = standards_check.run

    def fake(d, doc=None):
        rep = real(d, doc)
        rep["items"][0].update(status="error", details=["测试：图框不对"])
        return rep

    monkeypatch.setattr(standards_check, "run", fake)
    v = svc.validate_drawing(did)
    assert not v["passed"] and v["errors"][0]["code"] == "E_STD_PAPER"
    assert svc.export_artifacts(did)["error_code"] == "E_NOT_VALID"


def test_long_medium_label_between_close_equipment_passes():
    """回归（网页任务失败）：锅炉 → 急冷塔之间直管只有 12 mm，介质名「余热回收后烟气」26 mm 宽。
    以前候选位置只往下找 15 mm，找不到就压在急冷塔上，校验报 E_STD_TEXT_LINE，模型来回挪设备直到工具调用超限。"""
    from zhihui.agent.llm import DEMO_PLAN

    svc = ToolService()
    did = svc.create_drawing(title="生活垃圾焚烧烟气处理工艺流程图", title_block={"drawing_no": "PFD-01"})["drawing_id"]
    conns = [dict(c) for c in DEMO_PLAN["connections"]]
    conns[2]["medium"] = "余热回收后烟气"
    for r in (svc.place_equipment(did, DEMO_PLAN["equipment"]), svc.connect_ports(did, conns), svc.auto_layout(did)):
        assert r["ok"], r
    v = svc.validate_drawing(did)
    assert v["passed"], v["errors"]


def test_text_conflict_error_names_objects_and_fix():
    """文字压线的错误要指出文字属于哪个对象、被谁压，并给出能执行的修复办法（模型才不会乱挪）。"""
    from ezdxf.enums import TextEntityAlignment as TA

    from zhihui.cad.dxf_io import fix_insert

    svc, did = build("G001")
    d = svc.store.load(did)
    doc = build_doc(d)
    label = next(t for t in doc.modelspace().query("TEXT") if t.has_xdata("ZHIHUI") and t.get_xdata("ZHIHUI")[0][1] == "conn_label")
    cid = label.get_xdata("ZHIHUI")[1][1]
    from zhihui.cad.linework import collect

    tower = next(e for e in d.equipment if e.tag == "T-101")
    ins = next(r for r in doc.modelspace().query("INSERT") if r.has_xdata("ZHIHUI") and r.get_xdata("ZHIHUI")[1][1] == tower.id)
    side = max((g for g in collect(doc) if g.owner == ins.dxf.handle and g.vertical), key=lambda g: abs(g.a[1] - g.b[1]))
    fix_insert(label.set_placement((side.a[0], (side.a[1] + side.b[1]) / 2), align=TA.MIDDLE_CENTER))  # 把介质名挪到急冷塔的轮廓线上
    rep = run(d, doc)
    item = next(x for x in rep["items"] if x["id"] == "text_line")
    assert item["status"] == "error" and cid in item["refs"] and "T-101" in item["refs"]
    err = next(e for e in errors_for_validate(rep) if e["code"] == "E_STD_TEXT_LINE")
    assert cid in err["message"] and "T-101" in err["message"]
    assert "move_equipment" in err["hint"] and "ask_user" in err["hint"]
