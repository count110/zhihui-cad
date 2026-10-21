"""任务书第 10 节的 14 条演示口令：`--mock`（离线、不调模型）下按顺序在同一张图上跑通，并检查图里真的画出了对的东西。

整套口令只跑一次（module 级 fixture），每步之后留一份图纸快照，下面按口令编号逐条断言。
"""
import asyncio
import math
import os

import pytest

from gen_helpers import Env, pclose
from zhihui.agent.general.backend import LocalBackend
from zhihui.agent.general.engine import GeneralAgent

PHRASES = [
    "新建一张 A3 机械图，背景改成黑色，建一套国标机械图层，当前层设为粗实线",                         # 1
    "打开正交和对象捕捉，从原点向右画 100，再往上 60，再往左 100，闭合",                               # 2
    "画一个矩形，左下角在零零，长 120 宽 80；在矩形中心画一个直径 40 的圆",                           # 3
    "把刚才那个圆向外偏移 10，矩形四个角倒 R8 圆角",                                                 # 4
    "以大圆圆心为中心环形阵列 6 个直径 8 的小圆，分布圆直径 60",                                      # 5
    "把左边那条竖线删掉",                                                                           # 6
    "虚线显示成实线了，调一下线型比例",                                                              # 7
    "标注矩形的长和宽，标注大圆直径，长度加正负 0.1 的公差",                                          # 8
    "加一个平行度公差 0.02，基准 A，表面粗糙度 Ra3.2",                                               # 9
    "把小圆做成带属性的块，属性是规格，插到右上角",                                                   # 10
    "建一个布局，A3 横向，开一个 1 比 2 的视口并锁定，输出 PDF",                                      # 11
    "把这个带孔的板拉伸 30 变成实体，切到西南等轴测",                                                 # 12
    "给 C8 和 C9 加同心约束，C9 的直径等于 C8 的两倍",                                              # 13
    "撤销上一步",                                                                                   # 14a
    "把它改到中心线图层",                                                                           # 14b
    "全部显示",                                                                                     # 14c
]


class Run:
    def __init__(self, tmp_path):
        os.environ["ZHIHUI_LLM"] = "mock"
        self.env = Env(tmp_path)
        self.did = None
        self.outs, self.snaps, self.events = [], [], []

    def phrase(self, text, answer_to=None):
        ag = GeneralAgent(on_event=lambda k, p: self.events.append((k, p)), mock=True, backend=LocalBackend(self.env.svc), drawing_id=self.did)
        out = asyncio.run(ag.run(text))
        self.did = out.drawing_id or self.did
        self.outs.append(out)
        self.snaps.append(self.env.d(self.did).model_copy(deep=True))
        return out

    def d(self, n):
        return self.snaps[n - 1]


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    r = Run(tmp_path_factory.mktemp("demo"))
    for p in PHRASES:
        r.phrase(p)
    return r


def ok(out):
    return out.status in ("READY", "READY_WITH_WARNINGS")


def test_every_phrase_runs_offline_without_clarification_or_failure(demo):
    for i, (p, out) in enumerate(zip(PHRASES, demo.outs), 1):
        assert ok(out), f"{i}. {p} → {out.status}: {out.message}"
    assert all(o.usage["prompt_tokens"] == 0 and o.usage["completion_tokens"] == 0 for o in demo.outs)           # 全程没有调用模型


def test_01_new_mechanical_a3_black_background_gb_layers_current_cusi(demo):
    d = demo.d(1)
    assert d.mode == "general" and d.background == "#000000" and d.limits == (0, 0, 420, 297)
    names = [l.name for l in d.layers]
    assert {"粗实线", "细实线", "虚线", "中心线", "剖面线", "尺寸标注", "文字"} <= set(names) and d.current_layer == "粗实线"


def test_02_ortho_osnap_and_closed_rectangle_of_lines(demo):
    d = demo.d(2)
    assert d.draft.ortho_on and d.draft.osnap_on
    lines = [e for e in d.entities if e.kind == "line"]
    assert len(lines) == 4
    pts = [(tuple(e.data["p1"][:2]), tuple(e.data["p2"][:2])) for e in lines]
    assert pts[0] == ((0, 0), (100, 0)) and pts[1] == ((100, 0), (100, 60)) and pts[2] == ((100, 60), (0, 60)) and pts[3] == ((0, 60), (0, 0))        # 闭合：回到原点
    assert all(e.layer == "粗实线" for e in lines)


