"""把 DXF 渲染成 PNG / PDF 预览（直接读导出的 DXF，证明文件本身是对的）。"""

from __future__ import annotations

import logging
from pathlib import Path

import ezdxf
from ezdxf import bbox
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from ezdxf.addons.drawing import Frontend, RenderContext  # noqa: E402
from ezdxf.addons.drawing.config import BackgroundPolicy, ColorPolicy, Configuration, LineweightPolicy  # noqa: E402
from ezdxf.addons.drawing.matplotlib import MatplotlibBackend  # noqa: E402
from ezdxf.npshapes import to_matplotlib_path  # noqa: E402
from matplotlib.patches import PathPatch  # noqa: E402

from zhihui.ir import PAPER_SIZES  # noqa: E402

from .dxf_io import LATIN_STYLE, SYM_STYLE, TEXT_STYLE, cjk_font  # noqa: E402

logging.getLogger("ezdxf").setLevel(logging.WARNING)  # 渲染时会逐个报告跳过的不可见属性，太吵


class _Backend(MatplotlibBackend):
    """文字按 TrueType 字形自身的绕向（非零环绕）填充。

    ezdxf 默认用「嵌套关系」判断孔洞，仿宋这类笔画轮廓互相重叠的字体会被误判，「图」等字的一部分被填实；
    字形轮廓本身方向就是对的（外圈顺时针、孔逆时针），直接交给 matplotlib 的非零环绕规则即可。填充、实心等其他图形照旧。
    """

    _text = False

    def enter_entity(self, entity, properties):
        self._text = entity.dxftype() in ("TEXT", "MTEXT", "ATTRIB", "ATTDEF")
        super().enter_entity(entity, properties)

    def draw_filled_paths(self, paths, properties):
        if not self._text:
            return super().draw_filled_paths(paths, properties)
        try:
            patch = PathPatch(to_matplotlib_path(list(paths), detect_holes=False), color=properties.color, linewidth=0, fill=True, zorder=self._get_z())
        except ValueError:
            return
        self.ax.add_patch(patch)


def _font_path() -> str | None:
    """预览字体（仿宋）的文件路径，给 PDF 的可搜索文字层用。"""
    from matplotlib import font_manager

    name = cjk_font()
    if Path(name).is_file():
        return name
    for f in font_manager.findSystemFonts():
        if Path(f).name.lower() == Path(name).name.lower():
            return f
    return None


def _figure(doc, paper_size: str):
    """一页图：画布 = 真实图幅（mm → 英寸），黑白，线宽按图层线宽的绝对毫米数。返回 (fig, ax, 每图纸 mm 的模型单位)。"""
    for name in (TEXT_STYLE, LATIN_STYLE):
        if name in doc.styles:
            # 只改内存里的样式，让服务器上有的中文字体顶替；不影响交付的 DXF
            style = doc.styles.get(name)
            style.dxf.font = cjk_font()
            style.dxf.discard("bigfont")
    if SYM_STYLE in doc.styles:  # ²、³：宋体有这两个字形（仿宋、黑体没有）
        from ezdxf.fonts import fonts

        sym = doc.styles.get(SYM_STYLE).dxf.font
        if not fonts.font_manager.has_font(sym):
            doc.styles.get(SYM_STYLE).dxf.font = cjk_font()
    w_mm, h_mm = PAPER_SIZES.get(paper_size, PAPER_SIZES["A3"])
    fig = plt.figure(figsize=(w_mm / 25.4, h_mm / 25.4))
    ax = fig.add_axes((0, 0, 1, 1))
    msp = doc.modelspace()
    ext = bbox.extents(msp, fast=True)  # 外框（纸边线）就是图纸范围
    model_per_mm = (ext.extmax.x - ext.extmin.x) / w_mm if ext.has_data else 1.0
    # 黑白出图；画布即真实图幅，线宽按 DXF 图层线宽的绝对毫米数绘制（粗 0.7 / 中粗 0.35 / 细 0.25）。
    # 填充：最多算 10 s；图案线距小于图纸 0.2 mm 的改画实心，免得密图案拖慢或糊成一团黑
    cfg = Configuration(
        background_policy=BackgroundPolicy.WHITE,
        color_policy=ColorPolicy.BLACK,
        lineweight_policy=LineweightPolicy.ABSOLUTE,
        lineweight_scaling=1.0,
        hatching_timeout=10,
        min_hatch_line_distance=0.2 * model_per_mm,
    )
    # 不让 ezdxf 自动改画布尺寸：画布 = 真实图幅，绝对线宽才对得上
    Frontend(RenderContext(doc), _Backend(ax, adjust_figure=False), config=cfg).draw_layout(msp, finalize=True)
    if ext.has_data:
        ax.set_xlim(ext.extmin.x, ext.extmax.x)
        ax.set_ylim(ext.extmin.y, ext.extmax.y)
    return fig, ax, model_per_mm


