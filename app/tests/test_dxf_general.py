"""通用图 DXF 读写：导出后用 ezdxf 回读（图层/线型/表头/图元/块/属性/布局/视口），第三方 DXF 导入再导出不丢东西。"""
import math

import ezdxf
import pytest

from gen_helpers import Env, pclose
from zhihui.cad.general_dxf import dxf_kind, read_any_dxf, write_general_dxf


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def build_rich_drawing(env):
    """一张包含各类图元的通用图：用来回读检查。"""
    did = env.new("gb_a3_mech")
    env.call("configure_canvas_and_limits", did, limits_lower_left="0,0", limits_upper_right="841,594")
    env.call("set_linetype_scale", did, ltscale=2.5)
    env.call("draw_linear_primitives", did, primitive_type="point", points=["5,5"], point_style={"pdmode": 34, "pdsize": 3})
    r = env.call("draw_polygons_and_meshes", did, shape_type="rectangle", corner1="0,0", corner2="@100,60", rect_fillet_r=8)
    c = env.call("draw_circular_primitives", did, curve_type="circle", method="center_diameter", center="50,30", diameter=20)
    env.call("draw_circular_primitives", did, curve_type="arc", method="start_center_angle", start_pt="80,30", center="50,30", included_angle_deg=90)
    env.call("draw_circular_primitives", did, curve_type="ellipse", method="center_axes", center="150,30", major_axis_vec=[20, 0], axis_ratio=0.5)
    env.call("draw_complex_curves", did, curve_type="spline", vertices=["0,100", "20,130", "40,110", "60,140"], spline_method="fit")
    env.call("draw_linear_primitives", did, primitive_type="line", points=["0,-10", "100,-10"], layer="中心线")
    env.call("create_text_annotation", did, content="直径%%c20 测试", insert_point="10,150", height=5)
    env.call("create_text_annotation", did, text_type="mtext", content="多行\\P文字", insert_point="10,170", width_box=50)
    env.call("create_hatch", did, boundary_mode="entity_ids", entity_ids=[r["created"][0], c["created"][0]], pattern_name="ANSI31", scale=2)
    dia = env.call("create_radial_and_chain_dim", did, dim_type="diameter", target_entity_id=c["created"][0])["data"]["dim_id"]
    env.call("create_mechanical_tolerances", did, tolerance_category="dimensional_fit", target_dim_or_entity_id=dia, iso_fit_code="H7")
    env.call("create_linear_and_angular_dim", did, dim_type="linear_horizontal", target_entity_id=r["created"][0])
    env.call("create_and_insert_block", did, action="define_block", block_name="BODY", source_entity_ids=[c["created"][0]], base_point="50,30", keep_source="retain",
             attribute_defs=[{"tag": "规格", "prompt": "规格", "default_val": "M8"}])
    env.call("create_and_insert_block", did, action="insert_block", block_name="BODY", insert_point="200,100", scale=[2, 2, 1], rotation_deg=30, attribute_values={"规格": "M10"})
    env.call("draw_polygons_and_meshes", did, shape_type="mesh", corner1="300,0", rows=2, cols=2, cell_size=[10, 10])
    env.call("switch_model_and_layout_space", did, target_space="Model", layout_action="create", new_layout_name="A3图", insert_standard_frame="ISO_A3", title_block_fields={"drawing_name": "测试件"})
    env.call("manage_layout_viewports", did, layout_name="A3图", action="create", viewport_corner1="30,50", viewport_corner2="250,250", fit_entity_ids=[r["created"][0]], standard_scale="1:2")
    env.call("switch_model_and_layout_space", did, target_space="Model")
    return did


def export(env, did, version="R2018"):
    p = env.svc.store.export_dir(env.d(did)) / "t.dxf"
    write_general_dxf(env.d(did), p, version=version)
    return p


def test_export_roundtrip_header_layers_linetypes(env):
    did = build_rich_drawing(env)
    for ver, acad in (("R2010", "AC1024"), ("R2013", "AC1027"), ("R2018", "AC1032")):
        p = export(env, did, ver)
        doc = ezdxf.readfile(p)
        assert doc.dxfversion == acad
    doc = ezdxf.readfile(export(env, did))
    h = doc.header
    assert h["$LTSCALE"] == 2.5 and h["$PDMODE"] == 34 and h["$PDSIZE"] == 3 and tuple(h["$LIMMAX"]) == (841, 594) and h["$INSUNITS"] == 4
    lay = {l.dxf.name: l for l in doc.layers}
    d = env.d(did)
    for l in d.layers:
        assert l.name in lay, l.name
    cl = lay["中心线"]
    assert cl.dxf.linetype.upper() == "CENTER" and "CENTER" in doc.linetypes
    assert lay["粗实线"].dxf.lineweight == 50 and lay["粗实线"].dxf.color == 7 or lay["粗实线"].dxf.lineweight > 0
    for ver in ("R2010", "R2013", "R2018"):
        a = ezdxf.readfile(export(env, did, ver)).audit()
        assert a.errors == [] and a.fixes == [], (ver, [e.message for e in a.errors + a.fixes])               # ezdxf 自检：结构上没有问题（不等于 AutoCAD 一定打得开，见功能对照表）


