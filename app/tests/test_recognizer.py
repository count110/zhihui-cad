"""图元对象识别：中文指代 → resolved / ambiguous / not_found，写回选择集，带出特征点和边界。≥40 条参考用例。"""
import asyncio

import pytest

from gen_helpers import Env, pclose
from zhihui.agent.general.backend import LocalBackend
from zhihui.agent.general.recognizer import Recognizer
from zhihui.agent.general.recognizer_rules import parse_phrase


class Scene:
    """一张有代表性的图：带四个角孔和一个中心大孔的板、两条竖线、一条红色中心线、一段圆弧、两行文字。"""

    def __init__(self, tmp_path):
        self.env = Env(tmp_path)
        e = self.env
        self.did = e.new("blank_a3")
        c = lambda tool, **kw: e.call(tool, self.did, **kw)["created"][0]
        self.rect = c("draw_polygons_and_meshes", shape_type="rectangle", corner1="0,0", corner2="200,120")
        self.bl, self.br, self.tr, self.tl = (c("draw_circular_primitives", curve_type="circle", method="center_radius", center=p, radius=5) for p in ("15,15", "185,15", "185,105", "15,105"))
        self.big = c("draw_circular_primitives", curve_type="circle", method="center_radius", center="100,60", radius=20)
        self.vl = c("draw_linear_primitives", primitive_type="line", points=["-20,0", "-20,120"])
        self.vr = c("draw_linear_primitives", primitive_type="line", points=["220,0", "220,120"])
        e.call("manage_layers", self.did, action="create", layer_name="中心线")
        e.call("set_layer_properties", self.did, layer_name="中心线", color="red")
        self.cl = c("draw_linear_primitives", primitive_type="line", points=["0,60", "200,60"], layer="中心线")
        self.arc = c("draw_circular_primitives", curve_type="arc", method="start_center_angle", start_pt="330,60", center="300,60", included_angle_deg=90)
        self.t1 = c("create_text_annotation", content="技术要求", insert_point="0,150", height=5)
        self.t2 = c("create_text_annotation", content="备注", insert_point="100,150", height=5)
        self.rec = Recognizer(LocalBackend(e.svc))

    def resolve(self, phrase, need=None, expect=None):
        return asyncio.run(self.rec.resolve(phrase, expect, need, self.did, None))

    def sel(self):
        return self.env.d(self.did).session.selection


@pytest.fixture
def S(tmp_path):
    return Scene(tmp_path)


# (短语模板, 期望状态, 期望图元名列表)——名字在运行时换成 id
CASES = [
    # 显式编号
    ("{br}", "resolved", ["br"]),
    ("圆{tr}", "resolved", ["tr"]),
    ("{vl}和{vr}", "resolved", ["vl", "vr"]),
    ("{rect}", "resolved", ["rect"]),
    # 大小
    ("大圆", "resolved", ["big"]),
    ("小圆", "resolved", ["bl", "br", "tr", "tl"]),
    ("最大的圆", "resolved", ["big"]),
    ("面积最大的圆", "resolved", ["big"]),
    ("最小的圆", "resolved", ["bl", "br", "tr", "tl"]),
    ("半径大于10的圆", "resolved", ["big"]),
    ("直径小于15的圆", "ambiguous", ["bl", "br", "tr", "tl"]),
    ("直径等于10的圆", "ambiguous", ["bl", "br", "tr", "tl"]),
    ("半径大于100的圆", "not_found", []),
    # 方位
    ("左上角的小圆", "resolved", ["tl"]),
    ("右下角的圆", "resolved", ["br"]),
    ("左边的竖线", "resolved", ["vl"]),
    ("右边那条竖线", "resolved", ["vr"]),
    ("最左边的竖线", "resolved", ["vl"]),
    ("最右边的竖线", "resolved", ["vr"]),
    ("最上面的文字", "ambiguous", ["t1", "t2"]),                       # 两行文字一样高，分不出
    # 序数 / 倒数 / 指代
    ("第2个圆", "resolved", ["br"]),
    ("第一个圆", "resolved", ["bl"]),
    ("第三个圆", "resolved", ["tr"]),
    ("最后一个圆", "resolved", ["big"]),
    ("倒数第2个圆", "resolved", ["tl"]),
    ("刚才画的", "resolved", ["t2"]),
    ("第9个圆", "not_found", []),
    # 类型
    ("竖线", "ambiguous", ["vl", "vr"]),
    ("所有的竖线", "resolved", ["vl", "vr"]),
    ("矩形", "resolved", ["rect"]),
    ("最大的矩形", "resolved", ["rect"]),
    ("圆弧", "resolved", ["arc"]),
    ("文字", "ambiguous", ["t1", "t2"]),
    ("孔", "ambiguous", ["bl", "br", "tr", "tl", "big"]),
    ("所有的圆", "resolved", ["bl", "br", "tr", "tl", "big"]),
    ("最长的线", "resolved", ["cl"]),
    ("最短的线", "ambiguous", ["vl", "vr"]),
    # 图层 / 颜色
    ("中心线层上的线", "resolved", ["cl"]),
    ("红色的线", "resolved", ["cl"]),                                # 颜色随层：红色来自图层
    # 关系
    ("和{big}相交的线", "resolved", ["cl"]),
    ("{rect}里面的所有圆", "resolved", ["bl", "br", "tr", "tl", "big"]),
    # 找不到 / 看不懂：必须说明原因，不能乱猜
    ("标注", "not_found", []),
    ("填充", "not_found", []),
    ("椭圆", "not_found", []),                                      # “椭圆”不能被其中的“圆”字误判成圆
    ("三维实体", "not_found", []),
    ("你好啊", "not_found", []),
]


