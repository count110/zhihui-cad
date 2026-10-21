"""三维布尔运算：z 向棱柱走精确的 slab 路径（不需要额外依赖）；任意实体（旋转体/锥度体/扫掠体/旋转过的/上一次布尔的结果）走可选依赖 manifold3d。
没装 manifold3d 时，任意体布尔必须明确报 BOOLEAN_UNSUPPORTED 并给出安装提示。"""
import math

import pytest
from shapely.geometry import MultiPolygon, Point, Polygon

from gen_helpers import Env, pclose
from zhihui.cad.geom import solid
from zhihui.cad.geom.errors import GeomError

mf = pytest.importorskip("manifold3d", reason="任意体布尔需要可选依赖 manifold3d（pip install manifold3d）")


def sq(x0, y0, x1, y1):
    return MultiPolygon([Polygon([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])])


def signed_volume(d):
    import numpy as np
    V = np.array(d["vertices"])
    return sum(float(np.dot(V[a], np.cross(V[b], V[c]))) for a, b, c in d["faces"]) / 6.0


BOX = lambda: solid.extrude(sq(-20, -20, 20, 20), 40, base_z=-20)                                            # 40³，居中
CYL = lambda: solid.revolve(sq(-30, 0, 30, 10), (0, 0), (1, 0), 360)                                         # 沿 x 轴、半径 10、长 60，穿过方块
CYL_VOL_IN_BOX = math.pi * 100 * 40


# ------------------------------------------------------------------ 生成器的网格必须封闭且法线朝外（布尔运算和渲染都依赖）
HOLEY = MultiPolygon([Polygon([(0, 0), (40, 0), (40, 40), (0, 40)], [[(10, 10), (30, 10), (30, 30), (10, 30)]])])
GENERATORS = {
    "prism": lambda: solid.extrude(sq(0, 0, 20, 20), 10),
    "prism_holey": lambda: solid.extrude(HOLEY, 10),
    "prism_both": lambda: solid.extrude(sq(0, 0, 20, 20), 10, "both"),
    "tapered": lambda: solid.extrude(sq(0, 0, 20, 20), 10, taper_deg=5),
    "tapered_holey": lambda: solid.extrude(HOLEY, 10, taper_deg=3),
    "revolve_360": lambda: solid.revolve(sq(0, 10, 40, 30), (0, 0), (1, 0), 360),
    "revolve_180": lambda: solid.revolve(sq(0, 10, 40, 30), (0, 0), (1, 0), 180),
    "revolve_90": lambda: solid.revolve(sq(0, 10, 40, 30), (0, 0), (1, 0), 90),
    "revolve_touching_axis_180": lambda: solid.revolve(sq(0, 0, 40, 20), (0, 0), (1, 0), 180),
    "revolve_other_side_120": lambda: solid.revolve(sq(0, -30, 40, -10), (0, 0), (1, 0), 120),
    "revolve_negative_angle": lambda: solid.revolve(sq(0, 10, 40, 30), (0, 0), (1, 0), -120),
    "revolve_y_axis_90": lambda: solid.revolve(sq(10, 0, 30, 40), (0, 0), (0, 1), 90),
    "revolve_diagonal_axis_200": lambda: solid.revolve(sq(20, 0, 40, 10), (0, 0), (1, 1), 200),
    "revolve_holey_120": lambda: solid.revolve(MultiPolygon([Polygon([(0, 10), (40, 10), (40, 40), (0, 40)], [[(10, 20), (30, 20), (30, 30), (10, 30)]])]), (0, 0), (1, 0), 120),
    "sweep_line": lambda: solid.sweep(MultiPolygon([Point(0, 0).buffer(10, quad_segs=18)]), [(0, 0, 0), (0, 0, 40)]),
    "sweep_bent": lambda: solid.sweep(MultiPolygon([Point(0, 0).buffer(10, quad_segs=18)]), [(0, 0, 0), (0, 0, 30), (30, 0, 60)]),
    "sweep_holey": lambda: solid.sweep(HOLEY, [(0, 0, 0), (0, 30, 0)]),
}


