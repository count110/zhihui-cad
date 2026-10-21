"""图层、线型、文字样式、标注样式、图纸、样板的预设（GB/T 4457.4、GB/T 14665、GB/T 14691、GB/T 4458.4 的常用取值）。"""
from __future__ import annotations

PAPERS = {"A4": (297, 210), "A3": (420, 297), "A2": (594, 420), "A1": (841, 594), "A0": (1189, 841)}

# ACI 颜色名
COLOR_NAMES = {"红": 1, "红色": 1, "黄": 2, "黄色": 2, "绿": 3, "绿色": 3, "青": 4, "青色": 4, "蓝": 5, "蓝色": 5, "洋红": 6, "品红": 6, "白": 7, "白色": 7, "黑": 7, "黑色": 7, "灰": 8, "灰色": 8,
               "red": 1, "yellow": 2, "green": 3, "cyan": 4, "blue": 5, "magenta": 6, "white": 7, "black": 7, "gray": 8, "grey": 8}

LINETYPE_PATTERNS = {   # ezdxf 标准库没有的，自己补（首项为总长，正为线、负为空）
    "HIDDEN": [9.525, 6.35, -3.175], "HIDDEN2": [4.7625, 3.175, -1.5875], "HIDDENX2": [19.05, 12.7, -6.35],
}
LINETYPE_ALIASES = {"实线": "Continuous", "连续": "Continuous", "continuous": "Continuous", "虚线": "DASHED", "点画线": "CENTER", "中心线": "CENTER", "点划线": "CENTER",
                    "双点画线": "PHANTOM", "双点划线": "PHANTOM", "隐藏线": "HIDDEN", "细虚线": "HIDDEN", "点线": "DOT", "dashed": "DASHED", "center": "CENTER", "hidden": "HIDDEN",
                    "phantom": "PHANTOM", "dashdot": "DASHDOT", "dot": "DOT", "divide": "DIVIDE"}
KNOWN_LINETYPES = {"Continuous", "CENTER", "CENTER2", "CENTERX2", "DASHED", "DASHED2", "DASHEDX2", "PHANTOM", "PHANTOM2", "PHANTOMX2", "DASHDOT", "DASHDOT2", "DASHDOTX2",
                   "DOT", "DOT2", "DOTX2", "DIVIDE", "DIVIDE2", "DIVIDEX2", "HIDDEN", "HIDDEN2", "HIDDENX2", "ByLayer", "ByBlock"}

LAYER_PRESETS: dict[str, list[dict]] = {
    # GB/T 4457.4：粗实线 0.5（宽度比 2:1 取 0.5/0.25），虚线、点画线、剖面线、尺寸、文字
    "GB_MECH": [
        {"name": "粗实线", "color": 7, "linetype": "Continuous", "lineweight_mm": 0.5, "description": "可见轮廓线"},
        {"name": "细实线", "color": 7, "linetype": "Continuous", "lineweight_mm": 0.25, "description": "尺寸线、引出线、过渡线"},
        {"name": "虚线", "color": 2, "linetype": "DASHED", "lineweight_mm": 0.25, "description": "不可见轮廓线"},
        {"name": "中心线", "color": 1, "linetype": "CENTER", "lineweight_mm": 0.25, "description": "细点画线：轴线、对称中心线"},
        {"name": "剖面线", "color": 7, "linetype": "Continuous", "lineweight_mm": 0.25, "description": "剖面符号"},
        {"name": "尺寸标注", "color": 3, "linetype": "Continuous", "lineweight_mm": 0.25, "description": "尺寸、公差"},
        {"name": "文字", "color": 7, "linetype": "Continuous", "lineweight_mm": 0.25, "description": "文字说明、技术要求"},
    ],
    "GB_ARCH": [
        {"name": "墙体", "color": 7, "linetype": "Continuous", "lineweight_mm": 0.5},
        {"name": "门窗", "color": 4, "linetype": "Continuous", "lineweight_mm": 0.25},
        {"name": "轴线", "color": 1, "linetype": "CENTER", "lineweight_mm": 0.18},
        {"name": "标注", "color": 3, "linetype": "Continuous", "lineweight_mm": 0.18},
        {"name": "文字", "color": 7, "linetype": "Continuous", "lineweight_mm": 0.18},
        {"name": "家具", "color": 5, "linetype": "Continuous", "lineweight_mm": 0.18},
        {"name": "填充", "color": 8, "linetype": "Continuous", "lineweight_mm": 0.13},
    ],
    "ISO": [
        {"name": "Visible", "color": 7, "linetype": "Continuous", "lineweight_mm": 0.5},
        {"name": "Hidden", "color": 2, "linetype": "HIDDEN", "lineweight_mm": 0.25},
        {"name": "Center", "color": 1, "linetype": "CENTER", "lineweight_mm": 0.25},
        {"name": "Dimensions", "color": 3, "linetype": "Continuous", "lineweight_mm": 0.25},
        {"name": "Text", "color": 7, "linetype": "Continuous", "lineweight_mm": 0.25},
        {"name": "Hatch", "color": 8, "linetype": "Continuous", "lineweight_mm": 0.13},
    ],
}
# 默认“当前图层”：预设里第一项（机械/建筑/ISO 的主轮廓层）
PRESET_CURRENT = {"GB_MECH": "粗实线", "GB_ARCH": "墙体", "ISO": "Visible"}

