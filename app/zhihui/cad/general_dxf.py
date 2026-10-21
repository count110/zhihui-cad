"""通用 CAD 图纸（mode="general"）的 DXF 读写。

写出：图层表、线型、文字样式、标注样式、系统变量、VPORT、全部图元、块（含属性）、布局/页面设置/视口；把每个图元的句柄回写到 IR，
再把 IR 嵌进 DXF（和流程图一样放在 ZHIHUI 字典里），所以本系统导出的文件重新打开是无损的。
读入：任意第三方 DXF → GeoEntity（LINE/RAY/XLINE/MLINE/LWPOLYLINE/POLYLINE/SPLINE/CIRCLE/ARC/ELLIPSE/TEXT/MTEXT/DIMENSION/INSERT/HATCH/MESH）。
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import ezdxf
from ezdxf import colors as ezcolors
from ezdxf.enums import TextEntityAlignment

from zhihui import GENERATOR
from zhihui.ir import Drawing, GeoEntity, Layer
from zhihui.ir.general import ID_PREFIX
from .geom import presets
from .geom.entities import P, ang, ccw_span, dist, pl

APPID = "ZHIHUI"
CHUNK = 230
ALIGN = {"left": TextEntityAlignment.BOTTOM_LEFT, "center": TextEntityAlignment.BOTTOM_CENTER, "right": TextEntityAlignment.BOTTOM_RIGHT,
         "top_left": TextEntityAlignment.TOP_LEFT, "top_center": TextEntityAlignment.TOP_CENTER, "top_right": TextEntityAlignment.TOP_RIGHT,
         "middle_center": TextEntityAlignment.MIDDLE_CENTER, "bottom_left": TextEntityAlignment.BOTTOM_LEFT}
MTEXT_ATTACH = {"left": 7, "center": 8, "right": 9, "top_left": 1, "top_center": 2, "top_right": 3, "middle_center": 5, "bottom_left": 7}
MLINE_JUST = {"top": 0, "zero": 1, "bottom": 2}
VERSIONS = {"R2018": "R2018", "R2013": "R2013", "R2010": "R2010", "R2007": "R2007"}


def _rgb(hexs: str) -> tuple[int, int, int]:
    h = hexs.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _gcolor(v, default: tuple[int, int, int]) -> tuple[int, int, int]:
    """渐变色的颜色：#RRGGBB、[r,g,b] 或 ACI 数字。"""
    try:
        if isinstance(v, str) and v.startswith("#") and len(v) == 7:
            return _rgb(v)
        if isinstance(v, (list, tuple)) and len(v) == 3:
            return tuple(int(x) for x in v)  # type: ignore[return-value]
        if isinstance(v, int):
            return tuple(ezcolors.aci2rgb(v))  # type: ignore[return-value]
    except Exception:
        pass
    return default


def _attribs(e: GeoEntity) -> dict:
    a: dict = {"layer": e.layer}
    if e.color is not None:
        if isinstance(e.color, str) and e.color.startswith("#"):
            a["true_color"] = ezcolors.rgb2int(_rgb(e.color))
        else:
            a["color"] = int(e.color)
    if e.linetype:
        a["linetype"] = e.linetype
    if e.linetype_scale and abs(e.linetype_scale - 1.0) > 1e-9:
        a["ltscale"] = e.linetype_scale
    if e.lineweight_mm is not None:
        a["lineweight"] = int(round(e.lineweight_mm * 100))
    return a


def _ensure_linetype(doc, name: str) -> None:
    if not name or name.lower() in ("continuous", "bylayer", "byblock") or name in doc.linetypes:
        return
    pat = presets.LINETYPE_PATTERNS.get(name.upper())
    if pat:
        doc.linetypes.add(name.upper(), pattern=pat, description=name)


def _add_layers(doc, d: Drawing) -> None:
    for l in d.layers:
        _ensure_linetype(doc, l.linetype)
        if l.name in doc.layers:                                 # 0、Defpoints 等 ezdxf 已建好的层：改属性，不重复建
            lay = doc.layers.get(l.name)
            lay.dxf.color = l.color
            if l.linetype in doc.linetypes:
                lay.dxf.linetype = l.linetype
        else:
            lay = doc.layers.add(l.name, color=l.color, linetype=l.linetype if l.linetype in doc.linetypes else "Continuous")
        if l.true_color:
            lay.rgb = _rgb(l.true_color)
        if l.lineweight_mm is not None:
            lay.dxf.lineweight = int(round(l.lineweight_mm * 100))
        if l.transparency:
            lay.transparency = l.transparency / 100.0
        if not l.on:
            lay.off()
        if l.frozen:
            lay.freeze()
        if l.locked:
            lay.lock()
        lay.dxf.plot = 1 if l.plot else 0
        if l.description:
            lay.description = l.description


