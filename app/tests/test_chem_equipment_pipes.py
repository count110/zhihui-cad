"""化工流程图规范化 第 2 步：设备图例与标注（文章表 2-4、图 2-4、2.1/2.2 节），管道画法与标注（文章 3.1/3.2 节，表 2-6～2-10，图 2-5～2-7）。"""

import ezdxf
import pytest

from zhihui.agent.guards import Guard
from zhihui.cad import chem
from zhihui.cad.codes import lookup, medium_code, pipe_label, table
from zhihui.cad.dxf_io import read_dxf, write_dxf
from zhihui.cad.symbols import get_symbol, load_symbols, symbol_box
from zhihui.cad.tagrow import GAP_BAND, content_top, layout_tag_row, tag_band
from zhihui.ir import Connection
from zhihui.tools.core import ToolService

# 文章表 2-4：每类按图例补全（已有的图元在这里只核对类别）
TABLE_2_4 = {
    "T": ["packed_tower", "tower", "spray_tower"],
    "R": ["fixed_bed_reactor", "tubular_reactor", "fluidized_bed_reactor", "reactor"],
    "E": ["heat_exchanger_simple", "heat_exchanger", "u_tube_exchanger", "floating_head_exchanger", "kettle_reboiler", "double_pipe_exchanger",
          "evaporator", "cooler", "air_cooler"],
    "C": ["fan", "rotary_compressor_h", "rotary_compressor_v", "recip_compressor", "recip_compressor_4"],
    "P": ["pump", "water_ring_vacuum_pump", "gear_pump", "piston_pump", "submerged_pump"],
    "V": ["horizontal_vessel", "horizontal_vessel_boot", "vessel", "demister_separator", "cyclone", "esp", "tank", "gas_holder", "sphere_tank"],
    "F": ["box_furnace", "furnace"],
}


def _xd(e):
    return [v for _, v in e.get_xdata("ZHIHUI")] if e.has_xdata("ZHIHUI") else []


def test_table_2_4_symbols_complete():
    syms = load_symbols()
    for cat, ids in TABLE_2_4.items():
        for sid in ids:
            s = syms[sid]
            assert s.category == cat, sid
            assert s.source, sid
            x0, y0, x1, y1 = s.bbox
            for p in s.ports:  # 端口落在外框上，引出方向朝外
                assert (abs(p.x - x0) < 1e-6 and p.dir == "left") or (abs(p.x - x1) < 1e-6 and p.dir == "right") \
                    or (abs(p.y - y0) < 1e-6 and p.dir == "down") or (abs(p.y - y1) < 1e-6 and p.dir == "up"), (sid, p)
    article = [s for s in syms.values() if s.status == "article"]
    assert len(article) >= 22 and all("未与标准原文核对" in s.source or s.id.startswith("offsheet") for s in article)
    assert {"聚合釜", "冷却器", "纳氏泵", "球罐", "箱式炉"} <= {a for s in syms.values() for a in s.aliases}


def test_code_tables_transcribed_from_article():
    """文章表 2-6～2-10 转录，抽查（与第 10～12 页扫描图逐项对过）。"""
    spot = [
        ("material_codes", "CW", "循环冷却水上水"), ("material_codes", "HW", "循环冷却水回水"), ("material_codes", "LS", "低压蒸汽"),
        ("material_codes", "PL", "工艺液体"), ("material_codes", "PG", "工艺气体"), ("material_codes", "VT", "放空气"),
        ("material_codes", "SC", "蒸汽冷凝水"), ("material_codes", "CWR", "冷冻盐水回水"), ("material_codes", "VE", "真空排放气"),
        ("material_codes", "DR", "排液、排水"), ("material_codes", "TS", "伴热蒸汽"),
        ("pipe_material", "B", "碳钢"), ("pipe_material", "E", "不锈钢"), ("pipe_material", "H", "衬里及内防腐"),
        ("insulation", "2", "保温"), ("insulation", "7", "防止表面冷凝（低于 15℃）"),
    ]
    for tab, code, name in spot:
        assert lookup(tab, code)["name"] == name, (tab, code)
    assert len(table("material_codes")["codes"]) == 44
    assert lookup("temp_range", "C")["range"] == "21～70" and lookup("temp_range", "K") == {"range": "94～650", "min_c": 94, "max_c": 650, "pipe": "不锈钢管"}
    assert lookup("heat_tracing", "D")["medium"] == "蒸汽" and lookup("heat_tracing", "R")["medium"] == "水"
    for t in ("material_codes", "pipe_material", "temp_range", "insulation", "heat_tracing"):
        assert table(t)["verified"] is False and "未与标准原文核对" in table(t)["_source"]


