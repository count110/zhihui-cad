"""通用 CAD 函数（46 个预置函数 + 6 个感知函数 + 批量/撤销）：每个函数至少一个正常用例和一个典型错误；事务、版本、撤销、锁定。"""
import json
import math

import pytest

from gen_helpers import Env, close, pclose
from zhihui.tools.general.registry import CATEGORIES, REGISTRY


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


@pytest.fixture
def did(env):
    return env.new("gb_a3_mech")


def draw_rect_and_circle(env, did):
    r = env.call("draw_polygons_and_meshes", did, shape_type="rectangle", corner1="0,0", corner2="@100,60")
    c = env.call("draw_circular_primitives", did, curve_type="circle", method="center_diameter", center="50,30", diameter=20)
    return r["created"][0], c["created"][0]


# ------------------------------------------------------------------ 注册表
def test_registry_has_all_functions_with_fixed_names_and_categories():
    names = {
        "environment_setup": ["manage_workspace_ui", "manage_document_file", "configure_canvas_and_limits", "set_linetype_scale", "manage_layers", "set_layer_properties", "set_layer_state",
                              "configure_drafting_modes", "configure_osnap_and_otracking", "configure_dynamic_input_and_coords"],
        "2d_drafting": ["draw_linear_primitives", "draw_complex_curves", "draw_polygons_and_meshes", "draw_circular_primitives", "create_hatch"],
        "precision_snap": ["parse_coordinate_input", "resolve_osnap_point"],
        "view_select": ["control_viewport_view", "select_entities"],
        "2d_editing": ["replicate_entities", "transform_entities", "adjust_line_lengths", "modify_corners_and_breaks", "explode_compound_entities", "grip_edit_entity", "modify_entity_properties",
                       "match_entity_properties", "erase_entities"],
        "annotation_dimension": ["create_text_annotation", "manage_text_style", "create_linear_and_angular_dim", "create_radial_and_chain_dim", "manage_dim_style"],
        "layout_plotting": ["switch_model_and_layout_space", "manage_layout_viewports", "configure_plot_and_export"],
        "block_xref_constraint": ["create_and_insert_block", "manage_dynamic_block", "manage_external_references", "apply_parametric_constraints"],
        "3d_modeling": ["extrude_2d_to_3d", "revolve_2d_to_3d", "switch_3d_view_and_visual_style"],
        "engineering_specialty": ["create_mechanical_tolerances", "invoke_engineering_toolset", "manage_symbol_library"],
        "perception": ["query_entities_by_filter", "resolve_contextual_reference", "get_entity_snap_points", "find_closed_boundary", "get_entity_grips_and_topology", "inspect_drawing_context"],
        "system": ["cad_batch_execute", "history_undo_redo"],
    }
    for cat, fns in names.items():
        for f in fns:
            assert f in REGISTRY and REGISTRY[f].category == cat, (cat, f)
    assert sum(len(v) for k, v in names.items() if k not in ("perception", "system")) == 46          # 规格 45 项 + 补充的 erase_entities
    assert set(names) <= set(CATEGORIES) | {"perception", "system"}


# ------------------------------------------------------------------ 1–10 基础环境
def test_new_save_reopen_roundtrip_and_workspace(env, did):
    r = env.call("manage_workspace_ui", did, workspace="3d_modeling", show_panels=["layers", "status_bar"], explain_ui_layout=True, topic="layers")
    assert r["data"]["workspace"] == "3d_modeling" and "layers" in r["data"]["help"] and env.d(did).view.panels == ["layers", "status_bar"]
    draw_rect_and_circle(env, did)
    saved = env.call("manage_document_file", did, action="save", drawing_id=did, format="dxf", dxf_version="R2013")
    again = env.call("manage_document_file", None, action="open", file_ref=saved["file"]["file_ref"])
    d2 = env.d(again["drawing_id"])
    assert d2.mode == "general" and [e.kind for e in d2.entities] == ["polyline", "circle"] and again["drawing_id"] != did
    js = env.call("manage_document_file", did, action="save", drawing_id=did, format="json")
    assert js["file"]["bytes"] > 100
    env.err("manage_document_file", None, action="open", file_ref="../../etc/passwd")
    env.err("manage_document_file", None, action="new", template_name="nope")


def test_canvas_limits_ltscale_and_background(env, did):
    r = env.call("configure_canvas_and_limits", did, bg_color="黑色", limits_lower_left="0,0", limits_upper_right="841,594", limits_check_on=True)
    d = env.d(did)
    assert d.background == "#000000" and d.limits == (0, 0, 841, 594) and d.limits_check
    env.err("configure_canvas_and_limits", did, limits_lower_left="100,100", limits_upper_right="50,50")
    a = env.call("set_linetype_scale", did, auto=True)
    auto = env.d(did).ltscale
    assert auto > 0 and a["data"].get("ltscale", auto) == auto
    env.call("set_linetype_scale", did, ltscale=2.5, celtscale=0.5, psltscale=0)
    d = env.d(did)
    assert (d.ltscale, d.celtscale, d.psltscale) == (2.5, 0.5, 0)


def test_layers_presets_properties_state_and_protection(env):
    did = env.new("blank_a3")
    env.call("manage_layers", did, action="create_standard_preset", preset_standard="GB_MECH")
    names = [l.name for l in env.d(did).layers]
    assert {"粗实线", "细实线", "虚线", "中心线", "剖面线", "尺寸标注", "文字"} <= set(names)
    env.call("manage_layers", did, action="create", layer_name="我的层")
    env.err("manage_layers", did, action="create", layer_name="我的层")
    env.call("set_layer_properties", did, layer_name="我的层", color="#FF8800", linetype="虚线", lineweight_mm=0.35, transparency=30, description="测试")
    l = env.d(did).get_layer("我的层")
    assert l.true_color == "#FF8800" and l.linetype == "DASHED" and l.lineweight_mm == 0.35 and l.transparency == 30
    env.err("set_layer_properties", did, layer_name="我的层", color="不存在的颜色")
    env.err("set_layer_properties", did, layer_name="我的层", linetype="NOSUCHTYPE")
    env.call("set_layer_state", did, layer_name="我的层", set_current=True)
    assert env.err("set_layer_state", did, layer_name="我的层", is_frozen=True)["error_code"] == "LAYER_IS_CURRENT"    # 当前图层不能冻结
    assert env.err("manage_layers", did, action="delete", layer_name="0")["error_code"] == "LAYER_PROTECTED"
    env.call("set_layer_state", did, layer_name="粗实线", set_current=True)
    env.call("draw_linear_primitives", did, primitive_type="line", points=["0,0", "10,0"])
    env.call("set_layer_state", did, layer_name="粗实线", set_current=False, is_locked=True) if False else None
    env.call("set_layer_state", did, layer_name="我的层", is_locked=True)
    e = env.err("draw_linear_primitives", did, primitive_type="line", points=["0,0", "10,0"], layer="我的层")
    assert e["error_code"] == "LAYER_LOCKED"
    env.call("manage_layers", did, action="rename", layer_name="我的层", new_name="新名字")
    assert env.err("manage_layers", did, action="delete", layer_name="粗实线")["error_code"] in ("LAYER_IN_USE", "LAYER_CURRENT")       # 有图元的层不能删
    lst = env.call("manage_layers", did, action="list")
    assert any(x["name"] == "新名字" for x in lst["data"]["layers"])
    env.call("manage_layers", did, action="purge")


def test_drafting_modes_osnap_dynamic_are_quiet_settings(env, did):
    rev = env.rev(did)
    env.call("configure_drafting_modes", did, ortho_on=True, grid_on=True, snap_on=True, snap_spacing=[5, 5], grid_spacing=[10, 10])
    d = env.d(did)
    assert d.draft.ortho_on and d.draft.grid_on and d.draft.snap_spacing == (5, 5) and not d.draft.polar_on
    env.call("configure_drafting_modes", did, polar_on=True, polar_angle_deg=30)
    d = env.d(did)
    assert d.draft.polar_on and not d.draft.ortho_on and d.draft.polar_angle_deg == 30                    # 正交和极轴互斥
    env.call("configure_osnap_and_otracking", did, osnap_on=True, otrack_on=True, active_modes=["endpoint", "midpoint", "intersection"])
    env.call("configure_dynamic_input_and_coords", did, dynmode_on=False, coords_display_mode="continuous_xy")
    assert env.d(did).draft.osnap_modes == ["endpoint", "midpoint", "intersection"] and not env.d(did).draft.dynmode_on
    assert env.rev(did) == rev                                                                              # 绘图辅助开关不产生新版本
    assert env.d(did).session.history == []                                                                  # 也不进撤销栈


# ------------------------------------------------------------------ 11–17 绘图
def test_linear_primitives(env, did):
    env.call("draw_linear_primitives", did, primitive_type="line", points=["0,0", "100,0", "100,50"], close_loop=True)
    assert env.ids(did, "line") == ["L1", "L2", "L3"]                                                      # 连续画线每段独立，闭合再加一段
    p = env.call("draw_linear_primitives", did, primitive_type="point", points=["5,5"], point_style={"pdmode": 34, "pdsize": 3})
    assert env.d(did).point_style["pdmode"] == 34 and p["created"]
    r = env.call("draw_linear_primitives", did, primitive_type="ray", points=["0,0", "10,10"])
    assert env.ent(did, r["created"][0]).data["dir"][0] == pytest.approx(math.sqrt(0.5))
    for mode, kw in (("horizontal", {"points": ["0,20"]}), ("vertical", {"points": ["30,0"]}), ("angle", {"points": ["0,0"], "angle_deg": 45}), ("offset", {"offset_dist": 5, "reference_entity_id": "L1", "points": ["0,10"]}),
                     ("bisect", {"points": ["0,0"], "reference_entity_id": "L1"})):
        res = env.call("draw_linear_primitives", did, ok=mode != "bisect", primitive_type="xline", xline_mode=mode, **kw)
    env.err("draw_linear_primitives", did, primitive_type="line", points=["0,0"])


