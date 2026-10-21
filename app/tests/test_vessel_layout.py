"""化工设备图的布局与导出（任务书 10.1）：视口比例在 GB/T 14690 系列里；表格、视口、标题栏两两不重叠且不出内框；字高合规；
PDF 从布局「设备图」渲染；DXF 读回来，布局里能找到标题栏图名和管口表的符号。"""
import copy

import ezdxf
import pytest

from gen_helpers import Env, close
from zhihui.agent.general.prompts import EQUIP_FEW_SHOTS
from zhihui.cad import standards as STD
from zhihui.cad.equipment import check as CHK
from zhihui.cad.geom import entities as GE
from zhihui.cad.geom import layout as LYT

STD_SCALES = LYT.STANDARD_SCALES + LYT.SECOND_SCALES


def shot(i: int) -> dict:
    return copy.deepcopy(EQUIP_FEW_SHOTS[i][1]["workflow_steps"][0]["payload"]["args"]["spec"])


def build(tmp_path, spec: dict, **kw):
    env = Env(tmp_path)
    did = env.new("hg_equipment_a1")
    env.call("vessel_define", did, op="create", spec=spec, **kw)
    return env, did, env.d(did)


CASES = {
    "vertical_a1": (0, {}),
    "horizontal_a1": (5, {}),
    "vertical_a0": (0, {"sheet": "A0"}),
    "vertical_a2": (0, {"sheet": "A2"}),
    "horizontal_a0": (5, {"sheet": "A0"}),
    "horizontal_a2": (5, {"sheet": "A2"}),
}


def make(tmp_path, name: str):
    idx, kw = CASES[name]
    return build(tmp_path, shot(idx), **kw)


def ent_bbox(e):
    if e.kind == "text":
        p, h = e.data["p"], float(e.data["height"])
        w = STD.text_width(e.data["text"], h, float(e.data.get("width_factor", 0.7)))
        al = e.data.get("align", "left")
        x0 = p[0] - (w if al == "right" else w / 2 if al in ("center", "middle") else 0.0)
        return (x0, p[1], x0 + w, p[1] + h)
    return GE.entity_bbox(e)


def union(bbs):
    bbs = [b for b in bbs if b]
    return (min(b[0] for b in bbs), min(b[1] for b in bbs), max(b[2] for b in bbs), max(b[3] for b in bbs))


def overlap(a, b, tol=0.05) -> bool:
    return a[0] < b[2] - tol and b[0] < a[2] - tol and a[1] < b[3] - tol and b[1] < a[3] - tol


def inside(a, b, tol=0.05) -> bool:
    return a[0] >= b[0] - tol and a[1] >= b[1] - tol and a[2] <= b[2] + tol and a[3] <= b[3] + tol


def paper_group(d, prefix: str, layout="设备图"):
    return [e for e in d.entities if e.space == layout and e.id.startswith(prefix) and "equip:auto" in e.tags]


# ------------------------------------------------------------------ 比例
@pytest.mark.parametrize("name", list(CASES))
def test_viewport_scales_are_in_gb_t_14690_series_and_locked(tmp_path, name):
    env, did, d = make(tmp_path, name)
    lay = next(l for l in d.layouts if l["name"] == "设备图")
    assert len(lay["viewports"]) == 2
    for vp in lay["viewports"]:
        assert vp["scale"] in STD_SCALES
        assert vp["locked"] is True
        assert LYT.viewport_scale(vp) == vp["scale"]                     # 视口高度 / view_height 与比例一致
    rec = d.vessel.drawing["record"]
    assert [v["scale"] for v in rec["viewports"]] == [v["scale"] for v in lay["viewports"]]
    assert lay["viewports"][0]["scale"] == f"1:{rec['den_main']:g}"
    assert lay["viewports"][1]["scale"] == f"1:{rec['den_end']:g}"
    assert not any(e["code"] == "E_SCALE_NONSTANDARD" for e in CHK.run(d.vessel, d)["errors"])
    # 标题栏里的比例写主视图比例
    assert f"1:{rec['den_main']:g}" in {e.data["text"] for e in paper_group(d, "EQ-P-TB-") if e.kind == "text"}


