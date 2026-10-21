// 智绘CAD 前端：左边看图纸，右边像聊天一样提需求、回答、确认；细节收进“详情与报告”
const $ = (id) => document.getElementById(id);
const MAX_PROMPT = 2000;
const FINAL = ["READY", "READY_WITH_WARNINGS", "FAILED", "CANCELLED", "WAITING_CONFIRMATION"];
const STAGE_CN = {
  router: "正在理解你的需求", knowledge: "正在查找相似工艺", symbol: "图元库里没有，正在联网检索并生成示意图元", planner: "正在规划设备与连接",
  capability: "正在检查能画哪些内容", confirmation: "等你确认方案", builder: "正在绘制图纸", editor: "正在按你的要求修改", layout_fixer: "正在整理排版",
  reviewer: "正在逐项核对你的要求", repairer: "正在修复发现的问题", export: "正在导出 DXF / PNG / PDF", reporter: "正在生成交付报告",
};
const STAGE_STEP = { router: 0, knowledge: 0, symbol: 0, planner: 1, capability: 1, confirmation: 1, builder: 2, editor: 2, layout_fixer: 3, reviewer: 3, repairer: 3, export: 4, reporter: 4 };
const STATUS_CN = { QUEUED: "排队中", PLANNING: "处理中", RUNNING: "处理中", VALIDATING: "核对中", REPAIRING: "自动修复中", READY: "已完成", READY_WITH_WARNINGS: "已完成（有提示）", FAILED: "失败", CANCELLED: "已取消", WAITING_CONFIRMATION: "等你确认" };
const GAP_CN = { family_substitution: "用同类图元替代，待专业确认", ask_user: "需要你补充或确认", skip_hatch: "已跳过填充", unsupported: "当前不支持，已记录", symbol_generated: "已检索并自动生成示意图元，待专业确认" };
const TB_CN = { unit_name: "设计单位", project: "工程名称", drawing_name: "图名", drawing_no: "图号", title: "图名", date: "日期", stage: "设计阶段" };
const DL_NAMES = { dxf: "下载 DXF", pdf: "PDF", png: "PNG" };

let current = null, lastReady = null, lastEvent = 0, timer = null;
let view = "preview", artifacts = [], logs = [], lastTask = null, curPrompt = "";
let symbolCache = null, genSyms = [];
let progressEl = null, waiting = null;       // waiting: { spec, question }
let planDraft = null, undoStack = [], redoStack = [], planDismissed = false;

const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const store = (k, v) => { try { v === null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch { /* 隐私模式 */ } };
const load = (k) => { try { return localStorage.getItem(k); } catch { return null; } };

// ------------------------------------------------------------ 对话区
function addMsg(kind, html, extra = "") {
  const el = document.createElement("div");
  el.className = `msg ${kind} ${extra}`.trim();
  el.innerHTML = html;
  $("thread").appendChild(el);
  scrollThread();
  return el;
}
function scrollThread() { const t = $("thread"); requestAnimationFrame(() => { t.scrollTop = t.scrollHeight; }); }
function setProgress(text, step) {
  if (!progressEl) {
    progressEl = document.createElement("div");
    progressEl.className = "progress";
    progressEl.innerHTML = '<div class="spinner"></div><div><div class="ptext"></div><div class="steps">' + "<i></i>".repeat(5) + "</div></div>";
    $("thread").appendChild(progressEl);
  }
  progressEl.querySelector(".ptext").textContent = text;
  if (step !== undefined) progressEl.querySelectorAll(".steps i").forEach((b, i) => { b.className = i < step ? "done" : i === step ? "on" : ""; });
  scrollThread();
}
function clearProgress() { if (progressEl) { progressEl.remove(); progressEl = null; } }

// ------------------------------------------------------------ 图纸区：缩放、拖动
// 滚轮/触控板双指/捏合缩放（以鼠标所在点为中心），按住拖动平移，双击放大，键盘 + - 0 方向键，支持全屏。
const cv = { s: 1, x: 0, y: 0, fitS: 1 };
const ZOOM_MAX = 40;   // 相对“适应窗口”的倍数；SVG 矢量放大不糊
function applyCv() {
  $("drawing").style.transform = `translate(${cv.x}px, ${cv.y}px) scale(${cv.s})`;
  $("zoomLabel").textContent = Math.round((cv.s / cv.fitS) * 100) + "%";
}
function natural() {
  const img = $("drawing");
  return { w: img.naturalWidth || img.width || 1000, h: img.naturalHeight || img.height || 700 };
}
function fitCv() {
  const vp = $("viewport"), { w, h } = natural();
  cv.fitS = Math.min(vp.clientWidth / w, vp.clientHeight / h) * 0.97;
  cv.s = cv.fitS;
  cv.x = (vp.clientWidth - w * cv.s) / 2;
  cv.y = (vp.clientHeight - h * cv.s) / 2;
  applyCv();
}
function zoomAt(factor, cx, cy) {
  const r = $("viewport").getBoundingClientRect();
  const px = cx ?? r.width / 2, py = cy ?? r.height / 2;
  const ns = Math.max(cv.fitS * 0.25, Math.min(cv.fitS * ZOOM_MAX, cv.s * factor));
  cv.x = px - ((px - cv.x) * ns) / cv.s; cv.y = py - ((py - cv.y) * ns) / cv.s; cv.s = ns;
  applyCv();
}
function showDrawing(url, tag) {
  const img = $("drawing");
  img.hidden = false; $("welcome").hidden = true; $("zoomBar").hidden = false; $("viewTabs").hidden = false; $("viewHint").hidden = false;
  if (img.dataset.src !== url) {
    img.dataset.src = url;
    img.onload = () => { if (!img.naturalWidth) img.style.width = "3000px"; fitCv(); };   // 矢量图没有固有像素尺寸时给个大尺寸做缩放基准
    img.src = url;
  } else fitCv();
  $("previewTag").hidden = !tag; $("previewTag").textContent = tag || "";
}
function showWelcome() {
  const img = $("drawing");
  img.hidden = true; img.removeAttribute("src"); img.style.width = ""; delete img.dataset.src;
  $("welcome").hidden = false; $("zoomBar").hidden = true; $("viewTabs").hidden = true; $("previewTag").hidden = true; $("viewHint").hidden = true; $("downloads").innerHTML = "";
}
(() => {
  const vp = $("viewport"), pts = new Map(); let pinch = null, last = null;
  const center = () => { const r = vp.getBoundingClientRect(); const c = [...pts.values()].reduce((a, p) => [a[0] + p.x / pts.size, a[1] + p.y / pts.size], [0, 0]); return [c[0] - r.left, c[1] - r.top]; };
  vp.addEventListener("wheel", (e) => {
    if ($("drawing").hidden) return; e.preventDefault();
    const r = vp.getBoundingClientRect();
    const unit = e.deltaMode === 1 ? 33 : 1;                       // 行滚动换算成像素
    const k = e.ctrlKey ? 0.012 : 0.003;                           // 触控板捏合（ctrl+wheel）更灵敏
    zoomAt(Math.exp(-Math.max(-300, Math.min(300, e.deltaY * unit)) * k), e.clientX - r.left, e.clientY - r.top);
  }, { passive: false });
  vp.addEventListener("pointerdown", (e) => {
    if ($("drawing").hidden || e.target.closest("button, a")) return;
    vp.setPointerCapture(e.pointerId); pts.set(e.pointerId, { x: e.clientX, y: e.clientY }); vp.focus({ preventScroll: true });
    if (pts.size === 1) { last = { x: e.clientX, y: e.clientY }; vp.classList.add("panning"); }
    if (pts.size === 2) { const [a, b] = [...pts.values()]; pinch = { d: Math.hypot(a.x - b.x, a.y - b.y) }; }
  });
  vp.addEventListener("pointermove", (e) => {
    if (!pts.has(e.pointerId)) return;
    pts.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pts.size >= 2 && pinch) {                                   // 手机双指：缩放
      const [a, b] = [...pts.values()], d = Math.hypot(a.x - b.x, a.y - b.y), [cx, cy] = center();
      zoomAt(d / pinch.d, cx, cy); pinch.d = d;
    } else if (last) { cv.x += e.clientX - last.x; cv.y += e.clientY - last.y; last = { x: e.clientX, y: e.clientY }; applyCv(); }
  });
  const end = (e) => { pts.delete(e.pointerId); pinch = null; last = pts.size === 1 ? { ...[...pts.values()][0] } : null; if (!pts.size) vp.classList.remove("panning"); };
  vp.addEventListener("pointerup", end); vp.addEventListener("pointercancel", end);
  vp.addEventListener("dblclick", (e) => { if ($("drawing").hidden || e.target.closest("button, a")) return; const r = vp.getBoundingClientRect(); if (e.shiftKey) fitCv(); else zoomAt(2, e.clientX - r.left, e.clientY - r.top); });
  vp.addEventListener("keydown", (e) => {
    if ($("drawing").hidden) return;
    const step = 60;
    if (e.key === "+" || e.key === "=") zoomAt(1.25); else if (e.key === "-" || e.key === "_") zoomAt(0.8); else if (e.key === "0") fitCv();
    else if (e.key === "ArrowLeft") { cv.x += step; applyCv(); } else if (e.key === "ArrowRight") { cv.x -= step; applyCv(); }
    else if (e.key === "ArrowUp") { cv.y += step; applyCv(); } else if (e.key === "ArrowDown") { cv.y -= step; applyCv(); } else return;
    e.preventDefault();
  });
  $("zoomIn").onclick = () => zoomAt(1.4); $("zoomOut").onclick = () => zoomAt(1 / 1.4); $("zoomFit").onclick = fitCv;
  $("zoomActual").onclick = () => zoomAt(1 / cv.s);   // 图像按 1 个屏幕像素对 1 个图像像素显示
  $("zoomFull").onclick = () => { const st = document.querySelector(".stage"); st.classList.toggle("fullscreen"); $("zoomFull").textContent = st.classList.contains("fullscreen") ? "退出全屏" : "全屏"; setTimeout(fitCv, 50); };
  window.addEventListener("resize", () => { if (!$("drawing").hidden) fitCv(); });
})();
document.querySelectorAll(".tab").forEach((t) => (t.onclick = () => { view = t.dataset.view; renderPreview(); }));
const lastReadyTask = () => lastTask && ["READY", "READY_WITH_WARNINGS"].includes(lastTask.status);
function renderPreview() {
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.view === view));
  const a = view === "diff" ? artifacts.find((x) => x.format === "diff_png") : (artifacts.find((x) => x.format === "svg") || artifacts.find((x) => x.format === "png"));
  if (a) showDrawing(a.url, waiting && !lastReadyTask() ? "当前工作版本（未完成，仅供预览）" : "");
}