def _add_styles(doc, d: Drawing) -> None:
    for st in d.text_styles:
        name = st["name"]
        font = st.get("font_file", "")
        ttf = "simfang.ttf" if font in ("仿宋", "") or not font.lower().endswith((".shx", ".ttf", ".ttc")) else font
        if name not in doc.styles:
            doc.styles.add(name, font=ttf if not font.lower().endswith(".shx") else font)
        s = doc.styles.get(name)
        s.dxf.width = float(st.get("width_factor", 1.0))
        s.dxf.oblique = float(st.get("oblique_angle_deg", 0.0))
        s.dxf.height = float(st.get("fixed_height", 0.0))
        if st.get("bigfont_file"):
            s.dxf.bigfont = st["bigfont_file"]
    for ds in d.dim_styles:
        _ensure_dimstyle(doc, ds)


def _ensure_dimstyle(doc, ds: dict) -> str:
    name = ds["name"]
    if name in doc.dimstyles:
        return name
    arrow = {"closed_filled": "", "architectural_tick": "ARCHTICK", "dot": "DOT"}.get(ds.get("arrow_type", "closed_filled"), "")
    attrs = {"dimtxt": ds.get("text_height", 2.5), "dimasz": ds.get("arrow_size", 2.5), "dimexo": ds.get("ext_offset", 0.625), "dimexe": ds.get("ext_beyond", 1.25),
             "dimdec": ds.get("precision", 2), "dimscale": ds.get("dimscale", 1.0), "dimgap": 0.625, "dimtxsty": ds.get("text_style", "Standard") if ds.get("text_style") in doc.styles else "Standard",
             "dimtad": 1, "dimtih": 0, "dimtoh": 0}
    td = ds.get("tolerance_display", "none")
    if td in ("symmetric", "deviation", "limits"):
        attrs.update({"dimtol": 1 if td != "limits" else 0, "dimlim": 1 if td == "limits" else 0})
    ds_ = doc.dimstyles.new(name, dxfattribs=attrs)
    if arrow:
        try:
            ds_.set_arrows(blk=arrow)
        except Exception:
            pass
    return name


def _add_blocks(doc, d: Drawing) -> None:
    attach = getattr(doc, "zh_xref_mode", "attach") == "attach"
    for b in d.blocks:
        if b["name"] in doc.blocks:
            continue
        if b.get("xref") and attach:
            continue                                         # 导出时由 _attach_xref 写真正的外部参照
        blk = doc.blocks.new(name=b["name"], base_point=tuple(b.get("base_point", (0, 0, 0))))
        for be in b.get("entities", []):
            ge = GeoEntity(id=be.get("id", "x"), kind=be["kind"], layer=be.get("layer", "0"), data=be["data"], tags=be.get("tags", []), color=be.get("color"),
                           linetype=be.get("linetype"), lineweight_mm=be.get("lineweight_mm"))
            _add_entity(blk, doc, d, ge)
        for ad in b.get("attdefs", []):
            blk.add_attdef(ad["tag"], insert=tuple(ad.get("position", (0, 0, 0)))[:2], text=ad.get("default_val", ""),
                           dxfattribs={"height": ad.get("height", 2.5), "prompt": ad.get("prompt", ad["tag"]), "invisible": 1 if ad.get("invisible") else 0})


def _attach_xref(space, doc, d: Drawing, e: GeoEntity):
    from pathlib import Path
    dd = e.data
    rec = next((x for x in d.xrefs if x["name"] == dd["block"]), None)
    if rec is None or rec.get("status") == "unloaded":
        return None
    fname = Path(rec["path"]).name
    sc = float(dd.get("scale", [1, 1, 1])[0])
    at = _attribs(e)
    p = P(dd["p"])
    if rec["file_type"] in ("dxf", "dwg"):
        from ezdxf import xref as _xref
        ins = _xref.attach(doc, block_name=rec["name"], filename=fname, insert=p, scale=sc, rotation=float(dd.get("rotation", 0)), overlay=bool(rec.get("overlay")))
        ins.dxf.layer = e.layer
        return ins
    if rec["file_type"] == "pdf":
        ud = doc.add_underlay_def(filename=fname, fmt="pdf", name="1")
        return space.add_underlay(ud, insert=p, scale=(sc, sc, sc), rotation=float(dd.get("rotation", 0)), dxfattribs=at)
    bb = rec.get("bbox") or [0, 0, 100, 100]
    pw, ph = max(int(bb[2] - bb[0]), 1), max(int(bb[3] - bb[1]), 1)
    idef = doc.add_image_def(filename=fname, size_in_pixel=(pw, ph))
    return space.add_image(idef, insert=p, size_in_units=(pw * sc, ph * sc), rotation=float(dd.get("rotation", 0)), dxfattribs=at)