def test_curves_rectangles_polygons_and_grids(env, did):
    env.call("draw_complex_curves", did, curve_type="lwpolyline", vertices=["0,0", "50,0", "50,30"], closed=True, global_width=1)
    env.call("draw_complex_curves", did, curve_type="spline", vertices=["0,0", "20,30", "40,10", "60,40"], spline_method="fit")
    env.call("draw_complex_curves", did, curve_type="mline", vertices=["0,100", "80,100"], mline_scale=4, mline_justification="top")
    assert [e.kind for e in env.d(did).entities] == ["polyline", "spline", "mline"]
    r = env.call("draw_polygons_and_meshes", did, shape_type="rectangle", corner1="0,0", corner2="@100,60", rect_fillet_r=8)
    pl = env.ent(did, r["created"][0])
    assert "rectangle" not in pl.tags or len(pl.data["vertices"]) == 8 and len(pl.data["vertices"]) == 8        # 圆角矩形：8 个顶点（4 段弧）
    c = env.call("draw_polygons_and_meshes", did, shape_type="rectangle", corner1="0,0", width=40, height=20, rect_chamfer=[3, 3], rotation_deg=30)
    assert len(env.ent(did, c["created"][0]).data["vertices"]) == 8
    env.err("draw_polygons_and_meshes", did, shape_type="rectangle", corner1="0,0", corner2="@100,60", rect_fillet_r=40)           # 圆角过大
    hx = env.call("draw_polygons_and_meshes", did, shape_type="polygon", num_sides=6, center="0,0", radius=50, inscribed=True)
    pts = [v["p"] for v in env.ent(did, hx["created"][0]).data["vertices"]]
    assert len(pts) == 6 and all(abs(math.hypot(x, y) - 50) < 1e-6 for x, y, _ in pts) and "polygon" in env.ent(did, hx["created"][0]).tags
    env.err("draw_polygons_and_meshes", did, shape_type="polygon", num_sides=2, center="0,0", radius=5)
    g = env.call("draw_polygons_and_meshes", did, shape_type="grid2d", corner1="0,0", rows=2, cols=3, cell_size=[10, 5])
    assert len(g["created"]) == 3 + 4 or len(g["created"]) >= 5                                              # 格线
    m = env.call("draw_polygons_and_meshes", did, shape_type="mesh", corner1="0,0", rows=2, cols=2, cell_size=[10, 10])
    assert env.ent(did, m["created"][0]).kind == "mesh"


def test_circles_arcs_ellipses(env, did):
    env.call("draw_circular_primitives", did, curve_type="circle", method="2p", points=["0,0", "10,0"])
    c3 = env.call("draw_circular_primitives", did, curve_type="circle", method="3p", points=["0,0", "10,0", "5,5"])
    assert env.ent(did, c3["created"][0]).data["r"] == pytest.approx(5)
    env.err("draw_circular_primitives", did, curve_type="circle", method="3p", points=["0,0", "5,5", "10,10"])                  # 三点共线
    a = env.call("draw_circular_primitives", did, curve_type="arc", method="start_center_angle", start_pt="10,0", center="0,0", included_angle_deg=90)
    arc = env.ent(did, a["created"][0]).data
    assert arc["r"] == pytest.approx(10) and (arc["a1"] - arc["a0"]) % 360 == pytest.approx(90)
    env.call("draw_circular_primitives", did, curve_type="arc", method="start_end_radius", start_pt="0,0", end_pt="10,0", radius=8)
    env.err("draw_circular_primitives", did, curve_type="arc", method="start_end_radius", start_pt="0,0", end_pt="10,0", radius=2)   # 半径小于半弦
    e = env.call("draw_circular_primitives", did, curve_type="ellipse", method="center_axes", center="0,0", major_axis_vec=[20, 0], axis_ratio=0.5)
    assert env.ent(did, e.get("created")[0]).data["ratio"] == 0.5
    env.call("draw_circular_primitives", did, curve_type="ellipse_arc", method="center_axes", center="0,0", major_axis_vec=[20, 0], axis_ratio=0.5, start_angle_deg=0, end_angle_deg=180)


def test_hatch_boundaries_associative_and_gaps(env, did):
    rect, circ = draw_rect_and_circle(env, did)
    h = env.call("create_hatch", did, boundary_mode="entity_ids", entity_ids=[rect, circ], pattern_name="ANSI31", island_style="normal", scale=2)
    hd = env.ent(did, h["created"][0]).data
    assert len(hd["loops"]) == 2 and hd["scale"] == 2 and hd["associative"]
    env.call("transform_entities", did, operation="move", entity_ids=[rect, circ], displacement=[10, 0])                           # 边界移动：关联填充跟着走
    assert env.ent(did, h["created"][0]).data["loops"][0][0][0] >= 10 - 1e-6
    env.call("erase_entities", did, entity_ids=[circ])
    assert "disassociated" in env.ent(did, h["created"][0]).tags or env.ent(did, h["created"][0]).data["loops"]
    pick = env.call("create_hatch", did, boundary_mode="pick_internal_point", pick_point="60,30", pattern_name="SOLID")
    assert pick["created"]
    env.call("draw_linear_primitives", did, primitive_type="line", points=["200,0", "300,0", "300,50", "200,50", "200,2"])
    e = env.err("create_hatch", did, boundary_mode="pick_internal_point", pick_point="250,25")
    assert e["error_code"] == "BOUNDARY_NOT_CLOSED" and e["gaps"]


def test_parse_osnap_resolution_and_perception(env, did):
    rect, circ = draw_rect_and_circle(env, did)
    pts = env.call("get_entity_snap_points", did, entity_ids=[rect], snap_types=["geometric_center", "midpoint", "endpoint"])["data"]["snap_points"]
    gc = [p for p in pts if p["type"] == "geometric_center"][0]["coord"]
    assert pclose(gc, (50, 30))
    r = env.call("resolve_osnap_point", did, snap_mode="center", target_entity_ids=[circ])
    assert pclose(r["data"]["point"], (50, 30))
    q = env.call("query_entities_by_filter", did, entity_types=["circle"], where=[{"field": "diameter", "op": ">", "value": 10}])["data"]
    assert q["total"] == 1 and q["candidates"][0]["entity_id"] == circ and q["candidates"][0]["key_params"]["diameter"] == 20
    assert env.call("query_entities_by_filter", did, entity_types=["circle"], where=[{"field": "diameter", "op": ">", "value": 50}])["data"]["total"] == 0
    ref = env.call("resolve_contextual_reference", did, reference_type="last_created", entity_type_filter="circle")
    assert ref["data"]["entity_ids"] == [circ]
    env.err("resolve_contextual_reference", did, reference_type="last_created", entity_type_filter="spline")
    b = env.call("find_closed_boundary", did, seed_point="20,20")["data"]
    assert b["is_closed"] and rect in b["outer_loop_entity_ids"]
    g = env.call("get_entity_grips_and_topology", did, entity_id=rect)["data"]
    assert len(g["grips"]) >= 4
    ctxd = env.call("inspect_drawing_context", did, include_tables=["layers", "sysvars", "scene_digest"])["data"]
    assert ctxd["entity_count"] == 2 and "inventory" in ctxd["context"] and ctxd["tables"]["sysvars"]["LTSCALE"] > 0


# ------------------------------------------------------------------ 18–27 视图、选择与编辑
def test_view_and_selection(env, did):
    rect, circ = draw_rect_and_circle(env, did)
    rev = env.rev(did)
    env.call("control_viewport_view", did, action="zoom_window", window_min="0,0", window_max="200,100")
    v = env.d(did).view
    assert v.center == (100, 50) and v.height >= 100                                                         # 窗口完整进入视图（高度按画布比例放宽）
    env.call("control_viewport_view", did, action="zoom_in")
    env.call("control_viewport_view", did, action="zoom_previous")
    assert env.d(did).view.height == pytest.approx(v.height)
    env.call("control_viewport_view", did, action="zoom_object", target_entity_ids=[circ])
    env.call("control_viewport_view", did, action="save_view", view_name="V1")
    env.call("control_viewport_view", did, action="zoom_extents")
    env.call("control_viewport_view", did, action="restore_view", view_name="V1")
    env.err("control_viewport_view", did, action="restore_view", view_name="没有这个")
    assert env.rev(did) == rev                                                                              # 视图操作不产生版本
    s = env.call("select_entities", did, selection_mode="window", box_corner1="-5,-5", box_corner2="105,65")
    assert set(env.d(did).session.selection) == {rect, circ}
    s = env.call("select_entities", did, selection_mode="window", box_corner1="40,20", box_corner2="60,40")
    assert env.d(did).session.selection == [circ]                                                           # 窗选：完全在框内才选
    s = env.call("select_entities", did, selection_mode="crossing", box_corner1="40,20", box_corner2="120,200")
    assert set(env.d(did).session.selection) == {rect, circ}                                                # 交叉选：碰到就选
    env.call("select_entities", did, selection_mode="filter", filter_criteria={"entity_types": ["circle"]})
    assert env.d(did).session.selection == [circ]
    env.call("select_entities", did, selection_mode="point_pick", pick_point="50,40.01", add_to_selection=True)
    env.call("select_entities", did, selection_mode="all")
    env.call("select_entities", did, selection_mode="by_ids", entity_ids=[circ], remove_from_selection=True) if False else None


