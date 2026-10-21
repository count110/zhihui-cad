"""离线规则 Planner（--mock / 评测的 scripted 之外的离线路径）：把规范化后的中文口令转成计划 JSON。

做法：按标点切成小句 → 每个小句匹配一条「主规则」（新建一个意图）或「修饰规则」（给上一个意图补参数）→ 编译成步骤。
· 图元指代一律生成「识别步骤」，由 Recognizer 解析，规则 Planner 不碰几何；
· 缺关键数值（半径、长宽、高度…）就反问，不编造；
· 相邻、不需要看识别结果的工具步骤合并成一个 cad_batch_execute（一个版本、失败整体回滚）。
覆盖：规格第 10 节的 14 条演示口令及其常见变体，以及 AutoCAD 命令（交给 command_line）。写不进规则的说法，真实模式下交给大模型 Planner。
"""
from __future__ import annotations

import re
from typing import Any

from zhihui.agent import command_line as CL

from .plan import Plan

N = r"(-?\d+(?:\.\d+)?)"
PT = r"(@?-?\d+(?:\.\d+)?,-?\d+(?:\.\d+)?(?:,-?\d+(?:\.\d+)?)?|@?-?\d+(?:\.\d+)?<-?\d+(?:\.\d+)?|原点)"
DIRS = {"右": "right", "左": "left", "上": "up", "下": "down"}
COLOR_NAMES = {"黑": "#000000", "白": "#FFFFFF", "灰": "#808080", "红": 1, "黄": 2, "绿": 3, "青": 4, "蓝": 5}
GDT = {"平行度": "parallelism", "垂直度": "perpendicularity", "平面度": "flatness", "直线度": "straightness", "圆度": "circularity", "圆柱度": "cylindricity", "倾斜度": "angularity",
       "位置度": "position", "同轴度": "concentricity", "对称度": "symmetry", "圆跳动": "circular_runout", "全跳动": "total_runout"}
NON_BATCH = {"manage_document_file", "history_undo_redo", "cad_batch_execute", "configure_plot_and_export", "invoke_engineering_toolset"}


def P(s: str) -> str:
    return "0,0" if s == "原点" else s


class Ask(Exception):
    def __init__(self, question: str, options: list[str] | None = None):
        self.question, self.options = question, options or []


class Builder:
    def __init__(self, ctx: dict, utterance: str):
        self.ctx = ctx
        self.utterance = utterance
        self.steps: list[dict] = []          # {"kind","key","tool","args","query","need","summary","barrier"}
        self.pending: dict | None = None     # 还没落成步骤的意图（后面的小句还能补参数）
        self.last: dict = {}                 # 已落成的意图的关键步骤（供修饰语引用）
        self.notes: list[str] = []
        self.categories: list[str] = []
        self._n = 0
        self._recog_cache: dict[tuple, str] = {}
        self.layout_name: str | None = None
        self.dims: dict[str, str] = {}       # 标注名（长/宽/直径/半径）→ 步骤 key，修饰语（加公差）按名字找
        self.last_dim: str | None = None

    # ---- 步骤 ----
    def key(self, p: str) -> str:
        self._n += 1
        return f"{p}{self._n}"

    def tool(self, summary: str, tool: str, args: dict, barrier: bool = False) -> str:
        k = self.key("t")
        self.steps.append({"kind": "tool", "key": k, "tool": tool, "args": args, "summary": summary, "barrier": barrier})
        return k

    def recog(self, summary: str, query: str, need: list[str] | None = None, expect: dict | None = None) -> str:
        sig = (query, tuple(sorted(need or [])))
        if sig in self._recog_cache:
            return self._recog_cache[sig]
        k = self.key("r")
        self.steps.append({"kind": "recog", "key": k, "query": query, "need": need or ["entity_ids"], "expect": expect or {}, "summary": summary})
        self._recog_cache[sig] = k
        return k

    def perception(self, summary: str, tool: str, args: dict) -> str:
        k = self.key("r")
        self.steps.append({"kind": "recog", "key": k, "tool": tool, "args": args, "summary": summary})
        return k

    def flush(self) -> None:
        if self.pending is not None:
            p, self.pending = self.pending, None
            FLUSH[p["op"]](self, p)


# ------------------------------------------------------------------ 引用表达式
def E(key: str, path: str = "") -> str:
    return f"$@{key}{path}"


def ent(key: str, i: int = 0) -> str:
    return E(key, f".entity_ids[{i}]")


def pnt(key: str) -> str:
    return E(key, ".points.primary")


# ------------------------------------------------------------------ 主规则
RULES: list[tuple[re.Pattern, Any, bool]] = []
MODS: list[tuple[re.Pattern, Any]] = []


def rule(pattern: str, search: bool = False):
    def deco(fn):
        RULES.append((re.compile(pattern), fn, search))
        return fn
    return deco


def mod(pattern: str):
    def deco(fn):
        MODS.append((re.compile(pattern), fn))
        return fn
    return deco


def ref_query(b: Builder, phrase: str, need=None, summary: str | None = None) -> str:
    phrase = phrase.strip(" 的")
    b.last_ref = phrase                                          # 后面的小句（“颜色改成红色”）没有对象时沿用
    return b.recog(summary or f"识别「{phrase}」", phrase, need)


def target_phrase(m, group: str = "ref", default: str = "刚才那个") -> str:
    t = (m.groupdict().get(group) or "").strip()
    return t or default


# ---- 环境 -------------------------------------------------------
@rule(r"^新建(?:一张|一个|一份)?\s*(?:(A[0-4])\s*)?(机械|建筑|通用|空白|ISO)?(?:图纸|图(?!层)|文件|文档)")
def r_new(b, m, c):
    size = (m.group(1) or "A3").upper()
    kind = m.group(2) or "通用"
    n = size[1]
    tpl = {"机械": f"gb_a{n}_mech", "建筑": "gb_a3_arch", "ISO": "iso_a3", "通用": f"blank_a{n}", "空白": f"blank_a{n}"}[kind]
    from zhihui.cad.geom.presets import TEMPLATES
    if tpl not in TEMPLATES:
        b.notes.append(f"没有 {size} {kind}样板，改用空白 {size} 图纸")
        tpl = f"blank_a{n}"
    b.flush()
    b.tool(f"新建{size}{kind}图", "manage_document_file", {"action": "new", "template_name": tpl}, barrier=True)
    b.categories.append("environment_setup")


@rule(r"^背景(?:色)?(?:改成|改为|设为|设置为|换成|调成|变成)?(黑|白|灰|红|黄|绿|青|蓝)色?$")
def r_bg(b, m, c):
    b.flush()
    b.tool("设置背景色", "configure_canvas_and_limits", {"bg_color": COLOR_NAMES[m.group(1)]})


@rule(r"^(?:建|创建|新建|建立|加载)(?:一套)?(?:一套)?(国标|ISO|国际标准)?(机械|建筑)?(?:标准)?图层$|^(?:建|创建)(?:一套)?(国标机械|国标建筑|标准)图层")
def r_layers(b, m, c):
    b.flush()
    g = c
    preset = "GB_MECH"
    if "建筑" in g:
        preset = "GB_ARCH"
    elif "ISO" in g or "国际" in g:
        preset = "ISO"
    b.tool(f"建一套 {preset} 标准图层", "manage_layers", {"action": "create_standard_preset", "preset_standard": preset})


@rule(r"^(?:当前层|当前图层)(?:设为|设置为|切换到|改为|设成)\s*[「\"“]?(\S+?)(?:图层|层)?[」\"”]?$|^(?:把|将)?(\S+?)(?:图层|层)(?:设为|设置为|置为)当前(?:层|图层)?$|^(?:切换到|切到)(\S+?)(?:图层|层)$")
def r_cur_layer(b, m, c):
    name = next(g for g in m.groups() if g)
    b.flush()
    b.tool(f"当前图层设为 {name}", "set_layer_state", {"layer_name": name, "set_current": True})


@rule(r"^(?:新建|创建|新增|添加|建立|建|加)(?:一个|一层|个)?(?:新的?)?图层(?:叫做?|名为|名叫|命名为|名称是|名称为)?\s*[「\"“]?([^\s，,「」\"”]+?)[」\"”]?(?:图层)?(?:[，,\s]+(?:颜色|用)?(红|黄|绿|青|蓝|白|洋红)色?)?(?:[，,\s]*线型(?:为|是|设为)?\s*(\S+))?$")
def r_new_layer(b, m, c):
    b.flush()
    name, col, lt = m.group(1), m.group(2), m.group(3)
    b.tool(f"新建图层 {name}", "manage_layers", {"action": "create", "layer_name": name})
    props: dict = {}
    if col:
        props["color"] = COLOR_NAMES[col]
    if lt:
        props["linetype"] = {"虚线": "DASHED", "点画线": "CENTER", "中心线": "CENTER", "实线": "Continuous"}.get(lt, lt.upper())
    if props:
        b.tool(f"设置图层 {name} 属性", "set_layer_properties", {"layer_name": name, **props})