// ------------------------------------------------------------ 输入区
function updateCount() {
  const n = $("prompt").value.length;
  $("charCount").textContent = `${n} / ${MAX_PROMPT}`;
  $("charCount").classList.toggle("err", n > MAX_PROMPT);
  $("send").disabled = n > MAX_PROMPT || $("send").dataset.busy === "1";
  const ta = $("prompt"); ta.style.height = "auto"; ta.style.height = Math.min(200, ta.scrollHeight + 2) + "px";
}
function setBusy(b) { $("send").dataset.busy = b ? "1" : "0"; updateCount(); }
function setPlaceholder() {
  $("prompt").placeholder = waiting && !waiting.spec ? "输入你的回答…" : lastReady ? "接着说要改什么，例如：把 P-101 向右移动 800 mm" : "描述要画的流程…（Enter 发送，Shift+Enter 换行）";
  $("contWrap").hidden = !lastReady;
}
let preflightTimer = null;
async function preflight() {
  const text = $("prompt").value.trim(), box = $("preflight");
  if (text.length < 8 || (lastReady && $("continue").checked && !/[A-Z]{1,3}-\d{2,4}.*(→|进入|返回|连接)/.test(text))) { box.hidden = true; return; }
  try {
    const r = await fetch("/api/preflight", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ prompt: text.slice(0, MAX_PROMPT) }) }).then((x) => x.json());
    const bad = (r.items || []).filter((i) => i.support === "unsupported");
    const rows = [`识别到 ${r.equipment} 台设备、${(r.items || []).length} 项工艺要求`, ...(r.notes || []), ...bad.map((i) => `暂不能画：${i.text}（${i.reason}）`)];
    box.hidden = false; box.className = "preflight" + (bad.length ? " warn" : ""); box.innerHTML = rows.map((x) => `<div>${esc(x)}</div>`).join("");
  } catch { box.hidden = true; }
}
$("prompt").addEventListener("input", () => { updateCount(); clearTimeout(preflightTimer); preflightTimer = setTimeout(preflight, 700); });
$("prompt").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); } });
$("send").onclick = send;
$("file").onchange = () => { const f = $("file").files[0]; $("fileLabel").textContent = f ? `📎 ${f.name}` : "📎 上传 DXF 改图"; $("fileClear").hidden = !f; };
$("fileClear").onclick = () => { $("file").value = ""; $("file").onchange(); };
document.querySelectorAll(".example").forEach((b) => (b.onclick = () => { $("prompt").value = b.dataset.text; updateCount(); $("prompt").focus(); preflight(); }));