def _add_entity(space, doc, d: Drawing, e: GeoEntity):
    k, dd, at = e.kind, e.data, _attribs(e)
    if k == "point":
        return space.add_point(P(dd["p"]), dxfattribs=at)
    if k == "line":
        return space.add_line(P(dd["p1"]), P(dd["p2"]), dxfattribs=at)
    if k == "ray":
        return space.add_ray(P(dd["p"]), (dd["dir"][0], dd["dir"][1], 0), dxfattribs=at)
    if k == "xline":
        return space.add_xline(P(dd["p"]), (dd["dir"][0], dd["dir"][1], 0), dxfattribs=at)
    if k == "mline":
        name = dd.get("style", "STANDARD")
        if name not in doc.mline_styles:
            st = doc.mline_styles.new(name)
            spec = next((s for s in d.mline_styles if s.get("name", "").upper() == name.upper()), None)
            for off in (spec or {}).get("elements", [0.5, -0.5]):
                try:
                    st.elements.append(float(off), 0, "BYLAYER")
                except Exception:
                    break
        m = space.add_mline([P(p) for p in dd["points"]], close=bool(dd.get("closed")), dxfattribs={**at, "style_name": name if name in doc.mline_styles else "Standard"})
        m.set_scale_factor(float(dd.get("scale", 1.0)))
        m.dxf.justification = MLINE_JUST.get(dd.get("justification", "zero"), 1)
        return m
    if k == "polyline":
        pts = [(v["p"][0], v["p"][1], float(v.get("sw", 0) or 0), float(v.get("ew", 0) or 0), float(v.get("bulge", 0) or 0)) for v in dd["vertices"]]
        return space.add_lwpolyline(pts, format="xyseb", close=bool(dd.get("closed")), dxfattribs=at)
    if k == "spline":
        if dd.get("fit"):
            return space.add_spline(fit_points=[P(p) for p in dd["fit"]], degree=int(dd.get("degree", 3)), dxfattribs=at)
        return space.add_open_spline(control_points=[P(p) for p in dd["cv"]], degree=int(dd.get("degree", 3)), dxfattribs=at)
    if k == "circle":
        return space.add_circle(P(dd["c"]), float(dd["r"]), dxfattribs=at)
    if k == "arc":
        return space.add_arc(P(dd["c"]), float(dd["r"]), float(dd["a0"]), float(dd["a1"]), dxfattribs=at)
    if k == "ellipse":
        t0, t1 = float(dd.get("t0", 0)), float(dd.get("t1", 360))
        full = abs(t1 - t0) >= 360 - 1e-9
        return space.add_ellipse(P(dd["c"]), (dd["major"][0], dd["major"][1], 0), float(dd.get("ratio", 1)), 0 if full else math.radians(t0), math.tau if full else math.radians(t1), dxfattribs=at)
    if k == "text":
        st = dd.get("style", d.current_text_style)
        ta = {**at, "style": st if st in doc.styles else "Standard"}
        if dd.get("width_factor"):                                  # 文字自带宽度因子（如长仿宋 0.7）才写，旧图不受影响
            ta["width"] = float(dd["width_factor"])
        t = space.add_text(dd["text"], height=float(dd.get("height", 3.5)), rotation=float(dd.get("rotation", 0)), dxfattribs=ta)
        t.set_placement(P(dd["p"])[:2], align=ALIGN.get(dd.get("align", "left"), TextEntityAlignment.BOTTOM_LEFT))
        return t
    if k == "mtext":
        st = dd.get("style", d.current_text_style)
        m = space.add_mtext(dd["text"], dxfattribs={**at, "char_height": float(dd.get("height", 3.5)), "style": st if st in doc.styles else "Standard", "rotation": float(dd.get("rotation", 0)),
                                                    "attachment_point": MTEXT_ATTACH.get(dd.get("attach", "left"), 7)})
        m.dxf.insert = P(dd["p"])
        if dd.get("width"):
            m.dxf.width = float(dd["width"])
        if dd.get("line_spacing", 1.0) != 1.0:
            m.dxf.line_spacing_factor = float(dd["line_spacing"])
        if dd.get("bg_mask"):
            m.set_bg_color(0, scale=1.5)
        return m
    if k == "dimension":
        return _add_dimension(space, doc, d, e)
    if k == "hatch":
        h = space.add_hatch(color=7, dxfattribs=at)
        g = dd.get("gradient")
        if g:
            h.set_gradient(_gcolor(g.get("color1"), (255, 255, 255)), _gcolor(g.get("color2"), (0, 0, 255)), rotation=float(g.get("angle", 0)))
        elif dd.get("pattern", "").upper() == "SOLID":
            h.set_solid_fill(color=7)
        else:
            h.set_pattern_fill(dd.get("pattern", "ANSI31"), color=7, angle=float(dd.get("angle", 0)), scale=float(dd.get("scale", 1.0)))
        h.dxf.hatch_style = {"normal": 0, "outer": 1, "ignore": 2}.get(dd.get("island_style", "normal"), 0)
        for i, loop in enumerate(dd.get("loops", [])):
            h.paths.add_polyline_path([(p[0], p[1]) for p in loop], is_closed=True, flags=ezdxf.const.BOUNDARY_PATH_EXTERNAL if i == 0 else ezdxf.const.BOUNDARY_PATH_DEFAULT)
        return h
    if k == "insert":
        if dd.get("xref") and getattr(doc, "zh_xref_mode", "attach") == "attach":
            return _attach_xref(space, doc, d, e)
        sc = dd.get("scale", [1, 1, 1])
        r = space.add_blockref(dd["block"], P(dd["p"]), dxfattribs={**at, "xscale": sc[0], "yscale": sc[1], "zscale": sc[2], "rotation": float(dd.get("rotation", 0))})
        if dd.get("attribs"):
            r.add_auto_attribs(dd["attribs"])
        return r
    if k in ("mesh", "solid3d"):
        m = space.add_mesh(dxfattribs=at)
        with m.edit_data() as md:
            md.vertices = [P(v) for v in dd["vertices"]]
            md.faces = [list(f) for f in dd["faces"]]
        return m
    if k == "tolerance":
        return space.add_mtext(dd.get("text", ""), dxfattribs={**at, "char_height": 3.5, "insert": P(dd["p"])})
    if k == "leader":
        space.add_lwpolyline([(p[0], p[1]) for p in dd["points"]], dxfattribs=at)
        if dd.get("text"):
            t = space.add_text(dd["text"], height=3.5, dxfattribs=at)
            t.set_placement(P(dd["points"][-1])[:2], align=TextEntityAlignment.BOTTOM_LEFT)
        return None
    return None


