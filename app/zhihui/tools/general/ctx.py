"""通用函数的执行上下文：点/对象解析、增删改图元、图层与锁定检查、会话回写。"""
from __future__ import annotations

from typing import Any

from zhihui.cad.geom import coords, modify, select
from zhihui.cad.geom.entities import Pt, P, pl
from zhihui.cad.geom.errors import GeomError
from zhihui.ir import Drawing, GeoEntity, Layer
from zhihui.ir.general import ID_PREFIX
from zhihui.tools.core import ToolError

EntityRef = Any


def to_tool_error(ex: GeomError) -> ToolError:
    return ToolError(ex.code, ex.message, ex.hint, **ex.extra)


class Ctx:
    def __init__(self, d: Drawing, tool: str = "", svc=None):
        self.d = d
        self.tool = tool
        self.svc = svc                    # ToolService（出图等需要访问文件库的函数用）
        from zhihui.cad.geom import blocks as _blocks

        _blocks.set_context(d.blocks)     # 块参照的包围盒要查块定义
        self.created: list[str] = []
        self.modified: list[str] = []
        self.deleted: list[str] = []
        self.data: dict = {}
        self.warnings: list[str] = []
        self.notes: list[str] = []        # 默认值说明 / 摘要片段
        self.coord = coords.CoordContext(d)

    # ------------------------------------------------------------ 点
    def point(self, expr, update_last: bool = True) -> Pt:
        try:
            r = coords.parse_point(expr, self.coord)
        except GeomError as ex:
            raise to_tool_error(ex)
        self.warnings += r["warnings"]
        p = P(r["point"])
        if update_last:
            self.d.session.last_point = p
        return p

    def points(self, exprs) -> list[Pt]:
        return [self.point(x) for x in (exprs or [])]

    def opt_point(self, expr) -> Pt | None:
        return None if expr is None else self.point(expr)

    # ------------------------------------------------------------ 对象
    def entity(self, ref: str, for_edit: bool = False) -> GeoEntity:
        e = self.d.get_entity(str(ref))
        if e is None:
            raise ToolError("ENTITY_NOT_FOUND", f"没有图元 {ref}", "用 query_entities_by_filter 查找，或检查 id / 句柄是否写对")
        if for_edit:
            self.check_unlocked(e)
        return e

    def entities(self, refs, for_edit: bool = False) -> list[GeoEntity]:
        if refs is None:
            return []
        if isinstance(refs, str):
            refs = [refs]
        out, seen = [], set()
        for r in refs:
            e = self.entity(r, for_edit)
            if e.id not in seen:
                seen.add(e.id)
                out.append(e)
        return out

    def check_unlocked(self, e: GeoEntity) -> None:
        l = self.d.layer_of(e)
        if l is not None and l.locked:
            raise ToolError("LAYER_LOCKED", f"图元 {e.id} 在锁定的图层「{l.name}」上，不能修改", f"先用 set_layer_state 解锁图层 {l.name}")

    # ------------------------------------------------------------ 图层
    def layer_name(self, layer: str | None) -> str:
        name = layer or self.d.current_layer or "0"
        l = self.d.get_layer(name)
        if l is None:
            if name == "0":
                self.d.layers.insert(0, Layer(name="0"))
                return "0"
            names = [x.name for x in self.d.layers]
            raise ToolError("LAYER_NOT_FOUND", f"图层「{name}」不存在", f"先用 manage_layers 创建，或从现有图层选择：{names}", layers=names)
        if l.locked:
            raise ToolError("LAYER_LOCKED", f"图层「{l.name}」已锁定，不能在上面绘图", f"先用 set_layer_state 解锁 {l.name}")
        return l.name

    # ------------------------------------------------------------ 增删改
    def add(self, kind: str, data: dict, layer: str | None = None, tags: list[str] | None = None, src: GeoEntity | None = None, **props) -> GeoEntity:
        ent = GeoEntity(id=self.d.new_id(ID_PREFIX[kind]), kind=kind, layer=self.layer_name(layer if layer or not src else src.layer), data=data, tags=list(tags or []),
                        created_rev=self.d.revision + 1, modified_rev=self.d.revision + 1)
        if src is not None:
            for key in ("color", "linetype", "linetype_scale", "lineweight_mm", "transparency"):
                setattr(ent, key, getattr(src, key))
            if layer is None:
                ent.layer = src.layer
        for key, v in props.items():
            if v is not None and hasattr(ent, key):
                setattr(ent, key, v)
        ent.space = self.d.active_space
        self.d.entities.append(ent)
        self.created.append(ent.id)
        return ent

    def update(self, e: GeoEntity, data: dict | None = None, **fields) -> None:
        self.check_unlocked(e)
        if data is not None:
            e.data = data
        for k, v in fields.items():
            setattr(e, k, v)
        e.modified_rev = self.d.revision + 1
        if e.id not in self.modified and e.id not in self.created:
            self.modified.append(e.id)

    def remove(self, e: GeoEntity) -> None:
        self.check_unlocked(e)
        self.d.entities = [x for x in self.d.entities if x.id != e.id]
        self.deleted.append(e.id)
        for lst in (self.created, self.modified):
            if e.id in lst:
                lst.remove(e.id)
        self.d.session.selection = [s for s in self.d.session.selection if s != e.id]

    def apply_ops(self, ops: list[dict]) -> None:
        for op in ops:
            if op["op"] == "create":
                src = self.d.get_entity(op["from"]) if op.get("from") else None
                self.add(op["kind"], op["data"], tags=op.get("tags"), src=src)
            elif op["op"] == "replace":
                e = self.entity(op["id"])
                self.update(e, op["data"])
                if op.get("kind"):
                    e.kind = op["kind"]
                if op.get("tags") is not None:
                    e.tags = op["tags"]
            elif op["op"] == "delete":
                self.remove(self.entity(op["id"]))

    def export_dir(self):
        if self.svc is None:
            raise ToolError("INTERNAL", "此处没有文件库，不能输出文件", "")
        return self.svc.store.export_dir(self.d)

    def file_info(self, path) -> dict:
        import hashlib
        data = path.read_bytes()
        return {"file_ref": self.svc.store.rel(path), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "format": path.suffix.lstrip(".")}

    # ------------------------------------------------------------ 结果
    def result(self, summary: str, **extra) -> dict:
        d = self.d
        out = {"created": list(self.created), "modified": list(self.modified), "deleted": list(self.deleted), "summary": summary}
        kinds = {e.id: e.kind for e in d.entities if e.id in self.created}
        if kinds:
            out["created_kinds"] = kinds
        if self.data or extra:
            out["data"] = {**self.data, **extra}
        if self.warnings:
            out["warnings"] = list(dict.fromkeys(self.warnings))
        if self.notes:
            out["defaults_used"] = self.notes
        return out

    def record_session(self) -> None:
        s = self.d.session
        by_id = {e.id: e for e in self.d.entities}
        items = []
        for op, ids in (("modified", self.modified), ("created", self.created)):
            for i in ids:
                e = by_id.get(i)
                if e is not None and "equip:auto" not in e.tags:            # 设备图生成的图元不进“最近图元”（否则“刚才那条线”会被几百个生成图元挤掉）
                    items.append({"entity_id": e.id, "type": e.kind, "tags": e.tags, "layer": e.layer, "op": op, "rev": self.d.revision + 1, "summary": select.summary(e)})
        s.recent_entities = (list(reversed(items)) + s.recent_entities)[:50]
        ids_alive = {e.id for e in self.d.entities}
        s.recent_entities = [r for r in s.recent_entities if r["entity_id"] in ids_alive]