const looksLikeNew = (t) => /^\s*(请)?(帮我)?(画|绘制|生成|新建|做一张)/.test(t) || (t.match(/→/g) || []).length >= 2;
async function send() {
  const text = $("prompt").value.trim();
  if (!text || $("send").disabled) return;
  if (waiting && current) {            // 正在回答系统的问题
    $("prompt").value = ""; updateCount();
    return sendAnswer(text);
  }
  const fd = new FormData(); fd.append("prompt", text);
  const f = $("file").files[0];
  const cont = !f && lastReady && $("continue").checked && !looksLikeNew(text);   // “画一个…/A → B → C” 是新图，不是改旧图
  if (f) fd.append("file", f); else if (cont) fd.append("parent_id", lastReady);
  setBusy(true);
  try {
    const r = await fetch("/api/tasks", { method: "POST", body: fd });
    const j = await r.json();
    if (!r.ok) throw new Error(j.detail || r.status);
    $("prompt").value = ""; $("preflight").hidden = true; $("file").value = ""; $("file").onchange(); updateCount();
    if (j.mode === "general" || j.mode === "equipment") {                 // 这是一句通用绘图 / 化工设备图的口令（设备图任务 mode 仍是 general，route 才是 equipment）：交给外面的通用 CAD 页面接着显示进度
      const eq = j.mode === "equipment" || j.route === "equipment";
      addMsg("bot", eq ? "这句话是在要化工设备图，正在转到「化工设备图」工作空间继续…" : "这句话是通用绘图指令，正在回到通用绘图继续…"); setBusy(false);
      setTimeout(() => { if (window.parent !== window) parent.postMessage({ type: "zh:general-task", id: j.id, mode: eq ? "equipment" : "general" }, "*"); else location.href = "/#task=" + encodeURIComponent(j.id); }, 500); return;
    }
    start(j.id, { prompt: text, keep: cont || !!f });
  } catch (e) {
    addMsg("bot", `<h3 class="err">提交失败</h3>${esc(e.message)}`, "warnbox"); setBusy(false);
  }
}
async function sendAnswer(answer) {
  waiting = null; setPlaceholder();
  setProgress("已收到，继续处理…", 1);
  await fetch(`/api/tasks/${current}/reply`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ answer }) });
  poll();
}

// ------------------------------------------------------------ 任务生命周期
function start(id, { prompt, keep } = {}) {
  current = id; lastEvent = 0; logs = []; waiting = null; planDismissed = false; lastTask = null; if (prompt) curPrompt = prompt;
  store("zhihui.lastTask", id);
  if (location.hash !== "#/task/" + id) history.replaceState(null, "", "#/task/" + id);
  if (!keep) { $("thread").innerHTML = ""; artifacts = []; lastReady = null; showWelcome(); }
  clearProgress();
  if (prompt) addMsg("user", esc(prompt));
  $("btnDetails").disabled = true;
  setBusy(true); setPlaceholder();
  setProgress("排队中…", 0);
  poll();
}
function greet() {
  addMsg("bot", `<h3>你好，直接告诉我想画什么</h3><div class="sub">左边选一个例子，或在下面输入。写清设备（带位号）和连接关系就行；出图后继续说话就能改图。</div>`);
}
function newTask() {
  clearTimeout(timer); current = null; lastReady = null; waiting = null; lastTask = null; artifacts = [];
  store("zhihui.lastTask", null); history.replaceState(null, "", location.pathname);
  $("thread").innerHTML = ""; clearProgress(); showWelcome(); greet(); $("btnDetails").disabled = true; setBusy(false); setPlaceholder(); $("prompt").focus();
}
$("btnNew").onclick = newTask;

async function poll() {
  clearTimeout(timer);
  if (!current) return;
  let t;
  try {
    const r = await fetch(`/api/tasks/${current}?after=${lastEvent}`);
    if (!r.ok) { store("zhihui.lastTask", null); current = null; clearProgress(); addMsg("bot", "这个任务已不存在（可能被清理了），请新建。"); setBusy(false); return; }
    t = await r.json();
  } catch { timer = setTimeout(poll, 1500); return; }
  for (const ev of t.events) { lastEvent = ev.id; onEvent(ev); }
  if (FINAL.includes(t.status) && (t.result || t.status === "CANCELLED")) { clearProgress(); setBusy(false); finalize(t); loadHistory(); loadGaps(); return; }
  timer = setTimeout(poll, 700);
}
function onEvent(ev) {
  if (["tool_call", "stage", "execution_control", "status"].includes(ev.kind)) logs.push(ev);
  const stage = ev.role || ev.stage;
  if (ev.kind === "stage" && STAGE_CN[stage]) setProgress(STAGE_CN[stage], STAGE_STEP[stage]);
  else if (ev.kind === "execution_control" && /图元/.test(ev.action || "")) setProgress(ev.action, 0);
  else if (ev.kind === "status" && ev.status === "REPAIRING") setProgress("核对发现问题，正在自动修复…", 3);
  else if (ev.kind === "switch" && (ev.mode === "equipment" || ev.handoff === "equipment")) {      // 流程图链路里识别到设备图请求：原话交给外层页面走设备图入口
    addMsg("bot", "这句话是在要化工设备图，正在转到「化工设备图」工作空间…");
    const text = ev.prompt || curPrompt || "";
    if (text) setTimeout(() => { if (window.parent !== window) parent.postMessage({ type: "zh:equipment", prompt: text }, "*"); else location.href = "/"; }, 300);
  }
  else if (ev.kind === "user" && ev.text) addMsg("user", esc(ev.text));
  else if (ev.kind === "assistant" && ev.text) addMsg("bot", esc(ev.text));
}