def _add_dimension(space, doc, d: Drawing, e: GeoEntity):
    dd, at = e.data, _attribs(e)
    style = dd.get("style") or d.current_dim_style
    if style not in doc.dimstyles:
        ds = next((s for s in d.dim_styles if s["name"] == style), None) or presets.DIM_STYLES["ISO_25"]
        style = _ensure_dimstyle(doc, ds)
    txt = dd.get("text") or "<>"
    t = dd["dtype"]
    kw = {"dimstyle": style, "dxfattribs": at, "text": txt}
    if t == "linear_horizontal":
        dim = space.add_linear_dim(base=P(dd["loc"])[:2], p1=P(dd["p1"])[:2], p2=P(dd["p2"])[:2], angle=0, **kw)
    elif t == "linear_vertical":
        dim = space.add_linear_dim(base=P(dd["loc"])[:2], p1=P(dd["p1"])[:2], p2=P(dd["p2"])[:2], angle=90, **kw)
    elif t == "aligned":
        p1, p2, loc = P(dd["p1"]), P(dd["p2"]), P(dd["loc"])
        nx, ny = -(p2[1] - p1[1]), p2[0] - p1[0]
        L = math.hypot(nx, ny) or 1.0
        distance = ((loc[0] - p1[0]) * nx + (loc[1] - p1[1]) * ny) / L
        dim = space.add_aligned_dim(p1=p1[:2], p2=p2[:2], distance=distance, **kw)
    elif t == "angular":
        l1, l2 = dd["l1"], dd["l2"]
        dim = space.add_angular_dim_2l(base=P(dd["loc"])[:2], line1=(P(l1[0])[:2], P(l1[1])[:2]), line2=(P(l2[0])[:2], P(l2[1])[:2]), **kw)
    elif t in ("radius", "diameter"):
        c, r = P(dd["center"]), float(dd["radius"])
        a = ang(c, P(dd["loc"]))
        fn = space.add_radius_dim if t == "radius" else space.add_diameter_dim
        dim = fn(center=c[:2], radius=r, angle=a, **kw)
    elif t == "arc_length":
        c, r = P(dd["center"]), float(dd["radius"])
        dim = space.add_arc_dim_cra(center=c[:2], radius=r, start_angle=float(dd["a0"]), end_angle=float(dd["a1"]), distance=max(1.0, dist(c, P(dd["loc"])) - r), **kw)
    else:
        return None
    dim.render()
    return dim.dimension


def _setup_header(doc, d: Drawing) -> None:
    def put(k, v):
        try:
            doc.header[k] = v
        except Exception:
            pass
    put("$INSUNITS", 4)
    put("$LTSCALE", d.ltscale)
    put("$CELTSCALE", d.celtscale)
    put("$PSLTSCALE", d.psltscale)
    put("$LIMMIN", (d.limits[0], d.limits[1]))
    put("$LIMMAX", (d.limits[2], d.limits[3]))
    put("$LIMCHECK", 1 if d.limits_check else 0)
    try:                                                    # ezdxf 保存时用模型空间的 limmin/limmax 覆盖表头，两处都要写
        ms = doc.modelspace()
        ms.dxf.limmin = (d.limits[0], d.limits[1])
        ms.dxf.limmax = (d.limits[2], d.limits[3])
    except Exception:
        pass
    put("$PDMODE", int(d.point_style.get("pdmode", 0)))
    put("$PDSIZE", float(d.point_style.get("pdsize", 0)))
    put("$ORTHOMODE", 1 if d.draft.ortho_on else 0)
    put("$SNAPMODE", 1 if d.draft.snap_on else 0)
    put("$CLAYER", d.current_layer)
    try:
        vp = doc.viewports.get("*Active")[0]
        vp.dxf.snap_base = (0, 0)
        vp.dxf.snap_spacing = tuple(d.draft.snap_spacing)
        vp.dxf.grid_spacing = tuple(d.draft.grid_spacing)
        vp.dxf.center = tuple(d.view.center)
        vp.dxf.height = float(d.view.height)
    except Exception:
        pass


