"""图元指代的规则解析（离线）：中文短语 → RecSpec，再由 Recognizer 转成感知函数调用并打分。

覆盖：显式 id、类型词、方位词、大小比较、序数和指代、图层名和颜色、尺寸条件、关系、特征点词。
解析不了（没有任何线索）时 RecSpec.unparsed = True，由大模型兜底。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

ID_RX = re.compile(r"(?<![A-Za-z])(PT|ML|RY|XL|MS|TL|LD|PL|SP|EL|MT|[LCABDHTS])\s*(\d+)(?!\d)", re.I)
COLOR_WORDS = {"红": 1, "黄": 2, "绿": 3, "青": 4, "蓝": 5, "洋红": 6, "品红": 6, "白": 7, "灰": 8}

# 类型词：(正则, 类型列表, 标签, 方向)
TYPE_RULES: list[tuple[str, list[str], list[str], str | None]] = [
    (r"竖线|垂直线|竖直线|竖直的线|竖着的线", ["line"], [], "vertical"),
    (r"横线|水平线|水平的线|横着的线", ["line"], [], "horizontal"),
    (r"圆弧|弧线|弧(?!度)", ["arc"], [], None),
    (r"椭圆", ["ellipse"], [], None),
    (r"小圆|大圆|圆孔|圆形|圆(?![角弧心柱锥周环])|孔", ["circle"], [], None),
    (r"矩形|长方形|方框|方块|四边形", ["polyline"], ["rectangle"], None),
    (r"多边形|正[三四五六七八九十\d]+边形|[三四五六七八九十\d]+边形", ["polyline"], ["polygon"], None),
    (r"多段线|折线", ["polyline"], [], None),
    (r"样条|曲线", ["spline"], [], None),
    (r"文字|文本|字", ["text", "mtext"], [], None),
    (r"标注|尺寸", ["dimension"], [], None),
    (r"填充|剖面线|剖面", ["hatch"], [], None),
    (r"块参照|图块|块", ["insert"], [], None),
    (r"实体|三维体", ["solid3d"], [], None),
    (r"斜线|斜着的线|倾斜的线|斜的线", ["line"], [], "slanted"),
    (r"直线|线段|线条|(?<![心面型宽层])线(?![型宽层比例])", ["line"], [], None),
]
REGION_RULES = [
    (r"左上(?:角|方|边)?", "top_left"), (r"右上(?:角|方|边)?", "top_right"), (r"左下(?:角|方|边)?", "bottom_left"), (r"右下(?:角|方|边)?", "bottom_right"),
    (r"左边|左侧|左面|偏左|靠左", "left"), (r"右边|右侧|右面|偏右|靠右", "right"), (r"上边|上方|上面|顶部|靠上|偏上", "top"), (r"下边|下方|下面|底部|靠下|偏下", "bottom"),
    (r"中间|中央|居中|正中|中心位置", "center"),
]
SUPER_RULES = [
    (r"最左(?:边|侧)?", ("x", "min")), (r"最右(?:边|侧)?", ("x", "max")), (r"最上(?:边|面|方)?|最高", ("y", "max")), (r"最下(?:边|面|方)?|最低", ("y", "min")),
    (r"最大|最粗|最宽", ("area", "max")), (r"最小|最细", ("area", "min")), (r"最长", ("length", "max")), (r"最短", ("length", "min")),
    (r"靠近原点|离原点最近", ("origin", "min")),
]
FEATURE_RULES = [
    (r"圆心", ["center"]), (r"象限点", ["quadrant"]), (r"几何中心|形心|中心点|中心", ["center", "geometric_center"]), (r"中点", ["midpoint"]), (r"端点|起点|终点", ["endpoint"]),
    (r"交点", ["intersection"]), (r"切点", ["tangent"]), (r"垂足", ["perpendicular"]), (r"角点|顶点|四个角", ["endpoint"]),
]
CORNER_HINT = {"左下": "bottom-left", "右下": "bottom-right", "左上": "top-left", "右上": "top-right"}
CMP = {"大于": ">", "超过": ">", "多于": ">", "高于": ">", "小于": "<", "少于": "<", "低于": "<", "不到": "<", "至少": ">=", "不小于": ">=", "不少于": ">=", "不超过": "<=", "至多": "<=", "不大于": "<=", "等于": "=", "为": "=", "是": "="}
FIELD = {"半径": "radius", "直径": "diameter", "长度": "length", "长": "length", "宽度": "width", "宽": "width", "高度": "height", "高": "height", "面积": "area", "角度": "angle"}


@dataclass
class RecSpec:
    text: str = ""
    ids: list[str] = field(default_factory=list)
    context: str | None = None                 # last_created / last_modified / last_selected
    nth_from_last: int | None = None           # 倒数第 n 个（从 0 起）
    types: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    orient: str | None = None
    region: str | None = None
    superlative: tuple[str, str] | None = None
    size_class: str | None = None              # "大" / "小"：按尺寸分类选一类
    ordinal: int | None = None                 # 第 n 个（从 1 起）
    last: bool = False                         # 最后一个 / 最后画的
    layer: str | None = None
    color: int | None = None
    where: list[dict] = field(default_factory=list)
    relation: dict | None = None
    plural: bool = False
    count: int | None = None
    features: list[str] = field(default_factory=list)
    spatial_hint: str | None = None
    boundary: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def unparsed(self) -> bool:
        return not (self.ids or self.context or self.types or self.region or self.superlative or self.ordinal or self.last or self.layer or self.color is not None
                    or self.where or self.relation or self.size_class or self.nth_from_last is not None)


_CN_DIGIT = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_int(tok: str) -> int:
    """序数里的数字：阿拉伯数字或 一…九十九。"""
    if tok.isdigit():
        return int(tok)
    if tok == "十":
        return 10
    if tok.startswith("十"):
        return 10 + _CN_DIGIT.get(tok[1:], 0)
    if "十" in tok:
        a, b = tok.split("十", 1)
        return _CN_DIGIT.get(a, 1) * 10 + _CN_DIGIT.get(b, 0)
    return _CN_DIGIT.get(tok, 1)


def _num(s: str) -> float:
    return float(s)


def parse_phrase(text: str, layers: list[str] | None = None) -> RecSpec:
    t = text.strip()
    spec = RecSpec(text=t)
    work = t
    # ---- 显式 id（先抠出来，免得 “L2” 被当成别的词）----
    for m in ID_RX.finditer(work):
        spec.ids.append((m.group(1) + m.group(2)).upper())
    spec.ids = list(dict.fromkeys(spec.ids))
    work_noid = ID_RX.sub(" ", work)
    # ---- 关系（和 L3 相交的）----
    m = re.search(r"(?:和|与|跟|同)\s*(?:" + ID_RX.pattern + r")\s*(相交|平行|垂直|相切|同心)", work, re.I)      # 分组：1 前缀 2 数字 3 关系词
    if m:
        spec.relation = {"type": {"相交": "intersects", "平行": "parallel", "垂直": "perpendicular", "相切": "tangent", "同心": "concentric"}[m.group(3)], "to": (m.group(1) + m.group(2)).upper()}
        spec.ids = [i for i in spec.ids if i != spec.relation["to"]]
    m = re.search(r"(平行于|垂直于|相切于|同心于|与)\s*(?:" + ID_RX.pattern + r")", work, re.I)                      # 分组：1 关系词 2 前缀 3 数字
    if m and not spec.relation:
        word = m.group(1)
        rel = "parallel" if "平行" in word else "perpendicular" if "垂直" in word else "tangent" if "相切" in word else "concentric" if "同心" in word else None
        if rel:
            spec.relation = {"type": rel, "to": (m.group(2) + m.group(3)).upper()}
            spec.ids = [i for i in spec.ids if i != spec.relation["to"]]
    m = re.search(r"(?:" + ID_RX.pattern + r")\s*(?:里面|里边|内部|内|中)的", work, re.I)
    if m:
        spec.relation = {"type": "inside", "to": (m.group(1) + m.group(2)).upper()}
        spec.ids = [i for i in spec.ids if i != spec.relation["to"]]
    # ---- 指代 ----
    mn = re.search(r"倒数第\s*([\d一二两三四五六七八九十]+)\s*(?:个|条|根|段)", work)
    if mn:
        spec.nth_from_last = _cn_int(mn.group(1)) - 1
    elif re.search(r"刚才|刚画|刚刚|刚做|上[一1][个步]|最近(?:画|做|创建)|刚创建|前面那个", work):
        spec.context = "last_created"
    elif re.search(r"这些|它们|这几个|这两个|这三个|这\d+个|选中的|当前选择", work):
        spec.context = "last_selected"
        spec.plural = True
        mc = re.search(r"这(两|三|四|\d+)个", work)
        if mc:
            spec.count = {"两": 2, "三": 3, "四": 4}.get(mc.group(1)) or int(mc.group(1))
    elif re.fullmatch(r"\s*(?:它|这个|那个|该图元|此图元)\s*", work):
        spec.context = "last_selected_or_created"
    # ---- 序数 ----
    mo = re.search(r"第\s*([\d一二两三四五六七八九十]+)\s*(?:个|条|根|段)", work)
    if mo:
        spec.ordinal = _cn_int(mo.group(1))
    elif re.search(r"最后(?:[一1]个|[一1]条|画的|创建的)?|最新", work):
        spec.last = True
    # ---- 类型 ----
    accepted: list[tuple[int, int, tuple]] = []                                    # (起, 止, 规则)：按规则优先级接受，和已接受的重叠就忽略（“椭圆”里的“圆”）
    for rx, types, tags, orient in TYPE_RULES:
        for mm in re.finditer(rx, work_noid):
            if not any(mm.start() < b and a_ < mm.end() for a_, b, _ in accepted):
                accepted.append((mm.start(), mm.end(), (types, tags, orient)))
    if accepted:
        first = min(range(len(accepted)), key=lambda i: TYPE_RULES.index(next(r for r in TYPE_RULES if (r[1], r[2], r[3]) == accepted[i][2])))
        types, tags, orient = accepted[first][2]
        spec.types, spec.tags, spec.orient = list(types), list(tags), orient
        # 容器 + 里的东西：「矩形里的圆」→ 类型取最后出现的类型词
        last = max(accepted, key=lambda t: t[0])[2]
        if list(last[0]) != spec.types:
            spec.types, spec.tags, spec.orient = list(last[0]), list(last[1]), last[2]
    if re.search(r"轮廓|外框|边界|带孔的?板|(?<![A-Za-z])板", work_noid):
        spec.boundary = True
        spec.types, spec.tags, spec.orient = ["polyline", "circle"], [], None      # “带孔的板”里的“孔”不是要找的对象
        if not spec.superlative:
            spec.superlative = ("area", "max")                                    # 外轮廓 = 面积最大的那个
    if re.search(r"所有|全部|每个|每一个|所有的|这些|各个", work_noid):
        spec.plural = True
    if re.search(r"竖|垂直线", work_noid) and "line" in spec.types and not spec.orient:
        spec.orient = "vertical"
    # ---- 大小分类 / 最值 ----
    if re.search(r"大圆|较大的?圆|大的圆|大孔", work_noid):
        spec.size_class = "大"
    elif re.search(r"小圆|较小的?圆|小的圆|小孔", work_noid):
        spec.size_class = "小"
    for rx, sup in SUPER_RULES:
        if re.search(rx, work_noid):
            spec.superlative = sup
            if sup[0] == "area" and spec.types == ["line"]:
                spec.superlative = ("length", sup[1])
            break
    # ---- 方位（最X 已经是排序，不再当区域）----
    if not any(re.search(rx, work_noid) for rx, _ in SUPER_RULES if rx.startswith("最左") or rx.startswith("最右") or rx.startswith("最上") or rx.startswith("最下")):
        for rx, region in REGION_RULES:
            if re.search(rx, work_noid):
                spec.region = region
                break
    # ---- 图层 / 颜色 ----
    for name in sorted(layers or [], key=len, reverse=True):
        if name != "0" and name in work_noid:
            spec.layer = name
            break
    mc = re.search(r"(红|黄|绿|青|蓝|洋红|品红|白|灰)色?(?:的|线|圆)", work_noid)
    if mc and not spec.layer:
        spec.color = COLOR_WORDS[mc.group(1)]
    # ---- 尺寸条件 ----
    for m in re.finditer(r"(半径|直径|长度|长|宽度|宽|高度|高|面积|角度)\s*(大于|小于|少于|多于|超过|低于|高于|不到|至少|至多|不小于|不大于|不少于|不超过|等于|为|是|=|>=|<=|>|<)?\s*(\d+(?:\.\d+)?)", work_noid):
        fld, op, val = m.group(1), m.group(2), _num(m.group(3))
        field_name = FIELD[fld]
        if field_name == "length" and spec.tags and "rectangle" in spec.tags:
            field_name = "width"
        spec.where.append({"field": field_name, "op": CMP.get(op, op) if op else "=", "value": val})
    # ---- 特征点 ----
    for rx, snaps in FEATURE_RULES:
        if re.search(rx, work_noid):
            for s in snaps:
                if s not in spec.features:
                    spec.features.append(s)
    mh = re.search(r"(左下|右下|左上|右上)", work_noid)
    # 角点（“矩形的左下角”“左下角点”）和角落里的图元（“左上角的小圆”）要分开：后者的“角”后面还跟着“的/那个”等，是在描述图元的位置
    corner_point = bool(re.search(r"(?:左下|右下|左上|右上)(?:方|侧|边)?(?:的)?(?:角点|顶点|端点|点)|(?:左下|右下|左上|右上)角\s*(?:坐标|位置)?\s*[，,。]?\s*$", work_noid))
    if mh and corner_point:
        spec.spatial_hint = CORNER_HINT[mh.group(1)]
        if "endpoint" not in spec.features:
            spec.features = ["endpoint"] + [f for f in spec.features if f not in ("center", "geometric_center")]
        if spec.region in ("top_left", "top_right", "bottom_left", "bottom_right"):
            spec.region = None                      # “矩形的左下角”里的左下是点位，不是要找的图元的方位
    return spec
