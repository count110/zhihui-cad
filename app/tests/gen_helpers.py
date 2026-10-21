"""通用 CAD 测试的小工具：建图、调用函数、取图元、构造几何内核用的轻量图元。"""
from __future__ import annotations

import math
from types import SimpleNamespace

import zhihui.tools.general  # noqa: F401  注册 45+ 个函数
from zhihui.tools.core import ToolService
from zhihui.tools.general.registry import run_tool
from zhihui.tools.store import DrawingStore


class Env:
    def __init__(self, root):
        self.svc = ToolService(DrawingStore(root))

    def new(self, template: str = "gb_a3_mech", title: str = "t") -> str:
        r = run_tool(self.svc, None, "manage_document_file", {"action": "new", "template_name": template, "title": title})
        assert r["ok"], r
        return r["drawing_id"]

    def call(self, tool: str, did: str | None, ok: bool = True, **args) -> dict:
        r = run_tool(self.svc, did, tool, args)
        if ok:
            assert r.get("ok"), f"{tool}({args}) -> {r}"
        return r

    def err(self, tool: str, did: str | None, **args) -> dict:
        r = run_tool(self.svc, did, tool, args)
        assert not r.get("ok"), f"{tool}({args}) 应该失败，却成功了：{r}"
        return r

    def d(self, did: str):
        return self.svc.store.load(did)

    def ent(self, did: str, eid: str):
        e = self.d(did).get_entity(eid)
        assert e is not None, eid
        return e

    def ids(self, did: str, kind: str | None = None) -> list[str]:
        return [e.id for e in self.d(did).entities if kind is None or e.kind == kind]

    def rev(self, did: str) -> int:
        return self.d(did).revision


def E(kind: str, data: dict, id_: str = "X1", tags=None, layer: str = "0"):
    """几何内核用的轻量图元（只有 id / kind / data / tags / layer）。"""
    return SimpleNamespace(id=id_, kind=kind, data=data, tags=tags or [], layer=layer)


def LINE(p1, p2, id_="L1"):
    return E("line", {"p1": [p1[0], p1[1], 0.0], "p2": [p2[0], p2[1], 0.0]}, id_)


def CIRCLE(c, r, id_="C1"):
    return E("circle", {"c": [c[0], c[1], 0.0], "r": r}, id_)


def RECT(x0, y0, x1, y1, id_="PL1"):
    v = [{"p": [x, y, 0.0], "bulge": 0.0, "sw": 0.0, "ew": 0.0} for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
    return E("polyline", {"vertices": v, "closed": True, "width": 0.0}, id_, ["rectangle"])


def close(a, b, tol=1e-6):
    return abs(a - b) <= tol


def pclose(p, q, tol=1e-6):
    return math.hypot(p[0] - q[0], p[1] - q[1]) <= tol