def test_user_scale_is_kept_and_nonstandard_scale_is_refused(tmp_path):
    env, did, d = build(tmp_path, shot(0), scale="1:20")
    rec = d.vessel.drawing["record"]
    assert close(rec["den_main"], 20.0)
    assert d.vessel.sources["drawing.scale"].startswith("user:")
    assert next(l for l in d.layouts if l["name"] == "设备图")["viewports"][0]["scale"] == "1:20"
    r = env.err("vessel_define", did, op="patch", scale="1:7")
    assert r["error_code"] == "NONSTANDARD_SCALE"
    assert env.d(did).vessel.drawing["scale"] == "1:20"             # 失败后参数不变
    r = env.call("vessel_define", did, op="patch", scale="1:15")      # 第二系列允许
    assert next(l for l in env.d(did).layouts if l["name"] == "设备图")["viewports"][0]["scale"] == "1:15"


def test_default_scale_is_the_largest_priority_scale_that_fits(tmp_path):
    env, did, d = build(tmp_path, shot(0))
    rec = d.vessel.drawing["record"]
    assert close(rec["den_main"], 10.0)                              # A1 上 1:5 放不下，1:10 放得下
    assert rec["den_main"] in (1.0, 2.0, 5.0, 10.0, 20.0, 50.0, 100.0, 200.0)


# ------------------------------------------------------------------ 表格、视口、标题栏不重叠、不出框
@pytest.mark.parametrize("name", list(CASES))
def test_tables_viewports_title_block_do_not_overlap_and_stay_inside_frame(tmp_path, name):
    env, did, d = make(tmp_path, name)
    rec = d.vessel.drawing["record"]
    inner = rec["inner"]
    rects = [(v["id"], v["rect"]) for v in rec["viewports"]] + [(t["name"], t["rect"]) for t in rec["tables"]] + [("标题栏", rec["title_block"])]
    assert len(rects) == 7
    for n, r in rects:
        assert inside(r, inner), n
    for i, (n1, r1) in enumerate(rects):
        for n2, r2 in rects[i + 1:]:
            assert not overlap(r1, r2), (n1, n2)
    assert CHK.run(d.vessel, d)["errors"] == []


@pytest.mark.parametrize("name", ["vertical_a1", "horizontal_a1", "vertical_a2"])
def test_recorded_rectangles_match_the_drawn_paper_space_entities(tmp_path, name):
    """record 里的矩形不是自说自话：图纸空间里实际画出来的线和文字都在各自的矩形里，且各表之间不重叠。"""
    env, did, d = make(tmp_path, name)
    rec = d.vessel.drawing["record"]
    prefix = {"明细栏": "EQ-P-TBL-PARTS-", "管口表": "EQ-P-TBL-NOZ-", "设计数据表": "EQ-P-TBL-DESIGN-", "技术要求": "EQ-P-TECH-"}
    actual = {}
    for t in rec["tables"]:
        es = paper_group(d, prefix[t["name"]])
        assert es, t["name"]
        actual[t["name"]] = union(ent_bbox(e) for e in es)
        assert inside(actual[t["name"]], t["rect"], 0.5), (t["name"], actual[t["name"]], t["rect"])
    tb = union(ent_bbox(e) for e in paper_group(d, "EQ-P-TB-"))
    assert inside(tb, rec["title_block"], 0.5) and inside(tb, rec["inner"], 0.5)
    names = list(actual)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            assert not overlap(actual[a], actual[b]), (a, b)
        for v in rec["viewports"]:
            assert not overlap(actual[a], v["rect"]), (a, v["id"])
        assert not overlap(actual[a], tb)
    # 图框
    fr = union(ent_bbox(e) for e in paper_group(d, "EQ-P-FRAME-"))
    assert inside(rec["inner"], fr, 0.5)


@pytest.mark.parametrize("name", ["vertical_a1", "horizontal_a1", "vertical_a0", "horizontal_a2"])
def test_every_model_entity_is_inside_its_viewport_window(tmp_path, name):
    env, did, d = make(tmp_path, name)
    lay = next(l for l in d.layouts if l["name"] == "设备图")
    wins = {}
    for vp in lay["viewports"]:
        (x0, y0), (x1, y1) = vp["corners"]
        sc = float(vp["view_height"]) / (y1 - y0)                       # 模型单位 / 图纸 mm
        cx, cy = vp["target_center"]
        wins[vp["equip_view"]] = (cx - (x1 - x0) / 2 * sc, cy - (y1 - y0) / 2 * sc, cx + (x1 - x0) / 2 * sc, cy + (y1 - y0) / 2 * sc)
    for e in d.entities:
        if e.space != "Model" or "equip:auto" not in e.tags:
            continue
        view = next(t.split(":")[1] for t in e.tags if t.startswith("equip_view:"))
        bb = ent_bbox(e)
        assert bb and inside(bb, wins[view], 1e-6), (e.id, bb, wins[view])


