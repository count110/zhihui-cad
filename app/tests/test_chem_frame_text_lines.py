"""化工工艺流程图规范化 第 1 步：阶段、图幅分区、加长、流程图标题栏（文章图 2-3）、字体（GB/T 14691）、图线（GB/T 17450）。"""

import ezdxf
import pytest

from zhihui.agent.llm import DEMO_PLAN
from zhihui.cad import standards as stds
from zhihui.cad.dxf_io import build_doc, read_dxf, write_dxf
from zhihui.ir import Connection, Drawing, Equipment
from zhihui.tools.core import ToolService


def demo(**kw):
    svc = ToolService()
    did = svc.create_drawing(title="规范化", **kw)["drawing_id"]
    for r in (svc.place_equipment(did, DEMO_PLAN["equipment"]), svc.connect_ports(did, DEMO_PLAN["connections"]), svc.auto_layout(did)):
        assert r["ok"], r
    return svc, did


def test_title_block_grid_matches_article_fig_2_3():
    """流程图标题栏：列宽 20/25/15/15/45/25/35 = 180，行高 13/6/6/18/7 = 50（文章图 2-3，逐一比对格线）。"""
    from zhihui.cad.titleblock import build, load

    tpl = load("hg_flow")
    assert tpl["size"] == [180, 50] and sum(tpl["columns_mm"]) == 180 and sum(tpl["rows_mm"]) == 50
    d = Drawing(id="t", stage="material", sheet_no=2, sheet_total=3)
    t = build(d)
    xs = t.xs()
    assert [round(x, 3) for x in xs] == [0, 20, 45, 60, 75, 120, 145, 180]  # 竖格线
    ys = sorted({round(y, 3) for y in t.ys()})
    assert {0, 7, 25, 31, 37, 50} <= set(ys)  # 右半：13 / 6 / 6 / 18 / 7
    assert {13, 19} <= set(ys)  # 左半签字区 4 行 × 6
    texts = {c.text or c.placeholder for c in t.cells}
    assert {"职责", "签字", "日期", "设计", "绘图", "校核", "审核", "年限", "比例", "设计项目", "设计阶段"} <= texts
    assert {"物料流程图", "第 2 张", "共 3 张", "（单位名称）", "（工程名称）", "t-R0"} <= texts  # 设计阶段按 stage；张次；图号默认按图纸 id
    by = {c.key: t.box(c) for c in t.cells if c.key}
    assert by["unit_name"] == (0, 37, 120, 50) and by["project"] == (120, 37, 180, 50)
    assert by["drawing_name"] == (60, 7, 120, 37) and by["drawing_no"] == (120, 7, 180, 25)
    assert by["stage"] == (145, 25, 180, 31) and by["sheet_no"] == (120, 0, 145, 7) and by["sheet_total"] == (145, 0, 180, 7)


def test_text_heights_series_and_line_spacing():
    """GB/T 14691：字高只取系列值、汉字 ≥ 3.5、更大按 √2；B 型：行距 b2=1.5h / b3=1.3h，字间距 0.2h，词间距 0.6h；字母数字斜体 75°。"""
    gb = stds.load("gb_general")
    assert gb.heights == [1.8, 2.5, 3.5, 5, 7, 10, 14, 20]
    assert all(b / a == pytest.approx(2 ** 0.5, rel=0.04) for a, b in zip(gb.heights[2:], gb.heights[3:]))  # 3.5 以上按 √2
    assert gb.line_pitch(5) == pytest.approx(7.5) and gb.line_pitch(5, caps_only=True) == pytest.approx(6.5)
    assert gb.spacing("a") == pytest.approx(0.2) and gb.spacing("e") == pytest.approx(0.6) and gb.spacing("d") == pytest.approx(0.1)
    assert gb.text_width("A B", 5) == pytest.approx(5 * (0.7 + 0.6 + 0.7))  # 字母 0.7h（0.5h 字宽 + 0.2h 间距），空格 0.6h
    svc, did = demo()
    doc = build_doc(svc.store.load(did))
    d = svc.store.load(did)
    from zhihui.cad.dxf_io import LATIN_STYLE, TEXT_STYLE

    for t in doc.modelspace().query("TEXT"):
        h = t.dxf.height / d.scale
        assert any(abs(h - v) < 1e-6 for v in gb.heights), t.dxf.text
        if stds.is_cjk(t.dxf.text):
            assert h >= 3.5 and t.dxf.style == TEXT_STYLE and t.dxf.get("oblique", 0) == 0  # 汉字正体
        else:
            assert t.dxf.style == LATIN_STYLE and t.dxf.oblique == pytest.approx(15)  # 字母数字斜体，与水平线成 75°
    assert stds.load("gb_general").raw["text"]["latin_type"] == "B"  # 同一图样只用一种型式