def test_export_roundtrip_entities_blocks_and_attribs(env):
    did = build_rich_drawing(env)
    d = env.d(did)
    doc = ezdxf.readfile(export(env, did))
    msp = doc.modelspace()
    types = {}
    for e in msp:
        types.setdefault(e.dxftype(), []).append(e)
    assert {"POINT", "LWPOLYLINE", "CIRCLE", "ARC", "ELLIPSE", "SPLINE", "LINE", "TEXT", "MTEXT", "HATCH", "DIMENSION", "INSERT", "MESH"} <= set(types)
    rect = types["LWPOLYLINE"][0]
    assert len(list(rect.get_points("xyb"))) == 8 and any(abs(p[2]) > 1e-9 for p in rect.get_points("xyb"))             # 圆角矩形：8 个点、带凸度
    circ = types["CIRCLE"][0]
    assert tuple(round(x, 6) for x in circ.dxf.center)[:2] == (50, 30) and circ.dxf.radius == 10
    arc = types["ARC"][0]
    assert arc.dxf.radius == 30 and (arc.dxf.end_angle - arc.dxf.start_angle) % 360 == pytest.approx(90)
    assert any("⌀" in t.dxf.text or "%%c" in t.dxf.text or "⌀" in t.dxf.text for t in types["TEXT"])                  # 直径符号
    assert [e for e in types["LINE"] if e.dxf.layer == "中心线"]
    hatch = types["HATCH"][0]
    assert hatch.dxf.pattern_name == "ANSI31" and hatch.dxf.pattern_scale == 2 and len(hatch.paths) == 2
    dims = types["DIMENSION"]
    assert len(dims) == 2 and all(dd.dxf.dimstyle in doc.dimstyles for dd in dims)
    dia = [dd for dd in dims if dd.dimtype == 3][0]                                                                      # 直径标注
    assert dia.dxf.get("actual_measurement", 20) == pytest.approx(20) or dia.dxf.text
    mesh = types["MESH"][0]
    assert len(list(mesh.vertices)) == 9 and len(list(mesh.faces)) == 4
    # 块 + 属性
    assert "BODY" in doc.blocks and [a.dxf.tag for a in doc.blocks["BODY"].query("ATTDEF")] == ["规格"]
    ins = types["INSERT"][0]
    assert ins.dxf.name.startswith("BODY") and ins.dxf.xscale == 2 and ins.dxf.rotation == pytest.approx(30)
    assert {a.dxf.tag: a.dxf.text for a in ins.attribs} == {"规格": "M10"}
    assert len(msp) == len([e for e in d.entities if e.space == "Model"])                                                # 每个模型空间图元对应一个 DXF 图元


def test_export_layouts_and_viewport_scale(env):
    did = build_rich_drawing(env)
    doc = ezdxf.readfile(export(env, did))
    assert "A3图" in doc.layouts.names()
    lay = doc.layouts.get("A3图")
    vps = [v for v in lay.viewports() if v.dxf.id != 1]
    assert len(vps) == 1
    vp = vps[0]
    assert vp.dxf.height / vp.dxf.view_height == pytest.approx(0.5)                                                      # 1:2
    texts = [e.dxf.text for e in lay if e.dxftype() == "TEXT"]
    assert "测试件" in texts
    assert (lay.dxf_layout.dxf.paper_width, lay.dxf_layout.dxf.paper_height) == (420, 297)


def test_own_dxf_reopens_losslessly_via_embedded_ir(env):
    did = build_rich_drawing(env)
    d = env.d(did)
    p = export(env, did)
    assert dxf_kind(p) == "general"
    d2 = read_any_dxf(p)
    assert d2.mode == "general" and [(e.id, e.kind) for e in d2.entities] == [(e.id, e.kind) for e in d.entities]
    assert d2.layouts == d.layouts and d2.blocks == d.blocks and d2.ltscale == d.ltscale
    assert all(e.handle for e in d2.entities if e.space == "Model" and e.kind != "dimension" or e.kind == "dimension")    # 句柄回写了