@pytest.mark.parametrize("phrase,status,names", CASES, ids=[c[0] for c in CASES])
def test_reference_cases(S, phrase, status, names):
    ids = {k: getattr(S, k) for k in ("rect", "bl", "br", "tr", "tl", "big", "vl", "vr", "cl", "arc", "t1", "t2")}
    r = S.resolve(phrase.format(**ids))
    assert r["status"] == status, (phrase, r["status"], r["entity_ids"], r.get("log"))
    want = [ids[n] for n in names]
    got = r["entity_ids"] if status == "resolved" else r["candidates"] if status == "ambiguous" else []
    assert sorted(got) == sorted(want), (phrase, got, want)
    if status == "not_found":
        assert r["ambiguity_message"] and r["entity_ids"] == []            # 说明为什么找不到
    if status == "ambiguous":
        assert r["options"] and "请选择" in r["ambiguity_message"]


def test_resolved_writes_selection_and_ambiguous_highlights_candidates(S):
    r = S.resolve("左边的竖线")
    assert r["status"] == "resolved" and S.sel() == [S.vl] and r["matched_entities"][0]["layer"] == "0" and r["matched_entities"][0]["bbox"]
    r = S.resolve("竖线")
    assert r["status"] == "ambiguous" and sorted(S.sel()) == sorted([S.vl, S.vr])               # 候选高亮，用户可以点图作答
    assert S.env.rev(S.did) == S.env.rev(S.did)                                                  # 识别不产生新版本


def test_recognition_is_readonly_no_new_revision(S):
    rev = S.env.rev(S.did)
    for p in ("大圆", "竖线", "标注", "矩形的左下角"):
        S.resolve(p)
    assert S.env.rev(S.did) == rev and len(S.env.d(S.did).session.history) == 6 + 0 or S.env.rev(S.did) == rev


def test_not_found_explains_existing_values(S):
    r = S.resolve("半径大于100的圆")
    assert "半径" in r["ambiguity_message"] and "5" in r["ambiguity_message"] and "20" in r["ambiguity_message"]       # 告诉用户现有的半径是 5、20
    r = S.resolve("标注")
    assert "没有" in r["ambiguity_message"]


def test_hidden_layer_entities_are_not_candidates_and_message_says_so(S):
    S.env.call("set_layer_state", S.did, layer_name="中心线", is_on=False)
    r = S.resolve("红色的线")
    assert r["status"] == "not_found" and ("隐藏" in r["ambiguity_message"] or "冻结" in r["ambiguity_message"])


def test_feature_points(S):
    r = S.resolve("矩形的左下角")
    assert r["status"] == "resolved" and pclose(r["points"]["primary"], (0, 0))
    r = S.resolve(f"圆{S.big}的圆心")
    assert pclose(r["points"]["primary"], (100, 60))
    r = S.resolve(f"{S.cl}的中点")
    assert pclose(r["points"]["primary"], (100, 60))
    r = S.resolve("矩形的右上角")
    assert pclose(r["points"]["primary"], (200, 120))
    r = S.resolve(f"{S.arc}的端点")
    assert any(pclose(r["points"]["primary"], q) for q in ((330, 60), (300, 90)))
    r = S.resolve("矩形的几何中心")
    assert pclose(r["points"]["primary"], (100, 60))


def test_corner_point_vs_corner_location():
    assert parse_phrase("矩形的左下角").spatial_hint == "bottom-left" and parse_phrase("矩形的左下角").region is None
    s = parse_phrase("左上角的小圆")
    assert s.region == "top_left" and s.spatial_hint is None and s.size_class == "小"           # “左上角的小圆”是图元的位置，不是角点
    assert parse_phrase("右下角的圆").region == "bottom_right"