@rule(r"^(关闭|关掉|打开|开启|冻结|解冻|锁定|解锁)\s*[「\"“]?([^\s，,「」\"”]+?)(?:图层|层)[」\"”]?$")
def r_layer_state(b, m, c):
    b.flush()
    key = {"关闭": ("is_on", False), "关掉": ("is_on", False), "打开": ("is_on", True), "开启": ("is_on", True), "冻结": ("is_frozen", True), "解冻": ("is_frozen", False), "锁定": ("is_locked", True), "解锁": ("is_locked", False)}[m.group(1)]
    b.tool(f"{m.group(1)}图层 {m.group(2)}", "set_layer_state", {"layer_name": m.group(2), key[0]: key[1]})


@rule(r"^(\S+?)(?:图层|层)(颜色|线型|线宽)(?:设为|改为|改成|设置为)\s*(\S+?)$")
def r_layer_prop(b, m, c):
    b.flush()
    name, what, val = m.groups()
    if what == "颜色":
        v: Any = COLOR_NAMES.get(val.rstrip("色"), val)
        args = {"color": v}
    elif what == "线型":
        args = {"linetype": {"虚线": "DASHED", "点画线": "CENTER", "中心线": "CENTER", "实线": "Continuous"}.get(val, val.upper())}
    else:
        args = {"lineweight_mm": float(re.sub(r"[^\d.]", "", val))}
    b.tool(f"设置图层 {name} {what}", "set_layer_properties", {"layer_name": name, **args})


@rule(r"^图形界限(?:设为|设置为|改为)?\s*" + PT + r"\s*(?:到|至|-|和)\s*" + PT + r"$")
def r_limits(b, m, c):
    b.flush()
    b.tool("设置图形界限", "configure_canvas_and_limits", {"limits_lower_left": P(m.group(1)), "limits_upper_right": P(m.group(2)), "zoom_all": True})


@rule(r"线型比例|虚线.*(?:显示成实线|变成实线|看着像实线|不显示|不正常|看不出)|(?:点画线|中心线).*(?:显示成实线|看不出)", search=True)
def r_ltscale(b, m, c):
    b.flush()
    if b.steps and b.steps[-1].get("tool") == "set_linetype_scale":
        return                                              # 同一句话里说了两遍，只调一次
    mm = re.search(N, re.sub(r"[A-Za-z]\d+", "", c))
    if re.search(r"线型比例.*" + N, c) and mm:
        b.tool(f"线型比例设为 {mm.group(1)}", "set_linetype_scale", {"ltscale": float(mm.group(1))})
    else:
        b.tool("线型比例按图形界限自动计算", "set_linetype_scale", {"auto": True})
        b.notes.append("线型比例没给数值，按图形界限自动计算推荐值")


@rule(r"^(?:切换到|进入|切到)(二维草图|二维|注释|三维建模|三维)(?:空间|工作空间)?$")
def r_workspace(b, m, c):
    b.flush()
    ws = {"二维草图": "2d_drafting", "二维": "2d_drafting", "注释": "annotation", "三维建模": "3d_modeling", "三维": "3d_modeling"}[m.group(1)]
    b.tool(f"切换到 {ws} 工作空间", "manage_workspace_ui", {"workspace": ws})


@rule(r"(?:界面|工具栏|命令行|状态栏|图层面板|特性面板).{0,6}(?:怎么用|是什么|布局|介绍|说明)", search=True)
def r_ui_help(b, m, c):
    b.flush()
    b.tool("说明界面布局", "manage_workspace_ui", {"explain_ui_layout": True})


@rule(r"^(?:保存|另存为|导出成?)(?:为)?\s*(DXF|DWG|JSON|STL)(?:文件)?$|^保存(?:图纸|文件)?$")
def r_save(b, m, c):
    b.flush()
    fmt = (m.group(1) or "DXF").lower()
    b.tool(f"保存为 {fmt.upper()}", "manage_document_file", {"action": "save", "format": fmt}, barrier=True)


# ---- 绘图 -------------------------------------------------------
@rule(r"^从\s*" + PT + r"\s*(?:出发)?\s*(?:向|往)(右|左|上|下)(?:画|走|延伸)?\s*" + N + r"(?:\s*(?:mm|毫米))?$")
def r_path_start(b, m, c):
    b.flush()
    b.pending = {"op": "path", "points": [P(m.group(1)), f"{DIRS[m.group(2)]} {m.group(3)}"], "close": False}
    b.categories.append("2d_drafting")


@rule(r"^(?:画|绘制)(?:一条)?(?:直线|线)\s*(?:从)?\s*" + PT + r"\s*(?:到|至|，|,|\s)\s*" + PT + r"$")
def r_line2(b, m, c):
    b.flush()
    b.pending = {"op": "path", "points": [P(m.group(1)), P(m.group(2))], "close": False}
    b.categories.append("2d_drafting")


@rule(r"^从\s*" + PT + r"\s*(?:开始)?(?:画|连|连线|画线|画直线)到\s*" + PT + r"((?:\s*(?:再|然后)?(?:画|连|连线)?到\s*" + PT + r")*)$")
def r_path_chain(b, m, c):
    b.flush()
    rest = [P(x) for x in re.findall(PT, m.group(3) or "")]
    b.pending = {"op": "path", "points": [P(m.group(1)), P(m.group(2))] + rest, "close": False}
    b.categories.append("2d_drafting")


@rule(r"^在\s*" + PT + r"\s*(?:处|点|这里)?(?:画|绘制)(?:一个|个)?(?:(半径|直径)(?:为|是)?\s*" + N + r"\s*(?:mm|毫米)?\s*的?)?圆$")
def r_circle_at_pt(b, m, c):
    b.flush()
    p = {"op": "circle", "center": P(m.group(1))}
    if m.group(2):
        p["rd"], p["v"] = ("radius" if m.group(2) == "半径" else "diameter"), float(m.group(3))
    b.pending = p
    b.categories.append("2d_drafting")


@rule(r"^(?:画|绘制)(?:一个|一张|个)?(?:矩形|长方形|正方形)$")
def r_rect(b, m, c):
    b.flush()
    b.pending = {"op": "rect", "square": "正方形" in c}
    b.categories.append("2d_drafting")


@rule(r"^(?:画|绘制)(?:一个|一张|个)?(?:矩形|长方形)\s*(?:，|,)?(?:左下角|起点|角点)(?:在|为|于)?\s*" + PT + r"\s*(?:，|,)?\s*长\s*" + N + r"\s*(?:mm)?\s*(?:，|,)?宽\s*" + N)
def r_rect_full(b, m, c):
    b.flush()
    b.pending = {"op": "rect", "corner1": P(m.group(1)), "w": float(m.group(2)), "h": float(m.group(3))}
    b.categories.append("2d_drafting")


@rule(r"^(?:画|绘制)(?:一个|个)?正\s*(\d+)\s*边形$")
def r_polygon(b, m, c):
    b.flush()
    b.pending = {"op": "polygon", "n": int(m.group(1))}
    b.categories.append("2d_drafting")


@rule(r"^(?:画|绘制)(?:一个|一些|个)?(?:(半径|直径)(?:为|是)?\s*" + N + r"\s*(?:mm|毫米)?\s*的?)?圆$")
def r_circle(b, m, c):
    b.flush()
    p = {"op": "circle"}
    if m.group(1):
        p["rd"], p["v"] = ("radius" if m.group(1) == "半径" else "diameter"), float(m.group(2))
    b.pending = p
    b.categories.append("2d_drafting")


@rule(r"^在(?P<ref>.*?)(?P<feat>中心|圆心|中点|几何中心|左下角|右下角|左上角|右上角)(?:处|上|位置)?(?:画|绘制)(?:一个)?(?:(?P<rd>半径|直径)(?:为|是)?\s*" + N.replace("(", "(?P<v>", 1) + r"\s*(?:mm)?\s*的?)?圆$")
def r_circle_at(b, m, c):
    b.flush()
    d = m.groupdict()
    p = {"op": "circle", "center_ref": (d["ref"] or _default_target(b)) + d["feat"]}          # “在中心画一个圆”：没说哪个图元就取图里最主要的（矩形/刚画的），并在说明里告知
    if d.get("rd"):
        p["rd"], p["v"] = ("radius" if d["rd"] == "半径" else "diameter"), float(d["v"])
    b.pending = p
    b.categories += ["2d_drafting", "precision_snap"]


