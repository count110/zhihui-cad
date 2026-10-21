"""化工流程图规范化 第 3 步：阀门管件（文章表 2-11、3.2.3 节），仪表控制点（文章 3.2.4 节、表 2-12、2-13）。"""

import math

import ezdxf
import pytest

from zhihui.agent.guards import Guard
from zhihui.cad import fittings, instruments
from zhihui.cad.dxf_io import read_dxf, write_dxf
from zhihui.cad.standards import for_drawing
from zhihui.ir import Connection, Drawing, Equipment, Fitting
from zhihui.tools.core import ToolService

# 规范化要求 7.2 列出的全部名称（放空管、漏斗各有两种画法）
TABLE_2_11 = ["闸阀", "截止阀", "球阀", "旋塞阀", "隔膜阀", "三通截止阀", "三通球阀", "三通旋塞阀", "四通截止阀", "四通球阀", "四通旋塞阀",
              "角式截止阀", "角式球阀", "角式弹簧安全阀", "角式重锤安全阀", "减压阀", "疏水阀", "同心异径管", "圆形盲板正常开", "圆形盲板正常关",
              "8 字盲板正常开", "8 字盲板正常关", "旋启式止回阀", "蝶阀", "阻火器", "视镜", "喷射管", "文氏管", "锥型过滤器", "T 型过滤器", "法兰连接",
              "螺纹管帽", "软管连接", "管帽", "管端盲板", "管端法兰", "消声器", "安全淋浴器", "放空管", "漏斗"]


def _xd(e):
    return [v for _, v in e.get_xdata("ZHIHUI")] if e.has_xdata("ZHIHUI") else []


def test_fitting_library_covers_table_2_11():
    lib = fittings.library()
    for name in TABLE_2_11:
        fid = fittings.resolve(name)
        assert fid, name
        assert lib[fid]["graphics"] and lib[fid]["kind"] in ("inline", "across", "corner", "end")
    assert fittings.resolve("止回阀") == "swing_check" and fittings.resolve("安全阀") == "angle_safety_spring"
    with pytest.raises(ValueError, match="for_maintenance"):
        fittings.check({"type": "法兰"})  # 管道之间的法兰一般不画，检修用的才画（文章 3.2.3 节）
    assert fittings.check({"type": "法兰", "for_maintenance": True}).type == "flange"
    assert fittings.check({"type": "截止阀", "at": "to-12"}).at == "to-12"
    with pytest.raises(ValueError):
        fittings.check({"type": "截止阀", "at": "middle"})


def _pipe(path, fits):
    d = Drawing(id="f")
    d.equipment += [Equipment(id="E1", tag="V-1", symbol_id="tank", x=2000, y=9000), Equipment(id="E2", tag="V-2", symbol_id="tank", x=16000, y=6000)]
    d.connections.append(Connection(id="C1", from_equip="E1", from_port="out", to_equip="E2", to_port="in", path=path, fittings=fits))
    return d


def test_inline_valve_breaks_pipe_and_follows_direction(tmp_path):
    """阀门不是设备：插在管线上，管线在阀门处断开（长 6 宽 3），竖管上的阀门跟着转 90°。"""
    d = _pipe([(3000, 8000), (9000, 8000), (9000, 5000), (15000, 5000)],
              [Fitting(type="globe_valve", at=0.15), Fitting(type="ball_valve", at=0.5), Fitting(type="angle_globe", at=0.8)])
    pls = fittings.placements(d, d.connections[0])
    assert [p["type"] for p in pls] == ["globe_valve", "ball_valve", "angle_globe"]
    assert pls[1]["u"] == pytest.approx((0, -1))  # 落在竖段上
    assert pls[2]["p"] == pytest.approx((9000, 5000))  # 角式阀门在拐角
    write_dxf(d, tmp_path / "f.dxf")
    msp = ezdxf.readfile(tmp_path / "f.dxf").modelspace()
    parts = [e for e in msp.query("LWPOLYLINE") if _xd(e)[:1] in (["conn"], ["conn_part"])]
    gaps = []
    for a in parts:
        for b in parts:
            pa, pb = list(a.get_points("xy"))[-1], list(b.get_points("xy"))[0]
            if a is not b and (abs(pa[0] - pb[0]) < 1e-6 or abs(pa[1] - pb[1]) < 1e-6):
                gaps.append(round(math.dist(pa, pb)))
    assert 6 * d.scale in gaps  # 截止阀处断开 6 mm（图纸）
    sym = [e for e in msp if _xd(e)[:1] == ["fitting"]]
    assert {e.dxf.layer for e in sym} == {"ZH-阀门"} and {_xd(e)[3] for e in sym} == {"globe_valve", "ball_valve", "angle_globe"}
    std = for_drawing(d)
    old = std.raw.get("fittings")
    std.raw["fittings"] = {"valve_mm": [8, 4]}  # 配置选 8×4
    try:
        assert fittings.scale(d) == pytest.approx(8 / 6 * d.scale)
    finally:
        std.raw.pop("fittings")
        if old:
            std.raw["fittings"] = old