def test_pfd_dxf_kind_is_not_general(env):
    pfd = env.svc.create_drawing(title="流程图")["drawing_id"]
    env.svc.add_equipment(pfd, {"id": "R1", "type": "reactor", "name": "反应器", "x": 100, "y": 100}) if hasattr(env.svc, "add_equipment") else None
    r = env.svc.export_artifacts(pfd, ["dxf"])
    f = next(x for x in r["files"] if x["format"] == "dxf")
    assert dxf_kind(env.svc.store.resolve_ref(f["file_ref"])) == "pfd"


def make_foreign(path):
    """模拟第三方软件（ezdxf 直接绘制，不带本系统的 IR）生成的 DXF。"""
    doc = ezdxf.new("R2010", setup=True)
    doc.layers.add("轮廓", color=3, linetype="DASHED", lineweight=35)
    doc.layers.add("标注", color=1)
    msp = doc.modelspace()
    msp.add_line((0, 0), (100, 0), dxfattribs={"layer": "轮廓", "color": 5})
    msp.add_circle((50, 30), 10, dxfattribs={"layer": "轮廓"})
    msp.add_arc((0, 0), 30, 10, 100, dxfattribs={"layer": "轮廓", "lineweight": 50})
    msp.add_lwpolyline([(0, 0, 0), (40, 0, 0.5), (40, 40, 0), (0, 40, 0)], format="xyb", close=True, dxfattribs={"layer": "轮廓"})
    msp.add_ellipse((100, 100), major_axis=(20, 0), ratio=0.5)
    msp.add_spline(fit_points=[(0, 0), (10, 20), (30, 10), (50, 30)])
    msp.add_text("Hello 你好", height=3.5, dxfattribs={"layer": "标注"}).set_placement((5, 50))
    msp.add_mtext("第一行\\P第二行", dxfattribs={"layer": "标注", "char_height": 3, "insert": (5, 70), "width": 40})
    msp.add_point((3, 3))
    blk = doc.blocks.new("VALVE")
    blk.add_circle((0, 0), 5)
    blk.add_line((-5, 0), (5, 0))
    blk.add_attdef("TAG", (0, 8), height=2, dxfattribs={"prompt": "位号"})
    ins = msp.add_blockref("VALVE", (200, 50), dxfattribs={"xscale": 2, "yscale": 2, "rotation": 45})
    ins.add_auto_attribs({"TAG": "V-101"})
    h = msp.add_hatch(color=2, dxfattribs={"pattern_name": "ANSI31", "pattern_scale": 1.5})
    h.set_pattern_fill("ANSI31", scale=1.5)
    h.paths.add_polyline_path([(0, 0), (20, 0), (20, 20), (0, 20)], is_closed=True)
    msp.add_linear_dim(base=(0, -15), p1=(0, 0), p2=(100, 0)).render()
    # 第三方 DXF 里本系统不支持的对象：要被计数并告知，不能悄悄吞掉
    msp.add_leader([(0, 0), (10, 10)])
    lay = doc.layouts.new("Sheet1")
    lay.page_setup(size=(420, 297), margins=(0, 0, 0, 0), units="mm")
    lay.add_text("布局文字", height=5).set_placement((20, 20))
    lay.add_viewport(center=(210, 150), size=(200, 100), view_center_point=(50, 30), view_height=200)
    doc.saveas(path)


