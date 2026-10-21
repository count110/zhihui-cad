"""化工设备图的路由（任务书 6.2）：流程图 / 设备图 / 通用绘图三分、反例、类别规则、入口决策 entry_decision、语音纠错后再路由、流程图链路的交接。"""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from zhihui.agent.general.router import CATEGORY_RULES, EQUIP_CAT, categories_of, entry_decision, route
from zhihui.agent.speech_normalize import normalize

# ---------------------------------------------------------------- 任务书第 11 节的演示口令
DEMO = {
    1: "画一台立式储罐的设备图，内径1600，筒体高2400，标准椭圆封头，壁厚8，材料Q345R，设计压力0.6MPa，设计温度80℃，人孔DN500，进料口DN80，出料口DN80，两个液位计口DN25，四个耳式支座",
    2: "画一个立式储罐",
    3: "把进料口转到90度",
    4: "筒体加长500",
    5: "上封头再加一个DN50的放空口",
    6: "支座改成3个",
    7: "设计压力改成1.0MPa",
    8: "画一个卧式储罐装配图，内径2000，切线长6000，壁厚10，两个鞍座，管口有进料DN100、出料DN100、人孔DN500",
    9: "出一张管口方位图",
    10: "导出 DXF 和 PDF",
    11: "画精馏工段的工艺流程图",
}
# 任务书第 0 节表里的 8 条口令（现状全部被当成流程图）
TABLE0 = [
    "立式储罐设备图：内径1600，筒体高2400，椭圆封头，壁厚8，Q345R，0.6MPa，80℃，人孔DN500、进出料DN80、液位计口2×DN25，4个耳座",
    "卧式储罐装配图 DN2000，鞍座",
    "固定管板式换热器设备总图",
    "带夹套搅拌反应釜设备图",
    "储罐管口方位图",
    "DN100 PN16 板式平焊法兰零件图",
    "板式精馏塔设备图",
    "画一个立式储罐（不给尺寸）",
]


def eq(prompt, **kw):
    r = route(prompt, **kw)
    return r.mode == "equipment"


# ---------------------------------------------------------------- 演示口令 1–11
@pytest.mark.parametrize("n", [1, 8])
def test_demo_full_requests_are_equipment_and_carry_the_equipment_category(n):
    r = route(DEMO[n])
    assert r.mode == "equipment" and not r.ask_kind and EQUIP_CAT in r.categories
    assert EQUIP_CAT in categories_of(normalize(DEMO[n]).text)                    # 引擎算类别用的就是这个函数（口令先规范化）


def test_demo_2_only_a_device_name_is_equipment_with_ask_kind():
    r = route(DEMO[2])
    assert r.mode == "equipment" and r.ask_kind and EQUIP_CAT in r.categories
    assert r.confidence < 0.7                                                       # 路由自己不反问，只带标记


@pytest.mark.parametrize("n", [3, 4, 5, 6, 7, 9, 10])
def test_demo_followups_on_a_vessel_drawing_stay_equipment(n):
    r = route(DEMO[n], "equipment", has_vessel=True)
    assert r.mode == "equipment" and not r.ask_kind and EQUIP_CAT in r.categories
    assert entry_decision(DEMO[n], SimpleNamespace(vessel=object(), workspace=EQUIP_CAT, mode="general")).mode == "equipment"


def test_demo_11_flowchart_request_stays_pfd_everywhere():
    assert route(DEMO[11]).mode == "pfd"
    assert route(DEMO[11], "general").mode == "pfd"
    assert route(DEMO[11], "pfd").mode == "pfd"
    r = route(DEMO[11], "equipment", has_vessel=True)                                # 当前是设备图：明确要流程图才建议新建流程图
    assert r.mode == "pfd" and r.categories == [] and "不会改动当前图" in r.reason


# ---------------------------------------------------------------- 第 0 节表：8 条口令
@pytest.mark.parametrize("i", [0, 1, 2, 3, 4, 6])
def test_table0_equipment_requests(i):
    r = route(TABLE0[i])
    assert r.mode == "equipment" and not r.ask_kind and EQUIP_CAT in r.categories, r


def test_table0_flange_part_drawing_is_not_a_vessel_drawing():
    assert route(TABLE0[5]).mode != "equipment"                                       # 零件图是 P2，不能被设备图抢走