def _add_layouts(doc, d: Drawing, handles: dict) -> None:
    for lay in d.layouts:
        name = lay["name"]
        L = doc.layouts.get(name) if name in doc.layouts else doc.layouts.new(name)
        ps = lay.get("page_setup", {})
        w, h = ps.get("size", (420, 297))
        try:
            L.page_setup(size=(w, h), margins=(0, 0, 0, 0), units="mm", scale=(1, 1), rotation=0, offset=(0, 0), name=ps.get("paper", "ISO_A3"), device=ps.get("device", "DWG To PDF.pc3"))
        except Exception:
            pass
        for ve in [e for e in d.entities if e.space == name]:
            ent = _add_entity(L, doc, d, ve)
            if ent is not None:
                handles[ve.id] = ent.dxf.handle
        for vp in lay.get("viewports", []):
            (x0, y0), (x1, y1) = vp["corners"]
            v = L.add_viewport(center=((x0 + x1) / 2, (y0 + y1) / 2), size=(abs(x1 - x0), abs(y1 - y0)), view_center_point=tuple(vp["target_center"]), view_height=float(vp["view_height"]))
            if vp.get("locked"):
                v.dxf.status = 2
            if vp.get("frozen_layers"):
                try:
                    v.frozen_layers = list(vp["frozen_layers"])
                except Exception:
                    pass


def build_general_doc(d: Drawing, version: str = "R2018", xref_mode: str = "inline"):
    """xref_mode: "attach" 写真正的外部参照（导出 DXF）；"inline" 把缓存的内容当普通块写入（预览渲染）。"""
    doc = ezdxf.new(VERSIONS.get(version, "R2018"), setup=True)
    doc.zh_xref_mode = xref_mode
    doc.units = ezdxf.units.MM
    _setup_header(doc, d)
    _add_layers(doc, d)
    _add_styles(doc, d)
    _add_blocks(doc, d)
    msp = doc.modelspace()
    handles: dict[str, str] = {}
    for e in d.entities:
        if e.space != "Model":
            continue
        ent = _add_entity(msp, doc, d, e)
        if ent is not None:
            handles[e.id] = ent.dxf.handle
    _add_layouts(doc, d, handles)
    doc.zh_handles = handles
    return doc


def _chunks(text: str) -> list[str]:
    return [text[i:i + CHUNK] for i in range(0, len(text), CHUNK)] or [""]


def write_general_dxf(d: Drawing, path: str | Path, version: str = "R2018") -> str:
    doc = build_general_doc(d, version, xref_mode="attach")
    for e in d.entities:
        h = doc.zh_handles.get(e.id)
        if h:
            e.handle = h                                    # 句柄回写 IR（嵌入的 IR 也带着）
    root = doc.rootdict.add_new_dict(APPID)
    xr = root.add_xrecord("META")
    meta = {"generator": GENERATOR, "drawing_id": d.id, "revision": d.revision, "mode": "general"}
    tags = [(1, "META"), (1, json.dumps(meta, ensure_ascii=False)), (1, "IR")] + [(1, c) for c in _chunks(d.compat_json())]
    xr.reset(tags)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(path)
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ------------------------------------------------------------------ 读入
def read_any_dxf(path: str | Path) -> Drawing:
    """优先还原本系统嵌入的 IR；否则把第三方 DXF 的图元转成 GeoEntity。"""
    try:
        doc = ezdxf.readfile(str(path))
    except (IOError, ezdxf.DXFStructureError) as ex:
        raise ValueError(f"DXF 文件无法解析：{ex}") from ex
    root = doc.rootdict.get(APPID)
    if root is not None and "META" in root:
        tags = [v for _, v in root["META"].tags]
        try:
            meta = json.loads(tags[1])
            ir = Drawing.model_validate_json("".join(tags[3:]))
            if meta.get("mode") == "general" or ir.mode == "general":
                return ir
        except Exception:
            pass
    return read_general_doc(doc)


def _color_of(en) -> int | str | None:
    try:
        if en.dxf.hasattr("true_color") and en.dxf.true_color:
            r, g, b = ezcolors.int2rgb(en.dxf.true_color)
            return f"#{r:02X}{g:02X}{b:02X}"
        c = en.dxf.color
        return None if c in (256, 0) else int(c)
    except Exception:
        return None


def _v3(p) -> list[float]:
    return [round(float(p[0]), 9), round(float(p[1]), 9), round(float(p[2]) if len(p) > 2 else 0.0, 9)]


def _convert_dimension(en):
    """线性/对齐/半径/直径标注转成本系统的原生标注；其它类型返回 None（由调用方分解成线和文字）。"""
    from .geom import dims
    try:
        base = en.dimtype & 7 if hasattr(en, "dimtype") else int(en.dxf.dimtype) & 7
        txt = en.dxf.text if en.dxf.hasattr("text") else ""
        style = en.dxf.dimstyle if en.dxf.hasattr("dimstyle") else "Standard"
        if base in (0, 1):
            p1, p2, loc = _v3(en.dxf.defpoint2), _v3(en.dxf.defpoint3), _v3(en.dxf.defpoint)
            a = float(en.dxf.get("angle", 0.0)) % 180 if base == 0 else None
            if base == 0:
                if abs(a) < 1e-6:
                    dtype = "linear_horizontal"
                elif abs(a - 90) < 1e-6:
                    dtype = "linear_vertical"
                else:                                         # 旋转角正好沿两点连线 = 对齐标注（ezdxf 等软件把对齐标注写成带角度的线性标注）
                    along = math.degrees(math.atan2(p2[1] - p1[1], p2[0] - p1[0])) % 180
                    if min(abs(along - a), 180 - abs(along - a)) > 1e-4:
                        return None
                    dtype = "aligned"
            else:
                dtype = "aligned"
            m = dims.linear_measure(tuple(p1), tuple(p2), dtype)
            return "dimension", {"dtype": dtype, "p1": p1, "p2": p2, "loc": loc, "text": "" if txt in ("", "<>") else txt, "style": style, "measurement": m}
        if base in (3, 4):
            a, b = _v3(en.dxf.defpoint), _v3(en.dxf.defpoint4)
            if base == 3:
                c = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2, 0.0]
                r = math.hypot(a[0] - b[0], a[1] - b[1]) / 2
                loc, dtype, meas = a, "diameter", 2 * r
            else:
                c, loc = a, b
                r = math.hypot(a[0] - b[0], a[1] - b[1])
                dtype, meas = "radius", r
            if r < 1e-9:
                return None
            return "dimension", {"dtype": dtype, "center": c, "radius": r, "loc": loc, "text": "" if txt in ("", "<>") else txt, "style": style, "measurement": meas, "p1": c, "p2": loc}
    except Exception:
        return None
    return None


