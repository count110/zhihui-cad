"""化工设备图的容器参数（任务书第 4 节）：设备图 = 通用 CAD 图纸 + 一份 VesselSpec，参数是唯一的真源，图由确定性生成器从参数生成。

单位：mm、MPa（表压，允许负值表示真空/外压）、℃、kg、m³。
存储模型（VesselSpec）和入参模型（VesselPatchIn）分开：Planner 和前端表单只能提交 VesselPatchIn，它不含 sources / pending / warnings，
也不暴露 P1/P2 的内件、换热器管壳程；sources / pending / warnings 只由 zhihui/cad/equipment/spec_ops.py 写。
所有嵌套模型都是 extra="forbid"（pydantic 默认会静默丢弃写错的字段名）。

注意命名：Drawing.equipment 是流程图的设备列表、coord/spec.py 里已有 EquipmentSpec，所以这里一律叫 vessel。
"""
from __future__ import annotations

from typing import Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

VesselKind = Literal["vertical_vessel", "horizontal_vessel",                     # P0
                     "hx_fixed_tubesheet", "stirred_reactor_jacketed",           # P1
                     "tray_column", "packed_column", "hx_floating_head", "hx_u_tube"]  # P2
P0_KINDS = ("vertical_vessel", "horizontal_vessel")
HeadKind = Literal["EHA", "HHA", "THA", "SDH", "FLAT", "CONE"]   # 前四个为 GB/T 25198-2023 代号（待核对原文）；FLAT/CONE 为本项目内部代号
HeadPos = Literal["top", "bottom", "left", "right"]
NozzleLoc = Literal["shell", "top_head", "bottom_head", "left_head", "right_head"]
NozzleKind = Literal["pipe", "manhole", "handhole", "sight_glass", "level_gauge"]
SupportKind = Literal["lug", "leg", "skirt", "saddle", "ring"]


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------------ 存储模型
class DesignData(_M):
    """设计数据表；全部“只照抄”，缺了写“待填”，不阻止出图。"""
    design_standard: Optional[str] = None
    vessel_category: Optional[str] = None
    working_pressure_mpa: Optional[float] = None
    design_pressure_mpa: Optional[float] = None
    working_temp_c: Optional[float] = None
    design_temp_c: Optional[float] = None
    medium: Optional[str] = None
    medium_property: Optional[str] = None
    main_material: Optional[str] = None
    corrosion_allowance_mm: Optional[float] = None
    weld_joint_coeff: Optional[float] = None
    heat_treatment: Optional[str] = None
    ndt: Optional[str] = None
    test_type: Optional[str] = None
    test_pressure_mpa: Optional[float] = None
    leak_test: Optional[str] = None
    insulation_thickness_mm: Optional[float] = None
    fill_coeff: Optional[float] = None
    design_life_years: Optional[float] = None
    wind_pressure_kpa: Optional[float] = None
    seismic: Optional[str] = None
    safety_valve_set_mpa: Optional[float] = None
    shell_side: Optional["DesignData"] = None          # 换热器壳程（P1）
    tube_side: Optional["DesignData"] = None           # 换热器管程（P1）


class Shell(_M):
    inner_diameter_mm: Optional[float] = None          # 必问（GB/T 9019 以内径为基准）
    length_mm: Optional[float] = None                  # 筒体长度（两条环焊缝之间）
    tangent_length_mm: Optional[float] = None          # 用户说“切线长/切线间距”时填这里，由 R-LEN-01 换算 length_mm
    thickness_mm: Optional[float] = None               # 名义厚度，必问
    material: Optional[str] = None


class Head(_M):
    position: HeadPos
    kind: Optional[HeadKind] = None                    # 缺省 R-HEAD-01：EHA
    thickness_mm: Optional[float] = None               # EHA/HHA/THA/SDH 缺省同筒体（R-HEAD-02）；FLAT/CONE 必问
    straight_flange_mm: Optional[float] = None         # 直边高度，缺省查封头表
    material: Optional[str] = None                     # 缺省同筒体（R-MAT-01）
    cone_half_angle_deg: Optional[float] = None        # CONE 必填（P2）


class Nozzle(_M):
    uid: Optional[str] = None                          # 程序内部的稳定编号（n1、n2…），sources 的键用它；入参不能写
    mark: Optional[str] = None                         # 用户说了才填；None 时由 assign_marks() 按 R-MARK-01 分配，分配后保持稳定
    dn: Optional[float] = None                         # 必问
    purpose: Optional[str] = None                      # 规范名（受控词表 data/nozzle_purposes.json）
    purpose_text: Optional[str] = None                 # 用户原话里的说法
    kind: NozzleKind = "pipe"
    location: Optional[NozzleLoc] = None               # 缺省 R-NOZ-LOC-01/02
    axial_mm: Optional[float] = None                   # 筒体管口：尺寸基准 → 管口轴线
    radial_mm: Optional[float] = None                  # 封头管口：设备轴线 → 管口轴线
    orientation_deg: Optional[float] = None            # 方位角（R-ORI-01）
    projection_mm: Optional[float] = None              # 伸出长度（定义见任务书 4.4）
    pipe_series: Optional[Literal["A", "B"]] = None
    pipe_od_mm: Optional[float] = None
    pipe_thk_mm: Optional[float] = None
    pipe_material: Optional[str] = None
    pn: Optional[float] = None
    flange_std: Optional[str] = None
    flange_type: Optional[str] = None
    facing: Optional[str] = None
    std_part: Optional[str] = None
    reinforcement: Literal["none", "pad", "auto"] = "auto"