def test_table0_bare_vessel_asks_which_kind():
    r = route(TABLE0[7])
    assert r.mode == "equipment" and r.ask_kind


# ---------------------------------------------------------------- 6.2-1 词表与反例
@pytest.mark.parametrize("p", ["画设备图", "出设备总图", "画设备装配图", "出一张管口方位图", "出管口方位", "要管口表", "技术特性表", "设计数据表放右上角", "V-101 的设备图"])
def test_kind_words_are_equipment(p):
    assert eq(p) and eq(p, drawing_mode="general") and eq(p, drawing_mode="pfd")


@pytest.mark.parametrize("p", ["流程设备图", "设备图例", "设备图形", "画设备图例库"])
def test_not_kind_words(p):
    assert not eq(p)


@pytest.mark.parametrize("p", ["画一张减速器装配图", "画个齿轮泵总图", "给装配图加明细栏", "画明细栏", "总图"])
def test_assembly_words_need_a_vessel_name_in_the_same_sentence(p):
    for mode in (None, "general", "pfd"):
        assert not eq(p, drawing_mode=mode), (p, mode)


@pytest.mark.parametrize("p", ["画一台储罐的装配图", "反应釜总图", "换热器明细栏怎么画", "画一个卧式容器装配图", "画精馏塔装配图"])
def test_assembly_words_count_with_a_vessel_name(p):
    assert eq(p)


def test_assembly_word_in_another_sentence_does_not_count():
    assert not eq("画一张减速器装配图。储罐接到反应釜")                                # 装配图所在的句子里没有容器类设备名；另一句是流程


def test_device_name_plus_two_structure_words_without_flow_words():
    assert eq("储罐内径1600，壁厚8")
    assert eq("卧式储罐，两个鞍座，切线长6000")
    assert not eq("储罐内径1600，壁厚8，接到反应釜")                                   # 两台不同设备 / 流程词：这是流程
    assert not eq("储罐和反应釜，内径1600，壁厚8")                                     # 同一句出现两台不同设备算流程
    assert not eq("储罐的壁厚8，物料送往下一工序，内径1600")
    assert not eq("内径1600，壁厚8")                                                  # 没有设备名


def test_two_devices_by_tag_count_as_flow():
    assert not eq("V-101 内径1600 壁厚8，R-101 内径800 壁厚6")
    assert eq("V-101 内径1600 壁厚8")


def test_vertical_horizontal_alone_is_not_a_kind_signal():
    assert route("在流程图里加一个卧式储罐", "pfd").mode == "pfd"                      # 图例库里就有“卧式储罐”
    assert route("在流程图里加一个卧式储罐", "general").mode == "pfd"
    r = route("卧式储罐")
    assert r.mode == "equipment" and r.ask_kind                                        # 单独出现只是“一台设备名”：带 ask_kind，不是图种词
    assert not route("原料罐 V-101 → 卧式储罐 V-102").mode == "equipment"
    assert route("立式反应釜 R-101 送往卧式储罐 V-102").mode != "equipment"


def test_a_pfd_workspace_adding_one_device_stays_pfd():
    assert route("画一个储罐", "pfd").mode == "pfd"
    assert route("画一个储罐").ask_kind and route("画一个储罐", "general").ask_kind


def test_device_name_alone_never_overrides_editing_or_general_cad():
    assert route("把产品罐 V-102 向右移动 1000 毫米").mode != "equipment"
    assert route("删除回流罐 V-101").mode != "equipment"
    assert route("给容器加一个直径20的孔", "general").mode == "general"
    assert route("画一个矩形，左下角在零零，长120宽80", "general").mode == "general"


def test_flow_requests_with_equipment_names_stay_pfd():
    assert route("绘制乙醇水精馏工艺方案：原料罐 V-101 → 进料泵 P-101 → 精馏塔 T-101，塔底连接再沸器 E-102", "general").mode == "pfd"
    assert route("画一张工艺流程图：原料罐 V-101 → 进料泵 P-101 → 反应釜 R-101").mode == "pfd"
    assert route("画垃圾焚烧烟气流程：垃圾储坑 V-101 → 焚烧炉 F-101 → 余热锅炉 B-101").mode == "pfd"
    assert route("储罐的进料管道改成DN100").mode == "pfd"                              # 没有设备图在手，管道是流程词：和以前一样


