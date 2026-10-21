"""化工设备图函数（任务书 6.1，意图类别 chem_equipment）：vessel_define / vessel_generate_drawing / vessel_query / vessel_lookup_standard / vessel_check。

设备图 = 通用图纸 + Drawing.vessel（容器参数）。改图一律改参数再重新生成（补丁和重新生成在同一个 _mutate 事务里，一次成功修改只落一个版本）；
缺必问参数时报 SPEC_INCOMPLETE（图纸不变、不落版本），引擎直接转成反问。这里只有确定性代码，不调用大模型。
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import Field

from zhihui.cad.equipment import check as CHK
from zhihui.cad.equipment import sheet as SH
from zhihui.cad.equipment import spec_ops as S
from zhihui.cad.equipment import std_tables as T
from zhihui.cad.geom.errors import GeomError
from zhihui.ir.vessel import NozzleIn, SupportIn, VesselPatchIn
from zhihui.tools.core import ToolError
from .ctx import Ctx, to_tool_error
from .registry import Args, tool

CAT = "chem_equipment"
RULE_WORDS = {"R-HEAD-01": "封头型式按 EHA 标准椭圆封头", "R-HEAD-02": "封头厚度同筒体", "R-MAT-01": "封头材料同筒体", "head_eha": "封头直边高度查表",
              "R-SUP-01": "支座数量", "R-SUP-02": "支座型号", "R-SUP-POS-01": "鞍座位置", "R-SUP-POS-02": "耳座方位和高度", "R-NOZ-LOC-01": "管口位置（上/下封头、筒体）",
              "R-NOZ-LOC-02": "管口位置", "R-NOZ-POS-01": "筒体管口轴向位置", "R-NOZ-POS-02": "封头管口径向位置", "R-NOZ-ORI-01": "管口方位", "R-NOZ-PROJ-01": "管口伸出长度",
              "R-PIPE-01": "接管系列（B 系列）", "R-PN-01": "管口 PN（初选，待校核）", "R-FLG-01": "法兰型式和密封面（初选，待校核）", "R-MH-01": "人孔标准（初选，待校核）",
              "R-STD-01": "设计、制造与检验标准", "R-SHEET-01": "幅面 A1", "R-MARK-01": "管口符号"}


def _err(ex: GeomError) -> ToolError:
    return to_tool_error(ex)


GROUPS = [("封头型式、厚度、材料", {"R-HEAD-01", "R-HEAD-02", "R-MAT-01"}), ("支座数量、型号、位置", {"R-SUP-01", "R-SUP-02", "R-SUP-POS-01", "R-SUP-POS-02"}),
          ("管口位置、方位、伸出长度", {"R-NOZ-LOC-01", "R-NOZ-LOC-02", "R-NOZ-POS-01", "R-NOZ-POS-02", "R-NOZ-ORI-01", "R-NOZ-PROJ-01"}),
          ("管口 PN、法兰型式、人孔标准（初选，待校核）", {"R-PN-01", "R-FLG-01", "R-MH-01"}), ("接管 B 系列", {"R-PIPE-01"}), ("设计标准", {"R-STD-01"}),
          ("幅面 A1", {"R-SHEET-01"}), ("管口符号", {"R-MARK-01"})]


def defaults_text(spec, defaults: list[dict]) -> list[str]:
    """回复里“按默认，待确认”的清单（由代码按实际用了哪些默认值生成，不让模型自己写）。"""
    used = {x["rule_id"] for x in defaults}
    marks: list[str] = []
    for x in defaults:
        if x["rule_id"].startswith("R-NOZ"):
            p = S.display_path(spec, x["path"])
            if p.startswith("nozzles["):
                marks.append(p.split("]")[0][len("nozzles["):])
    out = []
    for word, ids in GROUPS:
        if used & ids:
            out.append(word + (f"（{'、'.join(dict.fromkeys(marks))}）" if "R-NOZ-ORI-01" in ids and marks else ""))
    return ["按默认，待确认：" + "；".join(out)] if out else []


class VesselDefineArgs(Args):
    op: Literal["create", "patch", "delete"]
    spec: Optional[VesselPatchIn] = Field(None, description="op=create：只写用户说了的值")
    set: Optional[dict[str, Any]] = Field(None, description="op=patch：{路径: 新值}，管口用选择器 nozzles[b] / nozzles[purpose=进料口] / nozzles[location=top_head]")
    add: Optional[dict[str, Any]] = Field(None, description="op=patch：相对改动 {路径: 增量}，如 {\"shell.length\": 500}")
    upsert_nozzles: Optional[list[NozzleIn]] = None
    remove_nozzles: Optional[list[str]] = Field(None, description="要删的管口：符号或选择器")
    upsert_supports: Optional[list[SupportIn]] = None
    remove_supports: Optional[list[str]] = None
    renumber: bool = Field(False, description="用户明确说“重新编号”时才为 true")
    regenerate: bool = True
    sheet: Optional[Literal["A0", "A1", "A2", "A3"]] = None
    scale: Optional[str] = None
    turn_id: Optional[str] = Field(None, description="由引擎注入，不要写")
    confirmed: bool = False


def _user_entities(d) -> list:
    return [e for e in d.entities if not any(t.startswith("equip:") for t in e.tags)]


def regenerate(ctx: Ctx, spec, extra_layouts: list[str] | None = None) -> dict:
    """参数 → 图（差量写入），并跑自检。返回 {summary_counts, check, plan}。"""
    d = ctx.d
    d.vessel = spec
    try:
        plan = SH.compose(spec, (spec.drawing.get("title_block") or {}), extra_layouts=extra_layouts if extra_layouts is not None else spec.drawing.get("extra_layouts"))
    except GeomError as ex:
        raise _err(ex)
    spec.drawing["record"] = plan.record
    spec.warnings = list(dict.fromkeys(spec.warnings + plan.warnings))
    res = SH.apply(d, plan, ctx)
    chk = CHK.run(spec, d)
    ctx.notes += [n for n in plan.notes if n not in ctx.notes]
    return {"counts": res, "check": chk, "plan": plan}


def _finish(ctx: Ctx, spec, info: dict, regen: dict | None, verb: str) -> dict:
    notes = defaults_text(spec, info["defaults"]) + list(dict.fromkeys(info["notes"]))
    ctx.notes += [n for n in notes if n not in ctx.notes]
    ws = list(info["warnings"])
    if spec.drawing.get("unverified_tables"):
        ws.append("W_UNVERIFIED_TABLE：用到了未核对的标准数据（" + "、".join(t for t in spec.drawing["unverified_tables"] if t != "rules") + "），图上技术要求已注明，仅供方案阶段使用")
    errs = []
    if regen:
        errs = [e["message"] for e in regen["check"]["errors"]]
        if errs:
            ws.append("设备图自检有错误（导出 DXF/PDF 交付件前需要处理）：" + "；".join(errs[:4]))
    ctx.warnings += [w for w in ws if w not in ctx.warnings]
    c = regen["counts"] if regen else {"created": [], "modified": [], "deleted": [], "unchanged": 0}
    rec = spec.drawing.get("record") or {}
    summ = f"{verb}：{S.summary(spec)}" + (f"；主视图 1:{S.fmt_value(rec.get('den_main'))}，{'管口方位图' if spec.vertical else '左视图'} 1:{S.fmt_value(rec.get('den_end'))}，布局「{SH.LAYOUT}」" if rec else "")
    if regen:
        summ += f"（新增 {len(c['created'])}、更新 {len(c['modified'])}、删除 {len(c['deleted'])} 个图元）"
    return ctx.result(summ, vessel_summary=S.summary(spec), defaults_used=[{"path": S.display_path(spec, x["path"]), "rule_id": x["rule_id"], "value": x["value"]} for x in info["defaults"]],
                      pending=[], warnings_list=ws, check_errors=errs, marks=[n.mark for n in spec.nozzles], layout=SH.LAYOUT if rec else None,
                      unchanged=c.get("unchanged", 0))


@tool("vessel_define", CAT, VesselDefineArgs)
def vessel_define(ctx: Ctx, a: VesselDefineArgs) -> dict:
    """化工设备图的参数：create 新建（spec 里只写用户说了的值）/ patch 改参数（set、add、upsert_nozzles、remove_nozzles、upsert_supports、remove_supports）/ delete 删除。
    程序按标准表和默认规则补齐其余参数，并从参数重新生成整张设备图（主视图、管口方位图或左视图、管口表、设计数据表、明细栏、技术要求、标题栏）。缺必问参数时报 SPEC_INCOMPLETE 并列出待问项。"""
    d = ctx.d
    turn = a.turn_id or f"r{d.revision + 1}"
    if a.op == "delete":
        if d.vessel is None:
            raise ToolError("NO_VESSEL", "当前图纸没有设备参数", "")
        if not a.confirmed:
            raise ToolError("CONFIRM_REQUIRED", "要删除设备参数和全部生成的图元", "确认后再删")
        for e in [e for e in d.entities if "equip:auto" in e.tags]:
            ctx.remove(e)
        d.layouts = [l for l in d.layouts if not l.get("equip")]
        d.vessel = None
        return ctx.result("已删除设备参数和生成的图元（手工画的图元保留）")
    try:
        if a.op == "create":
            if d.vessel is not None and not a.confirmed:
                raise ToolError("CONFIRM_REQUIRED", f"当前图纸已有设备参数（{S.summary(d.vessel)}），新建会替换它", "改参数请用 op=patch；确认替换再执行")
            if _user_entities(d) and not a.confirmed:
                raise ToolError("CONFIRM_REQUIRED", f"当前图纸里已有 {len(_user_entities(d))} 个手工图元，设备图会画在同一张图上", "建议新建一张化工设备图（样板 hg_equipment_a1）；确认在这张图上画再执行")
            spec, changed, warns = S.apply_create(a.spec or VesselPatchIn(), turn)
        else:
            if d.vessel is None:
                raise ToolError("NO_VESSEL", "当前图纸还没有设备参数，不能 patch", "先用 vessel_define(op=create) 新建")
            ops = {"set": a.set, "add": a.add, "upsert_nozzles": a.upsert_nozzles, "remove_nozzles": a.remove_nozzles, "upsert_supports": a.upsert_supports,
                   "remove_supports": a.remove_supports, "renumber": a.renumber}
            if not any(v for v in ops.values()) and not (a.sheet or a.scale):
                raise ToolError("MISSING_ARG", "patch 没有要改的内容", "写 set / add / upsert_nozzles / remove_nozzles 等")
            spec, changed, warns = S.apply_patch(d.vessel, ops, turn)
        for f, v in (("sheet", a.sheet), ("scale", a.scale)):
            if v:
                if f == "scale":
                    from zhihui.cad.geom import layout as LYT
                    if not LYT.is_standard_scale(v):
                        raise ToolError("NONSTANDARD_SCALE", f"比例 {v} 不是 GB/T 14690 的标准比例", f"可选：{LYT.STANDARD_SCALES + LYT.SECOND_SCALES}")
                spec.drawing[f] = v
                spec.sources[f"drawing.{f}"] = f"user:{turn}"
        info = S.fill_defaults(spec)
        info["warnings"] = list(dict.fromkeys(warns + info["warnings"]))
    except GeomError as ex:
        raise _err(ex)
    pend = S.missing(spec)
    if pend:
        raise ToolError("SPEC_INCOMPLETE", S.question_text(pend), "引擎会把这些项一次反问用户；不要编造", pending=pend)
    spec.pending = []
    regen = regenerate(ctx, spec) if a.regenerate else None
    if not a.regenerate:
        d.vessel = spec
    return _finish(ctx, spec, info, regen, "已新建设备参数并生成设备图" if a.op == "create" else "已修改设备参数并重新生成")


class VesselDrawArgs(Args):
    views: Optional[list[Literal["main", "orientation"]]] = Field(None, description="要出的视图；orientation = 另出一张“管口方位图”（立式俯视方位图 / 卧式左视图）")
    sheet: Optional[Literal["A0", "A1", "A2", "A3"]] = None
    scale: Optional[str] = None
    export: Optional[list[Literal["dxf", "pdf", "png", "svg"]]] = Field(None, description="要导出的文件；PDF/PNG 从布局「设备图」渲染")


@tool("vessel_generate_drawing", CAT, VesselDrawArgs, quiet_if=lambda a: bool(a.export) and not a.views and not a.sheet and not a.scale)
def vessel_generate_drawing(ctx: Ctx, a: VesselDrawArgs) -> dict:
    """参数不变时重新排版或出图：换幅面/比例、另出一张管口方位图（views=["orientation"]）、导出 DXF/PDF（export=["dxf","pdf"]，PDF 从布局「设备图」渲染；自检有错误时不导出交付件）。"""
    d = ctx.d
    if d.vessel is None:
        raise ToolError("NO_VESSEL", "当前图纸还没有设备参数", "先用 vessel_define(op=create) 新建")
    spec = d.vessel.model_copy(deep=True)
    turn = f"r{d.revision + 1}"
    for f, v in (("sheet", a.sheet), ("scale", a.scale)):
        if v:
            spec.drawing[f] = v
            spec.sources[f"drawing.{f}"] = f"user:{turn}"
    if a.views and "orientation" in a.views:
        spec.drawing["extra_layouts"] = [SH.ORI_LAYOUT]
    files: list[dict] = []
    if a.views or a.sheet or a.scale:
        try:
            info = S.fill_defaults(spec)
        except GeomError as ex:
            raise _err(ex)
        regen = regenerate(ctx, spec)
        out = _finish(ctx, spec, info, regen, "已重新排版设备图" + ("，另出一张「管口方位图」" if a.views and "orientation" in a.views else ""))
    else:
        out = ctx.result("参数没有变，图不用重新生成")
    if a.export:
        chk = CHK.run(d.vessel, d)
        if chk["errors"] and any(f in ("dxf", "pdf") for f in a.export):
            raise ToolError("E_VESSEL_CHECK", "设备图自检有错误，不能导出交付件：" + "；".join(e["message"] for e in chk["errors"][:4]), "先按提示改参数（vessel_define patch），预览照常可看")
        files = export_files(ctx, a.export)
        out.setdefault("data", {})["files"] = files
        out["summary"] += "；已导出 " + "、".join(f"{f['format'].upper()}" for f in files) + f"（PDF/PNG 从布局「{SH.LAYOUT}」渲染）"
    return out


def export_files(ctx: Ctx, formats: list[str]) -> list[dict]:
    from zhihui.cad.general_dxf import write_general_dxf
    from zhihui.cad.render_general import render_layout
    d = ctx.d
    out_dir = ctx.export_dir()
    stem = f"{d.id}_r{d.revision}"
    files = []
    if "dxf" in formats:
        p = out_dir / f"{stem}_设备图.dxf"
        write_general_dxf(d, p, version=d.saved_dxf_version or "R2018")
        files.append({**ctx.file_info(p), "format": "dxf", "source": "model+layouts"})
    for fmt in ("pdf", "png", "svg"):
        if fmt in formats:
            p = out_dir / f"{stem}_设备图.{fmt}"
            render_layout(d, SH.LAYOUT, p)
            files.append({**ctx.file_info(p), "format": fmt, "source": f"layout:{SH.LAYOUT}"})
    return files


class VesselQueryArgs(Args):
    what: Literal["summary", "nozzles", "supports", "pending", "sources", "parts", "dims"] = "summary"
    filter: Optional[str] = Field(None, description="管口选择器，如 purpose=进料口")


@tool("vessel_query", CAT, VesselQueryArgs, readonly=True)
def vessel_query(ctx: Ctx, a: VesselQueryArgs) -> dict:
    """查设备参数：summary 概要 / nozzles 管口列表（符号、DN、用途、位置、方位、来源）/ supports / pending 缺项 / sources 各字段来源 / parts 明细 / dims 尺寸链与总高。"""
    d = ctx.d
    spec = d.vessel
    if spec is None:
        raise ToolError("NO_VESSEL", "当前图纸没有设备参数", "先用 vessel_define(op=create) 新建")
    rec = spec.drawing.get("record") or {}
    if a.what == "nozzles":
        ns = spec.nozzles
        if a.filter:
            try:
                ns = S.resolve_selector(spec, a.filter, allow_many=True)
            except GeomError as ex:
                raise _err(ex)
        rows = [{"mark": n.mark, "dn": n.dn, "purpose": n.purpose, "location": n.location, "axial_mm": n.axial_mm, "radial_mm": n.radial_mm, "orientation_deg": n.orientation_deg,
                 "projection_mm": n.projection_mm, "pn": n.pn, "source": {f: ("user" if spec.sources.get(S.nkey(n, f), "").startswith("user:") else "rule" if spec.sources.get(S.nkey(n, f), "").startswith("rule:") else "table" if spec.sources.get(S.nkey(n, f), "").startswith("table:") else "")
                                                                        for f in ("dn", "location", "axial_mm", "orientation_deg", "projection_mm", "pn")}} for n in ns]
        return ctx.result(f"{len(rows)} 个管口：" + "；".join(S.describe_nozzle(n) for n in ns), nozzles=rows)
    if a.what == "supports":
        return ctx.result(f"{len(spec.supports)} 组支座", supports=[s.model_dump(exclude={"uid"}) for s in spec.supports])
    if a.what == "pending":
        p = S.missing(spec)
        return ctx.result("没有缺项" if not p else S.question_text(p), pending=p)
    if a.what == "sources":
        return ctx.result(f"{len(spec.sources)} 个字段有来源记录", sources={S.display_path(spec, k): v for k, v in spec.sources.items()})
    if a.what == "parts":
        return ctx.result(f"明细 {len(rec.get('parts', []))} 项", parts=rec.get("parts", []))
    if a.what == "dims":
        dm = rec.get("dim", {})
        return ctx.result(f"总{'高' if spec.vertical else '长'} {S.fmt_value(dm.get('overall'))} mm（按参数）", chain=dm.get("chain"), overall=dm.get("overall"))
    return ctx.result(S.summary(spec), kind=spec.kind, shell=spec.shell.model_dump(), heads=[h.model_dump() for h in spec.heads], design=spec.design.model_dump(exclude_none=True),
                      marks=[n.mark for n in spec.nozzles], warnings=spec.warnings)


class LookupArgs(Args):
    table_id: str
    keys: dict[str, Any] = Field(default_factory=dict)


@tool("vessel_lookup_standard", CAT, LookupArgs, readonly=True, needs_drawing=False)
def vessel_lookup_standard(svc, a: LookupArgs) -> dict:
    """查标准数据表（封头、钢管、管法兰、压力-温度额定值、人孔、耳座、鞍座）：返回这一行和 source / verified；查不到报 STD_SIZE_NOT_FOUND（不插值、不外推）。"""
    try:
        r = T.lookup(a.table_id, **a.keys)
    except GeomError as ex:
        raise _err(ex)
    return {"summary": f"{r['standard']} {a.table_id}：" + ("已核对" if r["verified"] else "未核对（verified=false）"), "data": r}


class CheckArgs(Args):
    pass


@tool("vessel_check", CAT, CheckArgs, readonly=True)
def vessel_check(ctx: Ctx, a: CheckArgs) -> dict:
    """设备图自检：必问项、管口重叠/越界/离焊缝太近/与支座冲突、符号重复、管口表与视图一致、件号与明细栏一致、尺寸链闭合、布局重叠、比例、字高。"""
    d = ctx.d
    if d.vessel is None:
        raise ToolError("NO_VESSEL", "当前图纸没有设备参数", "")
    r = CHK.run(d.vessel, d)
    return ctx.result(f"设备图自检：{len(r['errors'])} 个错误，{len(r['warnings'])} 个警告", **r)