@rule(r"^(?:画|绘制)(?:一个)?(?:填充|剖面线)$|^(?:给|为|对)(?P<ref>.+?)(?:加|画|填充|做)(?:上)?(?P<pat>金属|混凝土|砖|实心)?(?:的)?(?:剖面线|填充)$")
def r_hatch(b, m, c):
    b.flush()
    ref = (m.groupdict().get("ref") or "").strip()
    pat = {"金属": "ANSI31", "混凝土": "AR-CONC", "砖": "ANSI37", "实心": "SOLID"}.get(m.groupdict().get("pat") or "", "ANSI31")
    if not ref:
        raise Ask("要给哪个封闭区域填充？可以指明图元（如“矩形”）或边界内的一个点。", ["矩形", "给个点 50,50"])
    r = b.recog(f"识别「{ref}」的边界", ref, ["entity_ids", "boundary"])
    b.tool("填充", "create_hatch", {"boundary_mode": "entity_ids", "entity_ids": [E(r, ".boundary.outer"), E(r, ".boundary.islands")], "pattern_name": pat, "island_style": "normal"})
    b.categories.append("2d_drafting")


@rule(r"^在\s*" + PT + r"\s*(?:处)?(?:写|加|标注|添加)(?:一行|一段)?文字\s*[「\"“]?(.+?)[」\"”]?(?:，|,)?(?:字高\s*" + N + r")?$")
def r_text(b, m, c):
    b.flush()
    args = {"text_type": "single_line", "content": m.group(2), "insert_point": P(m.group(1))}
    if m.group(3):
        args["height"] = float(m.group(3))
    else:
        b.notes.append("文字高度用默认值 3.5")
    b.tool("写文字", "create_text_annotation", args)
    b.categories.append("annotation_dimension")


# ---- 编辑 -------------------------------------------------------
@rule(r"^(?:把|将)?(?P<ref>.+?)(?:向|往)(?P<side>外|内|左|右)(?:侧)?(?:偏移|偏置)\s*" + N.replace("(", "(?P<d>", 1) + r"(?:\s*(?:mm|毫米))?$")
def r_offset(b, m, c):
    b.flush()
    d = m.groupdict()
    r = ref_query(b, d["ref"])
    b.tool(f"偏移 {d['d']}", "replicate_entities", {"operation": "offset", "entity_ids": [ent(r)], "offset_distance": float(d["d"]), "offset_side": {"外": "outside", "内": "inside", "左": "left", "右": "right"}[d["side"]]})
    b.categories.append("2d_editing")


@rule(r"^(?:把|将)?(?P<ref>.*?)(?:的)?(?:[四4]个角|[四4]角|所有角|各个角|所有拐角|各角)?\s*倒(?:个)?\s*(?:R|r|半径)?\s*" + N.replace("(", "(?P<r>", 1) + r"\s*(?:的)?圆角$|^(?:把|将)?(?P<ref2>.*?)(?:的)?(?:[四4]个角|[四4]角|所有角|各个角)?\s*(?:圆角|倒圆角)\s*(?:R|r|半径)?\s*" + N.replace("(", "(?P<r2>", 1) + r"$")
def r_fillet(b, m, c):
    b.flush()
    d = m.groupdict()
    ref = (d.get("ref") or d.get("ref2") or "").strip(" 的")
    ref = re.sub(r"([四4]个角|[四4]角|所有角|各个角)$", "", ref).strip(" 的") or "刚才那个"
    r = ref_query(b, ref)
    rad = float(d.get("r") or d.get("r2"))
    b.tool(f"圆角 R{rad:g}", "modify_corners_and_breaks", {"operation": "fillet", "entity_id_1": ent(r), "fillet_radius": rad, "apply_to_entire_polyline": True})
    b.categories.append("2d_editing")


@rule(r"^(?:把|将)?(?P<ref>.*?)(?:的)?(?:[四4]个角|[四4]角|所有角)?\s*倒角\s*" + N.replace("(", "(?P<d>", 1) + r"$")
def r_chamfer(b, m, c):
    b.flush()
    d = m.groupdict()
    ref = re.sub(r"([四4]个角|[四4]角|所有角)$", "", (d.get("ref") or "").strip(" 的")).strip(" 的") or "刚才那个"
    r = ref_query(b, ref)
    b.tool(f"倒角 {d['d']}", "modify_corners_and_breaks", {"operation": "chamfer", "entity_id_1": ent(r), "chamfer_dist1": float(d["d"]), "chamfer_dist2": float(d["d"]), "apply_to_entire_polyline": True})
    b.categories.append("2d_editing")


@rule(r"^以(?P<ref>.+?)(?:的)?(?:圆心|中心)为(?:中心|圆心)(?:做|画|进行)?(?:环形|极轴)阵列\s*(?P<n>\d+)\s*个(?:(?P<rd>半径|直径)\s*" + N.replace("(", "(?P<v>", 1) + r"\s*的)?(?:小)?圆?$")
def r_polar_new(b, m, c):
    b.flush()
    d = m.groupdict()
    b.pending = {"op": "polar_new", "ref": d["ref"] + "圆心", "n": int(d["n"]), "item": ({"rd": "radius" if d["rd"] == "半径" else "diameter", "v": float(d["v"])} if d.get("rd") else None), "pitch_d": None}
    b.categories.append("2d_editing")


@rule(r"^(?:把|将)?(?P<ref>.+?)(?:做|进行|画)?(?:环形|极轴)阵列\s*(?P<n>\d+)\s*个(?:，|,)?(?:以\s*(?P<c>" + PT[1:-1] + r")\s*为中心)?$")
def r_polar_arr(b, m, c):
    b.flush()
    d = m.groupdict()
    r = ref_query(b, d["ref"])
    if d.get("c"):
        center = P(d["c"])
    else:
        raise Ask("环形阵列的中心点在哪里？", ["圆心 0,0", "以某个圆的圆心为中心（请说明是哪个圆）"])
    b.tool(f"环形阵列 {d['n']} 个", "replicate_entities", {"operation": "array_polar", "entity_ids": [ent(r)], "array_params": {"center": center, "count": int(d["n"]), "fill_angle_deg": 360, "rotate_items": True}})
    b.categories.append("2d_editing")


@rule(r"^(?:把|将)?(?P<ref>.+?)(?:做|进行)?矩形阵列\s*(?P<r>\d+)\s*行\s*(?P<c>\d+)\s*列(?:，|,)?行距\s*" + N.replace("(", "(?P<rs>", 1) + r"(?:，|,)?列距\s*" + N.replace("(", "(?P<cs>", 1) + r"$")
def r_rect_arr(b, m, c):
    b.flush()
    d = m.groupdict()
    r = ref_query(b, d["ref"])
    b.tool(f"{d['r']}×{d['c']} 矩形阵列", "replicate_entities", {"operation": "array_rect", "entity_ids": [ent(r)], "array_params": {"rows": int(d["r"]), "cols": int(d["c"]), "row_spacing": float(d["rs"]), "col_spacing": float(d["cs"])}})
    b.categories.append("2d_editing")


@rule(r"^(?:把|将)?(?P<ref>.+?)(?:向|往)(?P<d1>右|左|上|下)(?:平移|移动|移)\s*" + N.replace("(", "(?P<n1>", 1) + r"(?:\s*(?:mm|毫米))?(?:(?:，|,)?再?(?:向|往)(?P<d2>右|左|上|下)\s*" + N.replace("(", "(?P<n2>", 1) + r")?$")
def r_move(b, m, c):
    b.flush()
    d = m.groupdict()
    vec = [0.0, 0.0]
    for dd, nn in ((d["d1"], d["n1"]), (d.get("d2"), d.get("n2"))):
        if dd:
            v = float(nn)
            if dd == "右":
                vec[0] += v
            elif dd == "左":
                vec[0] -= v
            elif dd == "上":
                vec[1] += v
            else:
                vec[1] -= v
    r = ref_query(b, d["ref"])
    b.tool(f"移动 {vec[0]:g},{vec[1]:g}", "transform_entities", {"operation": "move", "entity_ids": [ent(r)], "displacement": vec})
    b.categories.append("2d_editing")