def test_main_view_and_end_view_viewports_are_aligned_for_horizontal_vessel(tmp_path):
    """卧式：左视图与主视图按投影关系对齐（设备轴线同高）。"""
    env, did, d = build(tmp_path, shot(5))
    lay = next(l for l in d.layouts if l["name"] == "设备图")
    ax = []
    for vp, view in zip(lay["viewports"], ("M", "L")):
        (x0, y0), (x1, y1) = vp["corners"]
        sc = float(vp["view_height"]) / (y1 - y0)
        lines = [e for e in d.entities if f"equip_view:{view}" in e.tags and "equip_part:AXIS" in e.tags and e.kind == "line"]
        horizontal = next(e for e in lines if close(e.data["p1"][1], e.data["p2"][1], 1e-9))      # 水平的那条设备轴线（左视图有十字）
        y_model = horizontal.data["p1"][1]
        ax.append(y0 + (y_model - (vp["target_center"][1] - (y1 - y0) / 2 * sc)) / sc)            # 轴线在图纸上的高度
    assert close(ax[0], ax[1], 1e-6)


# ------------------------------------------------------------------ 表格文字不出格、字高合规
def columns_of(d, prefix, rect):
    xs = {rect[0], rect[2]}
    for e in paper_group(d, prefix):
        if e.kind == "line" and "table_rule" in e.tags and close(e.data["p1"][0], e.data["p2"][0], 1e-9):
            xs.add(round(e.data["p1"][0], 6))
    return sorted(xs)


@pytest.mark.parametrize("name", ["vertical_a1", "horizontal_a1", "vertical_a2", "horizontal_a2"])
def test_table_cell_text_stays_inside_its_column(tmp_path, name):
    env, did, d = make(tmp_path, name)
    rec = d.vessel.drawing["record"]
    prefix = {"明细栏": "EQ-P-TBL-PARTS-", "管口表": "EQ-P-TBL-NOZ-", "设计数据表": "EQ-P-TBL-DESIGN-"}
    for t in rec["tables"]:
        if t["name"] not in prefix:
            continue
        rect = t["rect"]
        cols = columns_of(d, prefix[t["name"]], rect)
        assert len(cols) >= 5
        for e in paper_group(d, prefix[t["name"]]):
            if e.kind != "text" or "table_cell" not in e.tags:
                continue
            x0, y0, x1, y1 = ent_bbox(e)
            right = min(c for c in cols if c > e.data["p"][0] + 1e-6)
            assert x1 <= right + 1e-6, (t["name"], e.data["text"], x1, right)
            assert rect[1] - 1e-6 <= y0 and y1 <= rect[3] + 1e-6, (t["name"], e.data["text"])


def test_table_headers_are_not_split_in_the_middle_of_a_word(tmp_path):
    env, did, d = make(tmp_path, "vertical_a1")
    texts = {e.data["text"] for e in d.entities if e.space == "设备图" and "table_cell" in e.tags}
    for w in ("尺寸DN", "压力PN", "质量kg", "型式", "形式", "设备中心线"):
        assert w in texts, w
    assert "g" not in texts and "k" not in texts


@pytest.mark.parametrize("name", list(CASES))
def test_text_heights_meet_gb_t_14691(tmp_path, name):
    env, did, d = make(tmp_path, name)
    rec = d.vessel.drawing["record"]
    assert not any(e["code"] == "E_TEXT_HEIGHT" for e in CHK.run(d.vessel, d)["errors"])
    std = STD.load("gb_general")
    n = 0
    for e in d.entities:
        if e.kind != "text" or "equip:auto" not in e.tags:
            continue
        h = float(e.data["height"])
        if e.space == "Model":
            view = next(t.split(":")[1] for t in e.tags if t.startswith("equip_view:"))
            h /= rec["den_main"] if view == "M" else rec["den_end"]
        assert h >= std.min_height(e.data["text"]) - 1e-6, (e.id, h)
        n += 1
    assert n > 100


