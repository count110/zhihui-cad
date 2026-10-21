"""通用 CAD 图纸的预览与出图：PNG / SVG / PDF。

2D：把 IR 生成的 ezdxf 文档交给 ezdxf 绘图前端，颜色按图层/图元真实颜色，背景取 d.background，视窗取全图范围（或 window）。
3D：视图不是俯视（view3d != top）或有网格实体时，用 matplotlib 3D 画线框 / 消隐 / 着色三种视觉样式。
"""
from __future__ import annotations

import logging
import math
from pathlib import Path

import ezdxf
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from ezdxf import bbox  # noqa: E402
from ezdxf.addons.drawing import Frontend, RenderContext  # noqa: E402
from ezdxf.addons.drawing.config import BackgroundPolicy, ColorPolicy, Configuration, LineweightPolicy  # noqa: E402

from zhihui.ir import Drawing  # noqa: E402
from .dxf_io import cjk_font  # noqa: E402
from .general_dxf import build_general_doc  # noqa: E402
from .render import _Backend  # noqa: E402
from .geom.solid import feature_edges  # noqa: E402

logging.getLogger("ezdxf").setLevel(logging.WARNING)

ELEV_AZIM = {"top": (90, -90), "bottom": (-90, -90), "front": (0, -90), "back": (0, 90), "left": (0, 180), "right": (0, 0),
             "sw_iso": (35.264, -135), "se_iso": (35.264, -45), "ne_iso": (35.264, 45), "nw_iso": (35.264, 135)}


def _window(d: Drawing, doc, window) -> tuple[float, float, float, float]:
    if window:
        return tuple(window)                                    # type: ignore
    msp = doc.modelspace()
    ext = bbox.extents(msp, fast=True)
    lx0, ly0, lx1, ly1 = d.limits
    if ext.has_data:
        x0, y0, x1, y1 = min(lx0, ext.extmin.x), min(ly0, ext.extmin.y), max(lx1, ext.extmax.x), max(ly1, ext.extmax.y)
    else:
        x0, y0, x1, y1 = lx0, ly0, lx1, ly1
    m = max(x1 - x0, y1 - y0) * 0.02
    return x0 - m, y0 - m, x1 + m, y1 + m


def _fonts(doc) -> None:
    f = cjk_font()
    for st in doc.styles:
        if not st.dxf.font.lower().endswith(".shx"):
            st.dxf.font = f
            st.dxf.discard("bigfont")


def has_3d(d: Drawing) -> bool:
    return any(e.kind in ("mesh", "solid3d") for e in d.entities) or d.view.view3d != "top"


def render_2d(d: Drawing, out: Path, window=None, width_px: int = 1600, fmt: str | None = None) -> Path:
    doc = build_general_doc(d)
    _fonts(doc)
    x0, y0, x1, y1 = _window(d, doc, window)
    w, h = x1 - x0, y1 - y0
    dark = d.background.upper() in ("#000000", "#111111", "#1E1E1E")
    fig_w = width_px / 100
    fig = plt.figure(figsize=(fig_w, fig_w * h / w), facecolor=d.background)
    ax = fig.add_axes((0, 0, 1, 1))
    ax.set_facecolor(d.background)
    mm_per_unit = 25.4 * fig_w / w                                    # 画布每个模型单位多少毫米（用于最小线宽）
    cfg = Configuration(background_policy=BackgroundPolicy.BLACK if dark else BackgroundPolicy.WHITE, color_policy=ColorPolicy.COLOR,
                        lineweight_policy=LineweightPolicy.RELATIVE, lineweight_scaling=1.0, min_lineweight=0.25, hatching_timeout=10, min_hatch_line_distance=0.2 / max(mm_per_unit, 1e-6))
    Frontend(RenderContext(doc), _Backend(ax, adjust_figure=False), config=cfg).draw_layout(doc.modelspace(), finalize=True)
    ax.set_xlim(x0, x1)
    ax.set_ylim(y0, y1)
    ax.set_aspect("equal")
    _save(fig, out, fmt)
    return out


