"""坐标表达式（规格 4.4）：全部写法、解析结果的 source、与正交/极轴设置的交互、特征点和函数式。"""
import math

import pytest

from gen_helpers import Env, pclose
from zhihui.cad.geom.coords import CoordContext, parse_point
from zhihui.cad.geom.errors import GeomError


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


@pytest.fixture
def ctx(env):
    did = env.new("blank_a3")
    env.call("draw_linear_primitives", did, primitive_type="line", points=["0,0", "100,0"])           # L1
    env.call("draw_linear_primitives", did, primitive_type="line", points=["50,-20", "50,60"])        # L2 竖线，与 L1 交于 (50,0)
    env.call("draw_circular_primitives", did, curve_type="circle", method="center_radius", center="200,100", radius=30)   # C3
    env.call("draw_polygons_and_meshes", did, shape_type="rectangle", corner1="300,0", corner2="@40,20")                  # PL4
    d = env.d(did)
    d.session.last_point = (10.0, 20.0, 0.0)
    return CoordContext(d)


def pt(ctx, s):
    r = parse_point(s, ctx)
    return r["point"], r["source"], r["warnings"]


@pytest.mark.parametrize("expr,want,source", [
    ("100,50", [100, 50, 0], "absolute_xy"),
    ("100,50,10", [100, 50, 10], "absolute_xy"),
    ("-5.5,7", [-5.5, 7, 0], "absolute_xy"),
    ("@30,0", [40, 20, 0], "relative_xy"),
    ("@30,0,5", [40, 20, 5], "relative_xy"),
    ("@-10,-20", [0, 0, 0], "relative_xy"),
    ("80<30", [80 * math.cos(math.radians(30)), 80 * math.sin(math.radians(30)), 0], "absolute_polar"),
    ("@50<45", [10 + 50 * math.cos(math.radians(45)), 20 + 50 * math.sin(math.radians(45)), 0], "relative_polar"),
    ("right 100", [110, 20, 0], "direction"),
    ("向右100", [110, 20, 0], "direction"),
    ("up 30", [10, 50, 0], "direction"),
    ("向下 5", [10, 15, 0], "direction"),
    ("left 10", [0, 20, 0], "direction"),
    ("$LAST_POINT", [10, 20, 0], "last_point"),
])
def test_basic_forms(ctx, expr, want, source):
    p, s, w = pt(ctx, expr)
    assert s == source and pclose(p, want, 1e-6) and abs(p[2] - want[2]) < 1e-9 and w == []


def test_array_and_dict_and_fullwidth(ctx):
    assert pt(ctx, [3, 4])[0] == [3, 4, 0]
    assert pt(ctx, {"point": [5, 6]})[0] == [5, 6, 0]
    assert pt(ctx, "＠30，0")[0][:2] == [40, 20]                         # 全角符号


@pytest.mark.parametrize("expr,want", [
    ("C3.center", [200, 100]), ("L1.start", [0, 0]), ("L1.end", [100, 0]), ("L1.mid", [50, 0]),
    ("PL4.v0", [300, 0]), ("PL4.v2", [340, 20]), ("PL4.edge_mid(1)", [340, 10]), ("C3.quad(90)", [200, 130]), ("C3.quad(0)", [230, 100]),
    ("PL4.center", [320, 10]),
])
def test_entity_features(ctx, expr, want):
    assert pclose(pt(ctx, expr)[0], want, 1e-6)


@pytest.mark.parametrize("expr,want", [
    ("int(L1,L2)", [50, 0]),
    ("m2p(L1.start,L1.end)", [50, 0]),
    ("per(L1)", [10, 0]),                    # 从上一点 (10,20) 向 L1 作垂线
    ("nea(L1,30%)", [30, 0]),
    ("ext(L1,20)", [120, 0]),
])
def test_functions(ctx, expr, want):
    assert pclose(pt(ctx, expr)[0], want, 1e-6)


def test_tangent_from_last_point(ctx):
    d = ctx.d
    d.session.last_point = (200.0, 200.0, 0.0)                          # 圆外一点，到圆 C3 有两个切点
    p = pt(ctx, "tan(C3)")[0]
    assert abs(math.hypot(p[0] - 200, p[1] - 100) - 30) < 1e-6          # 在圆上
    assert abs((p[0] - 200) * (p[0] - 200) + (p[1] - 100) * (p[1] - 200)) < 1e-3   # 半径 ⟂ 切线


def test_ortho_quantizes_direction_and_polar_angles(ctx):
    ctx.d.draft.ortho_on = True
    p, s, w = pt(ctx, "@50<50")
    assert pclose(p, [10, 70]) and any("正交" in x for x in w)           # 50° 在正交下变成最近的 90°
    p, s, w = pt(ctx, "@50<44")
    assert pclose(p, [60, 20])                                          # 44° 离 0° 更近
    ctx.d.draft.ortho_on = False
    ctx.d.draft.polar_on, ctx.d.draft.polar_angle_deg = True, 15
    p, s, w = pt(ctx, "@10<31")
    assert abs(math.degrees(math.atan2(p[1] - 20, p[0] - 10)) - 30) < 1e-6 and any("极轴" in x for x in w)
    p, s, w = pt(ctx, "@10<30")
    assert w == []                                                      # 本来就是 15° 的整数倍：不提示


@pytest.mark.parametrize("expr,code", [
    ("abc", "INVALID_POINT"), ("1,2,3,4", "INVALID_POINT"), ([1], "INVALID_POINT"), ("C99.center", "ENTITY_NOT_FOUND"), ("L1.nope", "INVALID_ARGS"),
])
def test_bad_expressions(ctx, expr, code):
    with pytest.raises(GeomError) as ex:
        parse_point(expr, ctx)
    assert ex.value.code in (code, "INVALID_POINT", "INVALID_ARGS", "ENTITY_NOT_FOUND")


def test_relative_without_last_point_is_an_error(env):
    did = env.new("blank_a3")
    r = env.err("draw_linear_primitives", did, primitive_type="line", points=["@10,0", "@20,0"])
    assert r["error_code"] == "NO_LAST_POINT" and "绝对坐标" in r["hint"]


def test_parse_coordinate_input_tool_updates_last_point(env):
    did = env.new("blank_a3")
    r = env.call("parse_coordinate_input", did, coord_expressions=["10,10", "@5,5", "right 10"])
    pts = [x["point"] for x in r["data"]["points"]]
    assert pclose(pts[2], [25, 15])
    assert env.d(did).session.last_point[:2] == (25.0, 15.0) or list(env.d(did).session.last_point[:2]) == [25.0, 15.0]