@pytest.mark.parametrize("name", sorted(GENERATORS))
def test_generated_meshes_are_closed_manifolds_with_outward_normals(name):
    d = GENERATORS[name]()
    assert solid.mesh_problems(d["vertices"], d["faces"]) == {"open_edges": 0, "same_direction_edges": 0}
    assert signed_volume(d) == pytest.approx(d["volume"], rel=1e-9) and d["volume"] > 0


def test_orient_mesh_repairs_flipped_and_inverted_meshes():
    d = GENERATORS["prism"]()
    flipped = [list(f) for f in d["faces"]]
    flipped[3] = [flipped[3][0], flipped[3][2], flipped[3][1]]                                              # 一个面朝向反了
    assert solid.mesh_problems(d["vertices"], flipped)["same_direction_edges"] > 0
    fixed = solid.orient_mesh(d["vertices"], flipped)
    assert solid.mesh_problems(d["vertices"], fixed) == {"open_edges": 0, "same_direction_edges": 0}
    inside_out = [[a, c, b] for a, b, c in d["faces"]]                                                      # 整体翻面（体积为负）
    fixed2 = solid.orient_mesh(d["vertices"], inside_out)
    assert signed_volume({"vertices": d["vertices"], "faces": fixed2}) > 0


# ------------------------------------------------------------------ 任意体布尔
@pytest.mark.parametrize("op", ["subtract", "union", "intersect"])
def test_box_with_revolved_cylinder_volumes(op):
    r = solid.boolean(BOX(), CYL(), op)
    expected = {"subtract": 64000 - CYL_VOL_IN_BOX, "union": 64000 + math.pi * 100 * 60 - CYL_VOL_IN_BOX, "intersect": CYL_VOL_IN_BOX}[op]
    assert r["solid_kind"] == "boolean" and "slabs" not in r
    assert r["volume"] == pytest.approx(expected, rel=0.01)                                                 # 圆按 48 段折线逼近，差 <1%
    assert signed_volume(r) == pytest.approx(r["volume"], rel=1e-6)
    assert solid.mesh_problems(r["vertices"], r["faces"]) == {"open_edges": 0, "same_direction_edges": 0}
    bb = solid.mesh_bbox(r["vertices"])
    assert bb[0] >= -30 - 1e-6 and bb[3] <= 30 + 1e-6


def test_through_hole_by_revolved_cylinder_has_the_right_topology():
    r = solid.boolean(BOX(), CYL(), "subtract")
    import manifold3d as m
    man = m.Manifold(m.Mesh64(vert_properties=__import__("numpy").array(r["vertices"]), tri_verts=__import__("numpy").array(r["faces"], dtype="uint32")))
    assert man.genus() == 1                                                                                 # 通孔 = 一个“柄”


def test_tapered_and_swept_solids_can_be_boolean_operands():
    tap = solid.extrude(sq(-5, -5, 5, 5), 30, taper_deg=5, base_z=-5)                                       # z -5..25，只有 -5..20 在方块里
    sub, inter = solid.boolean(BOX(), tap, "subtract"), solid.boolean(BOX(), tap, "intersect")
    assert sub["volume"] + inter["volume"] == pytest.approx(64000, rel=1e-6)                                # 包含-排除：A−B 与 A∩B 的体积之和 = A
    assert inter["volume"] < tap["volume"] and solid.mesh_problems(sub["vertices"], sub["faces"])["open_edges"] == 0
    sw = solid.sweep(MultiPolygon([Point(0, 0).buffer(3, quad_segs=12)]), [(0, 0, -25), (0, 0, 25)])
    r2 = solid.boolean(BOX(), sw, "subtract")
    assert r2["volume"] == pytest.approx(64000 - math.pi * 9 * 40, rel=0.01)


def test_results_can_be_chained():
    r1 = solid.boolean(BOX(), CYL(), "subtract")
    tap = solid.extrude(sq(-5, -5, 5, 5), 30, taper_deg=5, base_z=0)
    r2 = solid.boolean(r1, tap, "subtract")
    assert r2["volume"] < r1["volume"] and signed_volume(r2) == pytest.approx(r2["volume"], rel=1e-6)
    assert solid.mesh_problems(r2["vertices"], r2["faces"]) == {"open_edges": 0, "same_direction_edges": 0}