def test_03_rectangle_120_by_80_and_circle_d40_at_its_center(demo):
    d = demo.d(3)
    rect = next(e for e in d.entities if e.kind == "polyline")
    xs = [v["p"][0] for v in rect.data["vertices"]]
    ys = [v["p"][1] for v in rect.data["vertices"]]
    assert (min(xs), min(ys), max(xs), max(ys)) == (0, 0, 120, 80) and "rectangle" in rect.tags
    c = next(e for e in d.entities if e.kind == "circle")
    assert pclose(c.data["c"], (60, 40)) and c.data["r"] == 20                                       # 中心来自矩形的几何中心，不是模型算的


def test_04_offset_circle_outward_10_and_rectangle_corners_filleted_r8(demo):
    d = demo.d(4)
    circles = sorted((e for e in d.entities if e.kind == "circle"), key=lambda e: e.data["r"])
    assert [c.data["r"] for c in circles] == [20, 30] and all(pclose(c.data["c"], (60, 40)) for c in circles)            # 向外偏移 = 半径变大
    rect = next(e for e in d.entities if e.kind == "polyline")
    vs = rect.data["vertices"]
    assert len(vs) == 8 and sum(1 for v in vs if abs(v["bulge"]) > 0) == 4
    assert all(abs(abs(v["bulge"]) - math.tan(math.pi / 8)) < 1e-9 for v in vs if abs(v["bulge"]) > 0)                  # 90° 圆弧的凸度
    xs = [v["p"][0] for v in vs]
    assert min(xs) == 0 and max(xs) == 120


def test_05_polar_array_six_d8_circles_on_d60_bolt_circle_around_big_circle_center(demo):
    d = demo.d(5)
    small = [e for e in d.entities if e.kind == "circle" and e.data["r"] == 4]
    assert len(small) == 6                                                                           # “阵列 6 个”包含原对象
    for c in small:
        assert abs(math.hypot(c.data["c"][0] - 60, c.data["c"][1] - 40) - 30) < 1e-9                  # 分布圆直径 60 → 半径 30
    angles = sorted(round(math.degrees(math.atan2(c.data["c"][1] - 40, c.data["c"][0] - 60)) % 360, 6) for c in small)
    diffs = [(b - a) for a, b in zip(angles, angles[1:] + [angles[0] + 360])]
    assert all(abs(x - 60) < 1e-6 for x in diffs)                                                    # 均布，间隔 60°
    assert any("包含原对象" in n for n in demo.outs[4].message.split("。")) or "阵列数量" in demo.outs[4].message


def test_06_left_vertical_line_deleted_right_one_kept(demo):
    before, after = demo.d(5), demo.d(6)
    gone = {e.id for e in before.entities} - {e.id for e in after.entities}
    assert len(gone) == 1
    e = before.get_entity(next(iter(gone)))
    assert e.kind == "line" and e.data["p1"][0] == 0 and e.data["p2"][0] == 0                         # 删的是左边那条竖线（x=0），x=100 的那条还在
    assert any(x.kind == "line" and x.data["p1"][0] == 100 and x.data["p2"][0] == 100 for x in after.entities)
    sel_events = [p for k, p in demo.events if k == "recognize" and "左边" in str(p.get("query", ""))]
    assert sel_events and sel_events[0]["status"] == "resolved"


def test_07_linetype_scale_adjusted_without_inventing_a_number(demo):
    assert demo.d(6).ltscale == 1.0 and demo.d(7).ltscale > 0
    assert "自动计算" in demo.outs[6].message or "线型比例" in demo.outs[6].message


