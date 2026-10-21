"""块：插入变换、块范围、动态块（参数化块）的变体生成。

块里的图元用「块坐标」（和定义时在模型空间里的坐标相同），插入时先减去基点，再缩放、旋转、平移到插入点。
动态块是等价实现：参数改了就重新生成一个静态块（名字 = 基础块名@参数哈希），插入引用变体块；导出 DXF 后是普通块，不是 AutoCAD 原生动态块。
"""
from __future__ import annotations

import copy
import hashlib
import json
import threading
from types import SimpleNamespace

from .entities import P, Transform, entity_bbox, pl, transform_data
from .errors import GeomError

_ctx = threading.local()


def set_context(blocks: list[dict] | None) -> None:
    """让 entity_bbox 之类的函数知道当前图纸的块定义（线程内有效）。"""
    _ctx.blocks = {b["name"]: b for b in (blocks or [])}


def get_context() -> dict:
    return getattr(_ctx, "blocks", {})


def instance_transform(base, insert, scale=(1, 1, 1), rotation: float = 0.0) -> Transform:
    T = Transform.translation(-base[0], -base[1], 0.0)
    T = T.then(Transform(scale[0], 0, 0, scale[1], 0, 0, sz=scale[2]))
    T = T.then(Transform.rotation((0, 0, 0), rotation))
    return T.then(Transform.translation(insert[0], insert[1], insert[2] if len(insert) > 2 else 0.0))


def block_entities(blk: dict) -> list[SimpleNamespace]:
    return [SimpleNamespace(id=be.get("id", f"b{i}"), kind=be["kind"], data=be["data"], layer=be.get("layer", "0"), tags=be.get("tags", [])) for i, be in enumerate(blk.get("entities", []))]


def block_bbox(blk: dict, T: Transform | None = None) -> tuple[float, float, float, float] | None:
    boxes = []
    for e in block_entities(blk):
        data = transform_data(e.kind, e.data, T) if T else e.data
        b = entity_bbox(SimpleNamespace(id=e.id, kind=e.kind, data=data))
        if b:
            boxes.append(b)
    if not boxes:
        return None
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))


def insert_bbox(data: dict, blocks: dict | None = None) -> tuple[float, float, float, float] | None:
    blk = (blocks if blocks is not None else get_context()).get(data.get("block"))
    p = P(data["p"])
    if blk is None:
        return (p[0], p[1], p[0], p[1])
    T = instance_transform(blk.get("base_point", (0, 0, 0)), p, data.get("scale", (1, 1, 1)), float(data.get("rotation", 0)))
    return block_bbox(blk, T) or (p[0], p[1], p[0], p[1])


# ------------------------------------------------------------------ 动态块
PARAM_TYPES = ("visibility", "linear_stretch", "flip", "rotation", "lookup")


def check_params(blk: dict, params: list[dict]) -> list[dict]:
    """校验参数定义，补默认值。geometry_refs 引用块内图元 id。"""
    ids = {e.id for e in block_entities(blk)}
    out = []
    names = set()
    for p in params:
        name, t = p.get("param_name"), p.get("param_type")
        if not name or t not in PARAM_TYPES:
            raise GeomError("INVALID_ARGS", f"参数定义不对：{p}", f"param_name 必填，param_type 只能是 {PARAM_TYPES}")
        if name in names:
            raise GeomError("INVALID_ARGS", f"参数名重复：{name}", "")
        names.add(name)
        q = dict(p)
        refs = p.get("geometry_refs") or []
        flat = [r for r in (refs.values() if isinstance(refs, dict) else [refs]) for r in (r if isinstance(r, list) else [r])] if isinstance(refs, dict) else refs
        bad = [r for r in flat if r not in ids]
        if bad:
            raise GeomError("ENTITY_NOT_FOUND", f"参数 {name} 引用的块内图元不存在：{bad}", f"块内图元：{sorted(ids)}")
        if t == "linear_stretch":
            q.setdefault("axis", "x")
            if q["axis"] not in ("x", "y"):
                raise GeomError("INVALID_ARGS", f"{name}: axis 只能是 x 或 y", "")
            if q.get("default_value") is None:
                raise GeomError("MISSING_ARG", f"线性拉伸参数 {name} 需要 default_value（当前几何对应的数值）", "向用户确认")
            if not flat:
                raise GeomError("INVALID_ARGS", f"线性拉伸参数 {name} 没有 geometry_refs：不写就没有图元会随参数拉伸，参数等于摆设", f"geometry_refs 写要随参数移动的块内图元 id 列表；这个块里的图元：{sorted(ids)}")
        elif t == "visibility":
            if not isinstance(refs, dict) or not refs:
                raise GeomError("INVALID_ARGS", f"可见性参数 {name} 的 geometry_refs 要写成 {{状态名: [图元 id…]}}", "")
            q.setdefault("allowed_values", list(refs.keys()))
            q.setdefault("default_value", q["allowed_values"][0])
        elif t == "flip":
            q.setdefault("default_value", False)
            q.setdefault("axis", "y")
        elif t == "rotation":
            q.setdefault("default_value", 0.0)
        elif t == "lookup":
            rows = []
            for x in q.get("allowed_values") or []:
                if isinstance(x, (str, int, float)):                    # 只有名字的写法：[“M6”, “M8”]
                    rows.append({"label": str(x), "params": {}})
                elif isinstance(x, dict) and "label" in x:
                    rows.append({"label": str(x["label"]), "params": x.get("params") or {}})
                else:
                    raise GeomError("INVALID_ARGS", f"查询参数 {name} 的 allowed_values 每项写成 {{label, params:{{其他参数名: 值}}}} 或直接写名字", "")
            if not rows:
                raise GeomError("INVALID_ARGS", f"查询参数 {name} 需要 allowed_values: [{{label, params:{{其他参数名: 值}}}}]", "")
            q["allowed_values"] = rows
            q.setdefault("default_value", rows[0]["label"])
        out.append(q)
    return out