def test_prism_results_with_steps_are_valid_operands_for_mesh_boolean():
    a = solid.extrude(sq(0, 0, 20, 20), 10)
    b = solid.extrude(sq(5, 5, 15, 15), 10, base_z=5)
    stepped = solid.boolean(a, b, "union")                                                                   # 阶梯形 slab 棱柱
    assert stepped["solid_kind"] == "prism" and len(stepped["slabs"]) == 2
    sw = solid.sweep(MultiPolygon([Point(0, 0).buffer(3, quad_segs=12)]), [(10, 10, -5), (10, 10, 25)])
    r = solid.boolean(stepped, sw, "subtract")
    assert r["volume"] == pytest.approx(stepped["volume"] - math.pi * 9 * 15, rel=0.02)                      # 孔穿过 0..15 的阶梯体


def test_two_prisms_use_the_exact_slab_path_even_without_manifold(monkeypatch):
    monkeypatch.setattr(solid, "manifold_available", lambda: False)
    a, b = solid.extrude(sq(0, 0, 20, 20), 10), solid.extrude(sq(5, 5, 15, 15), 10, base_z=5)
    r = solid.boolean(a, b, "subtract")
    assert r["solid_kind"] == "prism" and r["volume"] == pytest.approx(4000 - 1000 * 0.5)


def test_without_manifold3d_arbitrary_boolean_is_unsupported_with_install_hint(monkeypatch):
    monkeypatch.setattr(solid, "manifold_available", lambda: False)
    with pytest.raises(GeomError) as ex:
        solid.boolean(BOX(), CYL(), "subtract")
    assert ex.value.code == "BOOLEAN_UNSUPPORTED" and "pip install manifold3d" in ex.value.hint


def test_disjoint_intersection_is_an_empty_result_error():
    far = solid.revolve(sq(200, 0, 230, 10), (0, 0), (1, 0), 360)
    with pytest.raises(GeomError) as ex:
        solid.boolean(BOX(), far, "intersect")
    assert ex.value.code == "EMPTY_RESULT"


def test_open_mesh_is_rejected_with_a_clear_error_not_a_crash():
    cyl = CYL()
    broken = {**cyl, "faces": cyl["faces"][:-6]}                                                             # 缺几个面 → 不封闭
    with pytest.raises(GeomError) as ex:
        solid.boolean(BOX(), broken, "subtract")
    assert ex.value.code == "BOOLEAN_FAILED" and ex.value.extra["open_edges"] > 0


def test_coplanar_faces_are_handled():
    flush = solid.revolve(sq(0, 0, 20, 10), (0, 0), (1, 0), 360)                                              # 圆柱 x 0..20、半径 10，端面和方块的 x=20 面共面
    r = solid.boolean(BOX(), flush, "subtract")                                                               # 盲孔
    assert r["volume"] == pytest.approx(64000 - math.pi * 100 * 20, rel=0.01)
    assert solid.mesh_problems(r["vertices"], r["faces"]) == {"open_edges": 0, "same_direction_edges": 0}
    u = solid.boolean(BOX(), flush, "union")                                                                  # 圆柱完全在方块里：并集 = 方块
    assert u["volume"] == pytest.approx(64000, rel=1e-6)
    with pytest.raises(GeomError) as ex:
        solid.boolean(BOX(), solid.extrude(sq(-20, -20, 20, 20), 40, base_z=-20), "subtract")                  # 自己减自己 → 空
    assert ex.value.code == "EMPTY_RESULT"


# ------------------------------------------------------------------ 经函数调用（事务、版本、撤销、导出、渲染）
@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def build_plate_and_hole(env):
    did = env.new("blank_a3")
    c = lambda tool, **kw: env.call(tool, did, **kw)
    c("draw_polygons_and_meshes", shape_type="rectangle", corner1="-20,-20", corner2="20,20")
    sid = c("extrude_2d_to_3d", profile_entity_ids=["PL1"], height=40, direction="+z")["data"]["solid_id"]
    c("draw_polygons_and_meshes", shape_type="rectangle", corner1="-30,0", corner2="30,10")
    prof = env.ids(did, "polyline")[-1]
    return did, sid, prof