def test_medium_code_and_pipe_label_format():
    assert medium_code("循环冷却水上水") == ("CW", ["CW"])
    assert medium_code("低压蒸汽")[0] == "LS" and medium_code("去冷凝器的工艺气体")[0] == "PG"
    assert medium_code("循环冷却水") == ("", ["CW", "HW"])  # 上水还是回水说不清：不猜
    assert medium_code("蒸汽") == ("", ["LS", "MS", "HS"]) and medium_code("进料") == ("", [])
    c = Connection(id="C1", from_equip="E1", from_port="a", to_equip="E2", to_port="b",
                   pipe_no="CW0601", od="32", wall="3.5", material="A", grade="A", temp_code="C", insulation_code="6")
    assert pipe_label(c) == "CW0601-32×3.5A/A-C6"  # 文章图 2-7
    assert pipe_label(c.model_copy(update={"grade": "", "temp_code": "", "insulation_code": "", "material": "B", "od": "57"})) == "CW0601-57×3.5B"
    assert pipe_label(c.model_copy(update={"pipe_no": "LS0601", "od": "", "wall": "", "material": "", "grade": "", "temp_code": "", "insulation_code": ""})) == "LS0601"
    assert pipe_label(c, size_only=True) == "32×3.5A"


def _svc(**kw):
    svc = ToolService()
    did = svc.create_drawing(title="t", **kw)["drawing_id"]
    return svc, did


def test_hg_tag_numbering_no_reuse_and_same_units():
    """位号 = 分类号 + 工段号 + 序号 + 尾号（文章图 2-4，V0408A）；删掉的序号不复用；相同设备尾号 A、B；不给工段号退回 P-101。"""
    svc, did = _svc(section_no="04")
    r = svc.place_equipment(did, [{"symbol_id": "vessel", "name": "受槽"}, {"symbol_id": "pump", "name": "泵"}, {"symbol_id": "vessel", "name": "贮槽",
                                   "params": {"units": 2}}])
    assert [e["ref"] for e in r["equipment"]] == ["V0401", "P0401", "V0402AB"]
    r = svc.place_equipment(did, [{"symbol_id": "vessel", "same_as": "V0401", "name": "受槽"}])
    d = svc.store.load(did)
    assert {e.tag for e in d.equipment} == {"V0401A", "V0401B", "P0401", "V0402AB"}
    svc.connect_ports(did, [{"from_port": "V0402AB.out", "to_port": "P0401.in"}, {"from_port": "P0401.out", "to_port": "V0401A.in"}])
    r = svc.auto_layout(did)
    d = svc.store.load(did)
    assert {e.tag for e in d.equipment} == {"V0401AB", "V0402A", "V0402B", "P0401"} and r["renamed_tags"]  # 按流程顺序重排后定下来
    assert not d.auto_tags
    svc.edit_equipment(did, "V0402A", delete=True)
    svc.place_equipment(did, [{"symbol_id": "vessel", "name": "新槽"}])
    assert svc.store.load(did).equipment[-1].tag == "V0403"  # 不复用，接在后面
    svc, did = _svc(section_no="06")
    svc.place_equipment(did, [{"symbol_id": "tank", "tag": "V0605"}, {"symbol_id": "tank"}])
    assert svc.store.load(did).equipment[-1].tag == "V0606"  # 用户给的位号也占序号
    svc, did = _svc()  # 没给工段号（只能来自用户）：旧格式
    assert svc.place_equipment(did, [{"symbol_id": "pump"}])["equipment"][0]["ref"] == "P-101"