def test_check_reports_text_that_is_too_small(tmp_path):
    env, did, d = make(tmp_path, "vertical_a1")
    e = next(x for x in d.entities if x.kind == "text" and x.space == "设备图" and "table_cell" in x.tags)
    e.data["height"] = 2.0
    errs = CHK.run(d.vessel, d)["errors"]
    assert any(x["code"] == "E_TEXT_HEIGHT" for x in errs)


def test_check_reports_overlap_nonstandard_scale_and_out_of_frame(tmp_path):
    env, did, d = make(tmp_path, "vertical_a1")
    sp = d.vessel.model_copy(deep=True)
    rec = sp.drawing["record"]
    rec["viewports"][1]["rect"] = list(rec["viewports"][0]["rect"])
    rec["viewports"][0]["scale"] = "1:7"
    rec["tables"][0]["rect"] = [rec["inner"][0] - 5, rec["inner"][1], rec["inner"][2], rec["inner"][3]]
    codes = {e["code"] for e in CHK.run(sp, d)["errors"]}
    assert {"E_VIEW_OVERLAP", "E_SCALE_NONSTANDARD"} <= codes


def test_too_small_sheet_is_reported_by_the_check_and_blocks_delivery_files(tmp_path):
    """A3 放不下主视图 + 右侧三张表：不静默出错图，自检报 E_VIEW_OVERLAP，DXF/PDF 导出被拦（预览照常）。"""
    env, did, d = build(tmp_path, shot(0), sheet="A3")
    codes = {e["code"] for e in CHK.run(d.vessel, d)["errors"]}
    assert "E_VIEW_OVERLAP" in codes
    r = env.err("vessel_generate_drawing", did, export=["dxf", "pdf"])
    assert r["error_code"] == "E_VESSEL_CHECK"
    r = env.call("vessel_generate_drawing", did, export=["png"])
    assert r["data"]["files"][0]["source"] == "layout:设备图"


# ------------------------------------------------------------------ 导出：DXF 和 PDF
@pytest.mark.parametrize("name,title,marks", [("vertical_a1", "立式储罐设备图（方案图）", {"a", "b1", "b2", "c", "M1"}), ("horizontal_a1", "卧式储罐设备图（方案图）", {"a", "b", "M1"})])
def test_export_dxf_and_pdf_and_read_the_layout_back(tmp_path, name, title, marks):
    env, did, d = make(tmp_path, name)
    r = env.call("vessel_generate_drawing", did, export=["dxf", "pdf"])
    files = {f["format"]: f for f in r["data"]["files"]}
    assert set(files) == {"dxf", "pdf"}
    assert files["pdf"]["source"] == "layout:设备图"
    assert files["dxf"]["source"] == "model+layouts"
    pdf = env.svc.store.resolve_ref(files["pdf"]["file_ref"])
    assert pdf.read_bytes()[:5] == b"%PDF-" and pdf.stat().st_size > 10000
    dxf = env.svc.store.resolve_ref(files["dxf"]["file_ref"])
    doc = ezdxf.readfile(dxf)
    assert "设备图" in [l.name for l in doc.layouts]
    lay = doc.layouts.get("设备图")
    texts = [e.dxf.text if e.dxftype() == "TEXT" else e.text for e in lay if e.dxftype() in ("TEXT", "MTEXT")]
    assert title in texts                                                  # 标题栏图名
    assert {"管口表", "设计数据表", "技术要求"} <= set(texts)
    assert marks <= set(texts)                                             # 管口表的符号列
    rec = d.vessel.drawing["record"]
    assert f"1:{rec['den_main']:g}" in texts                               # 标题栏比例
    # 布局里的视口：比例和视口高度与参数一致
    vps = [v for v in lay.query("VIEWPORT") if v.dxf.get("view_height") and v.dxf.view_height < 1e5]
    heights = sorted(round(v.dxf.view_height, 3) for v in vps)
    lay_json = next(l for l in d.layouts if l["name"] == "设备图")
    for v in lay_json["viewports"]:
        assert round(float(v["view_height"]), 3) in heights
    # 模型空间的尺寸：标注样式按视口比例带 dimscale
    dims = list(doc.modelspace().query("DIMENSION"))
    assert len(dims) == len([e for e in d.entities if e.kind == "dimension" and "equip:auto" in e.tags])
    st = doc.dimstyles.get(f"EQ_1_{rec['den_main']:g}")
    assert st is not None and close(st.dxf.dimscale, rec["den_main"])


