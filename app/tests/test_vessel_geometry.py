"""化工设备图的几何（任务书 10.1）：封头与筒体在切线处连续、壁厚夸大只改图不改参数、尺寸取参数值、法兰面位置（4.4）、
管口方位（0° 朝上顺时针）、尺寸链闭合、件号与明细栏一一对应。数值精度 1e-6。"""
import copy
import math

import pytest

from gen_helpers import Env, close, pclose
from zhihui.agent.general.prompts import EQUIP_FEW_SHOTS
from zhihui.cad.equipment import check as CHK
from zhihui.cad.equipment import profile as PF
from zhihui.cad.equipment import views as VW
from zhihui.cad.geom import entities as GE

TOL = 1e-6


def shot(i: int) -> dict:
    return copy.deepcopy(EQUIP_FEW_SHOTS[i][1]["workflow_steps"][0]["payload"]["args"]["spec"])


def build(tmp_path, spec: dict, **kw):
    env = Env(tmp_path)
    did = env.new("hg_equipment_a1")
    env.call("vessel_define", did, op="create", spec=spec, **kw)
    return env, did, env.d(did)


def auto(d, view=None, part=None, space="Model", kind=None):
    out = []
    for e in d.entities:
        if "equip:auto" not in e.tags or (space and e.space != space):
            continue
        if view and f"equip_view:{view}" not in e.tags:
            continue
        if part and f"equip_part:{part}" not in e.tags:
            continue
        if kind and e.kind != kind:
            continue
        out.append(e)
    return out


def ends(e):
    """线、弧、椭圆弧的两个端点。"""
    prims = GE.primitives(e)
    if not prims or e.kind not in ("line", "arc", "ellipse"):
        return []
    a, b = prims[0], prims[-1]
    p = a.a if isinstance(a, GE.Seg) else a.start()
    q = b.b if isinstance(b, GE.Seg) else b.end()
    return [(p[0], p[1]), (q[0], q[1])]


def count_at(pts, target) -> int:
    return sum(1 for p in pts if pclose(p, target, TOL))


def outline_ends(d, parts_):
    pts = []
    for e in auto(d, "M"):
        part = next(t.split(":", 1)[1] for t in e.tags if t.startswith("equip_part:"))
        if part in parts_ and e.layer == "粗实线" and "seam" not in e.tags:
            pts += ends(e)
    return pts


def to_model(vertical: bool, x: float, y: float):
    """局部坐标（x 径向、y 轴向）→ 模型空间；卧式关于 y=x 镜像。"""
    return (x, y) if vertical else (y, x)


VERT = {"kind": "vertical_vessel", "shell": {"inner_diameter_mm": 1600, "length_mm": 2400, "thickness_mm": 8},
        "nozzles": [{"dn": 80, "purpose_text": "进料口"}], "supports": [{"kind": "lug", "count": 4}]}
HORZ = {"kind": "horizontal_vessel", "shell": {"inner_diameter_mm": 2000, "tangent_length_mm": 6000, "thickness_mm": 10},
        "nozzles": [{"dn": 100, "purpose_text": "进料"}, {"dn": 500, "kind": "manhole", "purpose_text": "人孔"}], "supports": [{"kind": "saddle", "count": 2}]}


