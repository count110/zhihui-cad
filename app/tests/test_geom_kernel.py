"""几何内核：曲线求交、最近点、偏移、圆角、修剪、相切圆、闭合边界、约束求解、拉伸/旋转/布尔、ISO 286。
每项至少一个正常用例和一个边界用例，数值精度 1e-6。"""
import math

import pytest

from gen_helpers import CIRCLE, LINE, RECT, Env, E, close, pclose
from zhihui.cad.geom import boundary, constraints as C, curves, mech, modify, solid
from zhihui.cad.geom.entities import Arc, Seg, Transform, entity_area, entity_bbox, primitives
from zhihui.cad.geom.errors import GeomError


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


# ------------------------------------------------------------------ 曲线
def test_line_line_and_circle_intersections():
    pts = curves.intersect(Seg((0, 0, 0), (10, 10, 0)), Seg((0, 10, 0), (10, 0, 0)))
    assert len(pts) == 1 and pclose(pts[0], (5, 5))
    assert curves.intersect(Seg((0, 0, 0), (1, 0, 0)), Seg((0, 1, 0), (1, 1, 0))) == []                      # 平行：无交点
    assert curves.intersect(Seg((0, 0, 0), (1, 0, 0)), Seg((5, -1, 0), (5, 1, 0))) == []                     # 线段不相交…
    assert pclose(curves.intersect(Seg((0, 0, 0), (1, 0, 0)), Seg((5, -1, 0), (5, 1, 0)), extend=True)[0], (5, 0))   # …延长后相交
    c = Arc((0, 0, 0), 5, 0, 360)
    xs = curves.intersect(Seg((-10, 0, 0), (10, 0, 0)), c)
    assert sorted(round(p[0], 6) for p in xs) == [-5.0, 5.0]
    assert curves.intersect(Seg((-10, 6, 0), (10, 6, 0)), c) == []                                            # 线在圆外
    tang = curves.intersect(Seg((-10, 5, 0), (10, 5, 0)), c)
    assert len(tang) == 1 and pclose(tang[0], (0, 5), 1e-6)                                                    # 相切：一个交点
    two = curves.intersect(Arc((0, 0, 0), 5, 0, 360), Arc((6, 0, 0), 5, 0, 360))
    assert len(two) == 2 and all(abs(math.hypot(p[0], p[1]) - 5) < 1e-6 for p in two)


def test_closest_midpoint_split_and_circle_from_3():
    s = Seg((0, 0, 0), (10, 0, 0))
    assert pclose(curves.closest(s, (3, 4, 0)), (3, 0)) and pclose(curves.closest(s, (-5, 4, 0)), (0, 0))      # 超出线段取端点
    assert pclose(curves.midpoint(s), (5, 0))
    parts = curves.split_at(s, [(3, 0, 0), (7, 0, 0)])
    assert [round(p.length, 6) for p in parts] == [3.0, 4.0, 3.0]
    c, r = curves.circle_from_3((0, 0, 0), (2, 0, 0), (1, 1, 0))
    assert pclose(c, (1, 0)) and close(r, 1.0)
    assert curves.circle_from_3((0, 0, 0), (1, 1, 0), (2, 2, 0)) is None                                      # 三点共线：无圆


def test_offset_prim_and_entity_errors():
    o = curves.offset_prim(Seg((0, 0, 0), (10, 0, 0)), 3, (5, 10, 0))
    assert pclose(o.a, (0, 3)) and pclose(o.b, (10, 3))
    c = curves.offset_prim(Arc((0, 0, 0), 10, 0, 360), 4, (0, 0, 0))                                           # 向内
    assert close(c.r, 6)
    assert curves.offset_prim(Arc((0, 0, 0), 10, 0, 360), 12, (0, 0, 0)) is None                              # 偏移距离大于半径：没有结果
    with pytest.raises(GeomError) as ex:
        modify.offset_entity(CIRCLE((0, 0), 10), 12, (0, 0, 0))
    assert ex.value.code == "OFFSET_TOO_LARGE" and ex.value.extra["max_distance"] == 10
    r = modify.offset_entity(RECT(0, 0, 100, 60), 10, (-5, -5, 0))                                             # 矩形向外偏移 10 → 120×80
    v = r[0]["data"]["vertices"]
    xs, ys = [p["p"][0] for p in v], [p["p"][1] for p in v]
    assert (min(xs), max(xs), min(ys), max(ys)) == (-10, 110, -10, 70)


