"""出图模板 water_elevation（给水处理 工艺流程及高程布置图）与 pfd_default 的回归。"""

import json
from pathlib import Path

import ezdxf
import pytest

from zhihui.agent.llm import DEMO_PLAN, plan_to_steps
from zhihui.bench.run import load_cases
from zhihui.cad import water
from zhihui.cad.dxf_io import read_dxf, write_dxf
from zhihui.cad.routing import collect_segments
from zhihui.cad.symbols import boxes_overlap, footprint, label_box, symbol_box
from zhihui.cad.validate import validate
from zhihui.tools.core import ToolService

GOLDEN = Path(__file__).with_name("golden") / "pfd_layout.json"


def build_plan(plan: dict):
    svc = ToolService()
    args = plan_to_steps(plan)[0][0]["arguments"]
    did = svc.create_drawing(**args)["drawing_id"]
    for r in (svc.place_equipment(did, plan["equipment"]), svc.connect_ports(did, plan["connections"]), svc.auto_layout(did)):
        assert r["ok"], r
    return svc, svc.store.load(did)


@pytest.fixture
def w001():
    return build_plan(load_cases()["W001"]["plan"])


def test_water_line_follows_elevation(w001):
    """沉淀池 > 滤池 > 清水池：水面线 y 的差 = Δ高程 × vscale（图纸 mm/m）× unit。"""
    _, d = w001
    f = d.elev_frame
    assert f["vscale"] > 0

    def water_y(tag):
        e = d.get_equipment(tag)
        key, port = water.align_key(e)
        from zhihui.cad.symbols import get_symbol

        return e.y + get_symbol(e.symbol_id).elev_ports[port] * d.unit, water.elevations(e)[key]

    (ys, hs), (yf, hf), (yc, hc) = water_y("V-101"), water_y("X-102"), water_y("V-102")
    assert ys > yf > yc
    k = f["vscale"] * d.unit
    assert ys - yf == pytest.approx((hs - hf) * k, abs=0.2)
    assert yf - yc == pytest.approx((hf - hc) * k, abs=0.2)
    # 每个高程都画了标注，数值保留两位小数，位置就在该高程处
    marks = {a.id.rstrip("<>v"): a for a in d.annotations if a.style == "elevation"}
    for e in d.equipment:
        for key, h in water.elevations(e).items():
            a = marks[f"EL_{e.id}_{key}"]
            assert a.text == f"{h:.2f}"
            assert a.y == pytest.approx(water.elev_y(d, h), abs=0.01)


def test_legend_is_codes_in_use(w001):
    svc, d = w001
    used = {c.code for c in d.connections if c.code}
    assert used == {"YS", "QS", "PAC", "CL"}
    # W001 的用户要求图例列全 6 种
    assert [it["code"] for it in d.legend] == ["YS", "QS", "FX", "CL", "PAC", "Q"]
    # 不要求时，图例 = 图中实际用到的代号
    d.legend_codes = []
    assert {it["code"] for it in water.legend_items(d)} == used
    d.code_names = {"YS": "原水管"}
    assert water.legend_items(d)[0] == {"code": "YS", "name": "原水管"}


