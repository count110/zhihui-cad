"""填充（HATCH）：边界求解、孤岛方式、缺口、文字避让、精确边界、图案比例。"""

import math

import ezdxf
import pytest
from ezdxf.entities.boundary_paths import BoundaryPathType, EdgeType

from zhihui.cad.hatch import HatchError, boundary_from_entities, check_pattern, find_boundary, paper_scale, write_hatch
from zhihui.ir import Drawing


def doc_msp():
    doc = ezdxf.new("R2018")
    return doc, doc.modelspace()


def rect_lines(msp, x0, y0, x1, y1, over=5.0):
    """4 根互相出头的 LINE 拼成矩形。"""
    return [
        msp.add_line((x0 - over, y0), (x1 + over, y0)),
        msp.add_line((x0 - over, y1), (x1 + over, y1)),
        msp.add_line((x0, y0 - over), (x0, y1 + over)),
        msp.add_line((x1, y0 - over), (x1, y1 + over)),
    ]


def test_pick_left_half_with_circle_island():
    _, msp = doc_msp()
    ents = rect_lines(msp, 0, 0, 100, 60)
    ents.append(msp.add_line((50, -5), (50, 65)))  # 分隔线
    ents.append(msp.add_circle((25, 30), 10))
    r = find_boundary(ents, (10, 10))
    expect = 50 * 60 - math.pi * 100
    assert abs(r.area - expect) / expect < 0.005
    assert r.islands == 1


@pytest.mark.parametrize("style, expect", [
    ("normal", 100 * 100 - math.pi * 900 + 400),  # 矩形 - 圆 + 圆里的小方块
    ("outer", 100 * 100 - math.pi * 900),
    ("ignore", 100 * 100),
])
def test_nested_islands_three_styles(style, expect):
    _, msp = doc_msp()
    ents = rect_lines(msp, 0, 0, 100, 100, over=0)
    ents.append(msp.add_circle((50, 50), 30))
    ents.append(msp.add_lwpolyline([(40, 40), (60, 40), (60, 60), (40, 60)], close=True))
    r = find_boundary(ents, (5, 5), style=style)
    assert abs(r.area - expect) / expect < 0.005


def _gap_rect(msp):
    return [
        msp.add_line((0, 0), (100, 0)), msp.add_line((0, 60), (100, 60)), msp.add_line((0, 0), (0, 60)),
        msp.add_line((100, 0), (100, 29.8)), msp.add_line((100, 30.2), (100, 60)),  # 右边 (100, 30) 处 0.4 mm 缺口
    ]


def test_gap_tolerance_and_gap_report():
    _, msp = doc_msp()
    ents = _gap_rect(msp)
    r = find_boundary(ents, (50, 30), gap_tol=0.5)
    assert abs(r.area - 6000) < 1
    with pytest.raises(HatchError) as ex:
        find_boundary(ents, (50, 30), gap_tol=0)
    e = ex.value
    assert e.code == "E_HATCH_NO_BOUNDARY"
    at = e.detail["gaps"][0]["at"]
    assert math.dist(at, (100, 30)) < 0.1
    assert e.detail["gaps"][0]["length_mm"] == pytest.approx(0.4, abs=0.01)
    assert "0.40 mm 缺口" in e.hint and "gap_tol" in e.hint


def test_pick_outside_or_on_line():
    _, msp = doc_msp()
    ents = rect_lines(msp, 0, 0, 100, 60)
    with pytest.raises(HatchError) as ex:
        find_boundary(ents, (300, 300))
    assert ex.value.code == "E_HATCH_NO_BOUNDARY"
    with pytest.raises(HatchError) as ex:
        find_boundary(ents, (50, 0))
    assert ex.value.code == "E_HATCH_ON_BOUNDARY"


def test_text_inside_region_is_cut_out():
    from shapely.geometry import box

    doc, msp = doc_msp()
    ents = rect_lines(msp, 0, 0, 100, 60)
    text_box = (20, 20, 40, 25)
    r = find_boundary(ents, (70, 50), text_boxes=[text_box])
    assert r.text_holes == 1
    assert r.geometry.intersection(box(*text_box)).area == 0
    # 外扩 1 mm 后仍不相交
    assert r.geometry.intersection(box(19.01, 19.01, 40.99, 25.99)).area == 0
    h = write_hatch(msp, r, "ANSI31", 1.0)
    assert len(h.paths) == 2