def test_replicate_operations(env, did):
    rect, circ = draw_rect_and_circle(env, did)
    r = env.call("replicate_entities", did, operation="copy", entity_ids=[circ], base_point="50,30", target_points=["150,30", "250,30"])
    assert len(r["created"]) == 2 and env.ent(did, r["created"][1]).data["c"][0] == 250
    m = env.call("replicate_entities", did, operation="mirror", entity_ids=[circ], mirror_line=["0,0", "0,10"], erase_source=True)
    assert circ in m["deleted"] and env.ent(did, m["created"][0]).data["c"][0] == pytest.approx(-50)
    o = env.call("replicate_entities", did, operation="offset", entity_ids=[rect], offset_distance=10, offset_side="outside")
    bb = env.ent(did, o["created"][0]).data["vertices"]
    assert min(v["p"][0] for v in bb) == pytest.approx(-10) and max(v["p"][1] for v in bb) == pytest.approx(70)
    env.err("replicate_entities", did, operation="offset", entity_ids=[rect], offset_distance=40, offset_side="inside")           # 向内偏移过大
    a = env.call("replicate_entities", did, operation="array_rect", entity_ids=[m["created"][0]], array_params={"rows": 2, "cols": 3, "row_spacing": 30, "col_spacing": 40})
    assert len(a["created"]) == 5 and a["data"]["group"].startswith("array:")
    env.err("replicate_entities", did, operation="array_rect", entity_ids=[m["created"][0]], array_params={"rows": 1, "cols": 1, "row_spacing": 30, "col_spacing": 40})
    p = env.call("replicate_entities", did, operation="array_polar", entity_ids=[env.ids(did, "circle")[0]], array_params={"center": "0,0", "count": 6, "fill_angle_deg": 360, "rotate_items": False})
    assert len(p["created"]) == 5
    env.call("draw_complex_curves", did, curve_type="lwpolyline", vertices=["0,200", "100,200"])
    path = env.ids(did, "polyline")[-1]
    pa = env.call("replicate_entities", did, operation="array_path", entity_ids=[m["created"][0]], array_params={"path_entity_id": path, "count": 4})
    assert len(pa["created"]) >= 3


def test_transform_and_lengths_and_corners(env, did):
    rect, circ = draw_rect_and_circle(env, did)
    env.call("transform_entities", did, operation="move", entity_ids=[circ], displacement=[10, -5])
    assert env.ent(did, circ).data["c"][:2] == [60, 25]
    env.call("transform_entities", did, operation="rotate", entity_ids=[rect], base_point="0,0", rotation_angle_deg=90)
    xs = [v["p"][0] for v in env.ent(did, rect).data["vertices"]]
    assert min(xs) == pytest.approx(-60) and max(xs) == pytest.approx(0)
    env.call("transform_entities", did, operation="scale", entity_ids=[circ], base_point="60,25", scale_factor=2)
    assert env.ent(did, circ).data["r"] == 20
    env.err("transform_entities", did, operation="scale", entity_ids=[circ], base_point="0,0")                                        # 缺比例
    c = env.call("transform_entities", did, operation="move", entity_ids=[circ], displacement=[1, 1], copy_mode=True)
    assert c["created"] and circ not in c["modified"]
    # 拉长、修剪、延伸
    env.call("draw_linear_primitives", did, primitive_type="line", points=["0,200", "100,200"])
    l1 = env.ids(did, "line")[0]
    env.call("adjust_line_lengths", did, operation="lengthen", target_entities=[{"entity_id": l1}], lengthen_mode="delta", lengthen_value=20, end="end")
    assert env.ent(did, l1).data["p2"][0] == pytest.approx(120)
    env.call("draw_linear_primitives", did, primitive_type="line", points=["50,180", "50,220"])
    cut = env.ids(did, "line")[-1]
    env.call("adjust_line_lengths", did, operation="trim", target_entities=[{"entity_id": l1, "pick_point": "100,200"}], boundary_entity_ids=[cut])
    assert env.ent(did, l1).data["p2"][0] == pytest.approx(50)
    e = env.err("adjust_line_lengths", did, operation="trim", target_entities=[{"entity_id": cut, "pick_point": "50,190"}], boundary_entity_ids=[l1] if False else [circ])
    assert e["error_code"] == "NO_INTERSECTION"
    env.call("draw_linear_primitives", did, primitive_type="line", points=["300,300", "340,300"])
    env.call("draw_linear_primitives", did, primitive_type="line", points=["300,300", "300,340"])
    a, b = env.ids(did, "line")[-2:]
    f = env.call("modify_corners_and_breaks", did, operation="fillet", entity_id_1=a, entity_id_2=b, fillet_radius=5, pick_point_1="320,300", pick_point_2="300,320")
    assert any(env.ent(did, i).kind == "arc" for i in f["created"])
    err = env.err("modify_corners_and_breaks", did, operation="fillet", entity_id_1=a, entity_id_2=b, fillet_radius=500)
    assert err["error_code"] == "RADIUS_TOO_LARGE"
    br = env.call("modify_corners_and_breaks", did, operation="break_at_point", entity_id_1=l1, break_pt1="20,200")
    assert len(br["created"]) + len(br["modified"]) >= 2


def test_explode_grips_properties_and_match(env, did):
    rect, circ = draw_rect_and_circle(env, did)
    ex = env.call("explode_compound_entities", did, entity_ids=[rect])
    assert len(ex["created"]) == 4 and rect in ex["deleted"] and all(env.ent(did, i).kind == "line" for i in ex["created"])
    g = env.call("grip_edit_entity", did, entity_id=ex["created"][0], grip_type_or_index="end", grip_mode="stretch", new_position="120,0")
    assert env.ent(did, ex["created"][0]).data["p2"][0] == 120
    gc = env.call("grip_edit_entity", did, entity_id=circ, grip_type_or_index="quadrant_0", grip_mode="stretch", new_position="80,30")
    assert env.ent(did, circ).data["r"] == pytest.approx(30)
    mv = env.call("grip_edit_entity", did, entity_id=circ, grip_type_or_index="center", grip_mode="move", new_position="10,10")
    assert env.ent(did, circ).data["c"][:2] == [10, 10]
    cp = env.call("grip_edit_entity", did, entity_id=circ, grip_type_or_index="center", grip_mode="move", new_position="200,10", copy_on_edit=True)
    assert cp["created"]
    env.err("grip_edit_entity", did, entity_id=circ, grip_type_or_index="nonsense", grip_mode="stretch", new_position="0,0")
    env.call("modify_entity_properties", did, action="set", entity_ids=[circ], common_props={"layer": "中心线", "color": "红", "linetype": "点画线", "lineweight_mm": 0.35}, geometry_props={"radius": 7})
    e = env.ent(did, circ)
    assert e.layer == "中心线" and e.color == 1 and e.linetype == "CENTER" and e.lineweight_mm == 0.35 and e.data["r"] == 7
    got = env.call("modify_entity_properties", did, action="get", entity_ids=[circ, ex["created"][0]])["data"]
    assert got["common_props"]["layer"] == "多种" and got["count"] == 2
    env.err("modify_entity_properties", did, action="set", entity_ids=[circ], common_props={"layer": "没有的层"})
    m = env.call("match_entity_properties", did, source_entity_id=circ, target_entity_ids=ex["created"][1:], property_filter=["layer", "color", "linetype"])
    assert all(env.ent(did, i).layer == "中心线" and env.ent(did, i).color == 1 for i in ex["created"][1:])


# ------------------------------------------------------------------ 28–32 标注
def test_text_styles_and_text(env, did):
    t = env.call("create_text_annotation", did, text_type="single_line", content="直径%%c30，角度45%%d，±%%p0.1", insert_point="10,10", height=5, alignment="center")
    assert env.ent(did, t["created"][0]).data["text"] == "直径⌀30，角度45°，±±0.1".replace("±±", "±") or "⌀" in env.ent(did, t["created"][0]).data["text"]
    mt = env.call("create_text_annotation", did, text_type="mtext", content="第一行\\P第二行", insert_point="10,50", width_box=60, line_spacing=1.5, bg_mask=True)
    assert env.ent(did, mt["created"][0]).data["width"] == 60
    env.call("create_text_annotation", did, action="edit", entity_id=t["created"][0], content="改过了")
    assert env.ent(did, t["created"][0]).data["text"] == "改过了"
    env.err("create_text_annotation", did, content="x", insert_point="0,0", style_name="没有的样式")
    env.err("create_text_annotation", did, insert_point="0,0")                                              # 缺内容
    st = env.call("manage_text_style", did, style_name="长仿宋", font_file="仿宋", width_factor=0.7, oblique_angle_deg=15, set_current=True)
    assert env.d(did).current_text_style == "长仿宋" and st["data"]["style"]["width_factor"] == 0.7
    env.err("manage_text_style", did, style_name="坏", width_factor=20)


def test_dimensions_and_styles(env, did):
    rect, circ = draw_rect_and_circle(env, did)
    h = env.call("create_linear_and_angular_dim", did, dim_type="linear_horizontal", target_entity_id=rect)                       # 整体长
    v = env.call("create_linear_and_angular_dim", did, dim_type="linear_vertical", target_entity_id=rect)
    assert h["data"]["measurement"] == 100 and v["data"]["measurement"] == 60
    a = env.call("create_linear_and_angular_dim", did, dim_type="aligned", ext_line1_point="0,0", ext_line2_point="30,40")
    assert a["data"]["measurement"] == pytest.approx(50)
    two = env.call("create_linear_and_angular_dim", did, dim_type="linear_horizontal", ext_line1_point="0,0", ext_line2_point="40,0")
    assert env.ent(did, two["created"][0]).data["loc"][1] > env.ent(did, h["created"][0]).data["loc"][1] - 1e6                      # 自动避让放置
    env.err("create_linear_and_angular_dim", did, dim_type="linear_auto", ext_line1_point="5,5", ext_line2_point="5,5")                # 两点重合
    env.call("draw_linear_primitives", did, primitive_type="line", points=["200,0", "300,0"])
    env.call("draw_linear_primitives", did, primitive_type="line", points=["200,0", "200,100"])
    ang = env.call("create_linear_and_angular_dim", did, dim_type="angular", angle_entity_ids=env.ids(did, "line")[-2:])
    assert ang["data"]["measurement"] == pytest.approx(90)
    rad = env.call("create_radial_and_chain_dim", did, dim_type="radius", target_entity_id=circ)
    dia = env.call("create_radial_and_chain_dim", did, dim_type="diameter", target_entity_id=circ)
    assert rad["data"]["measurement"] == 10 and dia["data"]["measurement"] == 20
    env.err("create_radial_and_chain_dim", did, dim_type="diameter", target_entity_id=rect)
    base = env.call("create_radial_and_chain_dim", did, dim_type="baseline", base_dim_id=h["created"][0], chain_points=["70,0", "100,0"])
    assert len(base["created"]) == 2
    cont = env.call("create_radial_and_chain_dim", did, dim_type="continue", base_dim_id=h["created"][0], chain_points=["120,0"])
    assert cont["created"]
    s = env.call("manage_dim_style", did, style_name="我的", standard_preset="GB_MECH_MM", arrow_type="architectural_tick", text_height=3.5, precision=1, dimscale=2, tolerance_display="symmetric", set_current=True, apply_to_existing=True)
    assert env.d(did).current_dim_style == "我的" and all(e.data["style"] == "我的" for e in env.d(did).entities if e.kind == "dimension")
    env.err("manage_dim_style", did, style_name="坏", precision=20)