def test_08_dimensions_length_width_diameter_with_symmetric_tolerance(demo):
    d = demo.d(8)
    dims = [e for e in d.entities if e.kind == "dimension"]
    kinds = {e.data["dtype"]: e for e in dims}
    assert {"linear_horizontal", "linear_vertical", "diameter"} <= set(kinds)
    assert kinds["linear_horizontal"].data["measurement"] == 120 and kinds["linear_vertical"].data["measurement"] == 80 and kinds["diameter"].data["measurement"] == 60       # 大圆 = 偏移出来的 R30
    tol = kinds["linear_horizontal"].data["tol"]
    assert tol["mode"] == "symmetric" and tol["upper"] == 0.1 and tol["lower"] == -0.1               # 只有长度带公差
    assert not kinds["linear_vertical"].data.get("tol")
    assert all(e.layer == "尺寸标注" for e in dims)


def test_09_parallelism_gdt_datum_a_and_roughness_ra32(demo):
    d = demo.d(9)
    texts = [e.data["text"] for e in d.entities if e.kind == "text"]
    assert "0.02" in texts and "A" in texts and "Ra 3.2" in texts
    new_tags = {t for e in d.entities if not demo.d(8).get_entity(e.id) for t in e.tags}
    assert {"gdt_cell", "gdt_symbol", "gdt_text", "roughness"} <= new_tags                                  # 框格、符号、文字、粗糙度符号都是新画的
    assert "形位公差的被测要素未指定" in demo.outs[8].message or "被测要素" in demo.outs[8].message                  # 自己做的默认选择要告诉用户


def test_10_small_circle_made_into_block_with_attribute_and_inserted_top_right(demo):
    d = demo.d(10)
    blk = next(b for b in d.blocks if b["name"] == "小圆")
    assert [a["tag"] for a in blk["attdefs"]] == ["规格"]
    ins = next(e for e in d.entities if e.kind == "insert")
    assert ins.data["block"] == "小圆"
    assert pclose(ins.data["p"], (112, 80)) or pclose(ins.data["p"], (120, 72))                     # 矩形四角已是 R8 圆角：右上角取圆角的端点（真实的捕捉点），不取不存在的尖角 (120,80)
    assert demo.outs[9].status == "READY_WITH_WARNINGS" and "规格" in demo.outs[9].message            # 属性值没给：留空并告知，不编造


def test_11_layout_a3_landscape_viewport_1_to_2_locked_and_pdf_exported(demo):
    d = demo.d(11)
    assert len(d.layouts) == 1
    lay = d.layouts[0]
    assert lay["page_setup"]["size"] == [420, 297] and len(lay["viewports"]) == 1
    vp = lay["viewports"][0]
    assert vp["scale"] == "1:2" and vp["locked"] is True
    (x0, y0), (x1, y1) = vp["corners"]
    assert abs(abs(y1 - y0) / vp["view_height"] - 0.5) < 1e-9                                        # 视口高度 / 模型视图高度 = 1/2
    assert d.active_space == "Model"
    pdfs = [f for f in (demo.outs[10].artifacts or []) if f.get("format") == "pdf"]
    assert len(pdfs) == 1 and pdfs[0]["bytes"] > 1000                                                # PDF 作为产物交给用户
    assert demo.env.svc.store.resolve_ref(pdfs[0]["file_ref"]).read_bytes()[:4] == b"%PDF"


def test_12_plate_with_holes_extruded_30_and_southwest_isometric(demo):
    d = demo.d(12)
    solid = next(e for e in d.entities if e.kind == "solid3d")
    zs = [v[2] for v in solid.data["vertices"]]
    assert solid.data["solid_kind"] == "prism" and min(zs) == 0 and max(zs) == 30 and solid.data["volume"] > 0       # 拉伸高度 30
    assert solid.data["volume"] < 120 * 80 * 30                                                      # 比实心板小：孔扣掉了
    assert d.view.view3d == "sw_iso"
    assert any(e.kind == "polyline" for e in d.entities)                                             # 原轮廓保留（和 AutoCAD 默认不同，已在说明里告知）
    assert "轮廓图元已保留" in demo.outs[11].message