@rule(r"^(?:把|将)?(?P<ref>.+?)(?:绕\s*(?P<c>" + PT[1:-1] + r"))?(?:旋转|转)\s*" + N.replace("(", "(?P<a>", 1) + r"\s*度$")
def r_rotate(b, m, c):
    b.flush()
    d = m.groupdict()
    r = ref_query(b, d["ref"], ["entity_ids", "snap:geometric_center"] if not d.get("c") else None)
    base = P(d["c"]) if d.get("c") else pnt(r)
    if not d.get("c"):
        b.notes.append("旋转基点未指定，取图元的几何中心")
    b.tool(f"旋转 {d['a']}°", "transform_entities", {"operation": "rotate", "entity_ids": [ent(r)], "base_point": base, "rotation_angle_deg": float(d["a"])})
    b.categories.append("2d_editing")


@rule(r"^(?:把|将)?(?P<ref>.+?)(?:放大|缩小|缩放)(?:到|为)?\s*" + N.replace("(", "(?P<k>", 1) + r"\s*倍$")
def r_scale(b, m, c):
    b.flush()
    d = m.groupdict()
    r = ref_query(b, d["ref"], ["entity_ids", "snap:geometric_center"])
    k = float(d["k"])
    if "缩小" in c:
        k = 1 / k
    b.tool(f"缩放 {k:g} 倍", "transform_entities", {"operation": "scale", "entity_ids": [ent(r)], "base_point": pnt(r), "scale_factor": k})
    b.categories.append("2d_editing")


@rule(r"^(?:把|将)?(?P<ref>.+?)(?:删掉|删除|去掉|擦掉|清除|不要了)$|^(?:删除|删掉|去掉|擦除)(?P<ref2>.+?)$")
def r_erase(b, m, c):
    b.flush()
    d = m.groupdict()
    ref = (d.get("ref") or d.get("ref2") or "").strip()
    if re.fullmatch(r"(?:刚才那步|上一步|刚才那个操作)", ref):
        raise ValueError("undo")                         # 交给命令通道
    r = ref_query(b, ref)
    b.tool("删除", "erase_entities", {"entity_ids": [E(r, ".entity_ids")]})
    b.categories.append("2d_editing")


@rule(r"^(?:分解|炸开|把(?P<ref2>.+?)炸开|把(?P<ref3>.+?)分解)(?P<ref>.*)$")
def r_explode(b, m, c):
    b.flush()
    d = m.groupdict()
    ref = (d.get("ref") or d.get("ref2") or d.get("ref3") or "").strip() or "刚才那个"
    r = ref_query(b, ref)
    b.tool("分解", "explode_compound_entities", {"entity_ids": [E(r, ".entity_ids")]})
    b.categories.append("2d_editing")


@rule(r"^(?:把|将)(?P<ref>.+?)(?:改到|移到|放到|换到|改放到)\s*[「\"“]?(?P<layer>\S+?)(?:图层|层)[」\"”]?$")
def r_to_layer(b, m, c):
    b.flush()
    d = m.groupdict()
    r = ref_query(b, d["ref"])
    b.tool(f"改到 {d['layer']} 层", "modify_entity_properties", {"action": "set", "entity_ids": [E(r, ".entity_ids")], "common_props": {"layer": d["layer"]}})
    b.categories.append("2d_editing")


@rule(r"^(?:把|将)(?P<ref>.+?)(?:的)?(?:颜色)?(?:改成|改为|设为|变成)(?P<col>红|黄|绿|青|蓝|白|灰)色?$")
def r_to_color(b, m, c):
    b.flush()
    d = m.groupdict()
    r = ref_query(b, d["ref"])
    b.tool(f"颜色改为{d['col']}", "modify_entity_properties", {"action": "set", "entity_ids": [E(r, ".entity_ids")], "common_props": {"color": COLOR_NAMES[d["col"]]}})
    b.categories.append("2d_editing")


@rule(r"^(?:颜色|颜色再|再把颜色)(?:也)?(?:改成|改为|设为|变成)(?P<col>红|黄|绿|青|蓝|白|灰)色?$")
def r_to_color_cont(b, m, c):
    ref = getattr(b, "last_ref", None)
    if not ref:
        raise Ask("要改哪个对象的颜色？", ["刚才那个", "选中的"])
    b.flush()
    r = ref_query(b, ref)
    col = m.group("col")
    b.tool(f"颜色改为{col}", "modify_entity_properties", {"action": "set", "entity_ids": [E(r, ".entity_ids")], "common_props": {"color": COLOR_NAMES[col]}})
    b.categories.append("2d_editing")


@rule(r"^(?:把|将)(?P<ref>.+?)的(?P<f>半径|直径)(?:改成|改为|设为|变成)\s*" + N.replace("(", "(?P<v>", 1) + r"$")
def r_resize(b, m, c):
    b.flush()
    d = m.groupdict()
    r = ref_query(b, d["ref"])
    b.tool(f"{d['f']}改为 {d['v']}", "modify_entity_properties", {"action": "set", "entity_ids": [E(r, ".entity_ids")], "geometry_props": {"radius" if d["f"] == "半径" else "diameter": float(d["v"])}})
    b.categories.append("2d_editing")


@rule(r"^(?:修剪|剪掉)(?P<ref>.+?)$|^(?:延伸|拉长到)(?P<ref2>.+?)(?:到(?P<bd>.+))?$")
def r_trim(b, m, c):
    b.flush()
    d = m.groupdict()
    op = "trim" if c.startswith(("修剪", "剪掉")) else "extend"
    ref = (d.get("ref") or d.get("ref2") or "").strip()
    r = ref_query(b, ref, ["entity_ids", "snap:midpoint"])
    args: dict = {"operation": op, "target_entities": [{"entity_id": ent(r), "pick_point": pnt(r)}]}
    if d.get("bd"):
        rb = ref_query(b, d["bd"])
        args["boundary_entity_ids"] = [E(rb, ".entity_ids")]
    b.notes.append("修剪/延伸的拾取点取图元中点；需要精确指定时请给出点")
    b.tool("修剪" if op == "trim" else "延伸", "adjust_line_lengths", args)
    b.categories.append("2d_editing")


@rule(r"^(?:选中|选择|选取)(?P<ref>.+)$")
def r_select(b, m, c):
    b.flush()
    ref_query(b, m.group("ref"), None, f"选中「{m.group('ref')}」")
    b.categories.append("view_select")


# ---- 标注 / 公差 -----------------------------------------------
@rule(r"^标注(?P<ref>.+?)的(?P<what>长和宽|长度和宽度|长宽|长度|宽度|长|宽|直径|半径|边长)$|^标注(?P<ref2>[^的]+?)(?P<what2>直径|半径)$")
def r_dim(b, m, c):
    b.flush()
    d = m.groupdict()
    ref = (d.get("ref") or d.get("ref2") or "").strip()
    what = d.get("what") or d.get("what2")
    b.categories.append("annotation_dimension")
    r = ref_query(b, ref)
    if what in ("直径", "半径"):
        k = b.tool(f"标注{what}", "create_radial_and_chain_dim", {"dim_type": "diameter" if what == "直径" else "radius", "target_entity_id": ent(r)})
        b.dims[what], b.last_dim = k, k
        return
    keys = {}
    if what in ("长和宽", "长度和宽度", "长宽", "边长"):
        keys["长"] = b.tool("标注长度", "create_linear_and_angular_dim", {"dim_type": "linear_horizontal", "target_entity_id": ent(r)})
        keys["宽"] = b.tool("标注宽度", "create_linear_and_angular_dim", {"dim_type": "linear_vertical", "target_entity_id": ent(r)})
    elif what in ("长", "长度"):
        keys["长"] = b.tool("标注长度", "create_linear_and_angular_dim", {"dim_type": "linear_horizontal", "target_entity_id": ent(r)})
    else:
        keys["宽"] = b.tool("标注宽度", "create_linear_and_angular_dim", {"dim_type": "linear_vertical", "target_entity_id": ent(r)})
    b.dims.update(keys)
    b.last_dim = list(keys.values())[-1]


@rule(r"^(?:(?:给|在)(?P<t>[^，,加标画]{1,6}?)(?:上)?)?(?:加|添加|标注|画)(?:一个)?(?P<g>平行度|垂直度|平面度|直线度|圆度|圆柱度|倾斜度|位置度|同轴度|对称度|圆跳动|全跳动)公差\s*" + N.replace("(", "(?P<v>", 1) + r"(?:\s*(?:mm|毫米))?(?:(?:，|,)?基准\s*(?P<ds>[A-Za-z](?:[，,、\s]*[A-Za-z])*))?$")
def r_gdt(b, m, c):
    b.flush()
    d = m.groupdict()
    b.pending = {"op": "gdt", "sym": GDT[d["g"]], "v": float(d["v"]), "datums": [x.upper() for x in re.findall(r"[A-Za-z]", d.get("ds") or "")], "phi": False, "target": d.get("t")}
    b.categories.append("engineering_specialty")


