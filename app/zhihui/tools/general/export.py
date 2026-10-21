"""通用图纸的出图与概览（供 ToolService.export_artifacts / inspect_drawing 在 mode=="general" 时调用）。"""
from __future__ import annotations

import hashlib
import json
from collections import Counter

from zhihui.ir import Drawing
from zhihui.tools.core import ToolError


def inspect_general(d: Drawing, include_state: bool = False) -> dict:
    kinds = Counter(e.kind for e in d.entities)
    by_layer = Counter(e.layer for e in d.entities)
    out = {"ok": True, "drawing_id": d.id, "revision": d.revision, "title": d.title, "mode": "general", "units": "mm",
           "workspace": d.workspace, "limits": list(d.limits), "current_layer": d.current_layer,
           "layers": [{"name": l.name, "color": l.color, "linetype": l.linetype, "on": l.on, "frozen": l.frozen, "locked": l.locked, "count": by_layer.get(l.name, 0)} for l in d.layers],
           "entity_counts": dict(kinds), "entity_total": len(d.entities), "blocks": [b["name"] for b in d.blocks], "layouts": [l["name"] for l in d.layouts],
           "view": {"center": list(d.view.center), "height": d.view.height, "view3d": d.view.view3d, "visual_style": d.view.visual_style},
           "draft": d.draft.model_dump(mode="json")}
    bb = d.bbox()
    if bb:
        out["bbox"] = [round(v, 3) for v in bb]
    if include_state:
        out["state"] = d.model_dump(mode="json")
    return out


def export_general(svc, d: Drawing, formats: list[str] | None, allow_warnings: bool = False) -> dict:
    formats = formats or ["dxf", "png", "pdf"]
    bad = set(formats) - {"dxf", "png", "pdf", "svg", "json", "dwg", "stl"}
    if bad:
        raise ToolError("E_BAD_ARG", f"不支持的格式 {sorted(bad)}", "可选 dxf / png / svg / pdf / dwg / json / stl")
    if "stl" in formats and not any(e.kind in ("mesh", "solid3d") for e in d.entities):
        raise ToolError("NO_SOLID", "图中没有三维实体或网格，不能导出 STL", "先用 extrude_2d_to_3d 或 revolve_2d_to_3d 生成实体")
    v = svc.validate_drawing(d.id)
    if not v["passed"]:
        raise ToolError("E_NOT_VALID", "图纸未通过校验，不能导出", "先按 errors 修复", errors=v["errors"][:5])
    if "dwg" in formats:
        from zhihui.cad.dwg import find_oda
        if find_oda() is None:
            raise ToolError("E_NO_ODA", "导出 DWG 需要 ODA File Converter，本机没有安装", "去掉 dwg 只导出 DXF（AutoCAD 可直接打开）")
    from zhihui.cad.general_dxf import write_general_dxf
    from zhihui.cad.render_general import render_general
    out_dir = svc.store.export_dir(d)
    stem = f"{d.id}_r{d.revision}"
    files: list[dict] = []

    def add(fmt: str, p) -> None:
        data = p.read_bytes()
        files.append({"format": fmt, "file_ref": svc.store.rel(p), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})

    dxf_path = out_dir / f"{stem}.dxf"
    write_general_dxf(d, dxf_path, version=d.saved_dxf_version or "R2018")
    add("dxf", dxf_path) if "dxf" in formats else None
    lay = next((l for l in d.layouts if l.get("equip") and l["name"] == "设备图"), None) if d.vessel is not None else None
    for fmt in ("png", "svg", "pdf"):
        if fmt in formats:
            if lay is not None:                                 # 设备图：PNG/PDF/SVG 从布局「设备图」渲染（任务书 6.1 导出改动），另出一张模型空间预览
                from zhihui.cad.render_general import render_layout
                p, _ = render_layout(d, lay["name"], out_dir / f"{stem}.{fmt}")
                add(fmt, p)
                files[-1]["source"] = f"layout:{lay['name']}"
            else:
                add(fmt, render_general(d, out_dir / f"{stem}.{fmt}"))
    if lay is not None and "png" in formats:
        add("png", render_general(d, out_dir / f"{stem}_model.png"))
        files[-1]["source"] = "model"
        files[-1]["preview"] = "model"
    if "stl" in formats:
        from zhihui.cad.geom.solid import to_stl_ascii
        p = out_dir / f"{stem}.stl"
        p.write_text(to_stl_ascii(stem, [(e.data["vertices"], e.data["faces"]) for e in d.entities if e.kind in ("mesh", "solid3d")]), encoding="utf-8")
        add("stl", p)
    if "json" in formats:
        p = out_dir / f"{stem}.json"
        p.write_text(d.to_json(), encoding="utf-8")
        add("json", p)
    if "dwg" in formats:
        from zhihui.cad.dwg import to_dwg
        add("dwg", to_dwg(dxf_path, dxf_path.with_suffix(".dwg")))
    return {"ok": True, "drawing_id": d.id, "revision": d.revision, "files": files,
            "validation": {"passed": v["passed"], "warnings": len(v["warnings"]), "stats": v["stats"]}, "warnings": v["warnings"][:20]}