def test_select_circle_writes_exact_arc():
    _, msp = doc_msp()
    c = msp.add_circle((0, 0), 20)
    r = boundary_from_entities([c])
    assert r.exact and abs(r.area - math.pi * 400) / (math.pi * 400) < 0.005
    h = write_hatch(msp, r, "ANSI31", 1.0)
    path = h.paths.paths[0]
    assert path.type == BoundaryPathType.EDGE
    assert [e.type for e in path.edges] == [EdgeType.ARC]
    with pytest.raises(HatchError) as ex:
        boundary_from_entities([msp.add_line((0, 0), (10, 0))])
    assert ex.value.code == "E_HATCH_NOT_CLOSED"


def test_pick_keeps_exact_polyline_boundary():
    _, msp = doc_msp()
    pl = msp.add_lwpolyline([(0, 0, 0), (100, 0, 0.5), (100, 50, 0), (0, 50, 0)], format="xyb", close=True)  # 右边是 bulge 弧
    r = find_boundary([pl], (50, 25))
    h = write_hatch(msp, r, "ANSI31", 1.0)
    path = h.paths.paths[0]
    assert path.type == BoundaryPathType.POLYLINE and any(abs(v[2]) > 0 for v in path.vertices)


def test_pattern_scale_paper_spacing():
    d = Drawing(id="x", paper_size="A3", scale=50)
    s = paper_scale("ANSI31", d.unit, 3)
    _, msp = doc_msp()
    r = boundary_from_entities([msp.add_circle((0, 0), 2000)])
    h = write_hatch(msp, r, "ANSI31", s, angle=0)
    line = h.pattern.lines[0]
    a = math.radians(line.angle)
    spacing = abs(-math.sin(a) * line.offset[0] + math.cos(a) * line.offset[1])
    assert abs(spacing - 3 * d.unit) / (3 * d.unit) < 0.01
    with pytest.raises(HatchError) as ex:
        check_pattern("ANSI3l")
    assert ex.value.code == "E_BAD_ARG" and "ANSI31" in ex.value.hint
    assert check_pattern("solid") == "SOLID"


# ---------------------------------------------------------------- 接入 IR 之后（8～11）

def _one_tank():
    from zhihui.tools.core import ToolService

    svc = ToolService()
    did = svc.create_drawing(title="填充关联")["drawing_id"]
    assert svc.place_equipment(did, [{"symbol_id": "tank", "tag": "V-101", "name": "储罐"}])["ok"]
    assert svc.auto_layout(did)["ok"]
    return svc, did


def test_hatch_follows_equipment_and_disassociates(tmp_path):
    from zhihui.cad.dxf_io import write_dxf

    svc, did = _one_tank()
    r = svc.add_hatch(did, pattern="ANSI31", paper_spacing=3, pick={"anchor": "V-101", "dx": 0, "dy": 0})
    assert r["ok"], r
    assert r["islands"] == 0 and r["area_m2"] > 0 and r["preview"].endswith(".png")
    d0 = svc.store.load(did)
    h0 = d0.hatches[0]
    assert h0.anchor_ref == d0.get_equipment("V-101").id and not h0.disassociated
    assert svc.move_equipment(did, "V-101", dx=1000, dy=0)["ok"]
    d1 = svc.store.load(did)
    h1 = d1.hatches[0]
    assert abs(h1.area - h0.area) / h0.area < 0.005
    assert h1.rings[0][0][0] == pytest.approx(h0.rings[0][0][0] + 1000, abs=0.01)
    write_dxf(d1, tmp_path / "a.dxf")
    hatch = next(iter(ezdxf.readfile(tmp_path / "a.dxf").modelspace().query('HATCH[layer=="ZH-剖面线"]')))
    xs = [v[0] for v in hatch.paths.paths[0].vertices]
    assert min(xs) == pytest.approx(min(x for x, _ in h1.rings[0]), abs=0.01)
    # 换成上面开口的坑：拾取点周围不再封闭 → 失去关联，校验给警告，沿用上次的边界
    assert svc.edit_equipment(did, "V-101", new_symbol_id="pit")["ok"]
    d2 = svc.store.load(did)
    assert d2.hatches[0].disassociated and d2.hatches[0].rings == h1.rings
    v = svc.validate_drawing(did)
    assert v["passed"] and "W_HATCH_DISASSOCIATED" in {w["code"] for w in v["warnings"]}
    found = svc.find_entities(did, kind="hatch")["candidates"]
    assert found[0]["ref"] == h0.id and found[0]["disassociated"]