# ------------------------------------------------------------------ 封头与筒体在切线处连续
@pytest.mark.parametrize("vertical", [True, False])
@pytest.mark.parametrize("kind", ["EHA", "HHA", "THA", "SDH", "FLAT"])
def test_head_and_shell_are_continuous_at_tangent_line(tmp_path, vertical, kind):
    spec = copy.deepcopy(VERT if vertical else HORZ)
    spec["heads"] = [{"kind": kind, **({"thickness_mm": 20} if kind == "FLAT" else {})}]
    env, did, d = build(tmp_path, spec)
    sp = d.vessel
    rec = sp.drawing["record"]
    den = rec["den_main"]
    ri, L = sp.shell.inner_diameter_mm / 2, sp.shell.length_mm
    pts = outline_ends(d, {"SHELL", "HEAD-B", "HEAD-T", "HEAD-L", "HEAD-R"})
    assert pts
    for h in sp.heads:
        pr = PF.head_profile(sp, h)
        tw = VW.drawn_thickness(h.thickness_mm, den)
        draw = PF.HeadProfile(pr.kind, pr.a, tw, pr.hsf, pr.hc, pr.rc, pr.rk)
        up = h.position in ("top", "right")
        sg, y_seam = (1.0, L) if up else (-1.0, 0.0)
        yt = y_seam + sg * pr.hsf
        assert pr.hsf == h.straight_flange_mm
        for s in (1.0, -1.0):
            # 切线处：内表面、外表面各有直边（或筒体）的端点和曲面的端点重合
            assert count_at(pts, to_model(vertical, s * ri, yt)) >= 2, (kind, h.position, "inner tangent")
            # 外表面：封头与筒体同厚时端点重合；厚度不同（平盖 20 对筒体 8）时封头外壁自己的直边和曲面相接，与筒体外壁之间是一个台阶
            same_wall = close(tw, rec["tw"], TOL)
            assert count_at(pts, to_model(vertical, s * (ri + tw), yt)) >= (2 if same_wall else 1), (kind, h.position, "outer tangent")
            # 曲面收尾处（SDH 的外表面在筒体外壁处有一小段竖线，FLAT 在板厚处收口）两个图元也重合
            assert count_at(pts, to_model(vertical, s * ri, yt + sg * draw.inner_y(ri))) >= 2
            assert count_at(pts, to_model(vertical, s * (ri + tw), yt + sg * draw.outer_y(ri + tw))) >= 2
        # 封头总高 = 直边 + 内曲面深度 + 厚度（环焊缝到外表面顶点，用真实厚度）
        real = PF.HeadProfile(pr.kind, pr.a, float(h.thickness_mm), pr.hsf, pr.hc, pr.rc, pr.rk)
        assert close(real.total_height, pr.hsf + pr.hc + float(h.thickness_mm), TOL)
        assert close(real.outer_y(0.0), pr.hc + float(h.thickness_mm), TOL)


@pytest.mark.parametrize("kind", ["EHA", "HHA", "THA", "SDH", "FLAT"])
def test_head_profile_inner_surface_meets_the_cylinder(kind):
    """纯数学：内表面在半径 Ri 处高度为 0（与筒体内壁相接），顶点高度 = 内曲面深度。"""
    di, t = 1600.0, 8.0
    p = PF.profile(kind, di, t, 25.0)
    assert close(p.inner_y(di / 2), 0.0, TOL)
    assert close(p.inner_y(0.0), p.hc, TOL)
    if kind != "SDH":
        assert close(p.outer_y(di / 2 + t), 0.0 if kind != "FLAT" else t, TOL)
    # 单调：离轴线越远越低
    ys = [p.inner_y(x) for x in (0.0, 100.0, 300.0, 600.0, 800.0)]
    assert all(a >= b - TOL for a, b in zip(ys, ys[1:]))


def test_eha_inner_depth_is_quarter_of_diameter():
    for di in (600.0, 1600.0, 3000.0):
        assert close(PF.profile("EHA", di, 8.0, 25.0).hc, di / 4, TOL)


# ------------------------------------------------------------------ 壁厚夸大：只改图，不改参数
@pytest.mark.parametrize("scale,delta,vertical", [("1:5", 8, True), ("1:10", 8, True), ("1:20", 8, True), ("1:10", 12, True), ("1:20", 10, False), ("1:10", 6, False)])
def test_drawn_wall_is_max_of_delta_and_scale_denominator(tmp_path, scale, delta, vertical):
    spec = copy.deepcopy(VERT if vertical else HORZ)
    spec["shell"]["thickness_mm"] = delta
    if vertical:
        spec["shell"].update({"inner_diameter_mm": 1000, "length_mm": 1500})
    else:
        spec["shell"].update({"inner_diameter_mm": 1000, "tangent_length_mm": 3000})
    env, did, d = build(tmp_path, spec, scale=scale)
    sp = d.vessel
    rec = sp.drawing["record"]
    den = float(scale.split(":")[1])
    assert close(rec["den_main"], den)
    want = max(float(delta), den * 1.0)
    assert close(rec["tw"], want, TOL)
    ri = sp.shell.inner_diameter_mm / 2
    # 筒体壁：内壁线在 ri，外壁线在 ri + 图上壁厚；参数里的 δ 不变
    xs = set()
    for e in auto(d, "M", "SHELL", kind="line"):
        p, q = e.data["p1"], e.data["p2"]
        a, b = (p[0], q[0]) if vertical else (p[1], q[1])
        c0, c1 = (p[1], q[1]) if vertical else (p[0], q[0])
        if close(a, b, TOL) and not close(c0, c1, TOL) and "seam" not in e.tags:
            xs.add(round(abs(a), 6))
    assert xs == {round(ri, 6), round(ri + want, 6)}
    assert sp.shell.thickness_mm == float(delta)
    # δ 的文字写参数值，不写图上的夸大值
    labels = [e for e in d.entities if "thickness_label" in e.tags]
    assert len(labels) == 1 and labels[0].data["text"] == f"δ={delta}"
    # 技术要求：夸大了才写“壁厚夸大画出”
    text = "".join(rec["tech_requirements"])
    assert ("壁厚夸大画出" in text) == (want > delta + 1e-9)
    # 涂黑的剖面宽度 = 图上壁厚
    ws = []
    for e in auto(d, "M", "SHELL", kind="hatch"):
        pts = e.data["loops"][0]
        ws.append(round(max(abs(p[0 if vertical else 1]) for p in pts) - min(abs(p[0 if vertical else 1]) for p in pts), 6))
    assert ws and all(close(w, want, TOL) for w in ws)