def test_no_tag_row_and_names_below(w001, tmp_path):
    svc, d = w001
    assert validate(d)["passed"]
    u = d.unit
    segs = collect_segments(d.connections)
    for e in d.equipment:
        lb, sb = label_box(e, u, d.template_id), symbol_box(e, u)
        if lb[0] == lb[2]:  # 名称写在图元内或不注
            continue
        assert lb[3] < sb[1], f"{e.tag} 的名称不在图元下方"
        assert abs((lb[0] + lb[2]) / 2 - (sb[0] + sb[2]) / 2) < 0.5 * u, f"{e.tag} 的名称没有居中"
        for o in d.equipment:
            if o.id != e.id:
                assert not boxes_overlap(lb, footprint(o, u, d.template_id)), f"{e.tag} 的名称压到 {o.tag}"
        for y, x0, x1 in segs.horizontal:
            assert not (lb[1] < y < lb[3] and x0 < lb[2] and x1 > lb[0]), f"管线穿过 {e.tag} 的名称"
        for x, y0, y1 in segs.vertical:
            assert not (lb[0] < x < lb[2] and y0 < lb[3] and y1 > lb[1]), f"管线穿过 {e.tag} 的名称"
    write_dxf(d, tmp_path / "w.dxf")
    msp = ezdxf.readfile(tmp_path / "w.dxf").modelspace()
    assert not list(msp.query('*[layer=="ZH-位号"]'))
    texts = [t.dxf.text for t in msp.query("TEXT")]
    assert d.title in texts and "说明：" in texts and "工艺流程设计图" in texts
    assert not any(t in texts for t in ("V-101", "X-102"))  # 位号不显示，只留在块属性里


def test_dxf_roundtrip_keeps_water_semantics(w001, tmp_path):
    _, d = w001
    write_dxf(d, tmp_path / "w.dxf")
    d2, rep = read_dxf(tmp_path / "w.dxf")
    assert rep.topology_consistent, rep.to_dict()
    assert d2.topology_hash() == d.topology_hash()
    assert not rep.texts  # 代号、名称、图例、说明等都不当作图中自由文字
    key = lambda c: c.id  # noqa: E731
    for a, b in zip(sorted(d.connections, key=key), sorted(d2.connections, key=key)):
        assert (a.code, a.spec, a.path) == (b.code, b.spec, b.path)
    assert d2.systems == d.systems and d2.title_block == d.title_block and d2.notes == d.notes
    assert {e.id: e.params for e in d2.equipment} == {e.id: e.params for e in d.equipment}
    el = lambda dd: sorted((a.id, a.text, a.style, a.x, a.y) for a in dd.annotations)  # noqa: E731
    assert el(d2) == el(d)


def test_pfd_default_unchanged(tmp_path):
    """旧模板：布局坐标、管线路径、拓扑与新增功能之前完全一致；导出的 IR 和管线 XDATA 不带新字段。"""
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    plans = {"DEMO": DEMO_PLAN} | {k: c["plan"] for k, c in load_cases().items() if k in golden}
    for name, plan in plans.items():
        _, d = build_plan(plan)
        g = golden[name]
        assert d.topology_hash() == g["topology"], name
        assert {e.tag: [e.x, e.y] for e in d.equipment} == g["positions"], name
        assert {c.id: [list(p) for p in c.path] for c in d.connections} == g["paths"], name
    write_dxf(d, tmp_path / "p.dxf")
    doc = ezdxf.readfile(tmp_path / "p.dxf")
    tags = [v for _, v in doc.rootdict["ZHIHUI"]["META"].tags]
    ir = json.loads("".join(tags[3:]))
    assert not set(ir) & {"title_block", "notes", "legend", "legend_codes", "code_names", "systems", "vscale", "elev_frame"}
    assert all("code" not in c and "spec" not in c for c in ir["connections"])
    for pl in doc.modelspace().query("LWPOLYLINE"):
        xd = pl.get_xdata("ZHIHUI") if pl.has_xdata("ZHIHUI") else []
        if xd and xd[0][1] == "conn":
            assert len(xd) == 8


def test_elevation_overflow_hint():
    plan = json.loads(json.dumps(load_cases()["W001"]["plan"]))
    plan["vscale"] = 50  # 1:20，在比例系列里，但高程差在 A3 上放不下
    svc = ToolService()
    did = svc.create_drawing(**plan_to_steps(plan)[0][0]["arguments"])["drawing_id"]
    svc.place_equipment(did, plan["equipment"])
    svc.connect_ports(did, plan["connections"])
    r = svc.auto_layout(did)
    assert not r["ok"] and r["error_code"] == "E_LAYOUT_OVERFLOW"
    assert "vscale" in r["hint"] and "A2" in r["hint"]