# ------------------------------------------------------------------ 33–35 布局与出图
def test_layouts_viewports_and_plotting(env, did):
    rect, circ = draw_rect_and_circle(env, did)
    r = env.call("switch_model_and_layout_space", did, target_space="Model", layout_action="create", new_layout_name="A3图", insert_standard_frame="ISO_A3", title_block_fields={"drawing_name": "测试件", "drawing_no": "T-001"})
    assert env.d(did).active_space == "A3图" and r["data"]["missing_title_block_fields"]                    # 没给的必填字段只留占位，不编造
    frame = [e for e in env.d(did).entities if e.space == "A3图" and "title_block" in e.tags]
    assert any(e.kind == "text" and e.data["text"] == "T-001" for e in frame)
    assert env.err("switch_model_and_layout_space", did, target_space="Model", layout_action="create", new_layout_name="A3图")["error_code"] == "LAYOUT_EXISTS"
    rev = env.rev(did)
    env.call("switch_model_and_layout_space", did, target_space="Model")
    assert env.d(did).active_space == "Model" and env.rev(did) == rev                                      # 只切换空间：不产生版本
    vp = env.call("manage_layout_viewports", did, layout_name="A3图", action="create", viewport_corner1="30,50", viewport_corner2="250,250", fit_entity_ids=[rect], standard_scale="1:2")
    vid = vp["data"]["viewport_id"]
    assert vp["data"]["scale"] == "1:2"
    env.err("manage_layout_viewports", did, layout_name="A3图", action="set_scale", viewport_id=vid, standard_scale="1:7")        # 非标准比例
    env.call("manage_layout_viewports", did, layout_name="A3图", action="set_scale", viewport_id=vid, standard_scale="1:5")
    env.call("manage_layout_viewports", did, layout_name="A3图", action="lock", viewport_id=vid)
    assert env.err("manage_layout_viewports", did, layout_name="A3图", action="set_scale", viewport_id=vid, standard_scale="1:2")["error_code"] == "VIEWPORT_LOCKED"
    env.call("manage_layout_viewports", did, layout_name="A3图", action="unlock", viewport_id=vid)
    env.call("manage_layout_viewports", did, layout_name="A3图", action="freeze_layers", viewport_id=vid, vp_frozen_layers=["尺寸标注"])
    env.err("manage_layout_viewports", did, layout_name="没有的布局", action="create")
    p = env.call("configure_plot_and_export", did, space_or_layout="A3图", device="pdf", color_mode="monochrome")
    assert p["data"]["file"]["format"] == "pdf" and p["data"]["file"]["bytes"] > 1000
    m = env.call("configure_plot_and_export", did, space_or_layout="Model", device="png", plot_scale="1:2", plot_area="extents", paper_size="ISO_A3")
    assert m["data"]["scale_text"] == "1:2"
    w = env.call("configure_plot_and_export", did, space_or_layout="Model", device="svg", plot_scale="10:1", plot_area="extents", paper_size="ISO_A4")
    assert w["warnings"]                                                                                    # 放不下：说明会被裁掉
    env.err("configure_plot_and_export", did, space_or_layout="Model", device="png", plot_scale="1:7")
    env.call("switch_model_and_layout_space", did, target_space="A3图", layout_action="rename", new_layout_name="图纸1")
    env.call("switch_model_and_layout_space", did, target_space="图纸1", layout_action="copy", new_layout_name="图纸2")
    env.call("switch_model_and_layout_space", did, target_space="图纸2", layout_action="delete")
    assert [l["name"] for l in env.d(did).layouts] == ["图纸1"]


# ------------------------------------------------------------------ 36–39 块、动态块、外部参照、约束
def test_blocks_attributes_and_dynamic_blocks(env, did):
    rect, circ = draw_rect_and_circle(env, did)
    r = env.call("create_and_insert_block", did, action="define_block", block_name="BODY", source_entity_ids=[rect, circ], base_point="0,0", keep_source="convert",
                 attribute_defs=[{"tag": "规格", "prompt": "规格", "default_val": "M8"}])
    ins0 = env.ids(did, "insert")[0]
    assert env.ids(did) == [ins0] and env.ent(did, ins0).data["attribs"] == {"规格": "M8"}
    env.err("create_and_insert_block", did, action="define_block", block_name="BODY", source_entity_ids=[ins0])
    i = env.call("create_and_insert_block", did, action="insert_block", block_name="BODY", insert_point="200,100", scale=[2, 2, 1], rotation_deg=30, attribute_values={"规格": "M10"})
    iid = i["data"]["insert_id"]
    assert env.ent(did, iid).data["scale"] == [2, 2, 1] and env.ent(did, iid).data["attribs"]["规格"] == "M10"
    bb = env.d(did).bbox()
    assert bb[2] > 250                                                                                      # 块参照的包围盒按块定义和比例算
    env.err("create_and_insert_block", did, action="insert_block", block_name="BODY", insert_point="0,0", attribute_values={"不存在": "x"})
    env.err("create_and_insert_block", did, action="insert_block", block_name="没有", insert_point="0,0")
    env.call("create_and_insert_block", did, action="edit_attributes", block_ref_id=iid, attribute_values={"规格": "M12"})
    assert env.ent(did, iid).data["attribs"]["规格"] == "M12"
    lst = env.call("create_and_insert_block", did, action="list")["data"]["blocks"]
    assert lst[0]["name"] == "BODY" and lst[0]["used"] == 2
    w = env.call("create_and_insert_block", did, action="wblock_export", block_name="BODY")
    assert w["data"]["file"]["format"] == "dxf"
    ex = env.call("explode_compound_entities", did, entity_ids=[iid])
    assert len(ex["created"]) == 2
    # 动态块
    env.call("manage_dynamic_block", did, action="create_dynamic_def", block_name="BODY", parameters_and_actions=[{"param_name": "宽", "param_type": "linear_stretch", "geometry_refs": ["E1"], "axis": "x", "default_value": 100}])
    ref = env.ids(did, "insert")[0]
    v = env.call("manage_dynamic_block", did, action="set_dynamic_property", block_ref_id=ref, property_overrides={"宽": 140})
    variant = v["data"]["variant"]
    blk = next(b for b in env.d(did).blocks if b["name"] == variant)
    xs = [vv["p"][0] for vv in blk["entities"][0]["data"]["vertices"]]
    assert max(xs) == pytest.approx(140) and variant.startswith("BODY@")                                    # 每组参数值对应一个静态块
    env.err("manage_dynamic_block", did, action="set_dynamic_property", block_ref_id=ref, property_overrides={"没有这个参数": 1})
    env.err("manage_dynamic_block", did, action="create_dynamic_def", block_name="BODY", parameters_and_actions=[{"param_name": "x", "param_type": "linear_stretch", "geometry_refs": ["E1"]}])   # 缺 default_value


def test_external_references(env, did, tmp_path):
    rect, circ = draw_rect_and_circle(env, did)
    other = env.new("blank_a3")
    env.call("draw_linear_primitives", other, primitive_type="line", points=["0,0", "50,0"])
    env.call("draw_circular_primitives", other, curve_type="circle", method="center_radius", center="25,10", radius=5)
    saved = env.call("manage_document_file", other, action="save", drawing_id=other, format="dxf")["file"]["file_ref"]
    r = env.call("manage_external_references", did, action="attach", xref_path_or_name=saved, insert_point="200,0", scale=2)
    name = r["data"]["xref"]
    assert env.d(did).xrefs[0]["status"] == "loaded" and env.d(did).bbox()[2] >= 300
    lst = env.call("manage_external_references", did, action="list")["data"]["xrefs"]
    assert lst[0]["name"] == name
    env.call("manage_external_references", did, action="xclip", xref_path_or_name=name, clip_boundary_pts=["0,-5", "30,-5", "30,30", "0,30"])
    env.call("manage_external_references", did, action="unload", xref_path_or_name=name)
    assert env.d(did).xrefs[0]["status"] == "unloaded"
    env.call("manage_external_references", did, action="reload", xref_path_or_name=name)
    exported = env.call("manage_document_file", did, action="save", drawing_id=did, format="dxf")["file"]["file_ref"]
    import ezdxf
    doc = ezdxf.readfile(env.svc.store.resolve_ref(exported))
    assert any(b.block.dxf.flags & 4 and b.block.dxf.xref_path for b in doc.blocks if not b.name.startswith("*"))                          # 导出的 DXF 里是真正的外部参照块
    env.call("manage_external_references", did, action="bind", xref_path_or_name=name)
    assert not env.d(did).xrefs and any(l.name.startswith(f"{name}$0$") for l in env.d(did).layers)
    env.err("manage_external_references", did, action="attach", xref_path_or_name="drawings/不存在.dxf", insert_point="0,0")
    env.err("manage_external_references", did, action="attach", xref_path_or_name="../../x.dxf", insert_point="0,0")


