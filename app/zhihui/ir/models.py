"""CAD-IR：图纸的内存模型，也是工具输入输出与落盘 JSON 的唯一格式。

约定（全员共用，改动需在群里同步）：
- 坐标单位为模型空间 mm；图元库里的尺寸是「图纸 mm」，乘以 drawing.scale 得到模型 mm。
- 设备坐标 (x, y) 是图元包围盒中心；None 表示尚未布局（等待 auto_layout）。
- 设备 id（E1、E2…）在图纸内永久不变；位号 tag（P-101）可以被 edit_equipment 修改。
- 管线路径 path 是正交折线顶点列表，首点 = 起点端口，末点 = 终点端口；None 表示待布线。
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

import re

from pydantic import BaseModel, Field

from .general import DraftSettings, GeoEntity, Layer, SessionState, ViewState
from .vessel import VesselSpec

Point = tuple[float, float]
LineType = Literal["process", "utility", "auxiliary", "instrument"]
Stage = Literal["scheme", "material", "construction"]
STAGE_NAMES = {"scheme": "方案流程图", "material": "物料流程图", "construction": "施工流程图"}

_EXT = re.compile(r"(A[0-4])\+([1-9])/4")


class _PaperSizes(dict):
    """图幅表；另外认「A1+2/4」这种按文章做法的加长幅面：每次加长图样宽度的 1/4（文章第 5 页；GB/T 14689 的加长是按短边整数倍，两者都支持）。"""

    def _ext(self, key):
        m = _EXT.fullmatch(str(key))
        if not m:
            return None
        w, h = dict.__getitem__(self, m.group(1))
        return (round(w * (1 + int(m.group(2)) / 4), 2), h)

    def __missing__(self, key):
        v = self._ext(key)
        if v is None:
            raise KeyError(key)
        return v

    def __contains__(self, key):
        return dict.__contains__(self, key) or self._ext(key) is not None

    def get(self, key, default=None):
        return self[key] if key in self else default


class _BaseFormat(dict):
    def __missing__(self, key):
        m = _EXT.fullmatch(str(key))
        if not m:
            raise KeyError(key)
        return m.group(1)

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default


# 图幅（图纸 mm，横向）：GB/T 14689-2008 基本幅面 + 加长幅面（流程很长时比缩小比例更合适）
PAPER_SIZES: dict[str, tuple[float, float]] = _PaperSizes({
    "A4": (297.0, 210.0),
    "A3": (420.0, 297.0),
    "A2": (594.0, 420.0),
    "A1": (841.0, 594.0),
    "A0": (1189.0, 841.0),
    "A3x3": (891.0, 420.0),
    "A3x4": (1189.0, 420.0),
    "A4x3": (630.0, 297.0),
    "A4x4": (841.0, 297.0),
    "A4x5": (1051.0, 297.0),
})
BASE_FORMAT = _BaseFormat({"A3x3": "A3", "A3x4": "A3", "A4x3": "A4", "A4x4": "A4", "A4x5": "A4"})


class Equipment(BaseModel):
    id: str = Field(description="图纸内永久 ID，如 E1")
    tag: str = Field(description="位号，如 P-101")
    symbol_id: str = Field(description="图元 ID，如 pump")
    name: str = Field(default="", description="中文名称，如 进料泵")
    # elevations（高程布置图）：{"water": 292.90, "bottom": 288.40, "top": 293.20, "ground": 288.40}，单位 m
    params: dict[str, str | float | int | dict[str, float]] = Field(default_factory=dict)
    x: float | None = None
    y: float | None = None

    @property
    def placed(self) -> bool:
        return self.x is not None and self.y is not None


class Fitting(BaseModel):
    """管线上的阀门、管件、管道附件（文章表 2-11）。不是设备，不编设备位号；出图时在管线上断开并插入符号。"""

    type: str = Field(description="管件类型，如 globe_valve 截止阀（见 zhihui/cad/fittings.json）")
    at: float | str = Field(default=0.5, description="位置：0～1 为沿管线长度的比例；「from+15」「to-15」为距起点 / 终点端口的图纸 mm")
    size_class: str = Field(default="", description="规格（如异径管「DN50×DN32」，只能来自用户）")
    for_maintenance: bool = Field(default=False, description="为安装、检修加的法兰、螺纹连接件（文章 3.2.3 节：这类才画）")


class Instrument(BaseModel):
    """仪表控制点（文章 3.2.4 节）：直径 10 mm 细实线圆，上半圆写字母组合代号，下半圆写工段序号；引线连到控制点。"""

    id: str = Field(description="图纸内永久 ID，如 I1")
    letters: str = Field(description="字母组合代号，如 TI、PdRA（文章表 2-13）")
    loop_no: str = Field(default="", description="工段序号（工序号 + 顺序号，3～5 位数字），只能来自用户")
    location: Literal["local", "local_inline", "central_panel", "central_back", "local_panel", "local_back"] = Field(
        default="local", description="安装位置（文章表 2-12）")
    attach_to: str = Field(description="控制点所在的设备 id 或连接 id")
    at: float = Field(default=0.5, description="连在管线上时沿管线的位置（0～1）")


class StreamRow(BaseModel):
    name: str = Field(description="组分名称")
    flow_kg_h: str = Field(default="", description="流量 kg/h（原样保留用户写的数字）")
    mole_pct: str = Field(default="", description="摩尔分率 %（原样保留用户写的数字）")


class StreamTable(BaseModel):
    """物料组成表（文章 3.1.2 节、图 3-2）：画在流程起点和使物料发生变化的设备后面，指引线连到所在管线；合计行由程序计算。"""

    id: str
    anchor: str = Field(description="所在管线的连接 id")
    rows: list[StreamRow] = Field(default_factory=list)
    title: str = Field(default="", description="表名（可省）")


class Connection(BaseModel):
    id: str = Field(description="图纸内永久 ID，如 C1")
    from_equip: str = Field(description="起点设备 id")
    from_port: str
    to_equip: str = Field(description="终点设备 id")
    to_port: str
    line_type: LineType = "process"
    medium: str = ""
    path: list[Point] | None = None
    code: str = Field(default="", description="管道代号，如 YS / QS / PAC / CL；只决定线上文字和图例")
    spec: str = Field(default="", description="管道规格，如 DN200（只能来自用户）")
    buried: bool = Field(default=False, description="埋地管道（文章表 2-5：虚线）")
    existing: bool = Field(default=False, description="原有管道（文章表 2-5：双点画线）")
    tracing: str = Field(default="", description="伴热类型代号（文章表 2-10，如 C 蒸汽单线伴热）；画成带符号的复合线（GB/T 17450 第 3.3.4 条），只能来自用户")
    # 管道标注（文章图 2-7：CW0601-32×3.5A/A-C6），除 pipe_no 可由程序按「物料代号 + 工段号 + 管段序号」编号外，都只能来自用户
    pipe_no: str = Field(default="", description="管道号 = 物料代号 + 工段号 + 管段序号，如 CW0601")
    od: str = Field(default="", description="管道外径（mm，只写数字）")
    wall: str = Field(default="", description="壁厚（mm，只写数字）")
    material: str = Field(default="", description="管道材料代号（文章表 2-7，如 B 碳钢）")
    grade: str = Field(default="", description="管道等级代号")
    temp_code: str = Field(default="", description="使用温度范围代号（文章表 2-8，可省略）")
    insulation_code: str = Field(default="", description="隔热隔音代号（文章表 2-9，可省略）")
    fittings: list[Fitting] = Field(default_factory=list, description="管线上的阀门、管件（文章表 2-11）")
    params: dict[str, str] = Field(default_factory=dict, description="管线旁的工艺参数（温度、压力），物料流程图及以上显示，只能来自用户")


class Annotation(BaseModel):
    id: str
    anchor_ref: str = Field(description="设备 id / 连接 id / 'title'")
    text: str
    style: Literal["note", "label", "elevation", "slope", "section", "index", "north"] = "note"
    x: float | None = None
    y: float | None = None
    rel: Point | None = Field(default=None, description="相对锚点的偏移（图纸 mm）；锚点移动后标注跟着走（文本位置链接）")


class DetailView(BaseModel):
    """局部放大图：锚点（设备 / 管线）+ 偏移（图纸 mm）定圆心，半径（图纸 mm），放大倍数（GB/T 14690 放大比例）。"""

    id: str
    anchor_ref: str
    rel: Point = (0.0, 0.0)
    radius: float = 10.0
    factor: float = 2.0
    label: str = ""


class Dimension(BaseModel):
    """尺寸（DIMENSION 实体）。量的是对象当前的几何，对象移动后跟着走；示意图不按比例时 text 写用户给的尺寸值。"""

    id: str
    measure: Literal["width", "height", "between_h", "between_v"] = "width"
    refs: list[str] = Field(default_factory=list, description="设备 id（width/height 一个，between 两个）")
    text: str = Field(default="", description="尺寸数字（只能来自用户）；空则显示实测值")
    side: Literal["auto", "top", "bottom", "left", "right"] = "auto"


class Schedule(BaseModel):
    """一览表：设备一览表（序号 / 位号 / 名称 / 规格型号 / 材料 / 数量 / 备注）或管道一览表（管道号 / DN / 介质 / 起点 / 终点）。
    inline 放在本图标题栏上方（GB/T 10609.2 明细栏），放不下时自动改为单独一页；sheet 单独出一页。"""

    kind: Literal["equipment", "pipe"]
    placement: Literal["inline", "sheet"] = "inline"


class SystemGroup(BaseModel):
    """子系统分组（如「PAC投药系统」）：高程布置图里放在图纸上半部各自的区域。"""

    id: str
    name: str
    members: list[str] = Field(default_factory=list, description="设备 id")
    boxed: bool | list[str] = Field(default=False, description="True 框住整个子系统；或给设备 id 列表，只框这几台（如计量泵组）")


class Hatch(BaseModel):
    """填充（AutoCAD HATCH）。DXF 每次都从 IR 重新生成，出图时按「锚点当前位置 + pick_rel」重新求边界，
    所以设备移动、换图元、重新布局后填充自动跟着走；求不出边界时 disassociated=True，沿用上次的边界。"""

    id: str = Field(description="图纸内永久 ID，如 H1")
    pattern: str = Field(default="ANSI31", description="图案名（ezdxf 标准图案库）或 SOLID")
    paper_spacing: float = Field(default=3.0, description="图纸上的线距（mm），内部换算成图案比例")
    angle: float = 0.0
    style: Literal["normal", "outer", "ignore"] = "normal"
    layer: str = "ZH-剖面线"
    mode: Literal["pick", "select"] = "pick"
    anchor_ref: str | None = Field(default=None, description="拾取点相对的设备 id / 连接 id；None 表示 pick_rel 是绝对坐标")
    region: str = Field(default='', description='设备命名填充区域；非空时不使用拾取点')
    pick_rel: Point | None = Field(default=None, description="有锚点时为相对锚点的偏移（图纸 mm）；没有锚点时为绝对坐标（模型 mm）")
    boundary_refs: list[str] = Field(default_factory=list, description="选对象方式的边界对象（设备 id / 连接 id）")
    gap_tol: float = Field(default=0.0, description="缺口容差（图纸 mm），对应 AutoCAD 的 HPGAPTOL")
    area: float = Field(default=0.0, description="填充面积（模型 mm²）")
    islands: int = 0
    exact: bool = False
    disassociated: bool = False
    rings: list[list[Point]] = Field(default_factory=list, description="上一次求出的边界（模型 mm，外环在前），失去关联时沿用")
    exterior_count: int = 1


# 新增字段在默认值时不写进 DXF 内嵌的 IR JSON，保证旧模板的导出文件逐字节不变
_NEW_DRAWING_FIELDS = ("title_block", "notes", "legend", "legend_codes", "code_names", "systems", "vscale", "elev_frame", "hatches", "standard", "binding", "dimensions", "breaks", "details", "title_template", "schedules", "auto_tags",
                       "stage", "section_no", "tag_style", "used_seqs", "sheet_no", "sheet_total", "zones", "frame", "ground", "repeats", "instruments", "stream_tables",
                       "mode", "workspace", "layers", "current_layer", "entities", "text_styles", "current_text_style", "dim_styles", "current_dim_style", "ltscale", "celtscale",
                       "psltscale", "limits", "limits_check", "background", "point_style", "blocks", "xrefs", "layouts", "active_space", "constraints", "toolset", "draft", "view",
                       "session", "mline_styles", "dynamic_blocks", "saved_dxf_version", "plot_setups", "vessel")
_NEW_CONNECTION_FIELDS = ("code", "spec", "buried", "existing", "tracing", "pipe_no", "od", "wall", "material", "grade", "temp_code", "insulation_code", "fittings", "params")


class RepeatSystem(BaseModel):
    """完全相同的局部系统只画一套，其余用细双点画线方框表示，框内注明系统名称和编号（文章 2.1 节）。"""

    id: str
    name: str = Field(description="系统名称，如 真空受槽系统")
    numbers: str = Field(default="", description="框内注的编号，如 V0605B 或 2#、3#（只能来自用户或已有位号）")
    members: list[str] = Field(default_factory=list, description="画出来的那一套的设备 id；方框放在它们旁边")


class Drawing(BaseModel):
    id: str
    title: str = "工艺流程图"
    units: Literal["mm"] = "mm"
    paper_size: str = "A3"
    scale: float = 50.0
    template_id: str = "pfd_default"
    equipment: list[Equipment] = Field(default_factory=list)
    connections: list[Connection] = Field(default_factory=list)
    annotations: list[Annotation] = Field(default_factory=list)
    revision: int = 0
    base_revision: int | None = Field(default=None, description="改图时的基线版本，用于差异报告与误改检查")
    declared_impact: list[str] = Field(default_factory=list, description="改图工具声明过的影响集（设备/连接 id），校验时以外的对象不得变化")
    source: Literal["generated", "uploaded"] = "generated"
    source_file: str | None = None
    next_seq: int = 1
    # ---- 出图模板 water_elevation（给水处理 工艺流程及高程布置图）用到的字段，其他模板忽略 ----
    title_block: dict[str, str | list[str]] = Field(default_factory=dict, description="标题栏字段，只能来自用户，缺的显示格子说明")
    title_template: str | dict = Field(default="", description="标题栏模板：内置模板 id（gb10609 / water_institute）或用户上传的模板 JSON；空则按标准配置")
    schedules: list[Schedule] = Field(default_factory=list, description="设备一览表 / 管道一览表")
    auto_tags: list[str] = Field(default_factory=list, description="位号由程序按流程顺序自动编号的设备 id")
    notes: list[str] = Field(default_factory=list, description="说明条目，只能来自用户")
    legend: list[dict[str, str]] = Field(default_factory=list, description="图例（自动生成）：[{code, name}]")
    legend_codes: list[str] = Field(default_factory=list, description="用户要求图例里列出的代号（即使图中没用到）")
    code_names: dict[str, str] = Field(default_factory=dict, description="覆盖默认的「代号→管线名称」")
    systems: list[SystemGroup] = Field(default_factory=list)
    vscale: float | None = Field(default=None, description="高程竖向比例（图纸 mm / m），None 为自动")
    elev_frame: dict[str, float] = Field(default_factory=dict, description="布局结果：datum 基准高程(m)、base_y 基准的模型 y、vscale")
    hatches: list[Hatch] = Field(default_factory=list, description="填充")
    dimensions: list[Dimension] = Field(default_factory=list, description="尺寸")
    breaks: bool = Field(default=False, description="很长的空管段自动折断（双折线），见 cad/water.break_spans；默认不开")
    details: list[DetailView] = Field(default_factory=list, description="局部放大图")
    standard: str = Field(default="", description="制图标准配置 gb_general / hg_pfd / water_supply；空为按模板默认")
    binding: bool = Field(default=False, description="图框留装订边（GB/T 14689：左 a=25，其余 c）")
    # ---- 化工工艺流程图（文章第三章）----
    stage: Stage = Field(default="scheme", description="设计阶段：scheme 方案流程图 / material 物料流程图 / construction 施工流程图（管道及仪表流程图）")
    section_no: str = Field(default="", description="车间或工段号，两位数字（01 起），由工程总负责人给定，只能来自用户")
    tag_style: Literal["hg", "legacy"] = Field(default="hg", description="自动位号格式：hg = 分类号+工段号+序号（V0408A，文章图 2-4）；legacy = P-101")
    used_seqs: dict[str, int] = Field(default_factory=dict, description="每类设备用过的最大序号（删除设备后位号留空、不再复用）")
    sheet_no: int = Field(default=1, description="第几张（分张时）")
    sheet_total: int = Field(default=1, description="共几张")
    zones: bool | None = Field(default=None, description="图幅分区（GB/T 14689）：None 自动（A2 及以上、加长幅面画），True / False 强制")
    frame: bool = Field(default=True, description="画图框和标题栏；方案流程图可省略（文章图 3-1），物料、施工流程图必须画")
    ground: bool | None = Field(default=None, description="厂房地平线（细实线 + EL±0.000，文章 2.1 节）：None 自动（施工流程图，或有设备给了安装高度 / 地下设备时画）")
    repeats: list[RepeatSystem] = Field(default_factory=list, description="相同的局部系统（只画一套，其余画细双点画线方框）")
    instruments: list[Instrument] = Field(default_factory=list, description="仪表控制点（文章 3.2.4 节）")
    stream_tables: list[StreamTable] = Field(default_factory=list, description="物料组成表（物料流程图，文章图 3-2）")

    # ---- 通用 CAD（mode="general"）；旧流程图这些字段全是默认值，不写进 DXF 内嵌 IR ----
    mode: Literal["pfd", "general"] = Field(default="pfd", description="pfd 化工/给水流程图；general 通用二维/三维 CAD")
    workspace: str = "2d_drafting"
    layers: list[Layer] = Field(default_factory=list)
    current_layer: str = "0"
    entities: list[GeoEntity] = Field(default_factory=list)
    text_styles: list[dict] = Field(default_factory=list)
    current_text_style: str = "Standard"
    dim_styles: list[dict] = Field(default_factory=list)
    current_dim_style: str = "ISO-25"
    ltscale: float = 1.0
    celtscale: float = 1.0
    psltscale: int = 1
    limits: tuple[float, float, float, float] = (0, 0, 420, 297)
    limits_check: bool = False
    background: str = "#FFFFFF"
    point_style: dict = Field(default_factory=lambda: {"pdmode": 0, "pdsize": 0})
    blocks: list[dict] = Field(default_factory=list)
    xrefs: list[dict] = Field(default_factory=list)
    layouts: list[dict] = Field(default_factory=list)
    active_space: str = "Model"
    constraints: list[dict] = Field(default_factory=list)
    toolset: str = ""
    draft: DraftSettings = Field(default_factory=DraftSettings)
    view: ViewState = Field(default_factory=ViewState)
    session: SessionState = Field(default_factory=SessionState)
    mline_styles: list[dict] = Field(default_factory=list)
    dynamic_blocks: list[dict] = Field(default_factory=list)
    saved_dxf_version: str = "R2018"
    plot_setups: dict = Field(default_factory=dict, description="打印/出图设置：空间名（Model 或布局名）→ 设置")
    # ---- 化工设备图（任务书 4.1）：mode 仍是 general；有 vessel 时前端进入“化工设备图”工作空间，图由 zhihui/cad/equipment 从参数生成 ----
    vessel: VesselSpec | None = Field(default=None, description="容器参数（设备图的唯一真源）；None 表示不是设备图")

    # ---- 查找 ----
    def new_id(self, prefix: str) -> str:
        nid = f"{prefix}{self.next_seq}"
        self.next_seq += 1
        return nid

    def get_equipment(self, ref: str) -> Equipment | None:
        """按 id 或位号（不区分大小写）查找设备。"""
        for e in self.equipment:
            if e.id == ref:
                return e
        low = ref.strip().lower()
        for e in self.equipment:
            if e.tag.lower() == low:
                return e
        return None

    def get_connection(self, ref: str) -> Connection | None:
        return next((c for c in self.connections if c.id == ref), None)

    # ---- 通用图元 ----
    def get_entity(self, ref: str) -> GeoEntity | None:
        """按 id（不区分大小写）或 DXF 句柄查找通用图元。"""
        key = str(ref).strip()
        up = key.upper()
        return next((e for e in self.entities if e.id.upper() == up or (e.handle and e.handle.upper() == up)), None)

    def entities_on_layer(self, layer: str) -> list[GeoEntity]:
        return [e for e in self.entities if e.layer == layer]

    def get_layer(self, name: str) -> Layer | None:
        low = name.strip().lower()
        return next((l for l in self.layers if l.name.lower() == low), None)

    def layer_of(self, entity: GeoEntity) -> Layer | None:
        return self.get_layer(entity.layer)

    def bbox(self) -> tuple[float, float, float, float] | None:
        from zhihui.cad.geom import blocks as _blocks
        from zhihui.cad.geom.entities import entity_bbox

        _blocks.set_context(self.blocks)
        boxes = [b for b in (entity_bbox(e) for e in self.entities) if b]
        if not boxes:
            return None
        return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))

    def get_hatch(self, ref: str) -> Hatch | None:
        return next((h for h in self.hatches if h.id == ref.strip().upper()), None)

    def connections_of(self, equip_id: str) -> list[Connection]:
        return [c for c in self.connections if equip_id in (c.from_equip, c.to_equip)]

    # ---- 尺寸 ----
    @property
    def unit(self) -> float:
        """图元、文字、间距用的「图纸 mm → 模型 mm」系数：比例 × 图幅放大倍数。

        图元库按 A3 设计；换 A2/A1 时图元随图幅等比放大，版面观感不变（加长幅面按基本幅面算，A3×3 同 A3）。
        文字不跟着放大，按标准配置从字高系列里取（standards.text_h）；图框、标题栏按真实图纸尺寸（只乘 scale）。
        """
        base = BASE_FORMAT.get(self.paper_size, self.paper_size)
        return self.scale * PAPER_SIZES[base][0] / PAPER_SIZES["A3"][0]

    def frame_size(self) -> tuple[float, float]:
        w, h = PAPER_SIZES[self.paper_size]
        return w * self.scale, h * self.scale

    def topology_hash(self, fold_valves: bool = False) -> str:
        """拓扑校验码：只看设备集合和连接关系，不看坐标。

        fold_valves=True：「阀门折叠」校验码——一进一出的 valve 设备折叠成一条连接上的管件，管件也计入，
        所以把 valve 设备迁移成管件（migrate_valves）前后这个校验码不变；没有阀门和管件的图与普通校验码相同。"""
        tag = {e.id: e.tag for e in self.equipment}
        conns = [(c.from_equip, c.from_port, c.to_equip, c.to_port, [f.type for f in c.fittings]) for c in self.connections
                 if c.from_equip in tag and c.to_equip in tag]
        eqs = list(self.equipment)
        if fold_valves:
            for v in [e for e in eqs if e.symbol_id == "valve"]:
                ins = [c for c in conns if c[2] == v.id]
                outs = [c for c in conns if c[0] == v.id]
                if len(ins) == 1 and len(outs) == 1:
                    a, b = ins[0], outs[0]
                    conns = [c for c in conns if c is not a and c is not b] + [(a[0], a[1], b[2], b[3], a[4] + ["gate_valve"] + b[4])]
                    eqs.remove(v)
        items = sorted(
            [f"E:{e.id}:{e.symbol_id}" for e in eqs]
            + [f"C:{f}.{fp}>{t}.{tp}" + ("|F:" + ",".join(fs) if fs and fold_valves else "") for f, fp, t, tp, fs in conns]
        )
        return hashlib.sha256("\n".join(items).encode()).hexdigest()[:16]

    def to_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), ensure_ascii=False, indent=1)

    def compat_json(self) -> str:
        """写进 DXF 的 IR：新字段取默认值时省略，旧模板图纸的输出与新增字段之前一致。"""
        fresh = Drawing(id=self.id)
        exclude: dict = {k: True for k in _NEW_DRAWING_FIELDS if getattr(self, k) == getattr(fresh, k)}
        conns = {i: {k: True for k in _NEW_CONNECTION_FIELDS if not getattr(c, k)} for i, c in enumerate(self.connections)}
        conns = {i: v for i, v in conns.items() if v}
        if conns:
            exclude["connections"] = conns
        return self.model_dump_json(exclude=exclude)
