"""通用二维/三维 CAD 的 IR 扩展（图层、几何图元、绘图辅助、会话上下文、视图）。

只增不改：这些模型挂在 Drawing 的新字段上，旧流程图 JSON 无需任何迁移即可加载。
几何参数 data 的格式见 zhihui/cad/geom/entities.py。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Kind = Literal["point", "line", "ray", "xline", "mline", "polyline", "spline", "circle", "arc", "ellipse",
               "text", "mtext", "dimension", "hatch", "insert", "solid3d", "mesh", "tolerance", "leader"]

# 图元 id 前缀：点 PT、直线 L、射线 RY、构造线 XL、多线 ML、多段线 PL、样条 SP、圆 C、弧 A、椭圆 EL、
# 文字 T、多行文字 MT、标注 D、填充 H、块参照 B、实体 S、网格 MS、公差 TL、引线 LD
ID_PREFIX: dict[str, str] = {"point": "PT", "line": "L", "ray": "RY", "xline": "XL", "mline": "ML", "polyline": "PL", "spline": "SP",
                             "circle": "C", "arc": "A", "ellipse": "EL", "text": "T", "mtext": "MT", "dimension": "D", "hatch": "H",
                             "insert": "B", "solid3d": "S", "mesh": "MS", "tolerance": "TL", "leader": "LD"}


class Layer(BaseModel):
    name: str
    color: int = 7                                  # ACI
    true_color: str | None = None                   # "#RRGGBB"
    linetype: str = "Continuous"
    lineweight_mm: float | None = None              # None = 默认
    transparency: int = 0
    on: bool = True
    frozen: bool = False
    locked: bool = False
    plot: bool = True
    description: str = ""


class GeoEntity(BaseModel):
    id: str
    kind: Kind
    layer: str = "0"
    color: int | str | None = None                  # None = ByLayer
    linetype: str | None = None
    linetype_scale: float = 1.0
    lineweight_mm: float | None = None
    transparency: int | None = None
    data: dict = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)   # 语义标签：rectangle / polygon / centerline / hole / array:<group> ...
    handle: str | None = None                       # 导出 DXF 时的句柄；上传图纸保留原句柄
    space: str = "Model"                            # Model 或布局名
    created_rev: int = 0
    modified_rev: int = 0


class DraftSettings(BaseModel):
    snap_on: bool = False
    snap_spacing: tuple[float, float] = (10, 10)
    grid_on: bool = False
    grid_spacing: tuple[float, float] = (10, 10)
    ortho_on: bool = False
    polar_on: bool = False
    polar_angle_deg: float = 15
    osnap_on: bool = True
    osnap_modes: list[str] = Field(default_factory=lambda: ["endpoint", "midpoint", "center", "intersection", "quadrant", "perpendicular", "tangent"])
    otrack_on: bool = False
    dynmode_on: bool = True
    pointer_input_on: bool = True
    dimension_input_on: bool = True
    coords_display_mode: Literal["static", "continuous_xy", "continuous_polar"] = "continuous_xy"


class SessionState(BaseModel):
    """不导出到 DXF，用于多轮对话、指代消解和撤销重做。"""
    last_point: tuple[float, float, float] | None = None
    recent_entities: list[dict] = Field(default_factory=list)    # 栈顶在前，最多 50 条：{entity_id, type, layer, op, rev, summary}
    selection: list[str] = Field(default_factory=list)
    history: list[dict] = Field(default_factory=list)            # [{rev, prev_rev, tool, args, utterance}]
    redo: list[dict] = Field(default_factory=list)               # 被撤销的历史项（含 rev，重做时取该版内容）
    prev_views: list[dict] = Field(default_factory=list)         # zoom_previous 用


class ViewState(BaseModel):
    center: tuple[float, float] = (210, 148.5)
    height: float = 297
    named_views: dict[str, dict] = Field(default_factory=dict)
    view3d: str = "top"
    visual_style: str = "2d_wireframe"
    projection: str = "parallel"
    orbit: dict = Field(default_factory=lambda: {"azimuth": 0, "elevation": 90})
    panels: list[str] = Field(default_factory=lambda: ["command_line", "toolbar", "layers", "properties", "status_bar"])
    zoom_factor: float = 1.0