def test_end_view_shell_circles_use_the_same_drawn_wall(tmp_path):
    env, did, d = build(tmp_path, shot(0))
    rec = d.vessel.drawing["record"]
    rs = sorted(e.data["r"] for e in auto(d, "O", "SHELL", kind="circle"))
    assert rs == [800.0, 800.0 + max(8.0, rec["den_end"])]


# ------------------------------------------------------------------ 尺寸：取参数值
def dim_ents(d):
    return [e for e in d.entities if e.kind == "dimension" and "equip:auto" in e.tags]


def geo_measure(e) -> float:
    p, q = e.data["p1"], e.data["p2"]
    return abs(q[1] - p[1]) if e.data["dtype"] == "linear_vertical" else abs(q[0] - p[0])


@pytest.mark.parametrize("idx", [0, 5])
def test_every_dimension_measurement_equals_its_geometry_and_style_matches_scale(tmp_path, idx):
    env, did, d = build(tmp_path, shot(idx))
    rec = d.vessel.drawing["record"]
    dims = dim_ents(d)
    assert len(dims) >= 8
    st = f"EQ_1_{rec['den_main']:g}"
    for e in dims:
        assert close(e.data["measurement"], geo_measure(e), TOL), e.id
        assert e.data["style"] == st
    s = next(x for x in d.dim_styles if x["name"] == st)
    assert close(s["dimscale"], rec["den_main"])


def test_vertical_dimensions_are_parameter_values(tmp_path):
    env, did, d = build(tmp_path, shot(0))
    sp = d.vessel
    rec = sp.drawing["record"]
    by = {e.id: e for e in dim_ents(d)}
    assert close(by["EQ-M-DIM-DI-1"].data["measurement"], 1600.0, TOL)               # Di
    assert by["EQ-M-DIM-DI-1"].data["text"] == "Φ<>"
    heads = {h.position: PF.head_profile(sp, h).total_height for h in sp.heads}
    total = 2400.0 + heads["top"] + heads["bottom"]
    assert close(by["EQ-M-DIM-TOTAL-1"].data["measurement"], total, TOL)             # 总高
    chain = [by[i].data["measurement"] for i in rec["dim"]["chain_ids"]]
    assert close(chain[0], heads["bottom"], TOL) and close(chain[-1], heads["top"], TOL)   # 封头总高
    assert close(sum(chain[1:-1]), 2400.0, TOL)                                      # 筒体长度（两条环焊缝之间，被管口分段）
    assert close(heads["top"], 25.0 + 400.0 + 8.0, TOL)
    # 每个管口的伸出长度、封头管口的径向位置也是参数值
    for n in sp.nozzles:
        ds = [e for i, e in by.items() if i.startswith(f"EQ-M-DIM-NZ-{n.mark}-")]
        assert ds, n.mark
        assert close(ds[0].data["measurement"], n.projection_mm, TOL)
        if n.location != "shell" and n.radial_mm:
            assert close(ds[1].data["measurement"], n.radial_mm, TOL)


