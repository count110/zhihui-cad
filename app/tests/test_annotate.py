"""S4 尺寸与标注：DIMENSION 实体、GB_DIM 样式、排列、五条规则、标注关联、专业符号块。"""

import ezdxf
import pytest

from zhihui.agent.llm import DEMO_PLAN
from zhihui.cad.dxf_io import read_dxf, write_dxf
from zhihui.tools.core import ToolService


def demo():
    svc = ToolService()
    did = svc.create_drawing(title="尺寸")["drawing_id"]
    for r in (svc.place_equipment(did, DEMO_PLAN["equipment"]), svc.connect_ports(did, DEMO_PLAN["connections"]), svc.auto_layout(did)):
        assert r["ok"], r
    return svc, did


def test_dimensions_are_real_dim_entities_and_roundtrip(tmp_path):
    svc, did = demo()
    assert svc.add_dimension(did, "T-101", "height", text="18000")["ok"]
    assert svc.add_dimension(did, "D-101", "width")["ok"]
    assert svc.add_dimension(did, "F-101", "between_h", to="B-101", text="9000")["ok"]
    d = svc.store.load(did)
    write_dxf(d, tmp_path / "a.dxf")
    doc = ezdxf.readfile(tmp_path / "a.dxf")
    dims = list(doc.modelspace().query("DIMENSION"))
    assert len(dims) == 3 and {x.dxf.dimstyle for x in dims} == {"GB_DIM"} and {x.dxf.layer for x in dims} == {"ZH-尺寸"}
    st = doc.dimstyles.get("GB_DIM")
    assert (st.dxf.dimtxt, st.dxf.dimexe, st.dxf.dimscale) == (3.5, 2.0, d.scale)  # 数字 3.5 号，尺寸界线超出 2 mm
    assert {x.dxf.text for x in dims} == {"18000", "<>", "9000"}
    d2, rep = read_dxf(tmp_path / "a.dxf")
    assert rep.topology_consistent and [x.model_dump() for x in d2.dimensions] == [x.model_dump() for x in d.dimensions]


def test_dimension_rules_and_arrangement():
    from zhihui.cad.dims import arrange, check
    from zhihui.cad.dxf_io import build_doc

    svc, did = demo()
    assert svc.add_dimension(did, "F-101", "between_h", to="T-101", text="20000")["ok"]  # 长的
    assert svc.add_dimension(did, "F-101", "between_h", to="B-101", text="9000")["ok"]  # 短的
    dup = svc.add_dimension(did, "B-101", "between_h", to="F-101")
    assert dup["error_code"] == "E_DIM_DUPLICATE"  # 同一尺寸不重复标注
    d = svc.store.load(did)
    lv = arrange(d)
    long_id, short_id = d.dimensions[0].id, d.dimensions[1].id
    assert lv[long_id] < lv[short_id]  # 尺寸放在下方：内小外大，长的更靠外（更低）
    assert abs(lv[short_id] - lv[long_id]) == pytest.approx(7 * d.scale)
    assert check(build_doc(d), d) == []
    assert svc.validate_drawing(did)["passed"]


def test_dimension_text_from_user_only():
    from zhihui.agent.guards import Guard

    said = [{"role": "user", "content": "急冷塔 T-101 高 18000"}]
    g = Guard()
    assert g.check("add_dimension", {"target": "T-101", "measure": "height", "text": "18000"}, said) is None
    assert g.check("add_dimension", {"target": "T-101", "measure": "height", "text": "20000"}, said)["error_code"] == "E_UNSOURCED_VALUE"


def test_annotation_follows_equipment_and_free_text_warning():
    svc, did = demo()
    a = svc.add_annotation(did, "T-101", "设计压力见设备表")["annotation_ref"]
    svc.add_annotation(did, "title", "示意")
    d0 = svc.store.load(did)
    an0 = next(x for x in d0.annotations if x.id == a)
    assert svc.move_equipment(did, "T-101", dx=0, dy=500)["ok"]
    an1 = next(x for x in svc.store.load(did).annotations if x.id == a)
    assert (an1.x - an0.x, an1.y - an0.y) == pytest.approx((0, 500))
    v = svc.validate_drawing(did)
    assert "W_FREE_TEXT" in {w["code"] for w in v["warnings"]}