def test_linetype_elements_read_back_from_dxf(tmp_path):
    """GB/T 17450 表 3：读回 DXF 线型定义，画 12d、短间隔 3d、长画 24d，d 为所在图层线宽。"""
    svc, did = demo(stage="construction")
    d = svc.store.load(did)
    write_dxf(d, tmp_path / "a.dxf")
    doc = ezdxf.readfile(tmp_path / "a.dxf")
    std = stds.for_drawing(d)
    for layer in ("ZH-虚线", "ZH-中心线", "ZH-双点画线", "ZH-管道-主物料埋地", "ZH-管道-仪表", "ZH-管道-原有"):
        lt = doc.layers.get(layer).dxf.linetype
        dw = doc.layers.get(layer).dxf.lineweight / 100
        pat = list(doc.linetypes.get(lt).simplified_line_pattern())
        no = lt[3:5]
        assert pat[0] == pytest.approx((24 if no in ("04", "05") else 12) * dw), layer  # 长画 24d / 画 12d
        assert pat[1] == pytest.approx(3 * dw), layer  # 短间隔 3d
    assert std.lineweight("thick") == 2 * std.lineweight("medium") == pytest.approx(4 * std.lineweight("thin"), abs=0.03)  # 4:2:1（按系列取整）
    assert all(v in std.raw["lines"]["allowed"] for v in std.raw["lines"]["group"].values())


def test_dashed_lines_meet_at_dashes():
    """GB/T 17450 第 5.2.1 条：虚线框四个角、中心线十字交点落在画线上（程序按相位拆开）。"""
    from zhihui.cad.dashsplit import split

    box = split([(0, 0), (60, 0), (60, 30), (0, 30)], [12, -3], 0.1, closed=True)
    corners = {(0, 0), (60, 0), (60, 30), (0, 30)}
    inner = {tuple(p) for pc in box for p in pc[1:-1]}
    assert corners <= inner  # 每个角都是某一段画线中间的折点
    cross = split([(0, -40), (0, 40)], [24, -3, 0, -3], 0.05, cuts=((0, 0),))
    assert any(pc[0][1] < 0 < pc[-1][1] for pc in cross)  # 十字交点在一段长画中间
    assert cross[0][0] == (0, -40) and cross[-1][-1] == (0, 40)  # 首末端是线段而不是点（文章第 4 页）


def test_pipe_layers_buried_existing_instrument_and_tracing(tmp_path):
    """文章表 2-5：埋地主物料 虚线 b、原有管道 双点画线 b、仪表管道 细虚线；伴热画成带短钩的复合线（GB/T 17450 3.3.4）。读回不丢属性。"""
    svc = ToolService()
    did = svc.create_drawing(title="管道", stage="construction")["drawing_id"]
    items = [{"symbol_id": "tank", "tag": "V0101", "name": "原料罐"}, {"symbol_id": "pump", "tag": "P0101", "name": "进料泵"},
             {"symbol_id": "tower", "tag": "T0101", "name": "精馏塔"}]
    assert svc.place_equipment(did, items)["ok"]
    r = svc.connect_ports(did, [{"from_port": "V0101.out", "to_port": "P0101.in", "buried": True},
                                {"from_port": "P0101.out", "to_port": "T0101.feed", "existing": True, "tracing": "C"}])
    assert r["ok"], r
    assert svc.connect_ports(did, [{"from_port": "T0101.top_out", "to_port": "V0101.in", "tracing": "Z"}])["error_code"] == "E_BAD_ARG"
    assert svc.auto_layout(did)["ok"]
    d = svc.store.load(did)
    write_dxf(d, tmp_path / "p.dxf")
    msp = ezdxf.readfile(tmp_path / "p.dxf").modelspace()
    layers = {e.dxf.layer for e in msp.query("LWPOLYLINE") if e.has_xdata("ZHIHUI") and e.get_xdata("ZHIHUI")[0][1] in ("conn", "conn_part")}
    assert {"ZH-管道-主物料埋地-分段", "ZH-管道-原有-分段"} <= layers
    assert sum(1 for e in msp if e.has_xdata("ZHIHUI") and e.get_xdata("ZHIHUI")[0][1] == "tracing") > 3
    d2, rep = read_dxf(tmp_path / "p.dxf")
    assert rep.topology_consistent and not rep.texts
    got = {c.id: (c.buried, c.existing, c.tracing) for c in d2.connections}
    assert sorted(got.values()) == [(False, True, "C"), (True, False, "")]
    assert all(len(c.path) == len(o.path) for c, o in zip(d2.connections, d.connections))  # 拆开的虚线读回仍是原路径