def test_revolve_boolean_through_the_tool_creates_one_version_and_can_be_undone(env):
    did, sid, prof = build_plate_and_hole(env)
    before = env.ent(did, sid).data
    rev = env.rev(did)
    r = env.call("revolve_2d_to_3d", did, profile_entity_ids=[prof], axis_mode="x_axis", boolean_op="subtract", target_solid_id=sid)
    e = env.ent(did, sid)
    assert env.rev(did) == rev + 1 and r["modified"] == [sid] and e.data["solid_kind"] == "boolean" and "boolean" in e.tags and "prism" not in e.tags
    assert e.data["volume"] == pytest.approx(64000 - math.pi * 100 * 40 / 2, rel=0.01)                      # 板是 z 0..40，圆柱 z ±10：只有一半在板里
    env.call("history_undo_redo", did, action="undo")
    assert env.ent(did, sid).data["solid_kind"] == "prism" and env.ent(did, sid).data["volume"] == pytest.approx(before["volume"])
    env.call("history_undo_redo", did, action="redo")
    assert env.ent(did, sid).data["solid_kind"] == "boolean"


def test_boolean_result_can_be_moved_rotated_exported_and_rendered(env, tmp_path):
    did, sid, prof = build_plate_and_hole(env)
    env.call("revolve_2d_to_3d", did, profile_entity_ids=[prof], axis_mode="x_axis", boolean_op="subtract", target_solid_id=sid)
    v0 = env.ent(did, sid).data["volume"]
    env.call("transform_entities", did, operation="move", entity_ids=[sid], displacement=[100, 50])
    env.call("transform_entities", did, operation="rotate", entity_ids=[sid], base_point="100,50", rotation_angle_deg=30)
    assert env.ent(did, sid).data["volume"] == pytest.approx(v0, rel=1e-3)                                    # 刚体变换不改体积
    stl = env.call("manage_document_file", did, action="save", drawing_id=did, format="stl")["file"]
    text = env.svc.store.resolve_ref(stl["file_ref"]).read_text(encoding="utf-8")
    assert text.count("facet normal") == len(env.ent(did, sid).data["faces"])
    import ezdxf
    dxf = env.call("manage_document_file", did, action="save", drawing_id=did, format="dxf")["file"]
    meshes = ezdxf.readfile(env.svc.store.resolve_ref(dxf["file_ref"])).modelspace().query("MESH")
    assert len(meshes) == 1 and len(list(meshes[0].faces)) == len(env.ent(did, sid).data["faces"])
    env.call("switch_3d_view_and_visual_style", did, standard_view="sw_isometric", visual_style="shaded")
    from zhihui.cad.render_general import render_general
    out = tmp_path / "boolean.png"
    render_general(env.d(did), out)
    assert out.stat().st_size > 2000


def test_rotated_plate_is_cut_by_the_revolved_cylinder(env):
    did, sid, prof = build_plate_and_hole(env)
    env.call("transform_entities", did, operation="rotate", entity_ids=[sid], base_point="0,0", rotation_angle_deg=30)       # 绕 z 转 30°：仍是 z 向棱柱，只是截面转了
    env.call("revolve_2d_to_3d", did, profile_entity_ids=[prof], axis_mode="x_axis", boolean_op="subtract", target_solid_id=sid)
    chord = 40 / math.cos(math.radians(30))                                                                   # x 轴穿过转了 30° 的方板：弦长 46.19
    assert env.ent(did, sid).data["volume"] == pytest.approx(64000 - math.pi * 100 * chord / 2, rel=0.01)


def test_scaled_target_still_works_as_a_boolean_target(env):
    did, sid, prof = build_plate_and_hole(env)
    env.call("transform_entities", did, operation="scale", entity_ids=[sid], base_point="0,0", scale_factor=2)
    r = env.call("revolve_2d_to_3d", did, profile_entity_ids=[prof], axis_mode="x_axis", boolean_op="subtract", target_solid_id=sid)
    assert r["ok"] and env.ent(did, sid).data["volume"] < 8 * 64000


def test_without_manifold3d_the_tool_reports_the_install_hint(env, monkeypatch):
    monkeypatch.setattr(solid, "manifold_available", lambda: False)
    did, sid, prof = build_plate_and_hole(env)
    rev = env.rev(did)
    e = env.err("revolve_2d_to_3d", did, profile_entity_ids=[prof], axis_mode="x_axis", boolean_op="subtract", target_solid_id=sid)
    assert e["error_code"] == "BOOLEAN_UNSUPPORTED" and "pip install manifold3d" in e["hint"] and env.rev(did) == rev and env.ent(did, sid).data["solid_kind"] == "prism"