def _convert_many(en, d: Drawing, skipped: dict[str, int]) -> list[GeoEntity]:
    """一个 DXF 图元 → 若干 GeoEntity。不能转成原生标注的 DIMENSION 分解成线/弧/文字（箭头等不支持的部分计入 skipped）。"""
    ge = _convert(en, d)
    if ge is not None:
        return [ge]
    if en.dxftype() == "DIMENSION":
        out: list[GeoEntity] = []
        try:
            parts = list(en.virtual_entities())
        except Exception:
            parts = []
        for v in parts:
            g = _convert(v, d)
            if g is None:
                skipped["DIMENSION(箭头等)"] = skipped.get("DIMENSION(箭头等)", 0) + 1
                continue
            g.layer = en.dxf.layer
            g.tags = ["dim_exploded"]
            out.append(g)
        skipped["DIMENSION→已分解"] = skipped.get("DIMENSION→已分解", 0) + 1
        return out
    skipped[en.dxftype()] = skipped.get(en.dxftype(), 0) + 1
    return []



def _convert(en, d: Drawing) -> GeoEntity | None:
    t = en.dxftype()
    data: dict
    kind = None
    tags: list[str] = []
    if t == "LINE":
        kind, data = "line", {"p1": _v3(en.dxf.start), "p2": _v3(en.dxf.end)}
    elif t == "POINT":
        kind, data = "point", {"p": _v3(en.dxf.location)}
    elif t in ("RAY", "XLINE"):
        u = en.dxf.unit_vector
        kind, data = t.lower(), {"p": _v3(en.dxf.start if t == "RAY" else en.dxf.start), "dir": [float(u[0]), float(u[1])]}
    elif t == "MLINE":
        kind, data = "mline", {"points": [_v3(v) for v in en.vertices] if hasattr(en, "vertices") else [], "style": en.dxf.style_name if en.dxf.hasattr("style_name") else "STANDARD",
                               "scale": float(en.dxf.scale_factor) if en.dxf.hasattr("scale_factor") else 1.0, "justification": {0: "top", 1: "zero", 2: "bottom"}.get(en.dxf.justification, "zero"),
                               "closed": bool(en.is_closed)}
        try:
            data["points"] = [_v3(v.location) if hasattr(v, "location") else _v3(v) for v in en.vertices]
        except Exception:
            data["points"] = []
    elif t == "LWPOLYLINE":
        verts = [{"p": [round(p[0], 9), round(p[1], 9), 0.0], "bulge": round(p[4], 12), "sw": p[2], "ew": p[3]} for p in en.get_points("xyseb")]
        kind, data = "polyline", {"vertices": verts, "closed": bool(en.closed), "width": float(en.dxf.const_width) if en.dxf.hasattr("const_width") else 0.0}
    elif t == "POLYLINE" and not (en.is_3d_polyline or en.is_polygon_mesh or en.is_poly_face_mesh):
        pts = [v.dxf.location for v in en.vertices]
        kind, data = "polyline", {"vertices": [{"p": _v3(p), "bulge": float(v.dxf.bulge) if v.dxf.hasattr("bulge") else 0.0, "sw": 0.0, "ew": 0.0} for p, v in zip(pts, en.vertices)], "closed": bool(en.is_closed), "width": 0.0}
    elif t == "SPLINE":
        fit = [_v3(p) for p in en.fit_points]
        kind, data = "spline", ({"fit": fit, "degree": en.dxf.degree, "closed": bool(en.closed)} if fit else {"cv": [_v3(p) for p in en.control_points], "degree": en.dxf.degree, "closed": bool(en.closed)})
    elif t == "CIRCLE":
        kind, data = "circle", {"c": _v3(en.dxf.center), "r": float(en.dxf.radius)}
    elif t == "ARC":
        kind, data = "arc", {"c": _v3(en.dxf.center), "r": float(en.dxf.radius), "a0": float(en.dxf.start_angle), "a1": float(en.dxf.end_angle)}
    elif t == "ELLIPSE":
        s, e = math.degrees(en.dxf.start_param), math.degrees(en.dxf.end_param)
        full = abs(e - s) >= 359.9999
        kind, data = "ellipse", {"c": _v3(en.dxf.center), "major": _v3(en.dxf.major_axis), "ratio": float(en.dxf.ratio), "t0": 0.0 if full else s, "t1": 360.0 if full else e}
    elif t == "TEXT":
        kind, data = "text", {"p": _v3(en.dxf.insert), "text": en.dxf.text, "height": float(en.dxf.height), "rotation": float(en.dxf.rotation), "style": en.dxf.style, "align": "left"}
    elif t == "MTEXT":
        kind, data = "mtext", {"p": _v3(en.dxf.insert), "text": en.text, "height": float(en.dxf.char_height), "width": float(en.dxf.width) if en.dxf.hasattr("width") else 0.0,
                               "rotation": float(en.dxf.rotation), "line_spacing": 1.0, "attach": "left", "bg_mask": False, "style": en.dxf.style}
    elif t == "DIMENSION":
        kd = _convert_dimension(en)
        if kd is None:
            return None
        kind, data = kd
    elif t == "INSERT":
        kind, data = "insert", {"block": en.dxf.name, "p": _v3(en.dxf.insert), "scale": [en.dxf.xscale, en.dxf.yscale, en.dxf.zscale], "rotation": float(en.dxf.rotation),
                                "attribs": {a.dxf.tag: a.dxf.text for a in en.attribs}}
    elif t == "HATCH":
        loops = []
        for pth in en.paths:
            if hasattr(pth, "vertices"):
                loops.append([[round(v[0], 9), round(v[1], 9), 0.0] for v in pth.vertices])
            else:
                pts = []
                for ed in pth.edges:
                    if ed.type == "LineEdge":
                        pts.append([ed.start[0], ed.start[1], 0.0])
                    elif ed.type in ("ArcEdge", "EllipseEdge"):
                        c = ed.center
                        if ed.type == "ArcEdge":
                            for a in range(int(ed.start_angle), int(ed.end_angle) + 1, 10):
                                pts.append([c[0] + ed.radius * math.cos(math.radians(a)), c[1] + ed.radius * math.sin(math.radians(a)), 0.0])
                if pts:
                    loops.append(pts)
        kind, data = "hatch", {"pattern": en.dxf.pattern_name, "scale": float(en.dxf.pattern_scale), "angle": float(en.dxf.pattern_angle), "loops": loops, "island_style": {0: "normal", 1: "outer", 2: "ignore"}.get(en.dxf.hatch_style, "normal"),
                               "associative": False, "source_ids": [], "gradient": None}
    elif t == "MESH":
        v = en.vertices
        f = en.faces
        kind, data = "mesh", {"vertices": [_v3(p) for p in v], "faces": [list(x) for x in f]}
    if kind is None:
        return None
    ent = GeoEntity(id=d.new_id(ID_PREFIX[kind]), kind=kind, layer=en.dxf.layer, color=_color_of(en), data=data, tags=tags, handle=en.dxf.handle)
    if en.dxf.hasattr("linetype") and en.dxf.linetype.upper() not in ("BYLAYER", "CONTINUOUS", ""):
        ent.linetype = en.dxf.linetype
    if en.dxf.hasattr("lineweight") and en.dxf.lineweight > 0:
        ent.lineweight_mm = en.dxf.lineweight / 100.0
    if en.dxf.hasattr("ltscale") and en.dxf.ltscale != 1.0:
        ent.linetype_scale = float(en.dxf.ltscale)
    return ent