TEXT_STYLES = {
    # 长仿宋：宽度因子 0.7；SHX 方案 gbenor.shx + gbcbig.shx（AutoCAD 自带），预览用仿宋 TTF
    "GB_仿宋": {"name": "GB_仿宋", "font_file": "仿宋", "bigfont_file": "", "fixed_height": 0.0, "width_factor": 0.7, "oblique_angle_deg": 0.0, "annotative": False},
    "GB_SHX": {"name": "GB_SHX", "font_file": "gbenor.shx", "bigfont_file": "gbcbig.shx", "fixed_height": 0.0, "width_factor": 1.0, "oblique_angle_deg": 0.0, "annotative": False},
    "Standard": {"name": "Standard", "font_file": "txt.shx", "bigfont_file": "", "fixed_height": 0.0, "width_factor": 1.0, "oblique_angle_deg": 0.0, "annotative": False},
}

DIM_STYLES = {
    "GB_MECH_MM": {"name": "GB_MECH_MM", "arrow_type": "closed_filled", "arrow_size": 3.5, "text_height": 3.5, "text_style": "GB_仿宋", "ext_offset": 1.0, "ext_beyond": 2.0,
                   "precision": 2, "dimscale": 1.0, "tolerance_display": "none", "base_spacing": 7.0},
    "GB_ARCH_MM": {"name": "GB_ARCH_MM", "arrow_type": "architectural_tick", "arrow_size": 2.0, "text_height": 3.0, "text_style": "GB_仿宋", "ext_offset": 2.0, "ext_beyond": 2.0,
                   "precision": 0, "dimscale": 1.0, "tolerance_display": "none", "base_spacing": 8.0},
    "ISO_25": {"name": "ISO-25", "arrow_type": "closed_filled", "arrow_size": 2.5, "text_height": 2.5, "text_style": "Standard", "ext_offset": 0.625, "ext_beyond": 1.25,
               "precision": 2, "dimscale": 1.0, "tolerance_display": "none", "base_spacing": 3.75},
}

TEMPLATES = {
    "blank_a4": {"paper": "A4", "layers": None}, "blank_a3": {"paper": "A3", "layers": None}, "blank_a2": {"paper": "A2", "layers": None},
    "blank_a1": {"paper": "A1", "layers": None}, "blank_a0": {"paper": "A0", "layers": None},
    "gb_a4_mech": {"paper": "A4", "layers": "GB_MECH", "text": "GB_仿宋", "dim": "GB_MECH_MM"},
    "gb_a3_mech": {"paper": "A3", "layers": "GB_MECH", "text": "GB_仿宋", "dim": "GB_MECH_MM"},
    "gb_a2_mech": {"paper": "A2", "layers": "GB_MECH", "text": "GB_仿宋", "dim": "GB_MECH_MM"},
    "gb_a3_arch": {"paper": "A3", "layers": "GB_ARCH", "text": "GB_仿宋", "dim": "GB_ARCH_MM"},
    "iso_a3": {"paper": "A3", "layers": "ISO", "text": "Standard", "dim": "ISO_25"},
    # 化工设备图（任务书 6.2-5）：A1、GB 机械图层 + 图框层；设备参数和图由 vessel_define 生成
    "hg_equipment_a1": {"paper": "A1", "layers": "GB_MECH", "text": "GB_仿宋", "dim": "GB_MECH_MM", "equipment": True},
}


def nice_ltscale(limits: tuple[float, float, float, float]) -> tuple[float, str]:
    """按图形界限推荐线型比例：把界限折算成“某个 ISO 图幅 × 标准出图比例”，比例即 LTSCALE。"""
    import math

    w, h = abs(limits[2] - limits[0]), abs(limits[3] - limits[1])
    diag = math.hypot(w, h)
    scales = (1, 2, 5, 10, 20, 50, 100, 200, 500, 1000)
    best = None
    for name, (pw, ph) in PAPERS.items():
        r = diag / math.hypot(pw, ph)
        s = min(scales, key=lambda x: abs(math.log(r / x)))
        err = abs(math.log(r / s))
        if best is None or err < best[0]:
            best = (err, s, name)
    return float(best[1]), f"界限 {w:g}×{h:g} 接近 {best[2]} 图幅按 1:{best[1]} 出图，线型比例取 {best[1]}"