@rule(r"^(?:再)?(?:(?:给|在)(?P<t>[^，,加标画]{1,6}?)(?:上)?)?(?:加|标|添加)(?:上)?表面粗糙度\s*(?P<p>Ra|Rz)\s*" + N.replace("(", "(?P<v>", 1) + r"(?P<proc>(?:，|,)?不去除材料|(?:，|,)?去除材料)?$|^表面粗糙度\s*(?P<p2>Ra|Rz)\s*" + N.replace("(", "(?P<v2>", 1) + r"$")
def r_rough(b, m, c):
    b.flush()
    d = m.groupdict()
    proc = "no_remove" if d.get("proc") and "不去除" in d["proc"] else "remove"
    b.pending = {"op": "rough", "p": d.get("p") or d.get("p2"), "v": float(d.get("v") or d.get("v2")), "proc": proc, "target": d.get("t")}
    b.categories.append("engineering_specialty")


# ---- 块 / 约束 ----------------------------------------------------
@rule(r"^(?:把|将)(?P<ref>.+?)(?:做成|制成|定义成|变成|创建成)(?:一个)?(?P<attr>带属性的)?块(?:(?:，|,)?(?:属性是|属性为|属性)(?P<an>\S+?))?(?:(?:，|,)?(?:插到|插入到|放到|插入)(?P<at>\S+))?$")
def r_block(b, m, c):
    b.flush()
    d = m.groupdict()
    b.pending = {"op": "block", "ref": d["ref"], "attr": d.get("an") or None, "has_attr": bool(d.get("attr")), "at": d.get("at")}
    b.categories.append("block_xref_constraint")


@rule(r"^(?:给|为|对)\s*(?P<a>[A-Za-z]{1,2}\d+|\S+?)\s*(?:和|与|跟)\s*(?P<b2>[A-Za-z]{1,2}\d+|\S+?)\s*加(?P<t>同心|平行|垂直|相切|相等|水平|竖直|共线|重合|固定)约束$")
def r_constraint(b, m, c):
    b.flush()
    d = m.groupdict()
    gt = {"同心": "concentric", "平行": "parallel", "垂直": "perpendicular", "相切": "tangent", "相等": "equal", "水平": "horizontal", "竖直": "vertical", "共线": "collinear", "重合": "coincident", "固定": "fix"}[d["t"]]
    ids = []
    for ph in (d["a"], d["b2"]):
        if re.fullmatch(r"[A-Za-z]{1,2}\d+", ph):
            ids.append(ph.upper())
        else:
            ids.append(ent(ref_query(b, ph)))
    k = b.tool(f"{d['t']}约束", "apply_parametric_constraints", {"constraint_category": "geometric", "geom_type": gt, "entity_ids": ids})
    b.last = {"cons_ids": ids}
    b.categories.append("block_xref_constraint")


@rule(r"^(?P<a>[A-Za-z]{1,2}\d+)的(?P<f>直径|半径)等于(?P<b2>[A-Za-z]{1,2}\d+)的(?:(?P<k>\d+(?:\.\d+)?)倍|一半|两倍|一倍)$")
def r_param_eq(b, m, c):
    b.flush()
    d = m.groupdict()
    a_id, b_id = d["a"].upper(), d["b2"].upper()
    ids = (b.last or {}).get("cons_ids") or []
    k = float(d["k"]) if d.get("k") else (0.5 if "一半" in c else 2.0 if "两倍" in c else 1.0)
    dt = "diameter" if d["f"] == "直径" else "radius"
    b.tool(f"{b_id} 的{d['f']}作为参数 d1", "apply_parametric_constraints", {"constraint_category": "dimensional", "dim_type": dt, "entity_ids": [b_id], "dim_param_name": "d1"})
    expr = f"d1 * {k:g}" if k != 1 else "d1"
    b.tool(f"{a_id} 的{d['f']} = {expr}", "apply_parametric_constraints", {"constraint_category": "dimensional", "dim_type": dt, "entity_ids": [a_id], "dim_param_name": "d2", "dim_expression": expr})
    b.categories.append("block_xref_constraint")


# ---- 布局 / 输出 -------------------------------------------------
@rule(r"^(?:建|新建|创建)(?:一个)?布局(?:(?:，|,)?(?:(A[0-4])\s*)?(横向|竖向)?)?$|^(?:建|新建|创建)(?:一个)?(?:(A[0-4])\s*)?(横向|竖向)?布局$")
def r_layout(b, m, c):
    b.flush()
    size = (m.group(1) or m.group(3) or "A3").upper()
    b.layout_name = "布局1"
    if (m.group(2) or m.group(4)) == "竖向":
        b.notes.append("竖向图纸暂不支持，按横向建立")
    b.tool(f"新建布局（{size} 横向）", "switch_model_and_layout_space", {"target_space": "Model", "layout_action": "create", "new_layout_name": "布局1", "paper_size": f"ISO_{size}"})
    b.categories.append("layout_plotting")


@rule(r"^(?:开|建|创建|新建|加)(?:一个)?\s*(?P<a>\d+)\s*比\s*(?P<b2>\d+)\s*(?:的)?视口(?:(?:，|,)?并?(?P<lock>锁定))?$")
def r_viewport(b, m, c):
    b.flush()
    d = m.groupdict()
    ln = b.layout_name or "布局1"
    k = b.tool("创建视口", "manage_layout_viewports", {"layout_name": ln, "action": "create", "standard_scale": f"{d['a']}:{d['b2']}"})
    b.last = {"vp": k, "layout": ln}
    if d.get("lock"):
        b.tool("锁定视口", "manage_layout_viewports", {"layout_name": ln, "action": "lock", "viewport_id": E(k, ".data.viewport_id")})
    b.categories.append("layout_plotting")


@rule(r"^(?:并)?锁定视口$|^视口锁定$")
def r_lock_vp(b, m, c):
    b.flush()
    vp = (b.last or {}).get("vp")
    ln = (b.last or {}).get("layout") or b.layout_name or "布局1"
    args = {"layout_name": ln, "action": "lock"}
    if vp:
        args["viewport_id"] = E(vp, ".data.viewport_id")
    b.tool("锁定视口", "manage_layout_viewports", args)
    b.categories.append("layout_plotting")


@rule(r"^(?:输出|导出|打印)(?:成|为)?\s*(PDF|PNG|SVG)(?:文件)?$")
def r_plot(b, m, c):
    b.flush()
    space = b.layout_name or b.ctx.get("current_space") or "Model"
    b.tool(f"输出 {m.group(1)}", "configure_plot_and_export", {"space_or_layout": space, "device": m.group(1).lower()}, barrier=True)
    if space != "Model":
        b.tool("切回模型空间", "switch_model_and_layout_space", {"target_space": "Model"}, barrier=True)
        b.notes.append("输出后已切回模型空间，后面的编辑默认针对模型")
    b.categories.append("layout_plotting")


# ---- 三维 ---------------------------------------------------------
@rule(r"^(?:把|将)(?P<ref>.+?)(?:拉伸|拉成)\s*" + N.replace("(", "(?P<h>", 1) + r"\s*(?:mm|毫米)?(?:高)?(?:，|,)?(?:变成|成为|成|做成)?(?:实体|三维实体|三维)?$|^(?:把|将)(?P<ref2>.+?)(?:拉伸|拉成)\s*" + N.replace("(", "(?P<h2>", 1) + r"\s*(?:变成|成为|成|做成)(?:实体|三维实体|三维)?$")
def r_extrude(b, m, c):
    b.flush()
    d = m.groupdict()
    ref = (d.get("ref") or d.get("ref2") or "").strip()
    h = float(d.get("h") or d.get("h2"))
    r = b.recog(f"识别「{ref}」的轮廓和孔", ref, ["entity_ids", "boundary"])
    b.tool(f"拉伸 {h:g} 成实体", "extrude_2d_to_3d", {"profile_entity_ids": [E(r, ".boundary.outer"), E(r, ".boundary.islands")], "include_islands": True, "height": h})
    b.categories.append("3d_modeling")


@rule(r"^(?:把|将)(?P<ref>.+?)绕\s*(?P<ax>X轴|Y轴)旋转成(?:回转体|实体)$")
def r_revolve(b, m, c):
    b.flush()
    d = m.groupdict()
    r = b.recog(f"识别「{d['ref']}」的轮廓", d["ref"], ["entity_ids", "boundary"])
    b.tool("旋转成回转体", "revolve_2d_to_3d", {"profile_entity_ids": [E(r, ".boundary.outer"), E(r, ".boundary.islands")], "axis_mode": "x_axis" if d["ax"] == "X轴" else "y_axis"})
    b.categories.append("3d_modeling")