def read_general_doc(doc) -> Drawing:
    """第三方 DXF → 通用 IR（不识别的图元类型计数后跳过，写进 skipped）。"""
    from .geom.entities import entity_bbox
    d = Drawing(id="dwg_pending", title="导入的图", mode="general", frame=False, source="uploaded")
    d.layers = []
    for lay in doc.layers:
        l = Layer(name=lay.dxf.name, color=abs(lay.dxf.color), linetype=lay.dxf.linetype, on=lay.is_on(), frozen=lay.is_frozen(), locked=lay.is_locked(), plot=bool(lay.dxf.plot))
        if lay.dxf.hasattr("lineweight") and lay.dxf.lineweight > 0:
            l.lineweight_mm = lay.dxf.lineweight / 100.0
        if lay.dxf.hasattr("true_color") and lay.dxf.true_color:
            r, g, b = ezcolors.int2rgb(lay.dxf.true_color)
            l.true_color = f"#{r:02X}{g:02X}{b:02X}"
        d.layers.append(l)
    if not d.get_layer("0"):
        d.layers.insert(0, Layer(name="0"))
    hv = doc.header
    d.ltscale = float(hv.get("$LTSCALE", 1.0))
    d.celtscale = float(hv.get("$CELTSCALE", 1.0))
    try:
        lm, lx = hv.get("$LIMMIN", (0, 0)), hv.get("$LIMMAX", (420, 297))
        d.limits = (float(lm[0]), float(lm[1]), float(lx[0]), float(lx[1]))
    except Exception:
        pass
    d.current_layer = hv.get("$CLAYER", "0") if hv.get("$CLAYER", "0") in {l.name for l in d.layers} else "0"
    d.point_style = {"pdmode": int(hv.get("$PDMODE", 0)), "pdsize": float(hv.get("$PDSIZE", 0))}
    for st in doc.styles:
        d.text_styles.append({"name": st.dxf.name, "font_file": st.dxf.font, "bigfont_file": st.dxf.bigfont if st.dxf.hasattr("bigfont") else "", "fixed_height": float(st.dxf.height),
                              "width_factor": float(st.dxf.width), "oblique_angle_deg": float(st.dxf.oblique), "annotative": False})
    for ds in doc.dimstyles:
        d.dim_styles.append({"name": ds.dxf.name, "arrow_type": "closed_filled", "arrow_size": float(ds.dxf.get("dimasz", 2.5)), "text_height": float(ds.dxf.get("dimtxt", 2.5)), "text_style": ds.dxf.get("dimtxsty", "Standard"),
                             "ext_offset": float(ds.dxf.get("dimexo", 0.625)), "ext_beyond": float(ds.dxf.get("dimexe", 1.25)), "precision": int(ds.dxf.get("dimdec", 2)), "dimscale": float(ds.dxf.get("dimscale", 1.0)),
                             "tolerance_display": "none", "base_spacing": 3.75})
    skipped: dict[str, int] = {}
    for en in doc.modelspace():
        d.entities.extend(_convert_many(en, d, skipped))
    for blk in doc.blocks:
        if blk.name.startswith("*") or blk.name.startswith("_"):
            continue
        ents = []
        for en in blk:
            ge = _convert(en, d)
            if ge is not None:
                ents.append({"kind": ge.kind, "data": ge.data, "tags": [], "layer": ge.layer})
        d.blocks.append({"name": blk.name, "base_point": list(blk.block.dxf.base_point), "entities": ents, "attdefs": [{"tag": a.dxf.tag, "prompt": a.dxf.prompt if a.dxf.hasattr("prompt") else a.dxf.tag,
                         "default_val": a.dxf.text, "position": list(a.dxf.insert), "height": a.dxf.height, "invisible": bool(a.dxf.invisible)} for a in blk.query("ATTDEF")]})
    _read_layouts(doc, d, skipped)
    b = d.bbox()
    if b:
        d.view.center = ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)
        d.view.height = max(b[3] - b[1], (b[2] - b[0]) * 297 / 420, 1.0) * 1.1
    if skipped:
        d.notes = [f"导入时跳过了不支持的对象：{', '.join(f'{k}×{v}' for k, v in skipped.items())}"]
    return d