def test_mock_agent_runs_w001():
    """--mock 离线模式：说「工艺流程及高程布置图」也能走完整流程出 DXF/PNG/PDF。"""
    import asyncio

    from zhihui.agent.llm import ScriptedLLM, demo_steps
    from zhihui.agent.orchestrator import Orchestrator

    out = asyncio.run(Orchestrator(ScriptedLLM(demo_steps(load_cases()["W001"]["prompt"], None))).run(load_cases()["W001"]["prompt"]))
    assert out.status == "READY", out.message
    d = ToolService().store.load(out.drawing_id)
    assert d.template_id == "water_elevation" and len(d.equipment) == 18


def test_subsystem_titles_and_dashed_box(w001, tmp_path):
    from zhihui.cad.symbols import port_point

    _, d = w001
    regions = {r["name"]: r for r in water.system_regions(d)}
    assert set(regions) == {"PAC投药系统", "二氧化氯投药系统"}
    u = d.unit
    for r in regions.values():  # 标题在组的下方，不压任何设备
        assert not any(boxes_overlap(r["title_box"], footprint(e, u, d.template_id)) for e in d.equipment)
    pac = regions["PAC投药系统"]
    pumps = [d.get_equipment(t) for t in ("P-201", "P-202")]
    for p in pumps:  # 虚线框只框计量泵
        fp = footprint(p, u, d.template_id)
        b = pac["box"]
        assert b[0] < fp[0] and b[1] < fp[1] and b[2] > fp[2] and b[3] > fp[3]
    assert not boxes_overlap(pac["box"], symbol_box(d.get_equipment("V-201"), u))
    for t, p in (("V-201", "P-201"), ("V-202", "P-202")):  # 计量泵与溶液池出口等高，管线走直线
        (_, ty), _ = port_point(d.get_equipment(t), "out", u)
        (_, py), _ = port_point(d.get_equipment(p), "in", u)
        assert ty == pytest.approx(py, abs=0.01)
    write_dxf(d, tmp_path / "w.dxf")
    msp = ezdxf.readfile(tmp_path / "w.dxf").modelspace()
    sys_texts = {t.dxf.text for t in msp.query("TEXT") if t.has_xdata("ZHIHUI") and t.get_xdata("ZHIHUI")[0][1] == "system"}
    assert sys_texts == set(regions)
    # 虚线框按线素相位拆成短线段（GB/T 17450 第 5.2.1 条：虚线相交于画线处），四个角都落在某一段画线的折点上
    pieces = [e for e in msp.query('LWPOLYLINE[layer=="ZH-虚线-分段"]') if e.get_xdata("ZHIHUI")[0][1] == "system"]
    assert len(pieces) > 8 and all(e.dxf.linetype == "BYLAYER" for e in pieces)
    assert msp.doc.layers.get("ZH-虚线-分段").dxf.linetype.upper() == "CONTINUOUS"
    x0, y0, x1, y1 = pac["box"]
    inner = {(round(x, 3), round(y, 3)) for e in pieces for x, y in list(e.get_points("xy"))[1:-1]}
    assert {(round(x0, 3), round(y0, 3)), (round(x1, 3), round(y0, 3)), (round(x1, 3), round(y1, 3)), (round(x0, 3), round(y1, 3))} <= inner


def test_dosing_lines_pick_dose_port():
    svc = ToolService()
    did = svc.create_drawing(template_id="water_elevation")["drawing_id"]
    svc.place_equipment(did, [
        {"symbol_id": "valveless_filter", "tag": "X-1"}, {"symbol_id": "dosing_point", "tag": "X-2"},
        {"symbol_id": "clear_well", "tag": "V-1"}, {"symbol_id": "clo2_generator", "tag": "M-1"},
        {"symbol_id": "metering_pump", "tag": "P-1"}, {"symbol_id": "pipe_mixer", "tag": "M-2"},
    ])
    r = svc.connect_ports(did, [
        {"from_port": "X-1", "to_port": "X-2", "code": "QS"},  # 主管线接 main_in，不接投药口
        {"from_port": "X-2", "to_port": "V-1", "code": "QS"},
        {"from_port": "M-1", "to_port": "X-2", "code": "CL"},  # 「CL→加药点」自动接 dose
        {"from_port": "P-1", "to_port": "M-2"},  # 计量泵出发、没写代号也算投药线
    ])
    assert [c["desc"] for c in r["connections"]] == ["X-1.out→X-2.main_in", "X-2.main_out→V-1.in", "M-1.out→X-2.dose", "P-1.out→M-2.dose"]