def test_horizontal_dimensions_are_parameter_values(tmp_path):
    env, did, d = build(tmp_path, shot(5))
    sp = d.vessel
    rec = sp.drawing["record"]
    by = {e.id: e for e in dim_ents(d)}
    assert by["EQ-M-DIM-DI-1"].data["dtype"] == "linear_vertical"
    assert close(by["EQ-M-DIM-DI-1"].data["measurement"], 2000.0, TOL)
    L = sp.shell.length_mm
    heads = {h.position: PF.head_profile(sp, h).total_height for h in sp.heads}
    assert close(by["EQ-M-DIM-TOTAL-1"].data["measurement"], L + heads["left"] + heads["right"], TOL)
    chain = [by[i].data["measurement"] for i in rec["dim"]["chain_ids"]]
    assert close(chain[0], heads["left"], TOL) and close(chain[-1], heads["right"], TOL)
    assert close(sum(chain[1:-1]), L, TOL)
    sads = [e for i, e in by.items() if i.startswith("EQ-M-DIM-SADDLE")]
    assert len(sads) == 3 and close(sum(e.data["measurement"] for e in sads), L, TOL)


def test_tangent_length_is_converted_to_shell_length_by_the_straight_flange_and_depth(tmp_path):
    """卧式切线长 6000 → 筒体长度 = 切线长 - 两端（直边 + 椭圆曲面深度/…）由 R-LEN-01 算；图上长度 = 参数。"""
    env, did, d = build(tmp_path, shot(5))
    sp = d.vessel
    assert sp.shell.tangent_length_mm == 6000.0
    # 切线之间的距离 = 筒体长度 + 两端直边（切线 = 封头直边与曲面的分界）
    hsf = sum(h.straight_flange_mm for h in sp.heads)
    assert close(sp.shell.length_mm + hsf, 6000.0, TOL)


# ------------------------------------------------------------------ 法兰面位置（任务书 4.4）
def flange_extreme(d, mark, axis: int, sign: float) -> float:
    """管口法兰外形（tag flange）在某个坐标轴上最靠外的位置（sign=+1 取最大，-1 取最小）。"""
    vals = []
    for e in auto(d, "M", f"NZ-{mark}", kind="polyline"):
        if "flange" in e.tags:
            vals += [v["p"][axis] for v in e.data["vertices"]]
    assert vals, mark
    return max(vals) if sign > 0 else min(vals)


def test_shell_nozzle_flange_face_is_ro_plus_projection_vertical(tmp_path):
    env, did, d = build(tmp_path, shot(0))
    sp = d.vessel
    ro = sp.shell.inner_diameter_mm / 2 + sp.shell.thickness_mm                      # 真实厚度，不用夸大后的
    seen = 0
    for n in sp.nozzles:
        if n.location != "shell":
            continue
        sd = VW.side_of(sp, n)
        face = flange_extreme(d, n.mark, 0, sd)
        assert close(abs(face), ro + n.projection_mm, TOL), n.mark
        assert (face > 0) == (sd > 0)
        # 管口表里的“设备中心线至法兰面距离”也是这个值
        seen += 1
    assert seen == 3


def test_shell_nozzle_flange_face_is_ro_plus_projection_horizontal(tmp_path):
    env, did, d = build(tmp_path, shot(5))
    sp = d.vessel
    ro = sp.shell.inner_diameter_mm / 2 + sp.shell.thickness_mm
    for n in sp.nozzles:
        sd = VW.side_of(sp, n)
        face = flange_extreme(d, n.mark, 1, sd)                                       # 卧式：径向在模型 y 方向
        assert close(abs(face), ro + n.projection_mm, TOL), n.mark


def test_head_nozzle_flange_face_is_projection_from_the_seam(tmp_path):
    env, did, d = build(tmp_path, shot(0))
    sp = d.vessel
    L = sp.shell.length_mm
    top = next(n for n in sp.nozzles if n.location == "top_head")
    bot = next(n for n in sp.nozzles if n.location == "bottom_head")
    assert close(flange_extreme(d, top.mark, 1, 1.0), L + top.projection_mm, TOL)         # 上封头：环焊缝（y=L）到法兰面
    assert close(flange_extreme(d, bot.mark, 1, -1.0), 0.0 - bot.projection_mm, TOL)      # 下封头：基准线（y=0）到法兰面
    # 管口轴线到设备轴线 = radial_mm
    xs = [v["p"][0] for e in auto(d, "M", f"NZ-{top.mark}", kind="polyline") if "flange" in e.tags for v in e.data["vertices"]]
    assert close((max(xs) + min(xs)) / 2, top.radial_mm, TOL)