VIEWS = {"西南": "sw_isometric", "东南": "se_isometric", "东北": "ne_isometric", "西北": "nw_isometric", "俯视": "top", "仰视": "bottom", "前视": "front", "后视": "back", "左视": "left", "右视": "right"}


@rule(r"^(?:切到|切换到|转到|换到|看)(?:一下)?(?P<v>西南|东南|东北|西北)等轴测(?:视图)?(?:看看|看一下)?$|^(?:切到|切换到)?(?P<v2>俯视|仰视|前视|后视|左视|右视)(?:图)?$")
def r_view3d(b, m, c):
    b.flush()
    d = m.groupdict()
    key = d.get("v") or d.get("v2")
    b.tool("切换三维视图", "switch_3d_view_and_visual_style", {"standard_view": VIEWS[key]})
    b.categories.append("3d_modeling")


@rule(r"^(?:切到|切换到|改成|设为)?(带边着色|着色|消隐|线框|二维线框)(?:样式|视觉样式)?$")
def r_style(b, m, c):
    b.flush()
    st = {"带边着色": "shaded_with_edges", "着色": "shaded", "消隐": "hidden", "线框": "wireframe", "二维线框": "2d_wireframe"}[m.group(1)]
    b.tool("切换视觉样式", "switch_3d_view_and_visual_style", {"visual_style": st})
    b.categories.append("3d_modeling")


# ---- 专业工具集 / 符号 ---------------------------------------------
@rule(r"^(?:画|插入|加)(?:一个)?(?P<size>M\d+)(?P<kind>螺栓|螺母|垫圈|内六角螺钉)(?:(?:，|,)?(?:长|长度)\s*" + N.replace("(", "(?P<L>", 1) + r")?(?:\s*(?:，|,)?(?:在|放到|位置)\s*" + PT.replace("(", "(?P<at>", 1) + r")?$")
def r_fastener(b, m, c):
    b.flush()
    d = m.groupdict()
    typ = {"螺栓": "hex_bolt", "螺母": "hex_nut", "垫圈": "washer", "内六角螺钉": "socket_head_cap_screw"}[d["kind"]]
    sp = {"type": typ, "size": d["size"]}
    if d.get("L"):
        sp["length"] = float(d["L"])
    if not d.get("at"):
        raise Ask("插到哪里？请给插入点坐标。", ["0,0", "100,100"])
    if typ in ("hex_bolt", "socket_head_cap_screw") and "length" not in sp:
        raise Ask(f"{d['size']} {d['kind']}的公称长度是多少（mm）？", ["20", "30", "40"])
    b.tool(f"画 {d['size']} {d['kind']}", "invoke_engineering_toolset", {"domain": "mechanical", "tool_name": "standard_fastener", "insert_point": P(d["at"]), "spec_params": sp}, barrier=True)
    b.categories.append("engineering_specialty")


@rule(r"^(?:插入|放置|加)(?P<kw>[^\s，,]+?)符号(?:到|在|于)\s*" + PT.replace("(", "(?P<at>", 1) + r"$")
def r_symbol(b, m, c):
    b.flush()
    d = m.groupdict()
    b.tool(f"插入符号 {d['kw']}", "manage_symbol_library", {"action": "insert_symbol", "symbol_keyword_or_id": d["kw"], "insert_point": P(d["at"])})
    b.categories.append("engineering_specialty")


# ------------------------------------------------------------------ 修饰规则
@mod(r"^(?:左下角|起点|角点|第一个角点)(?:在|为|于)?\s*" + PT + r"$")
def m_corner(b, m, c):
    p = b.pending
    if p and p["op"] == "rect":
        p["corner1"] = P(m.group(1))
        return True
    if p and p["op"] == "polygon":
        return False
    return False


@mod(r"^(?:长|长度)\s*" + N + r"(?:\s*(?:mm|毫米))?\s*(?:，|,)?\s*(?:宽|宽度)\s*" + N + r"(?:\s*(?:mm|毫米))?$")
def m_wh(b, m, c):
    p = b.pending
    if p and p["op"] == "rect":
        p["w"], p["h"] = float(m.group(1)), float(m.group(2))
        return True
    return False


@mod(r"^(?:长|长度|宽度?|边长)\s*" + N + r"(?:\s*(?:mm|毫米))?$")
def m_one_side(b, m, c):
    p = b.pending
    if p and p["op"] == "rect":
        v = float(m.group(1))
        if c.startswith(("长", "边长")):
            p["w"] = v
            if p.get("square"):
                p["h"] = v
        else:
            p["h"] = v
        return True
    return False


@mod(r"^(?:对角点|另一个角点|第二个角点)(?:在|为)?\s*" + PT + r"$")
def m_corner2(b, m, c):
    p = b.pending
    if p and p["op"] == "rect":
        p["corner2"] = P(m.group(1))
        return True
    return False


@mod(r"^(?:四角|四个角)?(?:圆角|倒圆角)\s*(?:R|r|半径)?\s*" + N + r"$")
def m_rect_fillet(b, m, c):
    p = b.pending
    if p and p["op"] == "rect":
        p["fillet"] = float(m.group(1))
        return True
    return False


@mod(r"^圆心(?:在|为|于)\s*" + PT + r"$")
def m_center(b, m, c):
    p = b.pending
    if p and p["op"] in ("circle", "polygon", "polar_new"):
        p["center"] = P(m.group(1))
        return True
    return False


@mod(r"^(?:圆心|中心)(?:在|为|于)\s*(?P<ref>.+?)$")
def m_center_ref(b, m, c):
    p = b.pending
    if p and p["op"] in ("circle", "polygon"):
        ref = m.group("ref")
        if re.fullmatch(PT, ref):
            return False
        p["center_ref"] = ref if re.search(r"圆心|中心|中点|角", ref) else ref + "中心"
        return True
    return False


@mod(r"^(半径|直径)(?:为|是)?\s*" + N + r"$")
def m_rd(b, m, c):
    p = b.pending
    if p and p["op"] in ("circle", "polygon"):
        p["rd"], p["v"] = ("radius" if m.group(1) == "半径" else "diameter"), float(m.group(2))
        return True
    return False


@mod(r"^(内接|外切)(?:于)?(?:圆)?(?:，|,)?(?:半径)?\s*" + N + r"$|^(?:外接圆|内切圆)?半径\s*" + N + r"$")
def m_poly_r(b, m, c):
    p = b.pending
    if p and p["op"] == "polygon":
        p["inscribed"] = "外切" not in c
        p["r"] = float(m.group(2) or m.group(3))
        return True
    return False


@mod(r"^分布圆(?:的)?直径\s*" + N + r"$")
def m_pitch(b, m, c):
    p = b.pending
    if p and p["op"] == "polar_new":
        p["pitch_d"] = float(m.group(1))
        return True
    return False


@mod(r"^(?:再)?(?:向|往)(右|左|上|下)\s*" + N + r"(?:\s*(?:mm|毫米))?$")
def m_path_more(b, m, c):
    p = b.pending
    if p and p["op"] == "path":
        p["points"].append(f"{DIRS[m.group(1)]} {m.group(2)}")
        return True
    return False


@mod(r"^(?:再)?长\s*" + N + r"\s*(?:，|,)?\s*角度\s*" + N + r"\s*度?$")
def m_path_polar(b, m, c):
    p = b.pending
    if p and p["op"] == "path":
        p["points"].append(f"@{m.group(1)}<{m.group(2)}")
        return True
    return False


@mod(r"^(?P<size>A[0-4])\s*(?P<ori>横向|竖向)?$|^(?P<ori2>横向|竖向)$")
def m_layout_paper(b, m, c):
    steps = [s for s in b.steps if s.get("tool") == "switch_model_and_layout_space" and s["args"].get("layout_action") == "create"]
    if not steps:
        return False
    d = m.groupdict()
    if d.get("size"):
        steps[-1]["args"]["paper_size"] = f"ISO_{d['size'].upper()}"
    if (d.get("ori") or d.get("ori2")) == "竖向":
        b.notes.append("竖向图纸暂不支持，按横向建立")
    return True


@mod(r"^(?:再)?(?:闭合|封闭|回到起点)$")
def m_close(b, m, c):
    p = b.pending
    if p and p["op"] == "path":
        p["close"] = True
        return True
    return False