def test_notes_via_add_annotation():
    svc = ToolService()
    did = svc.create_drawing(template_id="water_elevation", notes=["本图为黄海高程系统。"])["drawing_id"]
    r = svc.add_annotation(did, "notes", "各净水构筑物具体结构尺寸见设计详图。")
    assert r["ok"] and r["notes"] == ["本图为黄海高程系统。", "各净水构筑物具体结构尺寸见设计详图。"]
    pfd = svc.create_drawing()["drawing_id"]
    assert svc.add_annotation(pfd, "notes", "x")["error_code"] == "E_BAD_ARG"


def test_guard_blocks_invented_numbers_in_water_fields():
    from zhihui.agent.guards import Guard

    said = [{"role": "user", "content": "画一张水厂的工艺流程及高程布置图，进水管 DN200。说明：1. 本图为黄海高程系统。"}]
    g = Guard()
    ok = g.check("connect_ports", {"items": [{"from_port": "A", "to_port": "B", "spec": "DN200"}]}, said)
    bad = g.check("connect_ports", {"items": [{"from_port": "A", "to_port": "B", "spec": "DN250"}]}, said)
    assert ok is None and bad["error_code"] == "E_UNSOURCED_VALUE"
    assert g.check("create_drawing", {"notes": ["1. 本图为黄海高程系统。"]}, said) is None  # 序号不算数值
    assert g.check("create_drawing", {"title_block": {"date": "2013.08"}}, said)["error_code"] == "E_UNSOURCED_VALUE"
    assert g.check("place_equipment", {"items": [{"params": {"elevations": {"water": 292.9}}}]}, said)["error_code"] == "E_UNSOURCED_VALUE"
    assert g.check("add_annotation", {"anchor_ref": "notes", "text": "2. 池深 4.5 m"}, said)["error_code"] == "E_UNSOURCED_VALUE"


def test_edit_equipment_params_keeps_elevations(w001):
    svc, d = w001
    r = svc.edit_equipment(d.id, "V-102", params={"elevations": {"max_water": 288.92, "min_water": 285.22}, "容量": "600t"})
    assert r["ok"], r
    e = svc.store.load(d.id).get_equipment("V-102")
    assert e.params["elevations"] == {"max_water": 288.92, "min_water": 285.22} and e.params["容量"] == "600t"


def test_p2_font_hatches_and_tees(w001, tmp_path):
    _, d = w001
    write_dxf(d, tmp_path / "w.dxf")
    doc = ezdxf.readfile(tmp_path / "w.dxf")
    msp = doc.modelspace()
    assert doc.styles.get("ZH_CN").dxf.font == "simfang.ttf" and doc.styles.get("ZH_CN").dxf.width == 0.7  # 长仿宋（TrueType 仿宋 × 0.7）
    ground = {h.dxf.pattern_name for h in msp.query('HATCH[layer=="ZH-地面"]')}
    assert ground == {"EARTH", "ANSI31", "AR-CONC"}  # 地面素土 + 台阶处挡土墙（混凝土）
    # 两台计量泵、两台发生器、给水管分两路：共用端口的管线在相遇处并成三通，并管处画圆点
    tees = [e for e in msp.query("LWPOLYLINE") if e.has_xdata("ZHIHUI") and e.get_xdata("ZHIHUI")[0][1] == "tee"]
    assert len(tees) == 3
    d2, rep = read_dxf(tmp_path / "w.dxf")  # 三通、断开的管线读回仍是整条、拓扑一致
    assert rep.topology_consistent and {c.id: c.path for c in d2.connections} == {c.id: c.path for c in d.connections}