def test_horizontal_head_nozzle_flange_face_is_projection_from_the_seam(tmp_path):
    spec = shot(5)
    spec["nozzles"].append({"dn": 50, "purpose_text": "放空口", "location": "right_head"})
    spec["nozzles"].append({"dn": 50, "purpose_text": "排净口", "location": "left_head"})
    env, did, d = build(tmp_path, spec)
    sp = d.vessel
    L = sp.shell.length_mm
    for n in sp.nozzles:
        if n.location == "right_head":
            assert close(flange_extreme(d, n.mark, 0, 1.0), L + n.projection_mm, TOL)
        elif n.location == "left_head":
            assert close(flange_extreme(d, n.mark, 0, -1.0), -n.projection_mm, TOL)
    assert CHK.run(sp, d)["errors"] == []


def test_nozzle_table_distance_column_follows_the_definition(tmp_path):
    env, did, d = build(tmp_path, shot(0))
    sp = d.vessel
    ro = 800.0 + 8.0
    cells = {}
    for e in d.entities:
        if e.space == "设备图" and "table_cell" in e.tags and "equip:auto" in e.tags:
            cells.setdefault(e.id, e.data["text"])
    texts = list(cells.values())
    for n in sp.nozzles:
        want = ro + n.projection_mm if n.location == "shell" else n.projection_mm
        assert f"{want:g}" in texts, (n.mark, want)


# ------------------------------------------------------------------ 管口方位：0° 朝上，顺时针为正
ORIS = [0.0, 45.0, 90.0, 135.0, 180.0, 225.0, 270.0, 315.0]


def spread(kind: str) -> dict:
    marks = "abcdefgh"
    if kind == "vertical_vessel":
        return {"kind": kind, "shell": {"inner_diameter_mm": 2000, "length_mm": 3600, "thickness_mm": 8},
                "nozzles": [{"mark": m, "dn": 50, "purpose_text": "进料口", "location": "shell", "axial_mm": 500 + 300 * i, "orientation_deg": o} for i, (m, o) in enumerate(zip(marks, ORIS))],
                "supports": [{"kind": "lug", "count": 4}]}
    return {"kind": kind, "shell": {"inner_diameter_mm": 2000, "length_mm": 5000, "thickness_mm": 10},
            "nozzles": [{"mark": m, "dn": 50, "purpose_text": "进料口", "location": "shell", "axial_mm": 600 + 500 * i, "orientation_deg": o} for i, (m, o) in enumerate(zip(marks, ORIS))],
            "supports": [{"kind": "saddle", "count": 2}]}


@pytest.mark.parametrize("kind,view", [("vertical_vessel", "O"), ("horizontal_vessel", "L")])
def test_end_view_radial_lines_follow_orientation_clockwise_from_up(tmp_path, kind, view):
    env, did, d = build(tmp_path, spread(kind))
    sp = d.vessel
    rec = sp.drawing["record"]
    circles = auto(d, view, "SHELL", kind="circle")
    cx, cy = circles[0].data["c"][0], circles[0].data["c"][1]
    ro = 1000.0 + sp.shell.thickness_mm
    for n in sp.nozzles:
        ln = next(e for e in auto(d, view, f"NZ-{n.mark}", kind="line") if "neck" in e.tags)
        p0, p1 = ln.data["p1"], ln.data["p2"]
        dx, dy = p1[0] - cx, p1[1] - cy
        ang = math.degrees(math.atan2(dx, dy)) % 360.0                              # 从正上方顺时针量
        assert close(math.sin(math.radians(ang - n.orientation_deg)), 0.0, TOL) and close(math.cos(math.radians(ang - n.orientation_deg)), 1.0, TOL), (n.mark, ang)
        assert close(math.hypot(dx, dy), ro + n.projection_mm, TOL)                     # 端点 = 法兰面
        d0 = math.hypot(p0[0] - cx, p0[1] - cy)
        assert close(d0, 1000.0 + max(float(sp.shell.thickness_mm), rec["den_end"]), TOL)   # 起点在图上外壁
    # 0° 的那条线朝上（y 增大），90° 朝右（x 增大）
    up = next(e for e in auto(d, view, "NZ-a", kind="line") if "neck" in e.tags)
    assert up.data["p2"][1] > cy and close(up.data["p2"][0], cx, TOL)
    right = next(e for e in auto(d, view, "NZ-c", kind="line") if "neck" in e.tags)
    assert right.data["p2"][0] > cx and close(right.data["p2"][1], cy, TOL)
    # 角度刻度文字
    angles = sorted(e.data["text"] for e in auto(d, view, "ANGLE", kind="text"))
    assert angles == ["0°", "180°", "270°", "90°"]
    # 标签文字写参数里的角度
    for n in sp.nozzles:
        txts = [e.data["text"] for e in auto(d, view, f"NZ-{n.mark}", kind="text")]
        assert any(f"{n.orientation_deg:g}°" in t and n.mark in t for t in txts), (n.mark, txts)


