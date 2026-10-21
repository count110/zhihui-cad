"""S6 表格、标题栏、一览表、位号；S8 输出（多页 PDF、可搜索文字、DXF 完整性）。"""

import re

import ezdxf
import pytest

from zhihui.agent.llm import DEMO_PLAN
from zhihui.cad.dxf_io import read_dxf, write_dxf
from zhihui.cad.table import Cell, Table, from_template, validate_template
from zhihui.tools.core import ToolService


def demo(**kw):
    svc = ToolService()
    did = svc.create_drawing(title="一览表", **kw)["drawing_id"]
    for r in (svc.place_equipment(did, DEMO_PLAN["equipment"]), svc.connect_ports(did, DEMO_PLAN["connections"]), svc.auto_layout(did)):
        assert r["ok"], r
    return svc, did


def test_table_merged_cells_draw_no_inner_lines():
    """合并单元格内部不画格线；外框粗、内格细；与图框重合的边不画；同一直线上的段并成一条。"""
    t = Table(cols=[10, 20, 30], rows=[8, 8, 8], cells=[Cell(0, 0, "合并", rs=2, cs=2), Cell(2, 2, "右下")], skip_edges=("right",))
    segs = t.segments()
    inner_h = [(a, b) for a, b, outer in segs if not outer and a[1] == b[1]]
    assert not any(a[1] == 16.0 and a[0] < 30.0 for a, b in inner_h)  # 合并格（前两行 × 前两列）中间那条横线 y=16 不画
    assert ((30.0, 16.0), (60.0, 16.0)) in inner_h  # 合并格右边那一列照常有横线
    assert ((0.0, 8.0), (60.0, 8.0)) in inner_h  # 合并格下边：左右两段在一条直线上，并成一条
    assert not any(outer and a[0] == b[0] == 60.0 for a, b, outer in segs)  # 右边与图框重合，不画
    assert any(outer and a == (0.0, 24.0) and b == (60.0, 24.0) for a, b, outer in segs)  # 顶边一条到底
    with pytest.raises(ValueError):
        Table(cols=[10], rows=[8], cells=[Cell(0, 0, "a"), Cell(0, 0, "b")]).owner()  # 单元格重叠


def test_title_block_template_placeholder_and_user_upload(tmp_path):
    """标题栏 JSON 模板：空字段显示格子说明；用户上传自己的模板；坏模板报错。"""
    svc, did = demo()
    msp = _msp(svc, did, tmp_path)
    texts = {e.dxf.text for e in msp.query("TEXT")}
    assert "（单位名称）" in texts and "（工程名称）" in texts and "一览表" in texts  # 流程图标题栏按文章图 2-3；图名 = 图纸标题
    assert "方案流程图" in texts and "第 1 张" in texts and "共 1 张" in texts  # 设计阶段按 stage 自动填；第 张 共 张
    mine = {"size": [120, 30], "cells": [
        {"x": [0, 30], "y": [15, 30], "text": "公司"}, {"x": [30, 120], "y": [15, 30], "field": "unit_name", "placeholder": "（公司名称）"},
        {"x": [0, 30], "y": [0, 15], "text": "图名"}, {"x": [30, 120], "y": [0, 15], "field": "drawing_name", "role": "drawing_name"}],
        "auto": {"drawing_name": "title"}, "required": ["drawing_name"]}
    did2 = svc.create_drawing(title="自定义", title_template=mine, title_block={"unit_name": "某某设计院"})["drawing_id"]
    for r in (svc.place_equipment(did2, DEMO_PLAN["equipment"][:2]), svc.auto_layout(did2)):
        assert r["ok"], r
    texts2 = {e.dxf.text for e in _msp(svc, did2, tmp_path).query("TEXT")}
    assert {"公司", "某某设计院", "自定义"} <= texts2 and "（单位名称）" not in texts2
    bad = svc.create_drawing(title="x", title_template={"size": [120, 30], "cells": [{"x": [0, 200], "y": [0, 10], "text": "a"}]})
    assert bad["error_code"] == "E_BAD_ARG"
    assert validate_template(mine) == []
    t = from_template(mine, {"unit_name": ""})
    assert next(c for c in t.cells if c.key == "unit_name").placeholder == "（公司名称）"


def _msp(svc, did, tmp_path):
    d = svc.store.load(did)
    write_dxf(d, tmp_path / f"{did}.dxf")
    return ezdxf.readfile(tmp_path / f"{did}.dxf").modelspace()