@pytest.mark.parametrize("p", [
    "画一个卧式储罐，内径2000，切线长6000，壁厚10，两个鞍座，进料DN100出料DN100",
    "立式储罐 内径1600 筒体高2400 壁厚8",
    "画一个缓冲罐，内径1200，筒体高1800，壁厚8，椭圆封头",
    "画一个带夹套的搅拌反应釜，内径1600，带搅拌器",
])
def test_creation_requests_without_a_kind_word_but_with_structure_words_are_equipment(p):
    r = route(p)
    assert r.mode == "equipment" and not r.ask_kind and r.categories[0] == EQUIP_CAT


@pytest.mark.parametrize("p", ["画一个储罐到反应釜的流程", "画一个精馏塔的工艺流程图", "画储罐 V-101 的P&ID", "画一个带控制点的储罐流程图", "画一个储罐的工艺流程"])
def test_flowchart_requests_that_name_one_device_stay_pfd(p):
    assert route(p).mode == "pfd" and route(p, "general").mode == "pfd"


@pytest.mark.parametrize("p", ["画一个换热器", "我要一台储罐，直径一米六，高两米四", "画一个反应釜，尺寸待定"])
def test_one_device_and_nothing_else_asks_which_kind(p):
    r = route(p)
    assert r.mode == "equipment" and r.ask_kind and r.categories[0] == EQUIP_CAT


# ---------------------------------------------------------------- 6.2-3 当前是设备图
def test_储罐的进料管道改成DN100_on_a_vessel_drawing_is_equipment():
    assert route("储罐的进料管道改成DN100", "equipment", has_vessel=True).mode == "equipment"
    assert route("储罐的进料管道改成DN100", has_vessel=True).mode == "equipment"


@pytest.mark.parametrize("p", ["换热管换成φ19", "接到反应釜的管道删掉", "把泵和阀都删掉", "精馏塔的塔盘改成30块"])
def test_on_a_vessel_drawing_flow_words_and_device_names_do_not_score(p):
    assert route(p, "equipment", has_vessel=True).mode == "equipment"


@pytest.mark.parametrize("p", ["画精馏工段的工艺流程图", "出一张PFD", "改成P&ID", "画带控制点的工艺流程", "新建一张流程图", "画工艺流程图"])
def test_on_a_vessel_drawing_only_explicit_flowchart_words_suggest_pfd(p):
    r = route(p, "equipment", has_vessel=True)
    assert r.mode == "pfd"


def test_flowchart_as_the_source_is_not_a_flowchart_request():
    assert eq("把流程图里 V-101 生成设备图")
    assert route("把流程图里 V-101 生成设备图", "equipment", has_vessel=True).mode == "equipment"


def test_blank_equipment_drawing_without_vessel_may_ask_kind():
    r = route("画一个立式储罐", "equipment")                                           # hg_equipment_a1 刚新建、还没有 vessel
    assert r.mode == "equipment" and r.ask_kind
    assert not route("画一个立式储罐", "equipment", has_vessel=True).ask_kind


# ---------------------------------------------------------------- 6.2-4 类别规则与误命中
def test_category_rules_have_chem_equipment():
    assert EQUIP_CAT in dict(CATEGORY_RULES)
    for p in ("封头厚度8", "两个鞍座", "管口方位图", "夹套", "设计压力0.6MPa", "卧式储罐装配图"):
        assert EQUIP_CAT in categories_of(p), p
    for p in ("画一个圆，直径50", "圆环内径30外径40", "标注DN100的管子", "画一个矩形", "减速器装配图"):
        assert EQUIP_CAT not in categories_of(p), p                                    # 内径/外径/直径/DN 太通用，不单独触发


def test_chem_equipment_is_not_crowded_out_by_the_top_three():
    cats = categories_of("画一个图层里的矩形，标注尺寸，输出PDF，缩放，鞍座")
    assert EQUIP_CAT in cats and len(cats) <= 3