def test_edit_and_delete_hatch():
    svc, did = _one_tank()
    hid = svc.add_hatch(did, pick={"anchor": "V-101", "dx": 0, "dy": 0})["hatch_id"]
    r = svc.edit_hatch(did, hid, pattern="solid", angle=30)
    assert r["ok"] and r["changes"]["pattern"] == ["ANSI31", "SOLID"]
    assert svc.edit_hatch(did, hid, pattern="ANSI3l")["error_code"] == "E_BAD_ARG"
    assert svc.edit_hatch(did, hid, delete=True)["ok"]
    assert svc.store.load(did).hatches == []
    bad = svc.add_hatch(did, pick={"x": -1e6, "y": -1e6})
    assert bad["error_code"] == "E_HATCH_NO_BOUNDARY"


def test_hatches_roundtrip_through_dxf(tmp_path):
    from zhihui.cad.dxf_io import read_dxf, write_dxf

    svc, did = _one_tank()
    assert svc.add_hatch(did, pattern="EARTH", paper_spacing=2, angle=15, style="outer", pick={"anchor": "V-101", "dx": 0, "dy": 0}, gap_tol=0.5)["ok"]
    assert svc.add_hatch(did, pattern="SOLID", select=["V-101"])["ok"]
    d = svc.store.load(did)
    write_dxf(d, tmp_path / "a.dxf")
    d2, rep = read_dxf(tmp_path / "a.dxf")
    assert rep.topology_consistent and d2.topology_hash() == d.topology_hash()
    keys = ("id", "pattern", "paper_spacing", "angle", "style", "layer", "mode", "anchor_ref", "pick_rel", "boundary_refs", "gap_tol", "area", "rings")
    assert [{k: getattr(h, k) for k in keys} for h in d2.hatches] == [{k: getattr(h, k) for k in keys} for h in d.hatches]
    assert d2.next_seq > max(int(h.id[1:]) for h in d.hatches)


def test_render_20_hatches_under_10s(tmp_path):
    """A3 给水高程图（自带池壁、滤料、地面剖面线）再加 20 个填充（标题栏格子），10 秒内出 PNG。"""
    import time

    from zhihui.cad.dxf_io import write_dxf
    from zhihui.cad.render import render
    from zhihui.cad.symbols import WATER_TITLE_H, WATER_TITLE_W, inner_frame
    from zhihui.ir import Hatch
    from tests.test_water import build_plan
    from zhihui.bench.run import load_cases

    _, d = build_plan(load_cases()["W001"]["plan"])
    _, fy0, fx1, _ = inner_frame(d)
    s = d.scale
    x0 = fx1 - WATER_TITLE_W * s
    rh = 40 / 7
    cells = [(7, 40 - (i + 0.5) * rh) for i in range(7)] + [(29, 40 - (i + 0.5) * rh) for i in range(7)]
    cells += [(53, 36), (121, 36), (53, 28), (83.5, 28), (113, 28), (150.5, 28)]
    pats = ["ANSI31", "SOLID", "EARTH", "AR-SAND", "GRAVEL", "AR-CONC"]
    for i, (cx, cy) in enumerate(cells):
        d.hatches.append(Hatch(id=f"H{900 + i}", pattern=pats[i % len(pats)], paper_spacing=2, pick_rel=(x0 + cx * s, fy0 + cy * s)))
    assert len(d.hatches) == 20
    t0 = time.monotonic()
    write_dxf(d, tmp_path / "h.dxf")
    render(tmp_path / "h.dxf", tmp_path / "h.png", d.paper_size)
    assert time.monotonic() - t0 < 10
    msp = ezdxf.readfile(tmp_path / "h.dxf").modelspace()
    assert len(msp.query('HATCH[layer=="ZH-剖面线"]')) == 20