@pytest.mark.parametrize("template", ["pfd_default", "water_elevation"])
def test_crossing_jumps(template, tmp_path):
    """两条管线十字交叉。高程图：跨的那条在交点断开画半圆（主流程 YS/QS 不断）。
    化工流程图：断开一根、不画半圆（文章图 2-6：横线连续、竖线断开），断口宽取 hg_pfd 的 gap_mm。"""
    from zhihui.ir import Connection, Drawing, Equipment

    d = Drawing(id="dwg_x", template_id=template)
    for i, (x, y) in enumerate([(5000, 8000), (15000, 8000), (10000, 11000), (10000, 5000)], 1):
        d.equipment.append(Equipment(id=f"E{i}", tag=f"V-10{i}", symbol_id="tank", x=x, y=y))
    h_path = [(5000 + 13 * 1.25 * 50, 8000 - 9 * 1.25 * 50), (15000 - 13 * 1.25 * 50, 8000 - 9 * 1.25 * 50)]
    v_path = [(10000 + 13 * 1.25 * 50, 11000 - 9 * 1.25 * 50), (10000 + 13 * 1.25 * 50, 5000 + 5 * 1.25 * 50)]
    d.connections.append(Connection(id="C1", from_equip="E1", from_port="out", to_equip="E2", to_port="in", path=h_path, code="YS"))
    d.connections.append(Connection(id="C2", from_equip="E3", from_port="out", to_equip="E4", to_port="in", path=v_path, code="PAC"))
    write_dxf(d, tmp_path / "x.dxf")
    msp = ezdxf.readfile(tmp_path / "x.dxf").modelspace()
    arcs = [a for a in msp.query("ARC") if a.has_xdata("ZHIHUI") and a.get_xdata("ZHIHUI")[0][1] == "jump"]
    if template == "water_elevation":
        jumps = water.jump_points(d)
        assert list(jumps) == ["C2"]  # 主流程 YS 不断，PAC 跨过去
        assert len(arcs) == 1 and arcs[0].get_xdata("ZHIHUI")[1][1] == "C2"
        return
    from zhihui.cad import chem

    assert chem.break_points(d) == {"C2": [(v_path[0][0], h_path[0][1])]}  # 同为主物料：竖线断开
    assert not arcs
    pieces = [e for e in msp.query("LWPOLYLINE") if e.has_xdata("ZHIHUI") and e.get_xdata("ZHIHUI")[1][1] == "C2" and e.get_xdata("ZHIHUI")[0][1] in ("conn", "conn_part")]
    spans = sorted((min(y for _, y in e.get_points("xy")), max(y for _, y in e.get_points("xy"))) for e in pieces)
    assert len(pieces) == 2 and spans[1][0] - spans[0][1] == pytest.approx(2.0 * d.scale)  # 断口 2 mm（图纸）
    assert spans[0][1] < h_path[0][1] < spans[1][0]
    d.connections[1].line_type = "utility"  # 主物料与辅助物料交叉：断辅助物料线
    d.connections[0], d.connections[1] = d.connections[0].model_copy(update={"line_type": "utility"}), d.connections[1].model_copy(update={"line_type": "process"})
    assert list(chem.break_points(d)) == ["C1"]


def test_retaining_wall_block_has_hatch(tmp_path):
    from zhihui.cad.dxf_io import _define_block, _setup

    doc = ezdxf.new("R2018", setup=True)
    _setup(doc)
    blk = doc.blocks[_define_block(doc, "retaining_wall", 50.0)]
    assert [h.dxf.pattern_name for h in blk.query("HATCH")] == ["ANSI31", "AR-CONC"]  # 混凝土：ANSI31 + AR-CONC