def defaults(params: list[dict]) -> dict:
    return {p["param_name"]: p.get("default_value") for p in params}


def resolve_values(params: list[dict], overrides: dict | None) -> dict:
    vals = defaults(params)
    byname = {p["param_name"]: p for p in params}
    for k, v in (overrides or {}).items():
        if k not in byname:
            raise GeomError("PARAM_NOT_FOUND", f"没有参数「{k}」", f"可用参数：{list(byname)}")
        t = byname[k]["param_type"]
        if t == "visibility" and v not in byname[k]["allowed_values"]:
            raise GeomError("INVALID_ARGS", f"可见性状态「{v}」不存在", f"可选：{byname[k]['allowed_values']}")
        if t == "lookup" and v not in [x["label"] for x in byname[k]["allowed_values"]]:
            raise GeomError("INVALID_ARGS", f"查询值「{v}」不存在", f"可选：{[x['label'] for x in byname[k]['allowed_values']]}")
        if t in ("linear_stretch", "rotation") and not isinstance(v, (int, float)):
            raise GeomError("INVALID_ARGS", f"参数 {k} 需要数值", "")
        vals[k] = v
    for p in params:                                               # lookup 带出其他参数
        if p["param_type"] == "lookup":
            row = next(x for x in p["allowed_values"] if x["label"] == vals[p["param_name"]])
            for k2, v2 in (row.get("params") or {}).items():
                if k2 in byname and k2 not in (overrides or {}):
                    vals[k2] = v2
    return vals


def variant_name(base: str, vals: dict) -> str:
    h = hashlib.sha1(json.dumps(vals, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()[:6]
    return f"{base}@{h}"


def _move_points(kind: str, data: dict, dx: float, dy: float) -> dict:
    return transform_data(kind, data, Transform.translation(dx, dy))


def build_variant(base_blk: dict, params: list[dict], vals: dict) -> dict:
    """按参数值重新生成块几何，返回新的块定义（名字由调用方设置）。"""
    ents = copy.deepcopy(base_blk.get("entities", []))
    for i, e in enumerate(ents):
        e.setdefault("id", f"b{i}")
    by_id = {e["id"]: e for e in ents}
    hidden: set[str] = set()
    base = base_blk.get("base_point", [0, 0, 0])
    for p in params:
        name, t = p["param_name"], p["param_type"]
        v = vals.get(name)
        if t == "visibility":
            refs = p["geometry_refs"]
            shown = set(refs.get(v, []))
            hidden |= {i for lst in refs.values() for i in lst} - shown
        elif t == "linear_stretch":
            delta = float(v) - float(p["default_value"])
            dx, dy = (delta, 0.0) if p["axis"] == "x" else (0.0, delta)
            for r in p.get("geometry_refs") or []:
                e = by_id[r]
                e["data"] = _move_points(e["kind"], e["data"], dx, dy)
        elif t == "rotation":
            T = Transform.rotation((base[0], base[1]), float(v) - float(p["default_value"]))
            for r in p.get("geometry_refs") or [e["id"] for e in ents]:
                e = by_id[r]
                e["data"] = transform_data(e["kind"], e["data"], T)
        elif t == "flip" and v:
            ax = p.get("axis", "y")
            a, b = ((base[0], base[1]), (base[0], base[1] + 1)) if ax == "y" else ((base[0], base[1]), (base[0] + 1, base[1]))
            T = Transform.mirror(a, b)
            for r in p.get("geometry_refs") or [e["id"] for e in ents]:
                e = by_id[r]
                e["data"] = transform_data(e["kind"], e["data"], T)
    ents = [e for e in ents if e["id"] not in hidden]
    out = copy.deepcopy(base_blk)
    out["entities"] = ents
    return out