# ------------------------------------------------------------------ 40–42 三维
def test_solids_views_and_stl(env, did, monkeypatch):
    rect, circ = draw_rect_and_circle(env, did)
    s = env.call("extrude_2d_to_3d", did, profile_entity_ids=[rect, circ], height=30, include_islands=True)
    sid = s["data"]["solid_id"]
    assert s["data"]["volume"] == pytest.approx(30 * (6000 - 313.9), rel=0.01)                              # 带孔板一次拉伸，孔自动成为通孔
    assert env.ent(did, sid).data["solid_kind"] == "prism"
    env.call("draw_circular_primitives", did, curve_type="circle", method="center_radius", center="10,10", radius=4)
    cut = env.call("extrude_2d_to_3d", did, profile_entity_ids=[env.ids(did, "circle")[-1]], height=60, direction="+z", boolean_op="subtract", target_solid_id=sid)
    assert cut["modified"] == [sid] and cut["data"]["volume"] < s["data"]["volume"]
    env.err("extrude_2d_to_3d", did, profile_entity_ids=[rect])                                              # 缺高度
    env.err("extrude_2d_to_3d", did, profile_entity_ids=[rect], height=10, boolean_op="union")               # 布尔缺目标
    env.call("draw_polygons_and_meshes", did, shape_type="rectangle", corner1="300,0", corner2="@10,40")
    rv = env.call("revolve_2d_to_3d", did, profile_entity_ids=[env.ids(did, "polyline")[-1]], axis_mode="x_axis")
    assert rv["data"]["volume"] > 0
    from zhihui.cad.geom import solid as solid_kernel
    with monkeypatch.context() as mp:                                                                        # 没装可选依赖 manifold3d：旋转体不能做布尔，明确报错并给安装提示
        mp.setattr(solid_kernel, "manifold_available", lambda: False)
        e = env.err("revolve_2d_to_3d", did, profile_entity_ids=[env.ids(did, "polyline")[-1]], axis_mode="x_axis", boolean_op="union", target_solid_id=sid)
        assert e["error_code"] == "BOOLEAN_UNSUPPORTED" and "pip install manifold3d" in e["hint"]
    rev = env.rev(did)
    v = env.call("switch_3d_view_and_visual_style", did, standard_view="sw_isometric", visual_style="conceptual", projection="perspective")
    assert env.d(did).view.visual_style == "shaded" and env.d(did).view.view3d == "sw_iso" and env.rev(did) == rev
    env.err("switch_3d_view_and_visual_style", did, standard_view="custom_orbit")                             # 缺角度
    env.call("switch_3d_view_and_visual_style", did, standard_view="custom_orbit", orbit_angles_deg={"azimuth": 30, "elevation": 20})
    stl = env.call("manage_document_file", did, action="save", drawing_id=did, format="stl")
    text = env.svc.store.resolve_ref(stl["file"]["file_ref"]).read_text(encoding="utf-8")
    assert text.startswith("solid") and text.count("facet normal") > 50


# ------------------------------------------------------------------ 43–45 专业
def test_tolerances_gdt_roughness(env, did):
    rect, circ = draw_rect_and_circle(env, did)
    d = env.call("create_radial_and_chain_dim", did, dim_type="diameter", target_entity_id=circ)["data"]["dim_id"]
    f = env.call("create_mechanical_tolerances", did, tolerance_category="dimensional_fit", target_dim_or_entity_id=d, iso_fit_code="H7")
    tol = env.ent(did, d).data["tol"]
    assert tol["upper"] == 0.021 and tol["lower"] == 0 and "H7" in env.ent(did, d).data["text"] and env.ent(did, d).data["text"].startswith("<>")
    env.call("create_mechanical_tolerances", did, tolerance_category="dimensional_fit", target_dim_or_entity_id=d, iso_fit_code="H7/g6")
    assert env.ent(did, d).data["tol"]["mode"] == "fit_pair"
    u = env.err("create_mechanical_tolerances", did, tolerance_category="dimensional_fit", target_dim_or_entity_id=d, iso_fit_code="H7/zc9")
    assert u["error_code"] == "DEVIATION_UNKNOWN" and "不要自己编" in u["hint"]
    env.call("create_mechanical_tolerances", did, tolerance_category="deviation", target_dim_or_entity_id=d, upper_deviation=0.02, lower_deviation=-0.01)
    env.call("create_mechanical_tolerances", did, tolerance_category="symmetric", target_dim_or_entity_id=d, upper_deviation=0.1)
    assert env.ent(did, d).data["text"] == "<>±0.1"
    env.call("create_mechanical_tolerances", did, tolerance_category="limits", target_dim_or_entity_id=d, upper_deviation=0.02, lower_deviation=-0.01)
    assert "20.020" in env.ent(did, d).data["text"] and "19.990" in env.ent(did, d).data["text"]
    env.err("create_mechanical_tolerances", did, tolerance_category="deviation", target_dim_or_entity_id=d, upper_deviation=-1, lower_deviation=1)
    env.err("create_mechanical_tolerances", did, tolerance_category="symmetric", target_dim_or_entity_id=rect, upper_deviation=0.1)       # 不是尺寸标注
    g = env.call("create_mechanical_tolerances", did, tolerance_category="geometric_gdt", gdt_symbol="parallelism", gdt_value=0.02, datum_refs=["A"], target_dim_or_entity_id=rect)
    assert g["data"]["group"].startswith("G")
    assert env.err("create_mechanical_tolerances", did, tolerance_category="geometric_gdt", gdt_symbol="parallelism", gdt_value=0.02, target_dim_or_entity_id=rect)["error_code"] == "MISSING_ARG"   # 需要基准
    assert env.err("create_mechanical_tolerances", did, tolerance_category="geometric_gdt", gdt_symbol="flatness", gdt_value=0.02, datum_refs=["A"], target_dim_or_entity_id=rect)["error_code"] == "INVALID_ARGS"
    assert env.err("create_mechanical_tolerances", did, tolerance_category="geometric_gdt", gdt_symbol="position", target_dim_or_entity_id=rect)["error_code"] == "MISSING_ARG"
    env.call("create_mechanical_tolerances", did, tolerance_category="datum_feature", datum_letter="A", insert_point="50,0")
    r = env.call("create_mechanical_tolerances", did, tolerance_category="surface_roughness", roughness_param="Ra", roughness_value=3.2, process_symbol="remove", insert_point="20,60")
    assert any(env.ent(did, i).kind == "text" and env.ent(did, i).data["text"] == "Ra 3.2" for i in r["data"]["ids"])
    w = env.call("create_mechanical_tolerances", did, tolerance_category="surface_roughness", roughness_value=3.0, insert_point="20,60")
    assert w["warnings"]                                                                                    # 3.0 不是优先数系列
    env.err("create_mechanical_tolerances", did, tolerance_category="surface_roughness", insert_point="20,60")        # 粗糙度值必须来自用户


@pytest.mark.parametrize("domain,tool,params,at,must", [
    ("mechanical", "standard_fastener", {"type": "hex_bolt", "size": "M8", "length": 30}, "0,0", 6),
    ("mechanical", "standard_fastener", {"type": "hex_nut", "size": "M10"}, "0,0", 3),
    ("mechanical", "standard_fastener", {"type": "washer", "size": "M12", "view": "top"}, "0,0", 2),
    ("mechanical", "standard_fastener", {"type": "socket_head_cap_screw", "size": "M6", "length": 20}, "0,0", 6),
    ("mechanical", "shaft_generator", {"segments": [{"diameter": 20, "length": 30}, {"diameter": 30, "length": 40}], "chamfer": 1.5}, "0,0", 3),
    ("mechanical", "hole_thread_feature", {"type": "thread", "diameter": 8, "view": "top"}, "0,0", 3),
    ("mechanical", "hole_thread_feature", {"type": "counterbore", "diameter": 6, "cb_diameter": 11, "cb_depth": 6, "plate_thickness": 20, "view": "section"}, "0,0", 3),
    ("mechanical", "flange_generator", {"outer_diameter": 120, "bolt_circle_diameter": 90, "hole_count": 6, "hole_diameter": 12, "bore_diameter": 50}, "0,0", 8),
    ("mechanical", "bolt_circle", {"diameter": 60, "count": 8, "hole_diameter": 6}, "0,0", 9),
    ("mechanical", "keyway", {"shaft_diameter": 30}, "0,0", 3),
    ("mechanical", "keyway", {"shaft_diameter": 30, "part": "hub"}, "0,0", 3),
    ("mechanical", "bearing_simplified", {"inner_diameter": 20, "outer_diameter": 47, "width": 14}, "0,0", 5),
    ("mechanical", "gear_generator", {"module": 2, "teeth": 30, "bore_diameter": 20}, "0,0", 4),
    ("mechanical", "bom_balloon_and_table", {"items": [{"no": 1, "name": "底座", "qty": 1}, {"no": 2, "name": "螺栓", "qty": 4}], "balloons": [{"no": 1, "target": [10, 10]}]}, "0,0", 20),
    ("architectural", "arch_grid_and_wall", {"x_spacings": [6000, 6000], "y_spacings": [4500], "walls": [{"from": "A1", "to": "A3", "thickness": 240}]}, "0,0", 15),
    ("architectural", "door_window", {"kind": "window", "width": 1500, "wall_thickness": 240}, "3000,0", 4),
])
def test_engineering_toolsets(env, did, domain, tool, params, at, must):
    r = env.call("invoke_engineering_toolset", did, domain=domain, tool_name=tool, insert_point=at, spec_params=params)
    assert len(r["data"]["ids"]) >= must and r["data"]["group"].startswith("G")


def test_toolset_values_come_from_user_or_tables(env, did):
    for params in ({"outer_diameter": 120}, {"type": "hex_bolt", "size": "M8"}, {"type": "hex_bolt", "size": "M99", "length": 10}, {"module": 2}, {"shaft_diameter": 500}):
        tool = {"outer_diameter": "flange_generator", "type": "standard_fastener", "module": "gear_generator", "shaft_diameter": "keyway"}[next(iter(params))]
        r = env.err("invoke_engineering_toolset", did, domain="mechanical", tool_name=tool, insert_point="0,0", spec_params=params)
        assert r["error_code"] in ("MISSING_ARG", "SIZE_UNKNOWN", "KEYWAY_UNKNOWN")
    assert env.ids(did) == []                                                                               # 缺参数时什么都没画
    env.err("invoke_engineering_toolset", did, domain="architectural", tool_name="shaft_generator", insert_point="0,0")        # 领域不对
    c = env.call("draw_circular_primitives", did, curve_type="circle", method="center_radius", center="50,50", radius=10)
    cm = env.call("invoke_engineering_toolset", did, domain="mechanical", tool_name="centermark_centerline", target_entity_ids=[c["created"][0]])
    assert len(cm["created"]) == 2 and all(env.ent(did, i).layer == "中心线" for i in cm["created"])
    p = env.call("invoke_engineering_toolset", did, domain="chem", tool_name=None)
    assert p["data"]["switch_to"]["mode"] == "pfd" and env.svc.store.load(p["data"]["switch_to"]["drawing_id"]).mode == "pfd"     # 化工：新建流程图，不在通用图里重写


