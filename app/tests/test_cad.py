"""CAD 引擎：布局确定性、布线、DXF 往返、校验规则。"""

from zhihui.agent.llm import DEMO_PLAN
from zhihui.cad.dxf_io import read_dxf, write_dxf
from zhihui.cad.layout import auto_layout
from zhihui.cad.symbols import load_symbols
from zhihui.cad.validate import validate
from zhihui.ir import Connection, Drawing, Equipment


def build(plan, order=None):
    d = Drawing(id="dwg_test")
    eqs = plan["equipment"] if order is None else [plan["equipment"][i] for i in order]
    for e in eqs:
        d.equipment.append(Equipment(id=d.new_id("E"), tag=e["tag"], symbol_id=e["symbol_id"], name=e.get("name", "")))
    for c in plan["connections"]:
        (a, ap), (b, bp) = c["from_port"].split("."), c["to_port"].split(".")
        d.connections.append(Connection(id=d.new_id("C"), from_equip=d.get_equipment(a).id, from_port=ap, to_equip=d.get_equipment(b).id, to_port=bp))
    return d


def positions(d):
    return {e.tag: (e.x, e.y) for e in d.equipment}


def test_symbol_library_has_12_symbols_with_ports_on_bbox():
    syms = load_symbols()
    assert len(syms) >= 20
    for s in syms.values():
        x0, y0, x1, y1 = s.bbox
        for p in s.ports:
            on_edge = abs(p.x - x0) < 1e-6 or abs(p.x - x1) < 1e-6 or abs(p.y - y0) < 1e-6 or abs(p.y - y1) < 1e-6
            assert on_edge, f"{s.id}.{p.name} 不在包围盒边上"


def test_layout_is_deterministic_and_order_independent():
    a = build(DEMO_PLAN)
    b = build(DEMO_PLAN, order=list(reversed(range(len(DEMO_PLAN["equipment"])))))
    auto_layout(a)
    auto_layout(b)
    assert positions(a) == positions(b)
    r = validate(a)
    assert r["passed"], r["errors"]
    assert r["stats"]["crossings"] == 0


def test_dxf_roundtrip_keeps_topology(tmp_path):
    d = build(DEMO_PLAN)
    auto_layout(d)
    write_dxf(d, tmp_path / "a.dxf")
    d2, rep = read_dxf(tmp_path / "a.dxf")
    assert rep.topology_consistent
    assert d2.topology_hash() == d.topology_hash()
    assert positions(d2) == positions(d)


def test_validate_catches_overlap_and_unplaced():
    d = build(DEMO_PLAN)
    assert "E_UNPLACED" in {e["code"] for e in validate(d)["errors"]}
    auto_layout(d)
    d.equipment[1].x, d.equipment[1].y = d.equipment[0].x, d.equipment[0].y
    assert "E_OVERLAP" in {e["code"] for e in validate(d)["errors"]}


def test_moved_in_cad_is_detected(tmp_path):
    """模拟用户在 CAD 里挪了设备但没挪管线：读回时应发现并标记需要确认。"""
    import ezdxf

    d = build(DEMO_PLAN)
    auto_layout(d)
    write_dxf(d, tmp_path / "a.dxf")
    doc = ezdxf.readfile(tmp_path / "a.dxf")
    ref = next(r for r in doc.modelspace().query("INSERT") if r.dxf.name == "ZH_fan")
    ref.dxf.insert = (ref.dxf.insert.x, ref.dxf.insert.y + 2000)
    doc.saveas(tmp_path / "b.dxf")
    _, rep = read_dxf(tmp_path / "b.dxf")
    assert rep.stale_pipes and not rep.topology_consistent