def test_end_fitting_and_reducer_size(tmp_path):
    d = _pipe([(3000, 8000), (15000, 8000)], [Fitting(type="reducer", at=0.4, size_class="DN50×DN32"), Fitting(type="vent_hook", at=1.0)])
    pls = fittings.placements(d, d.connections[0])
    assert pls[1]["kind"] == "end" and pls[1]["p"] == (15000, 8000)
    write_dxf(d, tmp_path / "e.dxf")
    texts = [t.dxf.text for t in ezdxf.readfile(tmp_path / "e.dxf").modelspace().query("TEXT")]
    assert "DN50×DN32" in texts  # 异径管标「大端×小端」


def test_migrate_valve_equipment_keeps_folded_topology():
    """旧图里当作设备的阀门迁移成管件：阀门折叠拓扑校验码不变，迁移后普通校验码回到没加阀门时的样子。"""
    svc = ToolService()
    did = svc.create_drawing(title="t")["drawing_id"]
    svc.place_equipment(did, [{"symbol_id": "pump", "tag": "P-101"}, {"symbol_id": "tank", "tag": "V-101"}])
    svc.connect_ports(did, [{"from_port": "P-101.out", "to_port": "V-101.in", "medium": "物料"}])
    plain = svc.store.load(did).topology_hash()
    c = svc.store.load(did).connections[0].id
    svc.connect_ports(did, remove=[c])  # 在两台设备之间插阀门（旧做法：阀门当设备，同基准用例 M004）
    svc.place_equipment(did, [{"symbol_id": "valve", "tag": "XV-101", "name": "调节阀"}])
    svc.connect_ports(did, [{"from_port": "P-101.out", "to_port": "XV-101.in", "medium": "物料"}, {"from_port": "XV-101.out", "to_port": "V-101.in"}])
    assert svc.auto_layout(did)["ok"]
    r = svc.migrate_valves(did)
    assert r["ok"] and r["folded_unchanged"] and r["migrated"][0]["valve"] == "XV-101"
    assert r["before"]["topology"] != r["after"]["topology"] and r["after"]["topology"] == plain
    d = svc.store.load(did)
    assert [e.tag for e in d.equipment] == ["P-101", "V-101"] and [f.type for f in d.connections[0].fittings] == ["gate_valve"]
    assert svc.validate_drawing(did)["passed"]


def test_instrument_letters_table_2_13():
    assert instruments.check_letters("TI")[0] == "ok" and instruments.check_letters("PdRA")[0] == "ok" and instruments.check_letters("FfCT")[0] == "ok"
    assert instruments.check_letters("LI")[0] == "warn"  # 液位不在表 2-13 里，允许但提示
    lvl, msg = instruments.check_letters("TIR")
    assert lvl == "warn" and "最接近" in msg
    assert instruments.check_letters("QI")[0] == "error" and instruments.check_letters("TX")[0] == "error"


def _with_instruments(tmp_path):
    svc = ToolService()
    did = svc.create_drawing(title="t", stage="construction", section_no="06", paper_size="A2")["drawing_id"]
    svc.place_equipment(did, [{"symbol_id": "reactor", "tag": "R0601", "name": "反应釜"}, {"symbol_id": "vessel", "tag": "V0601", "name": "受槽"},
                              {"symbol_id": "pump", "tag": "P0601", "name": "泵"}])
    r = svc.connect_ports(did, [{"from_port": "R0601.out", "to_port": "P0601.in", "medium": "工艺液体"},
                                {"from_port": "P0601.out", "to_port": "V0601.in", "medium": "工艺液体", "fittings": [{"type": "截止阀", "at": "to-12"}]}])
    c1, c2 = (x["ref"] for x in r["connections"])
    assert svc.auto_layout(did)["ok"]
    assert svc.add_instrument(did, "TI", "R0601", "0601")["ok"]
    assert svc.add_instrument(did, "PI", "V0601", "0602", location="central_panel")["ok"]
    assert svc.add_instrument(did, "FI", c2, "0603", location="local_inline", at=0.4)["ok"]
    assert svc.add_instrument(did, "FIC", c1, "0604", location="central_back")["ok"]
    d = svc.store.load(did)
    write_dxf(d, tmp_path / "i.dxf")
    return svc, did, d, ezdxf.readfile(tmp_path / "i.dxf").modelspace()