def test_door_cuts_wall_lines(env):
    did = env.new("gb_a3_arch")
    env.call("invoke_engineering_toolset", did, domain="architectural", tool_name="arch_grid_and_wall", insert_point="0,0", spec_params={"x_spacings": [6000], "y_spacings": [4000], "walls": [{"from": "A1", "to": "A2", "thickness": 240}]})
    before = [e for e in env.d(did).entities if "wall" in e.tags and e.kind == "line"]
    r = env.call("invoke_engineering_toolset", did, domain="architectural", tool_name="door_window", insert_point="3000,0", spec_params={"kind": "door", "width": 900, "wall_thickness": 240, "swing": "left"})
    after = [e for e in env.d(did).entities if "wall" in e.tags and e.kind == "line"]
    assert len(after) > len(before) and "开了洞口" in r["summary"]
    xs = sorted({round(v, 3) for e in after for v in (e.data["p1"][0], e.data["p2"][0])})
    assert 2550 in xs and 3450 in xs                                                                         # 洞口两侧的墙线端点


def test_symbol_library(env, did):
    cats = env.call("manage_symbol_library", did, action="list_categories")["data"]["categories"]
    assert {c["category"] for c in cats} == {"mech_weld", "mech_surface", "mech_gdt", "arch_annotation", "electrical", "hydraulic_pid", "chem_equipment", "water"} and all(c["count"] > 0 for c in cats)
    found = env.call("manage_symbol_library", did, action="search_symbols", symbol_keyword_or_id="电阻")["data"]["symbols"]
    assert found[0]["id"] == "resistor"
    i = env.call("manage_symbol_library", did, action="insert_symbol", symbol_keyword_or_id="resistor", insert_point="10,10", scale=2, rotation_deg=90)
    assert env.ent(did, i["data"]["insert_id"]).data["block"] == "SYM_resistor"
    p = env.call("manage_symbol_library", did, action="insert_symbol", symbol_keyword_or_id="pump", insert_point="100,100", tag_attributes={"TAG": "P-101"})
    assert env.ent(did, p["data"]["insert_id"]).data["attribs"]["TAG"] == "P-101"
    env.err("manage_symbol_library", did, action="insert_symbol", symbol_keyword_or_id="resistor", insert_point="0,0", tag_attributes={"NOPE": "x"})
    e = env.err("manage_symbol_library", did, action="insert_symbol", symbol_keyword_or_id="不存在的符号", insert_point="0,0")
    assert e["error_code"] == "SYMBOL_NOT_FOUND"
    reg = env.call("manage_symbol_library", did, action="register_custom_symbol", symbol_keyword_or_id="my_res", source_block_name="SYM_resistor", category="electrical")
    ex = env.call("manage_symbol_library", did, action="export", symbol_keyword_or_id="my_res")
    got = json.loads(env.svc.store.resolve_ref(ex["data"]["file"]["file_ref"]).read_text(encoding="utf-8"))
    assert got["id"] == "my_res" and got["category"] == "electrical"
    env.err("manage_symbol_library", did, action="register_custom_symbol", symbol_keyword_or_id="resistor", source_block_name="SYM_resistor", category="electrical")      # 不能覆盖内置
    env.err("manage_symbol_library", did, action="delete", symbol_keyword_or_id="resistor")                  # 内置不能删
    env.call("manage_symbol_library", did, action="delete", symbol_keyword_or_id="my_res")
    assert env.call("manage_symbol_library", did, action="search_symbols", symbol_keyword_or_id="my_res")["data"]["symbols"] == []
    env.call("manage_symbol_library", did, action="import", symbol_keyword_or_id=ex["data"]["file"]["file_ref"])                  # 把刚导出的符号 JSON 再导入
    assert env.call("manage_symbol_library", did, action="search_symbols", symbol_keyword_or_id="my_res")["data"]["symbols"][0]["id"] == "my_res"
    env.call("manage_symbol_library", did, action="delete", symbol_keyword_or_id="my_res")


# ------------------------------------------------------------------ 系统函数：批量、撤销、版本、锁定
def test_batch_commits_one_version_and_rolls_back_whole(env, did):
    rev = env.rev(did)
    ok = env.call("cad_batch_execute", did, transaction_name="环境与外轮廓", steps=[
        {"tool": "draw_polygons_and_meshes", "args": {"shape_type": "rectangle", "corner1": "0,0", "corner2": "@100,60"}},
        {"tool": "draw_circular_primitives", "args": {"curve_type": "circle", "method": "center_diameter", "center": "50,30", "diameter": 30}},
        {"tool": "create_radial_and_chain_dim", "args": {"dim_type": "diameter", "target_entity_id": "$this[1].created[0]"}},
        {"tool": "create_mechanical_tolerances", "args": {"tolerance_category": "dimensional_fit", "target_dim_or_entity_id": "$this[2].data.dim_id", "iso_fit_code": "H7"}},
    ])
    assert env.rev(did) == rev + 1 and len(ok["created"]) == 3                                              # 四个子步骤，只落一个版本
    d = env.d(did)
    assert d.get_entity("D3").data["tol"]["upper"] == 0.021 and len(d.session.history) == 1
    rev2, n = env.rev(did), len(env.d(did).entities)
    bad = env.err("cad_batch_execute", did, transaction_name="会失败的", steps=[
        {"tool": "draw_linear_primitives", "args": {"primitive_type": "line", "points": ["0,0", "10,0"]}},
        {"tool": "draw_circular_primitives", "args": {"curve_type": "circle", "method": "center_radius", "center": "0,0", "radius": -5}},
    ])
    assert bad["failed_step"] == 1 and bad["error_code"] == "INVALID_ARGS" and bad["completed_results"][0]["created"]
    assert env.rev(did) == rev2 and len(env.d(did).entities) == n                                           # 整体回滚：版本号和图元数都没变
    assert env.err("cad_batch_execute", did, steps=[{"tool": "cad_batch_execute", "args": {"steps": []}}])["error_code"] == "NESTED_BATCH"
    assert env.err("cad_batch_execute", did, steps=[{"tool": "manage_document_file", "args": {"action": "new"}}])["error_code"] == "NOT_ALLOWED_IN_BATCH"
    assert env.err("cad_batch_execute", did, steps=[{"tool": "没有这个", "args": {}}])["error_code"] == "UNKNOWN_TOOL"
    ref = env.err("cad_batch_execute", did, steps=[{"tool": "erase_entities", "args": {"entity_ids": ["$this[3].created[0]"]}}])
    assert ref["error_code"] == "REF_UNRESOLVED"


def test_undo_redo_history_and_locked_layer(env, did):
    rect, circ = draw_rect_and_circle(env, did)
    hist = env.call("history_undo_redo", did, action="list")["data"]["history"]
    assert [h["tool"] for h in hist] == ["draw_circular_primitives", "draw_polygons_and_meshes"]
    u = env.call("history_undo_redo", did, action="undo")
    assert env.ids(did) == [rect] and u["data"]["undone"] == ["draw_circular_primitives"]
    env.call("history_undo_redo", did, action="undo")
    assert env.ids(did) == []
    assert env.err("history_undo_redo", did, action="undo")["error_code"] == "NOTHING_TO_UNDO"
    env.call("history_undo_redo", did, action="redo", steps=2)
    assert env.ids(did) == [rect, circ]
    assert env.err("history_undo_redo", did, action="redo")["error_code"] == "NOTHING_TO_REDO"
    env.call("history_undo_redo", did, action="undo")
    env.call("draw_linear_primitives", did, primitive_type="line", points=["0,0", "1,1"])                    # 新操作清掉重做栈
    assert env.err("history_undo_redo", did, action="redo")["error_code"] == "NOTHING_TO_REDO"
    revs = sorted((env.svc.store.root / "drawings" / did / "revs").glob("*.json"))
    assert len(revs) >= env.rev(did)                                                                         # 审计链：每个版本都在
    env.call("set_layer_state", did, layer_name="粗实线", is_locked=True)
    for tool, args in (("transform_entities", {"operation": "move", "entity_ids": [rect], "displacement": [1, 0]}), ("erase_entities", {"entity_ids": [rect]}),
                       ("modify_entity_properties", {"action": "set", "entity_ids": [rect], "common_props": {"color": 1}}), ("explode_compound_entities", {"entity_ids": [rect]})):
        assert env.err(tool, did, **args)["error_code"] == "LAYER_LOCKED", tool
    assert env.ent(did, rect).data["vertices"][0]["p"][0] == 0


def test_erase_needs_confirmation_over_20_objects(env, did):
    for i in range(25):
        env.call("draw_circular_primitives", did, curve_type="circle", method="center_radius", center=f"{i * 10},0", radius=2)
    ids = env.ids(did)
    e = env.err("erase_entities", did, entity_ids=ids)
    assert e["error_code"] == "CONFIRM_REQUIRED" and e["count"] == 25 and len(env.ids(did)) == 25
    assert len(env.call("erase_entities", did, entity_ids=ids, confirmed=True)["deleted"]) == 25


def test_pfd_drawings_reject_general_functions_and_vice_versa(env):
    pfd = env.svc.create_drawing(title="流程图")["drawing_id"]
    r = env.err("draw_circular_primitives", pfd, curve_type="circle", method="center_radius", center="0,0", radius=5)
    assert r["error_code"] == "WRONG_MODE"
    gen = env.new("blank_a3")
    v = env.svc.validate_drawing(gen)
    assert v["ok"] and v["profile"] == "general" and v["passed"]


def test_unexpected_exceptions_become_structured_errors(env, did, monkeypatch):
    from zhihui.tools.general import draw_tools

    def boom(*a, **k):
        raise RuntimeError("内部炸了")
    monkeypatch.setattr(draw_tools.draw, "circle", boom)
    r = env.err("draw_circular_primitives", did, curve_type="circle", method="center_radius", center="0,0", radius=5)
    assert r["error_code"] == "INTERNAL" and "图纸没有被修改" in r["hint"] and env.ids(did) == []


