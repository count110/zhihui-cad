"""规范自检报告（S9）：每次出图后对 DXF 逐项检查，给出标准出处和「符合项 / 总项」得分。

- 错误项（字高不合规、比例不在系列里、文字压线、尺寸重复……）由 validate 转成 errors，进入「校验 → 发回模型修复」循环；
- 警告项（自由文字没有关联对象、标题栏有格子没填……）只写进报告，不拦截出图；
- export_artifacts 导出 JSON + Markdown，前端展示。

检查的是最终 DXF（build_doc 的结果），不是 IR：所见即所查。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from zhihui.ir import Drawing

from .standards import LY, for_drawing, is_cjk

PASS, WARN, ERROR = "pass", "warning", "error"
EPS = 1e-3


@dataclass
class Item:
    id: str
    name: str
    source: str  # 标准号 + 条款
    status: str = PASS
    details: list[str] = field(default_factory=list)
    refs: list[str] = field(default_factory=list)
    hint: str = ""
    verified: bool | None = None  # 出处是否已与标准原文核对；None 时按 source 自动判定（见 _verified）

    def fail(self, level: str, msg: str, ref: str = "") -> None:
        if self.status != ERROR:
            self.status = level
        if len(self.details) < 20:
            self.details.append(msg)
        if ref and ref not in self.refs:
            self.refs.append(ref)


# ---------------------------------------------------------------- 文字

def _all_texts(doc):
    """(文字实体, 所属块参照句柄或 None)：模型空间文字、块属性、块里的文字、尺寸数字。"""
    for e in doc.modelspace():
        t = e.dxftype()
        if t in ("TEXT", "MTEXT"):
            yield e, None
        elif t == "INSERT":
            for a in e.attribs:
                if not (a.dxf.flags & 1) and a.dxf.text.strip():
                    yield a, e.dxf.handle
            for v in e.virtual_entities():
                if v.dxftype() in ("TEXT", "MTEXT"):
                    yield v, e.dxf.handle
        elif t == "DIMENSION":
            for v in e.virtual_entities():
                if v.dxftype() in ("TEXT", "MTEXT"):
                    yield v, e.dxf.handle


def _content(e) -> str:
    try:
        return e.plain_text().strip()
    except AttributeError:
        return str(e.dxf.get("text", "")).strip()


def _height(e) -> float:
    return float(e.dxf.get("char_height", 0) if e.dxftype() == "MTEXT" else e.dxf.get("height", 0))


def _xdata(e) -> list[str]:
    try:
        return [v for _, v in e.get_xdata("ZHIHUI")] if e.has_xdata("ZHIHUI") else []
    except Exception:  # 虚拟实体没有 XDATA
        return []


def _text_boxes(doc):
    """[(文字, 外框 (x0,y0,x1,y1), 所属块句柄, 实体)]；外框按预览字体（仿宋）实测。"""
    from ezdxf import bbox

    from .dxf_io import TEXT_STYLE, cjk_font

    if TEXT_STYLE in doc.styles:  # 和预览一致：SHX 本机没有，按仿宋量
        st = doc.styles.get(TEXT_STYLE)
        st.dxf.font = cjk_font()
        st.dxf.discard("bigfont")
    out = []
    for e, owner in _all_texts(doc):
        s = _content(e)
        if not s:
            continue
        ext = bbox.extents([e], fast=False)
        if ext.has_data:
            out.append((s, (ext.extmin.x, ext.extmin.y, ext.extmax.x, ext.extmax.y), owner, e))
    return out


def _shrink(b, k):
    return (b[0] + k, b[1] + k, b[2] - k, b[3] - k)


def _seg_hits_box(a, b, box) -> bool:
    """线段是否穿过矩形内部（只碰边不算）。Liang–Barsky 裁剪。"""
    x0, y0, x1, y1 = box
    if x1 <= x0 or y1 <= y0:
        return False
    dx, dy = b[0] - a[0], b[1] - a[1]
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, a[0] - x0), (dx, x1 - a[0]), (-dy, a[1] - y0), (dy, y1 - a[1])):
        if abs(p) < 1e-12:
            if q <= 0:
                return False
        else:
            r = q / p
            if p < 0:
                t0 = max(t0, r)
            else:
                t1 = min(t1, r)
            if t0 >= t1:
                return False
    return t1 - t0 > 1e-9


# ---------------------------------------------------------------- 检查

def run(d: Drawing, doc=None) -> dict:
    """对出图结果逐项检查，返回报告（dict，可直接转 JSON）。"""
    from .dxf_io import TEXT_STYLE, build_doc
    from .linework import check as line_check, collect
    from .standards import layer_table
    from .symbols import inner_frame

    doc = doc or build_doc(d)
    std = for_drawing(d)
    s = d.scale
    items: list[Item] = []
    msp = doc.modelspace()

    # 1 图幅与留边
    it = Item("paper", "图幅与图框留边", "GB/T 14689-2008 第 3.1 条（幅面尺寸）、第 3.2 条（图框格式 a / c / e）")
    if not std.is_paper(d.paper_size):
        it.fail(ERROR, f"图幅 {d.paper_size} 不在 GB/T 14689 的基本 / 加长幅面里")
    fx0, fy0, fx1, fy1 = inner_frame(d)
    border = [e for e in msp.query("LWPOLYLINE") if e.dxf.layer == LY["border"] and e.closed]
    if not d.frame:  # 方案流程图可以省略图框和标题栏（文章 3.1.1 节、图 3-1）：只画纸边线
        it.details.append(f"{d.paper_size}，方案流程图省略图框和标题栏（文章 3.1.1 节），只画纸边线")
    elif not border:
        it.fail(ERROR, "没有找到图框线（ZH-图框 层的闭合多段线）")
    else:
        xs = [p[0] for p in border[0].get_points("xy")]
        ys = [p[1] for p in border[0].get_points("xy")]
        if max(abs(min(xs) - fx0), abs(max(xs) - fx1), abs(min(ys) - fy0), abs(max(ys) - fy1)) > 0.01 * s:
            it.fail(ERROR, "图框线位置与 GB/T 14689 的留边尺寸不一致")
        left, bottom, right, top = std.margins(d.paper_size, d.binding)
        it.details.append(f"{d.paper_size}，留边 左 {left:g} / 下 {bottom:g} / 右 {right:g} / 上 {top:g} mm{'（装订）' if d.binding else ''}") if it.status == PASS else None
    items.append(it)

    # 2 比例
    it = Item("scale", "比例", "GB/T 14690-1993 第 4 条表 1、表 2（优先 / 允许的比例系列）")
    if not std.is_series_scale(d.scale):
        it.fail(ERROR, f"比例 1:{d.scale:g} 不在系列里", "scale")
    if d.template_id == "water_elevation" and d.elev_frame and d.elev_frame.get("vscale"):
        from .water import vertical_ratio

        n = vertical_ratio(d.elev_frame["vscale"])
        if not std.is_series_scale(n):
            it.fail(ERROR, f"竖向比例 1:{n:g} 不在系列里", "vscale")
    for dv in d.details:
        if not std.is_series_scale(1 / dv.factor):
            it.fail(ERROR, f"局部放大图 {dv.id} 的比例 {dv.factor:g}:1 不在系列里", dv.id)
    items.append(it)

    # 3、4 字高系列 / 汉字最小字高；5 字体
    texts = list(_all_texts(doc))
    series = set(std.raw["text"]["heights"])
    cjk_min = float(std.raw["text"]["cjk_min"])
    it_h = Item("text_height", "字高系列", "GB/T 14691-1993 第 3.2 条（字高 1.8、2.5、3.5、5、7、10、14、20 mm）")
    it_cjk = Item("cjk_height", "汉字最小字高", "GB/T 14691-1993 第 3.3 条（汉字高度不应小于 3.5 mm）")
    it_font = Item("font", "字体（汉字长仿宋、字母数字 B 型斜体，型式唯一）",
                   "GB/T 14691-1993 第 3.3 条（长仿宋，字宽 h/√2）、第 3.4 条（同一图样只用一种型式）、第 3.5 条（斜体与水平线成 75°）")
    from .dxf_io import LATIN_STYLE, dxf_font, text_style

    want_font, want_big = dxf_font(std)
    for name in (TEXT_STYLE, LATIN_STYLE):
        st = doc.styles.get(name) if name in doc.styles else None
        if st is None:
            it_font.fail(ERROR, f"没有文字样式 {name}")
            continue
        if (st.dxf.font or "").lower() != want_font.lower() or (st.dxf.get("bigfont", "") or "").lower() != want_big.lower():
            it_font.fail(ERROR, f"文字样式 {name} 字体 {st.dxf.font} + {st.dxf.get('bigfont', '') or '无大字体'}，应为 {want_font}" + (f" + {want_big}" if want_big else ""))
    st = doc.styles.get(TEXT_STYLE) if TEXT_STYLE in doc.styles else None
    if st is not None and abs(float(st.dxf.width) - float(std.raw["text"]["width_factor"])) > 0.01:
        it_font.fail(ERROR, f"汉字样式宽度因子 {st.dxf.width:g}，应为 {std.raw['text']['width_factor']}（h/√2）")
    if std.raw["text"].get("latin_type", "B") not in ("A", "B"):
        it_font.fail(ERROR, "字母数字型式只能是 A 型或 B 型")
    if it_font.status == PASS:
        it_font.details.append(f"{want_font}" + (f" + {want_big}" if want_big else "（TrueType 仿宋）")
                               + f"；汉字宽度因子 {std.raw['text']['width_factor']}；字母数字 {std.raw['text'].get('latin_type', 'B')} 型"
                               + ("、向右倾斜 15°（75°）" if text_style("A", std)[2] else ""))
    for e, _ in texts:
        txt = _content(e)
        if not txt:
            continue
        h = _height(e) / s
        if not any(abs(h - v) < 0.01 for v in series):
            it_h.fail(ERROR, f"「{txt[:12]}」字高 {h:.2f} mm 不在系列里", txt[:12])
        if is_cjk(txt) and h < cjk_min - 0.01:
            it_cjk.fail(ERROR, f"「{txt[:12]}」含汉字，字高 {h:.2f} mm < 3.5", txt[:12])
        style = e.dxf.get("style", "Standard")
        if e.dxftype() in ("TEXT", "ATTRIB"):
            want, _, obl = text_style(txt, std)
            if style != want:
                it_font.fail(ERROR, f"「{txt[:12]}」用了文字样式 {style}，应为 {want}（{'含汉字用正体' if want == TEXT_STYLE else '字母数字用 B 型斜体'}）", txt[:12])
            elif obl and abs(float(e.dxf.get("oblique", 0)) - obl) > 0.5:
                it_font.fail(ERROR, f"「{txt[:12]}」字母数字没有写成斜体（应向右倾斜 {obl:g}°）", txt[:12])
    items += [it_h, it_cjk, it_font]

    # 6 线宽组
    it = Item("lineweight", "线宽系列与 4:2:1", "GB/T 17450-1998 第 4.1 条（线宽 0.13～2 mm 系列；粗:中粗:细 = 4:2:1；同类图线宽度一致）")
    series = {int(round(v * 100)) for v in std.raw["lines"]["allowed"]}
    group = {k: std.lw100(k) for k in ("thick", "medium", "thin")}
    for k, v in group.items():
        if v not in series:
            it.fail(ERROR, f"{k} 线宽 {v / 100:g} mm 不在 GB/T 17450 线宽系列里")
    if group["thick"] != 2 * group["medium"] and abs(group["thick"] / 2 - group["medium"]) > 1 or abs(group["medium"] / 2 - group["thin"]) > 1:
        it.fail(ERROR, f"粗 {group['thick'] / 100:g} / 中粗 {group['medium'] / 100:g} / 细 {group['thin'] / 100:g} 不是 4:2:1（允许按系列取整）")
    allowed = set(group.values())
    for ly in doc.layers:
        lw = ly.dxf.get("lineweight", -3)
        if ly.dxf.name.startswith("ZH-") and lw not in allowed:
            it.fail(ERROR, f"图层 {ly.dxf.name} 线宽 {lw / 100:g} mm 不在本图线宽组 {sorted(v / 100 for v in allowed)}", ly.dxf.name)
    if it.status == PASS:
        it.details.append(f"粗 {std.lineweight('thick'):g} / 中粗 {std.lineweight('medium'):g} / 细 {std.lineweight('thin'):g} mm")
    items.append(it)

    # 7 线型比例
    it = Item("ltscale", "线型比例与线素长度", "GB/T 17450-1998 第 4.3 条表 3（画 12d、短间隔 3d、长画 24d、间隔 18d，d 为线宽）；$LTSCALE = 出图比例")
    if abs(float(doc.header.get("$LTSCALE", 1.0)) - s) > 1e-6:
        it.fail(ERROR, f"$LTSCALE = {doc.header.get('$LTSCALE')}，应等于出图比例 {s:g}")
    for e in msp:
        if abs(float(e.dxf.get("ltscale", 1.0)) - 1.0) > 1e-6:
            it.fail(ERROR, f"{e.dxftype()} 单独设了线型比例 {e.dxf.ltscale:g}", e.dxf.handle)
    from .standards import layer_table, linetype_elements

    for lname, spec in layer_table(std).items():
        lt = spec["linetype"]
        if not lt.startswith("ZH_"):
            continue
        if lt not in doc.linetypes:
            it.fail(ERROR, f"图层 {lname} 的线型 {lt} 没有定义")
            continue
        no, d_mm = lt[3:5], spec["lineweight"] / 100
        want = [round(abs(v), 4) for v in linetype_elements(std, no, d_mm)]
        got = [round(abs(v), 4) for v in doc.linetypes.get(lt).simplified_line_pattern()]
        if got != want:
            it.fail(ERROR, f"线型 {lt} 线素 {got} 与 GB/T 17450 表 3 × d={d_mm:g} 的 {want} 不一致")
    items.append(it)

    # 8 平行线间距；9 图线重合
    lw = line_check(doc, s, std.raw["lines"].get("min_gap_mm", 0.7))
    it = Item("gap", "平行线间距", "GB/T 17450-1998 第 5.1 条（两条平行线之间的最小间隙不得小于 0.7 mm）")
    for c in lw["close_parallels"]:
        it.fail(ERROR, f"{c['layers'][0]} 与 {c['layers'][1]} 平行线间隙 {c['gap_mm']:.2f} mm < 0.7")
    items.append(it)
    it = Item("overlap", "图线重合", "GB/T 4457.4-2002 第 4.4 条（图线重合时只画一种，优先级 可见 > 不可见 > 中心线 > 尺寸界线）")
    for c in lw["overlaps"]:
        it.fail(ERROR, f"{c['layers'][0]} 与 {c['layers'][1]} 重合 {c['length_mm']:.1f} mm")
    items.append(it)

    # 10 尺寸规则（5 条）
    from .dims import check as dim_check

    dim_issues = dim_check(doc, d) if d.dimensions else []
    for code, name, clause in (
        ("E_DIM_OVERLAP", "尺寸线不与其他图线重合", "第 4.2.2 条"),
        ("E_DIM_ON_OUTLINE", "尺寸线不画在轮廓线的延长线上", "第 4.2.2 条"),
        ("E_DIM_CROSS", "尺寸线、尺寸界线不交叉", "第 4.2.3 条"),
        ("E_DIM_DUPLICATE", "同一尺寸不重复标注", "第 4.1.3 条"),
        ("E_DIM_TEXT_CROSSED", "尺寸数字不被图线穿过", "第 4.4.4 条"),
    ):
        it = Item(f"dim_{code[6:].lower()}", f"尺寸：{name}", f"GB/T 4458.4-2003 {clause}")
        for x in dim_issues:
            if x["code"] == code:
                it.fail(ERROR, x["message"], ",".join(x["refs"]))
        if not d.dimensions:
            it.details.append("图中没有尺寸")
        items.append(it)

    # 11 文字和图线冲突、文字互相压盖
    it = Item("text_line", "文字与图线不冲突", "GB/T 50001-2017 第 4.0 节（图线不得与文字、数字或符号重叠、混淆）；GB/T 4458.4-2003 第 4.4.4 条")
    it2 = Item("text_text", "文字之间不压盖", "GB/T 14691-1993 第 3 条（字体工整、笔画清楚、间隔均匀）")
    boxes = _text_boxes(doc)
    segs = [g for g in collect(doc) if not g.layer.startswith(LY["dim"])]
    who = owners(doc, d)
    for txt, bx, owner, e in boxes:
        h = _height(e)
        inner = _shrink(bx, 0.2 * h)  # 字框往里收 0.2 倍字高：贴着文字的下划线、格线不算压线
        mine = text_owner(e, owner, who)
        for g in segs:
            if owner is not None and g.owner == owner and _xdata(e)[:1] != ["anno"]:  # 图元自己的文字和图形（如柜体上的名称）由图元设计保证
                continue
            if _seg_hits_box(g.a, g.b, inner):
                other = who.get(g.owner, "")
                it.fail(ERROR, f"「{txt[:12]}」{_of(mine)}被 {other or g.layer} 的图线穿过", mine or txt[:12])
                if other and other != mine:
                    it.refs.append(other) if other not in it.refs else None
                break
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            (ta, ba, oa, ea), (tb, bb, ob, eb) = boxes[i], boxes[j]
            k = 0.15 * min(_height(ea), _height(eb))
            a, b = _shrink(ba, k), _shrink(bb, k)
            # 加粗标题是同一段文字错开 0.12 倍字高描两遍，不算压盖
            twin = ta == tb and abs(ba[0] - bb[0]) < 0.3 * _height(ea) and abs(ba[1] - bb[1]) < 0.1 * _height(ea)
            if a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3] and not twin:
                ra, rb = text_owner(ea, oa, who), text_owner(eb, ob, who)
                it2.fail(ERROR, f"「{ta[:10]}」{_of(ra)}与「{tb[:10]}」{_of(rb)}重叠", ra or ta[:10])
                if rb and rb not in it2.refs:
                    it2.refs.append(rb)
    items += [it, it2]

    # 12 标题栏必填项
    from .titleblock import missing_required, template, values

    it = Item("title_block", "标题栏必填项", "GB/T 10609.1-2008 第 4.2 条（名称及代号区：图样名称、图样代号；其他区：比例、日期）")
    miss = missing_required(d) if d.frame else []
    if not d.frame:
        it.details.append("方案流程图省略了标题栏（文章 3.1.1 节、图 3-1）")
    for k in miss:
        it.fail(WARN, f"必填项 {k} 没有填（显示格子说明）", k)
    empty = [c.get("placeholder") for c in template(d)["cells"] if c.get("field") and c.get("placeholder") and not values(d).get(c["field"])] if d.frame else []
    if empty:
        it.fail(WARN, f"未填、显示格子说明的栏目：{'、'.join(empty)}（只能由用户提供，程序不编造）")
    if miss or empty:
        it.hint = "向用户索取后用 create_drawing 的 title_block 传入"
    items.append(it)

    # 13 图层规范：随层；0 层为空
    it = Item("bylayer", "颜色、线型、线宽随层", "GB/T 14665-2012 第 4 条（图层约定）；通行做法：实体属性一律 ByLayer")
    lt = layer_table(std)
    for e in msp:
        if e.dxf.get("color", 256) != 256 or e.dxf.get("linetype", "BYLAYER").upper() != "BYLAYER" or e.dxf.get("lineweight", -1) != -1:
            it.fail(ERROR, f"{e.dxftype()}（{e.dxf.layer}）颜色 / 线型 / 线宽没有随层", e.dxf.handle)
        if e.dxf.layer.startswith("ZH-") and e.dxf.layer not in lt and not e.dxf.layer.startswith("ZH-管道-"):
            it.fail(ERROR, f"图层 {e.dxf.layer} 不在标准图层表里", e.dxf.layer)
    items.append(it)
    it = Item("layer0", "0 层不放实体", "GB/T 14665-2012 第 4 条（图层约定）；块内图元放 0 层、插入后随插入层")
    n0 = sum(1 for e in msp if e.dxf.layer == "0")
    if n0:
        it.fail(ERROR, f"模型空间 0 层上有 {n0} 个实体")
    items.append(it)

    # 14 文字关联（警告）
    from .anchors import free_texts

    it = Item("anchored", "标注与对象关联", "《CATIA V5-6 R2014 工程图教程》5.4 节（标注与几何关联，对象移动后标注跟着走）")
    for f in free_texts(d):
        it.fail(WARN, f"标注 {f} 没有关联到设备或管线（对象移动后不会跟着走）", f)
    items.append(it)

    if d.template_id != "water_elevation":  # 化工流程图专项（规范化要求 12）
        from .chem_check import items as chem_items

        items += chem_items(d, doc, std, Item, PASS, WARN, ERROR)
    for x in items:
        if x.verified is None:
            x.verified = _verified(x.source)
    passed = sum(1 for x in items if x.status == PASS)
    ver = [x for x in items if x.verified]
    return {
        "drawing_id": d.id,
        "revision": d.revision,
        "standard": std.id,
        "template": d.template_id,
        "score": {"passed": passed, "total": len(items), "text": f"{passed}/{len(items)}"},
        "errors": sum(1 for x in items if x.status == ERROR),
        "warnings": sum(1 for x in items if x.status == WARN),
        # 已与标准原文核对的规则（GB/T 14691、GB/T 17450）和只来自文章 / 未取得原文的规则分开统计
        "verified": {"items": len(ver), "passed": sum(1 for x in ver if x.status == PASS),
                     "unverified_items": len(items) - len(ver), "unverified_passed": passed - sum(1 for x in ver if x.status == PASS)},
        "items": [asdict(x) for x in items],
    }


VERIFIED_SOURCES = ("GB/T 14691", "GB/T 17450")  # 01_项目文档/标准资料 里取得原文的只有这两项


def _verified(source: str) -> bool:
    """出处全部来自已取得原文的标准才算核对过；引了文章、HG、或其他没取得原文的标准都算未核对。"""
    import re

    refs = set(re.findall(r"(GB/T \d+(?:\.\d+)?|HG/T \d+(?:\.\d+)?|HG \d+|文章|CATIA|规范化要求)", source))
    return bool(refs) and refs <= set(VERIFIED_SOURCES)


HINTS = {
    "text_line": "refs 是冲突文字所属的对象（管线 id、设备位号、标注 id）和压过去的对象。管线上的介质名 / 规格放不下时："
                 "用 move_equipment 把这条管线一端的设备水平移开（dx 约等于文字宽度，如 1500～3000），给文字腾出一段直管；"
                 "或用 connect_ports 把 medium 改短。设备文字：移动该设备。每次只改一处再 validate；"
                 "同一台设备不要上下来回移动——两次还不行就 ask_user 说明情况。",
    "text_text": "两段文字重叠，refs 是它们所属的对象。移动其中一个对象（水平移开约一个文字宽度），或把文字改短；"
                 "每次只改一处再 validate，两次还不行就 ask_user。",
}


def errors_for_validate(report: dict) -> list[dict]:
    """报告里的错误项 → validate 的 errors（code / message / refs / hint）。"""
    out = []
    for x in report["items"]:
        if x["status"] == ERROR:
            out.append({"code": f"E_STD_{x['id'].upper()}", "message": f"{x['name']}：{'；'.join(x['details'][:3])}（{x['source']}）",
                        "refs": x["refs"][:10], "hint": x["hint"] or HINTS.get(x["id"], "按报告移动相关设备或标注，让开冲突；不要改动其他对象")})
    return out


def owners(doc, d: Drawing) -> dict[str, str]:
    """实体句柄 → 模型能操作的对象：设备位号、管线 id、标注 id。"""
    tag = {e.id: e.tag for e in d.equipment}
    out = {}
    for e in doc.modelspace():
        xd = _xdata(e)
        if not xd:
            continue
        if xd[0] == "equip" and len(xd) > 1:
            out[e.dxf.handle] = tag.get(xd[1], xd[1])
        elif xd[0] in ("conn", "conn_part", "conn_label", "arrow", "jump", "break") and len(xd) > 1:
            out[e.dxf.handle] = xd[1]
        elif xd[0] in ("anno", "elev") and len(xd) > 1:
            out[e.dxf.handle] = xd[1]
        elif xd[0] in ("tag_row", "name_label") and len(xd) > 1:
            out[e.dxf.handle] = tag.get(xd[1], xd[1])
    return out


def text_owner(e, insert_handle, who: dict[str, str]) -> str:
    """文字所属对象：块属性看所在的块参照，其余看文字自己的 XDATA。"""
    if insert_handle is not None:
        return who.get(insert_handle, "")
    try:
        return who.get(e.dxf.handle, "")
    except AttributeError:
        return ""


def _of(ref: str) -> str:
    return f"（{ref}）" if ref else ""


def to_markdown(report: dict, title: str = "") -> str:
    icon = {PASS: "✅ 符合", WARN: "⚠️ 警告", ERROR: "❌ 错误"}
    lines = [f"# 规范自检报告{('：' + title) if title else ''}", "",
             f"- 图纸：{report['drawing_id']}（第 {report['revision']} 版），模板 {report['template']}，标准配置 {report['standard']}",
             f"- 得分：**{report['score']['text']}**（符合项 / 总项），错误 {report['errors']} 项，警告 {report['warnings']} 项", ""]
    v = report.get("verified")
    if v:
        lines[-1:] = [f"- 已核对原文的规则（GB/T 14691、GB/T 17450）：{v['passed']}/{v['items']} 符合；未核对原文的规则（文章、HG/T 20519 等）：{v['unverified_passed']}/{v['unverified_items']} 符合", ""]
    lines += ["| # | 检查项 | 结果 | 标准出处 | 原文核对 | 说明 |", "|---|---|---|---|---|---|"]
    for i, x in enumerate(report["items"], 1):
        note = "；".join(x["details"][:4]).replace("|", "/")
        if len(x["details"]) > 4:
            note += f"……共 {len(x['details'])} 处"
        lines.append(f"| {i} | {x['name']} | {icon[x['status']]} | {x['source']} | {'是' if x.get('verified') else '否'} | {note} |")
    warns = [x for x in report["items"] if x["status"] == WARN]
    if warns:
        lines += ["", "## 警告说明", ""]
        for x in warns:
            lines.append(f"- **{x['name']}**：{'；'.join(x['details'][:6])}" + (f"。处理：{x['hint']}" if x["hint"] else ""))
    lines += ["", "> 标准号以《CATIA V5-6 R2014 工程图教程》第 1、2 章所列国标为准；条款号按标准常见版本整理，正式提交前请对照标准原文核对。"]
    return "\n".join(lines) + "\n"
