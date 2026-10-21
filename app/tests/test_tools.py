"""工具层：事务性、改图不误改、原件不覆写。"""

import ezdxf

from zhihui.agent.llm import DEMO_PLAN
from zhihui.cad.diff import signatures
from zhihui.cad.dxf_io import write_dxf
from zhihui.tools.core import ToolService


def make(svc, plan=DEMO_PLAN):
    did = svc.create_drawing(title=plan["title"])["drawing_id"]
    assert svc.place_equipment(did, plan["equipment"])["ok"]
    assert svc.connect_ports(did, plan["connections"])["ok"]
    assert svc.auto_layout(did)["ok"]
    return did


def upload(svc, did, data_dir):
    p = data_dir / "uploads" / "t1" / "in.dxf"
    p.parent.mkdir(parents=True)
    write_dxf(svc.store.load(did), p)
    return "uploads/t1/in.dxf", p


def test_failed_tool_does_not_change_drawing():
    svc = ToolService()
    did = make(svc)
    before = svc.store.load(did)
    r = svc.connect_ports(did, [{"from_port": "T-101.nope", "to_port": "D-101.in"}])
    assert not r["ok"] and r["error_code"] == "E_PORT_INVALID" and "端口" in r["hint"]
    assert svc.store.load(did).revision == before.revision


def test_move_keeps_everything_else(data_dir):
    svc = ToolService()
    ref, original = upload(svc, make(svc), data_dir)
    original_bytes = original.read_bytes()
    w = svc.inspect_drawing(file_ref=ref)
    assert w["ok"] and w["topology"]["topology_consistent"]
    wid = w["drawing_id"]
    base = svc.store.load(wid)
    r = svc.move_equipment(wid, "F-201", dx=800, dy=0)
    assert r["ok"], r
    v = svc.validate_drawing(wid)
    assert v["passed"] and v["profile"] == "modify", v
    after = svc.store.load(wid)
    sb, sa = signatures(base), signatures(after)
    changed = {k for k in sb if sb[k] != sa[k]}
    f201 = after.get_equipment("F-201").id
    assert changed == {f201, *(c.id for c in after.connections_of(f201))}
    ex = svc.export_artifacts(wid)
    assert ex["ok"] and ex["diff"]["moved"][0]["tag"] == "F-201"
    assert original.read_bytes() == original_bytes  # 原件永不覆写


def test_move_into_other_equipment_is_rejected(data_dir):
    svc = ToolService()
    ref, _ = upload(svc, make(svc), data_dir)
    wid = svc.inspect_drawing(file_ref=ref)["drawing_id"]
    d = svc.store.load(wid)
    a, b = d.get_equipment("F-201"), d.get_equipment("D-101")
    r = svc.move_equipment(wid, "F-201", dx=b.x - a.x, dy=b.y - a.y)
    assert not r["ok"] and r["error_code"] == "E_OVERLAP"


def test_modify_rejects_silent_full_relayout_and_cannot_whitelist(data_dir):
    svc = ToolService()
    ref, _ = upload(svc, make(svc), data_dir)
    wid = svc.inspect_drawing(file_ref=ref)["drawing_id"]
    # 改图时换图幅 = 整图重排，未经同意一律拒绝
    r = svc.auto_layout(wid, mode="incremental", paper_size="A2")
    assert not r["ok"] and r["error_code"] == "E_FULL_RELAYOUT"
    # 绕过工具直接改坐标：expected_changes 也不能把误改「放过」
    d = svc.store.load(wid)
    d.get_equipment("V-101").x += 500
    svc.store.save(d)
    v = svc.validate_drawing(wid, expected_changes=["V-101"])
    assert not v["passed"] and "E_UNEXPECTED_CHANGE" in {e["code"] for e in v["errors"]}


def test_connect_warns_when_inserting_without_removing_old_pipe(data_dir):
    svc = ToolService()
    ref, _ = upload(svc, make(svc), data_dir)
    wid = svc.inspect_drawing(file_ref=ref)["drawing_id"]
    svc.place_equipment(wid, [{"symbol_id": "valve", "tag": "XV-101"}])
    r = svc.connect_ports(wid, [{"from_port": "F-201.out", "to_port": "XV-101.in"}])
    assert r["ok"] and "remove" in r["notes"][0]


def test_find_entities_flags_ambiguity():
    svc = ToolService()
    plan = {
        "title": "t",
        "equipment": [{"symbol_id": "pump", "tag": "P-101"}, {"symbol_id": "pump", "tag": "P-102"}],
        "connections": [{"from_port": "P-101.out", "to_port": "P-102.in"}],
    }
    did = make(svc, plan)
    r = svc.find_entities(did, query="泵")
    assert len(r["candidates"]) == 2 and "ambiguous" in r


def test_inspect_rejects_path_escape_and_foreign(data_dir):
    svc = ToolService()
    assert svc.inspect_drawing(file_ref="../../etc/passwd")["error_code"] == "E_FORBIDDEN"
    doc = ezdxf.new()
    doc.modelspace().add_line((0, 0), (100, 0))
    (data_dir / "uploads").mkdir()
    doc.saveas(data_dir / "uploads" / "x.dxf")
    assert svc.inspect_drawing(file_ref="uploads/x.dxf")["error_code"] == "E_FOREIGN_DXF"