def _construction(tmp_path):
    svc, did = _svc(paper_size="A2", stage="construction", section_no="06")
    assert svc.place_equipment(did, [
        {"symbol_id": "reactor", "tag": "R0601", "name": "反应蒸馏釜", "params": {"feature": "F=110m²", "insulation": "2"}},
        {"symbol_id": "heat_exchanger_simple", "tag": "E0601", "name": "冷凝器", "params": {"feature": "F=60m²"}},
        {"symbol_id": "vessel", "tag": "V0605", "name": "真空受槽", "params": {"tracing": "C"}},
    ])["ok"]
    r = svc.connect_ports(did, [
        {"from_sheet": {"sheet": "5", "ref": "V0606"}, "to_port": "R0601.in", "medium": "物料残液", "od": "57", "wall": "3.5", "material": "B"},
        {"from_port": "R0601.out", "to_port": "E0601.in", "medium": "工艺气体", "od": "57", "wall": "3.5", "material": "B"},
        {"from_port": "E0601.out", "to_port": "V0605.in", "medium": "工艺液体", "od": "32", "wall": "3.5", "material": "B"},
        {"from_port": "V0605.out", "to_sheet": {"sheet": "6", "ref": "V0610"}, "medium": "工艺液体", "od": "32", "wall": "3.5", "material": "B"},
        {"from_sheet": {"sheet": "5", "ref": "P0601"}, "to_port": "E0601.shell_in", "medium": "循环冷却水", "line_type": "utility"},
    ])
    assert r["ok"], r
    assert any("CW/HW" in n for n in r["notes"])  # 循环冷却水有歧义：不自动填代号，提示 ask_user
    assert svc.repeat_system(did, "真空受槽系统", ["V0605"], "V0605B")["ok"]
    assert svc.auto_layout(did)["ok"]
    d = svc.store.load(did)
    write_dxf(d, tmp_path / "c.dxf")
    return svc, did, d, ezdxf.readfile(tmp_path / "c.dxf").modelspace()


def test_construction_pipe_labels_offsheet_ground_nozzles(tmp_path):
    svc, did, d, msp = _construction(tmp_path)
    nos = [c.pipe_no for c in d.connections]
    assert nos[:4] == ["PL0601", "PG0601", "PL0602", "PL0603"] and nos[4] == ""  # 物料代号 + 工段号 + 管段序号；代号不明的不编
    texts = {t.dxf.text: t for t in msp.query("TEXT")}
    assert "PG0601-57×3.5B" in texts and "PL0602-32×3.5B" in texts  # 文章图 2-7 格式
    assert texts["PG0601-57×3.5B"].dxf.height == pytest.approx(3.5 * d.scale)  # 字母数字 2.5 号，A2 升一档
    # 图纸接续：空心箭头（文章图 2-5）在图的左方 / 右方，箭头里写图号，旁边写来去位号
    arrows = {e.tag: e for e in d.equipment if e.symbol_id in chem.OFFSHEET}
    xs = [e.x for e in d.equipment if e.symbol_id not in chem.OFFSHEET]
    assert arrows["V0606"].x < min(xs) and arrows["P0601"].x < min(xs) and arrows["V0610"].x > max(xs)
    x0, y0, x1, y1 = symbol_box(arrows["V0606"], d.unit)
    assert ((x1 - x0) / d.unit, (y1 - y0) / d.unit) == pytest.approx((13.0, 6.0))  # 图元按 A3 设计，随图幅放大
    assert get_symbol("offsheet").bbox == pytest.approx((-6.5, -3.0, 6.5, 3.0))  # 10 + 3 长、6 高（A3 图纸 mm）
    assert {t.dxf.text for t in msp.query("TEXT") if _xd(t)[:1] == ["offsheet"]} == {"5", "6"}
    # 地平线 + EL±0.000、管口、隔热层、伴热管、相同系统方框
    kinds = [_xd(e)[0] for e in msp if _xd(e)]
    assert "EL±0.000" in texts and kinds.count("nozzle") >= 4
    assert "insulation" in kinds and "tracing" in kinds and "repeat" in kinds
    assert {"真空受槽系统", "V0605B"} <= set(texts)
    gl = chem.ground_line(d)
    assert gl[0] <= min(min(symbol_box(e, d.unit)[1] for e in d.equipment), min(p[1] for c in d.connections for p in c.path))
    # 特性参数（物料流程图及以上）写在名称下方，² 用带这个字形的样式
    assert texts["F=110m²"].dxf.style == "ZH_SYM"
    d2, rep = read_dxf(tmp_path / "c.dxf")
    assert rep.topology_consistent and not rep.texts
    assert [c.pipe_no for c in d2.connections] == nos