// ------------------------------------------------------------ 结果
function attentionItems(r) {
  const out = [];
  (r.requirements || []).filter((x) => x.kind !== "equipment" && x.status !== "verified").forEach((x) => out.push(`${x.status === "unsupported" ? "暂不能画" : "图里没有做到"}：${x.text}`));
  const v = r.validation || {};
  (v.errors || []).slice(0, 3).forEach((e) => out.push("图面检查：" + e.message));
  if (v.standards && v.standards.errors) out.push(`规范自检有 ${v.standards.errors} 项错误（详见“详情与报告”）`);
  return out;
}
function finalize(t) {
  lastTask = t;
  const r = t.result || {};
  $("btnDetails").disabled = !t.result;
  genSyms = r.generated_symbols || [];
  artifacts = r.artifacts || [];
  waiting = null;
  if (t.status === "FAILED" || t.status === "CANCELLED") {
    const el = addMsg("bot", `<h3 class="err">${t.status === "FAILED" ? "没能完成" : "已取消"}</h3>${esc(r.message || "")}<div class="sub">原图没有被改动。</div><div class="actions"><button type="button">重试</button></div>`, "warnbox");
    el.querySelector("button").onclick = () => { $("prompt").value = t.prompt; updateCount(); send(); };
    setPlaceholder(); return;
  }
  if (t.status === "WAITING_CONFIRMATION") return finalizeWaiting(t, r);
  lastReady = t.id;
  renderPreview(); renderDownloads(r);
  const v = r.validation || {}, q = (r.metrics || {}).requirements || {}, att = attentionItems(r), facts = [];
  if (v.stats) facts.push(`<span class="fact">设备 ${v.stats.equipment} 台 · 连接 ${v.stats.connections} 条</span>`);
  if (q.total) facts.push(`<span class="fact ${q.verified === q.total ? "ok" : "bad"}">需求 ${q.verified}/${q.total} 项已在图中核实</span>`);
  if (v.passed !== undefined) facts.push(`<span class="fact ${v.passed ? "ok" : "warn"}">图面检查${v.passed ? "通过" : "有问题"}</span>`);
  if (r.diff) {
    const d = r.diff, n = d.moved.length + d.added.length + d.removed.length + d.modified.length + (d.title_block || []).length;
    facts.push(`<span class="fact">本次改动 ${n} 处，其余 ${d.unchanged_count} 个对象未动</span>`);
  }
  const dxf = artifacts.find((a) => a.format === "dxf");
  const el = addMsg("bot", `<h3>${att.length ? `图纸已生成，有 ${att.length} 处需要你注意` : "图纸已生成 ✓"}</h3>
    <div class="facts">${facts.join("")}</div>
    ${att.length ? `<ul class="notes">${att.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}
    <div class="sub">想改什么直接在下面说，比如“把 P-101 向右移动 800 mm”。</div>
    <div class="actions">${dxf ? `<a class="main" href="${dxf.url}" download>下载 DXF</a>` : ""}<button type="button">查看详细报告</button></div>`, att.length ? "warnbox" : "");
  el.querySelector("button").onclick = () => openDrawer("报告");
  setPlaceholder();
}
function renderDownloads(r) {
  const items = ["dxf", "pdf", "png"].map((f) => artifacts.find((a) => a.format === f)).filter(Boolean);
  $("downloads").innerHTML = items.map((a) => `<a class="${a.format === "dxf" ? "main" : ""}" href="${a.url}" download>${DL_NAMES[a.format]}</a>`).join("") + (r && r.bundle_url ? `<a href="${r.bundle_url}" download>全部（ZIP）</a>` : "");
  $("diffTab").hidden = !artifacts.some((a) => a.format === "diff_png");
}
function finalizeWaiting(t, r) {
  waiting = { spec: r.spec, question: r.question };
  if (artifacts.some((a) => a.format === "png" || a.format === "svg")) renderPreview();
  const q = r.question || {};
  const opts = (q.options || []).filter((o) => !(r.spec && o === "确认当前方案"));
  let html;
  if (r.spec) {
    const gen = (r.generated_symbols || []).length;
    html = `<h3>我整理了一个方案，请你核对</h3><div class="facts"><span class="fact">${r.spec.equipment.length} 台设备</span><span class="fact">${(r.spec.streams || []).length} 条连接</span>${(r.spec.terminals || []).length ? `<span class="fact">${r.spec.terminals.length} 个进出口</span>` : ""}${gen ? `<span class="fact warn">${gen} 个自动生成的图元</span>` : ""}</div>
      <div>${esc(q.question || "")}</div>
      <div class="actions"><button type="button" class="main" data-open-plan>核对并确认方案</button><button type="button" data-quick-confirm>不改，直接确认</button>${opts.map((o, i) => `<button type="button" data-opt="${i}">${esc(o)}</button>`).join("")}</div>`;
  } else {
    html = `<h3>需要你确认一下</h3><div>${esc(q.question || r.message || "")}</div><div class="actions">${opts.map((o, i) => `<button type="button" data-opt="${i}">${esc(o)}</button>`).join("")}</div><div class="sub">也可以直接在下方输入你的回答。</div>`;
  }
  const el = addMsg("bot", html, "warnbox");
  el.querySelector("[data-open-plan]")?.addEventListener("click", () => openPlan(r.spec));
  el.querySelector("[data-quick-confirm]")?.addEventListener("click", () => confirmSpec(r.spec));
  el.querySelectorAll("[data-opt]").forEach((b) => b.addEventListener("click", () => { el.querySelectorAll("button").forEach((x) => (x.disabled = true)); sendAnswer(opts[Number(b.dataset.opt)]); }));
  setPlaceholder();
  if (r.spec && !planDismissed) openPlan(r.spec);
}

// ------------------------------------------------------------ 详情抽屉
const TABS = ["报告", "需求核验", "图面检查", "改动", "用量", "过程记录"];
function openDrawer(tab) {
  $("drawer").hidden = false;
  $("drawerTabs").innerHTML = TABS.map((n) => `<button type="button" class="${n === tab ? "active" : ""}" data-tab="${n}">${n}</button>`).join("");
  $("drawerTabs").querySelectorAll("button").forEach((b) => (b.onclick = () => openDrawer(b.dataset.tab)));
  $("drawerBody").innerHTML = drawerHtml(tab);
}
$("btnDetails").onclick = () => openDrawer("报告");
document.querySelectorAll("[data-close]").forEach((el) => (el.onclick = () => { el.closest(".drawer").hidden = true; }));
function drawerHtml(tab) {
  const r = (lastTask && lastTask.result) || {};
  if (tab === "报告") return `<div class="md">${r.report ? mdToHtml(r.report) : "<p>暂无报告。</p>"}</div>${gapsHtml()}`;
  if (tab === "需求核验") return reqHtml(r);
  if (tab === "图面检查") return validationHtml(r);
  if (tab === "改动") return diffHtml(r.diff);
  if (tab === "用量") return usageHtml(r.usage);
  return `<div class="sub">给排查问题用的详细记录。</div>` + logs.filter((e) => e.kind === "tool_call" || e.kind === "execution_control").map((e) => e.kind === "tool_call"
    ? `<div class="logrow ${e.ok ? "" : "bad"}"><span class="ms">${e.ms} ms</span><b>${e.ok ? "✓" : "✗"} ${esc(e.name)}</b> <span class="sub">${esc(e.summary)}</span><details><summary>参数</summary><pre>${esc(JSON.stringify(e.arguments, null, 1))}</pre></details></div>`
    : `<div class="logrow">${esc(e.action || "")}${e.debug ? `<details><summary>技术细节</summary><pre>${esc(e.debug)}</pre></details>` : ""}</div>`).join("");
}
function reqHtml(r) {
  const reqs = (r.requirements || []).filter((x) => x.kind !== "equipment");
  if (!reqs.length) return "<p>这次需求里没有可逐项核对的工艺要求。</p>";
  const m = r.metrics || {}, q = m.requirements || {}, mark = { verified: ["ok", "✓"], missing: ["err", "✗"], unsupported: ["warn", "—"] };
  return `<h3>工艺需求核验 <span class="${q.verified === q.total ? "ok" : "err"}">${q.verified}/${q.total}</span></h3><div class="sub">按你写的原话，逐项核对图里是否真有这条连接、边界、仪表或标高。</div>
    <ul class="std">${reqs.map((x) => `<li class="${mark[x.status][0]}"><b>${mark[x.status][1]}</b> ${esc(x.text)}</li>`).join("")}</ul>
    ${(m.unchecked_clauses || []).length ? `<details><summary>${m.unchecked_clauses.length} 句描述无法逐项核对（不计入）</summary><ul>${m.unchecked_clauses.map((c) => `<li>${esc(c)}</li>`).join("")}</ul></details>` : ""}`;
}
function validationHtml(r) {
  const v = r.validation; if (!v) return "<p>暂无。</p>";
  const sc = v.standards, icon = { pass: "✓", warning: "!", error: "✗" }, cls = { pass: "ok", warning: "warn", error: "err" };
  return `<h3>图面几何检查 <span class="${v.passed ? "ok" : "err"}">${v.passed ? "通过" : "未通过"}</span></h3><div class="sub">只检查重叠、断线、穿设备等几何问题，不判断工艺对不对。</div>
    <ul><li>设备 ${v.stats?.equipment ?? "-"} 台，连接 ${v.stats?.connections ?? "-"} 条；管线交叉 ${v.stats?.crossings ?? 0} 处，折弯 ${v.stats?.bends ?? 0} 个</li>${[...(v.errors || []), ...(v.warnings || [])].map((w) => `<li>${esc(w.message)}</li>`).join("")}</ul>
    ${sc ? `<h3>制图规范自检 <span class="${sc.errors ? "err" : "ok"}">${esc(sc.score.text)}</span></h3><div class="sub">错误 ${sc.errors} 项，警告 ${sc.warnings} 项（${esc(sc.standard)}）。只代表制图规范，不代表工艺正确。</div>
    <ul class="std">${sc.items.map((x) => `<li class="${cls[x.status]}"><b>${icon[x.status]}</b> ${esc(x.name)}<span class="src">${esc(x.source)}</span>${x.status !== "pass" && x.details.length ? `<div class="sum">${esc(x.details.slice(0, 3).join("；"))}</div>` : ""}</li>`).join("")}</ul>` : ""}`;
}
function diffHtml(d) {
  if (!d) return "<p>这次是新画的图，没有改动对比。</p>";
  const items = [...d.moved.map((m) => `移动 ${esc(m.tag)}：dx=${m.dx}, dy=${m.dy} mm`), ...d.added.map((m) => `新增 ${esc(m.tag)}`), ...d.removed.map((m) => `删除 ${esc(m.tag)}`),
    ...d.modified.map((m) => `修改 ${esc(m.tag)}：${esc(Object.keys(m.changes).join("、"))}`), ...d.connections.rerouted.map((c) => `重连 ${esc(c.desc)}`),
    ...d.connections.added.map((c) => `新连接 ${esc(c.desc)}`), ...d.connections.removed.map((c) => `删连接 ${esc(c.desc)}`),
    ...(d.title_block || []).map((c) => `标题栏「${esc(TB_CN[c.field] || c.field)}」：${esc(c.before || "（空）")} → ${esc(c.after || "（空）")}`)];
  return `<h3>改动对比</h3><ul>${items.map((i) => `<li>${i}</li>`).join("")}<li class="ok">其余 ${d.unchanged_count} 个对象未改动</li></ul>`;
}
function usageHtml(u) {
  if (!u) return "<p>暂无。</p>";
  const tot = u.total || {};
  return `<h3>消耗</h3><ul><li><b>本轮</b>：工具 ${u.tool_calls} 次 · Token ${u.prompt_tokens} 入 / ${u.completion_tokens} 出 · ${u.seconds} 秒${u.search_calls ? ` · 联网检索 ${u.search_calls} 次` : ""}</li>
    ${tot.tool_calls !== undefined ? `<li><b>任务累计</b>：工具 ${tot.tool_calls} 次 · Token ${tot.prompt_tokens} 入 / ${tot.completion_tokens} 出 · ${tot.seconds} 秒</li>` : ""}<li>模型：${esc((u.models || []).join(", ") || "—")}</li></ul>`;
}
let gapCache = [];
function gapsHtml() {
  const mine = gapCache.filter((g) => g.task_id === current);
  return mine.length ? `<h3>知识缺口记录</h3><ul>${mine.map((g) => `<li>${esc(g.query)}：${esc(GAP_CN[g.fallback_used] || g.fallback_used)}</li>`).join("")}</ul>` : "";
}
async function loadGaps() { try { const r = await fetch("/api/knowledge-gaps"); if (r.ok) gapCache = (await r.json()).items; } catch { /* 看板只读，失败不影响 */ } }
function mdToHtml(md) {
  const out = []; let list = false; const inline = (t) => esc(t).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/`(.+?)`/g, "<code>$1</code>");
  for (const raw of String(md || "").split("\n")) {
    const line = raw.trimEnd(), li = line.match(/^\s*[-*]\s+(.*)$/);
    if (li) { if (!list) { out.push("<ul>"); list = true; } out.push(`<li>${inline(li[1])}</li>`); continue; }
    if (list) { out.push("</ul>"); list = false; }
    const h = line.match(/^(#{1,4})\s+(.*)$/);
    if (h) out.push(`<h${h[1].length + 1}>${inline(h[2])}</h${h[1].length + 1}>`); else if (line.trim()) out.push(`<p>${inline(line)}</p>`);
  }
  if (list) out.push("</ul>");
  return out.join("");
}

// ------------------------------------------------------------ 历史与恢复
$("btnHistory").onclick = () => { loadHistory(); $("historyDrawer").hidden = false; };
async function loadHistory() {
  try {
    const items = (await fetch("/api/tasks?limit=30").then((r) => r.json())).items;
    $("history").innerHTML = items.length ? items.map((t) => `<li class="${t.id === current ? "cur" : ""}" data-id="${esc(t.id)}"><b class="${/READY/.test(t.status) ? "ok" : t.status === "FAILED" ? "err" : "warn"}">${esc(STATUS_CN[t.status] || t.status)}</b>${esc(t.prompt)}${t.parent_id ? ' <span class="sub">（接着上一版改）</span>' : ""}</li>`).join("") : "<li>还没有任务</li>";
    $("history").querySelectorAll("li[data-id]").forEach((li) => (li.onclick = async () => {
      $("historyDrawer").hidden = true;
      const t = await fetch("/api/tasks/" + li.dataset.id).then((r) => r.json());
      start(li.dataset.id, { prompt: t.prompt });
    }));
  } catch { /* 历史加载失败不影响主流程 */ }
}
async function restore() {
  const id = (location.hash.match(/^#\/task\/(\w+)/) || [])[1] || load("zhihui.lastTask");
  if (!id) return;
  try {
    const r = await fetch("/api/tasks/" + id);
    if (!r.ok) { store("zhihui.lastTask", null); return; }
    const t = await r.json();
    start(id, { prompt: t.prompt });
  } catch { /* 后端未起时静默 */ }
}
async function health() {
  try {
    const r = await fetch("/api/health").then((x) => x.json());
    $("health").textContent = r.llm === "mock" ? "测试模式（未接入大模型）" : `${r.model.replace(/^deepseek:/, "DeepSeek · ")}${r.agent_mode === "multi" ? " · 多智能体" : ""}`;
    const note = $("mockNote");               // 没有离线版：没接入大模型就明确提醒，不假装能用
    note.hidden = !(r.llm === "mock" || r.key_missing);
    note.innerHTML = r.llm === "mock" ? "当前是<b>测试模式</b>（没有接入大模型），只用于开发测试，不是正式使用形态。" : "<b>还没有接入大模型</b>：请点右上角「⚙ 设置」填写 API Key，保存后即可使用。";
    $("btnSettings").classList.toggle("alert", !!r.key_missing);
    if (r.key_missing) $("health").textContent = "未填写 API Key，点“设置”填写";
  } catch { $("health").textContent = "后端未连接"; }
}

// ------------------------------------------------------------ 方案确认弹窗
async function symbols() {
  if (symbolCache) return symbolCache;
  try { symbolCache = (await fetch("/api/symbols").then((r) => r.json())).items; } catch { symbolCache = []; }
  return symbolCache;
}
const symOf = (id) => (symbolCache || []).find((s) => s.id === id);
const splitRef = (ref) => { const s = String(ref || ""); const i = s.indexOf("."); return i < 0 ? [s, ""] : [s.slice(0, i), s.slice(i + 1)]; };
const endpoints = () => [...planDraft.equipment.map((e) => ({ id: e.tag, label: `${e.tag} ${e.name || ""}`, symbol_id: e.symbol_id })), ...(planDraft.terminals || []).map((t) => ({ id: t.id, label: `${t.id} ${t.name}（进出口）`, symbol_id: "pipe_terminal" }))];
function portOptions(tag, kind, selected) {
  const ep = endpoints().find((e) => e.id === tag), sym = ep && symOf(ep.symbol_id);
  return `<option value="">自动选口</option>` + (sym ? sym.ports : []).filter((p) => p.kind === kind).map((p) => `<option value="${esc(p.name)}" ${p.name === selected ? "selected" : ""}>${esc(p.semantic)}</option>`).join("");
}
function epSelect(value, f, i) {
  const eps = endpoints();
  return `<select data-f="${f}" data-i="${i}">${eps.map((p) => `<option value="${esc(p.id)}" ${p.id === value ? "selected" : ""}>${esc(p.label)}</option>`).join("")}${eps.some((p) => p.id === value) ? "" : `<option selected value="${esc(value)}">${esc(value || "（未选）")}（不存在）</option>`}</select>`;
}
const snapshot = () => { undoStack.push(structuredClone(planDraft)); if (undoStack.length > 30) undoStack.shift(); redoStack = []; };
function flowInfo(s) {
  const tags = s.equipment.map((e) => e.tag), edges = (s.streams || []).map((x) => [splitRef(x.from ?? x.from_)[0], splitRef(x.to)[0]]).filter(([a, b]) => tags.includes(a) && tags.includes(b) && a !== b);
  const out = Object.fromEntries(tags.map((t) => [t, []])); edges.forEach(([a, b]) => out[a].push(b));
  const state = {}, back = [];
  const dfs = (u) => { state[u] = 1; for (const v of out[u]) { if (state[v] === 1) back.push([u, v]); else if (!state[v]) dfs(v); } state[u] = 2; };
  const hasIn = new Set(edges.map(([, b]) => b)); tags.filter((t) => !hasIn.has(t)).concat(tags).forEach((t) => { if (!state[t]) dfs(t); });
  const dag = edges.filter(([a, b]) => !back.some(([x, y]) => x === a && y === b)), indeg = Object.fromEntries(tags.map((t) => [t, 0])); dag.forEach(([, b]) => indeg[b]++);
  const order = [], queue = tags.filter((t) => indeg[t] === 0);
  while (queue.length) { const u = queue.shift(); order.push(u); dag.filter(([a]) => a === u).forEach(([, b]) => { if (--indeg[b] === 0) queue.push(b); }); }
  tags.forEach((t) => { if (!order.includes(t)) order.push(t); });
  const inc = {}; dag.forEach(([, b]) => (inc[b] = (inc[b] || 0) + 1));
  return { order, back, branching: tags.some((t) => out[t].length > 1) || Object.values(inc).some((n) => n > 1) };
}
function flowHtml() {
  const s = planDraft, f = flowInfo(s), byTag = Object.fromEntries(s.equipment.map((e) => [e.tag, e]));
  return f.order.map((t) => `<span class="node ${String(byTag[t].symbol_id).startsWith("gen_") ? "gen" : ""}">${esc(t)} ${esc(byTag[t].name || "")}</span>`).join('<span class="arrow">→</span>') +
    (f.branching ? '<div class="loop sub">含分支或汇合，具体接法见下面“连接”</div>' : "") + (f.back.length ? `<div class="loop">回流 / 循环：${f.back.map(([a, b]) => `${esc(a)} → ${esc(b)}`).join("、")}</div>` : "");
}
function refreshDerived() {
  $("flowBox").innerHTML = flowHtml();
  const j = $("planJSON"); if (j && document.activeElement !== j) j.value = JSON.stringify(planDraft, null, 2);
  planDraft.equipment.forEach((e, i) => {
    const row = document.querySelector(`[data-eq="${i}"]`);
    if (row) { row.classList.toggle("inferred", e.source === "inferred"); const c = row.querySelector(".src"); if (c) c.textContent = e.source === "inferred" ? "系统推测" : "已确认"; }
  });
}
function genBlock() {
  if (!genSyms.length) return "";
  const used = new Set(planDraft.equipment.map((e) => e.symbol_id));
  return `<section class="sec"><h3>自动生成的图元 <small>图元库里没有这些设备，已检索并生成示意图元，请核对外形和管口</small></h3><div class="gen-grid">${genSyms.map((g) => `<div class="gen-card ${used.has(g.id) ? "" : "unused"}">
    <img src="${g.preview_url}" alt="${esc(g.name)}"><div><b>${esc(g.name)}</b> <small>${g.reused ? "（曾生成，本次复用）" : ""}</small></div>
    <div class="sub">模板 ${esc(g.template)} · 可信度 ${g.confidence}${g.searched ? " · 已联网检索" : " · 没有检索依据，仅按名称的通用画法"}</div>
    <div class="sub">管口：${g.ports.map((p) => esc(p.name + "（" + (p.kind === "in" ? "入" : "出") + "，" + p.semantic + "）")).join("、")}</div>
    ${g.sources.length ? `<div class="sub">依据：${g.sources.map((u) => `<a href="${esc(u)}" target="_blank" rel="noopener noreferrer">${esc(u.replace(/^https?:\/\//, "").slice(0, 40))}</a>`).join("；")}</div>` : ""}${used.has(g.id) ? "" : '<div class="sub">当前方案没有使用它</div>'}</div>`).join("")}</div>
    <div class="sub">不合适？在下面“设备”里把图元换成别的，或确认后在 DXF 里手工修改。确认方案即表示接受这些示意图元。</div></section>`;
}
async function openPlan(spec) {
  await symbols();
  if (spec) { planDraft = structuredClone(spec); undoStack = []; redoStack = []; }
  $("planModal").hidden = false; $("planError").textContent = "";
  drawPlan();
}
function drawPlan() {
  const s = planDraft, body = $("planBody"), keep = body.scrollTop;
  s.streams = s.streams || []; s.terminals = s.terminals || []; s.instruments = s.instruments || []; s.fittings = s.fittings || []; s.assumptions = s.assumptions || [];
  s.drawing = s.drawing || {}; s.drawing.title_block = s.drawing.title_block || {};
  s.streams.forEach((x) => { if (x.from === undefined) x.from = x.from_ ?? ""; delete x.from_; });
  const syms = symbolCache || [], tb = s.drawing.title_block;
  const symSel = (e, i) => `<select class="sym" data-f="eq.symbol" data-i="${i}">${syms.map((y) => `<option value="${esc(y.id)}" ${y.id === e.symbol_id ? "selected" : ""}>${esc(y.name)}</option>`).join("")}${syms.some((y) => y.id === e.symbol_id) ? "" : `<option selected value="${esc(e.symbol_id)}">${esc(e.symbol_id)}（不存在）</option>`}</select>`;
  body.innerHTML = `
    <section class="sec"><h3>流程概览 <small>由下面的连接自动生成</small></h3><div class="flow" id="flowBox"></div></section>
    ${genBlock()}
    <section class="sec"><h3>设备 <small>${s.equipment.length} 台 · 位号、名称、图元样式</small></h3>
      ${s.equipment.map((e, i) => `<div class="row ${e.source === "inferred" ? "inferred" : ""}" data-eq="${i}"><input class="tag" data-f="eq.tag" data-i="${i}" value="${esc(e.tag)}" aria-label="位号"><input class="name" data-f="eq.name" data-i="${i}" value="${esc(e.name)}" placeholder="设备名称" aria-label="设备名称">${symSel(e, i)}<span class="src">${e.source === "inferred" ? "系统推测" : "已确认"}</span><button class="del" type="button" data-del-eq="${i}" title="删除">✕</button></div>`).join("")}
      <button class="addrow" type="button" id="addEq">＋ 增加设备</button></section>
    <section class="sec"><h3>连接 <small>${s.streams.length} 条 · 从哪台设备的哪个口，到哪台设备的哪个口</small></h3>
      ${s.streams.map((x, i) => { const [fa, fp] = splitRef(x.from), [ta, tp] = splitRef(x.to); return `<div class="row ${x.source === "inferred" ? "inferred" : ""}"><span class="group">${epSelect(fa, "st.from", i)}<select data-f="st.fromPort" data-i="${i}">${portOptions(fa, "out", fp)}</select></span><span class="arrow">→</span><span class="group">${epSelect(ta, "st.to", i)}<select data-f="st.toPort" data-i="${i}">${portOptions(ta, "in", tp)}</select></span><input class="medium" data-f="st.medium" data-i="${i}" value="${esc(x.medium || "")}" placeholder="介质（可空）"><button class="del" type="button" data-del-st="${i}" title="删除">✕</button></div>`; }).join("")}
      <button class="addrow" type="button" id="addSt">＋ 增加连接</button></section>
    <section class="sec"><h3>进出系统的物流 <small>${s.terminals.length} 个 · 原料进来、产品出去、冷却水供回等</small></h3>
      ${s.terminals.map((t, i) => { const [ta, tp] = splitRef(t.connected_to); return `<div class="row ${t.source === "inferred" ? "inferred" : ""}"><input class="name" data-f="tm.name" data-i="${i}" value="${esc(t.name)}" aria-label="名称"><select data-f="tm.dir" data-i="${i}"><option value="in" ${t.direction === "in" ? "selected" : ""}>进入系统</option><option value="out" ${t.direction === "out" ? "selected" : ""}>离开系统</option></select><span class="arrow">接在</span><select data-f="tm.to" data-i="${i}">${s.equipment.map((e) => `<option value="${esc(e.tag)}" ${e.tag === ta ? "selected" : ""}>${esc(e.tag)} ${esc(e.name || "")}</option>`).join("")}</select><select data-f="tm.port" data-i="${i}">${portOptions(ta, t.direction, tp)}</select><button class="del" type="button" data-del-tm="${i}" title="删除">✕</button></div>`; }).join("") || '<div class="sub">没有</div>'}</section>
    ${(s.instruments.length || s.fittings.length) ? `<section class="sec"><h3>仪表与阀门</h3>${s.instruments.map((x, i) => `<div class="row">仪表 ${esc(x.letters)}-${esc(x.loop_no)}<span class="src">${esc(x.attach_conn || x.attach_to || "")}</span><button class="del" type="button" data-del-ins="${i}">✕</button></div>`).join("")}${s.fittings.map((x, i) => `<div class="row">阀门/管件：${esc((x.items?.[0]?.fittings || []).map((f) => f.type).join("、"))}<span class="src">${esc(x.items?.[0]?.ref_conn || "")}</span><button class="del" type="button" data-del-fit="${i}">✕</button></div>`).join("")}</section>` : ""}
    <section class="sec"><h3>标题栏 <small>留空就不填，不会替你编造</small></h3><div class="tb-grid">${[["drawing_name", "图名"], ["drawing_no", "图号"], ["unit_name", "设计单位"], ["project", "工程名称"]].map(([k, l]) => `<label>${l}<input data-f="tb" data-k="${k}" value="${esc(tb[k] || "")}"></label>`).join("")}</div></section>
    ${s.assumptions.length ? `<section class="sec"><h3>系统的假设 <small>请确认是否合理</small></h3><ul>${s.assumptions.map((a) => `<li>${esc(a)}</li>`).join("")}</ul></section>` : ""}
    <details class="sec"><summary>高级：完整方案 JSON</summary><textarea id="planJSON" rows="10"></textarea><button type="button" class="ghost" id="applyJSON">应用 JSON 修改</button></details>`;
  refreshDerived(); body.scrollTop = keep;
}
// 事件委托：文本输入即时更新（不重绘），下拉和增删重绘
$("planBody").addEventListener("focusin", (e) => { if (e.target.matches("input")) e.target._pending = structuredClone(planDraft); });
$("planBody").addEventListener("input", (e) => {
  const el = e.target, f = el.dataset.f, i = Number(el.dataset.i);
  if (!f || el.tagName === "SELECT") return;
  if (f === "eq.tag" || f === "eq.name") { const x = planDraft.equipment[i]; x[f === "eq.tag" ? "tag" : "name"] = el.value; x.source = "user"; x.confidence = 1; }
  else if (f === "st.medium") { planDraft.streams[i].medium = el.value; planDraft.streams[i].source = "user"; }
  else if (f === "tm.name") { planDraft.terminals[i].name = el.value; planDraft.terminals[i].source = "user"; }
  else if (f === "tb") { const k = el.dataset.k; if (el.value) planDraft.drawing.title_block[k] = el.value; else delete planDraft.drawing.title_block[k]; if (k === "drawing_name" && el.value) planDraft.drawing.title = el.value; }
  refreshDerived();
});
$("planBody").addEventListener("change", (e) => {
  const el = e.target, f = el.dataset.f, i = Number(el.dataset.i);
  if (el.tagName === "INPUT") { if (el._pending) { undoStack.push(el._pending); redoStack = []; el._pending = null; } return; }
  if (!f) return;
  snapshot();
  const s = planDraft, join = (a, p) => a + (p ? "." + p : "");
  if (f === "eq.symbol") { const x = s.equipment[i]; x.symbol_id = el.value; x.source = "user"; x.confidence = 1; }
  else if (f.startsWith("st.")) {
    const x = s.streams[i], [fa, fp] = splitRef(x.from), [ta, tp] = splitRef(x.to);
    if (f === "st.from") x.from = el.value; else if (f === "st.fromPort") x.from = join(fa, el.value); else if (f === "st.to") x.to = el.value; else if (f === "st.toPort") x.to = join(ta, el.value);
    x.source = "user";
  } else if (f.startsWith("tm.")) {
    const t = s.terminals[i], [ta] = splitRef(t.connected_to);
    if (f === "tm.dir") { t.direction = el.value; t.connected_to = ta; } else if (f === "tm.to") t.connected_to = el.value; else if (f === "tm.port") t.connected_to = join(ta, el.value);
    t.source = "user";
  }
  drawPlan();
});
$("planBody").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  const s = planDraft;
  if (b.id === "addEq") { snapshot(); let n = 101; const used = new Set([...s.equipment.map((x) => x.tag), ...s.terminals.map((t) => t.id)]); while (used.has("V-" + n)) n++; s.equipment.push({ tag: "V-" + n, name: "", symbol_id: "tank", source: "user", confidence: 1, params: {}, family: "" }); drawPlan(); }
  else if (b.id === "addSt") { snapshot(); const eps = endpoints(); s.streams.push({ from: eps[0]?.id || "", to: eps[1]?.id || "", medium: "", source: "user" }); drawPlan(); }
  else if (b.id === "applyJSON") { try { snapshot(); planDraft = JSON.parse($("planJSON").value); drawPlan(); } catch (err) { $("planError").textContent = "JSON 格式错误：" + err.message; } }
  else if (b.dataset.delEq !== undefined) removeEquipment(Number(b.dataset.delEq));
  else if (b.dataset.delSt !== undefined) { snapshot(); s.streams.splice(Number(b.dataset.delSt), 1); drawPlan(); }
  else if (b.dataset.delTm !== undefined) { snapshot(); s.terminals.splice(Number(b.dataset.delTm), 1); drawPlan(); }
  else if (b.dataset.delIns !== undefined) { snapshot(); s.instruments.splice(Number(b.dataset.delIns), 1); drawPlan(); }
  else if (b.dataset.delFit !== undefined) { snapshot(); s.fittings.splice(Number(b.dataset.delFit), 1); drawPlan(); }
});
function removeEquipment(index) {
  const e = planDraft.equipment[index], tag = e.tag;
  const links = planDraft.streams.filter((x) => splitRef(x.from)[0] === tag || splitRef(x.to)[0] === tag), bounds = planDraft.terminals.filter((t) => splitRef(t.connected_to)[0] === tag);
  if (!confirm(`删除 ${tag} ${e.name || ""}？\n它有 ${links.length} 条连接、${bounds.length} 个进出口。`)) return;
  snapshot();
  const ins = links.filter((x) => splitRef(x.to)[0] === tag), outs = links.filter((x) => splitRef(x.from)[0] === tag);
  let bridged = false;
  if (ins.length && outs.length && confirm("点“确定”：把上游直接接到下游，保持流程连通。\n点“取消”：同时删除这些连接。")) {
    ins.forEach((a) => outs.forEach((b) => planDraft.streams.push({ from: a.from, to: b.to, medium: a.medium || b.medium || "", source: "user" }))); bridged = true;
  }
  planDraft.streams = planDraft.streams.filter((x) => !links.includes(x)); planDraft.terminals = planDraft.terminals.filter((t) => !bounds.includes(t)); planDraft.equipment.splice(index, 1);
  drawPlan(); $("planError").innerHTML = `<span class="okmsg">已删除 ${esc(tag)}${bridged ? "，并把上游接到了下游" : "及其连接"}；可点“撤销”恢复。</span>`;
}
$("undoPlan").onclick = () => { if (undoStack.length) { redoStack.push(structuredClone(planDraft)); planDraft = undoStack.pop(); drawPlan(); } };
$("redoPlan").onclick = () => { if (redoStack.length) { undoStack.push(structuredClone(planDraft)); planDraft = redoStack.pop(); drawPlan(); } };
document.querySelectorAll("[data-close-plan]").forEach((el) => (el.onclick = () => { $("planModal").hidden = true; planDismissed = true; }));
$("confirmPlan").onclick = () => confirmSpec(planDraft, true);
async function confirmSpec(spec, fromModal) {
  const btn = $("confirmPlan"); btn.disabled = true;
  try {
    const res = await fetch(`/api/tasks/${current}/confirm`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(spec) });
    const data = await res.json();
    if (!res.ok) {
      const list = (data.issues || []).map((i) => `<li>${esc(i.message)}</li>`).join("");
      if (!fromModal) { await symbols(); planDraft = structuredClone(spec); await openPlan(); }
      $("planError").innerHTML = `<b>${esc(typeof data.detail === "string" ? data.detail : "方案需要修改")}</b>${list ? `<ul>${list}</ul>` : ""}`;
      return;
    }
    $("planModal").hidden = true; waiting = null; setBusy(true); setPlaceholder(); setProgress("方案已确认，开始绘图…", 2); poll();
  } catch (e) { $("planError").textContent = "提交失败：" + e.message; } finally { btn.disabled = false; }
}

// 设置对话框在 shell.js 里；保存后刷新连接状态
window.addEventListener("zhsettings:saved", () => health());
window.addEventListener("message", (e) => {          // 外面的通用 CAD 页面把一句流程图口令交过来
  const m = e.data || {};
  if (m.type === "zh:prompt" && m.text && !$("send").disabled) { $("prompt").value = m.text; updateCount(); send(); }
});

document.addEventListener("keydown", (e) => { if (e.key === "Escape") { if (document.querySelector(".stage.fullscreen")) { $("zoomFull").click(); return; } $("planModal").hidden = true; $("drawer").hidden = true; $("historyDrawer").hidden = true; } });

updateCount(); setPlaceholder();
health(); loadGaps(); loadHistory();
restore().then(() => {
  if (!current) greet();
  const m = location.hash.match(/^#prompt=(.+)$/);        // 从“通用绘图”页转来的流程图口令：自动填入并发送
  if (m && !current) { history.replaceState(null, "", location.pathname); $("prompt").value = decodeURIComponent(m[1]); updateCount(); send(); }
});