def test_annotation_symbol_blocks_roundtrip(tmp_path):
    svc, did = demo()
    assert svc.add_annotation(did, "T-101", "A", style="section")["ok"]
    assert svc.add_annotation(did, "D-101", "1/3", style="index")["ok"]
    assert svc.add_annotation(did, "S-101", "", style="north")["ok"]
    pipe = svc.store.load(did).connections[-1].id
    assert svc.add_annotation(did, pipe, "i=0.005", style="slope")["ok"]
    assert svc.add_annotation(did, "T-101", "x", style="bogus")["error_code"] == "E_BAD_ARG"
    d = svc.store.load(did)
    write_dxf(d, tmp_path / "a.dxf")
    msp = ezdxf.readfile(tmp_path / "a.dxf").modelspace()
    names = sorted(r.dxf.name for r in msp.query("INSERT") if r.dxf.name.startswith("ZH_ANNO_"))
    assert names == ["ZH_ANNO_index", "ZH_ANNO_north", "ZH_ANNO_section", "ZH_ANNO_section", "ZH_ANNO_slope"]  # 剖切符号成对
    assert "A—A" in [t.dxf.text for t in msp.query("TEXT")]
    d2, rep = read_dxf(tmp_path / "a.dxf")
    got = sorted((x.style, x.text) for x in d2.annotations)
    assert got == sorted((x.style, x.text) for x in d.annotations) and not rep.texts


def test_break_rule_triggers_only_on_long_empty_runs(tmp_path):
    """折断：直线段超过可用高度的 30% 且中间没有对象才断，两端各留 15 mm，断口两个双折线；中间有对象就不断。"""
    from zhihui.cad import water
    from zhihui.ir import Connection, Drawing, Equipment

    def build(blocker: bool):
        d = Drawing(id="dwg_b", breaks=True)
        d.equipment += [Equipment(id="E1", tag="V-1", symbol_id="tank", x=10000, y=12000),
                        Equipment(id="E2", tag="V-2", symbol_id="tank", x=10000, y=3500)]
        top = (10000 + 13 * 1.25 * 50, 12000 - 9 * 1.25 * 50)
        bot = (10000 - 13 * 1.25 * 50, 3500 + 5 * 1.25 * 50)
        x = top[0] + 500
        d.connections.append(Connection(id="C1", from_equip="E1", from_port="out", to_equip="E2", to_port="in",
                                        path=[top, (x, top[1]), (x, bot[1]), bot]))
        if blocker:
            d.equipment.append(Equipment(id="E3", tag="P-1", symbol_id="pump", x=x + 300, y=(top[1] + bot[1]) / 2))
        return d

    d = build(False)
    spans = water.break_spans(d)
    assert list(spans) == ["C1"] and list(spans["C1"]) == [1]  # 只断那段长竖线
    t0, t1 = spans["C1"][1]
    assert t0 == pytest.approx(15 * d.scale)
    write_dxf(d, tmp_path / "b.dxf")
    msp = ezdxf.readfile(tmp_path / "b.dxf").modelspace()
    assert sum(1 for r in msp.query("INSERT") if r.dxf.name == "ZH_ANNO_break") == 2
    d2, rep = read_dxf(tmp_path / "b.dxf")
    assert rep.topology_consistent and d2.connections[0].path == d.connections[0].path  # 断开的管线读回仍是整条
    assert water.break_spans(build(True)) == {}  # 中间有设备，不断
    off = build(False)
    off.breaks = False
    assert water.break_spans(off) == {}  # 默认不开


def test_detail_view(tmp_path):
    """局部放大图：原处索引圆 + 罗马数字；空白处放大 2 倍画出圆内图形，标「I 放大 2:1」；比例不在系列里报错。"""
    svc, did = demo()
    assert svc.add_detail_view(did, "F-201", radius=8, factor=3)["error_code"] == "E_BAD_ARG"  # 3:1 不在放大比例系列
    r = svc.add_detail_view(did, "F-201", radius=8, factor=2)
    assert r["ok"] and r["label"] == "I"
    d = svc.store.load(did)
    write_dxf(d, tmp_path / "v.dxf")
    msp = ezdxf.readfile(tmp_path / "v.dxf").modelspace()
    det = [e for e in msp if e.has_xdata("ZHIHUI") and e.get_xdata("ZHIHUI")[0][1] == "detail"]
    circles = sorted(round(e.dxf.radius / d.scale, 3) for e in det if e.dxftype() == "CIRCLE")
    assert circles == [8, 16]  # 原处 r = 8，放大图 r = 16
    assert "I 放大 2:1" in [e.dxf.text for e in det if e.dxftype() == "TEXT"]
    assert sum(1 for e in det if e.dxftype() == "LWPOLYLINE") > 3  # 圆内的设备图形和管线被复制放大
    d2, rep = read_dxf(tmp_path / "v.dxf")
    assert rep.topology_consistent and not rep.warnings and d2.details == d.details
    assert svc.add_detail_view(did, "", remove=[r["detail_ref"]])["ok"] and not svc.store.load(did).details