def _read_layouts(doc, d: Drawing, skipped: dict[str, int]) -> None:
    """第三方 DXF 的布局：图纸空间图元 + 视口（比例 = 视口高度 / 模型视图高度）。"""
    from .geom import layout as L
    for lay in doc.layouts:
        if lay.name == "Model":
            continue
        try:
            w, h = float(lay.dxf_layout.dxf.paper_width), float(lay.dxf_layout.dxf.paper_height)
        except Exception:
            w, h = 0.0, 0.0
        if w < 10 or h < 10:
            w, h = 420.0, 297.0
        if int(lay.dxf_layout.dxf.get("plot_rotation", 0)) in (1, 3):
            w, h = h, w
        rec = {"name": lay.name, "page_setup": {"paper": "自定义", "size": [w, h], "orientation": "landscape" if w >= h else "portrait", "device": "pdf", "plot_area": "layout", "plot_scale": "1:1",
                                              "center_plot": True, "color_mode": "monochrome", "lineweights_on": True, "dpi": 150}, "viewports": [], "frame": None, "title_block": {}}
        n = 0
        for en in lay:
            if en.dxftype() == "VIEWPORT":
                if int(en.dxf.id) == 1:
                    continue
                n += 1
                cx, cy = en.dxf.center[0], en.dxf.center[1]
                vw, vh = float(en.dxf.width), float(en.dxf.height)
                vhm = float(en.dxf.view_height) or 1.0
                vc = en.dxf.view_center_point
                try:
                    frozen = list(en.frozen_layers)
                except Exception:
                    frozen = []
                rec["viewports"].append({"id": f"{lay.name}-{n}", "corners": [[cx - vw / 2, cy - vh / 2], [cx + vw / 2, cy + vh / 2]], "target_center": [float(vc[0]), float(vc[1])], "view_height": vhm,
                                         "scale": L.ratio_to_text(vh / vhm), "locked": bool(int(en.dxf.get("status", 0)) & 0x4000) or int(en.dxf.get("status", 0)) == 2, "frozen_layers": frozen})
                continue
            for ge in _convert_many(en, d, skipped):
                ge.space = lay.name
                d.entities.append(ge)
        d.layouts.append(rec)



def dxf_kind(path) -> str:
    """DXF 是谁生成的：pfd（本系统的流程图）/ general（本系统的通用图）/ foreign（第三方软件的文件）。"""
    try:
        doc = ezdxf.readfile(str(path))
    except Exception:
        return "foreign"
    root = doc.rootdict.get(APPID)
    if root is None or "META" not in root:
        return "foreign"
    try:
        meta = json.loads([v for _, v in root["META"].tags][1])
    except Exception:
        return "foreign"
    return "general" if meta.get("mode") == "general" else "pfd"