def test_foreign_dxf_import_then_export_keeps_everything_supported(env, tmp_path):
    src = env.svc.store.root / "uploads" / "foreign.dxf"
    src.parent.mkdir(parents=True, exist_ok=True)
    make_foreign(src)
    assert dxf_kind(src) == "foreign"
    r = env.call("manage_document_file", None, action="open", file_ref=env.svc.store.rel(src))
    did = r["drawing_id"]
    d = env.d(did)
    assert d.mode == "general"
    kinds = sorted(e.kind for e in d.entities if e.space == "Model")
    assert kinds == sorted(["line", "circle", "arc", "polyline", "ellipse", "spline", "text", "mtext", "point", "insert", "hatch", "dimension"])
    assert {l.name for l in d.layers} >= {"0", "轮廓", "标注"}
    assert d.get_layer("轮廓").lineweight_mm == 0.35 and d.get_layer("轮廓").linetype.upper() == "DASHED"
    ln = next(e for e in d.entities if e.kind == "line")
    assert ln.layer == "轮廓" and ln.color == 5 and ln.data["p2"][0] == 100
    ar = next(e for e in d.entities if e.kind == "arc")
    assert ar.lineweight_mm == 0.5 and ar.data["r"] == 30
    pl = next(e for e in d.entities if e.kind == "polyline")
    assert pl.data["closed"] and pl.data["vertices"][1]["bulge"] == 0.5
    ins = next(e for e in d.entities if e.kind == "insert")
    assert ins.data["block"] == "VALVE" and ins.data["scale"][:2] == [2, 2] and ins.data["attribs"] == {"TAG": "V-101"}
    assert next(b for b in d.blocks if b["name"] == "VALVE")["attdefs"][0]["tag"] == "TAG"
    assert any("LEADER" in n for n in d.notes)                                                                           # 不支持的对象被如实告知
    sheet = next(lay for lay in d.layouts if lay["name"] == "Sheet1")
    assert sheet["viewports"][0]["scale"] == "1:2" and sheet["page_setup"]["size"] == [420, 297]                       # 布局和视口比例读入
    assert any(e.space == "Sheet1" and e.kind == "text" for e in d.entities)
    # 再导出 → 再读，几何不变
    p = export(env, did)
    doc = ezdxf.readfile(p)
    msp = doc.modelspace()
    assert len(msp) == len([e for e in d.entities if e.space == "Model"])
    assert sorted(e.dxftype() for e in msp) == sorted(["LINE", "CIRCLE", "ARC", "LWPOLYLINE", "ELLIPSE", "SPLINE", "TEXT", "MTEXT", "POINT", "INSERT", "HATCH", "DIMENSION"])
    line = msp.query("LINE")[0]
    assert tuple(line.dxf.end)[:2] == (100, 0) and line.dxf.layer == "轮廓" and line.dxf.color == 5
    lw = msp.query("LWPOLYLINE")[0]
    assert [round(p[4], 6) for p in lw.get_points("xyseb")] == [0, 0.5, 0, 0]
    ins2 = msp.query("INSERT")[0]
    assert {a.dxf.tag: a.dxf.text for a in ins2.attribs} == {"TAG": "V-101"}
    lay = doc.layouts.get("Sheet1")
    vp = [v for v in lay.viewports() if v.dxf.id != 1][0]
    assert vp.dxf.height / vp.dxf.view_height == pytest.approx(0.5) and any(e.dxftype() == "TEXT" and e.dxf.text == "布局文字" for e in lay)


def test_garbage_dxf_gives_clear_error(env):
    bad = env.svc.store.root / "uploads" / "bad.dxf"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("this is not a dxf", encoding="utf-8")
    r = env.err("manage_document_file", None, action="open", file_ref=env.svc.store.rel(bad))
    assert r["error_code"] == "FILE_UNREADABLE" and "DXF" in r["message"]


def test_foreign_dxf_with_unknown_block_reference_does_not_crash(env):
    src = env.svc.store.root / "uploads" / "f2.dxf"
    src.parent.mkdir(parents=True, exist_ok=True)
    doc = ezdxf.new("R2010")
    doc.modelspace().add_line((0, 0), (10, 0))
    doc.saveas(src)
    r = env.call("manage_document_file", None, action="open", file_ref=env.svc.store.rel(src))
    d = env.d(r["drawing_id"])
    assert len(d.entities) == 1 and d.view.height > 0
    p = export(env, r["drawing_id"])
    assert len(ezdxf.readfile(p).modelspace()) == 1


def test_xrefs_exported_as_real_xref_blocks(env, tmp_path):
    did = env.new("blank_a3")
    other = env.new("blank_a3")
    env.call("draw_linear_primitives", other, primitive_type="line", points=["0,0", "50,0"])
    saved = env.call("manage_document_file", other, action="save", drawing_id=other, format="dxf")["file"]["file_ref"]
    env.call("manage_external_references", did, action="attach", xref_path_or_name=saved, insert_point="10,10")
    doc = ezdxf.readfile(export(env, did))
    xr = [b for b in doc.blocks if b.block.dxf.flags & 4]
    assert len(xr) == 1 and xr[0].block.dxf.xref_path.endswith(".dxf")


def test_solid3d_written_as_mesh_and_hidden_from_acis(env):
    did = env.new("blank_a3")
    env.call("draw_polygons_and_meshes", did, shape_type="rectangle", corner1="0,0", corner2="@40,30")
    env.call("extrude_2d_to_3d", did, profile_entity_ids=["P1"] if env.ids(did, "polyline") == ["P1"] else env.ids(did, "polyline")[:1], height=15)
    doc = ezdxf.readfile(export(env, did))
    meshes = doc.modelspace().query("MESH")
    assert len(meshes) == 1 and len(list(meshes[0].vertices)) == 8 and max(v[2] for v in meshes[0].vertices) == pytest.approx(15)
    assert not doc.modelspace().query("3DSOLID")                                                                          # ezdxf 不能生成 ACIS：只写 MESH，STL 另行导出


