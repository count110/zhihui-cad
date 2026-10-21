"""命令快速通道：AutoCAD 风格命令和中文模板 → 函数调用；写法不全给出怎么写；不是命令返回 None。"""
import pytest

from zhihui.agent import command_line as CL


def one(text):
    c = CL.parse(text)
    assert c is not None and len(c.calls) == 1, (text, c)
    return c.calls[0]


@pytest.mark.parametrize("text,tool,args", [
    ("L 0,0 @100,0 @0,50", "draw_linear_primitives", {"primitive_type": "line", "points": ["0,0", "@100,0", "@0,50"], "close_loop": False}),
    ("L 0,0 @100,0 @0,50 C", "draw_linear_primitives", {"primitive_type": "line", "points": ["0,0", "@100,0", "@0,50"], "close_loop": True}),
    ("LINE 0,0 right 100 up 50", "draw_linear_primitives", {"primitive_type": "line", "points": ["0,0", "right 100", "up 50"], "close_loop": False}),
    ("PL 0,0 @50,0 @0,30 C", "draw_complex_curves", {"curve_type": "lwpolyline", "vertices": ["0,0", "@50,0", "@0,30"], "closed": True}),
    ("C 50,50 20", "draw_circular_primitives", {"curve_type": "circle", "method": "center_radius", "center": "50,50", "radius": 20.0}),
    ("C 50,50 D 30", "draw_circular_primitives", {"curve_type": "circle", "method": "center_diameter", "center": "50,50", "diameter": 30.0}),
    ("C 2P 0,0 10,0", "draw_circular_primitives", {"curve_type": "circle", "method": "2p", "points": ["0,0", "10,0"]}),
    ("C 3P 0,0 10,0 5,5", "draw_circular_primitives", {"curve_type": "circle", "method": "3p", "points": ["0,0", "10,0", "5,5"]}),
    ("C TTR L1 L2 10", "draw_circular_primitives", {"curve_type": "circle", "method": "ttr", "tangent_entities": ["L1", "L2"], "radius": 10.0}),
    ("A 0,0 10,10 20,0", "draw_circular_primitives", {"curve_type": "arc", "method": "3p", "points": ["0,0", "10,10", "20,0"]}),
    ("REC 0,0 @100,60", "draw_polygons_and_meshes", {"shape_type": "rectangle", "corner1": "0,0", "corner2": "@100,60"}),
    ("POL 6 0,0 I 50", "draw_polygons_and_meshes", {"shape_type": "polygon", "num_sides": 6, "center": "0,0", "radius": 50.0, "inscribed": True}),
    ("POL 5 0,0 C 30", "draw_polygons_and_meshes", {"shape_type": "polygon", "num_sides": 5, "center": "0,0", "radius": 30.0, "inscribed": False}),
    ("PO 10,10", "draw_linear_primitives", {"primitive_type": "point", "points": ["10,10"]}),
    ("H 50,50", "create_hatch", {"boundary_mode": "pick_internal_point", "pick_point": "50,50", "pattern_name": "ANSI31", "scale": 1.0}),
    ("H C8 PL3", "create_hatch", {"boundary_mode": "entity_ids", "entity_ids": ["C8", "PL3"], "pattern_name": "ANSI31", "scale": 1.0}),
    ("T 10,10 3.5 技术要求", "create_text_annotation", {"text_type": "single_line", "content": "技术要求", "insert_point": "10,10", "height": 3.5}),
    ("DLI 0,0 100,0 50,20", "create_linear_and_angular_dim", {"dim_type": "linear_auto", "ext_line1_point": "0,0", "ext_line2_point": "100,0", "dim_line_location": "50,20"}),
    ("DRA C8", "create_radial_and_chain_dim", {"dim_type": "radius", "target_entity_id": "C8"}),
    ("DDI c8", "create_radial_and_chain_dim", {"dim_type": "diameter", "target_entity_id": "C8"}),
    ("DAN L1 L2", "create_linear_and_angular_dim", {"dim_type": "angular", "angle_entity_ids": ["L1", "L2"]}),
    ("O 10 C8 外", "replicate_entities", {"operation": "offset", "entity_ids": ["C8"], "offset_distance": 10.0, "offset_side": "outside"}),
    ("O 5 L1 左", "replicate_entities", {"operation": "offset", "entity_ids": ["L1"], "offset_distance": 5.0, "offset_side": "left"}),
    ("O 5 L1 10,10", "replicate_entities", {"operation": "offset", "entity_ids": ["L1"], "offset_distance": 5.0, "offset_side_point": "10,10"}),
    ("F R5 L1 L2", "modify_corners_and_breaks", {"operation": "fillet", "entity_id_1": "L1", "entity_id_2": "L2", "fillet_radius": 5.0}),
    ("F R8 PL6", "modify_corners_and_breaks", {"operation": "fillet", "entity_id_1": "PL6", "fillet_radius": 8.0, "apply_to_entire_polyline": True}),
    ("CHA 5,3 L1 L2", "modify_corners_and_breaks", {"operation": "chamfer", "entity_id_1": "L1", "entity_id_2": "L2", "chamfer_dist1": 5.0, "chamfer_dist2": 3.0}),
    ("M C8 @10,0", "transform_entities", {"operation": "move", "entity_ids": ["C8"], "displacement": "@10,0"}),
    ("M C8 0,0 20,10", "transform_entities", {"operation": "move", "entity_ids": ["C8"], "base_point": "0,0", "target_point": "20,10"}),
    ("CO C8 0,0 @10,0", "replicate_entities", {"operation": "copy", "entity_ids": ["C8"], "base_point": "0,0", "target_points": ["@10,0"]}),
    ("RO PL6 0,0 30", "transform_entities", {"operation": "rotate", "entity_ids": ["PL6"], "base_point": "0,0", "rotation_angle_deg": 30.0}),
    ("SC C8 0,0 2", "transform_entities", {"operation": "scale", "entity_ids": ["C8"], "base_point": "0,0", "scale_factor": 2.0}),
    ("MI C8 0,0 10,0", "replicate_entities", {"operation": "mirror", "entity_ids": ["C8"], "mirror_line": ["0,0", "10,0"], "erase_source": False}),
    ("MI C8 0,0 10,0 Y", "replicate_entities", {"operation": "mirror", "entity_ids": ["C8"], "mirror_line": ["0,0", "10,0"], "erase_source": True}),
    ("ARR L1 3 4 20 30", "replicate_entities", {"operation": "array_rect", "entity_ids": ["L1"], "array_params": {"rows": 3, "cols": 4, "row_spacing": 20.0, "col_spacing": 30.0}}),
    ("ARP C8 0,0 6 360", "replicate_entities", {"operation": "array_polar", "entity_ids": ["C8"], "array_params": {"center": "0,0", "count": 6, "fill_angle_deg": 360.0, "rotate_items": True}}),
    ("E L3 C8", "erase_entities", {"entity_ids": ["L3", "C8"]}),
    ("X PL6", "explode_compound_entities", {"entity_ids": ["PL6"]}),
    ("LTS 2", "set_linetype_scale", {"ltscale": 2.0}),
    ("LTS", "set_linetype_scale", {"auto": True}),
    ("Z E", "control_viewport_view", {"action": "zoom_extents"}),
    ("Z A", "control_viewport_view", {"action": "zoom_all"}),
    ("Z W 0,0 100,100", "control_viewport_view", {"action": "zoom_window", "window_min": "0,0", "window_max": "100,100"}),
    ("Z 2", "control_viewport_view", {"action": "zoom_scale", "scale_factor": 2.0}),
    ("Z O C8", "control_viewport_view", {"action": "zoom_object", "target_entity_ids": ["C8"]}),
    ("U", "history_undo_redo", {"action": "undo", "steps": 1}),
    ("U 3", "history_undo_redo", {"action": "undo", "steps": 3}),
    ("REDO", "history_undo_redo", {"action": "redo", "steps": 1}),
    ("ORTHO ON", "configure_drafting_modes", {"ortho_on": True}),
    ("GRID OFF", "configure_drafting_modes", {"grid_on": False}),
    ("OSNAP ON", "configure_osnap_and_otracking", {"osnap_on": True}),
    ("DYN OFF", "configure_dynamic_input_and_coords", {"dynmode_on": False}),
    ("LIMITS 0,0 420,297", "configure_canvas_and_limits", {"limits_lower_left": "0,0", "limits_upper_right": "420,297"}),
    ("LIST C8", "modify_entity_properties", {"action": "get", "entity_ids": ["C8"]}),
    ("EXT PL1 C2 30", "extrude_2d_to_3d", {"profile_entity_ids": ["PL1", "C2"], "height": 30.0}),
    ("VIEW SW", "switch_3d_view_and_visual_style", {"standard_view": "sw_isometric"}),
    ("TR L3", "adjust_line_lengths", {"operation": "trim", "target_entities": [{"entity_id": "L3", "pick_point": "L3.mid"}], "boundary_entity_ids": None}),
])
def test_autocad_style_commands(text, tool, args):
    c = one(text)
    assert c["tool"] == tool and c["args"] == args