def _save(fig, out: Path, fmt: str | None, dpi: int = 100) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    suffix = (fmt or out.suffix.lstrip(".")).lower()
    if suffix == "svg":
        with matplotlib.rc_context({"svg.fonttype": "path"}):
            fig.savefig(out, format="svg", facecolor=fig.get_facecolor())
    elif suffix == "pdf":
        with matplotlib.rc_context({"pdf.fonttype": 42}):
            fig.savefig(out, format="pdf", facecolor=fig.get_facecolor())
    else:
        fig.savefig(out, dpi=min(dpi, int(4000 / max(fig.get_figwidth(), 1e-6))), facecolor=fig.get_facecolor())
    plt.close(fig)


# ------------------------------------------------------------------ 3D
def _segments_of(d: Drawing, e) -> list[list[tuple[float, float, float]]]:
    from .geom.entities import primitives, sample
    if e.kind in ("text", "mtext", "dimension", "hatch", "insert", "point", "tolerance", "leader", "ray", "xline"):
        return []
    out = []
    for pr in primitives(e):
        pts = sample(pr, 0.2)
        out.append([(p[0], p[1], p[2] if len(p) > 2 else 0.0) for p in pts])
    return out


def render_3d(d: Drawing, out: Path, width_px: int = 1400, fmt: str | None = None) -> Path:
    from mpl_toolkits.mplot3d.art3d import Line3DCollection, Poly3DCollection
    elev, azim = ELEV_AZIM.get(d.view.view3d, (d.view.orbit.get("elevation", 35.264), d.view.orbit.get("azimuth", -135)))
    if d.view.view3d == "custom":
        elev, azim = d.view.orbit.get("elevation", 35.264), d.view.orbit.get("azimuth", -135)
    style = d.view.visual_style
    dark = d.background.upper() in ("#000000", "#111111", "#1E1E1E")
    fg = "white" if dark else "black"
    fig = plt.figure(figsize=(width_px / 100, width_px / 100 * 0.75), facecolor=d.background)
    ax = fig.add_subplot(111, projection="3d", computed_zorder=False)
    ax.set_facecolor(d.background)
    ax.view_init(elev=elev, azim=azim)
    try:
        ax.set_proj_type("ortho" if d.view.projection == "parallel" else "persp")
    except Exception:
        pass
    allp: list[tuple[float, float, float]] = []
    lines: list = []
    for e in d.entities:
        lay = d.get_layer(e.layer)
        if lay and (not lay.on or lay.frozen):
            continue
        if e.kind in ("mesh", "solid3d"):
            V = [tuple(v) + ((0.0,) if len(v) == 2 else ()) for v in e.data["vertices"]]
            polys = [[V[i] for i in f] for f in e.data["faces"]]
            allp += V
            fe = feature_edges(V, e.data["faces"])               # 只画 CAD 意义上的棱线，不画三角剖分的对角线
            if style in ("wireframe", "2d_wireframe"):
                lines += [[V[i], V[j]] for i, j in fe]
            else:
                import numpy as np
                shade = style in ("shaded", "shaded_with_edges")
                if shade:
                    cols = []
                    for pg in polys:
                        a_, b_, c_ = (np.array(pg[0]), np.array(pg[1]), np.array(pg[2]))
                        n = np.cross(b_ - a_, c_ - a_)
                        nn = np.linalg.norm(n)
                        k = 0.55 + 0.45 * abs(n[2] / nn + 0.6 * n[0] / nn + 0.3 * n[1] / nn) / 1.9 if nn > 0 else 0.7
                        cols.append((0.62 * k, 0.72 * k, 0.84 * k, 1.0))
                    col = Poly3DCollection(polys, facecolors=cols, edgecolors=cols, linewidths=0.3)      # 边色同面色，盖住三角形之间的缝
                else:                                                                                     # hidden：面用背景色挡住后面的线
                    col = Poly3DCollection(polys, facecolors=d.background, edgecolors=d.background, linewidths=0.3)
                ax.add_collection3d(col)
                if style != "shaded":
                    lines += [[V[i], V[j]] for i, j in fe]
            continue
        for seg in _segments_of(d, e):
            lines.append(seg)
            allp += seg
    if lines:
        ax.add_collection3d(Line3DCollection(lines, colors=fg, linewidths=0.8))
    if allp:
        xs, ys, zs = zip(*allp)
        c = ((max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2, (max(zs) + min(zs)) / 2)
        r = max(max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs), 1.0) / 2 * 1.1
        ax.set_xlim(c[0] - r, c[0] + r)
        ax.set_ylim(c[1] - r, c[1] + r)
        ax.set_zlim(c[2] - r, c[2] + r)
    ax.set_axis_off()
    fig.subplots_adjust(0, 0, 1, 1)
    _save(fig, out, fmt)
    return out