def test_stage_frame_zones_and_find_by_zone():
    """阶段：方案流程图可不画图框，物料 / 施工流程图必须画；A2 及以上自动画图幅分区和对中符号，find_entities 能按「C3 区」查。"""
    svc = ToolService()
    assert svc.create_drawing(stage="material", frame=False)["error_code"] == "E_BAD_ARG"
    assert svc.create_drawing(section_no="6")["error_code"] == "E_BAD_ARG"  # 工段号两位数字
    svc2, did = demo(stage="scheme", frame=False)
    doc = build_doc(svc2.store.load(did))
    assert not [e for e in doc.modelspace() if e.dxf.layer == "ZH-图框"] and len([e for e in doc.modelspace() if e.dxf.layer == "ZH-标题栏"]) == 1  # 只有纸边线
    svc3, did3 = demo(paper_size="A2")
    d = svc3.store.load(did3)
    doc = build_doc(d)
    zone_texts = [e.dxf.text for e in doc.modelspace().query("TEXT") if e.has_xdata("ZHIHUI") and e.get_xdata("ZHIHUI")[0][1] == "zone"]
    assert "1" in zone_texts and "A" in zone_texts and len(zone_texts) % 2 == 0
    marks = [e for e in doc.modelspace().query("LINE") if e.has_xdata("ZHIHUI") and e.get_xdata("ZHIHUI")[0][1] == "center_mark"]
    assert len(marks) == 4
    from zhihui.cad.zones import grid, zone_of

    xs, ys = grid(d)
    assert (len(xs) - 1) % 2 == 0 and (len(ys) - 1) % 2 == 0
    assert all(25 <= (b - a) / d.scale <= 75 for a, b in zip(xs, xs[1:]))  # 每格 25～75 mm
    pump = d.get_equipment("F-201")
    z = zone_of(d, pump.x, pump.y)
    r = svc3.find_entities(did3, query=f"{z}区的风机")
    assert r["candidates"] and r["candidates"][0]["ref"] == "F-201" and r["candidates"][0]["zone"] == z
    assert all(c.get("zone") == z for c in svc3.find_entities(did3, zone=z)["candidates"])


def test_long_flow_extends_paper_by_quarters_instead_of_rows():
    """流程过长：不再折成多排，按每次加长图样宽度的 1/4 加长幅面（文章第 5 页）；加长到上限还不够提示分张。"""
    svc = ToolService()
    did = svc.create_drawing(title="长流程")["drawing_id"]
    eq = [{"symbol_id": "tank", "tag": f"V01{i:02d}", "name": f"罐{i}"} for i in range(1, 12)]
    assert svc.place_equipment(did, eq)["ok"]
    conns = [{"from_port": f"V01{i:02d}", "to_port": f"V01{i + 1:02d}"} for i in range(1, 11)]
    assert svc.connect_ports(did, conns)["ok"]
    r = svc.auto_layout(did)
    assert r["ok"], r
    d = svc.store.load(did)
    assert d.paper_size.startswith("A3+") and r["rows"] == 1
    ys = {round(e.y) for e in d.equipment}
    assert len(ys) == 1  # 一排
    from zhihui.cad.layout import LayoutError, auto_layout

    many = Drawing(id="m", paper_size="A4")
    many.equipment = [Equipment(id=f"E{i}", tag=f"V{i:04d}", symbol_id="tank") for i in range(40)]
    many.connections = [Connection(id=f"C{i}", from_equip=f"E{i}", from_port="out", to_equip=f"E{i + 1}", to_port="in") for i in range(39)]
    with pytest.raises(LayoutError) as ex:
        auto_layout(many)
    assert ex.value.code == "E_LAYOUT_TOO_LONG" and "分张" in ex.value.hint