def test_13_concentric_and_diameter_equals_twice_constraint_solved(demo):
    d = demo.d(13)
    c8, c9 = d.get_entity("C8"), d.get_entity("C9")
    assert pclose(c8.data["c"], c9.data["c"]) and abs(c9.data["r"] - 2 * c8.data["r"]) < 1e-9
    assert pclose(c8.data["c"], demo.d(12).get_entity("C8").data["c"]) and c8.data["r"] == 4                  # 后选的 C9 向先选的 C8 靠拢，C8 不动（和 AutoCAD 一致）
    assert [c["type"] for c in d.constraints] == ["concentric", "diameter", "diameter"]                # 同心 + C8 直径作参数 d1 + C9 直径 = d1 × 2
    assert "数值未指定，取当前测量值" in demo.outs[12].message                                          # 没有凭空给参数值


def test_14a_undo_reverts_exactly_the_constraint_step(demo):
    before, after = demo.d(13), demo.d(14)
    assert len(after.constraints) == 0 and not pclose(after.get_entity("C9").data["c"], after.get_entity("C8").data["c"])
    assert after.get_entity("C9").data["r"] == 4                                                     # 位置和半径都回到约束之前
    assert [e.id for e in after.entities] == [e.id for e in before.entities]


def test_14b_it_refers_to_last_object_and_moves_to_centerline_layer(demo):
    before, after = demo.d(14), demo.d(15)
    changed = [e.id for e in after.entities if before.get_entity(e.id) and before.get_entity(e.id).layer != e.layer]
    assert len(changed) == 1 and after.get_entity(changed[0]).layer == "中心线"
    assert after.get_layer("中心线") is not None


def test_14c_show_all_zooms_to_extents(demo):
    d = demo.d(16)
    bb = d.bbox()
    assert d.view.height >= (bb[3] - bb[1]) and d.view.height > 0
    cx, cy = (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2
    assert abs(d.view.center[0] - cx) < 1e-6 * max(1, abs(cx)) + 1.0 and abs(d.view.center[1] - cy) < 1.0 + 1e-6 * max(1, abs(cy))


def test_each_phrase_produced_at_most_one_version_per_mutating_step(demo):
    """每次成功修改产生一个版本；整句口令里的多个子步骤合并成一个事务的话版本数不会爆炸。"""
    revs = [s.revision for s in demo.snaps]
    assert revs == sorted(revs) and revs[-1] < 120


# ------------------------------------------------------------------ 口语误识别的写法也要能跑通
ASR_VARIANTS = [
    ("画一个举行，左下角在零零，长一百二十宽八十", lambda d: _rect_is(d, 0, 0, 120, 80)),
    ("画一个源，圆心在一百逗号五十，半径二十五", lambda d: _circle_is(d, (100, 50), 25)),
    ("打开正焦和对象捕捉", lambda d: d.draft.ortho_on and d.draft.osnap_on),
    ("建一个图成叫辅助线", lambda d: d.get_layer("辅助线") is not None),
    ("从零零画到一百逗号零再画到一百逗号六十", lambda d: len([e for e in d.entities if e.kind == "line"]) == 2),
    ("画一个直径四十的圆圆心在原点", lambda d: _circle_is(d, (0, 0), 20)),
    ("在原点画一个半径五的圆，然后撒销", lambda d: not any(e.kind == "circle" for e in d.entities)),
]


def _rect_is(d, x0, y0, x1, y1):
    r = [e for e in d.entities if e.kind == "polyline"]
    if not r:
        return False
    xs = [v["p"][0] for v in r[0].data["vertices"]]
    ys = [v["p"][1] for v in r[0].data["vertices"]]
    return (min(xs), min(ys), max(xs), max(ys)) == (x0, y0, x1, y1)


def _circle_is(d, c, r):
    cs = [e for e in d.entities if e.kind == "circle"]
    return bool(cs) and pclose(cs[0].data["c"], c) and abs(cs[0].data["r"] - r) < 1e-9


@pytest.mark.parametrize("phrase,check", ASR_VARIANTS, ids=[p for p, _ in ASR_VARIANTS])
def test_asr_error_variants_still_work(tmp_path, phrase, check):
    r = Run(tmp_path)
    out = r.phrase(phrase)
    assert ok(out), (phrase, out.status, out.message)
    assert check(r.snaps[-1]), (phrase, [(e.id, e.kind, e.data) for e in r.snaps[-1].entities][:5])
    assert out.usage["prompt_tokens"] == 0
