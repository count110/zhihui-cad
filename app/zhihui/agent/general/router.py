"""路由（7.4）：用户原话 + 当前图纸的 mode → 走流程图（pfd）、化工设备图（equipment）还是通用绘图（general），并判断意图类别（1–3 个）。

先用关键词规则；拿不准时返回空类别，由 Planner 在计划里给出 intent_categories，再用对应的工具子集重新请求一次。
设备图的三分规则见任务书 6.2：图种词 / 设备名 + 结构词 / 只有设备名（ask_kind）；当前图纸是设备图时只有明确的流程图图种词才建议新建流程图。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

EQUIP_CAT = "chem_equipment"

# 流程图（化工/给水）的信号词
PFD_WORDS = r"流程|工艺|PFD|P&ID|PID|管道及仪表|带控制点|给水处理|净水厂|水厂|高程布置|换热|精馏|反应釜|反应器|储罐|储槽|储坑|离心泵|压缩机|蒸发器|冷凝|再沸器|填料塔|缓冲罐|管线|管道|位号|污水|废水|焚烧|垃圾|烟气|锅炉|除尘|风机|烟囱|沉淀|过滤"
# “N 块塔板 / 块数”里的“块”是量词，不是块定义
_BLOCK = r"(?<![\d一二两三四五六七八九十百几多])(?<!\d\s)块(?!数|塔板|塔盘|塔)"
# 通用 CAD 的信号词
GENERAL_WORDS = (r"矩形|圆弧|直线|多段线|样条|椭圆|偏移|倒角|圆角|阵列|镜像|修剪|延伸|图层|线型|正交|捕捉|栅格|极轴|图形界限|线型比例|标注|尺寸|公差|粗糙度|基准|填充|剖面线|布局|视口|拉伸|旋转成|等轴测|" + _BLOCK +
                 r"|属性块|约束|同心|平行度|中心线|机械图|建筑图|轴网|螺栓|法兰|齿轮|键槽|图框|DXF|命令行|撤销|重做|全部显示|缩放")
CMD_START = re.compile(r"^\s*(?:L|C|A|REC|POL|PL|O|F|CHA|TR|EX|M|CO|RO|SC|MI|ARR|ARP|E|X|LA|LTS|Z|U|REDO|ORTHO|SNAP|GRID|POLAR|OSNAP|DYN|LIMITS|LIST|EXT|VIEW|NEW|SAVE|HELP|H|T|DLI|DAL|DRA|DDI|DAN)(?:\s|$)", re.I)

# ---------------------------------------------------------------- 化工设备图词表（任务书 6.2-1）
# 图种词：“流程设备图”“设备图例/设备图形”不算
EQ_KIND = r"(?<!流程)(?:设备图|设备总图|设备装配图)(?!例|形)|管口方位图?|管口表|技术特性表|设计数据表"
# 设备名（容器类）；单字的 罐/釜/塔 兜底，塔盘/塔板/塔顶这类不算设备名
_NAMES = (r"储罐|贮罐|储槽|贮槽|储存罐|缓冲罐|回流罐|分液罐|计量罐|中间罐|容器|反应釜|反应器|反应罐|搅拌釜|聚合釜|换热器|冷凝器|再沸器|蒸发器|冷却器|加热器|分离器|汽包|"
          r"精馏塔|吸收塔|解吸塔|萃取塔|干燥塔|板式塔|填料塔|塔器|高位槽|沉降槽")
EQ_NAME = _NAMES + r"|罐|釜|塔(?![盘板顶底吊])"
# “装配图/总图/明细栏”只有同一句里出现容器类设备名才算图种词（否则“减速器装配图”会被抢走）
EQ_ASSY = r"装配图|总图|明细栏"
# 结构词
EQ_STRUCT = (r"封头|筒体|切线长|鞍座|耳座|耳式支座|支腿|裙座|人孔|手孔|补强圈|管板|折流板|换热管|塔盘|板间距|夹套|搅拌器|壁厚|名义厚度|设计压力|设计温度|腐蚀裕量|"
             r"内径|外径|直径|筒体高|DN\s*\d+|PN\s*\d+")
# 流程词
EQ_FLOW = r"流程|工艺|物料|管道|管线|接到|连到|送到|送往|泵|阀|PFD|P&ID|带控制点|管道及仪表"
# 明确的流程图图种词
PFD_CHART = r"流程图|工艺流程|PFD|P&ID|带控制点|管道及仪表"
# “把流程图里的 V-101 生成设备图”：流程图在这里只是来源，不是要画的图
_PFD_SOURCE = re.compile(r"(?:流程图|PFD|P&ID)\s*(?:里面?|中|上)(?:的)?", re.I)
_SENT = re.compile(r"[。；;！!？?\n]+")
_TAG = re.compile(r"(?<![A-Za-z0-9])(?!DN|PN)([A-Z]{1,3})-\d{2,4}(?![A-Za-z0-9])")
_DEVICE_TAG = set("VRTEDH")                    # 位号首字母属于容器 / 塔 / 换热器（P 泵、F 炉不算）
_CREATE = re.compile(r"画|绘|新建|创建|生成|设计|做[一个]|来[一个]|出[一张]")
_EDIT = re.compile(r"删除|删掉|移动|[左右上下]移|平移|挪|改成|改为|修改|调整|替换|连接|复制|旋转|缩放|放大|缩小|对齐|标注|命名|编号")     # 改已有设备的话，不是“新画一台”
_STOP_PREFIX = set("的一二两三四五六七八九十个台座只式型立卧画绘制把将和与及、，, 给在")

_KIND_RX = re.compile(EQ_KIND)
_NAME_RX = re.compile(EQ_NAME)
_ASSY_RX = re.compile(EQ_ASSY)
_STRUCT_RX = re.compile(EQ_STRUCT, re.I)
_FLOW_RX = re.compile(EQ_FLOW, re.I)
_CHART_RX = re.compile(PFD_CHART, re.I)

# 工具子集用的类别：设备图词表中“单独出现就足以说明是设备图”的那部分（内径/外径/直径/DN/PN 太通用，不进类别规则）
_CAT_STRUCT = (r"封头|筒体|切线长|鞍座|耳座|耳式支座|支腿|裙座|人孔|手孔|补强圈|管板|折流板|换热管|塔盘|板间距|夹套|搅拌器|壁厚|名义厚度|设计压力|设计温度|腐蚀裕量|筒体高")
_CAT_EQUIP = EQ_KIND + r"|(?:" + _NAMES + r")[^。；;！!？?\n]{0,12}?(?:" + EQ_ASSY + r")|" + _CAT_STRUCT

CATEGORY_RULES: list[tuple[str, str]] = [
    ("environment_setup", r"新建.{0,6}图|背景|图层|线型比例|图形界限|正交|栅格|捕捉|极轴|对象追踪|动态输入|工作空间|(?:二维|三维|建模|草图|绘图|注释)\S{0,3}空间|切换.{0,4}空间|保存|另存|国标|线宽|图纸"),
    ("2d_drafting", r"画|绘制|矩形|圆|直线|射线|构造线|多段线|样条|椭圆|多边形|网格|填充|剖面线|点"),
    ("precision_snap", r"坐标|圆心|中点|端点|交点|切点|垂足|象限点|几何中心|捕捉点"),
    ("view_select", r"放大|缩小|全部显示|范围缩放|平移|缩放|选中|选择|框选|视图"),
    ("2d_editing", r"偏移|复制|镜像|阵列|移动|旋转|缩放比例|比例缩放|拉伸|拉长|修剪|剪掉|剪去|剪短|延伸|打断|倒角|圆角|分解|删除|删掉|夹点|拖到|拖动|拖拽|拉到|端点拖|特性|改到|改成|格式刷|刷成|刷一下|匹配|对齐"),
    ("annotation_dimension", r"标注|尺寸|文字|注释|直径标|半径标|角度标|文字样式|标注样式"),
    ("layout_plotting", r"布局|视口|图纸空间|输出|打印|PDF|出图|图框|比例尺|模型空间"),
    # “固定管板 / 固定鞍座 / 固定端”不是约束；“20 块塔板 / 块数”的块不是块定义
    ("block_xref_constraint", _BLOCK + r"|属性|动态块|外部参照|参照|约束|同心|平行约束|垂直约束|相切约束|固定(?!管板|鞍座|鞍|端|支座|头)"),
    ("3d_modeling", r"拉伸成|拉伸|实体|旋转成|回转|三维|等轴测|西南|东南|视觉样式|着色|消隐|布尔|差集|并集"),
    ("engineering_specialty", r"公差|粗糙度|形位|基准|平行度|垂直度|同轴度|位置度|H\d|g\d|螺栓|螺母|垫圈|法兰|齿轮|键槽|轴承|轴网|门窗|明细表|序号球|符号库|焊接符号|工具集"),
    (EQUIP_CAT, _CAT_EQUIP),
]


@dataclass
class Route:
    mode: str                                   # general / pfd / equipment
    categories: list[str] = field(default_factory=list)
    confidence: float = 0.5
    reason: str = ""
    ask_kind: bool = False                      # 只有设备名、分不清要流程图还是设备图：设备图 Planner 一次性同时问图种和必问项


def categories_of(text: str, max_n: int = 3) -> list[str]:
    hits: list[tuple[int, str]] = []
    for cat, rx in CATEGORY_RULES:
        n = len(re.findall(rx, text))
        if n:
            hits.append((n, cat))
    hits.sort(key=lambda t: -t[0])
    cats = [c for _, c in hits[:max_n]]
    if any(c == EQUIP_CAT for _, c in hits) and EQUIP_CAT not in cats:
        cats = cats[:max_n - 1] + [EQUIP_CAT]               # 设备图词表命中的话，别让它被别的类别挤掉（拿不到 vessel_* 函数就画不了）
    if CMD_START.match(text) and "2d_drafting" not in cats:
        cats.append("2d_drafting")
    return cats


# ---------------------------------------------------------------- 设备图信号
@dataclass
class _Sig:
    kind_word: bool = False          # 图种词
    has_device: bool = False         # 有设备名（或容器类位号）
    n_devices: int = 0               # 全句出现几台不同设备
    n_struct: int = 0                # 结构词个数
    flow: bool = False               # 有流程词，或同一句出现两台及以上不同设备
    pfd_chart: bool = False          # 明确要流程图（流程图 / 工艺流程 / PFD / P&ID / 带控制点）


def _device_names(sentence: str) -> set[str]:
    """一句话里的设备名（不同的名字各算一台）；单字的罐/釜/塔向左带上最多两个字（原料罐 ≠ 产品罐）。"""
    out: set[str] = set()
    for m in _NAME_RX.finditer(sentence):
        w = m.group(0)
        if len(w) == 1:
            i = m.start()
            while i > 0 and m.start() - i < 2 and "一" <= sentence[i - 1] <= "鿿" and sentence[i - 1] not in _STOP_PREFIX:
                i -= 1
            w = sentence[i:m.end()]
        out.add(w)
    return out


def _normalized(text: str) -> str:
    """路由前先做口述规范化（设备途→设备图、桶体→筒体、二十→20…），入口直接拿到语音识别原文也能判断。"""
    t = (text or "").strip()
    try:
        from zhihui.agent.speech_normalize import normalize
        return normalize(t).text
    except Exception:
        return t


def _signals(p: str) -> _Sig:
    sig = _Sig()
    names: set[str] = set()
    tags: set[str] = set()
    assy = False
    for s in (x for x in _SENT.split(p) if x.strip()):
        nm, tg = _device_names(s), {m.group(0) for m in _TAG.finditer(s)}
        names |= nm
        tags |= tg
        if max(len(nm), len(tg)) >= 2:
            sig.flow = True                                   # 同一句出现两台及以上不同设备：这是在描述流程
        if nm and _ASSY_RX.search(s):
            assy = True
    dev_tags = {t for t in tags if t[0] in _DEVICE_TAG}
    sig.kind_word = bool(_KIND_RX.search(p)) or assy
    sig.has_device = bool(names or dev_tags)
    sig.n_devices = max(len(names), len(tags))
    sig.n_struct = len(_STRUCT_RX.findall(p))
    sig.flow = sig.flow or bool(_FLOW_RX.search(p))
    sig.pfd_chart = bool(_CHART_RX.search(_PFD_SOURCE.sub(" ", p)))
    return sig


def _only_name(sig: _Sig, gen: int, text: str) -> bool:
    """只提到了一台设备（没有图种词、至多一个结构词、没有流程词和通用 CAD 词、不是改已有设备）：分不清要流程图还是设备图。"""
    return sig.has_device and sig.n_devices == 1 and not sig.kind_word and sig.n_struct <= 1 and not sig.flow and gen == 0 and not _EDIT.search(text)


def _eq_cats(cats: list[str]) -> list[str]:
    return [EQUIP_CAT] + [c for c in cats if c != EQUIP_CAT][:2]


def route(prompt: str, drawing_mode: str | None = None, has_dxf: bool = False, has_vessel: bool = False) -> Route:
    raw = prompt.strip()
    p = raw
    pn = _normalized(raw)
    sig = _signals(pn)
    pfd = len(re.findall(PFD_WORDS, p, re.I))
    gen = len(re.findall(GENERAL_WORDS, p)) + (2 if CMD_START.match(p) else 0)
    cats = categories_of(p)
    gen_w = max(0, gen - p.count("尺寸"))                       # “不给尺寸 / 尺寸待定”不算通用绘图指令（只用于“只有设备名”的判断）
    # ---- 当前就是设备图：只有明确的流程图图种词才建议新建流程图，设备名、“管道/换热”这类词不计分
    if has_vessel or drawing_mode == "equipment":
        if sig.pfd_chart:
            return Route("pfd", [], 0.7, "当前图纸是设备图，但这句话明确要流程图：请新建流程图（不会改动当前图）")
        return Route("equipment", _eq_cats(categories_of(pn)), 0.9, "当前图纸是设备图", ask_kind=(not has_vessel) and _only_name(sig, gen_w, pn))
    # ---- 设备图的强信号：图种词；或设备名 + ≥2 个结构词且没有流程词
    if sig.kind_word:
        return Route("equipment", _eq_cats(categories_of(pn)), 0.95, "命中设备图的图种词")
    if sig.has_device and sig.n_struct >= 2 and not sig.flow:
        return Route("equipment", _eq_cats(categories_of(pn)), 0.8, "设备名 + 结构词（封头、筒体、壁厚……）：按化工设备图处理")
    # ---- 只有一台设备名：分不清要流程图还是设备图，交给设备图 Planner 一次性同时问图种和必问项（路由层自己不反问）
    if _only_name(sig, gen_w, pn) and drawing_mode != "pfd" and not has_dxf and (drawing_mode is None or _CREATE.search(pn)):
        return Route("equipment", _eq_cats(categories_of(pn)), 0.55, "只提到了一台设备：先问清是要工艺流程图还是化工设备图", ask_kind=True)
    # ---- 以下是原来的流程图 / 通用绘图判断
    if drawing_mode == "general":
        if pfd >= 2 and pfd > gen:
            return Route("pfd", [], 0.6, "图纸是通用图，但这句话像是在要流程图：请新建流程图（不会改动当前图）")
        return Route("general", cats, 0.9, "当前图纸是通用图")
    if drawing_mode == "pfd":
        if gen >= 3 and pfd == 0:
            return Route("general", cats, 0.55, "当前是流程图，但这句话是通用绘图指令")
        return Route("pfd", [], 0.9, "当前图纸是流程图")
    if has_dxf:
        return Route("pfd", [], 0.5, "上传了 DXF：先按流程图改图流程处理")
    if pfd == 0 and gen == 0:
        return Route("pfd", [], 0.3, "没有明显特征，沿用流程图流程")
    if gen > pfd:
        return Route("general", cats, min(0.95, 0.5 + 0.1 * (gen - pfd)), "通用绘图关键词")
    return Route("pfd", [], min(0.95, 0.5 + 0.1 * (pfd - gen)), "流程图关键词")


def entry_decision(prompt: str, drawing=None, has_dxf: bool = False) -> Route:
    """入口路由（/api/route、create_task、/chat、基准共用）：drawing 是 zhihui.ir.Drawing 或 None。
    有设备参数（drawing.vessel）或工作空间是化工设备图时按设备图处理，否则按图纸自己的 mode。"""
    mode, has_vessel = None, False
    if drawing is not None:
        has_vessel = getattr(drawing, "vessel", None) is not None
        if has_vessel or getattr(drawing, "workspace", None) == EQUIP_CAT:
            mode = "equipment"
        else:
            mode = getattr(drawing, "mode", None)
    return route(prompt, mode, has_dxf, has_vessel)