def _texts(doc):
    """图上所有文字实体（含块里的文字、属性、尺寸数字）。"""
    for e in doc.modelspace():
        t = e.dxftype()
        if t in ("TEXT", "MTEXT"):
            yield e
        elif t == "INSERT":
            for a in e.attribs:
                if not (a.dxf.flags & 1) and a.dxf.text.strip():
                    yield a
            for v in e.virtual_entities():
                if v.dxftype() in ("TEXT", "MTEXT"):
                    yield v
        elif t == "DIMENSION":
            for v in e.virtual_entities():
                if v.dxftype() in ("TEXT", "MTEXT"):
                    yield v


def _text_layer(ax, doc, model_per_mm: float) -> int:
    """PDF 可搜索、可复制：在字形曲线上叠一层透明的真文字（字体嵌入为 TrueType），位置和大小与图上文字一致。"""
    from matplotlib.font_manager import FontProperties

    fp = _font_path()
    prop = FontProperties(fname=fp) if fp else FontProperties()
    import os

    sym_path = os.path.join(os.environ.get("WINDIR", "C:/Windows"), "Fonts", "simsun.ttc")
    sym_prop = FontProperties(fname=sym_path) if os.path.exists(sym_path) else prop  # ²、³：仿宋没有这两个字形（同 ZH_SYM 样式）
    n = 0
    for e in _texts(doc):
        try:
            text = e.plain_text() if e.dxftype() == "MTEXT" else e.plain_text()
        except AttributeError:
            text = e.dxf.get("text", "")
        text = text.strip()
        if not text:
            continue
        ext = bbox.extents([e], fast=True)
        if not ext.has_data:
            continue
        h = e.dxf.get("char_height", None) if e.dxftype() == "MTEXT" else e.dxf.get("height", 2.5)
        pt = (h or 2.5) / model_per_mm / 25.4 * 72
        rot = e.dxf.get("rotation", 0.0) or 0.0
        ax.text(ext.extmin.x, ext.extmin.y, text, fontproperties=sym_prop if any(ch in "²³" for ch in text) else prop, fontsize=pt, rotation=rot,
                ha="left", va="bottom", alpha=0.0, rotation_mode="anchor")
        n += 1
    return n


def render(dxf_path: str | Path, out_path: str | Path, paper_size: str = "A3", dpi: int = 150) -> Path:
    """按扩展名输出 .png / .svg / .pdf（PDF 带可搜索文字层）。"""
    out_path = Path(out_path)
    if out_path.suffix.lower() == ".pdf":
        return render_pdf([dxf_path], out_path, paper_size)
    doc = ezdxf.readfile(str(dxf_path))
    fig, _, _ = _figure(doc, paper_size)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.suffix.lower() == ".svg":  # 矢量预览：网页里无限放大都清晰；文字已是字形轮廓，不依赖字体
        with matplotlib.rc_context({"svg.fonttype": "path"}):
            fig.savefig(out_path, facecolor="white")
        plt.close(fig)
        return out_path
    w_mm, _ = PAPER_SIZES.get(paper_size, PAPER_SIZES["A3"])
    dpi = min(dpi, int(3600 / (w_mm / 25.4)))  # 大图幅限制 PNG 像素
    fig.savefig(out_path, dpi=dpi, facecolor="white")
    plt.close(fig)
    return out_path


def render_pdf(dxf_paths: list[str | Path], out_path: str | Path, paper_size: str = "A3") -> Path:
    """多张图合成一个多页 PDF：每页按真实图幅（mm），黑白；文字是嵌入字体的真文字（透明层），能搜索和复制。"""
    from matplotlib.backends.backend_pdf import PdfPages

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with matplotlib.rc_context({"pdf.fonttype": 42}), PdfPages(out_path, metadata={"Creator": "智绘CAD"}) as pdf:
        for p in dxf_paths:
            doc = ezdxf.readfile(str(p))
            fig, ax, mpm = _figure(doc, paper_size)
            _text_layer(ax, doc, mpm)
            pdf.savefig(fig, facecolor="white")
            plt.close(fig)
    return out_path