@mod(r"^基准\s*(?P<ds>[A-Za-z](?:[，,、\s]*[A-Za-z])*)$")
def m_datum(b, m, c):
    p = b.pending
    if p and p["op"] == "gdt":
        p["datums"] = [x.upper() for x in re.findall(r"[A-Za-z]", m.group("ds"))]
        return True
    return False


@mod(r"^(?:属性(?:是|为)?)\s*(?P<an>\S+)$")
def m_attr(b, m, c):
    p = b.pending
    if p and p["op"] == "block":
        p["attr"], p["has_attr"] = m.group("an"), True
        return True
    return False


@mod(r"^(?:插到|插入到|放到|插入)\s*(?P<at>\S+)$")
def m_block_at(b, m, c):
    p = b.pending
    if p and p["op"] == "block":
        p["at"] = m.group("at")
        return True
    return False


@mod(r"^(?:长度|长|宽度|宽)?(?:加|标)(?:上)?(?:正负|±)\s*" + N + r"\s*的?公差$")
def m_sym_tol(b, m, c):
    if not b.dims:
        return False
    which = "宽" if c.startswith("宽") else "长" if c.startswith("长") else None
    key = b.dims.get(which) if which else b.last_dim
    if key is None:
        key = b.last_dim
    b.flush()
    b.tool("加对称公差", "create_mechanical_tolerances", {"tolerance_category": "symmetric", "target_dim_or_entity_id": E(key, ".data.dim_id"), "upper_deviation": float(m.group(1))})
    b.categories.append("engineering_specialty")
    return True


@mod(r"^(?:加|标)(?:上)?(?P<code>[A-Za-z]\d+(?:/[A-Za-z]\d+)?)(?:公差|配合)$")
def m_fit(b, m, c):
    if not b.dims:
        return False
    key = b.dims.get("直径") or b.dims.get("半径") or b.last_dim
    b.flush()
    b.tool(f"加 {m.group('code')} 公差", "create_mechanical_tolerances", {"tolerance_category": "dimensional_fit", "target_dim_or_entity_id": E(key, ".data.dim_id"), "iso_fit_code": m.group("code")})
    b.categories.append("engineering_specialty")
    return True


# ------------------------------------------------------------------ 落成步骤
def f_path(b, p):
    if len(p["points"]) < 2:
        raise Ask("线的终点在哪里？", ["向右 100", "@100,0"])
    k = b.tool("画直线", "draw_linear_primitives", {"primitive_type": "line", "points": p["points"], "close_loop": p["close"]})
    b.last = {"path": k}


def f_rect(b, p):
    args: dict = {"shape_type": "rectangle"}
    if p.get("corner1") is None:
        raise Ask("矩形的左下角在哪里？", ["0,0", "50,50"])
    args["corner1"] = p["corner1"]
    if p.get("corner2"):
        args["corner2"] = p["corner2"]
    elif p.get("w") and p.get("h"):
        args["width"], args["height"] = p["w"], p["h"]
    else:
        raise Ask("矩形的长和宽是多少？", ["长 100 宽 60", "长 120 宽 80"])
    if p.get("fillet"):
        args["rect_fillet_r"] = p["fillet"]
    k = b.tool("画矩形", "draw_polygons_and_meshes", args)
    b.last = {"rect": k}


def f_polygon(b, p):
    if not (p.get("r") or p.get("v")):
        raise Ask(f"正{p['n']}边形的半径是多少？中心在哪里？", ["中心 0,0，半径 50"])
    center = p.get("center")
    if center is None and p.get("center_ref"):
        center = pnt(b.recog("识别中心", p["center_ref"], ["entity_ids", "snap:geometric_center"]))
    if center is None:
        raise Ask("多边形的中心在哪里？", ["0,0"])
    r = p.get("r") or (p["v"] if p.get("rd") == "radius" else p["v"] / 2)
    b.tool(f"画正{p['n']}边形", "draw_polygons_and_meshes", {"shape_type": "polygon", "num_sides": p["n"], "center": center, "radius": r, "inscribed": p.get("inscribed", True)})


def f_circle(b, p):
    center = p.get("center")
    if center is None and p.get("center_ref"):
        rk = b.recog(f"识别「{p['center_ref']}」", p["center_ref"], ["entity_ids", "snap:geometric_center"])
        center = pnt(rk)
    if center is None or p.get("v") is None:
        miss = []
        if center is None:
            miss.append("圆心放在哪里")
        if p.get("v") is None:
            miss.append("半径或直径是多少")
        raise Ask("？".join(miss) + "？", ["圆心 0,0，半径 10", "圆心在当前视图中心，直径 50"])
    args = {"curve_type": "circle", "method": "center_radius" if p["rd"] == "radius" else "center_diameter", "center": center, p["rd"]: p["v"]}
    k = b.tool("画圆", "draw_circular_primitives", args)
    b.last = {"circle": k}


def f_polar_new(b, p):
    if p.get("pitch_d") is None:
        raise Ask("分布圆（阵列所在的圆）的直径是多少？", ["60", "80"])
    if not p.get("item"):
        raise Ask("阵列的小圆直径是多少？", ["8", "10"])
    rk = b.recog(f"识别「{p['ref']}」", p["ref"], ["entity_ids", "snap:center"])
    c = pnt(rk)
    item = p["item"]
    ck = b.tool("画小圆", "draw_circular_primitives", {"curve_type": "circle", "method": "center_radius" if item["rd"] == "radius" else "center_diameter", "center": c, item["rd"]: item["v"]})
    b.tool("移到分布圆上", "transform_entities", {"operation": "move", "entity_ids": [E(ck, ".created[0]")], "displacement": [p["pitch_d"] / 2, 0]})
    b.tool(f"环形阵列 {p['n']} 个", "replicate_entities", {"operation": "array_polar", "entity_ids": [E(ck, ".created[0]")], "array_params": {"center": c, "count": p["n"], "fill_angle_deg": 360, "rotate_items": False}})
    b.notes.append("环形阵列数量包含原对象；小圆先画在圆心再平移半个分布圆直径")


def _default_target(b) -> str:
    inv = b.ctx.get("inventory") or {}
    has_poly = bool(inv.get("polyline(closed)") or inv.get("polyline")) or any(r.get("type") == "polyline" for r in (b.ctx.get("recent_entities") or []))
    made_rect = any(s.get("tool") == "draw_polygons_and_meshes" for s in b.steps)
    return "矩形" if (has_poly or made_rect) else "刚才那个"


def f_gdt(b, p):
    if p["sym"] in ("parallelism", "perpendicularity", "angularity", "concentricity", "symmetry", "circular_runout", "total_runout") and not p["datums"]:
        raise Ask("这个形位公差的基准是哪个？", ["A", "B"])
    ph = p.get("target") or _default_target(b)
    rk = b.recog(f"识别被测要素「{ph}」", ph, ["entity_ids"])
    b.notes.append(f"形位公差的被测要素未指定，取「{ph}」")
    k = b.tool("加形位公差框格", "create_mechanical_tolerances", {"tolerance_category": "geometric_gdt", "gdt_symbol": p["sym"], "gdt_value": p["v"], "datum_refs": p["datums"] or None, "target_dim_or_entity_id": ent(rk)})
    b.last = {"gdt_target": rk}


def f_rough(b, p):
    ph = (b.last or {}).get("gdt_target")
    if ph is None:
        phrase = p.get("target") or _default_target(b)
        ph = b.recog(f"识别被标注表面「{phrase}」", phrase, ["entity_ids"])
        b.notes.append(f"粗糙度的标注表面未指定，取「{phrase}」")
    b.tool("加粗糙度符号", "create_mechanical_tolerances", {"tolerance_category": "surface_roughness", "roughness_param": p["p"], "roughness_value": p["v"], "process_symbol": p["proc"], "target_dim_or_entity_id": ent(ph)})