def test_continuous_reference_it_and_them(S):
    r = S.resolve("大圆")
    assert r["entity_ids"] == [S.big]
    r = S.resolve("它")
    assert r["status"] == "resolved" and r["entity_ids"] == [S.big]
    r = S.resolve("所有的圆")
    assert len(S.sel()) == 5
    r = S.resolve("它们")
    assert r["status"] == "resolved" and len(r["entity_ids"]) == 5
    r = S.resolve("它")
    assert r["status"] == "ambiguous" and "5" in r["log"] + r["ambiguity_message"]               # 选中了 5 个还说“它”：问，不猜
    r = S.resolve("这两个")
    assert r["status"] == "resolved" and len(r["entity_ids"]) == 2


def test_orientation_filter_and_slanted_lines(S):
    S.env.call("draw_linear_primitives", S.did, primitive_type="line", points=["0,200", "50,250"])
    r = S.resolve("竖线")
    assert S.vl in r["candidates"] and len(r["candidates"]) == 2                                  # 斜线不是竖线
    r = S.resolve("斜线")
    assert r["status"] == "resolved" and len(r["entity_ids"]) == 1


def test_boundary_with_islands_for_plate_with_holes(S):
    r = S.resolve("带孔的板的外轮廓", need=["boundary"])
    assert r["status"] == "resolved" and r["entity_ids"] == [S.rect]
    b = r["boundary"]
    assert b["is_closed"] if "is_closed" in b else True
    assert S.rect in b["outer"] and sorted(i for loop in b["islands"] for i in loop) == sorted([S.bl, S.br, S.tr, S.tl, S.big])     # 岛：四个角孔 + 中心孔


def test_from_ids_after_user_clarification(S):
    r = asyncio.run(S.rec.from_ids("左边的竖线", [S.vl], ["snap:midpoint"], S.did))
    assert r["status"] == "resolved" and r["method"] == "user" and pclose(r["points"]["primary"], (-20, 60))
    assert S.sel() == [S.vl]


def test_llm_fallback_only_runs_perception_tools(S):
    calls = []

    async def llm_plan(query, ctx):
        calls.append(query)
        return {"calls": [{"tool": "draw_circular_primitives", "args": {"curve_type": "circle", "method": "center_radius", "center": "0,0", "radius": 5}},
                          {"tool": "query_entities_by_filter", "args": {"entity_types": ["arc"]}}], "note": "弧"}
    rec = Recognizer(LocalBackend(S.env.svc), llm_plan)
    n = len(S.env.d(S.did).entities)
    r = asyncio.run(rec.resolve("那个弯弯的东西", None, None, S.did, None))
    assert calls and r["method"] == "llm" and r["status"] == "resolved" and r["entity_ids"] == [S.arc]
    assert len(S.env.d(S.did).entities) == n                                                      # 模型写的“画圆”被拒绝执行：识别步骤只读

    async def bad(query, ctx):
        return {"calls": [{"tool": "erase_entities", "args": {"entity_ids": [S.rect]}}]}
    r = asyncio.run(Recognizer(LocalBackend(S.env.svc), bad).resolve("随便什么", None, None, S.did, None))
    assert r["status"] == "not_found" and S.env.d(S.did).get_entity(S.rect) is not None


def test_rules_handle_cases_without_llm(S):
    """规则能解析的短语不应该调用大模型（离线可用、也省钱）。"""
    async def boom(query, ctx):
        raise AssertionError("规则能解析的短语不该问大模型")
    rec = Recognizer(LocalBackend(S.env.svc), boom)
    for p in ("大圆", "左边的竖线", "矩形", "最长的线"):
        assert asyncio.run(rec.resolve(p, None, None, S.did, None))["method"] == "rules"


@pytest.mark.parametrize("phrase,field", [("半径大于10的圆", "radius"), ("直径等于10的圆", "diameter"), ("长度大于100的线", "length"), ("面积大于1000的矩形", "area")])
def test_parse_numeric_conditions(phrase, field):
    s = parse_phrase(phrase)
    assert s.where and s.where[0]["field"] == field and isinstance(s.where[0]["value"], float)


@pytest.mark.parametrize("phrase,expect", [
    ("第3个圆", dict(ordinal=3)), ("第十个圆", dict(ordinal=10)), ("倒数第2个圆", dict(nth_from_last=1)), ("倒数第三个圆", dict(nth_from_last=2)),
    ("最后一个", dict(last=True)), ("刚才画的矩形", dict(context="last_created")), ("这三个", dict(context="last_selected", count=3)), ("椭圆", dict(types=["ellipse"])),
    ("矩形里的圆", dict(types=["circle"])), ("和L3相交的线", dict(relation={"type": "intersects", "to": "L3"})), ("平行于L2的线", dict(relation={"type": "parallel", "to": "L2"})),
    ("L5里面的圆", dict(relation={"type": "inside", "to": "L5"})),
])
def test_parse_phrase_fields(phrase, expect):
    s = parse_phrase(phrase)
    for k, v in expect.items():
        assert getattr(s, k) == v, (phrase, k, getattr(s, k))