class Support(_M):
    uid: Optional[str] = None                          # 程序内部的稳定编号（s1、s2…）
    kind: Optional[SupportKind] = None                 # 立式必问；卧式缺省鞍座
    count: Optional[int] = None                        # R-SUP-01
    model: Optional[str] = None                        # 设计条件齐全才初选（R-SUP-02）
    positions: Optional[list[dict]] = None             # 耳座 [{orientation_deg, elevation_mm}]；鞍座 [{axial_mm, fixed}]
    height_mm: Optional[float] = None                  # 支腿/裙座高度：必问
    material: Optional[str] = None


class Internals(_M):                                   # P1/P2；P0 只建模型，不画，也不在入参里暴露
    tubes: Optional[dict] = None
    baffles: Optional[dict] = None
    tubesheets: Optional[dict] = None
    trays: Optional[dict] = None
    packing: Optional[dict] = None
    agitator: Optional[dict] = None
    jacket: Optional[dict] = None


class VesselSpec(_M):
    kind: VesselKind
    tag: Optional[str] = None
    name: Optional[str] = None
    design: DesignData = Field(default_factory=DesignData)
    shell: Shell = Field(default_factory=Shell)
    heads: list[Head] = Field(default_factory=list)
    nozzles: list[Nozzle] = Field(default_factory=list)
    supports: list[Support] = Field(default_factory=list)
    internals: Internals = Field(default_factory=Internals)
    drawing: dict = Field(default_factory=dict)          # sheet、scale、orientation_convention、mark_style、title_block、marks_assigned、detached
    sources: dict[str, str] = Field(default_factory=dict)  # 字段路径 → "user:<轮次>" | "rule:<id>" | "table:<id>" | "calc:<id>" | "pfd:<位号>.<键>"
    pending: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @property
    def vertical(self) -> bool:
        return self.kind != "horizontal_vessel"


DesignData.model_rebuild()


# ------------------------------------------------------------------ 入参模型（Planner / 前端表单只能提交这个）
class Quantity(_M):
    """带单位的数：{"value": 1.6, "unit": "m"}，由程序换算（模型不做单位换算）。"""
    value: float
    unit: str


Num = Union[float, Quantity, str]          # 数、带单位的对象，或 "600kPa" 这样的字符串


class DesignDataIn(_M):
    design_standard: Optional[str] = None
    vessel_category: Optional[str] = None
    working_pressure_mpa: Optional[Num] = None
    design_pressure_mpa: Optional[Num] = None
    working_temp_c: Optional[Num] = None
    design_temp_c: Optional[Num] = None
    medium: Optional[str] = None
    medium_property: Optional[str] = None
    main_material: Optional[str] = None
    corrosion_allowance_mm: Optional[Num] = None
    weld_joint_coeff: Optional[Num] = None
    heat_treatment: Optional[str] = None
    ndt: Optional[str] = None
    test_type: Optional[str] = None
    test_pressure_mpa: Optional[Num] = None
    leak_test: Optional[str] = None
    insulation_thickness_mm: Optional[Num] = None
    fill_coeff: Optional[Num] = None
    design_life_years: Optional[Num] = None
    wind_pressure_kpa: Optional[Num] = None
    seismic: Optional[str] = None
    safety_valve_set_mpa: Optional[Num] = None


class ShellIn(_M):
    inner_diameter_mm: Optional[Num] = None
    length_mm: Optional[Num] = None
    tangent_length_mm: Optional[Num] = None
    thickness_mm: Optional[Num] = None
    material: Optional[str] = None


class HeadIn(_M):
    position: Optional[HeadPos] = None                 # 缺省作用于全部封头
    kind: Optional[HeadKind] = None
    thickness_mm: Optional[Num] = None
    straight_flange_mm: Optional[Num] = None
    material: Optional[str] = None


class NozzleIn(_M):
    mark: Optional[str] = None
    dn: Optional[Num] = None
    purpose: Optional[str] = None
    purpose_text: Optional[str] = None
    kind: Optional[NozzleKind] = None
    location: Optional[NozzleLoc] = None
    axial_mm: Optional[Num] = None
    radial_mm: Optional[Num] = None
    orientation_deg: Optional[Num] = None
    projection_mm: Optional[Num] = None
    pipe_series: Optional[Literal["A", "B"]] = None
    pipe_od_mm: Optional[Num] = None
    pipe_thk_mm: Optional[Num] = None
    pipe_material: Optional[str] = None
    pn: Optional[Num] = None
    flange_std: Optional[str] = None
    flange_type: Optional[str] = None
    facing: Optional[str] = None
    std_part: Optional[str] = None
    count: Optional[int] = Field(None, ge=1, le=20, description="同样的管口有几个（“两个液位计口DN25”写 2），由程序展开成多个管口")


class SupportIn(_M):
    kind: Optional[SupportKind] = None
    count: Optional[int] = Field(None, ge=1, le=12)
    model: Optional[str] = None
    height_mm: Optional[Num] = None
    material: Optional[str] = None


class DrawingIn(_M):
    sheet: Optional[Literal["A0", "A1", "A2", "A3"]] = None
    scale: Optional[str] = None


class VesselPatchIn(_M):
    kind: Optional[Literal["vertical_vessel", "horizontal_vessel"]] = None
    tag: Optional[str] = None
    name: Optional[str] = None
    design: Optional[DesignDataIn] = None
    shell: Optional[ShellIn] = None
    heads: Optional[list[HeadIn]] = None
    nozzles: Optional[list[NozzleIn]] = None
    supports: Optional[list[SupportIn]] = None
    drawing: Optional[DrawingIn] = None