def test_layer_command_creates_and_sets_properties():
    c = CL.parse("LA 新建 中心线 红 CENTER")
    assert [x["tool"] for x in c.calls] == ["manage_layers", "set_layer_properties"]
    assert c.calls[1]["args"] == {"layer_name": "中心线", "color": 1, "linetype": "CENTER"}
    assert CL.parse("LA 中心线 设为当前").calls[0] == {"tool": "set_layer_state", "args": {"layer_name": "中心线", "set_current": True}}
    assert CL.parse("LA 虚线 冻结").calls[0]["args"] == {"layer_name": "虚线", "is_frozen": True}
    assert CL.parse("LA 粗实线 锁定").calls[0]["args"]["is_locked"] is True
    assert CL.parse("LA").calls[0]["args"]["action"] == "list"
    with pytest.raises(CL.CmdError):
        CL.parse("LA 新建")


@pytest.mark.parametrize("text,calls", [
    ("打开正交", [("configure_drafting_modes", {"ortho_on": True})]),
    ("关闭栅格", [("configure_drafting_modes", {"grid_on": False})]),
    ("打开正交和对象捕捉", [("configure_drafting_modes", {"ortho_on": True}), ("configure_osnap_and_otracking", {"osnap_on": True})]),
    ("把当前图层设为中心线", [("set_layer_state", {"layer_name": "中心线", "set_current": True})]),
    ("当前图层设为粗实线层", [("set_layer_state", {"layer_name": "粗实线", "set_current": True})]),
    ("放大", [("control_viewport_view", {"action": "zoom_in"})]),
    ("缩小", [("control_viewport_view", {"action": "zoom_out"})]),
    ("全部显示", [("control_viewport_view", {"action": "zoom_extents"})]),
    ("撤销", [("history_undo_redo", {"action": "undo", "steps": 1})]),
    ("撤销上一步", [("history_undo_redo", {"action": "undo", "steps": 1})]),
    ("刚才那步不要了", [("history_undo_redo", {"action": "undo", "steps": 1})]),
    ("撤销3步", [("history_undo_redo", {"action": "undo", "steps": 3})]),
    ("重做", [("history_undo_redo", {"action": "redo", "steps": 1})]),
    ("线型比例设为2", [("set_linetype_scale", {"ltscale": 2.0})]),
    ("画一个半径 20 的圆，圆心在 50,50", [("draw_circular_primitives", {"curve_type": "circle", "method": "center_radius", "center": "50,50", "radius": 20.0})]),
    ("画一个直径 30 的圆，圆心在 0,0", [("draw_circular_primitives", {"curve_type": "circle", "method": "center_diameter", "center": "0,0", "diameter": 30.0})]),
    ("画一条线从 0,0 到 @100,0", [("draw_linear_primitives", {"primitive_type": "line", "points": ["0,0", "@100,0"]})]),
])
def test_chinese_templates(text, calls):
    c = CL.parse(text)
    assert c is not None and [(x["tool"], x["args"]) for x in c.calls] == calls