@pytest.mark.parametrize("kind", ["vertical_vessel", "horizontal_vessel"])
def test_rotated_view_puts_0_to_180_on_the_correct_side(tmp_path, kind):
    """立式：0°~180° 转到右侧；卧式：270°~90° 转到上侧（从左封头端看的 0° 在正上方）。"""
    env, did, d = build(tmp_path, spread(kind))
    sp = d.vessel
    ro = 1000.0 + sp.shell.thickness_mm
    for n in sp.nozzles:
        th = n.orientation_deg % 360
        face = flange_extreme(d, n.mark, 0 if kind == "vertical_vessel" else 1, VW.side_of(sp, n))
        if kind == "vertical_vessel":
            assert (face > 0) == (th < 180.0), (n.mark, th)
        else:
            assert (face > 0) == (th >= 270.0 or th < 90.0), (n.mark, th)
        assert close(abs(face), ro + n.projection_mm, TOL)


def test_head_nozzle_position_in_orientation_view_is_polar_from_axis(tmp_path):
    spec = shot(0)
    spec["nozzles"][1]["orientation_deg"] = 30           # 进料口在上封头，用户给了方位
    env, did, d = build(tmp_path, spec)
    sp = d.vessel
    c = auto(d, "O", "SHELL", kind="circle")[0].data["c"]
    n = next(x for x in sp.nozzles if x.location == "top_head")
    assert n.orientation_deg == 30.0 and n.radial_mm > 0
    ring = [e for e in auto(d, "O", f"NZ-{n.mark}", kind="circle") if "flange" in e.tags][0]
    px, py = ring.data["c"][0] - c[0], ring.data["c"][1] - c[1]
    assert close(math.hypot(px, py), n.radial_mm, TOL)
    assert close(math.degrees(math.atan2(px, py)) % 360.0, 30.0, 1e-5)
    # 主视图：30° 在右侧，径向位置 = radial_mm
    ds = [e for e in dim_ents(d) if e.id.startswith(f"EQ-M-DIM-NZ-{n.mark}-")]
    assert close(ds[1].data["measurement"], n.radial_mm, TOL)


# ------------------------------------------------------------------ 尺寸链
@pytest.mark.parametrize("idx", [0, 5])
def test_dimension_chain_closes_to_parameter_total(tmp_path, idx):
    env, did, d = build(tmp_path, shot(idx))
    sp = d.vessel
    dm = sp.drawing["record"]["dim"]
    expect = sp.shell.length_mm + sum(PF.head_profile(sp, h).total_height for h in sp.heads)
    assert close(sum(dm["chain"]), dm["overall"], TOL)
    assert close(dm["overall"], expect, TOL)
    assert close(dm["expected"], expect, TOL)
    assert CHK.run(sp, d)["errors"] == []


@pytest.mark.parametrize("kind", ["EHA", "HHA", "THA", "SDH", "FLAT"])
def test_dimension_chain_closes_for_every_head_kind(tmp_path, kind):
    spec = copy.deepcopy(VERT)
    spec["heads"] = [{"kind": kind, **({"thickness_mm": 20} if kind == "FLAT" else {})}]
    env, did, d = build(tmp_path, spec)
    sp = d.vessel
    dm = sp.drawing["record"]["dim"]
    expect = 2400.0 + sum(PF.head_profile(sp, h).total_height for h in sp.heads)
    assert close(sum(dm["chain"]), expect, TOL)
    assert close(dm["overall"], expect, TOL)


