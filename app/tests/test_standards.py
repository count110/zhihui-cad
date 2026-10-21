"""制图标准配置（S1）与字体修正（S2）。"""

import json

import ezdxf
import numpy as np
import pytest

from zhihui.agent.llm import DEMO_PLAN
from zhihui.cad import standards as stds
from zhihui.ir import Drawing
from zhihui.tools.core import ToolService


def test_load_and_inherit():
    gb, hg, w = stds.load("gb_general"), stds.load("hg_pfd"), stds.load("water_supply")
    assert hg.id == "hg_pfd" and hg.get("extends") == "gb_general"
    assert hg.paper_sizes() == gb.paper_sizes()  # 没写的沿用通用
    assert hg.lineweight("thick") == 0.7 and gb.lineweight("thick") == 1.0  # 行业标准覆盖
    assert gb.lineweight("thick") / gb.lineweight("medium") == 2 and gb.lineweight("medium") / gb.lineweight("thin") == 2
    assert w.get("title_block") == "water_institute" and w.text_height("name") == gb.text_height("name")
    assert stds.for_drawing(Drawing(id="x")).id == "hg_pfd"
    assert stds.for_drawing(Drawing(id="x", template_id="water_elevation")).id == "water_supply"
    assert stds.for_drawing(Drawing(id="x", standard="gb_general")).id == "gb_general"