# ------------------------------------------------------------------ 审查发现的回归：内腔壳（孔、空腔）必须保持朝内
def test_revolving_a_section_with_a_hole_keeps_the_cavity_shell_inward():
    holey = MultiPolygon([Polygon([(0, 10), (40, 10), (40, 40), (0, 40)], [[(10, 20), (30, 20), (30, 30), (10, 30)]])])
    d = solid.revolve(holey, (0, 0), (1, 0), 360)
    outer = solid.revolve(sq(0, 10, 40, 40), (0, 0), (1, 0), 360)["volume"]
    hole = solid.revolve(sq(10, 20, 30, 30), (0, 0), (1, 0), 360)["volume"]
    assert d["volume"] == pytest.approx(outer - hole, rel=1e-9)                                              # 外 − 孔，不是 外 + 孔
    assert signed_volume(d) == pytest.approx(d["volume"], rel=1e-9)
    assert solid.mesh_problems(d["vertices"], d["faces"]) == {"open_edges": 0, "same_direction_edges": 0}


def hollow_cylinder():
    big = solid.revolve(sq(-30, 0, 30, 20), (0, 0), (1, 0), 360)
    small = solid.revolve(sq(-10, 0, 10, 8), (0, 0), (1, 0), 360)                                            # 完全包在里面
    return solid.boolean(big, small, "subtract"), big, small


def test_enclosed_cavity_survives_when_a_boolean_result_is_reused():
    hollow, big, small = hollow_cylinder()
    assert hollow["volume"] == pytest.approx(big["volume"] - small["volume"], rel=1e-6)
    far = solid.revolve(sq(100, 0, 130, 10), (0, 0), (1, 0), 360)
    u = solid.boolean(hollow, far, "union")
    assert u["volume"] == pytest.approx(hollow["volume"] + far["volume"], rel=1e-6)                           # 空腔还在
    cube = solid.extrude(sq(-100, -100, 100, 100), 200, base_z=-100)
    i = solid.boolean(hollow, cube, "intersect")
    assert i["volume"] == pytest.approx(hollow["volume"], rel=1e-6)
    assert signed_volume(i) == pytest.approx(i["volume"], rel=1e-6)


def test_hollow_prism_without_slabs_keeps_its_cavity():
    outer = solid.extrude(sq(0, 0, 40, 40), 40)
    inner = solid.extrude(sq(10, 10, 30, 30), 20, base_z=10)
    hollow = solid.boolean(outer, inner, "subtract")                                                          # 全包在里面的空腔（slab 路径）
    assert hollow["volume"] == pytest.approx(64000 - 8000)
    stripped = {k: v for k, v in hollow.items() if k != "slabs"}                                              # 模拟移动/旋转后丢了 slab 结构
    cube = solid.extrude(sq(-100, -100, 100, 100), 200, base_z=-100)
    r = solid.boolean(stripped, cube, "intersect")
    assert r["volume"] == pytest.approx(56000, rel=1e-6)


def _box(x0, x1):
    v = [[x0, x0, x0], [x1, x0, x0], [x1, x1, x0], [x0, x1, x0], [x0, x0, x1], [x1, x0, x1], [x1, x1, x1], [x0, x1, x1]]
    f = [[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4], [1, 2, 6], [1, 6, 5], [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7]]
    return v, f


def test_orient_mesh_decides_each_shell_by_nesting_depth():
    vo, fo = _box(0, 40)
    vi, fi = _box(10, 30)
    V = vo + vi
    inward = [[a + 8, c + 8, b + 8] for a, b, c in fi]                                                       # 腔壳朝内（正确）
    outward = [[a + 8, b + 8, c + 8] for a, b, c in fi]                                                      # 腔壳朝外（错误）
    for name, faces, expected in (("cavity inward stays", fo + inward, 56000), ("cavity outward is fixed", fo + outward, 56000),
                                  ("everything inverted", [[a, c, b] for a, b, c in fo] + outward, 56000), ("outer inverted only", [[a, c, b] for a, b, c in fo] + inward, 56000)):
        fixed = solid.orient_mesh(V, faces)
        assert signed_volume({"vertices": V, "faces": fixed}) == pytest.approx(expected), name
    v2, f2 = _box(50, 60)
    far_flipped = fo + [[a + 8, c + 8, b + 8] for a, b, c in f2]
    assert signed_volume({"vertices": vo + v2, "faces": solid.orient_mesh(vo + v2, far_flipped)}) == pytest.approx(64000 + 1000)    # 不相交的两块各自修复