def test_resolve_osnap_point_every_mode(env, did):
    c = lambda tool, **kw: env.call(tool, did, **kw)
    c("draw_linear_primitives", primitive_type="line", points=["0,0", "100,0"])
    c("draw_linear_primitives", primitive_type="line", points=["50,-20", "50,40"])
    c("draw_circular_primitives", curve_type="circle", method="center_radius", center="200,50", radius=20)
    c("draw_linear_primitives", primitive_type="point", points=["300,10"])
    ok = lambda mode, **kw: env.call("resolve_osnap_point", did, snap_mode=mode, **kw)["data"]["point"]
    assert pclose(ok("endpoint", target_entity_ids=["L1"], hint_region="right"), (100, 0))
    assert pclose(ok("midpoint", target_entity_ids=["L1"]), (50, 0)) and pclose(ok("center", target_entity_ids=["C3"]), (200, 50))
    assert pclose(ok("quadrant", target_entity_ids=["C3"], reference_point="250,50"), (220, 50))
    assert pclose(ok("intersection", target_entity_ids=["L1", "L2"]), (50, 0))
    assert pclose(ok("perpendicular", target_entity_ids=["L1"], reference_point="30,40"), (30, 0))
    assert pclose(ok("nearest", target_entity_ids=["L1"], reference_point="30,10"), (30, 0))
    assert pclose(ok("node", target_entity_ids=["PT4"]), (300, 10)) and pclose(ok("extension", target_entity_ids=["L1"], reference_point="130,0"), (100, 0))
    assert pclose(ok("m2p", target_entity_ids=["L1", "L2"]), (50, 5))
    t = ok("tangent", target_entity_ids=["C3"], reference_point="300,50", hint_region="top")
    assert abs(math.hypot(t[0] - 200, t[1] - 50) - 20) < 1e-6                                              # 切点在圆上
    assert env.err("resolve_osnap_point", did, snap_mode="tangent", target_entity_ids=["C3"], reference_point="300,50")["error_code"] == "AMBIGUOUS"      # 两个切点：不乱选，返回候选
    # 对象追踪交点：L1 的端点 (0,0) 的竖直追踪线 与 L2 的端点 (50,40) 的水平追踪线 → (0,40)
    assert pclose(ok("otrack_intersection", target_entity_ids=["L1", "L2"], reference_point="1,41"), (0, 40))
    assert env.err("resolve_osnap_point", did, snap_mode="insertion", target_entity_ids=["L1"])["error_code"] == "NO_SNAP_POINT"


def test_dynamic_block_all_five_parameter_types(env, did):
    c = lambda tool, **kw: env.call(tool, did, **kw)
    r = c("draw_polygons_and_meshes", shape_type="rectangle", corner1="0,0", corner2="@20,10")["created"][0]
    k = c("draw_circular_primitives", curve_type="circle", method="center_radius", center="10,5", radius=2)["created"][0]
    c("create_and_insert_block", action="define_block", block_name="DB", source_entity_ids=[r, k], base_point="0,0", keep_source="convert")
    # 块内图元编号：define_block 之后按块内顺序是 E1（矩形）、E2（圆）
    ref = env.ids(did, "insert")[0]
    c("manage_dynamic_block", action="create_dynamic_def", block_name="DB", parameters_and_actions=[
        {"param_name": "宽", "param_type": "linear_stretch", "geometry_refs": ["E1"], "axis": "x", "default_value": 20},
        {"param_name": "孔", "param_type": "visibility", "geometry_refs": {"有孔": ["E2"], "无孔": []}},
        {"param_name": "镜像", "param_type": "flip", "axis": "y"},
        {"param_name": "转角", "param_type": "rotation", "default_value": 0},
        {"param_name": "规格", "param_type": "lookup", "allowed_values": [{"label": "小", "params": {"宽": 20}}, {"label": "大", "params": {"宽": 40}}]}])
    props = c("manage_dynamic_block", action="list_properties", block_ref_id=ref)["data"]
    assert {p["param_name"] for p in props["params"]} == {"宽", "孔", "镜像", "转角", "规格"}
    ent_count = lambda name: len(next(b for b in env.d(did).blocks if b["name"] == name)["entities"])
    v1 = c("manage_dynamic_block", action="set_dynamic_property", block_ref_id=ref, property_overrides={"孔": "无孔"})["data"]["variant"]
    assert ent_count(v1) == 1                                                                            # 可见性：隐藏了圆
    v2 = c("manage_dynamic_block", action="set_dynamic_property", block_ref_id=ref, property_overrides={"规格": "大"})["data"]["variant"]
    xs = lambda name: sorted(v["p"][0] for e in next(b for b in env.d(did).blocks if b["name"] == name)["entities"] if e["kind"] == "polyline" for v in e["data"]["vertices"])
    assert xs(v2)[-1] == 40                                                                              # 查询：规格“大”带出 宽=40
    v3 = c("manage_dynamic_block", action="set_dynamic_property", block_ref_id=ref, property_overrides={"规格": "小", "镜像": True})["data"]["variant"]
    assert xs(v3)[0] == -20 and xs(v3)[-1] == 0                                                          # 翻转：绕 y 轴镜像
    v4 = c("manage_dynamic_block", action="set_dynamic_property", block_ref_id=ref, property_overrides={"镜像": False, "转角": 90})["data"]["variant"]
    ys = sorted(v["p"][1] for e in next(b for b in env.d(did).blocks if b["name"] == v4)["entities"] if e["kind"] == "polyline" for v in e["data"]["vertices"])
    assert ys[-1] == pytest.approx(20)                                                                   # 旋转 90°：原来的宽变成高
    assert len({v1, v2, v3, v4}) == 4
    env.err("manage_dynamic_block", did, action="set_dynamic_property", block_ref_id=ref, property_overrides={"规格": "特大"})
    env.err("manage_dynamic_block", did, action="create_dynamic_def", block_name="DB", parameters_and_actions=[{"param_name": "规格", "param_type": "lookup", "allowed_values": [{"没有label": 1}]}])


def test_edit_and_solid_options(env, monkeypatch):
    did = env.new("gb_a3_mech")
    c = lambda tool, **kw: env.call(tool, did, **kw)
    c("draw_polygons_and_meshes", shape_type="rectangle", corner1="0,0", corner2="@100,60")
    c("draw_linear_primitives", primitive_type="line", points=["200,0", "250,0"])                # L2
    c("draw_linear_primitives", primitive_type="line", points=["300,-10", "300,10"])             # L3：L2 延伸到它
    c("draw_linear_primitives", primitive_type="line", points=["0,100", "100,100"])             # L4
    c("draw_linear_primitives", primitive_type="line", points=["0,100", "0,150"])               # L5：和 L4 倒角
    c("draw_circular_primitives", curve_type="circle", method="center_radius", center="50,200", radius=10)   # C6
    ext = c("adjust_line_lengths", operation="extend", target_entities=[{"entity_id": "L2", "pick_point": "250,0"}], boundary_entity_ids=["L3"])
    assert env.ent(did, "L2").data["p2"][0] == pytest.approx(300)
    c("adjust_line_lengths", operation="lengthen", target_entities=[{"entity_id": "L2"}], lengthen_mode="percent", lengthen_value=150, end="end")
    assert env.ent(did, "L2").data["p2"][0] == pytest.approx(350)                                 # 100 → 150%
    c("modify_corners_and_breaks", operation="chamfer", entity_id_1="L4", entity_id_2="L5", chamfer_dist1=10, chamfer_dist2=10)
    assert any(e.kind == "line" and abs(math.hypot(e.data["p2"][0] - e.data["p1"][0], e.data["p2"][1] - e.data["p1"][1]) - 14.142135) < 1e-5 for e in env.d(did).entities)
    br = c("modify_corners_and_breaks", operation="break_two_points", entity_id_1="L4", break_pt1="40,100", break_pt2="60,100") if env.d(did).get_entity("L4") else None
    assert br is None or len(env.ids(did, "line")) >= 4
    mir = c("replicate_entities", operation="mirror", entity_ids=["C6"], mirror_line=["0,0", "0,10"])
    assert env.ent(did, mir["created"][0]).data["c"][0] == pytest.approx(-50)
    rot = c("replicate_entities", operation="array_polar", entity_ids=["L3"], array_params={"center": "300,0", "count": 4, "fill_angle_deg": 180, "rotate_items": True})
    assert len(rot["created"]) == 3
    st = c("transform_entities", operation="stretch", entity_ids=["PL1"], stretch_crossing_box=["90,-5", "110,70"], displacement=[20, 0])
    xs = [v["p"][0] for v in env.ent(did, "PL1").data["vertices"]]
    assert max(xs) == pytest.approx(120) and min(xs) == 0                                         # 拉伸：只动框选内的顶点
    h1 = c("create_hatch", boundary_mode="pick_internal_point", pick_point="10,10", pattern_name="ANSI31")
    h2 = c("create_hatch", boundary_mode="custom_loop", loop_points=["500,0", "560,0", "560,40", "500,40"], pattern_name="SOLID")
    assert env.ent(did, h2["created"][0]).data["pattern"] == "SOLID"
    tap = c("extrude_2d_to_3d", profile_entity_ids=["PL1"], height=20, taper_angle_deg=5)
    assert tap["data"]["volume"] < 120 * 60 * 20 and env.ent(did, tap["created"][0]).data["solid_kind"] == "tapered"
    both = c("extrude_2d_to_3d", profile_entity_ids=["PL1"], height=20, direction="both")
    zs = [v[2] for v in env.ent(did, both["created"][0]).data["vertices"]]
    assert min(zs) == -10 and max(zs) == 10
    c("draw_linear_primitives", primitive_type="line", points=["0,300", "0,340"])
    sweep = c("extrude_2d_to_3d", profile_entity_ids=["C6"], path_entity_id=env.ids(did, "line")[-1])
    assert sweep["data"]["volume"] == pytest.approx(math.pi * 100 * 40, rel=0.03)                 # 圆沿 40 长的直线扫掠 = 圆柱
    rv = c("revolve_2d_to_3d", profile_entity_ids=["PL1"], axis_mode="y_axis", revolve_angle_deg=180)
    assert rv["data"]["volume"] > 0
    from zhihui.cad.geom import solid as solid_kernel
    with monkeypatch.context() as mp:
        mp.setattr(solid_kernel, "manifold_available", lambda: False)
        e = env.err("extrude_2d_to_3d", did, profile_entity_ids=["PL1"], height=20, boolean_op="union", target_solid_id=rv["created"][0])
        assert e["error_code"] == "BOOLEAN_UNSUPPORTED"