def test_user_standard_overrides(tmp_path, monkeypatch):
    for f in stds.STD_DIR.glob("*.json"):
        (tmp_path / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "my_company.json").write_text(json.dumps(
        {"id": "my_company", "extends": "hg_pfd", "text": {"roles": {"name": 5}}, "paper": {"binding": True}}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(stds, "STD_DIR", tmp_path)
    stds.load.cache_clear()
    try:
        my = stds.load("my_company")
        assert my.text_height("name") == 5 and my.text_height("tag") == 2.5
        assert my.lineweight("thick") == 0.7  # 继承两层：my_company → hg_pfd → gb_general
        assert my.margins("A3") == (25, 5, 5, 5)
        assert "my_company" in stds.available()
    finally:
        stds.load.cache_clear()


def test_dxf_font_option_shx(tmp_path, monkeypatch):
    """文字样式字体是标准配置里的选项：默认 TrueType 仿宋，text.dxf_font = "shx" 换成 gbenor.shx + gbcbig.shx；自检按配置判断。"""
    from zhihui.cad.dxf_io import build_doc, dxf_font
    from zhihui.cad.standards_check import run

    assert dxf_font(stds.load("hg_pfd")) == ("simfang.ttf", "")
    for f in stds.STD_DIR.glob("*.json"):
        (tmp_path / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "hg_shx.json").write_text(json.dumps({"id": "hg_shx", "extends": "hg_pfd", "text": {"dxf_font": "shx"}}), encoding="utf-8")
    monkeypatch.setattr(stds, "STD_DIR", tmp_path)
    stds.load.cache_clear()
    try:
        svc = ToolService()
        did = svc.create_drawing(title="SHX", standard="hg_shx")["drawing_id"]
        for r in (svc.place_equipment(did, DEMO_PLAN["equipment"]), svc.connect_ports(did, DEMO_PLAN["connections"]), svc.auto_layout(did)):
            assert r["ok"], r
        d = svc.store.load(did)
        st = build_doc(d).styles.get("ZH_CN")
        assert (st.dxf.font, st.dxf.bigfont, st.dxf.width) == ("gbeitc.shx", "gbcbig.shx", 0.7)  # B 型斜体 SHX + 大字体（规范化要求 3.5）
        font = next(x for x in run(d)["items"] if x["id"] == "font")
        assert font["status"] == "pass" and "gbcbig.shx" in font["details"][0]
    finally:
        stds.load.cache_clear()


def test_text_heights_series_and_paper_steps():
    gb = stds.load("gb_general")
    assert [gb.text_height("name", p) for p in ("A4", "A3", "A2", "A1", "A0")] == [3.5, 3.5, 5, 5, 7]
    assert gb.text_height("tag", "A3", "P-101") == 2.5 and gb.text_height("tag", "A3", "泵") == 3.5  # 汉字不小于 3.5
    assert gb.fit_height("drawing_name", "A3", "很长很长很长很长很长很长很长的图名", 66) in gb.heights
    assert gb.fit_height("drawing_name", "A3", "很长很长很长很长很长很长很长很长很长很长很长的图名", 30) == 3.5  # 不低于汉字最小字高


def _texts(path):
    doc = ezdxf.readfile(path)
    msp = doc.modelspace()
    out = [(t.dxf.text, t.dxf.height) for t in msp.query("TEXT")]
    for ins in msp.query("INSERT"):
        out += [(a.dxf.text, a.dxf.height) for a in ins.attribs if not (a.dxf.flags & 1)]
        out += [(v.dxf.text, v.dxf.height) for v in ins.virtual_entities() if v.dxftype() == "TEXT"]
    return doc, out


@pytest.mark.parametrize("case", ["G001", "W001"])
def test_all_text_heights_compliant(case, tmp_path):
    from tests.test_water import build_plan
    from zhihui.bench.run import load_cases
    from zhihui.cad.dxf_io import write_dxf

    _, d = build_plan(load_cases()[case]["plan"])
    write_dxf(d, tmp_path / "a.dxf")
    doc, texts = _texts(tmp_path / "a.dxf")
    gb = stds.load("gb_general")
    style = doc.styles.get("ZH_CN")
    assert (style.dxf.font, style.dxf.get("bigfont", ""), style.dxf.width) == ("simfang.ttf", "", 0.7)  # 默认 TrueType 仿宋
    for text, h in texts:
        paper = round(h / d.scale, 3)
        assert paper in gb.heights, f"{text!r} 字高 {paper} 不在字高系列"
        if stds.is_cjk(text):
            assert paper >= 3.5, f"{text!r} 是汉字，字高 {paper} < 3.5"


def test_scale_validation():
    svc = ToolService()
    bad = svc.create_drawing(scale=48)
    assert bad["error_code"] == "E_BAD_ARG" and "1:40" in bad["hint"] and "1:50" in bad["hint"]
    for ok in (1, 50, 150, 250, 0.5):
        assert svc.create_drawing(scale=ok)["ok"], ok
    v = svc.create_drawing(template_id="water_elevation", vscale=6.75)
    assert v["error_code"] == "E_BAD_ARG" and "1:150" in v["hint"]
    assert svc.create_drawing(template_id="water_elevation", vscale=10)["ok"]  # 1:100
    assert svc.create_drawing(paper_size="A3x3")["ok"] and svc.create_drawing(standard="nope")["error_code"] == "E_BAD_ARG"


def test_elongated_paper_and_binding_margins():
    from zhihui.cad.symbols import inner_frame

    d = Drawing(id="x", paper_size="A3x3", scale=50)
    assert d.frame_size() == (891 * 50, 420 * 50) and d.unit == 50  # 加长幅面按 A3 取图元大小
    assert inner_frame(d) == (500, 500, 881 * 50, 410 * 50)  # A3 不留装订边 e = 10
    b = Drawing(id="y", paper_size="A2", scale=50, binding=True)
    assert inner_frame(b) == (25 * 50, 10 * 50, 584 * 50, 410 * 50)  # A2 留装订边 a = 25、c = 10


def _inner_ink(png) -> float:
    import matplotlib.pyplot as plt

    img = plt.imread(png)[..., :3].mean(axis=2)
    dark = img < 0.5
    ys, xs = np.nonzero(dark)
    box = dark[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    h, w = box.shape
    return float(box[h // 6: -h // 6, w // 6: -w // 6].mean())


@pytest.mark.parametrize("ch", ["图", "国", "回"])
def test_fangsong_glyphs_not_filled(ch, tmp_path):
    """仿宋预览：「图」等字以前会被填实（中间墨量 0.44），修好后与「国」「回」一样（约 0.2）。"""
    from zhihui.cad.dxf_io import TEXT_STYLE, _setup
    from zhihui.cad.render import render

    doc = ezdxf.new("R2018")
    _setup(doc)
    doc.modelspace().add_text(ch, height=50, dxfattribs={"style": TEXT_STYLE, "width": 0.7})
    doc.saveas(tmp_path / "t.dxf")
    render(tmp_path / "t.dxf", tmp_path / "t.png", "A4", dpi=60)
    assert _inner_ink(tmp_path / "t.png") < 0.32


# ---------------------------------------------------------------- S3 图线 / S7 图层

def _export(case, tmp_path):
    from tests.test_water import build_plan
    from zhihui.bench.run import load_cases
    from zhihui.cad.dxf_io import write_dxf

    _, d = build_plan(load_cases()[case]["plan"])
    write_dxf(d, tmp_path / f"{case}.dxf")
    return d, ezdxf.readfile(tmp_path / f"{case}.dxf")


@pytest.mark.parametrize("case", ["G001", "W001"])
def test_linetypes_ltscale_and_layer_rules(case, tmp_path):
    d, doc = _export(case, tmp_path)
    assert doc.header["$LTSCALE"] == d.scale and doc.header["$INSUNITS"] == 4
    std = stds.for_drawing(d)
    thin = std.lw100("thin")
    # 每个虚线类图层按自己的线宽 d 生成线型（GB/T 17450 第 4.3 条表 3）：虚线 画 12d 隔 3d，点画线 长画 24d 隔 3d 点 隔 3d
    assert doc.layers.get("ZH-虚线").dxf.linetype == f"ZH_02_d{thin:03d}" and doc.layers.get("ZH-中心线").dxf.linetype == f"ZH_04_d{thin:03d}"
    dw = thin / 100
    assert list(doc.linetypes.get(f"ZH_02_d{thin:03d}").simplified_line_pattern()) == pytest.approx([12 * dw, 3 * dw])
    assert list(doc.linetypes.get(f"ZH_04_d{thin:03d}").simplified_line_pattern()) == pytest.approx([24 * dw, 3 * dw, 0, 3 * dw])
    thick = std.lw100("thick")
    assert list(doc.linetypes.get(f"ZH_02_d{thick:03d}").simplified_line_pattern()) == pytest.approx([12 * thick / 100, 3 * thick / 100])  # 埋地管道
    assert {"CENTER", "DASHED", "PHANTOM"} <= {lt.dxf.name for lt in doc.linetypes}  # 旧名保留，兼容
    weights = {doc.layers.get(n).dxf.lineweight for n in std.raw["layers"]["table"]}
    assert weights <= {std.lw100(k) for k in ("thick", "medium", "thin")}  # 只用本标准线宽组的三档
    color_lw = {}
    for layer in doc.layers:  # 颜色与线宽一一对应
        if layer.dxf.name.startswith("ZH-"):
            color_lw.setdefault(layer.dxf.color, set()).add(layer.dxf.lineweight)
    assert all(len(v) == 1 for v in color_lw.values()), color_lw
    for e in doc.modelspace():  # 0 层不放实体；颜色、线型、线宽都随层
        assert e.dxf.layer != "0", e.dxftype()
        assert e.dxf.get("color", 256) == 256 and e.dxf.get("linetype", "BYLAYER").upper() == "BYLAYER" and e.dxf.get("lineweight", -1) == -1, (e.dxftype(), e.dxf.layer)
        if e.dxftype() == "INSERT":
            assert all(a.dxf.layer == e.dxf.layer for a in e.attribs)
    for blk in doc.blocks:  # 块里的图元都在 0 层
        if blk.name.startswith("ZH_"):
            assert all(v.dxf.layer == "0" for v in blk), blk.name


@pytest.mark.parametrize("case", ["G001", "W001"])
def test_no_overlaps_or_close_parallels(case, tmp_path):
    from zhihui.cad.linework import check

    d, doc = _export(case, tmp_path)
    r = check(doc, d.scale)
    assert r["overlaps"] == [] and r["close_parallels"] == [], r


def test_parallel_gap_and_overlap_priority():
    from zhihui.cad.dxf_io import _setup
    from zhihui.cad.linework import check

    doc = ezdxf.new("R2018", setup=True)
    _setup(doc, Drawing(id="x"))
    msp = doc.modelspace()
    msp.add_line((0, 0), (100, 0), dxfattribs={"layer": "ZH-细实线"})
    msp.add_line((0, 0.5), (100, 0.5), dxfattribs={"layer": "ZH-细实线"})  # 0.5 mm < 0.7 mm
    msp.add_line((0, 10), (100, 10), dxfattribs={"layer": "ZH-细实线"})
    msp.add_line((0, 11), (100, 11), dxfattribs={"layer": "ZH-细实线"})  # 1 mm，合格
    msp.add_line((0, 20), (100, 20), dxfattribs={"layer": "ZH-中心线"})
    msp.add_line((30, 20), (60, 20), dxfattribs={"layer": "ZH-粗实线"})  # 与中心线重合
    r = check(doc, 1.0)
    assert [round(x["gap_mm"], 2) for x in r["close_parallels"]] == [0.5]
    assert len(r["overlaps"]) == 1 and r["overlaps"][0]["keep"] == "visible" and r["overlaps"][0]["length_mm"] == 30
