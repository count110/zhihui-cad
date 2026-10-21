"""化工流程图规范化 第 4 步：物料组成表（文章 3.1.2 节、图 3-2）、工艺参数、图例及代号（图 3-4）、智能体（阶段识别、提示词、接口合同）。"""

import json
from pathlib import Path

import ezdxf

from zhihui.agent.guards import Guard, detect_stage
from zhihui.agent.llm import plan_to_steps
from zhihui.agent.prompts import SYSTEM_PROMPT
from zhihui.cad import chem_tables
from zhihui.cad.dxf_io import read_dxf, write_dxf
from zhihui.tools.core import ToolService

ROOT = Path(__file__).resolve().parents[1]


def _xd(e):
    return [v for _, v in e.get_xdata("ZHIHUI")] if e.has_xdata("ZHIHUI") else []


def _material():
    svc = ToolService()
    did = svc.create_drawing(title="物料流程图", stage="material", section_no="06", paper_size="A2")["drawing_id"]
    svc.place_equipment(did, [{"symbol_id": "reactor", "tag": "R0601", "name": "反应蒸馏釜"}, {"symbol_id": "vessel", "tag": "V0605", "name": "真空受槽"},
                              {"symbol_id": "pipe_terminal", "tag": "X1", "name": "物料残液"}])
    r = svc.connect_ports(did, [{"from_port": "X1.out", "to_port": "R0601.in", "medium": "物料残液"},
                                {"from_port": "R0601.out", "to_port": "V0605.in", "medium": "工艺液体", "params": {"t": "95℃", "p": "0.1MPa"}}])
    assert svc.auto_layout(did)["ok"]
    return svc, did, [c["ref"] for c in r["connections"]]


def test_stream_table_totals_from_article(tmp_path):
    """文章图 3-2 的两张表：合计由程序算，和原图一致（55.04 / 100.0；40.09）。"""
    assert chem_tables.total(["18.52", "32.42", "2.34", "1.76"]) == "55.04"
    assert chem_tables.total(["32.5", "16.8", "24.3", "26.4"]) == "100.0"
    assert chem_tables.total(["38.03", "1.24", "0.82", "0"]) == "40.09"
    assert chem_tables.total(["1", "x"]) == ""  # 有一项不是数字就不算
    svc, did, (c1, c2) = _material()
    r = svc.add_stream_table(did, anchor=c1, rows=[{"name": "组分1", "flow_kg_h": "18.52", "mole_pct": "32.5"}, {"name": "组分2", "flow_kg_h": "32.42", "mole_pct": "16.8"},
                                                   {"name": "组分3", "flow_kg_h": "2.34", "mole_pct": "24.3"}, {"name": "组分4", "flow_kg_h": "1.76", "mole_pct": "26.4"}])
    assert r["ok"] and r["totals"] == {"flow_kg_h": "55.04", "mole_pct": "100.0"}
    assert svc.add_stream_table(did, anchor=c2, rows=[{"name": "A", "flow_kg_h": "1", "mole_pct": "60"}])["notes"]  # 合计不是 100 要提示
    assert svc.add_stream_table(did, anchor=c1, rows=[{"name": "A", "flow_kg_h": "约10"}])["error_code"] == "E_BAD_ARG"
    d = svc.store.load(did)
    write_dxf(d, tmp_path / "m.dxf")
    msp = ezdxf.readfile(tmp_path / "m.dxf").modelspace()
    texts = [t.dxf.text for t in msp.query("TEXT")]
    assert {"序号", "名称", "流量(kg/h)", "摩尔分率(%)", "合计", "55.04", "100.0"} <= set(texts)
    assert all(e.dxf.layer == "ZH-表格" for e in msp if _xd(e)[:2] == ["table", "stream:ST1"] and e.dxftype() == "LINE")  # 表格线、指引线细实线
    assert "t=95℃  p=0.1MPa" in texts  # 物料流程图显示管线旁的工艺参数
    d2, rep = read_dxf(tmp_path / "m.dxf")
    assert rep.topology_consistent and not rep.texts and len(d2.stream_tables) == 2
    d.stage = "scheme"
    write_dxf(d, tmp_path / "s.dxf")
    assert "t=95℃  p=0.1MPa" not in [t.dxf.text for t in ezdxf.readfile(tmp_path / "s.dxf").modelspace().query("TEXT")]  # 方案流程图不显示