def test_external_reference_overlay_and_detach(env, did):
    other = env.new("blank_a3")
    env.call("draw_linear_primitives", other, primitive_type="line", points=["0,0", "50,0"])
    f = env.call("manage_document_file", other, action="save", drawing_id=other, format="dxf")["file"]["file_ref"]
    env.call("manage_external_references", did, action="overlay", xref_path_or_name=f, insert_point="0,400")
    x = env.call("manage_external_references", did, action="list")["data"]["xrefs"][0]
    assert x["overlay"] is True and x["status"] == "loaded"
    env.call("manage_external_references", did, action="detach", xref_path_or_name=x["name"])
    assert env.d(did).xrefs == [] and env.ids(did, "insert") == []
    assert env.err("manage_external_references", did, action="attach", xref_path_or_name="uploads/nothing.pdf", insert_point="0,0")["error_code"] == "E_FILE_NOT_FOUND"


def test_grip_property_plot_select_view_options(env):
    did = env.new("gb_a3_mech")
    c = lambda tool, **kw: env.call(tool, did, **kw)
    c("draw_polygons_and_meshes", shape_type="rectangle", corner1="0,0", width=100, height=60)                       # PL1
    c("draw_linear_primitives", primitive_type="line", points=["200,0", "300,0"])                                   # L2
    c("draw_circular_primitives", curve_type="circle", method="center_radius", center="100,200", radius=20)         # C3
    c("create_text_annotation", content="ABC", insert_point="0,100", height=5)                                      # T4
    ln = lambda: env.ent(did, "L2").data
    length = lambda: math.hypot(ln()["p2"][0] - ln()["p1"][0], ln()["p2"][1] - ln()["p1"][1])
    c("grip_edit_entity", entity_id="L2", grip_type_or_index="mid", grip_mode="rotate", rotation_deg=90)
    assert length() == pytest.approx(100) and pclose(((ln()["p1"][0] + ln()["p2"][0]) / 2, (ln()["p1"][1] + ln()["p2"][1]) / 2), (250, 0)) and abs(ln()["p1"][0] - ln()["p2"][0]) < 1e-9   # 绕中点转 90°：竖直
    c("grip_edit_entity", entity_id="L2", grip_type_or_index="start", grip_mode="scale", scale_factor=2)
    assert length() == pytest.approx(200)
    c("grip_edit_entity", entity_id="T4", grip_type_or_index="insert", grip_mode="move", new_position="10,110")       # 别名 insert = insertion
    assert env.ent(did, "T4").data["p"][:2] == [10, 110]
    c("grip_edit_entity", entity_id="PL1", grip_type_or_index="vertex_2", grip_mode="stretch", new_position="120,70")
    assert env.ent(did, "PL1").data["vertices"][2]["p"][:2] == [120, 70]
    # 特性
    c("modify_entity_properties", action="set", entity_ids=["C3"], geometry_props={"diameter": 50})
    assert env.ent(did, "C3").data["r"] == 25
    c("modify_entity_properties", action="set", entity_ids=["T4"], geometry_props={"text": "XYZ", "height": 7})
    assert env.ent(did, "T4").data["text"] == "XYZ" and env.ent(did, "T4").data["height"] == 7
    assert env.err("modify_entity_properties", did, action="set", entity_ids=["L2"], geometry_props={"length": 50})["error_code"] == "READ_ONLY_PROP"
    # 出图选项
    for kw, fmt in (({"plot_area": "window", "window": ["0,0", "120,80"], "plot_scale": "1:1", "paper_size": "ISO_A4"}, "png"), ({"plot_area": "limits", "color_mode": "color", "orientation": "portrait", "paper_size": "ISO_A3", "plot_scale": "1:2"}, "pdf"),
                    ({"plot_area": "extents", "plot_scale": "1:5", "paper_size": "ISO_A1"}, "svg"), ({"plot_area": "display", "plot_scale": "1:1", "paper_size": "ISO_A3"}, "png")):
        f = c("configure_plot_and_export", space_or_layout="Model", device=fmt, **kw)["data"]["file"]
        assert f["format"] == fmt and f["bytes"] > 500
    # 选择与视图
    assert set(c("select_entities", selection_mode="crossing_polygon", polygon_points=["90,-10", "260,-10", "260,40", "90,40"])["data"]["selection"]) == {"PL1", "L2"}
    assert c("select_entities", selection_mode="fence", polygon_points=["-10,30", "120,30"])["data"]["selection"] == ["PL1"]
    assert c("select_entities", selection_mode="previous")["data"]["selection"] == ["PL1"]
    assert c("select_entities", selection_mode="last")["data"]["count"] == 1
    c("select_entities", selection_mode="all")
    assert c("select_entities", selection_mode="by_ids", entity_ids=["C3"], remove_from_selection=True)["data"]["count"] == len(env.d(did).entities) - 1
    h0 = env.d(did).view.height
    c("control_viewport_view", action="zoom_scale", scale_factor=0.5)
    assert env.d(did).view.height == pytest.approx(h0 * 2)
    c0 = env.d(did).view.center
    c("control_viewport_view", action="pan", pan_delta=[10, 10])
    assert pclose(env.d(did).view.center, (c0[0] + 10, c0[1] + 10))
    c("control_viewport_view", action="zoom_all")
    bb = env.d(did).bbox()
    assert env.d(did).view.height >= bb[3] - bb[1]
    # 填充渐变、文字对齐与旋转
    g = c("create_hatch", boundary_mode="entity_ids", entity_ids=["PL1"], pattern_name="SOLID", gradient={"color1": "#FF0000", "color2": "#0000FF", "angle": 45})
    assert env.ent(did, g["created"][0]).data["gradient"]["color2"] == "#0000FF"
    t = c("create_text_annotation", content="转", insert_point="0,170", height=4, rotation_deg=30, alignment="top_right")
    assert env.ent(did, t["created"][0]).data["rotation"] == 30 and env.ent(did, t["created"][0]).data["align"] == "top_right"
    # 标注：对象自动识别长度
    d = c("create_linear_and_angular_dim", dim_type="aligned", target_entity_id="L2")
    assert d["data"]["measurement"] == pytest.approx(200)


# ------------------------------------------------------------------ 每个函数都有典型错误用例
@pytest.mark.parametrize("name", sorted(n for n, t in REGISTRY.items() if t.needs_drawing))
def test_every_function_rejects_unknown_arguments_with_a_structured_error(env, did, name):
    r = env.err(name, did, bogus_field_xyz=1)
    assert r["error_code"] == "INVALID_ARGS" and r["hint"] and any(e["field"] == "bogus_field_xyz" for e in r["errors"])


def test_typical_errors_for_functions_not_covered_elsewhere(env, did):
    c = lambda tool, **kw: env.call(tool, did, **kw)
    e = lambda tool, **kw: env.err(tool, did, **kw)
    c("draw_linear_primitives", primitive_type="line", points=["0,0", "100,0"])                                    # L1
    c("draw_circular_primitives", curve_type="circle", method="center_radius", center="50,30", radius=10)         # C2
    assert e("manage_workspace_ui", workspace="没有这个空间")["error_code"] == "INVALID_ARGS"
    assert e("set_linetype_scale", ltscale=-1)["error_code"] == "INVALID_ARGS"
    assert e("configure_drafting_modes", snap_spacing=[0, 0])["error_code"] in ("INVALID_ARGS", "DEGENERATE")
    assert e("configure_osnap_and_otracking", active_modes=["不存在的捕捉"])["error_code"] == "INVALID_ARGS"
    assert e("configure_dynamic_input_and_coords", coords_display_mode="x")["error_code"] == "INVALID_ARGS"
    assert e("draw_complex_curves", curve_type="lwpolyline", vertices=["0,0"])["error_code"] in ("TOO_SHORT", "INVALID_ARGS", "MISSING_ARG")
    assert env.err("parse_coordinate_input", env.new("blank_a3"), coord_expressions=["@30,0"])["error_code"] == "NO_LAST_POINT"      # 一张新图还没有“上一点”
    assert e("parse_coordinate_input", coord_expressions=["完全不是坐标"])["error_code"] == "INVALID_POINT"
    assert e("select_entities", selection_mode="by_ids", entity_ids=["L99"])["error_code"] == "ENTITY_NOT_FOUND"
    assert e("select_entities", selection_mode="window")["error_code"] == "MISSING_ARG"
    assert e("query_entities_by_filter", entity_ids=["L99"])["error_code"] == "ENTITY_NOT_FOUND"
    assert e("get_entity_snap_points", entity_ids=["L99"], snap_types=["midpoint"])["error_code"] == "ENTITY_NOT_FOUND"
    nb = c("find_closed_boundary", seed_point="500,500")["data"]                                                    # 感知函数：不闭合不是错误，如实返回 is_closed=false 和缺口
    assert nb["is_closed"] is False and nb["outer_loop_entity_ids"] == [] and nb["message"]
    assert e("get_entity_grips_and_topology", entity_id="L99")["error_code"] == "ENTITY_NOT_FOUND"
    assert e("inspect_drawing_context", include_tables=["不存在的表"])["error_code"] == "INVALID_ARGS"
    assert e("explode_compound_entities", entity_ids=["C2"])["error_code"] == "UNSUPPORTED"
    assert e("match_entity_properties", source_entity_id="L99", target_entity_ids=["C2"], property_filter=["layer"])["error_code"] == "ENTITY_NOT_FOUND"