# ------------------------------------------------------------------ 圆角 / 修剪 / 倒角
def test_fillet_two_lines_and_radius_too_large():
    ops = modify.fillet_lines(LINE((0, 0), (100, 0), "L1"), LINE((0, 0), (0, 100), "L2"), 10, (50, 0, 0), (0, 50, 0))
    arcs = [o for o in ops if o["op"] == "create" and o["kind"] == "arc"]
    assert len(arcs) == 1 and close(arcs[0]["data"]["r"], 10) and pclose(arcs[0]["data"]["c"], (10, 10))
    with pytest.raises(GeomError) as ex:
        modify.fillet_lines(LINE((0, 0), (20, 0), "L1"), LINE((0, 0), (0, 20), "L2"), 50, (10, 0, 0), (0, 10, 0))
    assert ex.value.code == "RADIUS_TOO_LARGE" and 0 < ex.value.extra.get("max_radius", 20) <= 20
    zero = modify.fillet_lines(LINE((0, 0), (100, 10), "L1"), LINE((0, 30), (100, -20), "L2"), 0, (80, 8, 0), (80, -16, 0))      # R0：直接闭合成角
    assert not [o for o in zero if o.get("kind") == "arc"]


def test_chamfer_and_polyline_corners():
    ops = modify.chamfer_lines(LINE((0, 0), (100, 0), "L1"), LINE((0, 0), (0, 100), "L2"), 10, 20, (50, 0, 0), (0, 50, 0))
    ln = [o for o in ops if o["op"] == "create" and o["kind"] == "line"]
    assert len(ln) == 1 and {tuple(round(v, 6) for v in ln[0]["data"]["p1"][:2]), tuple(round(v, 6) for v in ln[0]["data"]["p2"][:2])} == {(10.0, 0.0), (0.0, 20.0)}
    pc = modify.polyline_corners(RECT(0, 0, 100, 60), "fillet", 8)
    assert len(pc["vertices"]) == 8 and any(abs(v["bulge"]) > 0 for v in pc["vertices"])
    with pytest.raises(GeomError) as ex:
        modify.polyline_corners(RECT(0, 0, 100, 60), "fillet", 40)                                            # 60 的边容不下两个 R40
    assert ex.value.code == "RADIUS_TOO_LARGE"


def test_trim_extend_break_and_no_intersection():
    base = LINE((0, 0), (100, 0), "L1")
    cut = LINE((50, -10), (50, 10), "L2")
    ops = modify.trim_entity(base, (80, 0, 0), [cut])
    keep = [o for o in ops if o["op"] in ("replace", "create")]
    assert keep and pclose(keep[0]["data"]["p2"], (50, 0)) and pclose(keep[0]["data"]["p1"], (0, 0))          # 拾取点在右段：删右段
    with pytest.raises(GeomError) as ex:
        modify.trim_entity(base, (80, 0, 0), [LINE((200, 5), (200, 9), "L3")])                                # 没有任何边界与之相交
    assert ex.value.code == "NO_INTERSECTION"
    ext = modify.extend_entity(LINE((0, 0), (40, 0), "L1"), (40, 0, 0), [LINE((100, -5), (100, 5), "L2")])
    assert pclose(ext["p2"], (100, 0))                                                                    # extend_entity 返回新 data
    pieces = modify.break_entity(base, (30, 0, 0), None)                                                      # 一点处打断：两段
    assert len([o for o in pieces if o["op"] in ("replace", "create")]) == 2


