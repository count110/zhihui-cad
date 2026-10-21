"""通用 CAD 的接口：场景、ops（按钮/夹点/面板直接调用函数，不经过大模型）、命令行、对话、渲染、语音识别。

和流程图的任务接口共存：对话走 /api/tasks（任务里带 drawing_id），事件流、取消、反问都复用现有机制。
所有写操作都在 API 进程里经同一个 ToolService 执行（同一把文件锁），所以按钮、夹点和智能体不会互相踩。
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from zhihui.tools.core import ToolService
from zhihui.tools.store import data_dir

router = APIRouter()
_svc: ToolService | None = None


def general_svc() -> ToolService:
    global _svc
    if _svc is None or _svc.store.root != data_dir():
        _svc = ToolService()
    return _svc


def _app():
    from zhihui.api import app as app_mod
    return app_mod


def _load(drawing_id: str):
    try:
        return general_svc().store.load(drawing_id)
    except (KeyError, ValueError):
        raise HTTPException(404, "图纸不存在")


class NewDrawing(BaseModel):
    template_name: str = "gb_a3_mech"
    title: str = ""


@router.post("/api/drawings")
async def new_drawing(body: NewDrawing) -> dict:
    from zhihui.tools.general.registry import run_tool
    r = await asyncio.to_thread(run_tool, general_svc(), None, "manage_document_file", {"action": "new", "template_name": body.template_name, "title": body.title})
    if not r.get("ok"):
        raise HTTPException(400, r.get("message", "新建失败"))
    return {"drawing_id": r["drawing_id"], "revision": r["revision"]}


@router.get("/api/drawings")
def list_drawings(limit: int = 30) -> dict:
    root = data_dir() / "drawings"
    rows = []
    if root.is_dir():
        for p in sorted(root.glob("dwg_*/current.json"), key=lambda x: x.stat().st_mtime, reverse=True)[:limit * 3]:
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
            except ValueError:
                continue
            if raw.get("mode", "pfd") != "general":
                continue
            rows.append({"drawing_id": raw["id"], "title": raw.get("title", ""), "revision": raw.get("revision", 0), "entities": len(raw.get("entities", [])), "updated": p.stat().st_mtime})
            if len(rows) >= limit:
                break
    return {"items": rows}


@router.get("/api/drawings/{drawing_id}/scene")
async def scene(drawing_id: str, since: int | None = None, space: str | None = None) -> dict:
    d = _load(drawing_id)
    if since is not None and since == d.revision and space is None:
        return {"unchanged": True, "revision": d.revision}
    from zhihui.cad.scene import build_scene
    return await asyncio.to_thread(build_scene, d, space)


class Op(BaseModel):
    tool: str
    args: dict = Field(default_factory=dict)
    utterance: str = ""


@router.post("/api/drawings/{drawing_id}/ops")
async def ops(drawing_id: str, op: Op) -> dict:
    """按钮、夹点、特性面板、状态栏开关直接调用函数：同样校验、落版本、可撤销，不经过大模型。"""
    from zhihui.tools.general.registry import REGISTRY, run_tool
    if op.tool not in REGISTRY:
        raise HTTPException(400, f"没有函数 {op.tool}")
    return await asyncio.to_thread(run_tool, general_svc(), drawing_id, op.tool, op.args, op.utterance)


class Command(BaseModel):
    text: str


@router.post("/api/drawings/{drawing_id}/command")
async def command(drawing_id: str, body: Command) -> dict:
    """命令行：先走离线快速通道（AutoCAD 命令、中文模板），匹配不上返回 handled=false，前端再提交给智能体。"""
    from zhihui.agent import command_line as CL
    from zhihui.agent.speech_normalize import normalize
    from zhihui.tools.general.registry import run_tool
    norm = normalize(body.text)
    try:
        cmd = CL.parse(norm.text)
    except CL.CmdError as ex:
        return {"ok": False, "handled": True, "message": ex.message, "hint": ex.hint, "normalized": norm.text}
    if cmd is None:
        return {"ok": True, "handled": False, "normalized": norm.text, "changes": norm.changes[:8]}
    if cmd.help:
        return {"ok": True, "handled": True, "help": cmd.help, "normalized": norm.text}
    call = cmd.as_call()
    res = await asyncio.to_thread(run_tool, general_svc(), drawing_id, call["tool"], call["args"], body.text)
    return {"ok": bool(res.get("ok")), "handled": True, "summary": cmd.summary, "result": res, "normalized": norm.text, "call": call}


class RouteBody(BaseModel):
    prompt: str
    drawing_id: str | None = None
    context: str | None = None               # "pfd"：前端正在看流程图（这句话按“当前是流程图”路由）


def _peek(drawing_id: str | None):
    """按 id 读图纸；没有 id、图纸不存在都返回 None（路由时不报错，按话术判断）。"""
    if not drawing_id:
        return None
    try:
        return general_svc().store.load(drawing_id)
    except (KeyError, ValueError, OSError):
        return None


def task_route(prompt: str, drawing_id: str | None):
    """任务绑定的图纸 + 这句话 → (入口路由结果, 任务实际使用的 drawing_id)。
    路由是设备图而这张图不是设备图（没有 vessel、工作空间也不是化工设备图）：不把设备画进用户的草图，drawing_id 置空，由引擎新建 hg_equipment_a1。"""
    from zhihui.agent.general.router import EQUIP_CAT, entry_decision
    d = _peek(drawing_id)
    r = entry_decision(prompt, d)
    if r.mode == "equipment" and d is not None and d.vessel is None and d.workspace != EQUIP_CAT:
        return r, None
    return r, drawing_id


@router.post("/api/route")
def route_prompt(body: RouteBody) -> dict:
    """这句话该走流程图、化工设备图还是通用绘图（两个页面共用；通用绘图页发送前先问一下，要流程图就转到流程图页）。
    当前图纸有设备参数（或是设备图样板）时按设备图判断：只有明确要流程图才建议转走。"""
    from zhihui.agent.general.router import entry_decision, route
    d = _peek(body.drawing_id)
    r = route(body.prompt, "pfd") if (body.context == "pfd" and d is None) else entry_decision(body.prompt, d)      # 正在看流程图：按“当前是流程图”判断
    return {"mode": r.mode, "confidence": r.confidence, "reason": r.reason, "categories": r.categories, "ask_kind": r.ask_kind, "has_vessel": d is not None and d.vessel is not None}


class Chat(BaseModel):
    prompt: str


@router.post("/api/drawings/{drawing_id}/chat")
async def chat(drawing_id: str, body: Chat) -> dict:
    """自然语言口令：创建一个绑定到这张图的任务，由通用智能体（Planner → Recognizer → Executor）处理；事件流和反问沿用 /api/tasks。"""
    _load(drawing_id)
    app = _app()
    prompt = body.prompt.strip()
    if not prompt or len(prompt) > 2000:
        raise HTTPException(400, "请输入 1–2000 字的描述")
    task_id = "t_" + uuid.uuid4().hex[:12]
    r, did = task_route(prompt, drawing_id)
    app.db.create_task(task_id, prompt, None, None)
    app.db.set_state(task_id, {"general": True, "drawing_id": did, "route": r.mode, "categories": r.categories})
    await app.queue.put((task_id, None))
    return {"id": task_id, "status": "QUEUED"}


# ------------------------------------------------------------------ 化工设备图：参数面板用的接口
_DESIGN_LABELS = {
    "design_standard": "设计规范", "vessel_category": "容器类别", "working_pressure_mpa": "工作压力（MPa）", "design_pressure_mpa": "设计压力（MPa）", "working_temp_c": "工作温度（℃）",
    "design_temp_c": "设计温度（℃）", "medium": "介质", "medium_property": "介质特性", "main_material": "主要受压元件材料", "corrosion_allowance_mm": "腐蚀裕量（mm）",
    "weld_joint_coeff": "焊接接头系数", "heat_treatment": "热处理", "ndt": "无损检测", "test_type": "试验类型", "test_pressure_mpa": "试验压力（MPa）", "leak_test": "泄漏试验",
    "insulation_thickness_mm": "保温层厚度（mm）", "fill_coeff": "充装系数", "design_life_years": "设计寿命（年）", "wind_pressure_kpa": "基本风压（kPa）", "seismic": "抗震设防",
    "safety_valve_set_mpa": "安全阀整定压力（MPa）"}
_DESIGN_SHOWN = ("design_pressure_mpa", "design_temp_c", "working_pressure_mpa", "working_temp_c", "medium", "main_material", "corrosion_allowance_mm")     # 空着也要显示“待填”
_POS_CN = {"top": "上", "bottom": "下", "left": "左", "right": "右"}
_NOZZLE_SRC_FIELDS = ("mark", "dn", "purpose", "kind", "location", "axial_mm", "radial_mm", "orientation_deg", "projection_mm", "pn", "flange_type", "facing", "std_part")
_SUPPORT_SRC_FIELDS = ("kind", "count", "model", "height_mm", "material", "positions")


def _src_kind(src: str | None) -> str:
    k = (src or "").split(":", 1)[0]
    return k if k in ("user", "rule", "table", "calc") else ""


def _positions_text(positions) -> str | None:
    """支座位置：耳座 [{orientation_deg, elevation_mm}] → “0°、90°…”；鞍座 [{axial_mm, fixed}] → “距环焊缝 … 固定”。"""
    from zhihui.cad.equipment.spec_ops import fmt_value
    if not positions:
        return None
    out = []
    for p in positions:
        if "orientation_deg" in p:
            out.append(f"{fmt_value(p['orientation_deg'])}°")
        elif "axial_mm" in p:
            out.append(f"{fmt_value(p['axial_mm'])}" + ("（固定）" if p.get("fixed") else ""))
        else:
            out.append(str(p))
    return "、".join(out)


def vessel_payload(d) -> dict:
    """设备参数面板的数据：每个字段带中文名、格式化后的值、来源（user/rule/table/calc/pending）和是否未核对；另有管口、支座、待问项、警告、自检结果。"""
    from zhihui.cad.equipment import check as eq_check
    from zhihui.cad.equipment import spec_ops as SO
    v = d.vessel
    unverified_tables = set(v.drawing.get("unverified_tables") or [])

    def src_of(key: str) -> str | None:
        if key in v.sources:
            return v.sources[key]
        return next((val for k, val in v.sources.items() if k.startswith(key + ".") or k.startswith(key + "[")), None)

    def field(key: str, shown: str, label: str, value, text: str | None = None) -> dict:
        src = src_of(key)
        unverified = bool(src) and (src.startswith("rule:") or (src.startswith("table:") and src.split(":", 1)[1] in unverified_tables))
        return {"path": shown, "label": label, "value": text if text is not None else SO.fmt_value(value), "source": "pending" if value is None else _src_kind(src), "unverified": unverified}

    fields = [field("kind", "kind", "设备类型", v.kind)]
    sh = v.shell
    fields += [field("shell.inner_diameter_mm", "shell.inner_diameter_mm", "筒体内径 Di（mm）", sh.inner_diameter_mm),
               field("shell.length_mm", "shell.length_mm", "筒体长度（mm）", sh.length_mm)]
    if sh.tangent_length_mm is not None:
        fields.append(field("shell.tangent_length_mm", "shell.tangent_length_mm", "切线长（mm）", sh.tangent_length_mm))
    fields += [field("shell.thickness_mm", "shell.thickness_mm", "筒体名义厚度 δ（mm）", sh.thickness_mm), field("shell.material", "shell.material", "筒体材料", sh.material)]
    for h in v.heads:
        cn = _POS_CN.get(h.position, h.position)
        for f, lab in (("kind", "型式"), ("thickness_mm", "名义厚度（mm）"), ("straight_flange_mm", "直边高度（mm）"), ("material", "材料")):
            fields.append(field(SO.hkey(h, f), SO.hkey(h, f), f"{cn}封头{lab}", getattr(h, f)))
    for f, lab in _DESIGN_LABELS.items():
        val = getattr(v.design, f, None)
        if val is not None or f in _DESIGN_SHOWN:
            fields.append(field(f"design.{f}", f"design.{f}", lab, val))
    for i, s in enumerate(v.supports):
        for f, lab in (("kind", "型式"), ("count", "数量"), ("model", "型号")):
            fields.append(field(SO.skey(s, f), SO.display_path(v, SO.skey(s, f)), f"支座{i + 1}{lab}", getattr(s, f)))
        fields.append(field(SO.skey(s, "positions"), SO.display_path(v, SO.skey(s, "positions")), f"支座{i + 1}位置", s.positions or None, _positions_text(s.positions)))
    fields.append(field("drawing.sheet", "drawing.sheet", "图幅", v.drawing.get("sheet")))
    nozzles = []
    for n in v.nozzles:
        nozzles.append({"mark": n.mark, "dn": n.dn, "purpose": n.purpose or n.purpose_text, "kind": n.kind, "location": n.location, "axial_mm": n.axial_mm, "radial_mm": n.radial_mm,
                        "orientation_deg": n.orientation_deg, "projection_mm": n.projection_mm, "pn": n.pn, "flange_type": n.flange_type, "facing": n.facing, "std_part": n.std_part,
                        "sources": {f: _src_kind(v.sources.get(SO.nkey(n, f))) for f in _NOZZLE_SRC_FIELDS}})
    supports = []
    for i, s in enumerate(v.supports):
        supports.append({"index": i, "kind": s.kind, "count": s.count, "model": s.model, "height_mm": s.height_mm, "material": s.material, "positions": s.positions,
                         "sources": {f: _src_kind(src_of(SO.skey(s, f))) for f in _SUPPORT_SRC_FIELDS}})
    try:
        check = eq_check.run(v, d)
    except Exception as ex:                                        # 自检本身出错也不能让参数面板打不开：当作一条错误给出来
        check = {"errors": [{"code": "E_CHECK_CRASH", "message": f"自检出错：{type(ex).__name__}: {ex}"}], "warnings": []}
    rec = v.drawing.get("record") or {}
    return {"summary": SO.summary(v), "kind": v.kind, "tag": v.tag, "name": v.name, "fields": fields, "nozzles": nozzles, "supports": supports, "pending": SO.missing(v), "warnings": list(v.warnings),
            "check": check, "record": {"den_main": rec.get("den_main"), "den_end": rec.get("den_end"), "viewports": rec.get("viewports")}, "layouts": [l["name"] for l in d.layouts if l.get("equip")]}


@router.get("/api/drawings/{drawing_id}/vessel")
async def vessel(drawing_id: str) -> dict:
    """化工设备图的参数、来源、管口、支座、待问项和自检结果（前端参数面板用）；不是设备图时 vessel 为 null。"""
    d = _load(drawing_id)
    if d.vessel is None:
        return {"drawing_id": drawing_id, "revision": d.revision, "vessel": None}
    return {"drawing_id": drawing_id, "revision": d.revision, "vessel": await asyncio.to_thread(vessel_payload, d)}


@router.get("/api/drawings/{drawing_id}/render")
async def render(drawing_id: str, fmt: str = "png", space: str | None = None, window: str | None = None):
    """服务器渲染的预览（三维视图、布局出图、打印预览）；二维视图前端自己画。"""
    d = _load(drawing_id)
    if fmt not in ("png", "svg"):
        raise HTTPException(400, "fmt 只能是 png 或 svg")
    import hashlib
    key = hashlib.sha1(f"{d.view.view3d}|{d.view.visual_style}|{d.view.projection}|{d.view.orbit}|{d.background}|{window}".encode()).hexdigest()[:8]      # 视图方向、视觉样式变了不产生新版本，缓存键要带上
    out = data_dir() / "drawings" / drawing_id / "preview" / f"r{d.revision}_{(space or d.active_space).replace('/', '_')}_{key}.{fmt}"
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)

        def work():
            from zhihui.cad.render_general import render_general, render_layout
            if (space or d.active_space) != "Model" and any(l["name"] == (space or d.active_space) for l in d.layouts):
                render_layout(d, space or d.active_space, out, None, show_viewports=False)
            else:
                render_general(d, out, [float(x) for x in window.split(",")] if window else None)
        await asyncio.to_thread(work)
    return FileResponse(out, media_type="image/png" if fmt == "png" else "image/svg+xml", headers={"Cache-Control": "no-store"})


@router.get("/api/drawings/{drawing_id}/revisions")
def revisions(drawing_id: str) -> dict:
    d = _load(drawing_id)
    return {"revision": d.revision, "history": [{"rev": h["rev"], "tool": h["tool"], "utterance": h.get("utterance", ""), "args": h.get("args")} for h in reversed(d.session.history[-50:])], "redo": len(d.session.redo)}


class ExportBody(BaseModel):
    formats: list[str] = Field(default_factory=lambda: ["dxf", "png", "pdf"])


@router.post("/api/drawings/{drawing_id}/export")
async def export(drawing_id: str, body: ExportBody) -> dict:
    app = _app()
    r = await asyncio.to_thread(general_svc().export_artifacts, drawing_id, body.formats, True)
    if not r.get("ok"):
        raise HTTPException(400, r.get("message", "导出失败"))
    return {"files": [{**f, "url": app.signed_url(f["file_ref"])} for f in r["files"]], "validation": r.get("validation"), "warnings": r.get("warnings", [])}


@router.get("/api/tools")
def tools(categories: str | None = None) -> dict:
    """工具目录（前端的特性面板、帮助用）。"""
    from zhihui.tools.general.registry import catalog
    cats = [c for c in (categories or "").split(",") if c] or None
    return {"tools": catalog(cats)}


# ------------------------------------------------------------------ 语音识别（可选）
def asr_engine() -> str | None:
    for name in ("faster_whisper", "vosk"):
        try:
            __import__(name)
            return name.replace("_", "-")
        except ImportError:
            continue
    return None


@router.get("/api/asr/status")
def asr_status() -> dict:
    eng = asr_engine()
    return {"available": eng is not None, "engine": eng, "note": "浏览器自带的语音识别（Web Speech API）不依赖这里；本地识别只在装了 faster-whisper 或 vosk 时启用。"}


@router.post("/api/asr")
async def asr(file: UploadFile = File(...)) -> dict:
    eng = asr_engine()
    if eng is None:
        raise HTTPException(501, "本机没有安装 faster-whisper 或 vosk，本地语音识别不可用；请用浏览器的语音输入，或直接打字")
    data = await file.read(25 * 1024 * 1024 + 1)
    if len(data) > 25 * 1024 * 1024:
        raise HTTPException(400, "音频超过 25 MB")
    tmp = data_dir() / "uploads" / f"asr_{uuid.uuid4().hex[:8]}"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(data)
    try:
        def work() -> str:
            if eng == "faster-whisper":
                from faster_whisper import WhisperModel
                model = WhisperModel(os.environ.get("ZHIHUI_ASR_MODEL", "small"), device="cpu", compute_type="int8")
                segs, _ = model.transcribe(str(tmp), language="zh")
                return "".join(s.text for s in segs).strip()
            raise RuntimeError("vosk 需要 16kHz 单声道 WAV 和本地模型目录，请改用 faster-whisper 或浏览器语音输入")
        text = await asyncio.to_thread(work)
    except Exception as ex:
        raise HTTPException(500, f"识别失败：{ex}")
    finally:
        tmp.unlink(missing_ok=True)
    return {"text": text, "engine": eng}