def test_check_reports_a_broken_chain(tmp_path):
    env, did, d = build(tmp_path, shot(0))
    sp = d.vessel.model_copy(deep=True)
    sp.drawing["record"]["dim"]["chain"][1] += 5.0
    assert any(e["code"] == "E_DIM_CHAIN" for e in CHK.run(sp, d)["errors"])


# ------------------------------------------------------------------ 件号与明细栏
def segs_cross(a, b, c, e) -> bool:
    def o(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
    d1, d2, d3, d4 = o(a, b, c), o(a, b, e), o(c, e, a), o(c, e, b)
    return d1 * d2 < -1e-9 and d3 * d4 < -1e-9


@pytest.mark.parametrize("idx", [0, 5])
def test_balloon_numbers_match_the_parts_list_one_to_one(tmp_path, idx):
    env, did, d = build(tmp_path, shot(idx))
    rec = d.vessel.drawing["record"]
    drawn = sorted(int(t.split(":")[1]) for e in d.entities if e.space == "Model" for t in e.tags if t.startswith("balloon:"))
    nos = [p["no"] for p in rec["parts"]]
    assert drawn == sorted(nos) == list(range(1, len(nos) + 1))
    parts_tbl = next(t for t in rec["tables"] if t["name"] == "明细栏")
    assert sorted(int(x) for x in parts_tbl["first_col"]) == drawn
    assert len(set(p["name"] for p in rec["parts"])) == len(rec["parts"])               # 同规格合并，不重复列
    # 明细栏里的名称和数量能在图纸空间文字里找到
    cell_text = "".join(e.data["text"] for e in d.entities if e.space == "设备图" and "table_cell" in e.tags)
    for p in rec["parts"]:
        assert p["name"].replace(" ", "")[:6] in cell_text.replace(" ", "")


@pytest.mark.parametrize("idx", [0, 5])
def test_balloons_are_in_order_and_leaders_do_not_cross(tmp_path, idx):
    env, did, d = build(tmp_path, shot(idx))
    sp = d.vessel
    pos, leaders = {}, {}
    for e in auto(d, "M", kind="text"):
        t = next((t for t in e.tags if t.startswith("balloon:")), None)
        if t:
            pos[int(t.split(":")[1])] = e.data["p"]
    for e in auto(d, "M", kind="line"):
        part = next(t for t in e.tags if t.startswith("equip_part:"))
        if part.startswith("equip_part:BAL-") and "leader" in e.tags:
            leaders[int(part.split("-")[1])] = (e.data["p1"], e.data["p2"])
    assert sorted(pos) == sorted(leaders) == list(range(1, len(pos) + 1))
    key = 1 if sp.vertical else 0
    seq = [pos[i][key] for i in sorted(pos)]
    assert all(a > b for a, b in zip(seq, seq[1:])) if sp.vertical else all(a < b for a, b in zip(seq, seq[1:]))
    ids = sorted(leaders)
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            assert not segs_cross(leaders[a][0], leaders[a][1], leaders[b][0], leaders[b][1]), (a, b)


def test_check_reports_balloon_without_table_row(tmp_path):
    env, did, d = build(tmp_path, shot(0))
    d.entities = [e for e in d.entities if "balloon:3" not in e.tags]
    assert any(e["code"] == "E_TABLE_MISMATCH" and "件号" in e["message"] for e in CHK.run(d.vessel, d)["errors"])


def test_every_nozzle_symbol_is_in_main_view_end_view_and_table(tmp_path):
    env, did, d = build(tmp_path, shot(0))
    sp = d.vessel
    rec = sp.drawing["record"]
    marks = sorted(n.mark for n in sp.nozzles)
    assert marks == ["M1", "a", "b1", "b2", "c"]
    assert sorted(rec["nozzle_rows"]) == sorted(rec["main_marks"]) == sorted(rec["end_marks"]) == marks
    for view in ("M", "O"):
        got = set()
        for e in auto(d, view, kind="text"):
            got |= {t.split(":", 1)[1] for t in e.tags if t.startswith("equip_mark:")}
        assert got == set(marks), view
