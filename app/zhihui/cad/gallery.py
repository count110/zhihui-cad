"""图元总览：python -m zhihui.cad.gallery

生成到 03_CAD算法与图元库/：
  图元总览.pdf / .png   所有图元按实际相对大小排列，标出端口（○ + 名称）、类别代号、出处
  图元清单.md           表格版，供化工专业逐项核对（改 JSON 后重新生成）
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import ezdxf
from ezdxf.enums import TextEntityAlignment

from .dxf_io import _define_block, _setup, _text
from .render import render
from .standards import LY
from .symbols import CATEGORIES, DIRS, load_symbols

OUT_DIR = Path(__file__).resolve().parents[2] / "03_CAD算法与图元库"
COLS, PAPER = 9, (1189.0, 841.0)  # A0 横向（补齐文章表 2-4 后 57 种，A1 格子太小）
STATUS_CN = {"standard": "按标准", "article": "按文章表 2-4", "simplified": "简化·待确认", "generated": "自动生成·待确认"}


def build(out_dir: Path = OUT_DIR) -> list[Path]:
    syms = sorted(load_symbols().values(), key=lambda s: ("TPCERFSVMX".index(s.category) if s.category in "TPCERFSVMX" else 99, s.family, s.id))
    doc = ezdxf.new("R2018", setup=True)
    _setup(doc)
    for name, color in (("GALLERY", 7), ("GALLERY_PORT", 1)):
        doc.layers.add(name, color=color)
    msp = doc.modelspace()
    W, H = PAPER
    msp.add_lwpolyline([(0, 0), (W, 0), (W, H), (0, H)], close=True, dxfattribs={"layer": LY["frame"]})
    msp.add_lwpolyline([(10, 10), (W - 10, 10), (W - 10, H - 10), (10, H - 10)], close=True, dxfattribs={"layer": LY["border"]})
    n_std = sum(s.status == "standard" for s in syms)
    n_art = sum(s.status == "article" for s in syms)
    _text(msp, f"智绘CAD 图元总览（共 {len(syms)} 种：按标准 {n_std}，按文章表 2-4 {n_art}，简化 {len(syms) - n_std - n_art}）", (20, H - 22), 7, "GALLERY")
    _text(msp, "依据 HG/T 20519.2-2009 表 8.0.6 / 表 9.0.5、文章表 2-4（未核对原文）；○ 为端口（连接点），箭头方向为管线引出方向；“简化·待确认”为标准未列入、按实物外形简化的图元，需化工专业核对。",
          (20, H - 30), 3, "GALLERY")
    rows = -(-len(syms) // COLS)
    cw, ch = (W - 20) / COLS, (H - 50) / rows
    for i, s in enumerate(syms):
        col, row = i % COLS, i // COLS
        cx0, cy1 = 10 + col * cw, H - 40 - row * ch
        msp.add_lwpolyline([(cx0, cy1), (cx0 + cw, cy1), (cx0 + cw, cy1 - ch), (cx0, cy1 - ch)], close=True, dxfattribs={"layer": LY["frame"]})
        x0, y0, x1, y1 = s.bbox
        ox = cx0 + cw / 2 - (x0 + x1) / 2
        oy = cy1 - 8 - (ch - 26) / 2 - (y0 + y1) / 2
        ref = msp.add_blockref(_define_block(doc, s.id, 1.0), (ox, oy), dxfattribs={"layer": LY["equip"]})
        ref.add_auto_attribs({"TAG": f"{s.category}-101", "NAME": "", "SYMBOL": s.id, "EID": "", "PARAMS": ""})
        from .named_regions import definitions
        from .symbols import SYMBOL_SIZE
        regions = definitions(s.id)
        for region_name, definition in regions.items():
            entity = msp.add_hatch(color=8)
            entity.set_pattern_fill('ANSI31', scale=1.5 if region_name == 'fill' else 0.8)
            entity.paths.add_polyline_path([(ox+x*SYMBOL_SIZE,oy+y*SYMBOL_SIZE) for x,y in definition['points']],is_closed=True)
            for hole in definition.get('holes',[]):
                entity.paths.add_polyline_path([(ox+x*SYMBOL_SIZE,oy+y*SYMBOL_SIZE) for x,y in hole],is_closed=True,flags=0)
        if regions:
            _text(msp, '区域: '+', '.join(regions), (cx0+3,cy1-13), 1.6, 'GALLERY')
        for p in s.ports:
            px, py = ox + p.x, oy + p.y
            vx, vy = DIRS[p.dir]
            msp.add_circle((px, py), 0.9, dxfattribs={"layer": "GALLERY_PORT"})
            msp.add_line((px, py), (px + vx * 4, py + vy * 4), dxfattribs={"layer": "GALLERY_PORT"})
            align = {"left": TextEntityAlignment.MIDDLE_RIGHT, "right": TextEntityAlignment.MIDDLE_LEFT,
                     "up": TextEntityAlignment.BOTTOM_CENTER, "down": TextEntityAlignment.TOP_CENTER}[p.dir]
            _text(msp, p.name, (px + vx * 5, py + vy * 5), 1.8, "GALLERY_PORT", align)
        _text(msp, f"{s.id}  {s.name_cn}", (cx0 + 3, cy1 - 5), 3, "GALLERY", TextEntityAlignment.TOP_LEFT)
        _text(msp, f"{s.category} {CATEGORIES.get(s.category, '')} · {STATUS_CN.get(s.status, s.status)}", (cx0 + 3, cy1 - 9.5), 2.2, "GALLERY", TextEntityAlignment.TOP_LEFT)
        for k, line in enumerate(textwrap.wrap(s.source, 34)[:3]):
            _text(msp, line, (cx0 + 3, cy1 - ch + 9 - k * 3), 1.8, "GALLERY", TextEntityAlignment.BOTTOM_LEFT)
    out_dir.mkdir(parents=True, exist_ok=True)
    dxf = out_dir / "图元总览.dxf"
    doc.saveas(dxf)
    pdf = render(dxf, out_dir / "图元总览.pdf", "A1")
    png = render(dxf, out_dir / "图元总览.png", "A1", dpi=80)
    dxf.unlink()

    lines = [
        "# 图元清单（自动生成，勿手改；改 `zhihui/cad/symbols/*.json` 后运行 `python -m zhihui.cad.gallery`）",
        "",
        f"共 {len(syms)} 种。核对时对照 `01_项目文档/参考标准/HG_T_20519.2-2009_工艺系统.pdf` 第 8 章，"
        "在「核对意见」列写结论（通过 / 需修改：说明）。",
        "",
        "| 图元 id | 名称 | 类别 | 状态 | 出处 | 端口（名称·进出·方向） | 常见叫法 | 核对意见 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for s in syms:
        ports = "<br>".join(f"{p.name}·{'进' if p.kind == 'in' else '出'}·{p.dir}" for p in s.ports)
        lines.append(f"| `{s.id}` | {s.name_cn} | {s.category} {CATEGORIES.get(s.category, '')} | {STATUS_CN.get(s.status, s.status)} | {s.source} | {ports} | {'、'.join(s.aliases)} |  |")
    md = out_dir / "图元清单.md"
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return [pdf, png, md]


if __name__ == "__main__":
    for p in build():
        print("已生成", p)