@pytest.mark.parametrize("p", ["固定管板式换热器", "固定鞍座在左边", "固定端和滑动端", "20块塔板", "塔板数20块", "块数改成30", "一共 30 块塔板"])
def test_false_hits_on_constraint_and_block_categories_are_gone(p):
    assert "block_xref_constraint" not in categories_of(p), p


@pytest.mark.parametrize("p", ["固定这条线", "把这个定义成块", "插入块", "创建属性块", "固定圆心"])
def test_real_constraint_and_block_requests_still_hit(p):
    assert "block_xref_constraint" in categories_of(p), p


# ---------------------------------------------------------------- 入口决策 entry_decision(prompt, drawing, has_dxf)
def drawing(mode="general", workspace="2d_drafting", vessel=None):
    return SimpleNamespace(mode=mode, workspace=workspace, vessel=vessel)


def test_entry_decision_without_a_drawing_is_the_plain_route():
    for p in DEMO.values():
        assert entry_decision(p).mode == route(p).mode
    assert entry_decision(DEMO[1]).mode == "equipment" and entry_decision(DEMO[2]).ask_kind


def test_entry_decision_reads_vessel_and_workspace_from_the_drawing():
    assert entry_decision(DEMO[3], drawing(vessel=object())).mode == "equipment"
    assert entry_decision(DEMO[3], drawing(workspace=EQUIP_CAT)).mode == "equipment"
    assert entry_decision(DEMO[11], drawing(vessel=object())).mode == "pfd"
    assert entry_decision("画一个矩形", drawing()).mode == "general"                    # 普通通用图：原来的判断
    assert entry_decision("画一个储罐", drawing(mode="pfd")).mode == "pfd"
    assert entry_decision("画一个储罐的设备图", drawing(mode="pfd")).mode == "equipment"
    assert entry_decision("画一个矩形", drawing(workspace=EQUIP_CAT)).mode == "equipment"      # 设备图工作空间里，通用编辑也留在设备图


def test_entry_decision_has_dxf_keeps_the_flowchart_default():
    assert entry_decision("改一下", None, True).mode == "pfd"
    assert entry_decision("画一个储罐的设备图", None, True).mode == "equipment"


def test_entry_decision_with_a_real_drawing_object():
    from zhihui.ir import Drawing
    from zhihui.ir.vessel import VesselSpec
    plain = Drawing(id="dwg_a", mode="general")
    sheet = Drawing(id="dwg_b", mode="general", workspace=EQUIP_CAT)
    withv = Drawing(id="dwg_c", mode="general", vessel=VesselSpec(kind="vertical_vessel"))
    assert entry_decision("画一个矩形", plain).mode == "general"
    assert entry_decision("画一个立式储罐", sheet).ask_kind                              # 样板新建、还没参数
    assert entry_decision("把进料口转到90度", withv).mode == "equipment" and not entry_decision("把进料口转到90度", withv).ask_kind
    assert entry_decision(DEMO[11], withv).mode == "pfd"


def test_route_keeps_positional_signature():
    assert route("画个圆", "general", False).mode == "general"
    assert route("画个圆", None, True).mode == "pfd"                                    # 上传 DXF：先按流程图改图流程
    r = route("没有特征", None, False)
    assert (r.mode, r.categories, r.ask_kind) == ("pfd", [], False)


# ---------------------------------------------------------------- 语音纠错后再路由
@pytest.mark.parametrize("asr,fixed", [
    ("设备途", "设备图"), ("方为图", "方位图"), ("管口方向图", "管口方位图"), ("风头", "封头"), ("疯头", "封头"), ("耳坐", "耳座"), ("案座", "鞍座"), ("安座", "鞍座"),
    ("桶体", "筒体"), ("人空", "人孔"), ("夜位计", "液位计"), ("液味计", "液位计"), ("内经", "内径"), ("切线常", "切线长"),
])
def test_lexicon_fixes_equipment_words(asr, fixed):
    assert normalize(f"加一个{asr}").text == f"加1个{fixed}"


@pytest.mark.parametrize("src", ["风格统一", "一般情况下用粗实线", "画一个圆", "标注尺寸", "设备管理", "加一个安装孔", "同一个图层", "水桶体积", "人空间"])
def test_lexicon_does_not_touch_ordinary_phrases(src):
    for bad in ("封头", "鞍座", "筒体", "设备图", "人孔", "液位计", "内径"):
        assert bad not in normalize(src).text