# ------------------------------------------------------------------ 相切圆 / 边界
def test_circle_ttr_and_no_solution(env):
    did = env.new("blank_a3")
    env.call("draw_linear_primitives", did, primitive_type="line", points=["0,0", "100,0"])
    env.call("draw_linear_primitives", did, primitive_type="line", points=["0,0", "0,100"])
    r = env.call("draw_circular_primitives", did, curve_type="circle", method="ttr", tangent_entities=["L1", "L2"], radius=10)
    e = env.ent(did, r["created"][0])
    assert close(e.data["r"], 10) and abs(abs(e.data["c"][0]) - 10) < 1e-6 and abs(abs(e.data["c"][1]) - 10) < 1e-6
    env.call("draw_circular_primitives", did, curve_type="circle", method="center_radius", center="500,500", radius=5)
    e2 = env.err("draw_circular_primitives", did, curve_type="circle", method="ttr", tangent_entities=["L1", "C4"], radius=1)   # 半径 1 的圆不可能同时与 L1 和远处的小圆相切
    assert e2["error_code"] in ("NO_SOLUTION", "TTR_NO_SOLUTION", "NO_TANGENT_CIRCLE")


def test_closed_boundary_with_island_and_gap():
    outer = RECT(0, 0, 100, 60, "PL1")
    hole = CIRCLE((50, 30), 10, "C2")
    r = boundary.find_closed_boundary([outer, hole], candidate_ids=["PL1", "C2"])
    assert r["is_closed"] and len(r["island_loops"]) == 1 and abs(r["enclosed_area"] - (6000 - math.pi * 100)) < 10      # 圆按弦高公差折线化，面积略小
    open_pl = E("polyline", {"vertices": [{"p": [0, 0, 0], "bulge": 0, "sw": 0, "ew": 0}, {"p": [10, 0, 0], "bulge": 0, "sw": 0, "ew": 0}, {"p": [10, 10, 0], "bulge": 0, "sw": 0, "ew": 0},
                                          {"p": [0.5, 10, 0], "bulge": 0, "sw": 0, "ew": 0}, {"p": [0.5, 0.4, 0], "bulge": 0, "sw": 0, "ew": 0}], "closed": False, "width": 0}, "P9")
    g = boundary.find_closed_boundary([open_pl], candidate_ids=["P9"], tolerance=0.01)
    assert not g["is_closed"] and g["gaps"] and g["gaps"][0]["size"] < 1.0                                    # 不闭合：给出缺口位置和大小


# ------------------------------------------------------------------ 约束求解
def _constraint_fixture(env):
    did = env.new("blank_a3")
    env.call("draw_linear_primitives", did, primitive_type="line", points=["0,0", "100,3"])
    env.call("draw_linear_primitives", did, primitive_type="line", points=["100,3", "98,60"])
    return did


def test_constraints_solve_and_dimension_expression(env):
    did = _constraint_fixture(env)
    env.call("apply_parametric_constraints", did, constraint_category="geometric", geom_type="horizontal", entity_ids=["L1"])
    env.call("apply_parametric_constraints", did, constraint_category="geometric", geom_type="perpendicular", entity_ids=["L1", "L2"])
    env.call("apply_parametric_constraints", did, constraint_category="geometric", geom_type="coincident", entity_ids=["L1", "L2"])
    env.call("apply_parametric_constraints", did, constraint_category="dimensional", dim_type="distance", entity_ids=["L1"], dim_param_name="d1", dim_expression=120)
    env.call("apply_parametric_constraints", did, constraint_category="dimensional", dim_type="distance", entity_ids=["L2"], dim_param_name="d2", dim_expression="d2 = d1 * 0.5")
    d = env.d(did)
    l1, l2 = d.get_entity("L1").data, d.get_entity("L2").data
    assert abs(l1["p1"][1] - l1["p2"][1]) < 1e-6 and abs(math.hypot(l1["p2"][0] - l1["p1"][0], 0) - 120) < 1e-5     # 水平、长 120
    assert abs(math.hypot(l2["p2"][0] - l2["p1"][0], l2["p2"][1] - l2["p1"][1]) - 60) < 1e-5 and abs(l2["p2"][0] - l2["p1"][0]) < 1e-5   # 竖直、长 60
    env.call("apply_parametric_constraints", did, constraint_category="dimensional", dim_param_name="d1", dim_expression=200)              # 改参数：d2 跟着变
    l2 = env.d(did).get_entity("L2").data
    assert abs(math.hypot(l2["p2"][0] - l2["p1"][0], l2["p2"][1] - l2["p1"][1]) - 100) < 1e-5
    lst = env.call("apply_parametric_constraints", did, constraint_category="list")["data"]
    assert lst["params"] == {"d1": 200.0, "d2": 100.0} and all(c["satisfied"] for c in lst["constraints"])