def test_instruments_drawn_per_table_2_12(tmp_path):
    svc, did, d, msp = _with_instruments(tmp_path)
    circles = {_xd(e)[1]: e for e in msp.query("CIRCLE") if _xd(e)[:1] == ["instrument"]}
    assert len(circles) == 4 and all(c.dxf.radius == pytest.approx(5 * d.scale) for c in circles.values())  # 直径 10 mm
    ids = {i.letters: i.id for i in d.instruments}
    def lines(iid):  # 圆内的横线（引线可能也是水平的，按两端都在圆内区分）
        c = circles[iid].dxf
        return [e for e in msp.query("LINE") if _xd(e)[:2] == ["instrument", iid] and abs(e.dxf.start[1] - e.dxf.end[1]) < 1e-6
                and all(math.dist((p.x, p.y), (c.center.x, c.center.y)) <= c.radius + 1e-6 for p in (e.dxf.start, e.dxf.end))]
    assert len(lines(ids["TI"])) == 0 and len(lines(ids["PI"])) == 1  # 就地无线，集中盘面一条实线
    back = [e for e in msp.query("LWPOLYLINE") if _xd(e)[:2] == ["instrument", ids["FIC"]]]
    assert back and all(e.dxf.layer.startswith("ZH-虚线") for e in back)  # 集中盘后：虚线
    txt = {t.dxf.text: t for t in msp.query("TEXT") if _xd(t)[:1] == ["instrument"]}
    cti = circles[ids["TI"]].dxf.center
    assert txt["TI"].dxf.align_point[1] > cti[1] > txt["0601"].dxf.align_point[1]  # 字母在上半圆，工段序号在下半圆
    lay = {it["ins"].letters: it for it in instruments.layout(d)}
    assert lay["FI"]["inline"] and lay["FI"]["c"] == lay["FI"]["p"]  # 嵌在管道中：圆骑在管线上
    assert not lay["TI"]["inline"] and math.dist(lay["TI"]["c"], lay["TI"]["p"]) >= 12 * d.scale - 1e-6  # 引线连到控制点
    f = svc.find_entities(did, query="TI 0601")
    assert f["candidates"][0]["kind"] == "instrument" and f["candidates"][0]["ref"] == ids["TI"]


def test_instrument_edit_delete_and_roundtrip(tmp_path):
    svc, did, d, msp = _with_instruments(tmp_path)
    assert svc.add_instrument(did, letters="TQ", attach_to="R0601")["error_code"] == "E_BAD_ARG"
    assert svc.add_instrument(did, letters="TI", attach_to="R0601", loop_no="06")["error_code"] == "E_BAD_ARG"  # 3～5 位数字
    r = svc.add_instrument(did, letters="LI", attach_to="V0601")
    assert r["ok"] and any("表 2-13" in n for n in r["notes"]) and any("工段序号" in n for n in r["notes"])
    assert svc.add_instrument(did, ref="TI0601", letters="TIA")["ok"]
    assert "TIA" in [i.letters for i in svc.store.load(did).instruments]
    d2, rep = read_dxf(tmp_path / "i.dxf")
    assert rep.topology_consistent and not rep.texts
    assert len(d2.instruments) == 4 and any(c.fittings for c in d2.connections)
    svc.edit_equipment(did, "V0601", delete=True)  # 设备删掉，挂在上面的仪表一起删
    assert {i.letters for i in svc.store.load(did).instruments} == {"TIA", "FIC"}


def test_guard_instrument_loop_and_fitting_size():
    g = Guard()
    said = [{"role": "user", "content": "反应釜上装温度指示 TI 0601，泵出口加异径管 DN50×DN32"}]
    assert g.check("add_instrument", {"letters": "TI", "loop_no": "0601"}, said) is None
    assert g.check("add_instrument", {"letters": "TI", "loop_no": "0605"}, said)["error_code"] == "E_UNSOURCED_VALUE"
    it = {"from_port": "a", "to_port": "b", "fittings": [{"type": "reducer", "size_class": "DN80×DN50"}]}
    assert g.check("connect_ports", {"items": [it]}, said)["error_code"] == "E_UNSOURCED_VALUE"