def test_tiny_solids_are_not_reported_as_empty():
    s = 1e-3
    cube = solid.extrude(sq(-20 * s, -20 * s, 20 * s, 20 * s), 40 * s, base_z=-20 * s)
    cyl = solid.revolve(sq(-30 * s, 0, 30 * s, 10 * s), (0, 0), (1, 0), 360)
    sub, inter = solid.boolean(cube, cyl, "subtract"), solid.boolean(cube, cyl, "intersect")
    assert sub["volume"] + inter["volume"] == pytest.approx(cube["volume"], rel=1e-6) and inter["volume"] > 0


def test_slab_path_and_mesh_path_agree_on_random_prisms():
    """差分测试：两个 z 向棱柱，精确的 slab 布尔 vs manifold 网格布尔，体积应当一致（形状都是精确多边形）。"""
    import random
    rnd = random.Random(20261020)
    def rand_prism():
        x0, y0 = rnd.uniform(-30, 10), rnd.uniform(-30, 10)
        w, h = rnd.uniform(5, 40), rnd.uniform(5, 40)
        outer = Polygon([(x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h)])
        if rnd.random() < 0.4 and w > 12 and h > 12:
            outer = Polygon(outer.exterior.coords, [[(x0 + 3, y0 + 3), (x0 + w - 3, y0 + 3), (x0 + w - 3, y0 + h - 3), (x0 + 3, y0 + h - 3)]])
        z0 = rnd.choice([0, 0, 5, -5, 10])
        return solid.extrude(MultiPolygon([outer]), rnd.choice([10, 20, 30]), base_z=z0)
    for _ in range(60):
        a, b = rand_prism(), rand_prism()
        for op in ("union", "subtract", "intersect"):
            try:
                exact = solid.boolean(a, b, op)
            except GeomError:
                with pytest.raises(GeomError):
                    solid.boolean_mesh(a, b, op)                                                              # 精确路径说“空”，网格路径也该说“空”
                continue
            mesh = solid.boolean_mesh(a, b, op)
            assert mesh["volume"] == pytest.approx(exact["volume"], rel=1e-6, abs=1e-6)
            assert solid.mesh_problems(mesh["vertices"], mesh["faces"]) == {"open_edges": 0, "same_direction_edges": 0}


def test_face_contact_result_keeps_the_correct_volume_and_stays_a_closed_manifold():
    """审查用例：B 的侧面和 A 的孔壁、底面全部共面。manifold3d 在这种“只擦边接触”的输入上会输出零厚度的碎片：
    体积和封闭性是对的，但包围盒可能偏大——这是已知限制（见功能对照表第 40 项），这里只锁住体积和封闭性。"""
    a = {"vertices": [], "faces": [], "slabs": [{"z0": 3, "z1": 7, "polys": [{"outer": [[0, 0], [8, 0], [8, 8], [0, 8]], "holes": [[[6, 6], [6, 3], [2, 3], [2, 6]]]}]}]}
    b = {"vertices": [], "faces": [], "slabs": [{"z0": 3, "z1": 6, "polys": [{"outer": [[4, 3], [4, 8], [2, 8], [2, 3]], "holes": []}]}]}
    r = solid.boolean_mesh(a, b, "intersect")
    assert r["volume"] == pytest.approx(12.0) and solid.mesh_problems(r["vertices"], r["faces"]) == {"open_edges": 0, "same_direction_edges": 0}
    ex = solid.boolean(a, b, "intersect")                                                                    # 两个棱柱走精确的 slab 路径，结果没有碎片
    assert ex["volume"] == pytest.approx(12.0) and solid.mesh_bbox(ex["vertices"]) == pytest.approx((2, 6, 3, 4, 8, 6))