def test_over_constrained_unsolvable_and_unsafe_expression(env):
    did = _constraint_fixture(env)
    env.call("apply_parametric_constraints", did, constraint_category="dimensional", dim_type="distance", entity_ids=["L1"], dim_param_name="d1", dim_expression=50)
    r = env.err("apply_parametric_constraints", did, constraint_category="dimensional", dim_type="distance", entity_ids=["L1"], dim_param_name="d3", dim_expression=80)
    assert r["error_code"] == "OVER_CONSTRAINED" and r["suspects"]                                            # 同一条线又要 50 又要 80
    n = len(env.d(did).constraints)
    assert n == 1                                                                                              # 失败的约束没有留下
    bad = env.err("apply_parametric_constraints", did, constraint_category="dimensional", dim_type="distance", entity_ids=["L2"], dim_param_name="d9", dim_expression="__import__('os').system('x')")
    assert bad["error_code"] in ("BAD_EXPRESSION", "PARAM_NOT_FOUND")                                          # 禁止 eval：不是四则运算就拒绝
    assert C.eval_expr("(2 + 3) * d1 / 4 - 1", {"d1": 8}) == 9.0
    with pytest.raises(GeomError):
        C.eval_expr("d1 +", {"d1": 1})
    with pytest.raises(GeomError):
        C.eval_expr("d1 / 0", {"d1": 1})


def test_unsolvable_circle_tangent(env):
    did = env.new("blank_a3")
    env.call("draw_linear_primitives", did, primitive_type="line", points=["0,0", "100,0"])
    env.call("draw_circular_primitives", did, curve_type="circle", method="center_radius", center="50,30", radius=5)
    env.call("apply_parametric_constraints", did, constraint_category="geometric", geom_type="fix", entity_ids=["L1"])
    env.call("apply_parametric_constraints", did, constraint_category="geometric", geom_type="tangent", entity_ids=["L1", "C2"])
    c = env.ent(did, "C2").data
    assert abs(abs(c["c"][1]) - c["r"]) < 1e-5                                                                 # 圆心到直线的距离 = 半径
    r = env.err("apply_parametric_constraints", did, constraint_category="dimensional", dim_type="radius", entity_ids=["C2"], dim_param_name="r1", dim_expression=-3)
    assert r["error_code"] == "INVALID_ARGS"


# ------------------------------------------------------------------ 三维
def test_extrude_with_island_volume_and_taper_and_boolean():
    plate = solid.section_from_entities([RECT(0, 0, 100, 60, "PL1"), CIRCLE((50, 30), 10, "C2")], include_islands=True)
    s = solid.extrude(plate, 20)
    assert abs(s["volume"] - 20 * (6000 - 72 / 2 * 100 * math.sin(2 * math.pi / 72))) < 1e-3 and s["solid_kind"] == "prism"
    assert abs(solid.mesh_volume(s["vertices"], s["faces"]) - s["volume"]) < 1e-3                              # 网格体积（散度定理）和解析体积一致
    solid_no_hole = solid.section_from_entities([RECT(0, 0, 100, 60, "PL1"), CIRCLE((50, 30), 10, "C2")], include_islands=False)
    assert abs(solid.extrude(solid_no_hole, 20)["volume"] - 6000 * 20) < 1e-6                                  # 关掉孤岛：实心板
    t = solid.extrude(solid.section_from_entities([RECT(0, 0, 100, 100)]), 50, taper_deg=5)
    assert 50 * 100 * 100 > t["volume"] > 50 * 80 * 80
    with pytest.raises(GeomError) as ex:
        solid.extrude(solid.section_from_entities([RECT(0, 0, 10, 10)]), 50, taper_deg=45)                      # 锥度太大：顶面退化
    assert ex.value.code in ("TAPER_TOO_LARGE", "TAPER_UNSUPPORTED")
    a = solid.extrude(solid.section_from_entities([RECT(0, 0, 100, 100)]), 30)
    b = solid.extrude(solid.section_from_entities([CIRCLE((50, 50), 20)]), 10, base_z=10)
    cut = solid.boolean(a, b, "subtract")
    assert abs(cut["volume"] - (100 * 100 * 30 - solid.slabs_volume(b["slabs"]))) < 1e-6
    both = solid.boolean(a, b, "union")
    assert abs(both["volume"] - a["volume"]) < 1e-6                                                            # b 完全在 a 里面
    inter = solid.boolean(a, b, "intersect")
    assert abs(inter["volume"] - b["volume"]) < 1e-6