def test_asr_variants_route_like_the_clean_text():
    asr = "画一台立式储罐的设备途，内径一千六，桶体高两千四，椭圆风头，壁厚八，设计压力零点六兆帕，四个耳坐"
    fixed = normalize(asr).text
    assert "设备图" in fixed and "筒体高2400" in fixed and "椭圆封头" in fixed and "耳座" in fixed and "内径1600" in fixed
    r = route(fixed)
    assert r.mode == "equipment" and not r.ask_kind and EQUIP_CAT in r.categories
    assert route(asr).mode == "equipment"                                               # 入口直接拿语音识别原文也能判断
    assert EQUIP_CAT in categories_of(fixed)
    assert route("出一张管口方为图", "equipment", has_vessel=True).mode == "equipment"
    assert route("出一张管口方为图").mode == "equipment"
    assert route("画一个卧式储罐，内经两千，切线常六千，壁厚十，两个案座").mode == "equipment"
    assert route("画一个卧式储罐，内经两千，切线常六千，壁厚十，两个案座").categories[0] == EQUIP_CAT
    assert route("画精馏工段的工艺流程图", "equipment", has_vessel=True).mode == "pfd"


# ---------------------------------------------------------------- 不回退：基准里的流程图 / 通用用例都不能被设备图抢走
def test_existing_bench_prompts_are_never_routed_to_equipment():
    root = Path(__file__).resolve().parents[1] / "06_测试与基准用例" / "cases"
    seen = 0
    for f in sorted(root.glob("*.json")):
        try:
            c = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if not isinstance(c, dict) or c.get("category") == "E":
            continue
        for key in ("prompt", "prompt_asr"):
            p = c.get(key)
            if isinstance(p, str):
                seen += 1
                for mode in (None, "general", "pfd"):
                    assert route(p, mode).mode != "equipment", (f.name, mode, p)
    assert seen > 50


# ---------------------------------------------------------------- 流程图链路：handoff
def test_flowchart_router_output_hands_equipment_requests_over():
    from zhihui.agent.coord.roles import RouterOutput, route as pfd_route
    assert RouterOutput(task_type="new_drawing").handoff is None
    assert pfd_route(DEMO[1]).handoff == "equipment"
    assert pfd_route(DEMO[8]).handoff == "equipment"
    assert pfd_route("画一张储罐管口方位图").handoff == "equipment"
    assert pfd_route(DEMO[11]).handoff is None
    assert pfd_route("画垃圾焚烧烟气流程：垃圾储坑 V-101 → 焚烧炉 F-101 → 余热锅炉 B-101").handoff is None
    assert pfd_route("画一个储罐").handoff is None                                      # 流程图里加一台储罐不算设备图请求
    assert pfd_route("把 F-201 右移 800 mm", "uploads/x.dxf").handoff is None
    assert "handoff" in RouterOutput.model_json_schema()["properties"]


def test_flowchart_coordinator_returns_a_switch_hint_instead_of_a_process_template(data_dir):
    from zhihui.agent.coord import Coordinator
    from zhihui.api.db import DB
    db = DB(data_dir / "zhihui.db")
    prompt = "画一台卧式储罐的设备图，内径2000，壁厚10，两个鞍座"
    db.create_task("t_handoff", prompt, None)
    out = asyncio.run(Coordinator(on_event=None, task_id="t_handoff", db=db, mock=True).run(prompt, None))
    assert out.status == "WAITING_CONFIRMATION" and out.drawing_id is None
    assert "化工设备图" in out.message and "工艺模板" in out.message
    assert out.spec is None                                                              # 没有套工艺模板、没有规划流程图
    assert db.get_task("t_handoff")["state"]["intent"]["handoff"] == "equipment"
    # 用户回答“仍按工艺流程图处理”：不再拦截，照常往下走（不会死循环）
    db.create_task("t_handoff2", prompt, None)
    out2 = asyncio.run(Coordinator(on_event=None, task_id="t_handoff2", db=db, mock=True).run(prompt, None, answer="仍按工艺流程图处理"))
    assert "化工设备图请求" not in out2.message