def render_general(d: Drawing, out: str | Path, window=None, width_px: int = 1600) -> Path:
    out = Path(out)
    if d.view.view3d != "top" or any(e.kind in ("mesh", "solid3d") for e in d.entities):
        if d.view.view3d != "top" or d.view.visual_style != "2d_wireframe":
            return render_3d(d, out, width_px)
    return render_2d(d, out, window, width_px)


# ------------------------------------------------------------------ 布局与打印
def _cfg(color: bool, dark: bool = False, scale_hint: float = 1.0) -> Configuration:
    return Configuration(background_policy=BackgroundPolicy.BLACK if dark else BackgroundPolicy.WHITE, color_policy=ColorPolicy.COLOR if color else ColorPolicy.BLACK,
                         lineweight_policy=LineweightPolicy.ABSOLUTE, lineweight_scaling=1.0, hatching_timeout=10, min_hatch_line_distance=0.2 * scale_hint)


def _flat_lineweight(frontend, mm: float = 0.25) -> None:
    def fn(entity, props):
        props.lineweight = mm
    frontend.push_property_override_function(fn)


def _draw(ax, doc, layout, cfg, lineweights_on: bool) -> None:
    fe = Frontend(RenderContext(doc), _Backend(ax, adjust_figure=False), config=cfg)
    if not lineweights_on:
        _flat_lineweight(fe)
    fe.draw_layout(layout, finalize=True)


def render_layout(d: Drawing, layout_name: str, out: Path, setup: dict | None = None, show_viewports: bool = False) -> tuple[Path, dict]:
    """布局（图纸空间）：纸张 = 真实图幅（mm），图纸空间图元 + 每个视口里按比例裁剪的模型内容。"""
    from matplotlib.patches import Rectangle
    lay = next(x for x in d.layouts if x["name"] == layout_name)
    ps = {**lay["page_setup"], **(setup or {})}
    W, H = ps["size"]
    color = ps.get("color_mode", "monochrome") == "color"
    fig = plt.figure(figsize=(W / 25.4, H / 25.4), facecolor="white")
    ax = fig.add_axes((0, 0, 1, 1))
    ax.set_xlim(0, W)
    ax.set_ylim(0, H)
    doc = build_general_doc(d)
    _fonts(doc)
    _draw(ax, doc, doc.layouts.get(layout_name), _cfg(color), ps.get("lineweights_on", True))
    ax.set_xlim(0, W)                                           # draw_layout 会按内容重设坐标范围，这里恢复成整张纸
    ax.set_ylim(0, H)
    for vp in lay["viewports"]:
        (x0, y0), (x1, y1) = vp["corners"]
        vw, vh = abs(x1 - x0), abs(y1 - y0)
        vax = fig.add_axes((min(x0, x1) / W, min(y0, y1) / H, vw / W, vh / H))
        vdoc = build_general_doc(d)
        _fonts(vdoc)
        for name in vp.get("frozen_layers", []):
            if name in vdoc.layers:
                vdoc.layers.get(name).freeze()
        hh = float(vp["view_height"])
        hw = hh * vw / vh
        cx, cy = vp["target_center"]
        vax.set_xlim(cx - hw / 2, cx + hw / 2)
        vax.set_ylim(cy - hh / 2, cy + hh / 2)
        vax.set_axis_off()
        vax.patch.set_alpha(0.0)
        _draw(vax, vdoc, vdoc.modelspace(), _cfg(color, scale_hint=hh / vh), ps.get("lineweights_on", True))
        vax.set_xlim(cx - hw / 2, cx + hw / 2)
        vax.set_ylim(cy - hh / 2, cy + hh / 2)
        if show_viewports:
            ax.add_patch(Rectangle((min(x0, x1), min(y0, y1)), vw, vh, fill=False, edgecolor="#2E6FDB", linewidth=0.6, linestyle="--"))
    _save(fig, out, out.suffix.lstrip("."), ps.get("dpi", 150))
    return out, {"scale_text": "按视口", "paper": [W, H], "viewports": len(lay["viewports"])}