def test_tag_row_floats_above_equipment_band(tmp_path):
    svc, did, d, msp = _construction(tmp_path)
    band = tag_band(d)
    assert band[1] - content_top(d) == pytest.approx(GAP_BAND * d.scale, abs=1.0)  # 紧贴设备带 10 mm（规范化要求 5.7 的 8～12 mm）
    assert any(it.feature == "F=60m²" for it in layout_tag_row(d))
    d.stage = "scheme"
    assert not any(it.feature for it in layout_tag_row(d))  # 方案流程图不写特性参数


def test_vertical_label_rotated_and_long_pipe_arrows(tmp_path):
    """竖向管线的标注写在左侧、字头向左（旋转 90°）；长管线中间补箭头（文章 3.1、3.2 节）。"""
    svc, did, d, msp = _construction(tmp_path)
    rotated = [t for t in msp.query("TEXT") if _xd(t)[:1] == ["conn_label"] and abs(t.dxf.rotation - 90) < 1e-6]
    for t in rotated:
        c = d.get_connection(_xd(t)[1])
        x = t.dxf.align_point[0]
        assert any(abs(a[0] - b[0]) < 1e-6 and x < a[0] for a, b in zip(c.path, c.path[1:]))  # 在某条竖段左侧
    from zhihui.ir import Drawing, Equipment

    dd = Drawing(id="x")
    dd.equipment += [Equipment(id="E1", tag="V-1", symbol_id="tank", x=2000, y=8000), Equipment(id="E2", tag="V-2", symbol_id="tank", x=19000, y=8000)]
    c = Connection(id="C1", from_equip="E1", from_port="out", to_equip="E2", to_port="in", path=[(3000, 7500), (18000, 7500)])
    assert len(chem.mid_arrows(dd, c)) == int(15000 // (100 * dd.scale))  # 每满 100 mm 补一个


def test_guard_covers_new_pipe_fields():
    g = Guard()
    said = [{"role": "user", "content": "循环冷却水上水管 CW0601，外径 32 壁厚 3.5，不锈钢；接第 5 张图"}]
    ok = {"from_port": "a", "to_port": "b", "pipe_no": "CW0601", "od": "32", "wall": "3.5", "material": "E", "from_sheet": {"sheet": "5"}}
    assert g.check("connect_ports", {"items": [ok]}, said) is None  # 不锈钢 → E 算用户给的
    assert g.check("connect_ports", {"items": [{**ok, "od": "57"}]}, said)["error_code"] == "E_UNSOURCED_VALUE"
    assert g.check("connect_ports", {"items": [{**ok, "material": "B"}]}, said)["error_code"] == "E_UNSOURCED_VALUE"
    assert g.check("connect_ports", {"items": [{**ok, "from_sheet": {"sheet": "7"}}]}, said)["error_code"] == "E_UNSOURCED_VALUE"
    assert g.check("place_equipment", {"items": [{"symbol_id": "vessel", "params": {"tracing": "C"}}]}, said)["error_code"] == "E_UNSOURCED_VALUE"
    assert g.check("create_drawing", {"section_no": "04"}, said)["error_code"] == "E_UNSOURCED_VALUE"


def test_bad_codes_rejected():
    svc, did = _svc(stage="construction", section_no="06")
    svc.place_equipment(did, [{"symbol_id": "tank", "tag": "V0601"}, {"symbol_id": "pump", "tag": "P0601"}])
    assert svc.connect_ports(did, [{"from_port": "V0601.out", "to_port": "P0601.in", "material": "Z"}])["error_code"] == "E_BAD_ARG"
    assert svc.connect_ports(did, [{"from_port": "V0601.out", "to_port": "P0601.in", "od": "DN50"}])["error_code"] == "E_BAD_ARG"
    assert svc.place_equipment(did, [{"symbol_id": "tank", "params": {"insulation": "X"}}])["error_code"] == "E_BAD_ARG"
    r = svc.connect_ports(did, [{"from_port": "V0601.out", "to_port": "P0601.in", "medium": "工艺液体"}])
    cid = r["connections"][0]["ref"]
    assert svc.connect_ports(did, [{"ref": cid, "od": "57", "wall": "3.5", "material": "B"}])["ok"]  # 改标注字段不动拓扑
    c = svc.store.load(did).get_connection(cid)
    assert (c.pipe_no, c.od, c.material) == ("PL0601", "57", "B")
