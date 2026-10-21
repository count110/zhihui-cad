"""通用 CAD 图纸的校验：只查「图是否自洽、能否正确落成 DXF」，不替用户判断设计对不对。

error 阻止导出；warning 提醒（界限外、重复图元、关联断开、尺寸文字被改写…）。结果格式与流程图校验器一致：
{"passed", "errors":[{code,message,refs,hint}], "warnings":[...], "stats":{}}。
"""
from __future__ import annotations

import json
import math
from collections import Counter

from zhihui.ir import Drawing
from .geom import entities as ge


def _finite(x) -> bool:
    if isinstance(x, bool):
        return True
    if isinstance(x, (int, float)):
        return math.isfinite(x)
    if isinstance(x, (list, tuple)):
        return all(_finite(v) for v in x)
    if isinstance(x, dict):
        return all(_finite(v) for v in x.values())
    return True


def _err(code, msg, refs=(), hint=""):
    return {"code": code, "message": msg, "refs": list(refs), "hint": hint}


def validate_general(d: Drawing) -> dict:
    errors: list[dict] = []
    warnings: list[dict] = []
    layers = {l.name for l in d.layers}
    blocks = {b["name"] for b in d.blocks}
    ids = Counter(e.id for e in d.entities)
    for k, n in ids.items():
        if n > 1:
            errors.append(_err("E_DUP_ID", f"图元编号 {k} 重复 {n} 次", [k], "编号必须唯一，请重新生成"))
    sigs: dict[str, str] = {}
    for e in d.entities:
        if not _finite(e.data):
            errors.append(_err("E_NONFINITE", f"{e.id} 含有非法数值（NaN/无穷）", [e.id], "删除或重画这个图元"))
            continue
        if e.layer not in layers:
            errors.append(_err("E_LAYER_MISSING", f"{e.id} 所在图层「{e.layer}」不存在", [e.id], "先新建这个图层，或把图元改到已有图层"))
        k, dd = e.kind, e.data
        if k == "circle" and dd.get("r", 0) <= 0:
            errors.append(_err("E_ZERO_SIZE", f"{e.id} 圆半径必须大于 0", [e.id]))
        elif k == "arc" and (dd.get("r", 0) <= 0 or abs(ge.ccw_span(dd["a0"], dd["a1"])) < 1e-9):
            errors.append(_err("E_ZERO_SIZE", f"{e.id} 圆弧半径或角度范围为 0", [e.id]))
        elif k == "line" and ge.dist(ge.P(dd["p1"]), ge.P(dd["p2"])) < 1e-9:
            warnings.append(_err("W_ZERO_LENGTH", f"{e.id} 直线长度为 0", [e.id], "多半是误操作，可删除"))
        elif k == "polyline" and len(dd.get("vertices", [])) < 2:
            errors.append(_err("E_ZERO_SIZE", f"{e.id} 多段线至少需要 2 个顶点", [e.id]))
        elif k == "ellipse" and not (0 < dd.get("ratio", 0) <= 1):
            errors.append(_err("E_BAD_ELLIPSE", f"{e.id} 椭圆短长轴比必须在 (0,1]", [e.id]))
        elif k == "insert" and dd.get("block") not in blocks:
            errors.append(_err("E_BLOCK_MISSING", f"{e.id} 引用的块「{dd.get('block')}」不存在", [e.id], "先定义这个块，或删除插入"))
        elif k == "hatch" and not dd.get("loops"):
            errors.append(_err("E_HATCH_EMPTY", f"{e.id} 填充没有边界", [e.id], "重新指定填充边界"))
        elif k == "hatch" and dd.get("associative") and any(s not in ids for s in dd.get("source_ids", [])):
            warnings.append(_err("W_HATCH_DETACHED", f"{e.id} 的关联边界已被删除，填充保留为非关联", [e.id]))
        elif k == "dimension" and dd.get("text") and "<>" not in dd.get("text", ""):
            warnings.append(_err("W_DIM_TEXT_OVERRIDE", f"{e.id} 尺寸文字被改写为「{dd['text']}」，不再随几何变化", [e.id], "确认是有意改写"))
        if d.limits_check:
            b = ge.entity_bbox(e)
            if b and (b[0] < d.limits[0] - 1e-6 or b[1] < d.limits[1] - 1e-6 or b[2] > d.limits[2] + 1e-6 or b[3] > d.limits[3] + 1e-6):
                warnings.append(_err("W_OUTSIDE_LIMITS", f"{e.id} 超出图形界限", [e.id]))
        if k in ("line", "circle", "arc", "polyline", "text", "point"):
            sig = f"{k}|{e.layer}|{json.dumps(dd, sort_keys=True, default=str)}"
            if sig in sigs:
                warnings.append(_err("W_DUPLICATE", f"{e.id} 与 {sigs[sig]} 完全重合", [e.id, sigs[sig]], "重复图元会让出图线变粗、数量统计翻倍，建议删除一个"))
            else:
                sigs[sig] = e.id
    if d.current_layer not in layers:
        errors.append(_err("E_CURRENT_LAYER", f"当前图层「{d.current_layer}」不存在", [], "把当前图层设为已有图层"))
    for lay in d.layers:
        if lay.name == d.current_layer and (lay.frozen or lay.locked):
            warnings.append(_err("W_CURRENT_LAYER_LOCKED", f"当前图层「{lay.name}」被冻结或锁定，新图元画不上去", [], ""))
    kinds = Counter(e.kind for e in d.entities)
    return {"passed": not errors, "errors": errors, "warnings": warnings,
            "stats": {"entities": len(d.entities), "layers": len(d.layers), "kinds": dict(kinds), "blocks": len(d.blocks), "mode": "general"}}