def test_revolve_volume_and_errors(monkeypatch):
    rect = solid.section_from_entities([RECT(10, 0, 20, 40)])
    r = solid.revolve(rect, (0, 0, 0), (0, 1, 0), 360, 96)                                                     # 绕 Y 轴：圆环柱 π(20²−10²)·40
    exact = math.pi * (400 - 100) * 40
    assert abs(r["volume"] - exact) / exact < 0.01
    with pytest.raises(GeomError) as ex:
        solid.revolve(solid.section_from_entities([RECT(-5, 0, 5, 10)]), (0, 0, 0), (0, 1, 0))                 # 轮廓跨过旋转轴
    assert ex.value.code == "PROFILE_CROSSES_AXIS"
    with monkeypatch.context() as mp:
        mp.setattr(solid, "manifold_available", lambda: False)                                                  # 没装可选依赖 manifold3d：旋转体不是 z 向棱柱，不能布尔
        with pytest.raises(GeomError) as ex:
            solid.boolean(r, solid.extrude(solid.section_from_entities([RECT(0, 0, 5, 5)]), 5), "union")
        assert ex.value.code == "BOOLEAN_UNSUPPORTED"


def test_open_profile_is_an_error():
    with pytest.raises(GeomError) as ex:
        solid.section_from_entities([LINE((0, 0), (10, 0)), LINE((10, 0), (10, 10))])
    assert ex.value.code in ("BOUNDARY_NOT_CLOSED", "NO_PROFILE")


# ------------------------------------------------------------------ ISO 286 与机械标准件
@pytest.mark.parametrize("size,code,up,lo", [
    (30, "H7", 21, 0), (30, "g6", -7, -20), (50, "H7", 25, 0), (50, "f7", -25, -50), (20, "m6", 21, 8), (40, "k6", 18, 2),
    (40, "K7", 7, -18), (40, "N7", -8, -33), (40, "P7", -17, -42), (40, "F7", 50, 25), (30, "JS7", 10, -10), (100, "H8", 54, 0),
    (10, "h6", 0, -9), (10, "p6", 24, 15), (40, "n6", 33, 17), (40, "m6", 25, 9), (80, "H9", 74, 0), (120, "H11", 220, 0), (40, "G7", 34, 9), (40, "M7", 0, -25),
    (500, "H7", 63, 0), (3, "H7", 10, 0),
])
def test_iso286_table(size, code, up, lo):
    d = mech.deviation(size, code)
    assert d["upper_um"] == pytest.approx(up) and d["lower_um"] == pytest.approx(lo)


def test_iso286_pairs_and_unknown_is_not_invented():
    pair = mech.fit(30, "H7/g6")
    assert [p["kind"] for p in pair] == ["hole", "shaft"] and pair[0]["upper_mm"] == 0.021 and pair[1]["lower_mm"] == -0.02
    for size, code in ((600, "H7"), (30, "H12"), (30, "zc6"), (30, "R7")):
        with pytest.raises(GeomError) as ex:
            mech.deviation(size, code)
        assert ex.value.code == "DEVIATION_UNKNOWN" and "用户" in ex.value.hint                                   # 查不到：让上层反问用户
    with pytest.raises(GeomError):
        mech.deviation(30, "7H")


def test_standard_parts_and_keyways_from_tables():
    std = mech.mech_std()
    assert std["hex_bolt"]["M8"] == {"s": 13.0, "k": 5.3} and std["hex_nut"]["M10"]["m"] == 8.4 and std["washer"]["M12"]["d2"] == 24.0
    assert mech.keyway(30)["b"] == 8 and mech.keyway(31)["b"] == 10 and mech.keyway(110)["t1"] == 10.0
    with pytest.raises(GeomError):
        mech.keyway(120)
    ok, series = mech.nice_roughness("Ra", 3.2)
    assert ok and not mech.nice_roughness("Ra", 3.0)[0]
