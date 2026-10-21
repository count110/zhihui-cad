"""Web API + 任务队列 + Worker（比赛期单进程部署：API 和 Worker 在同一进程，用 asyncio 队列）。

状态机：QUEUED → PLANNING → RUNNING → VALIDATING (→ REPAIRING → RUNNING …) → READY
        分支：WAITING_CONFIRMATION / FAILED / CANCELLED

运行：uvicorn zhihui.api.app:app --port 8000
环境变量：ZHIHUI_SECRET（下载签名密钥）、ZHIHUI_WORKERS（并发数，默认 2）、ZHIHUI_LLM=mock（离线演示）
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import re
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError

from zhihui.agent.llm_required import LLMUnavailable
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from zhihui.agent.llm import make_llm
from zhihui.agent.orchestrator import MAX_SECONDS, Orchestrator
from zhihui.tools.core import MAX_UPLOAD_BYTES
from zhihui.tools.store import data_dir

from .db import DB, PAUSED_STATUSES, TERMINAL_STATUSES

ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = ROOT / "05_前端" / "web"
SECRET = os.environ.get("ZHIHUI_SECRET") or secrets.token_hex(16)
LINK_TTL = 3600
FINAL = TERMINAL_STATUSES | PAUSED_STATUSES

db: DB
queue: asyncio.Queue


# ------------------------------------------------------------------ 签名下载
def sign(ref: str, exp: int) -> str:
    return hmac.new(SECRET.encode(), f"{ref}|{exp}".encode(), hashlib.sha256).hexdigest()[:32]


def signed_url(ref: str) -> str:
    exp = int(time.time()) + LINK_TTL
    return f"/api/files?ref={ref}&exp={exp}&sig={sign(ref, exp)}"


def present(task: dict) -> dict:
    """返回给前端的任务视图：产物换成带签名的临时链接，内部消息历史不外露。"""
    res = task.get("result") or {}
    arts = [{**a, "url": signed_url(a["file_ref"])} for a in res.get("artifacts", [])]
    gens = [{**g, "preview_url": signed_url(g["preview_ref"])} for g in res.get("generated_symbols", [])]
    bundle = {"bundle_url": signed_url("bundle:" + task["id"]).replace("/api/files?ref=", "/api/bundle?ref=")} if arts and task["status"] in ("READY", "READY_WITH_WARNINGS") else {}
    return {
        "id": task["id"],
        "status": task["status"],
        "prompt": task["prompt"],
        "file_ref": task["file_ref"],
        "parent_id": task["parent_id"],
        "created_at": task["created_at"],
        "updated_at": task["updated_at"],
        "result": {**{k: v for k, v in res.items() if k != "artifacts"}, "artifacts": arts, "generated_symbols": gens, **bundle} if res else None,
    }


# ------------------------------------------------------------------ Worker
class Cancelled(Exception):
    pass


async def run_task(task_id: str, answer: str | None = None) -> None:
    task = db.get_task(task_id)
    if task is None or task["status"] == "CANCELLED":
        return

    def on_event(kind: str, payload: dict) -> None:
        if db.get_task(task_id)["status"] == "CANCELLED":
            raise Cancelled()  # 用户取消：在下一步之前停下
        db.add_event(task_id, kind, payload)
        # 终态由 set_result 和结果一起写入，否则前端可能先看到 READY、却还拿不到产物
        if kind == "status" and payload["status"] not in FINAL:
            db.set_status(task_id, payload["status"])

    prompt, file_ref, resume = task["prompt"], task["file_ref"], task["messages"]
    mock = os.environ.get("ZHIHUI_LLM", "real") == "mock"
    agent_mode=os.getenv('ZHIHUI_AGENT_MODE','multi')
    if answer is not None and mock and agent_mode=='single':
        prompt, resume = answer, None  # 脚本模型不支持续接，直接按回答重跑
    gstate = task.get("state") or {}
    try:
        if gstate.get("general"):
            from zhihui.agent.general.backend import LocalBackend
            from zhihui.agent.general.engine import GeneralAgent
            backend = LocalBackend(general_api.general_svc()) if os.getenv("ZHIHUI_GENERAL_BACKEND", "local") == "local" else None     # local：和按钮、夹点同一个进程、同一把锁；mcp：走 MCP 协议
            orch = GeneralAgent(on_event=on_event, task_id=task_id, db=db, mock=mock, backend=backend, drawing_id=gstate.get("drawing_id"), route_hint=gstate.get("route"))     # route_hint=equipment：没有图纸就新建化工设备图样板
        elif agent_mode=='multi':
            from zhihui.agent.coord import Coordinator
            orch=Coordinator(on_event=on_event,task_id=task_id,db=db,mock=mock)
        elif agent_mode=='single':orch=Orchestrator(make_llm(prompt,file_ref),on_event)
        else:raise ValueError('ZHIHUI_AGENT_MODE must be single or multi')
        out = await asyncio.wait_for(orch.run(prompt, file_ref, resume=resume if answer is not None else None, answer=answer), MAX_SECONDS + 20)
        result = {
            "message": out.message,
            "drawing_id": out.drawing_id,
            "question": out.question,
            "artifacts": out.artifacts,
            "validation": out.validation,
            "diff": out.diff,
            "usage": out.usage,
            "spec":out.spec,
            "report":out.report,
            "requirements":out.requirements,
            "metrics":out.metrics,
            "generated_symbols":out.generated_symbols,
        }
        if gstate.get("general") and out.drawing_id and out.drawing_id != gstate.get("drawing_id"):
            db.set_state(task_id, {**gstate, "drawing_id": out.drawing_id})          # 智能体新建/打开了图：下一次反问回答接着用这张
        db.set_result(task_id, out.status, result, out.messages if out.status == "WAITING_CONFIRMATION" else None)
        if out.status == "FAILED":
            db.add_event(task_id, "status", {"status": "FAILED", "reason": out.message})
    except Cancelled:
        pass
    except asyncio.TimeoutError:
        db.set_result(task_id, "FAILED", {"message": "任务超时"})
        db.add_event(task_id, "status", {"status": "FAILED", "reason": "任务超时"})
    except LLMUnavailable as ex:  # 没接入大模型 / 接口失败：直接告诉用户怎么办，不当成内部错误
        db.set_result(task_id, "FAILED", {"message": str(ex)})
        db.add_event(task_id, "status", {"status": "FAILED", "reason": str(ex)})
    except Exception as ex:  # 兜底：任何异常都不能拖垮 Worker
        llm_err = _find_llm_error(ex)                    # 多智能体在 TaskGroup 里跑，LLMUnavailable 会被包进 ExceptionGroup：拆出来
        if llm_err is not None:
            db.set_result(task_id, "FAILED", {"message": str(llm_err)})
            db.add_event(task_id, "status", {"status": "FAILED", "reason": str(llm_err)})
            return
        db.set_result(task_id, "FAILED", {"message": f"内部错误：{type(ex).__name__}: {ex}"})
        db.add_event(task_id, "status", {"status": "FAILED", "reason": str(ex)})


async def worker() -> None:
    while True:
        task_id, answer = await queue.get()
        try:
            await run_task(task_id, answer)
        finally:
            queue.task_done()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global db, queue
    db = DB(data_dir() / "zhihui.db")
    db.recover_interrupted()
    queue = asyncio.Queue()
    workers = [asyncio.create_task(worker()) for _ in range(int(os.environ.get("ZHIHUI_WORKERS", "2")))]
    yield
    for w in workers:
        w.cancel()


app = FastAPI(title="智绘CAD API", version="0.1.0", lifespan=lifespan)

from zhihui.api import general as general_api  # noqa: E402  通用 CAD：场景 / ops / 命令行 / 对话 / 渲染 / 语音

app.include_router(general_api.router)


# ------------------------------------------------------------------ 接口
@app.get("/api/health")
def health() -> dict:
    mode = os.environ.get("ZHIHUI_LLM", "real")
    model = ""
    if mode != "mock":
        from zhihui.agent.llm import PROVIDERS

        prov = os.environ.get("ZHIHUI_LLM_PRIMARY", "deepseek")
        cfg = PROVIDERS.get(prov, {})
        model = f"{prov}:{os.environ.get(cfg.get('model_env', ''), '') or cfg.get('model', '')}" + ("" if os.environ.get(cfg.get("key_env", "")) else "（未配置密钥）")
    key_missing = mode != "mock" and "未配置密钥" in model
    return {"ok": True, "llm": mode, "model": model, "key_missing": key_missing, "agent_mode":os.getenv('ZHIHUI_AGENT_MODE','multi'), "mcp": os.environ.get("ZHIHUI_MCP", "stdio"), "queue": queue.qsize()}


@app.post("/api/tasks")
async def create_task(prompt: str = Form(...), file: UploadFile | None = File(None), parent_id: str | None = Form(None), drawing_id: str | None = Form(None)) -> dict:
    prompt = prompt.strip()
    if not prompt or len(prompt) > 2000:
        raise HTTPException(400, "请输入 1–2000 字的描述")
    task_id = "t_" + uuid.uuid4().hex[:12]
    file_ref = None
    if file is not None and file.filename:
        if not file.filename.lower().endswith(".dxf"):
            raise HTTPException(400, "只支持 .dxf 文件")
        data = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(400, "文件超过 10 MB")
        safe = re.sub(r"[^\w.\-]", "_", Path(file.filename).name)[-80:]
        dest = data_dir() / "uploads" / task_id / safe
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        file_ref = f"uploads/{task_id}/{safe}"
    elif parent_id:
        # 在上一次结果上继续改：把上次导出的 DXF 当作上传件
        parent = db.get_task(parent_id)
        arts = ((parent or {}).get("result") or {}).get("artifacts") or []
        dxf = next((a["file_ref"] for a in arts if a["format"] == "dxf"), None)
        if dxf is None:
            raise HTTPException(400, "上一个任务没有可修改的 DXF")
        file_ref = dxf
    db.create_task(task_id, prompt, file_ref, parent_id)
    gen = _general_target(prompt, file_ref, parent_id, drawing_id)
    if gen is not None:
        db.set_state(task_id, {"general": True, "drawing_id": gen.get("drawing_id"), "route": gen.get("route"), "categories": gen.get("categories") or []})     # route：入口路由结果（equipment 时引擎新建设备图样板）
    await queue.put((task_id, None))
    # 设备图任务也由通用智能体处理，所以 mode 仍是 general（流程图页据此跳过去）；route 告诉前端它其实是设备图
    return {"id": task_id, "status": "QUEUED", **({"mode": "general", "drawing_id": gen.get("drawing_id"), "route": gen.get("route")} if gen is not None else {"mode": "pfd"})}


def _find_llm_error(ex: BaseException) -> "LLMUnavailable | None":
    if isinstance(ex, LLMUnavailable):
        return ex
    for sub in getattr(ex, "exceptions", ()) or ():
        found = _find_llm_error(sub)
        if found is not None:
            return found
    return None


def _general_target(prompt: str, file_ref: str | None, parent_id: str | None, drawing_id: str | None) -> dict | None:
    """这个任务走通用智能体还是流程图：显式给了 drawing_id → 通用；上传的 DXF 不是本系统的流程图 → 通用；接着上一个通用任务；否则按话术路由。
    走通用智能体时返回 {"drawing_id", "route": general|equipment|pfd, "categories"}：route 是入口路由结果（按图纸算：有设备参数或设备图样板按设备图），
    设备图口令不会画进用户的草图（drawing_id 置空，引擎新建 hg_equipment_a1）；走流程图返回 None。"""
    from zhihui.agent.general.router import categories_of, entry_decision
    if drawing_id:
        r, did = general_api.task_route(prompt, drawing_id)
        return {"drawing_id": did, "route": r.mode, "categories": r.categories}
    if file_ref and not parent_id:
        from zhihui.cad.general_dxf import dxf_kind
        kind = dxf_kind(data_dir() / file_ref)
        return {"drawing_id": None, "route": "general", "categories": categories_of(prompt)} if kind in ("general", "foreign") else None
    if parent_id:
        parent = db.get_task(parent_id) or {}
        st = parent.get("state") or {}
        if st.get("general"):
            r, did = general_api.task_route(prompt, st.get("drawing_id") or ((parent.get("result") or {}).get("drawing_id")))
            return {"drawing_id": did, "route": r.mode, "categories": r.categories}
        return None
    r = entry_decision(prompt, None)
    if r.mode == "equipment" or (r.mode == "general" and r.confidence >= 0.55):
        return {"drawing_id": None, "route": r.mode, "categories": r.categories}
    return None


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str, after: int = 0) -> dict:
    task = db.get_task(task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    return {**present(task), "events": db.events(task_id, after)}


class Reply(BaseModel):
    answer: str


from zhihui.agent.coord.spec import ProcessSpec,compile_spec,plan_issues
from pydantic import ValidationError

@app.post('/api/tasks/{task_id}/confirm')
async def confirm_plan(task_id:str,request:Request):
    task=db.get_task(task_id)
    if task is None:raise HTTPException(404,'任务不存在')
    if task['status']!='WAITING_CONFIRMATION' or not (task.get('state') or {}).get('spec'):
        raise HTTPException(409,'任务没有待确认的方案')
    try:raw=await request.json()
    except ValueError:return JSONResponse({'detail':'方案不是合法的 JSON，请检查括号和引号','issues':[]},status_code=400)
    issues=plan_issues(raw);body=None
    if not issues:
        try:
            body=ProcessSpec.model_validate(raw);compile_spec(body)
        except ValidationError as error:
            issues=[{'field':'.'.join(str(x) for x in e['loc']),'message':'字段 '+'.'.join(str(x) for x in e['loc'])+' 的内容不符合要求'} for e in error.errors()[:5]]
        except (ValueError,KeyError) as error:
            issues=[{'field':'','message':'方案中的图元或端口不合法，请核对设备图元和连接端口（'+type(error).__name__+'）'}]
    if issues:
        return JSONResponse({'detail':f'方案有 {len(issues)} 处需要修改：'+'；'.join(i['message'] for i in issues[:3])+('……' if len(issues)>3 else ''),'issues':issues},status_code=422)
    from zhihui.cad.symgen import confirm_generated
    confirm_generated([e.symbol_id for e in body.equipment])      # the person accepted the generated symbols by confirming the plan
    state=task['state'];state['spec']=body.model_dump(mode='json');state['confirmed']=True
    state['user_facts']['confirmed_spec']=state['spec'];db.set_state(task_id,state)
    db.add_event(task_id,'user',{'text':'已确认编辑后的方案'})
    db.set_status(task_id,'QUEUED');await queue.put((task_id,'确认方案'))
    return {'id':task_id,'status':'QUEUED'}


@app.exception_handler(RequestValidationError)
async def validation_error(_, ex: RequestValidationError):
    fields = '、'.join(sorted({str(e['loc'][-1]) for e in ex.errors() if e.get('loc')})) or '请求内容'
    return JSONResponse({'detail': f'提交的内容不完整或格式不对：{fields}', 'issues': []}, status_code=422)


@app.get('/api/tasks')
def list_tasks(limit: int = 30) -> dict:
    return {'items': [{**t, 'prompt': t['prompt'][:60]} for t in db.list_tasks(limit)]}


class Preflight(BaseModel):
    prompt: str


@app.post('/api/preflight')
def preflight(body: Preflight) -> dict:
    """What the engine can and cannot draw from this sentence, before any task is created. Deterministic: no model, no tokens."""
    from zhihui.agent.coord.requirements import extract_requirements
    from zhihui.agent.coord.review import describe
    every = extract_requirements(body.prompt[:2000])
    reqs = [r for r in every if r.kind != 'equipment']
    notes = []
    if re.search(r'P&ID|PID|管道及仪表|带控制点', body.prompt, re.I):
        notes.append('P&ID：可画仪表、阀门、止回阀、液位报警；仪表信号线当前不支持，会在交付说明里逐项列出。')
    return {'equipment': len(every) - len(reqs), 'notes': notes,
            'unsupported': [describe(r) + '：' + r.reason for r in reqs if r.support == 'unsupported'],
            'items': [{'kind': r.kind, 'text': describe(r), 'support': r.support, 'reason': r.reason} for r in reqs]}


@app.get('/api/symbols')
def symbols() -> dict:
    from zhihui.cad.symbols import load_symbols
    return {'items': [{'id': s.id, 'name': s.name_cn, 'category': s.category, 'family': s.family, 'aliases': list(s.aliases),
                       'ports': [{'name': p.name, 'kind': p.kind, 'semantic': p.semantics or p.name} for p in s.ports]}
                      for s in load_symbols().values() if s.id not in ('offsheet', 'offsheet_left', 'retaining_wall')]}


@app.get('/api/bundle')
def bundle(ref: str, exp: int, sig: str):
    import io
    import zipfile
    if exp < time.time() or not hmac.compare_digest(sig, sign(ref, exp)) or not ref.startswith('bundle:'):
        raise HTTPException(403, '链接无效或已过期')
    task = db.get_task(ref[len('bundle:'):])
    if task is None or not task.get('result'):
        raise HTTPException(404, '任务不存在')
    root = data_dir().resolve()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        for a in (task['result'].get('artifacts') or []):
            p = (root / a['file_ref']).resolve()
            if root in p.parents and p.is_file():
                z.write(p, p.name)
        if task['result'].get('report'):
            z.writestr('交付报告.md', task['result']['report'])
    return Response(buf.getvalue(), media_type='application/zip', headers={'Content-Disposition': f'attachment; filename="{task["id"]}_delivery.zip"'})


@app.get('/api/knowledge-gaps')
def knowledge_gaps():
    from zhihui.kb.gaps import GapStore
    return {'items':GapStore(db.path).list()}


@app.get('/api/tasks/{task_id}/report')
def task_report(task_id:str):
    task=db.get_task(task_id)
    if task is None:raise HTTPException(404,'任务不存在')
    return {'markdown':(task.get('result') or {}).get('report','')}


@app.post("/api/tasks/{task_id}/reply")
async def reply(task_id: str, body: Reply) -> dict:
    task = db.get_task(task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    if task["status"] != "WAITING_CONFIRMATION":
        raise HTTPException(409, "任务当前不在等待确认状态")
    db.add_event(task_id, "user", {"text": body.answer})
    db.set_status(task_id, "QUEUED")
    await queue.put((task_id, body.answer))
    return {"id": task_id, "status": "QUEUED"}


@app.post("/api/tasks/{task_id}/cancel")
def cancel(task_id: str) -> dict:
    task = db.get_task(task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    if task["status"] in FINAL - {"WAITING_CONFIRMATION"}:
        raise HTTPException(409, "任务已结束")
    db.set_result(task_id, "CANCELLED", task.get("result") or {"message": "用户取消"})
    db.add_event(task_id, "status", {"status": "CANCELLED"})
    return {"id": task_id, "status": "CANCELLED"}


@app.get("/api/files")
def download(ref: str, exp: int, sig: str):
    if exp < time.time() or not hmac.compare_digest(sig, sign(ref, exp)):
        raise HTTPException(403, "链接无效或已过期")
    root = data_dir().resolve()
    p = (root / ref).resolve()
    if root not in p.parents or not p.is_file():
        raise HTTPException(404, "文件不存在")
    media = {".dxf": "application/dxf", ".png": "image/png", ".svg": "image/svg+xml", ".pdf": "application/pdf", ".json": "application/json"}.get(p.suffix, "application/octet-stream")
    return FileResponse(p, media_type=media, filename=p.name)


# ------------------------------------------------------------------ 设置（API Key 等）
from zhihui.api import settings as settings_mod


class SettingsBody(BaseModel):
    values: dict = {}
    clear: list[str] = []


class ProviderBody(BaseModel):
    provider: str


def _guard_settings(request: Request) -> None:
    if not settings_mod.allowed_client(request.client.host if request.client else None, request.headers.get("X-Zhihui-Admin")):
        raise HTTPException(403, "只能在运行服务的这台电脑上修改设置（或配置 ZHIHUI_ADMIN_TOKEN）")


@app.get("/api/settings")
def get_settings(request: Request) -> dict:
    _guard_settings(request)
    return settings_mod.current()


@app.put("/api/settings")
def put_settings(body: SettingsBody, request: Request) -> dict:
    _guard_settings(request)
    try:
        changed = settings_mod.update(body.values, body.clear)
    except settings_mod.SettingsError as ex:
        raise HTTPException(400, str(ex))
    return {"changed": changed, "settings": settings_mod.current()}


@app.post("/api/settings/test")
async def test_settings(body: ProviderBody, request: Request) -> dict:
    _guard_settings(request)
    try:
        return await settings_mod.test_provider(body.provider)
    except settings_mod.SettingsError as ex:
        raise HTTPException(400, str(ex))


@app.middleware("http")
async def no_stale_frontend(request: Request, call_next):
    """页面和脚本每次都向服务器确认是否更新（有变化才重新下载），升级后用户不用手动强刷。"""
    resp = await call_next(request)
    if not request.url.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.exception_handler(HTTPException)
async def http_error(_, ex: HTTPException):
    return JSONResponse({"detail": ex.detail}, status_code=ex.status_code)


if WEB_DIR.exists():
    @app.get("/", include_in_schema=False)
    def home() -> FileResponse:
        """入口就是通用 CAD 页面；化工流程图是它里面的一个工作空间（index.html 只用来被嵌入）。"""
        return FileResponse(WEB_DIR / "cad.html", headers={"Cache-Control": "no-store"})

    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