def test_legend_lists_only_used_items(tmp_path):
    svc = ToolService()
    did = svc.create_drawing(title="施工", stage="construction", section_no="06", paper_size="A2")["drawing_id"]
    svc.place_equipment(did, [{"symbol_id": "tank", "tag": "V0601"}, {"symbol_id": "pump", "tag": "P0601"}])
    r = svc.connect_ports(did, [{"from_port": "V0601.out", "to_port": "P0601.in", "medium": "工艺液体", "od": "57", "wall": "3.5", "material": "B",
                                 "fittings": [{"type": "截止阀"}]},
                                {"from_port": "P0601.out", "to_sheet": {"sheet": "5", "ref": "V0602"}, "medium": "工艺液体", "material": "B"}])
    c1 = r["connections"][0]["ref"]
    svc.auto_layout(did)
    svc.add_instrument(did, "PI", "P0601", "0601")
    d = svc.store.load(did)
    keys = [e["key"] for e in chem_tables.legend_entries(d)]
    assert keys == ["fitting:globe_valve", "offsheet", "code:PL", "material:B", "instrument:PI"]
    assert [e["label"] for e in chem_tables.legend_entries(d)][2:] == ["PL  工艺液体", "B  碳钢", "PI  压力或真空指示"]  # 名称按表 2-6、2-7、2-13 原文
    write_dxf(d, tmp_path / "c.dxf")
    drawn = {_xd(e)[1] for e in ezdxf.readfile(tmp_path / "c.dxf").modelspace() if _xd(e)[:1] == ["legend"]} - {"_title"}
    assert drawn == set(keys)
    svc.connect_ports(did, [{"ref": c1, "fittings": []}])  # 改图后图例跟着变
    assert "fitting:globe_valve" not in [e["key"] for e in chem_tables.legend_entries(svc.store.load(did))]
    svc2 = ToolService()  # 旧的方案流程图用例：没有代号、管件，不出图例
    did2 = svc2.create_drawing(title="t")["drawing_id"]
    svc2.place_equipment(did2, [{"symbol_id": "tank", "tag": "V-101"}, {"symbol_id": "pump", "tag": "P-101"}])
    svc2.connect_ports(did2, [{"from_port": "V-101.out", "to_port": "P-101.in", "medium": "物料"}])
    assert chem_tables.legend_entries(svc2.store.load(did2)) == []


def test_stage_detection_and_prompt():
    assert detect_stage("请画物料残液蒸馏处理系统的施工流程图") == "construction"
    assert detect_stage("画一张 P&ID") == "construction" and detect_stage("带控制点的工艺流程图") == "construction"
    assert detect_stage("物料流程图") == "material" and detect_stage("方案流程图") == "scheme"
    assert detect_stage("画工艺流程图") is None and detect_stage("RAPID 系统") is None
    args = {"title": "x"}
    Guard().normalize("create_drawing", args, [{"role": "user", "content": "画 PID"}])
    assert args["stage"] == "construction"
    args = {"title": "x", "stage": "scheme"}
    Guard().normalize("create_drawing", args, [{"role": "user", "content": "画 PID"}])
    assert args["stage"] == "scheme"  # 模型自己写了就不改
    for word in ("化工工艺流程图规范", "fittings", "add_instrument", "add_stream_table", "section_no", "ask_user"):
        assert word in SYSTEM_PROMPT


def test_guard_stream_values_and_params():
    g = Guard()
    said = [{"role": "user", "content": "组分1 流量 18.52 kg/h，摩尔分率 32.5%；出口 95℃"}]
    ok = {"rows": [{"name": "组分1", "flow_kg_h": "18.52", "mole_pct": "32.5"}]}
    assert g.check("add_stream_table", ok, said) is None
    assert g.check("add_stream_table", {"rows": [{"name": "组分1", "flow_kg_h": "20"}]}, said)["error_code"] == "E_UNSOURCED_VALUE"
    it = {"from_port": "a", "to_port": "b", "params": {"t": "120℃"}}
    assert g.check("connect_ports", {"items": [it]}, said)["error_code"] == "E_UNSOURCED_VALUE"


def test_plan_steps_and_contracts():
    plan = {"title": "t", "stage": "construction", "section_no": "06", "frame": False, "equipment": [], "connections": [],
            "instruments": [{"letters": "TI", "attach_to": "R0601", "loop_no": "0601"}],
            "stream_tables": [{"anchor": "X1.out→R0601.in", "rows": []}], "repeats": [{"name": "s", "members": ["V0605A"]}]}
    steps = plan_to_steps(plan)
    assert steps[0][0]["arguments"] == {"title": "t", "stage": "construction", "section_no": "06", "frame": False}
    assert [c["name"] for c in steps[3]] == ["repeat_system", "add_instrument", "add_stream_table"]
    tools = {t["name"]: t for t in json.loads((ROOT / "01_项目文档" / "接口约定" / "tools.schema.json").read_text(encoding="utf-8"))}
    assert {"add_instrument", "add_stream_table", "repeat_system", "migrate_valves"} <= set(tools)
    assert {"stage", "section_no", "tag_style"} <= set(tools["create_drawing"]["input_schema"]["properties"])
    conn = json.dumps(tools["connect_ports"]["input_schema"], ensure_ascii=False)
    for k in ("pipe_no", "od", "wall", "material", "grade", "temp_code", "insulation_code", "buried", "existing", "fittings", "params"):
        assert f'"{k}"' in conn, k