@pytest.mark.parametrize("text,hint", [
    ("L 0,0", "L 0,0 @100,0"), ("C 50,50", "C 50,50 20"), ("REC 0,0", "REC 0,0 @100,60"), ("POL 6 0,0", "POL 6 0,0 I 50"), ("O 10 C8", "外/内/左/右"), ("F 5", "F R5 L1 L2"),
    ("M", "M C8 @10,0"), ("RO C8", "RO PL6 0,0 30"), ("ARR L1 3", "ARR L1 3 4 20 30"), ("E", "E L3"), ("Z X", "Z E"), ("ORTHO", "ORTHO ON"), ("T 10,10", "T 10,10 3.5"),
])
def test_incomplete_commands_say_how_to_write_them(text, hint):
    with pytest.raises(CL.CmdError) as ex:
        CL.parse(text)
    assert hint in ex.value.hint


@pytest.mark.parametrize("text", ["帮我设计一个法兰盘", "把小圆做成带属性的块，属性是规格", "C 语言怎么学", "hello world", "", "   ", "标注矩形的长和宽"])
def test_not_a_command_falls_through(text):
    assert CL.parse(text) is None


def test_multiple_calls_become_one_batch_and_help():
    c = CL.parse("打开正交和对象捕捉")
    b = c.as_call()
    assert b["tool"] == "cad_batch_execute" and len(b["args"]["steps"]) == 2
    h = CL.parse("help")
    assert h.help and "REC" in h.help and h.calls == []