def test_equipment_schedule_merges_same_spec_and_leaves_missing_blank(tmp_path):
    """设备一览表：按流程顺序；同名同规格并成一行（数量 2、位号 P-101A/B）；没给的规格、材料留空，不编造。"""
    from zhihui.cad.schedule import equipment_rows, pipe_rows

    svc = ToolService()
    did = svc.create_drawing(title="泵组")["drawing_id"]
    items = [{"symbol_id": "tank", "tag": "V-101", "name": "原料罐"},
             {"symbol_id": "pump", "tag": "P-101A", "name": "进料泵", "params": {"规格型号": "Q=10m3/h H=30m", "材料": "304"}},
             {"symbol_id": "pump", "tag": "P-101B", "name": "进料泵", "params": {"规格型号": "Q=10m3/h H=30m", "材料": "304"}},
             {"symbol_id": "tower", "tag": "T-101", "name": "精馏塔"}]
    assert svc.place_equipment(did, items)["ok"]
    conns = [{"from_port": "V-101.out", "to_port": "P-101A.in"}, {"from_port": "V-101.out", "to_port": "P-101B.in"},
             {"from_port": "P-101A.out", "to_port": "T-101.feed"}, {"from_port": "P-101B.out", "to_port": "T-101.feed", "medium": "原料"}]
    assert svc.connect_ports(did, conns)["ok"] and svc.auto_layout(did)["ok"]
    d = svc.store.load(did)
    rows = equipment_rows(d)
    assert rows == [["1", "V-101", "原料罐", "", "", "1", ""],
                    ["2", "P-101A/B", "进料泵", "Q=10m3/h H=30m", "304", "2", ""],
                    ["3", "T-101", "精馏塔", "", "", "1", ""]]
    pr = pipe_rows(d)
    assert [r[0] for r in pr] == ["L-01", "L-02", "L-03", "L-04"] and all(r[1] == "" for r in pr)  # DN 没给就空
    assert pr[0][3].startswith("V-101")
    assert svc.add_schedule(did, "equipment")["ok"]
    texts = {e.dxf.text for e in _msp(svc, did, tmp_path).query("TEXT")}
    assert {"设备一览表", "规格型号", "P-101A/B"} <= texts
    d2, rep = read_dxf(tmp_path / f"{did}.dxf")
    assert rep.topology_consistent and not rep.texts  # 表格是装饰，不当自由文字
    assert d2.schedules == svc.store.load(did).schedules


def test_auto_tags_numbered_in_flow_order():
    """没给位号的设备：按流程顺序编号（同类从 101 起），用户给的位号不动、不重号。"""
    svc = ToolService()
    did = svc.create_drawing(title="自动位号")["drawing_id"]
    items = [{"symbol_id": "pump", "name": "回流泵"}, {"symbol_id": "tank", "name": "原料罐"},
             {"symbol_id": "pump", "name": "进料泵"}, {"symbol_id": "pump", "tag": "P-101", "name": "备用泵"}]
    r = svc.place_equipment(did, items)
    assert r["ok"], r
    tags = [e["ref"] for e in r["equipment"]]
    assert len(set(tags)) == 4 and tags[3] == "P-101"
    conns = [{"from_port": f"{tags[1]}.out", "to_port": f"{tags[2]}.in"}, {"from_port": f"{tags[2]}.out", "to_port": f"{tags[0]}.in"}]
    assert svc.connect_ports(did, conns)["ok"]
    lay = svc.auto_layout(did)
    assert lay["ok"]
    d = svc.store.load(did)
    by_name = {e.name: e.tag for e in d.equipment}
    assert by_name == {"原料罐": "V-101", "进料泵": "P-102", "回流泵": "P-103", "备用泵": "P-101"}  # 流程：原料罐 → 进料泵 → 回流泵
    assert svc.edit_equipment(did, by_name["回流泵"], new_tag="P-201")["ok"]
    reflux = next(e.id for e in d.equipment if e.name == "回流泵")
    assert reflux not in svc.store.load(did).auto_tags  # 用户改过的位号以后不再自动重编


def test_export_multipage_pdf_searchable_and_dxf_tables(tmp_path):
    """S8：一览表单独一页时 PDF 为多页、真实图幅；文字可搜索（不是曲线）；DXF 有文字样式、标注样式、线型、$INSUNITS。"""
    svc, did = demo()
    assert svc.add_schedule(did, "pipe", "sheet")["ok"]
    r = svc.export_artifacts(did)
    assert r["ok"], r
    fmts = [f["format"] for f in r["files"]]
    assert fmts.count("dxf_sheet") == 1 and "pdf" in fmts
    pdf = svc.store.resolve_ref(next(f["file_ref"] for f in r["files"] if f["format"] == "pdf"))
    raw = pdf.read_bytes()
    boxes = re.findall(rb"/MediaBox \[\s*0 0 ([\d.]+) ([\d.]+)\s*\]", raw)
    assert len(boxes) == 2  # 两页：本图 + 管道一览表
    assert all((round(float(w) / 72 * 25.4), round(float(h) / 72 * 25.4)) == (420, 297) for w, h in boxes)  # 真实 A3 图幅
    assert b"/CIDFontType2" in raw and b"/ToUnicode" in raw  # 嵌入 TrueType 字体 + Unicode 映射：文字可搜索、可复制
    try:
        import pymupdf
    except ImportError:
        return
    doc = pymupdf.open(pdf)
    assert doc.page_count == 2
    assert "布袋除尘器" in doc[0].get_text() and "管道一览表" in doc[1].get_text()
    dxf = ezdxf.readfile(svc.store.resolve_ref(next(f["file_ref"] for f in r["files"] if f["format"] == "dxf")))
    assert dxf.header["$INSUNITS"] == 4 and dxf.header["$LTSCALE"] == svc.store.load(did).scale
    assert {"CENTER", "DASHED", "PHANTOM"} <= {lt.dxf.name for lt in dxf.linetypes}
    st = dxf.styles.get("ZH_CN")
    assert st.dxf.font.lower() == "simfang.ttf" and not st.dxf.get("bigfont", "")  # 默认 TrueType 仿宋，查看器不缺字体
    assert "GB_DIM" in dxf.dimstyles  # 没有尺寸也写好标注样式


def test_dwg_needs_oda(monkeypatch):
    """可选 DWG：没装 ODA File Converter 时给明确的错误，不影响其他格式。"""
    from zhihui.cad import dwg

    monkeypatch.setattr(dwg, "find_oda", lambda: None)
    svc, did = demo()
    r = svc.export_artifacts(did, ["dxf", "dwg"])
    assert r["error_code"] == "E_NO_ODA" and "DXF" in r["hint"]
    assert svc.export_artifacts(did, ["dxf"])["ok"]