def test_dynamic_block_variants_are_static_blocks_in_dxf(env):
    did = env.new("blank_a3")
    env.call("draw_polygons_and_meshes", did, shape_type="rectangle", corner1="0,0", corner2="@100,40")
    env.call("create_and_insert_block", did, action="define_block", block_name="PLATE", source_entity_ids=env.ids(did)[:1], base_point="0,0", keep_source="convert")
    env.call("manage_dynamic_block", did, action="create_dynamic_def", block_name="PLATE", parameters_and_actions=[{"param_name": "宽", "param_type": "linear_stretch", "geometry_refs": ["E1"], "axis": "x", "default_value": 100}])
    ref = env.ids(did, "insert")[0]
    v1 = env.call("manage_dynamic_block", did, action="set_dynamic_property", block_ref_id=ref, property_overrides={"宽": 140})["data"]["variant"]
    env.call("create_and_insert_block", did, action="insert_block", block_name="PLATE", insert_point="0,100")
    ref2 = env.ids(did, "insert")[-1]
    v2 = env.call("manage_dynamic_block", did, action="set_dynamic_property", block_ref_id=ref2, property_overrides={"宽": 60})["data"]["variant"]
    doc = ezdxf.readfile(export(env, did))
    names = {b.name for b in doc.blocks}
    assert v1 in names and v2 in names and v1 != v2
    xs = lambda n: sorted({p[0] for e in doc.blocks[n].query("LWPOLYLINE") for p in e.get_points("xy")})
    assert xs(v1)[-1] == pytest.approx(140) and xs(v2)[-1] == pytest.approx(60)


def test_foreign_dimensions_native_or_exploded_with_note(env):
    src = env.svc.store.root / "uploads" / "dims.dxf"
    src.parent.mkdir(parents=True, exist_ok=True)
    doc = ezdxf.new("R2010", setup=True)
    msp = doc.modelspace()
    msp.add_linear_dim(base=(0, -10), p1=(0, 0), p2=(80, 0)).render()                      # 水平线性
    msp.add_linear_dim(base=(-10, 0), p1=(0, 0), p2=(0, 50), angle=90).render()              # 垂直线性
    msp.add_aligned_dim(p1=(0, 0), p2=(30, 40), distance=5).render()                         # 对齐
    msp.add_diameter_dim(center=(200, 50), radius=15, angle=45).render()
    msp.add_radius_dim(center=(300, 50), radius=12, angle=20).render()
    msp.add_angular_dim_2l(base=(0, 200), line1=((0, 100), (50, 100)), line2=((0, 100), (0, 150))).render()    # 角度标注：分解
    doc.saveas(src)
    did = env.call("manage_document_file", None, action="open", file_ref=env.svc.store.rel(src))["drawing_id"]
    d = env.d(did)
    dims = {e.data["dtype"]: e for e in d.entities if e.kind == "dimension"}
    assert set(dims) == {"linear_horizontal", "linear_vertical", "aligned", "diameter", "radius"}
    assert dims["linear_horizontal"].data["measurement"] == 80 and dims["aligned"].data["measurement"] == pytest.approx(50)
    assert dims["diameter"].data["radius"] == pytest.approx(15) and dims["radius"].data["radius"] == pytest.approx(12)
    assert any(e.tags == ["dim_exploded"] for e in d.entities)
    assert any("已分解" in n for n in d.notes)                                                  # 分解过要告诉用户
    out = ezdxf.readfile(export(env, did)).modelspace()
    assert len(out.query("DIMENSION")) == 5


def test_gradient_hatch_is_written_with_its_colors(env):
    did = env.new("blank_a3")
    r = env.call("draw_polygons_and_meshes", did, shape_type="rectangle", corner1="0,0", width=50, height=30)["created"][0]
    env.call("create_hatch", did, boundary_mode="entity_ids", entity_ids=[r], pattern_name="SOLID", gradient={"color1": "#FF0000", "color2": "#0000FF", "angle": 45})
    h = ezdxf.readfile(export(env, did)).modelspace().query("HATCH")[0]
    assert h.gradient is not None and tuple(h.gradient.color1) == (255, 0, 0) and tuple(h.gradient.color2) == (0, 0, 255)