def test_png_and_svg_exports_are_rendered_from_the_layout(tmp_path):
    env, did, d = make(tmp_path, "vertical_a1")
    r = env.call("vessel_generate_drawing", did, export=["png", "svg"])
    fs = {f["format"]: f for f in r["data"]["files"]}
    assert fs["png"]["source"] == "layout:设备图" and fs["svg"]["source"] == "layout:设备图"
    png = env.svc.store.resolve_ref(fs["png"]["file_ref"]).read_bytes()
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert "<svg" in env.svc.store.resolve_ref(fs["svg"]["file_ref"]).read_text(encoding="utf-8")[:2000]


# ------------------------------------------------------------------ 管口方位图单独成一张布局
@pytest.mark.parametrize("idx,view", [(0, "O"), (5, "L")])
def test_orientation_layout_has_viewport_table_and_title_without_overlap(tmp_path, idx, view):
    env, did, d = build(tmp_path, shot(idx))
    r = env.call("vessel_generate_drawing", did, views=["orientation"])
    d = env.d(did)
    assert [l["name"] for l in d.layouts] == ["设备图", "管口方位图"]
    lay = next(l for l in d.layouts if l["name"] == "管口方位图")
    assert len(lay["viewports"]) == 1
    vp = lay["viewports"][0]
    assert vp["scale"] in STD_SCALES and vp["locked"] and vp["equip_view"] == view
    assert LYT.viewport_scale(vp) == vp["scale"]
    (x0, y0), (x1, y1) = vp["corners"]
    ix0, iy0, ix1, iy1 = LYT.inner_frame(LYT.paper_size("ISO_A2"), True)
    assert inside((x0, y0, x1, y1), (ix0, iy0, ix1, iy1))
    tbl = union(ent_bbox(e) for e in paper_group(d, "EQ-Q-TBL-NOZ-", "管口方位图"))
    tb = union(ent_bbox(e) for e in paper_group(d, "EQ-Q-TB-", "管口方位图"))
    assert inside(tbl, (ix0, iy0, ix1, iy1), 0.5) and inside(tb, (ix0, iy0, ix1, iy1), 0.5)
    assert not overlap((x0, y0, x1, y1), tbl) and not overlap((x0, y0, x1, y1), tb) and not overlap(tbl, tb)
    # 视口窗口能装下整个方位图/左视图
    sc = float(vp["view_height"]) / (y1 - y0)
    cx, cy = vp["target_center"]
    win = (cx - (x1 - x0) / 2 * sc, cy - (y1 - y0) / 2 * sc, cx + (x1 - x0) / 2 * sc, cy + (y1 - y0) / 2 * sc)
    for e in d.entities:
        if e.space == "Model" and f"equip_view:{view}" in e.tags:
            assert inside(ent_bbox(e), win, 1e-6), e.id
    # 管口表里是全部管口
    first_col = sorted(d.vessel.drawing["record"]["orientation_layout"]["nozzle_rows"])
    assert first_col == sorted(n.mark for n in d.vessel.nozzles)
    texts = {e.data["text"] for e in paper_group(d, "EQ-Q-TBL-NOZ-", "管口方位图") if e.kind == "text"}
    assert set(first_col) <= texts
    assert any("管口方位图" in e.data["text"] for e in paper_group(d, "EQ-Q-TB-", "管口方位图") if e.kind == "text")
    assert CHK.run(d.vessel, d)["errors"] == []


def test_orientation_layout_is_exported_to_dxf(tmp_path):
    env, did, d = build(tmp_path, shot(0))
    env.call("vessel_generate_drawing", did, views=["orientation"])
    r = env.call("vessel_generate_drawing", did, export=["dxf"])
    doc = ezdxf.readfile(env.svc.store.resolve_ref(r["data"]["files"][0]["file_ref"]))
    names = [l.name for l in doc.layouts]
    assert "设备图" in names and "管口方位图" in names
    texts = [e.dxf.text if e.dxftype() == "TEXT" else e.text for e in doc.layouts.get("管口方位图") if e.dxftype() in ("TEXT", "MTEXT")]
    assert any("管口方位图" in t for t in texts) and {"a", "b1", "b2", "c", "M1"} <= set(texts)
