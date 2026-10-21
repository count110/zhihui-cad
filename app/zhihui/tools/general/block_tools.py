"""块、动态块、外部参照（函数 36–38，意图类别 block_xref_constraint）。"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import Field
from shapely.geometry import Polygon, box

from zhihui.cad.geom import blocks as B
from zhihui.cad.geom.entities import P, entity_bbox, pl, transform_data
from zhihui.ir import Layer
from zhihui.tools.core import ToolError
from .ctx import Ctx
from .registry import Args, Ids, Pt, tool


def _need(cond, msg: str, hint: str = "") -> None:
    if not cond:
        raise ToolError("MISSING_ARG", msg, hint or "向用户确认")


def get_block(d, name: str) -> dict:
    blk = next((b for b in d.blocks if b["name"].lower() == name.lower()), None)
    if blk is None:
        raise ToolError("BLOCK_NOT_FOUND", f"没有块「{name}」", f"现有块：{[b['name'] for b in d.blocks] or '无'}")
    return blk


def _block_from_entities(ents, base, attdefs: list[dict]) -> list[dict]:
    out = []
    for i, e in enumerate(ents):
        out.append({"id": f"E{i + 1}", "kind": e.kind, "data": copy.deepcopy(e.data), "layer": e.layer, "tags": list(e.tags), "color": e.color, "linetype": e.linetype, "lineweight_mm": e.lineweight_mm})
    return out


# ------------------------------------------------------------------ 36 create_and_insert_block
class AttDef(Args):
    tag: str = Field(min_length=1, max_length=40, pattern=r"^[^\s]+$")
    prompt: str = ""
    default_val: str = ""
    position: Pt = None
    height: float = Field(2.5, gt=0)
    invisible: bool = False


class BlockArgs(Args):
    action: Literal["define_block", "insert_block", "edit_attributes", "redefine", "wblock_export", "list"]
    block_name: Optional[str] = None
    base_point: Pt = None
    source_entity_ids: Ids = None
    keep_source: Literal["retain", "convert", "delete"] = "convert"
    attribute_defs: Optional[list[AttDef]] = None
    insert_point: Pt = None
    scale: Optional[list[float]] = None
    rotation_deg: float = 0.0
    attribute_values: Optional[dict] = None
    block_ref_id: Optional[str] = None
    layer: Optional[str] = None


def _insert_attribs(blk: dict, given: dict | None, ctx: Ctx) -> dict:
    out = {}
    given = {k.upper(): v for k, v in (given or {}).items()}
    tags = {ad["tag"].upper() for ad in blk.get("attdefs", [])}
    bad = [k for k in given if k not in tags]
    if bad:
        raise ToolError("ATTRIB_NOT_FOUND", f"块「{blk['name']}」没有属性 {bad}", f"可用属性：{[ad['tag'] for ad in blk.get('attdefs', [])]}")
    for ad in blk.get("attdefs", []):
        v = given.get(ad["tag"].upper())
        if v is None:
            v = ad.get("default_val", "")
            if not v and not ad.get("invisible"):
                ctx.warnings.append(f"属性 {ad['tag']}（{ad.get('prompt') or '无提示'}）没有给值，也没有默认值，留空")
        out[ad["tag"]] = str(v)
    return out


@tool("create_and_insert_block", "block_xref_constraint", BlockArgs)
def create_and_insert_block(ctx: Ctx, a: BlockArgs) -> dict:
    """创建块（define_block，可带属性定义）、插入块（insert_block，可改比例/旋转/属性值）、修改插入块的属性值（edit_attributes）、重定义块（redefine，所有插入同步更新）、把块写成独立 DXF（wblock_export）、列出块（list）。keep_source 决定定义块后原图元保留/转成块参照（默认）/删除。"""
    d = ctx.d
    act = a.action
    if act == "list":
        use = {}
        for e in d.entities:
            if e.kind == "insert":
                use[e.data["block"]] = use.get(e.data["block"], 0) + 1
        rows = [{"name": b["name"], "entities": len(b.get("entities", [])), "attributes": [x["tag"] for x in b.get("attdefs", [])], "used": use.get(b["name"], 0), "xref": bool(b.get("xref")),
                 "dynamic": any(x["name"] == b["name"] for x in d.dynamic_blocks), "entity_ids": [e.get("id") for e in b.get("entities", [])][:40]} for b in d.blocks]
        return ctx.result(f"共 {len(rows)} 个块", blocks=rows)
    if act in ("define_block", "redefine"):
        _need(a.block_name, "需要 block_name")
        _need(a.source_entity_ids, "需要 source_entity_ids（组成块的图元）")
        exists = next((b for b in d.blocks if b["name"].lower() == a.block_name.lower()), None)
        if act == "define_block" and exists:
            raise ToolError("BLOCK_EXISTS", f"块「{a.block_name}」已存在", "换个名字，或用 action=redefine 重新定义")
        if act == "redefine" and not exists:
            raise ToolError("BLOCK_NOT_FOUND", f"没有块「{a.block_name}」，不能重定义", "")
        if exists and exists.get("xref"):
            raise ToolError("INVALID_ARGS", f"「{a.block_name}」是外部参照，不能重定义", "")
        ents = ctx.entities(a.source_entity_ids, for_edit=(a.keep_source != "retain"))
        if any(e.kind == "insert" and e.data.get("block", "").lower() == a.block_name.lower() for e in ents):
            raise ToolError("BLOCK_RECURSION", "块不能包含对自己的插入", "")
        if a.base_point is None:
            bbs = [b for b in (entity_bbox(e) for e in ents) if b]
            base = (min(b[0] for b in bbs), min(b[1] for b in bbs), 0.0)
            ctx.notes.append(f"基点未指定，取所选图元包围盒的左下角 ({base[0]:g},{base[1]:g})")
        else:
            base = ctx.point(a.base_point)
        attdefs = []
        for ad in a.attribute_defs or []:
            if any(x["tag"].upper() == ad.tag.upper() for x in attdefs):
                raise ToolError("INVALID_ARGS", f"属性标签重复：{ad.tag}", "")
            pos = ctx.point(ad.position, update_last=False) if ad.position is not None else (base[0], base[1] + 5 * (len(attdefs) + 1), 0.0)
            attdefs.append({"tag": ad.tag, "prompt": ad.prompt or ad.tag, "default_val": ad.default_val, "position": pl(pos), "height": ad.height, "invisible": ad.invisible})
        blk = {"name": a.block_name, "base_point": pl(base), "entities": _block_from_entities(ents, base, attdefs), "attdefs": attdefs}
        if exists:
            exists.update(blk)
            summary = f"重定义块「{a.block_name}」（{len(ents)} 个图元），{sum(1 for e in d.entities if e.kind == 'insert' and e.data['block'] == a.block_name)} 处插入同步更新"
            for e in d.entities:
                if e.kind == "insert" and e.data["block"] == a.block_name:
                    e.data["attribs"] = {ad["tag"]: e.data.get("attribs", {}).get(ad["tag"], ad["default_val"]) for ad in attdefs}
                    ctx.modified.append(e.id) if e.id not in ctx.modified else None
        else:
            d.blocks.append(blk)
            summary = f"定义块「{a.block_name}」：{len(ents)} 个图元，基点 ({base[0]:g},{base[1]:g})，{len(attdefs)} 个属性"
        if a.keep_source == "delete":
            for e in ents:
                ctx.remove(e)
            summary += "，原图元已删除"
        elif a.keep_source == "convert" and act == "define_block":
            for e in ents:
                ctx.remove(e)
            ins = ctx.add("insert", {"block": a.block_name, "p": pl(base), "scale": [1.0, 1.0, 1.0], "rotation": 0.0, "attribs": {x["tag"]: x["default_val"] for x in attdefs}}, a.layer or ents[0].layer)
            summary += f"，原图元已转成块参照 {ins.id}"
        else:
            summary += "，原图元保留"
        return ctx.result(summary, block=a.block_name, entity_ids_in_block=[x["id"] for x in blk["entities"]])
    if act == "insert_block":
        _need(a.block_name, "需要 block_name")
        blk = get_block(d, a.block_name)
        if blk.get("xref") and blk["xref"].get("status") == "unloaded":
            ctx.warnings.append("这是已卸载的外部参照，插入后不会显示内容")
        _need(a.insert_point is not None, "需要 insert_point", "向用户确认插入点")
        sc = list(a.scale or [1.0, 1.0, 1.0])
        if len(sc) == 1:
            sc = [sc[0], sc[0], sc[0]]
        if len(sc) == 2:
            sc = [sc[0], sc[1], 1.0]
        if len(sc) != 3 or any(abs(x) < 1e-9 for x in sc):
            raise ToolError("INVALID_ARGS", "比例必须是 [sx,sy,sz]，且不能为 0", "")
        p = ctx.point(a.insert_point)
        e = ctx.add("insert", {"block": blk["name"], "p": pl(p), "scale": sc, "rotation": a.rotation_deg, "attribs": _insert_attribs(blk, a.attribute_values, ctx)}, a.layer)
        return ctx.result(f"在 ({p[0]:g},{p[1]:g}) 插入块「{blk['name']}」{e.id}（比例 {sc}，旋转 {a.rotation_deg:g}°）", insert_id=e.id)
    if act == "edit_attributes":
        _need(a.block_ref_id, "需要 block_ref_id（块参照的 id）")
        e = ctx.entity(a.block_ref_id, for_edit=True)
        if e.kind != "insert":
            raise ToolError("INVALID_ARGS", f"{e.id} 不是块参照", "")
        blk = get_block(d, e.data["block"].split("@")[0] if "@" in e.data["block"] else e.data["block"])
        _need(a.attribute_values, "需要 attribute_values")
        vals = _insert_attribs({**blk, "attdefs": blk.get("attdefs", [])}, {**e.data.get("attribs", {}), **a.attribute_values}, ctx)
        ctx.update(e, {**e.data, "attribs": vals})
        return ctx.result(f"更新 {e.id} 的属性：{ {k: vals[k] for k in a.attribute_values if k in vals} }")
    if act == "wblock_export":
        _need(a.block_name, "需要 block_name")
        blk = get_block(d, a.block_name)
        from zhihui.cad.general_dxf import write_general_dxf
        from zhihui.tools.general.env_tools import new_general_drawing
        nd = new_general_drawing("blank_a3", blk["name"])
        nd.id = d.id
        for l in d.layers:
            if nd.get_layer(l.name) is None:
                nd.layers.append(copy.deepcopy(l))
        from zhihui.ir import GeoEntity
        base = blk.get("base_point", [0, 0, 0])
        for be in blk["entities"]:
            nd.entities.append(GeoEntity(id=nd.new_id(__import__("zhihui.ir.general", fromlist=["ID_PREFIX"]).ID_PREFIX[be["kind"]]), kind=be["kind"], layer=be["layer"] if nd.get_layer(be["layer"]) else "0",
                                         data=transform_data(be["kind"], be["data"], B.instance_transform(base, (0, 0, 0)))))
        p = ctx.export_dir() / f"{d.id}_{blk['name']}.dxf"
        write_general_dxf(nd, p)
        return ctx.result(f"块「{blk['name']}」已导出为独立 DXF（基点放在原点）", file=ctx.file_info(p))
    raise ToolError("INVALID_ARGS", f"未知操作 {act}", "")


# ------------------------------------------------------------------ 37 manage_dynamic_block
class DynParam(Args):
    param_name: str
    param_type: Literal["visibility", "linear_stretch", "flip", "rotation", "lookup"]
    geometry_refs: Any = None
    allowed_values: Optional[list] = None
    default_value: Any = None
    axis: Optional[Literal["x", "y"]] = None


class DynBlockArgs(Args):
    action: Literal["create_dynamic_def", "set_dynamic_property", "list_properties"]
    block_name: Optional[str] = None
    block_ref_id: Optional[str] = None
    parameters_and_actions: Optional[list[DynParam]] = None
    property_overrides: Optional[dict] = None


@tool("manage_dynamic_block", "block_xref_constraint", DynBlockArgs)
def manage_dynamic_block(ctx: Ctx, a: DynBlockArgs) -> dict:
    """动态块（等价实现：参数化块）。create_dynamic_def：给已有块定义参数（visibility 可见性、linear_stretch 线性拉伸、flip 翻转、rotation 旋转、lookup 查询表）；set_dynamic_property：改某个插入的参数，程序重新生成块几何；list_properties：列出参数和当前值。导出 DXF 后每组参数值对应一个静态块，在 AutoCAD 里不是原生动态块。"""
    d = ctx.d
    if a.action == "create_dynamic_def":
        _need(a.block_name, "需要 block_name（已有的块）")
        blk = get_block(d, a.block_name)
        _need(a.parameters_and_actions, "需要 parameters_and_actions")
        params = B.check_params(blk, [p.model_dump(exclude_none=True) for p in a.parameters_and_actions])
        d.dynamic_blocks = [x for x in d.dynamic_blocks if x["name"] != blk["name"]] + [{"name": blk["name"], "params": params}]
        return ctx.result(f"块「{blk['name']}」定义了 {len(params)} 个动态参数：{[p['param_name'] for p in params]}", defaults=B.defaults(params),
                          equivalent_note="等价实现：每组参数值生成一个静态块；AutoCAD 里打开不是原生动态块")
    if a.action == "list_properties":
        if a.block_ref_id:
            e = ctx.entity(a.block_ref_id)
            base = e.data.get("dyn", {}).get("base") or e.data.get("block")
            cur = e.data.get("dyn", {}).get("values")
        else:
            _need(a.block_name, "需要 block_name 或 block_ref_id")
            base, cur = a.block_name, None
        dyn = next((x for x in d.dynamic_blocks if x["name"] == base), None)
        if dyn is None:
            raise ToolError("NOT_DYNAMIC", f"块「{base}」没有动态参数", "先 create_dynamic_def")
        return ctx.result(f"块「{base}」有 {len(dyn['params'])} 个动态参数", params=[{k: v for k, v in p.items() if k != "geometry_refs"} | {"geometry_refs": p.get("geometry_refs")} for p in dyn["params"]], current=cur or B.defaults(dyn["params"]))
    _need(a.block_ref_id, "需要 block_ref_id（要改的块参照）")
    _need(a.property_overrides, "需要 property_overrides（参数名 → 新值）")
    e = ctx.entity(a.block_ref_id, for_edit=True)
    if e.kind != "insert":
        raise ToolError("INVALID_ARGS", f"{e.id} 不是块参照", "")
    base = e.data.get("dyn", {}).get("base") or e.data["block"]
    dyn = next((x for x in d.dynamic_blocks if x["name"] == base), None)
    if dyn is None:
        raise ToolError("NOT_DYNAMIC", f"块「{base}」没有动态参数", "先 create_dynamic_def")
    prev = e.data.get("dyn", {}).get("values") or B.defaults(dyn["params"])
    keep_prev = {k: v for k, v in prev.items() if k not in a.property_overrides}
    for p in dyn["params"]:                                    # 这次改了查询参数：它带出的参数值要覆盖旧值，不能被旧值挡住
        if p["param_type"] == "lookup" and p["param_name"] in a.property_overrides:
            row = next((x for x in p["allowed_values"] if x["label"] == a.property_overrides[p["param_name"]]), None)
            for k2 in (row or {}).get("params", {}):
                keep_prev.pop(k2, None)
    vals = B.resolve_values(dyn["params"], {**keep_prev, **a.property_overrides})
    name = B.variant_name(base, vals) if vals != B.defaults(dyn["params"]) else base
    if name != base and not any(b["name"] == name for b in d.blocks):
        v = B.build_variant(get_block(d, base), dyn["params"], vals)
        v["name"] = name
        d.blocks.append(v)
    ctx.update(e, {**e.data, "block": name, "dyn": {"base": base, "values": vals}})
    return ctx.result(f"{e.id} 的参数改为 {a.property_overrides}，使用块「{name}」", values=vals, variant=name)


# ------------------------------------------------------------------ 38 manage_external_references
class XrefArgs(Args):
    action: Literal["attach", "overlay", "reload", "unload", "detach", "bind", "xclip", "list"]
    xref_path_or_name: Optional[str] = None
    file_type: Optional[Literal["dxf", "dwg", "pdf", "image"]] = None
    insert_point: Pt = None
    scale: float = Field(1.0, gt=0)
    rotation_deg: float = 0.0
    clip_boundary_pts: Optional[list[Pt]] = None


def _file_type(path: Path, given: str | None) -> str:
    if given:
        return given
    ext = path.suffix.lower()
    return {".dxf": "dxf", ".dwg": "dwg", ".pdf": "pdf", ".png": "image", ".jpg": "image", ".jpeg": "image", ".bmp": "image", ".tif": "image", ".tiff": "image"}.get(ext, "")


def _clip_ents(ents: list[dict], clip: list | None) -> list[dict]:
    if not clip:
        return ents
    poly = Polygon([(p[0], p[1]) for p in clip])
    out = []
    from types import SimpleNamespace
    for e in ents:
        bb = entity_bbox(SimpleNamespace(id="x", kind=e["kind"], data=e["data"]))
        if bb is None or box(*bb).intersects(poly):
            out.append(e)
    return out


def _load_dxf_entities(path: Path) -> tuple[list[dict], tuple | None]:
    from zhihui.cad.general_dxf import read_any_dxf
    sub = read_any_dxf(path)
    ents = [{"id": f"E{i + 1}", "kind": e.kind, "data": e.data, "layer": e.layer, "tags": list(e.tags), "color": e.color, "linetype": e.linetype, "lineweight_mm": e.lineweight_mm} for i, e in enumerate(sub.entities)]
    return ents, sub.bbox()


def _placeholder(ftype: str, path: Path, scale: float) -> tuple[list[dict], tuple, bool]:
    """PDF / 图片外部参照：预览只画外框和文件名。图片用像素尺寸，PDF 无法读页面尺寸，按 A4 竖放估计。"""
    w, h, assumed = 210.0, 297.0, True
    if ftype == "image":
        try:
            from PIL import Image
            with Image.open(path) as im:
                w, h = float(im.size[0]), float(im.size[1])
                assumed = False
        except Exception:
            pass
    ents = [{"id": "E1", "kind": "polyline", "layer": "0", "tags": ["xref_frame"], "data": {"vertices": [{"p": [x, y, 0.0], "bulge": 0.0, "sw": 0.0, "ew": 0.0} for x, y in ((0, 0), (w, 0), (w, h), (0, h))], "closed": True, "width": 0.0}},
            {"id": "E2", "kind": "text", "layer": "0", "tags": [], "data": {"p": [5.0, h / 2, 0.0], "text": f"{ftype.upper()} 外部参照：{path.name}", "height": max(min(w, h) / 20, 2.0), "rotation": 0.0, "style": "Standard", "align": "left"}}]
    return ents, (0, 0, w, h), assumed


def _xref(d, name: str) -> dict:
    x = next((x for x in d.xrefs if x["name"].lower() == name.lower()), None)
    if x is None:
        raise ToolError("XREF_NOT_FOUND", f"没有外部参照「{name}」", f"现有：{[x['name'] for x in d.xrefs] or '无'}")
    return x


def _reload(ctx: Ctx, x: dict) -> None:
    """按记录重新读外部文件，刷新块缓存。"""
    path = ctx.svc.store.resolve_ref(x["path"]) if ctx.svc else Path(x["path"])
    blk = next(b for b in ctx.d.blocks if b["name"] == x["name"])
    if not path.exists():
        x["status"] = "missing"
        blk["entities"] = []
        ctx.warnings.append(f"外部参照「{x['name']}」的文件找不到：{x['path']}（保留引用，内容不显示）")
        return
    if x["file_type"] == "dxf":
        ents, bb = _load_dxf_entities(path)
        x["size_assumed"] = False
    elif x["file_type"] in ("pdf", "image"):
        ents, bb, assumed = _placeholder(x["file_type"], path, x["scale"])
        x["size_assumed"] = assumed
    else:
        x["status"] = "missing"
        raise ToolError("DWG_XREF_UNSUPPORTED", "DWG 外部参照需要 ODA 转换器读取内容，本机不能读取 DWG", "先把 DWG 另存为 DXF 再引用")
    blk["entities"] = _clip_ents(ents, x.get("clip"))
    x["status"] = "loaded"
    x["bbox"] = list(bb) if bb else None


@tool("manage_external_references", "block_xref_constraint", XrefArgs)
def manage_external_references(ctx: Ctx, a: XrefArgs) -> dict:
    """外部参照：attach / overlay（DXF 读入内容用于显示，导出时写真正的外部参照；PDF 写 PDFUNDERLAY，预览只画外框和文件名；图片写 IMAGE）、reload、unload、detach、bind（并入图中，变成普通块）、xclip（只在本系统预览和绑定时裁剪，不写入 DXF）、list。路径只能是任务给出的 file_ref。"""
    d = ctx.d
    act = a.action
    if act == "list":
        return ctx.result(f"共 {len(d.xrefs)} 个外部参照", xrefs=[{k: v for k, v in x.items() if k != "clip"} | {"clipped": bool(x.get("clip"))} for x in d.xrefs])
    if act in ("attach", "overlay"):
        _need(a.xref_path_or_name, "需要 xref_path_or_name（文件 file_ref）")
        try:
            path = ctx.svc.store.resolve_ref(a.xref_path_or_name)
        except PermissionError:
            raise ToolError("E_FORBIDDEN", "file_ref 越界", "只能使用任务给出的 file_ref")
        if not path.exists():
            raise ToolError("E_FILE_NOT_FOUND", f"找不到文件 {a.xref_path_or_name}", "确认上传文件的 file_ref")
        ft = _file_type(path, a.file_type)
        if ft not in ("dxf", "dwg", "pdf", "image"):
            raise ToolError("E_FILE_REJECTED", f"不认识的文件类型 {path.suffix}", "支持 DXF、DWG、PDF、图片")
        name = path.stem
        if any(b["name"].lower() == name.lower() for b in d.blocks):
            raise ToolError("BLOCK_EXISTS", f"已有同名的块或外部参照「{name}」", "先 detach，或把文件改名")
        _need(a.insert_point is not None, "需要 insert_point", "向用户确认插入点")
        p = ctx.point(a.insert_point)
        rec = {"name": name, "path": a.xref_path_or_name, "file_type": ft, "overlay": act == "overlay", "status": "loaded", "insert": pl(p), "scale": a.scale, "rotation": a.rotation_deg, "clip": [pl(ctx.point(c, False)) for c in a.clip_boundary_pts] if a.clip_boundary_pts else None}
        d.blocks.append({"name": name, "base_point": [0.0, 0.0, 0.0], "entities": [], "attdefs": [], "xref": {"path": a.xref_path_or_name, "file_type": ft, "overlay": act == "overlay", "status": "loaded"}})
        d.xrefs.append(rec)
        _reload(ctx, rec)
        d.blocks[-1]["xref"]["status"] = rec["status"]
        e = ctx.add("insert", {"block": name, "p": pl(p), "scale": [a.scale] * 3, "rotation": a.rotation_deg, "attribs": {}, "xref": True}, None, ["xref"])
        extra = ""
        if rec.get("size_assumed"):
            extra = "（PDF 页面尺寸读不到，预览按 A4 竖放估计，仅作占位）"
            ctx.warnings.append("PDF 外部参照的尺寸是估计值，只用于预览占位")
        if ft == "dxf":
            ctx.notes.append("导出 DXF 时写真正的外部参照（路径为相对 file_ref），目标机器上需要同一路径才能显示")
        return ctx.result(f"{'叠加' if act == 'overlay' else '附着'}外部参照「{name}」（{ft}）于 ({p[0]:g},{p[1]:g}){extra}", xref=name, insert_id=e.id, status=rec["status"])
    _need(a.xref_path_or_name, "需要 xref_path_or_name（外部参照名）")
    x = _xref(d, Path(a.xref_path_or_name).stem if "." in a.xref_path_or_name else a.xref_path_or_name)
    blk = get_block(d, x["name"])
    ins = [e for e in d.entities if e.kind == "insert" and e.data.get("block") == x["name"]]
    if act == "reload":
        _reload(ctx, x)
        blk["xref"]["status"] = x["status"]
        return ctx.result(f"重新载入「{x['name']}」：{x['status']}（{len(blk['entities'])} 个图元）")
    if act == "unload":
        x["status"], blk["xref"]["status"], blk["entities"] = "unloaded", "unloaded", []
        return ctx.result(f"已卸载「{x['name']}」（引用保留，内容不显示；reload 可恢复）")
    if act == "detach":
        for e in ins:
            ctx.remove(e)
        d.blocks = [b for b in d.blocks if b is not blk]
        d.xrefs = [y for y in d.xrefs if y is not x]
        return ctx.result(f"已拆离「{x['name']}」，移除 {len(ins)} 处引用")
    if act == "xclip":
        _need(a.clip_boundary_pts and len(a.clip_boundary_pts) >= 3, "需要 clip_boundary_pts（至少 3 个点；空表示取消裁剪）")
        x["clip"] = [pl(ctx.point(c, False)) for c in a.clip_boundary_pts]
        _reload(ctx, x)
        ctx.notes.append("裁剪框只在本系统预览和绑定时生效，不写入 DXF；保留与裁剪框相交的整个图元，不做逐段截断")
        return ctx.result(f"「{x['name']}」按裁剪框显示（{len(blk['entities'])} 个图元）")
    if act == "bind":
        if x["status"] != "loaded" or x["file_type"] not in ("dxf",):
            raise ToolError("XREF_NOT_BINDABLE", "只能绑定已载入的 DXF 外部参照", "先 reload；PDF/图片不能绑定")
        base = x["name"]
        for ent in blk["entities"]:
            lname = f"{base}$0${ent['layer']}"
            if d.get_layer(lname) is None:
                src = d.get_layer(ent["layer"])
                d.layers.append(Layer(name=lname, color=src.color if src else 7))
            ent["layer"] = lname
        blk.pop("xref", None)
        d.xrefs = [y for y in d.xrefs if y is not x]
        for e in ins:
            e.data.pop("xref", None)
            e.tags = [t for t in e.tags if t != "xref"]
            ctx.modified.append(e.id) if e.id not in ctx.modified else None
        return ctx.result(f"已绑定「{base}」：并入图中成为普通块，图层改名为 {base}$0$原图层名")
    raise ToolError("INVALID_ARGS", f"未知操作 {act}", "")