def f_block(b, p):
    src = b.recog(f"识别「{p['ref']}」", p["ref"], ["entity_ids"])
    name = re.sub(r"[^\w一-鿿]", "", p["ref"])[:12] or "块1"
    defs = None
    if p.get("has_attr") or p.get("attr"):
        defs = [{"tag": p.get("attr") or "属性1", "prompt": p.get("attr") or "属性1", "default_val": ""}]
        b.notes.append("属性值没给，默认留空（属性标签：" + defs[0]["tag"] + "）")
    args = {"action": "define_block", "block_name": name, "source_entity_ids": [ent(src)], "keep_source": "retain"}
    if defs:
        args["attribute_defs"] = defs
    bk = b.tool(f"定义块「{name}」", "create_and_insert_block", args)
    if p.get("at"):
        at = p["at"]
        if re.fullmatch(PT, at):
            ip = P(at)
        else:
            corner = re.search(r"(左下|右下|左上|右上)角?", at)
            if not corner:
                raise Ask(f"「{at}」是哪个位置？请给插入点坐标。", ["0,0", "矩形的右上角"])
            host = "矩形" if _default_target(b) == "矩形" else None
            if host is None:
                raise Ask("插到哪里？请给插入点坐标。", ["0,0"])
            ip = pnt(b.recog(f"识别{host}的{corner.group(1)}角", f"{host}的{corner.group(1)}角", ["entity_ids", "snap:endpoint"]))
            b.notes.append(f"「{at}」按「{host}」的{corner.group(1)}角理解")
        iargs = {"action": "insert_block", "block_name": name, "insert_point": ip}
        if defs:
            iargs["attribute_values"] = {}
        b.tool(f"插入块「{name}」", "create_and_insert_block", iargs)


FLUSH = {"path": f_path, "rect": f_rect, "polygon": f_polygon, "circle": f_circle, "polar_new": f_polar_new, "gdt": f_gdt, "rough": f_rough, "block": f_block}


# ------------------------------------------------------------------ 切句与编译
LEAD = re.compile(r"^(?:然后|接着|再然后|之后|最后|并且|并|同时|另外|还要)(?:就)?")


def split_clauses(text: str) -> list[str]:
    out: list[str] = []
    for part in re.split(r"[；;。\n]+", text):
        part = re.sub(r"^((?:然后|接着|并)?(?:画|绘制)[^，,]*?(?:圆|矩形|长方形|正方形))(?=(?:圆心|左下角|起点|角点)[在为于])", r"\1，", part)       # 口述里没停顿的“画一个圆圆心在原点”
        for c in re.split(r"，|,(?!\d)", part):
            c = LEAD.sub("", c.strip(" 　")).strip()
            if re.match(r"^再(?!向|往|闭合|长|把|加|标|将|对|给)", c):
                c = c[1:].strip()
            if not c:
                continue
            # 有些主规则自己带逗号（“写文字…，字高5”“画M8螺栓，长30，在100,100”）：被逗号切开的补充参数并回上一句
            if out and ((re.match(r"^字高\s*\d", c) and re.search(r"文字", out[-1])) or (re.match(r"^(?:长|长度|在|放到|位置)", c) and re.match(r"^(?:画|插入|加)(?:一个)?M\d+", out[-1]))):
                out[-1] += "，" + c
                continue
            out.append(c)
    return out


def _try_command(clause: str):
    try:
        return CL.parse(clause)
    except CL.CmdError:
        return None


def apply_clause(b: Builder, clause: str) -> None:
    # 1 修饰语优先（给上一个意图补参数）
    for rx, fn in MODS:
        m = rx.match(clause)
        if m and fn(b, m, clause):
            return
    # 2 命令通道的模板（打开正交、撤销、AutoCAD 命令）
    cmd = _try_command(clause)
    if cmd is not None and cmd.calls:
        b.flush()
        call = cmd.as_call()                                  # 多个调用合成一个批量
        b.tool(cmd.summary, call["tool"], call["args"], barrier=call["tool"] in NON_BATCH)
        b.categories.append("environment_setup" if cmd.name in ("", "ORTHO", "GRID", "SNAP") else "2d_drafting")
        return
    # 3 主规则
    for rx, fn, use_search in RULES:
        m = rx.search(clause) if use_search else rx.match(clause)
        if m:
            try:
                fn(b, m, clause)
                return
            except ValueError as ex:
                if str(ex) == "undo":
                    break
                raise
    cmd = _try_command(clause)
    if cmd is not None and cmd.calls:
        return
    raise Ask(f"没看懂「{clause}」，能换个说法吗？", ["用图元编号指明对象（如 C8、L3）", "用 AutoCAD 命令，如 C 50,50 20"])


def compile_plan(b: Builder, utterance: str, ctx: dict) -> Plan:
    b.flush()
    steps = b.steps
    # 1 分组：相邻 tool 步骤合并（识别步骤、barrier 步骤隔开）
    groups: list[list[dict]] = []
    for s in steps:
        if s["kind"] == "tool" and not s["barrier"] and s["tool"] not in NON_BATCH and groups and groups[-1][0]["kind"] == "tool" and not groups[-1][0]["barrier"] and groups[-1][0]["tool"] not in NON_BATCH:
            groups[-1].append(s)
        else:
            groups.append([s])
    # 2 编号
    index: dict[str, tuple[str, int]] = {}
    for gi, g in enumerate(groups, 1):
        for si, s in enumerate(g):
            index[s["key"]] = (f"s{gi}", si if len(g) > 1 else -1)
    ref_rx = re.compile(r"^\$@(\w+)(.*)$")

    def conv(obj: Any, gid: str, in_group: bool) -> Any:
        if isinstance(obj, str):
            m = ref_rx.match(obj)
            if not m:
                return obj
            k, rest = m.group(1), m.group(2)
            sid, si = index[k]
            if sid == gid and in_group and si >= 0:
                return f"$this[{si}]{rest}"
            if si >= 0:                                       # 引用另一个批量里的某个子步骤：批量结果里的 step_results[i]
                return f"${sid}.step_results[{si}]{rest}"
            return f"${sid}{rest}"
        if isinstance(obj, list):
            return [conv(x, gid, in_group) for x in obj]
        if isinstance(obj, dict):
            return {k: conv(v, gid, in_group) for k, v in obj.items()}
        return obj

    out_steps = []
    dep_rx = re.compile(r"\$(s\d+)")
    prev_tool_sid = None
    for gi, g in enumerate(groups, 1):
        sid = f"s{gi}"
        if g[0]["kind"] == "recog":
            s = g[0]
            payload = {"tool": s["tool"], "args": conv(s["args"], sid, False)} if s.get("tool") else {"query": s["query"], "need": s["need"], **({"expect": s["expect"]} if s.get("expect") else {})}
            out_steps.append({"step_id": sid, "target_agent": "entity_recognition_agent", "depends_on": [], "action_summary": s["summary"], "payload": payload})
        elif len(g) == 1:
            s = g[0]
            out_steps.append({"step_id": sid, "target_agent": "tool_execution_agent", "depends_on": [], "action_summary": s["summary"], "payload": {"tool": s["tool"], "args": conv(s["args"], sid, False)}})
        else:
            payload = {"tool": "cad_batch_execute", "args": {"transaction_name": g[0]["summary"][:20] + "等", "steps": [{"tool": s["tool"], "args": conv(s["args"], sid, True)} for s in g]}}
            out_steps.append({"step_id": sid, "target_agent": "tool_execution_agent", "depends_on": [], "action_summary": "；".join(s["summary"] for s in g)[:60], "payload": payload})
    for st in out_steps:
        deps = sorted(set(dep_rx.findall(repr(st["payload"]))) - {st["step_id"]}, key=lambda x: int(x[1:]))
        st["depends_on"] = deps
    cats = list(dict.fromkeys(b.categories))[:3]
    preview = "；".join(dict.fromkeys(b.notes))
    return Plan.model_validate({"thought": "规则解析（离线）", "intent_categories": cats, "normalized_utterance": utterance, "workflow_steps": out_steps, "replan_after": None,
                                "clarification_needed": False, "clarification": {"question": "", "options": [], "candidates": []}, "user_response_preview": preview})


def plan_offline(utterance: str, ctx: dict | None = None) -> Plan:
    """规范化后的口令 → 计划；缺信息返回反问型计划（没有步骤）。"""
    ctx = ctx or {}
    utterance = re.sub(r"(?<![\d.第])1\s*(?=[张个条份套根段行])", "一", utterance)     # 规范化把“一个/一张”变成了“1个/1张”，规则里按原来的说法写
    utterance = re.sub(r"(?<=[\u4e00-\u9fff])[ \t]+|[ \t]+(?=[\u4e00-\u9fff])", "", utterance)       # 汉字两侧的空格没有意义（“C9 的直径”→“C9的直径”）
    b = Builder(ctx, utterance)
    try:
        for clause in split_clauses(utterance):
            apply_clause(b, clause)
        return compile_plan(b, utterance, ctx)
    except Ask as a:
        return Plan.model_validate({"thought": "缺少关键信息", "intent_categories": list(dict.fromkeys(b.categories))[:3], "normalized_utterance": utterance, "workflow_steps": [],
                                    "clarification_needed": True, "clarification": {"question": a.question, "options": a.options, "candidates": []}, "user_response_preview": ""})