def plot(d: Drawing, space: str, ps: dict, out_dir: Path) -> tuple[Path, dict]:
    """按打印设置输出；返回 (文件路径, 说明)。Model：按打印范围和比例把模型放到纸上；布局：按布局渲染。"""
    from .geom import layout as L
    dev = ps.get("device", "pdf")
    out = Path(out_dir) / f"{d.id}_r{d.revision}_{space}_plot.{dev}"
    out.parent.mkdir(parents=True, exist_ok=True)
    if space != "Model":
        return render_layout(d, space, out, ps)
    W, H = ps["size"]
    doc = build_general_doc(d)
    _fonts(doc)
    msp = doc.modelspace()
    area = ps.get("plot_area", "extents")
    warnings: list[str] = []
    if area == "window":
        (x0, y0), (x1, y1) = ps["window"]
    elif area == "limits":
        x0, y0, x1, y1 = d.limits
    elif area == "display":
        hh = d.view.height
        hw = hh * W / H
        x0, y0, x1, y1 = d.view.center[0] - hw / 2, d.view.center[1] - hh / 2, d.view.center[0] + hw / 2, d.view.center[1] + hh / 2
    else:
        ext = bbox.extents(msp, fast=True)
        if not ext.has_data:
            raise ValueError("图中没有可打印的内容")
        x0, y0, x1, y1 = ext.extmin.x, ext.extmin.y, ext.extmax.x, ext.extmax.y
    aw, ah = max(x1 - x0, 1e-9), max(y1 - y0, 1e-9)
    m = 10.0
    sc = ps.get("plot_scale", "fit")
    if sc == "fit":
        s = min((W - 2 * m) / aw, (H - 2 * m) / ah)
        # 取不超过 fit 的标准比例，方便出图（找不到就用 fit 值并说明）
        std = [r for r in (L.scale_ratio(t) for t in L.STANDARD_SCALES) if r <= s + 1e-12]
        if std:
            s = max(std)
        scale_text = L.ratio_to_text(s)
    else:
        s = L.scale_ratio(sc)
        scale_text = sc
        if aw * s > W - 2 * m + 1e-6 or ah * s > H - 2 * m + 1e-6:
            warnings.append(f"按 {sc} 打印，图形范围 {aw * s:.0f}×{ah * s:.0f} mm 超出纸张可用区域 {W - 2 * m:.0f}×{H - 2 * m:.0f} mm，边缘会被裁掉")
    cx, cy = ((x0 + x1) / 2, (y0 + y1) / 2) if ps.get("center_plot", True) else (x0 + (W / 2 - m) / s, y0 + (H / 2 - m) / s)
    fig = plt.figure(figsize=(W / 25.4, H / 25.4), facecolor="white")
    ax = fig.add_axes((0, 0, 1, 1))
    _draw(ax, doc, msp, _cfg(ps.get("color_mode", "monochrome") == "color", scale_hint=1 / s), ps.get("lineweights_on", True))
    ax.set_xlim(cx - W / (2 * s), cx + W / (2 * s))
    ax.set_ylim(cy - H / (2 * s), cy + H / (2 * s))
    _save(fig, out, dev, ps.get("dpi", 150))
    return out, {"scale_text": scale_text, "paper": [W, H], "area": [x0, y0, x1, y1], "warnings": warnings}
