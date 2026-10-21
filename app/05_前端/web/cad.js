/* 智绘CAD · 通用绘图前端：SVG 交互视图、捕捉与追踪、选择与夹点、图层与特性面板、命令行与语音、对话与计划进度。
   所有改图都走 /api/drawings/{id}/ops（直接调用函数，不经过大模型）或 /chat（智能体），落版本、可撤销。 */
(() => {
"use strict";
const $ = (s, r = document) => r.querySelector(s);
const NS = "http://www.w3.org/2000/svg";
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const el = (tag, attrs = {}, parent) => { const e = document.createElementNS(NS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); if (parent) parent.appendChild(e); return e; };
const fnum = (v) => String(+(+v).toFixed(4));
const fpt = (p) => `${fnum(p[0])},${fnum(p[1])}`;
const dist = (a, b) => Math.hypot(a[0] - b[0], a[1] - b[1]);

const LT = { DASHED: [6, 3], HIDDEN: [3, 2], CENTER: [14, 3, 3, 3], DASHDOT: [8, 3, 1, 3], DOT: [0.5, 3], PHANTOM: [14, 3, 3, 3, 3, 3], DIVIDE: [10, 3, 1, 3, 1, 3], BORDER: [10, 3, 10, 3, 1, 3] };
const SNAP_CN = { end: "端点", mid: "中点", cen: "圆心", qua: "象限点", nod: "节点", ins: "插入点", gce: "几何中心", int: "交点", nea: "最近点", grid: "栅格" };
const SNAP_MODE = { end: "endpoint", mid: "midpoint", cen: "center", qua: "quadrant", nod: "node", ins: "insertion", gce: "geometric_center", int: "intersection", nea: "nearest" };
const SNAP_PRI = { end: 1, int: 1, mid: 2, cen: 2, gce: 2, qua: 3, nod: 3, ins: 3, nea: 6, grid: 9 };

const st = {
  id: null, sc: null, model: null, rev: -1, W: 800, H: 600, view: { cx: 210, cy: 148, s: 2 }, serverView: "", toolName: "select", tool: null,
  sel: new Set(), cand: new Set(), hover: null, cur: null, snap: null, lw: true, props: null, drag: null, space: "Model",
  task: null, taskAfter: 0, planSteps: [], pickAnswer: null, busy: false, spaceDown: false, panning: null, lastMid: 0, scenes: {},
  // 工作空间（顶栏）：pfd 化工流程图 / equip 化工设备图 / general 通用绘图；设备图相关状态
  ws: "general", prevWs: "general", pfdOn: false, vessel: null, vesselErr: "", vTok: 0, eqView: null, eqViewUser: false, eqBusy: false, sheetTok: 0, sheetKey: "", sheetFor: "", autoDone: null, vpMsg: null, vaDraft: { purpose: "", dn: "", loc: "" }, lastPrompt: "",
};

// ------------------------------------------------------------------ 接口
async function api(path, opts = {}) {
  const r = await fetch(path, { headers: opts.body ? { "Content-Type": "application/json" } : {}, ...opts, body: opts.body ? JSON.stringify(opts.body) : undefined });
  let j = null; try { j = await r.json(); } catch { /* 非 JSON */ }
  if (!r.ok) throw new Error((j && (j.detail || j.message)) || `HTTP ${r.status}`);
  return j;
}
async function op(tool, args = {}, { quiet = false, refresh: doRefresh = true, warn = true } = {}) {
  if (!st.id) { log("请先新建或打开一张图纸", "err"); return null; }
  const r = await api(`/api/drawings/${st.id}/ops`, { method: "POST", body: { tool, args } });
  if (!r.ok) { log(`${r.message || r.error_code}${r.hint ? "（" + r.hint + "）" : ""}`, "err"); smsg(r.message || r.error_code); return r; }
  if (!quiet && r.summary) log(r.summary, "ok");
  if (warn) (r.warnings || []).forEach((w) => log("注意：" + w, "warn"));
  if (doRefresh) await refresh(true);
  return r;
}

// ------------------------------------------------------------------ 日志与提示
function log(text, cls = "") { const d = document.createElement("div"); if (cls) d.className = cls; d.textContent = text; const b = $("#cmdlog"); b.appendChild(d); b.scrollTop = b.scrollHeight; while (b.children.length > 200) b.removeChild(b.firstChild); }
function smsg(t) { $("#smsg").textContent = t || ""; }
function setPrompt(t) { $("#prompt").textContent = t || "命令："; }

// ------------------------------------------------------------------ 视图变换
const svg = $("#svg"), world = $("#world"), stage = $("#stage");
function applyView() {
  const v = st.view;
  world.setAttribute("transform", `translate(${st.W / 2} ${st.H / 2}) scale(${v.s} ${-v.s}) translate(${-v.cx} ${-v.cy})`);
  world.style.setProperty("--px", String(1 / v.s));
  $("#paperG").setAttribute("transform", world.getAttribute("transform"));
  updateHud();
  drawGrid(); drawOverlay();
}
function updateHud() {
  const sc = st.sc, h = $("#hud");
  if (!sc) { h.textContent = "未打开图纸"; return; }
  h.textContent = sheetOn() ? `${sc.title || st.id} · 布局「${eqView()}」出图预览 · 第 ${sc.revision} 版 · 滚轮缩放 · 拖动平移 · 双击适应` : `${sc.title || st.id} · ${st.space === "Model" ? "模型" : st.space} · 比例 ${st.view.s.toFixed(2)} px/mm · ${sc.entity_count} 个图元`;
}
const s2m = (x, y) => [(x - st.W / 2) / st.view.s + st.view.cx, -(y - st.H / 2) / st.view.s + st.view.cy];
const m2s = (x, y) => [(x - st.view.cx) * st.view.s + st.W / 2, -(y - st.view.cy) * st.view.s + st.H / 2];
function evPt(ev) { const r = svg.getBoundingClientRect(); return s2m(ev.clientX - r.left, ev.clientY - r.top); }
function zoomAt(f, sx, sy) {
  const [mx, my] = s2m(sx, sy); st.view.s = Math.min(Math.max(st.view.s * f, 1e-4), 1e5);
  st.view.cx = mx - (sx - st.W / 2) / st.view.s; st.view.cy = my + (sy - st.H / 2) / st.view.s; applyView(); saveViewSoon();
}
function fitBox(b, margin = 1.08) {
  if (!b) return; const w = Math.max(b[2] - b[0], 1), h = Math.max(b[3] - b[1], 1);
  st.view.s = Math.min(st.W / (w * margin), st.H / (h * margin)); st.view.cx = (b[0] + b[2]) / 2; st.view.cy = (b[1] + b[3]) / 2; applyView(); saveViewSoon();
}
function zoomExtents() {
  const sc = st.sc; if (!sc) return;
  if (st.space !== "Model") { const l = sc.layouts.find((x) => x.name === st.space); if (l) return fitBox([0, 0, ...l.page_setup.size], 1.06); }
  const b = sc.bbox ? sc.bbox.slice() : sc.limits.slice(); fitBox(b);
}
let viewTimer = null;
function saveViewSoon() {
  clearTimeout(viewTimer);
  viewTimer = setTimeout(() => {
    if (!st.id || st.space !== "Model") return;
    const w = st.view; const hw = st.W / w.s / 2, hh = st.H / w.s / 2;
    st.serverView = `${fnum(w.cx)},${fnum(w.cy)},${fnum(hh * 2)}`;
    api(`/api/drawings/${st.id}/ops`, { method: "POST", body: { tool: "control_viewport_view", args: { action: "zoom_window", window_min: `${fnum(w.cx - hw)},${fnum(w.cy - hh)}`, window_max: `${fnum(w.cx + hw)},${fnum(w.cy + hh)}` } } }).catch(() => {});
  }, 600);
}
new ResizeObserver(() => { const r = stage.getBoundingClientRect(); st.W = r.width; st.H = r.height; svg.setAttribute("viewBox", `0 0 ${st.W} ${st.H}`); applyView(); sheetOnResize(); }).observe(stage);

// ------------------------------------------------------------------ 场景渲染
const fgColor = () => (st.sc && st.space === "Model" && isDark(st.sc.background) ? "#f2f2f2" : "#111111");     // 布局是白纸，永远用深色
function isDark(hex) { const m = /^#?([0-9a-f]{6})$/i.exec(hex || ""); if (!m) return false; const n = parseInt(m[1], 16); return ((n >> 16) * 0.3 + ((n >> 8) & 255) * 0.59 + (n & 255) * 0.11) < 110; }
const colorOf = (c) => (c === "fg" ? fgColor() : c);

async function refresh(force = false) {
  if (!st.id) return;
  try {
    const q = new URLSearchParams(); if (!force && st.rev >= 0) q.set("since", st.rev);
    const sc = await api(`/api/drawings/${st.id}/scene?${q}`);
    if (sc.unchanged) return;
    await loadScene(sc);
  } catch (e) { smsg(e.message); }
}
async function loadScene(sc) {
  const prevSpace = st.space;
  st.sc = sc; st.rev = sc.revision; st.space = sc.space;
  st.model = null;
  if (sc.space !== "Model") { try { st.model = await api(`/api/drawings/${st.id}/scene?space=Model`); } catch { /* 没有模型内容 */ } }
  st.sel = new Set(sc.session.selection || []);
  const sv = `${fnum(sc.view.center[0])},${fnum(sc.view.center[1])},${fnum(sc.view.height)}`;
  const first = st.firstLoad !== st.id || prevSpace !== st.space;
  if (first) { st.firstLoad = st.id; zoomExtentsFromScene(sc); st.serverView = sv; }
  else if (sv !== st.serverView) { st.serverView = sv; st.view.cx = sc.view.center[0]; st.view.cy = sc.view.center[1]; st.view.s = st.H / Math.max(sc.view.height, 1e-6); }  // 智能体/命令改了视图
  stage.style.background = sc.space === "Model" ? (sc.background || "#ffffff") : "#8a93a6";
  document.documentElement.style.setProperty("--fg", fgColor());
  $("#img3d").style.background = sc.background || "#fff";
  renderAll();
  renderLayers(); renderInfo(); renderTabs(); renderFlags(); updateProps(); show3d();
  $("#btnUndo").disabled = !sc.session.undo; $("#btnRedo").disabled = !sc.session.redo;
  afterScene();
}
function zoomExtentsFromScene(sc) {
  if (sc.space !== "Model") { const l = sc.layouts.find((x) => x.name === sc.space); if (l) return fitBox([0, 0, ...l.page_setup.size], 1.06); }
  const b = sc.bbox ? sc.bbox.slice() : null;
  if (b) fitBox([Math.min(b[0], sc.limits[0]), Math.min(b[1], sc.limits[1]), Math.max(b[2], sc.limits[2]), Math.max(b[3], sc.limits[3])]); else fitBox(sc.limits.slice());
}

function renderAll() {
  const sc = st.sc, entG = $("#entG"), vpG = $("#vpG"), paperG = $("#paperG"), defs = $("#defs");
  entG.replaceChildren(); vpG.replaceChildren(); paperG.replaceChildren(); defs.replaceChildren();
  st.hatchN = 0;
  const layout = st.space !== "Model" ? sc.layouts.find((l) => l.name === st.space) : null;
  if (layout) {
    const [W, H] = layout.page_setup.size;
    el("rect", { x: 3, y: -3, width: W, height: H, fill: "#00000022", transform: "scale(1 -1)" }, paperG).setAttribute("transform", `translate(0 ${H}) scale(1 -1) translate(0 ${-H})`);
    el("rect", { x: 0, y: 0, width: W, height: H, fill: "#ffffff", stroke: "#8a93a6", "stroke-width": 0.4 }, paperG);
  }
  for (const e of sc.entities) mkEnt(e, entG, sc, 1);
  if (layout && st.model) {
    layout.viewports.forEach((vp, i) => {
      const [[x0, y0], [x1, y1]] = vp.corners; const w = Math.abs(x1 - x0), h = Math.abs(y1 - y0), cx = (x0 + x1) / 2, cy = (y0 + y1) / 2;
      const k = h / vp.view_height; const cid = `vpc${i}`;
      const cp = el("clipPath", { id: cid }, defs); el("rect", { x: Math.min(x0, x1), y: Math.min(y0, y1), width: w, height: h }, cp);
      const g = el("g", { class: "vp", "clip-path": `url(#${cid})` }, vpG);
      const inner = el("g", { transform: `translate(${cx} ${cy}) scale(${k}) translate(${-vp.target_center[0]} ${-vp.target_center[1]})` }, g);
      inner.style.setProperty("--px", String(1 / (st.view.s * k)));
      for (const e of st.model.entities) if (!(vp.frozen_layers || []).includes(e.layer)) mkEnt(e, inner, st.model, 1, false);
      const fr = el("rect", { class: "frame", x: Math.min(x0, x1), y: Math.min(y0, y1), width: w, height: h, fill: "none", stroke: "#2e6fdb", "stroke-dasharray": "6 4" }, vpG);
      fr.style.setProperty("stroke-width", "1px");
      const lb = el("text", { x: Math.min(x0, x1) + 1.5, y: -(Math.min(y0, y1) + h - 3), transform: "scale(1 -1)", "font-size": 3, fill: "#2e6fdb" }, vpG); lb.textContent = `${vp.id} · ${vp.scale}${vp.locked ? " 🔒" : ""}`;
    });
  }
  applyView();
}

function patternId(kind, scale, angle, col) {
  const id = `hp${++st.hatchN}`; const sp = Math.max(scale * 3, 0.3);
  const p = el("pattern", { id, patternUnits: "userSpaceOnUse", width: sp, height: sp, patternTransform: `rotate(${angle + (kind === "ANSI31" || kind === "ANSI32" ? 45 : 0)})` }, $("#defs"));
  const sw = el("path", { d: `M0 0L0 ${sp}`, stroke: col, fill: "none" }, p); sw.style.setProperty("stroke-width", "calc(var(--px) * 1)");
  if (/^ANSI3[4-8]|AR-|HONEY|NET|CROSS/.test(kind)) el("path", { d: `M0 0L${sp} 0`, stroke: col, fill: "none", style: "stroke-width:calc(var(--px) * 1)" }, p);
  return id;
}

function mkEnt(e, parent, sc, k, interactive = true) {
  if (e.hidden) return;
  const g = el("g", { class: "ent" + (e.locked ? " locked" : ""), "data-id": e.id }, parent);
  const col = colorOf(e.col);
  g.style.color = col; g.style.stroke = col;
  const wpx = st.lw ? Math.max(1, e.lw * 3.2) : 1;
  g.style.strokeWidth = `calc(var(--px) * ${wpx})`;
  const dash = LT[e.lt];
  if (dash) g.style.strokeDasharray = dash.map((v) => v * (sc.ltscale || 1) * (e.lts || 1)).join(" ");
  for (const it of e.items) {
    if (it.t === "path") {
      if (it.fill) { el("path", { d: it.d, class: "fill", fill: col }, g); continue; }
      const p = el("path", { d: it.d }, g);
      if (it.mesh) p.style.strokeOpacity = "0.7";
      if (interactive) { const h = el("path", { d: it.d, class: "hit" }, g); h.style.strokeWidth = "calc(var(--px) * 9)"; }
    } else if (it.t === "circle") {
      el("circle", { cx: it.c[0], cy: it.c[1], r: it.r }, g);
      if (interactive) { const h = el("circle", { cx: it.c[0], cy: it.c[1], r: it.r, class: "hit" }, g); h.style.strokeWidth = "calc(var(--px) * 9)"; }
    } else if (it.t === "point") {
      const c = el("circle", { cx: it.p[0], cy: it.p[1], r: 0 }, g); c.style.r = "calc(var(--px) * 2.2)"; c.style.fill = col;
    } else if (it.t === "text") {
      const t = el("text", { transform: `translate(${it.p[0]} ${it.p[1]}) scale(1 -1) rotate(${-(it.rot || 0)})`, "font-size": it.h * 1.3, fill: col, stroke: "none", "text-anchor": it.align === "center" ? "middle" : it.align === "right" ? "end" : "start", y: -(it.dy || 0) }, g);
      t.textContent = it.s; t.style.fontFamily = '"FangSong","STFangsong","Noto Serif CJK SC",serif';
    } else if (it.t === "hatch") {
      const d = it.loops.map((l) => "M" + l.map((p) => p[0] + " " + p[1]).join("L") + "Z").join("");
      const h = el("path", { d, "fill-rule": "evenodd", stroke: "none" }, g);
      if (it.solid) { h.setAttribute("fill", col); h.setAttribute("fill-opacity", "0.55"); } else h.setAttribute("fill", `url(#${patternId(it.pattern, it.scale, it.angle, col)})`);
    }
  }
  if (interactive) { if (st.sel.has(e.id)) g.classList.add("sel"); if (st.cand.has(e.id)) g.classList.add("cand"); }
}
const entById = (id) => (st.sc ? st.sc.entities.find((e) => e.id === id) : null);

// ------------------------------------------------------------------ 栅格、叠加层、捕捉
function drawGrid() {
  const g = $("#gridG"); g.replaceChildren(); const sc = st.sc; if (!sc || st.space !== "Model") return;
  const dr = sc.draft;
  if (dr.grid) {
    let gs = dr.grid_spacing[0] || 10; while (gs * st.view.s < 9) gs *= 5; while (gs * st.view.s > 200 && gs > 1e-3) gs /= 5;
    const [x0, y0] = s2m(0, st.H), [x1, y1] = s2m(st.W, 0); let d = "", major = "";
    for (let x = Math.ceil(x0 / gs) * gs; x <= x1; x += gs) (Math.abs(Math.round(x / gs)) % 5 === 0 ? (major += `M${x} ${y0}L${x} ${y1}`) : (d += `M${x} ${y0}L${x} ${y1}`));
    for (let y = Math.ceil(y0 / gs) * gs; y <= y1; y += gs) (Math.abs(Math.round(y / gs)) % 5 === 0 ? (major += `M${x0} ${y}L${x1} ${y}`) : (d += `M${x0} ${y}L${x1} ${y}`));
    for (const [dd, c] of [[d, "var(--grid)"], [major, "var(--grid-major)"]]) { const p = el("path", { d: dd, fill: "none", stroke: c }, g); p.style.strokeWidth = "calc(var(--px) * 0.6)"; }
  }
  const L = sc.limits; const r = el("rect", { x: L[0], y: L[1], width: L[2] - L[0], height: L[3] - L[1], fill: "none", stroke: "var(--grid-major)", "stroke-dasharray": "6 6" }, g); r.style.strokeWidth = "calc(var(--px) * 1)";
}

function visibleEnts() { const sc = st.sc; return sc ? sc.entities.filter((e) => !e.hidden) : []; }
function segsOf(e) {          // 直线 / 多段线（只取 M L）的线段
  if (e._segs) return e._segs; const out = [];
  if (e.k === "line" || e.k === "polyline") for (const it of e.items) if (it.t === "path" && !/A/.test(it.d)) {
    const re = /([ML])(-?[\d.e+-]+) (-?[\d.e+-]+)/g; let m, last = null;
    while ((m = re.exec(it.d))) { const p = [+m[2], +m[3]]; if (m[1] === "L" && last) out.push([last, p]); last = p; }
  }
  return (e._segs = out);
}
function segInt(a, b, c, d) {
  const r = [b[0] - a[0], b[1] - a[1]], s = [d[0] - c[0], d[1] - c[1]]; const den = r[0] * s[1] - r[1] * s[0]; if (Math.abs(den) < 1e-12) return null;
  const t = ((c[0] - a[0]) * s[1] - (c[1] - a[1]) * s[0]) / den, u = ((c[0] - a[0]) * r[1] - (c[1] - a[1]) * r[0]) / den;
  return t >= 0 && t <= 1 && u >= 0 && u <= 1 ? [a[0] + t * r[0], a[1] + t * r[1]] : null;
}
function computeSnap(p) {
  const sc = st.sc; if (!sc || st.space !== "Model") return { p, kind: null };
  const dr = sc.draft, rad = 14 / st.view.s; let best = null;
  const modes = new Set(dr.osnap ? dr.osnap_modes : []);
  const consider = (kind, pt) => { const dd = dist(pt, p); if (dd > rad) return; if (!best || SNAP_PRI[kind] < SNAP_PRI[best.kind] || (SNAP_PRI[kind] === SNAP_PRI[best.kind] && dd < best.d)) best = { kind, p: pt, d: dd }; };
  if (dr.osnap || st.tool?.forceSnap) {
    const near = [];
    for (const e of visibleEnts()) {
      if (!e.bb || e.bb[0] - rad > p[0] || e.bb[2] + rad < p[0] || e.bb[1] - rad > p[1] || e.bb[3] + rad < p[1]) continue;
      near.push(e);
      for (const [t, x, y] of e.sn || []) if (modes.has(SNAP_MODE[t])) consider(t, [x, y]);
    }
    if (modes.has("intersection") && near.length > 1) {
      const segs = near.flatMap((e) => segsOf(e).map((s) => ({ e, s }))).slice(0, 120);
      for (let i = 0; i < segs.length; i++) for (let j = i + 1; j < segs.length; j++) if (segs[i].e !== segs[j].e) { const q = segInt(segs[i].s[0], segs[i].s[1], segs[j].s[0], segs[j].s[1]); if (q) consider("int", q); }
    }
    if (modes.has("nearest") && !best) for (const e of near) for (const [a, b] of segsOf(e)) {
      const dx = b[0] - a[0], dy = b[1] - a[1], L2 = dx * dx + dy * dy || 1, t = Math.max(0, Math.min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L2)); consider("nea", [a[0] + t * dx, a[1] + t * dy]);
    }
  }
  if (best) return { p: best.p, kind: best.kind };
  let q = p.slice();
  const base = st.tool?.base || (st.drag?.grip ? st.drag.from : null);           // 正交/极轴只相对于本命令已经取过的上一点；第一个点不受限
  if (base && (st.tool?.active || st.drag?.grip)) {
    if (dr.ortho) { if (Math.abs(q[0] - base[0]) >= Math.abs(q[1] - base[1])) q[1] = base[1]; else q[0] = base[0]; }
    else if (dr.polar) { const a = Math.atan2(q[1] - base[1], q[0] - base[0]), step = (dr.polar_angle || 15) * Math.PI / 180, r = dist(q, base), a2 = Math.round(a / step) * step; if (Math.abs(a - a2) < 4 * Math.PI / 180) q = [base[0] + r * Math.cos(a2), base[1] + r * Math.sin(a2)]; }
  }
  if (dr.snap) { const [sx, sy] = dr.snap_spacing; q = [Math.round(q[0] / sx) * sx, Math.round(q[1] / sy) * sy]; return { p: q, kind: "grid" }; }
  return { p: q, kind: null };
}

function drawOverlay() {
  const g = $("#overG"); g.replaceChildren(); const px = 1 / st.view.s;
  if (st.sc && st.space === "Model") {
    // 夹点
    const ids = [...st.sel].slice(0, 40);
    if (!st.tool?.active) for (const id of ids) { const e = entById(id); if (!e || e.locked) continue; for (const [idx, role, x, y] of e.gr || []) { const r = el("rect", { x: x - 4 * px, y: y - 4 * px, width: 8 * px, height: 8 * px, class: "grip", "data-eid": id, "data-gi": idx, "data-role": role, "data-x": x, "data-y": y }, g); r.style.strokeWidth = "calc(var(--px) * 1.4)"; } }
    if (st.drag?.grip) { const d = st.drag; const l = el("path", { d: `M${d.from[0]} ${d.from[1]}L${d.to[0]} ${d.to[1]}`, class: "rubber" }, g); l.style.strokeWidth = "1.2px"; }
    if (st.drag?.box) { const b = st.drag.box; const r = el("rect", { x: Math.min(b[0], b[2]), y: Math.min(b[1], b[3]), width: Math.abs(b[2] - b[0]), height: Math.abs(b[3] - b[1]), class: "winbox", fill: b[2] >= b[0] ? "#2458d622" : "#16a34a22", "stroke-dasharray": b[2] >= b[0] ? "none" : "5 4" }, g); r.style.strokeWidth = "1px"; }
    if (st.tool?.preview) st.tool.preview(g, st.cur);
    if (st.snap?.kind && st.snap.kind !== "grid") drawSnapMark(g, st.snap.kind, st.snap.p, px);
    if (st.snap?.p && st.tool?.active && st.tool.base && (st.sc.draft.ortho || st.sc.draft.polar)) { const l = el("path", { d: `M${st.tool.base[0]} ${st.tool.base[1]}L${st.snap.p[0]} ${st.snap.p[1]}`, class: "rubber" }, g); l.style.stroke = "#16a34a"; l.style.strokeDasharray = "2 4"; }
  }
}
function drawSnapMark(g, kind, p, px) {
  const r = 6 * px, [x, y] = p;
  if (kind === "end") el("rect", { x: x - r, y: y - r, width: 2 * r, height: 2 * r, class: "snapmk" }, g);
  else if (kind === "mid") el("path", { d: `M${x - r} ${y - r}L${x + r} ${y - r}L${x} ${y + r}Z`, class: "snapmk" }, g);
  else if (kind === "cen" || kind === "gce") el("circle", { cx: x, cy: y, r, class: "snapmk" }, g);
  else if (kind === "qua") el("path", { d: `M${x} ${y - r}L${x + r} ${y}L${x} ${y + r}L${x - r} ${y}Z`, class: "snapmk" }, g);
  else if (kind === "int") el("path", { d: `M${x - r} ${y - r}L${x + r} ${y + r}M${x - r} ${y + r}L${x + r} ${y - r}`, class: "snapmk" }, g);
  else el("path", { d: `M${x - r} ${y - r}L${x + r} ${y - r}L${x - r} ${y + r}L${x + r} ${y + r}Z`, class: "snapmk" }, g);
}

// ------------------------------------------------------------------ 选择
async function selectIds(ids, add = false) {
  const set = add ? new Set([...st.sel, ...ids]) : new Set(ids);
  st.sel = set; st.cand = new Set(); applySelClasses(); updateProps(); drawOverlay();
  if (st.id) api(`/api/drawings/${st.id}/ops`, { method: "POST", body: { tool: "select_entities", args: { selection_mode: "by_ids", entity_ids: [...set] } } }).catch(() => {});
}
function applySelClasses() { document.querySelectorAll("#entG .ent").forEach((g) => { const id = g.dataset.id; g.classList.toggle("sel", st.sel.has(id)); g.classList.toggle("cand", st.cand.has(id)); }); }
function boxSelect(b, crossing, add) {
  const x0 = Math.min(b[0], b[2]), x1 = Math.max(b[0], b[2]), y0 = Math.min(b[1], b[3]), y1 = Math.max(b[1], b[3]);
  const ids = visibleEnts().filter((e) => !e.locked && e.bb && (crossing ? !(e.bb[2] < x0 || e.bb[0] > x1 || e.bb[3] < y0 || e.bb[1] > y1) : e.bb[0] >= x0 && e.bb[2] <= x1 && e.bb[1] >= y0 && e.bb[3] <= y1)).map((e) => e.id);
  selectIds(ids, add);
}

// ------------------------------------------------------------------ 工具（点选取点 → 调用函数）
const TOOL_DEFS = [
  ["选择", [["select", "↖", "选择"]]],
  ["绘图", [["line", "╱", "直线"], ["pline", "⌇", "多段线"], ["rect", "▭", "矩形"], ["circle", "◯", "圆"], ["arc", "◠", "圆弧"], ["poly", "⬡", "多边形"], ["hatch", "▨", "填充"], ["text", "A", "文字"]]],
  ["修改", [["move", "✥", "移动"], ["copy", "⧉", "复制"], ["rotate", "⟳", "旋转"], ["scale", "⤡", "缩放"], ["mirror", "⇋", "镜像"], ["offset", "⫿", "偏移"], ["trim", "✂", "修剪"], ["fillet", "◜", "圆角"], ["erase", "⌫", "删除"], ["explode", "✺", "分解"]]],
  ["标注", [["dimlin", "↔", "线性"], ["dimrad", "R", "半径"], ["dimdia", "⌀", "直径"]]],
];
function buildTools() {
  const box = $("#tools"); box.replaceChildren();
  for (const [g, items] of TOOL_DEFS) {
    const h = document.createElement("div"); h.className = "grp"; h.textContent = g; box.appendChild(h);
    for (const [id, ic, name] of items) { const b = document.createElement("button"); b.type = "button"; b.dataset.tool = id; b.innerHTML = `<span class="ic">${ic}</span>${name}`; b.title = name; b.addEventListener("click", () => startTool(id)); box.appendChild(b); }
  }
}
function setToolButtons() { document.querySelectorAll("#tools button").forEach((b) => b.classList.toggle("on", b.dataset.tool === st.toolName)); }
function cancelTool(msg) { st.tool = null; st.toolName = "select"; setPrompt(); setToolButtons(); st.cur && drawOverlay(); stage.classList.toggle("cross", false); if (msg) smsg(msg); drawOverlay(); }

// 每个工具：prompt() 当前提示；click(p, ent)；text(s) 命令行输入；finish()；preview(g,cur)
function pts2(pts) { return pts.map((p) => (typeof p === "string" ? p : fpt(p))); }
function mkCollector(def) {
  const t = { name: def.name, active: true, pts: [], base: null, ...def };
  t.addPoint = (p) => { t.pts.push(p); t.base = typeof p === "string" ? t.base : p; };
  return t;
}
function startTool(name) {
  if (name === "select") return cancelTool();
  if (!st.id) { log("先新建或打开一张图纸", "err"); return; }
  const need = ["move", "copy", "rotate", "scale", "mirror", "erase", "explode"];
  if (need.includes(name) && !st.sel.size) { st.pendingTool = name; log(`先选择对象，再用「${TOOL_DEFS.flatMap((g) => g[1]).find((x) => x[0] === name)[2]}」（可以先框选）`, "warn"); }
  st.toolName = name; setToolButtons(); stage.classList.add("cross");
  const sel = () => [...st.sel];
  const T = {
    line: () => mkCollector({ name, prompt: () => (T_.pts.length ? "下一点 [C 闭合 / Enter 结束]" : "直线 起点"), click(p) { T_.addPoint(p); }, finish: async (close) => { if (T_.pts.length < 2) return cancelTool(); await op("draw_linear_primitives", { primitive_type: "line", points: pts2(T_.pts), close_loop: !!close }); cancelTool(); },
      preview(g, cur) { if (!cur || !T_.pts.length) return; const num = T_.pts.filter((p) => typeof p !== "string"); const pp = [...num, cur]; const l = el("path", { d: "M" + pp.map((p) => p.join(" ")).join("L"), class: "rubber" }, g); l.style.strokeWidth = "1.2px"; } }),
    pline: () => mkCollector({ name, prompt: () => (T_.pts.length ? "下一点 [C 闭合 / Enter 结束]" : "多段线 起点"), click(p) { T_.addPoint(p); }, finish: async (close) => { if (T_.pts.length < 2) return cancelTool(); await op("draw_complex_curves", { curve_type: "lwpolyline", vertices: pts2(T_.pts), closed: !!close }); cancelTool(); },
      preview(g, cur) { if (!cur || !T_.pts.length) return; const pp = [...T_.pts.filter((p) => typeof p !== "string"), cur]; el("path", { d: "M" + pp.map((p) => p.join(" ")).join("L"), class: "rubber" }, g).style.strokeWidth = "1.2px"; } }),
    rect: () => mkCollector({ name, prompt: () => (T_.pts.length ? "对角点（可输入 @长,宽）" : "矩形 第一个角点"), click(p) { T_.addPoint(p); if (T_.pts.length === 2) T_.finish(); }, finish: async () => { await op("draw_polygons_and_meshes", { shape_type: "rectangle", corner1: pts2(T_.pts)[0], corner2: pts2(T_.pts)[1] }); cancelTool(); },
      preview(g, cur) { if (!cur || !T_.pts.length || typeof T_.pts[0] === "string") return; const a = T_.pts[0]; el("rect", { x: Math.min(a[0], cur[0]), y: Math.min(a[1], cur[1]), width: Math.abs(cur[0] - a[0]), height: Math.abs(cur[1] - a[1]), class: "rubber" }, g).style.strokeWidth = "1.2px"; } }),
    circle: () => mkCollector({ name, prompt: () => (T_.pts.length ? "半径（点取或输入数值 / D 直径）" : "圆 圆心"), click(p) { if (!T_.pts.length) T_.addPoint(p); else { T_.radius = dist(T_.pts[0], p); T_.finish(); } },
      text(s) { if (/^\d+(\.\d+)?$/.test(s) && T_.pts.length) { T_.radius = +s; T_.finish(); return true; } return false; }, finish: async () => { await op("draw_circular_primitives", { curve_type: "circle", method: "center_radius", center: pts2(T_.pts)[0], radius: T_.radius }); cancelTool(); },
      preview(g, cur) { if (!cur || !T_.pts.length || typeof T_.pts[0] === "string") return; el("circle", { cx: T_.pts[0][0], cy: T_.pts[0][1], r: dist(T_.pts[0], cur), class: "rubber" }, g).style.strokeWidth = "1.2px"; } }),
    arc: () => mkCollector({ name, prompt: () => ["圆弧 起点", "第二点", "终点"][Math.min(T_.pts.length, 2)], click(p) { T_.addPoint(p); if (T_.pts.length === 3) T_.finish(); }, finish: async () => { await op("draw_circular_primitives", { curve_type: "arc", method: "3p", points: pts2(T_.pts) }); cancelTool(); } }),
    poly: () => mkCollector({ name, prompt: () => (T_.pts.length ? "半径（点取或输入）" : T_.sides ? "多边形 中心" : "边数（输入 3–1024）"), click(p) { if (!T_.sides) return; if (!T_.pts.length) T_.addPoint(p); else { T_.radius = dist(T_.pts[0], p); T_.finish(); } },
      text(s) { if (!T_.sides && /^\d+$/.test(s)) { T_.sides = +s; setPrompt("多边形 中心"); return true; } if (T_.pts.length && /^\d+(\.\d+)?$/.test(s)) { T_.radius = +s; T_.finish(); return true; } return false; }, finish: async () => { await op("draw_polygons_and_meshes", { shape_type: "polygon", num_sides: T_.sides, center: pts2(T_.pts)[0], radius: T_.radius, inscribed: true }); cancelTool(); } }),
    hatch: () => mkCollector({ name, prompt: () => "填充：点取封闭区域内部的一点", click(p) { T_.addPoint(p); T_.finish(); }, finish: async () => { await op("create_hatch", { boundary_mode: "pick_internal_point", pick_point: pts2(T_.pts)[0] }); cancelTool(); } }),
    text: () => mkCollector({ name, prompt: () => (T_.pts.length ? "输入文字内容" : "文字 插入点"), click(p) { if (!T_.pts.length) { T_.addPoint(p); setPrompt("输入文字内容，回车确认"); $("#cmdin").focus(); } },
      text(s) { if (T_.pts.length && s) { op("create_text_annotation", { text_type: "single_line", content: s, insert_point: pts2(T_.pts)[0] }).then(() => cancelTool()); return true; } return false; } }),
    move: () => mkCollector({ name, prompt: () => (T_.pts.length ? "目标点" : "移动 基点"), click(p) { T_.addPoint(p); if (T_.pts.length === 2) T_.finish(); }, finish: async () => { await op("transform_entities", { operation: "move", entity_ids: sel(), base_point: pts2(T_.pts)[0], target_point: pts2(T_.pts)[1] }); cancelTool(); },
      preview(g, cur) { if (cur && T_.pts.length && typeof T_.pts[0] !== "string") el("path", { d: `M${T_.pts[0][0]} ${T_.pts[0][1]}L${cur[0]} ${cur[1]}`, class: "rubber" }, g).style.strokeWidth = "1.2px"; } }),
    copy: () => mkCollector({ name, prompt: () => (T_.pts.length ? "目标点（Enter 结束）" : "复制 基点"), click(p) { T_.addPoint(p); if (T_.pts.length >= 2) { op("replicate_entities", { operation: "copy", entity_ids: sel(), base_point: pts2(T_.pts)[0], target_points: [pts2(T_.pts).slice(-1)[0]] }).then(() => { T_.pts = [T_.pts[0]]; }); } }, finish: async () => cancelTool() }),
    rotate: () => mkCollector({ name, prompt: () => (T_.pts.length ? "旋转角度（输入数值，或点取）" : "旋转 基点"), click(p) { if (!T_.pts.length) T_.addPoint(p); else { const a = Math.atan2(p[1] - T_.pts[0][1], p[0] - T_.pts[0][0]) * 180 / Math.PI; T_.angle = +a.toFixed(3); T_.finish(); } },
      text(s) { if (T_.pts.length && /^-?\d+(\.\d+)?$/.test(s)) { T_.angle = +s; T_.finish(); return true; } return false; }, finish: async () => { await op("transform_entities", { operation: "rotate", entity_ids: sel(), base_point: pts2(T_.pts)[0], rotation_angle_deg: T_.angle }); cancelTool(); } }),
    scale: () => mkCollector({ name, prompt: () => (T_.pts.length ? "比例因子（输入数值）" : "缩放 基点"), click(p) { if (!T_.pts.length) T_.addPoint(p); },
      text(s) { if (T_.pts.length && /^\d+(\.\d+)?$/.test(s) && +s > 0) { op("transform_entities", { operation: "scale", entity_ids: sel(), base_point: pts2(T_.pts)[0], scale_factor: +s }).then(() => cancelTool()); return true; } return false; } }),
    mirror: () => mkCollector({ name, prompt: () => (T_.pts.length ? "镜像线 第二点" : "镜像线 第一点"), click(p) { T_.addPoint(p); if (T_.pts.length === 2) T_.finish(); }, finish: async () => { await op("replicate_entities", { operation: "mirror", entity_ids: sel(), mirror_line: pts2(T_.pts) }); cancelTool(); } }),
    offset: () => mkCollector({ name, prompt: () => (T_.dist == null ? "偏移距离（输入数值）" : T_.target ? "点取偏移的一侧" : "点选要偏移的对象"), click(p, ent) { if (T_.dist == null) return; if (!T_.target) { if (ent) { T_.target = ent.id; setPrompt(this.prompt()); } } else { op("replicate_entities", { operation: "offset", entity_ids: [T_.target], offset_distance: T_.dist, offset_side_point: fpt(p) }).then(() => { T_.target = null; }); } },
      text(s) { if (T_.dist == null && /^\d+(\.\d+)?$/.test(s) && +s > 0) { T_.dist = +s; setPrompt("点选要偏移的对象"); return true; } return false; } }),
    trim: () => mkCollector({ name, prompt: () => "点选要修剪的线段（以其他对象为边界）", click(p, ent) { if (ent) op("adjust_line_lengths", { operation: "trim", target_entities: [{ entity_id: ent.id, pick_point: fpt(p) }] }); } }),
    fillet: () => mkCollector({ name, prompt: () => (T_.r == null ? "圆角半径（输入数值）" : T_.ids.length ? "点选第二个对象" : "点选第一个对象"), ids: [], click(p, ent) { if (T_.r == null || !ent) return; T_.ids.push(ent.id); if (T_.ids.length === 2) { op("modify_corners_and_breaks", { operation: "fillet", entity_id_1: T_.ids[0], entity_id_2: T_.ids[1], fillet_radius: T_.r }).then(() => { T_.ids = []; }); } else setPrompt(this.prompt()); },
      text(s) { if (T_.r == null && /^\d+(\.\d+)?$/.test(s)) { T_.r = +s; setPrompt("点选第一个对象"); return true; } return false; } }),
    erase: () => ({ name, active: true, run: async () => { await op("erase_entities", { entity_ids: sel(), confirmed: sel().length > 20 }); selectIds([]); cancelTool(); } }),
    explode: () => ({ name, active: true, run: async () => { await op("explode_compound_entities", { entity_ids: sel() }); selectIds([]); cancelTool(); } }),
    dimlin: () => mkCollector({ name, prompt: () => ["线性标注 第一条界线起点", "第二条界线起点", "尺寸线位置"][Math.min(T_.pts.length, 2)], click(p) { T_.addPoint(p); if (T_.pts.length === 3) T_.finish(); }, finish: async () => { await op("create_linear_and_angular_dim", { dim_type: "linear_auto", ext_line1_point: pts2(T_.pts)[0], ext_line2_point: pts2(T_.pts)[1], dim_line_location: pts2(T_.pts)[2] }); cancelTool(); } }),
    dimrad: () => mkCollector({ name, prompt: () => "点选圆或圆弧", click(p, ent) { if (ent) op("create_radial_and_chain_dim", { dim_type: "radius", target_entity_id: ent.id }); } }),
    dimdia: () => mkCollector({ name, prompt: () => "点选圆或圆弧", click(p, ent) { if (ent) op("create_radial_and_chain_dim", { dim_type: "diameter", target_entity_id: ent.id }); } }),
  };
  let T_ = null; T_ = T[name]();
  st.tool = T_; if (T_.run) { if (st.sel.size) T_.run(); else { st.toolName = "select"; st.tool = null; setToolButtons(); } return; }
  if (need.includes(name) && !st.sel.size) { st.tool = null; return setToolButtons(); }
  setPrompt(T_.prompt()); $("#cmdin").focus();
}
st.startTool = startTool;
function toolClick(p, ent) {
  const t = st.tool; if (!t) return false;
  t.click && t.click(p, ent); setPrompt(t.prompt ? t.prompt() : ""); drawOverlay(); return true;
}

// ------------------------------------------------------------------ 鼠标与键盘
let hoverId = null;
svg.addEventListener("mousemove", (ev) => {
  if (!st.sc) return;
  const raw = evPt(ev);
  if (st.panning) { st.view.cx -= (ev.movementX) / st.view.s; st.view.cy += (ev.movementY) / st.view.s; applyView(); return; }
  const sn = computeSnap(raw); st.snap = sn; st.cur = sn.p;
  $("#coord").textContent = `X: ${sn.p[0].toFixed(3)}  Y: ${sn.p[1].toFixed(3)}`;
  const tip = $("#tip");
  if (sn.kind && sn.kind !== "grid") { const r = svg.getBoundingClientRect(); const [sx, sy] = m2s(...sn.p); tip.textContent = SNAP_CN[sn.kind]; tip.style.left = sx + 12 + "px"; tip.style.top = sy + 10 + r.top * 0 + "px"; tip.hidden = false; } else tip.hidden = true;
  if (st.drag) {
    if (st.drag.grip) st.drag.to = sn.p; else if (st.drag.box) st.drag.box[2] = raw[0], st.drag.box[3] = raw[1];
  } else if (!st.tool) {
    const e = ev.target.closest && ev.target.closest(".ent"); const id = e ? e.dataset.id : null;
    if (id !== hoverId) { document.querySelectorAll("#entG .ent.hov").forEach((x) => x.classList.remove("hov")); if (e) e.classList.add("hov"); hoverId = id; }
  }
  drawOverlay();
});
svg.addEventListener("mouseleave", () => { $("#tip").hidden = true; });
svg.addEventListener("mousedown", (ev) => {
  if (!st.sc) return; stage.focus();
  if (ev.button === 1 || (ev.button === 0 && st.spaceDown)) { ev.preventDefault(); const now = Date.now(); if (ev.button === 1 && now - st.lastMid < 350) zoomExtents(); st.lastMid = now; st.panning = true; stage.classList.add("pan"); return; }
  if (ev.button === 2) { ev.preventDefault(); if (st.tool) { if (st.tool.finish && st.tool.pts?.length) st.tool.finish(); else cancelTool(); } return; }
  if (ev.button !== 0) return;
  const raw = evPt(ev), sn = computeSnap(raw), p = sn.p;
  const entEl = ev.target.closest && ev.target.closest(".ent"); const ent = entEl ? entById(entEl.dataset.id) : null;
  const grip = ev.target.classList && ev.target.classList.contains("grip") ? ev.target : null;
  if (grip && !st.tool) { st.drag = { grip: { id: grip.dataset.eid, idx: +grip.dataset.gi, role: grip.dataset.role }, from: [+grip.dataset.x, +grip.dataset.y], to: [+grip.dataset.x, +grip.dataset.y] }; return; }
  if (st.tool) { if (st.tool.click) toolClick(p, ent); return; }
  if (st.cand.size && ent && st.cand.has(ent.id)) { answerPick(ent.id); return; }          // 识别反问：点图作答
  if (ent) { if (ev.ctrlKey || ev.metaKey) { const s = new Set(st.sel); s.has(ent.id) ? s.delete(ent.id) : s.add(ent.id); selectIds([...s]); } else if (ev.shiftKey) selectIds([ent.id], true); else selectIds([ent.id]); return; }
  st.drag = { box: [raw[0], raw[1], raw[0], raw[1]], add: ev.shiftKey };
});
window.addEventListener("mouseup", async (ev) => {
  if (st.panning) { st.panning = null; stage.classList.remove("pan"); saveViewSoon(); return; }
  const d = st.drag; if (!d) return; st.drag = null;
  if (d.grip) {
    if (dist(d.from, d.to) > 1e-6 / st.view.s) { const mode = ["mid", "center"].includes(d.grip.role) ? "move" : "stretch"; await op("grip_edit_entity", { entity_id: d.grip.id, grip_type_or_index: d.grip.idx, grip_mode: mode, new_position: fpt(d.to) }); }
    drawOverlay(); return;
  }
  if (d.box) {
    const [x0, y0, x1, y1] = d.box; const moved = Math.hypot((x1 - x0) * st.view.s, (y1 - y0) * st.view.s) > 4;
    if (moved) boxSelect(d.box, x1 < x0, d.add); else if (!d.add) selectIds([]);
    drawOverlay();
  }
});
svg.addEventListener("contextmenu", (e) => e.preventDefault());
svg.addEventListener("wheel", (ev) => { ev.preventDefault(); const r = svg.getBoundingClientRect(); zoomAt(ev.deltaY < 0 ? 1.15 : 1 / 1.15, ev.clientX - r.left, ev.clientY - r.top); }, { passive: false });
svg.addEventListener("dblclick", (ev) => { if (!st.tool && !(ev.target.closest && ev.target.closest(".ent"))) zoomExtents(); });
document.addEventListener("keydown", (ev) => {
  const typing = ["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName);
  if (ev.key === " " && !typing) { st.spaceDown = true; stage.classList.add("pan"); ev.preventDefault(); }
  if (ev.key === "Escape") { cancelTool(); st.cand = new Set(); selectIds([]); $("#cmdin").blur(); }
  if (typing && document.activeElement.id === "cmdin") return;
  if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "z") { ev.preventDefault(); doUndo(); }
  else if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "y") { ev.preventDefault(); doRedo(); }
  else if (ev.key === "Delete" && !typing) { if (st.sel.size) op("erase_entities", { entity_ids: [...st.sel], confirmed: st.sel.size > 20 }).then(() => selectIds([])); }
  else if (ev.key === "Enter" && st.tool && st.tool.finish && !typing) st.tool.finish();
  else if (ev.key === "F8") { ev.preventDefault(); toggleFlag("ortho"); } else if (ev.key === "F9") { ev.preventDefault(); toggleFlag("snap"); } else if (ev.key === "F7") { ev.preventDefault(); toggleFlag("grid"); }
  else if (ev.key === "F3") { ev.preventDefault(); toggleFlag("osnap"); } else if (ev.key === "F10") { ev.preventDefault(); toggleFlag("polar"); } else if (ev.key === "F12") { ev.preventDefault(); toggleFlag("dyn"); }
  else if (!typing && !ev.ctrlKey && !ev.metaKey && ev.key.length === 1 && /[a-z]/i.test(ev.key)) { $("#cmdin").focus(); }
});
document.addEventListener("keyup", (ev) => { if (ev.key === " ") { st.spaceDown = false; stage.classList.remove("pan"); } });
async function doUndo() { await op("history_undo_redo", { action: "undo" }); } async function doRedo() { await op("history_undo_redo", { action: "redo" }); }
$("#btnUndo").addEventListener("click", doUndo); $("#btnRedo").addEventListener("click", doRedo);
$("#zIn").addEventListener("click", () => (sheetOn() ? sheetZoomAt(1.4, st.W / 2, st.H / 2) : zoomAt(1.4, st.W / 2, st.H / 2)));
$("#zOut").addEventListener("click", () => (sheetOn() ? sheetZoomAt(1 / 1.4, st.W / 2, st.H / 2) : zoomAt(1 / 1.4, st.W / 2, st.H / 2)));
$("#zExt").addEventListener("click", () => (sheetOn() ? sheetFit() : zoomExtents()));

// ------------------------------------------------------------------ 状态栏开关
const FLAG_OPS = {
  snap: (on) => op("configure_drafting_modes", { snap_on: on }, { quiet: true }), grid: (on) => op("configure_drafting_modes", { grid_on: on }, { quiet: true }), ortho: (on) => op("configure_drafting_modes", { ortho_on: on }, { quiet: true }),
  polar: (on) => op("configure_drafting_modes", { polar_on: on }, { quiet: true }), osnap: (on) => op("configure_osnap_and_otracking", { osnap_on: on }, { quiet: true }), otrack: (on) => op("configure_osnap_and_otracking", { otrack_on: on }, { quiet: true }),
  dyn: (on) => op("configure_dynamic_input_and_coords", { dynmode_on: on }, { quiet: true }),
};
function flagVal(f) { const d = st.sc?.draft; if (f === "lw") return st.lw; return d ? d[f] : false; }
function renderFlags() { document.querySelectorAll("#status button[data-flag]").forEach((b) => b.classList.toggle("on", !!flagVal(b.dataset.flag))); }
async function toggleFlag(f) { if (f === "lw") { st.lw = !st.lw; renderAll(); renderFlags(); return; } if (!st.sc) return; await FLAG_OPS[f](!flagVal(f)); }
document.querySelectorAll("#status button[data-flag]").forEach((b) => b.addEventListener("click", () => toggleFlag(b.dataset.flag)));

// ------------------------------------------------------------------ 面板：图层 / 特性 / 信息
const LTYPES = ["Continuous", "DASHED", "HIDDEN", "CENTER", "DASHDOT", "DOT", "PHANTOM", "DIVIDE", "BORDER"];
const LWS = [0.13, 0.18, 0.25, 0.35, 0.5, 0.7, 1.0, 1.4];
function hexOf(c) { return /^#[0-9a-f]{6}$/i.test(c) ? c : c === "fg" ? "#000000" : "#000000"; }
function renderLayers() {
  const sc = st.sc, p = $("#pane-layers"); if (!sc) { p.innerHTML = '<div class="empty">未打开图纸</div>'; return; }
  p.innerHTML = `<div style="display:flex;gap:6px;margin-bottom:6px"><button class="btn" id="lyNew" type="button">＋ 新建图层</button><span class="sub" style="align-self:center">当前：${esc(sc.current_layer)}</span></div>
  <table class="layers"><thead><tr><th>名称</th><th>开</th><th>冻</th><th>锁</th><th>色</th><th>线型</th><th>线宽</th></tr></thead><tbody>${sc.layers.map((l, i) => `<tr class="${l.current ? "cur" : ""}" data-i="${i}">
    <td class="nm" title="点击设为当前层：${esc(l.name)}（${l.count} 个图元）">${esc(l.name)}</td>
    <td><button class="ic ${l.on ? "" : "off"}" data-a="on" type="button">${l.on ? "👁" : "◌"}</button></td><td><button class="ic ${l.frozen ? "" : "off"}" data-a="frz" type="button">${l.frozen ? "❄" : "☀"}</button></td>
    <td><button class="ic ${l.locked ? "" : "off"}" data-a="lck" type="button">${l.locked ? "🔒" : "🔓"}</button></td>
    <td><span class="swatch" style="background:${l.color === "fg" ? fgColor() : l.color}"><input type="color" data-a="col" value="${hexOf(l.color)}"/></span></td>
    <td><select data-a="lt">${LTYPES.map((t) => `<option ${t.toLowerCase() === String(l.linetype).toLowerCase() ? "selected" : ""}>${t}</option>`).join("")}</select></td>
    <td><select data-a="lw">${LWS.map((w) => `<option ${Math.abs(w - (l.lineweight_mm || 0.25)) < 1e-6 ? "selected" : ""}>${w}</option>`).join("")}</select></td></tr>`).join("")}</tbody></table>`;
  p.querySelector("#lyNew").onclick = async () => { const n = prompt("新图层名称："); if (n) await op("manage_layers", { action: "create", layer_name: n.trim() }); };
  p.querySelectorAll("tbody tr").forEach((tr) => {
    const l = sc.layers[+tr.dataset.i];
    tr.querySelector(".nm").onclick = () => op("set_layer_state", { layer_name: l.name, set_current: true });
    tr.querySelectorAll("[data-a]").forEach((x) => {
      const a = x.dataset.a;
      if (a === "on") x.onclick = () => op("set_layer_state", { layer_name: l.name, is_on: !l.on });
      else if (a === "frz") x.onclick = () => op("set_layer_state", { layer_name: l.name, is_frozen: !l.frozen });
      else if (a === "lck") x.onclick = () => op("set_layer_state", { layer_name: l.name, is_locked: !l.locked });
      else if (a === "col") x.onchange = () => op("set_layer_properties", { layer_name: l.name, color: x.value });
      else if (a === "lt") x.onchange = () => op("set_layer_properties", { layer_name: l.name, linetype: x.value });
      else if (a === "lw") x.onchange = () => op("set_layer_properties", { layer_name: l.name, lineweight_mm: +x.value });
    });
  });
}
let propTimer = null;
function updateProps() { clearTimeout(propTimer); propTimer = setTimeout(loadProps, 120); }
async function loadProps() {
  const p = $("#pane-props"); const ids = [...st.sel].filter((i) => entById(i));
  if (!st.id || !ids.length) { p.innerHTML = '<div class="empty">选中图元后在这里查看和修改特性<br/><span class="sub">（点选、框选，或用命令行）</span></div>'; st.props = null; return; }
  const r = await api(`/api/drawings/${st.id}/ops`, { method: "POST", body: { tool: "modify_entity_properties", args: { action: "get", entity_ids: ids } } }).catch(() => null);
  if (!r || !r.ok) { p.innerHTML = `<div class="empty">${esc(r?.message || "读取特性失败")}</div>`; return; }
  const cp = r.data.common_props, gp = r.data.geometry_props || {}, sc = st.sc;
  const layerSel = `<select data-k="layer">${r.data.common_props.layer === "多种" ? "<option>多种</option>" : ""}${sc.layers.map((l) => `<option ${l.name === cp.layer ? "selected" : ""}>${esc(l.name)}</option>`).join("")}</select>`;
  const colV = cp.color === null ? "随层" : cp.color;
  let geo = "";
  const edit = (k, label, v, type = "number") => `<div class="row"><label>${label}</label><input data-g="${k}" type="${type}" step="any" value="${esc(v)}"/></div>`;
  if (ids.length === 1) {
    const e = entById(ids[0]);
    if (e.k === "circle") geo = edit("radius", "半径", gp.radius) + edit("diameter", "直径", gp.diameter) + `<div class="row"><label>圆心</label><span class="sub">${(gp.center || []).slice(0, 2).map((x) => (+x).toFixed(3)).join(", ")}</span></div>` + `<div class="sub">面积 ${gp.area ? gp.area.toFixed(3) : ""}</div>`;
    else if (e.k === "arc") geo = edit("radius", "半径", gp.radius) + `<div class="sub">${gp.start_angle}° → ${gp.end_angle}°，弧长 ${(gp.length || 0).toFixed(3)}</div>`;
    else if (e.k === "text" || e.k === "mtext") geo = edit("content", "内容", gp.text, "text") + edit("height", "字高", gp.height);
    else if (e.k === "line") geo = `<div class="sub">长度 ${(gp.length || 0).toFixed(3)}，角度 ${(gp.angle || 0).toFixed(2)}°<br/>(${(gp.p1 || []).slice(0, 2).map((x) => (+x).toFixed(3))}) → (${(gp.p2 || []).slice(0, 2).map((x) => (+x).toFixed(3))})</div>`;
    else if (e.k === "polyline") geo = `<div class="sub">${gp.vertices} 个顶点，${gp.closed ? "闭合" : "开放"}，长度 ${(gp.length || 0).toFixed(3)}${gp.area ? "，面积 " + gp.area.toFixed(3) : ""}</div>`;
  }
  p.innerHTML = `<h4>${ids.length === 1 ? ids[0] + "（" + r.data.types[0] + "）" : `${ids.length} 个对象（${r.data.types.join("、")}）`}</h4>
    <div class="row"><label>图层</label>${layerSel}</div>
    <div class="row"><label>颜色</label><input data-k="color" type="color" value="${hexOf(typeof colV === "string" ? colV : "#000000")}"/><button class="btn" data-k="bylayer" type="button">随层</button><span class="sub">${esc(colV)}</span></div>
    <div class="row"><label>线型</label><select data-k="linetype"><option value="">随层</option>${LTYPES.map((t) => `<option ${String(cp.linetype || "").toLowerCase() === t.toLowerCase() ? "selected" : ""}>${t}</option>`).join("")}</select></div>
    <div class="row"><label>线宽</label><select data-k="lineweight_mm"><option value="">随层</option>${LWS.map((w) => `<option ${cp.lineweight_mm === w ? "selected" : ""}>${w}</option>`).join("")}</select></div>
    <div class="row"><label>线型比例</label><input data-k="linetype_scale" type="number" step="any" value="${cp.linetype_scale === "多种" ? "" : cp.linetype_scale}"/></div>
    ${geo ? "<h4>几何</h4>" + geo : ""}`;
  const set = (common, geometry) => op("modify_entity_properties", { action: "set", entity_ids: ids, ...(common ? { common_props: common } : {}), ...(geometry ? { geometry_props: geometry } : {}) });
  p.querySelectorAll("[data-k]").forEach((x) => {
    const k = x.dataset.k;
    if (k === "bylayer") x.onclick = () => set({ color: "ByLayer" });
    else x.onchange = () => { let v = x.value; if (k === "lineweight_mm" || k === "linetype_scale") v = v === "" ? null : +v; if (k === "linetype" && v === "") v = "ByLayer"; if (k === "lineweight_mm" && v === null) return; set({ [k]: v }); };
  });
  p.querySelectorAll("[data-g]").forEach((x) => { x.onchange = () => set(null, { [x.dataset.g]: x.type === "number" ? +x.value : x.value }); });
}
function renderInfo() {
  const sc = st.sc, p = $("#pane-info"); if (!sc) { p.innerHTML = '<div class="empty">未打开图纸</div>'; return; }
  const kinds = {}; sc.entities.forEach((e) => (kinds[e.k] = (kinds[e.k] || 0) + 1));
  p.innerHTML = `<div class="props"><h4>图纸</h4><div class="sub">${esc(sc.title)} · ${esc(sc.drawing_id)} · 第 ${sc.revision} 版<br/>界限 (${sc.limits.map((x) => +x.toFixed(1)).join(", ")}) · 线型比例 ${sc.ltscale}<br/>文字样式 ${esc(sc.current_text_style)} · 标注样式 ${esc(sc.current_dim_style)}</div>
  <h4>当前空间图元（${sc.entities.length}）</h4><div class="sub">${Object.entries(kinds).map(([k, v]) => `${k} ×${v}`).join("　") || "无"}</div>
  <h4>块</h4><div class="sub">${sc.blocks.join("、") || "无"}</div><h4>布局</h4><div class="sub">${sc.layouts.map((l) => `${esc(l.name)}（${l.viewports.length} 个视口）`).join("、") || "无"}</div>
  <h4>约束（${sc.constraints.length}）</h4><div class="sub">${sc.constraints.map((c) => esc(c.id + " " + (c.category === "dimensional" ? c.dim.name + "=" + (c.dim.expr ?? c.dim.value) : c.type + " " + c.refs.join(" ")))).join("<br/>") || "无"}</div>
  <h4>撤销栈</h4><div class="sub">可撤销 ${sc.session.undo} 步，可重做 ${sc.session.redo} 步</div></div>`;
}
document.querySelectorAll("#stabs button").forEach((b) => b.addEventListener("click", () => switchPane(b.dataset.p)));
function switchPane(n) { document.querySelectorAll("#stabs button").forEach((x) => x.classList.toggle("on", x.dataset.p === n)); ["vessel", "chat", "layers", "props", "info"].forEach((p) => ($("#pane-" + p).hidden = p !== n)); if (n === "props") loadProps(); }

// ------------------------------------------------------------------ 空间标签与三维视图
function renderTabs() {
  const sc = st.sc, b = $("#spaceTabs"); b.replaceChildren();
  if (PFD.arts.length) { const x = document.createElement("button"); x.type = "button"; x.textContent = "工艺流程图"; x.title = "最近一次生成的工艺流程图（对话里说流程图就会更新）"; x.classList.toggle("on", !!st.pfdOn); x.onclick = () => pfdShow(true); b.appendChild(x); }
  if (!sc) return;
  if (st.pfdOn) { const add = (label, fn) => { const x = document.createElement("button"); x.type = "button"; x.textContent = label; x.onclick = fn; b.appendChild(x); }; add(st.ws === "equip" ? "设备图" : "图纸", () => pfdShow(false)); return; }
  if (st.ws === "equip") return renderEqTabs(b);
  const mk = (name, label) => { const x = document.createElement("button"); x.type = "button"; x.textContent = label; x.classList.toggle("on", st.space === name); x.onclick = async () => { await op("switch_model_and_layout_space", { target_space: name }, { quiet: true }); }; b.appendChild(x); };
  mk("Model", "模型"); sc.layouts.forEach((l) => mk(l.name, l.name));
}
const VIEWS = [["top", "俯"], ["front", "前"], ["left", "左"], ["right", "右"], ["sw_isometric", "西南"], ["se_isometric", "东南"], ["ne_isometric", "东北"], ["nw_isometric", "西北"]];
const VSTYLES = [["2d_wireframe", "二维线框"], ["wireframe", "线框"], ["hidden", "消隐"], ["shaded", "着色"], ["shaded_with_edges", "带边着色"]];
function show3d() {
  const sc = st.sc, on = sc && st.space === "Model" && (sc.view.view3d !== "top" || sc.view.visual_style !== "2d_wireframe") && sc.entities.some((e) => e.k === "solid3d" || e.k === "mesh");
  $("#img3d").hidden = !on; svg.style.visibility = on ? "hidden" : "visible"; $("#v3d").hidden = !(sc && (on || sc.workspace === "3d_modeling")); $("#viewctl").hidden = on;
  if (sc) {
    const v = $("#v3d"); v.replaceChildren();
    VIEWS.forEach(([k, n]) => { const b = document.createElement("button"); b.type = "button"; b.textContent = n; b.classList.toggle("on", sc.view.view3d === (k === "top" ? "top" : k.replace("_isometric", "_iso"))); b.onclick = () => op("switch_3d_view_and_visual_style", { standard_view: k }); v.appendChild(b); });
    const s = document.createElement("select"); VSTYLES.forEach(([k, n]) => { const o = document.createElement("option"); o.value = k; o.textContent = n; o.selected = sc.view.visual_style === k; s.appendChild(o); }); s.onchange = () => op("switch_3d_view_and_visual_style", { visual_style: s.value }); v.appendChild(s);
  }
  if (on) $("#img3d").src = `/api/drawings/${st.id}/render?fmt=png&_=${sc.revision}-${sc.view.view3d}-${sc.view.visual_style}`;
}

// ------------------------------------------------------------------ 命令行 / 对话 / 语音
const cmdin = $("#cmdin");
cmdin.addEventListener("keydown", (ev) => { if (ev.key === "Enter") { ev.preventDefault(); sendCmd(); } else if (ev.key === "ArrowUp") { histNav(-1); } else if (ev.key === "ArrowDown") { histNav(1); } });
$("#btnSend").addEventListener("click", sendCmd);
const hist = []; let hi = 0;
function histNav(d) { if (!hist.length) return; hi = Math.max(0, Math.min(hist.length, hi + d)); cmdin.value = hist[hi] || ""; }
async function sendCmd() {
  const text = cmdin.value.trim(); if (!text) { if (st.tool?.finish) st.tool.finish(); return; }
  cmdin.value = ""; hist.push(text); hi = hist.length;
  log("› " + text, "me");
  if (st.tool?.text && st.tool.text(text)) return;
  if (/^(c|close|闭合)$/i.test(text) && st.tool?.finish) { st.tool.finish(true); return; }
  if (st.tool?.name && st.tool.pts && /^[@\d.,<-]+$/.test(text) && !st.tool.text) { st.tool.click(text); setPrompt(st.tool.prompt()); drawOverlay(); return; }   // 键入坐标当作点取
  if (st.pickAnswer) { const t = st.pickAnswer; st.pickAnswer = null; await replyTask(t, text); return; }
  cancelTool();
  if (st.pfdOn) {                                     // 正在看流程图：这句话多半是接着改流程图；路由说不是，就回到图纸继续
    const rt = await routeOf(text, "pfd");
    if (!rt || rt.mode === "pfd") { try { await chat(text, rt || { mode: "pfd" }); } catch (e) { log(e.message, "err"); } return; }
    pfdShow(false);
    if (rt.mode === "equipment") { try { await chat(text, rt); } catch (e) { log(e.message, "err"); } return; }
  }
  if (!st.id) {                                       // 还没有图纸：先问路由，要设备图/流程图就不先建一张机械图
    const rt = await routeOf(text);
    if (rt && (rt.mode === "pfd" || rt.mode === "equipment")) { try { await chat(text, rt); } catch (e) { log(e.message, "err"); } return; }
    await newDrawing("gb_a3_mech", "");
  }
  try {
    const r = await api(`/api/drawings/${st.id}/command`, { method: "POST", body: { text } });
    if (r.handled) {
      if (r.help) { log(r.help); return; }
      if (!r.ok) { log(r.message || r.result?.message || "命令没有执行", "err"); if (r.hint || r.result?.hint) log("提示：" + (r.hint || r.result.hint), "warn"); return; }
      log(r.summary + (r.result?.summary && r.result.summary !== r.summary ? "：" + r.result.summary : ""), "ok"); (r.result?.warnings || []).forEach((w) => log("注意：" + w, "warn"));
      if (r.result?.drawing_id && r.result.drawing_id !== st.id) await openDrawing(r.result.drawing_id); else await refresh(true);
      return;
    }
    await chat(text);
  } catch (e) { log(e.message, "err"); }
}
function addMsg(html, cls = "") { const d = document.createElement("div"); d.className = "msgb " + cls; d.innerHTML = html; const lg = $("#chatlog"); const em = lg.querySelector(".empty"); if (em) em.remove(); lg.appendChild(d); $("#pane-chat").scrollTop = $("#pane-chat").scrollHeight; return d; }
async function routeOf(text, context) {                 // 这句话该走流程图 / 设备图 / 通用绘图；路由失败就当通用绘图处理
  try { return await api("/api/route", { method: "POST", body: { prompt: text, drawing_id: context === "pfd" ? null : st.id, context: context || null } }); } catch { return null; }
}
async function chat(text, rt0) {
  switchPane("chat"); addMsg(esc(text), "me"); st.lastPrompt = text;
  const h = await checkLLM();
  if (h.key_missing) { addMsg("<span class=\"err\">还没有接入大模型</span>：自然语言口令需要大模型。请在弹出的「设置」里填写 API Key，保存后再发一次。", "q"); ZhShell.openSettings(); return; }
  const rt = rt0 || await routeOf(text);
  if (rt && rt.mode === "pfd") { await pfdSend(text); return; }                // 流程图：同一个对话框里提交，结果显示在中间（当前图纸不会被改动）
  if (rt && rt.mode === "equipment") {
    if (!eqTarget(rt)) {                                  // 当前图不是设备图：先新建一张化工设备图再发口令，绝不画进用户的草图里
      const id = await newEquipDrawing("");
      if (!id) return;
      addMsg("这句话是在要化工设备图，已新建一张「化工设备图」并切换过去；你原来的图纸没有被改动。");
    } else if (st.ws !== "equip") setWs("equip");
  }
  if (!st.id) await newDrawing("gb_a3_mech", "");
  const r = await api(`/api/drawings/${st.id}/chat`, { method: "POST", body: { prompt: text } });
  startPoll(r.id);
}
async function replyTask(tid, answer) { addMsg(esc(answer), "me"); await api(`/api/tasks/${tid}/reply`, { method: "POST", body: { answer } }); if (tid === PFD.task) { st.busy = true; pfdProgress("已收到，继续处理…"); pfdPoll(); } else startPoll(tid, true); }
async function answerPick(id) { if (st.task) { st.cand = new Set(); applySelClasses(); log("› " + id, "me"); const t = st.task; st.pickAnswer = null; await replyTask(t, id); } }
let pollTimer = null;
function startPoll(tid, resume = false) {
  st.task = tid; if (!resume) { st.taskAfter = 0; } st.planSteps = []; st.planBox = null; clearTimeout(pollTimer); st.busy = true; smsg("智能体处理中…"); syncVesselLock();
  const tick = async () => {
    try {
      const t = await api(`/api/tasks/${tid}?after=${st.taskAfter}`);
      for (const ev of t.events) { st.taskAfter = ev.id; await onEvent(ev); }
      if (["READY", "READY_WITH_WARNINGS", "FAILED", "WAITING_CONFIRMATION", "CANCELLED"].includes(t.status)) { await onDone(t); return; }
    } catch (e) { log(e.message, "err"); st.busy = false; syncVesselLock(); return; }
    pollTimer = setTimeout(tick, 350);
  };
  tick();
}
function planBox() {
  if (st.planBox && document.body.contains(st.planBox)) return st.planBox;
  st.planBox = addMsg("", "plan"); st.planBox.className = "plan"; st.planBox.style.maxWidth = "100%"; return st.planBox;
}
function drawPlan() {
  const b = planBox(); b.innerHTML = `<b>计划</b> <span class="sub">${esc(st.planThought || "")}</span>` + st.planSteps.map((s) => `<div class="st ${s.state}"><i>${{ run: "●", ok: "✓", bad: "✗", wait: "○" }[s.state === "run" ? "run" : s.state === "ok" ? "ok" : s.state === "bad" ? "bad" : "wait"]}</i><span>${esc(s.step_id)} ${esc(s.summary)}</span></div>${s.log ? `<div class="log">${esc(s.log)}</div>` : ""}`).join("");
  $("#pane-chat").scrollTop = $("#pane-chat").scrollHeight;
}
async function onEvent(ev) {
  const k = ev.kind;
  if (k === "normalized") log(`识别为：${ev.normalized}`);
  else if (k === "plan") { st.planThought = ev.preview || ev.thought; st.planSteps = ev.steps.map((s) => ({ ...s, state: "wait", log: "" })); drawPlan(); }
  else if (k === "step_start") { const s = st.planSteps.find((x) => x.step_id === ev.step_id); if (s) { s.state = "run"; drawPlan(); } }
  else if (k === "recognize") { const s = st.planSteps.find((x) => x.step_id === ev.step_id); if (s) { s.state = ev.status === "resolved" ? "ok" : "bad"; s.log = ev.message || ""; drawPlan(); } if (ev.candidates && ev.status === "ambiguous") { st.cand = new Set(ev.candidates); applySelClasses(); } log(ev.message || "", ev.status === "resolved" ? "" : "warn"); }
  else if (k === "tool_result") { const s = st.planSteps.find((x) => x.step_id === ev.step_id); if (s) { s.state = "ok"; s.log = ev.summary || ""; drawPlan(); } if (ev.drawing_id && ev.drawing_id !== st.id) await openDrawing(ev.drawing_id); else await refresh(true); }
  else if (k === "step_failed") { const s = st.planSteps.find((x) => x.step_id === ev.step_id); if (s) { s.state = "bad"; s.log = ev.error?.message || ""; drawPlan(); } }
  else if (k === "drawing") { if (ev.drawing_id !== st.id) await openDrawing(ev.drawing_id); }
  else if (k === "switch") {                                                   // 引擎让转工作空间：用户在反问里选了“工艺流程图”→ 原话交给“化工流程图”
    const p = ev.payload || ev;
    if (p.mode === "pfd") {
      let text = p.prompt || st.lastPrompt || "";
      // 用户已经明确选了“工艺流程图”，但原话（如“画一个立式储罐”）到了流程图页还会被入口路由当成设备图再弹回来：原话里没有流程图字样就补上，免得来回转
      if (text && !/流程|PFD|P&ID|管道/i.test(text)) text += "，要工艺流程图";
      addMsg("按你的选择，按工艺流程图来画（当前图纸不会被改动）。"); pfdSend(text, true);
    }
  }
  else if (k === "clarify") { /* 在 onDone 里展示 */ }
}
// 设备图反问带 items（待问项）：每项给几个可点的例子，点一下把“项目 取值”填进输入框，改好再回车作答
function fillAnswer(it, opt) {
  const short = String(it.label || "").replace(/[（(][^）)]*[）)]/g, "").replace(/\s*DN$/i, "").trim();
  const piece = it.path === "drawing_type" || !short || !/^(DN)?[\d.]+$/i.test(opt.trim()) ? opt : `${short} ${opt}`;      // 只有一个数（1600、DN25）才要带上项目名，“切线长 3000”“耳式支座”这类自己就说清了
  cmdin.value = (cmdin.value.trim() ? cmdin.value.trim().replace(/[，,；;]*$/, "，") : "") + piece; cmdin.focus();
}
function renderQuestionItems(d, items, tid) {
  const box = document.createElement("div"); box.className = "qitems";
  items.forEach((it) => {
    const row = document.createElement("div"); row.className = "qi";
    const lb = document.createElement("span"); lb.textContent = it.label || it.path; row.appendChild(lb);
    (it.options || []).slice(0, 4).forEach((opt) => {
      const b = document.createElement("button"); b.type = "button"; b.textContent = opt.length > 30 ? opt.slice(0, 28) + "…" : opt; b.title = opt;
      b.onclick = () => {
        if (it.path === "drawing_type" && /流程图/.test(opt) && !/设备图|装配图/.test(opt)) { st.pickAnswer = null; replyTask(tid, opt); return; }   // 选工艺流程图：直接作答，转去流程图
        fillAnswer(it, opt);
      };
      row.appendChild(b);
    });
    box.appendChild(row);
  });
  const tip = document.createElement("div"); tip.className = "sub"; tip.textContent = "点例子会填进下面的输入框，可以改，回车一次答完。"; box.appendChild(tip);
  d.appendChild(box);
}
async function onDone(t) {
  st.busy = false; syncVesselLock(); smsg(""); const r = t.result || {};
  if (r.drawing_id && r.drawing_id !== st.id) await openDrawing(r.drawing_id); else await refresh(true);
  if (t.status === "WAITING_CONFIRMATION") {
    const q = r.question || {}; st.cand = new Set(q.candidates || []); applySelClasses();
    const d = addMsg(esc(q.question || r.message), "q");
    if ((q.options || []).length) { const o = document.createElement("div"); o.className = "opts"; q.options.forEach((opt) => { const b = document.createElement("button"); b.type = "button"; b.textContent = opt.length > 36 ? opt.slice(0, 34) + "…" : opt; b.title = opt; b.onclick = () => { st.pickAnswer = null; replyTask(t.id, opt); }; o.appendChild(b); }); d.appendChild(o); }
    else if ((q.items || []).length) renderQuestionItems(d, q.items, t.id);
    if ((q.candidates || []).length) { const s = document.createElement("div"); s.className = "sub"; s.textContent = "已在图上用红色高亮候选，可以直接点图作答。"; d.appendChild(s); }
    st.pickAnswer = t.id; setPrompt("请回答："); log(q.question || r.message, "warn");
  } else {
    st.pickAnswer = null; setPrompt();
    const cls = t.status === "FAILED" ? "err" : t.status === "READY_WITH_WARNINGS" ? "warn" : "ok";
    addMsg(`<span class="${cls}">${esc(r.message || t.status)}</span>`); log(r.message || t.status, cls);
    if (r.validation && r.validation.errors?.length) log("校验问题：" + r.validation.errors.map((e) => e.message).join("；"), "warn");
  }
}

// ---- 语音输入（Web Speech API，说完先填进输入框，可修改再发送）----
const Rec = window.SpeechRecognition || window.webkitSpeechRecognition; let rec = null, recOn = false, asrLocal = false;
$("#btnMic").addEventListener("click", () => {
  if (recOn) { rec && rec.stop(); return; }
  if (!Rec) { asrLocal ? recordLocal() : log("这个浏览器不支持语音识别（建议用 Chrome / Edge），请改用文字输入。", "warn"); return; }
  rec = new Rec(); rec.lang = "zh-CN"; rec.continuous = true; rec.interimResults = true; let finalT = "";
  rec.onstart = () => { recOn = true; $("#btnMic").classList.add("rec"); smsg("正在听…"); finalT = ""; };
  rec.onresult = (e) => { let interim = ""; for (let i = e.resultIndex; i < e.results.length; i++) { const t = e.results[i][0].transcript; if (e.results[i].isFinal) finalT += t; else interim += t; } cmdin.value = finalT + interim; };
  rec.onerror = (e) => { log("语音识别出错：" + e.error + (e.error === "not-allowed" ? "（浏览器没有麦克风权限）" : ""), "err"); };
  rec.onend = () => { recOn = false; $("#btnMic").classList.remove("rec"); smsg(""); cmdin.focus(); if ($("#autoSend").checked && cmdin.value.trim()) sendCmd(); };
  rec.start();
});
async function recordLocal() {
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true }); const mr = new MediaRecorder(stream); const chunks = []; recOn = true; $("#btnMic").classList.add("rec"); smsg("正在录音，再点一次结束");
    mr.ondataavailable = (e) => chunks.push(e.data);
    mr.onstop = async () => { stream.getTracks().forEach((t) => t.stop()); recOn = false; $("#btnMic").classList.remove("rec"); smsg("识别中…"); const fd = new FormData(); fd.append("file", new Blob(chunks, { type: "audio/webm" }), "a.webm");
      try { const r = await fetch("/api/asr", { method: "POST", body: fd }); const j = await r.json(); if (!r.ok) throw new Error(j.detail); cmdin.value = j.text; if ($("#autoSend").checked) sendCmd(); } catch (e) { log("本地识别失败：" + e.message, "err"); } smsg(""); };
    rec = { stop: () => mr.stop() }; mr.start();
  } catch (e) { log("无法使用麦克风：" + e.message, "err"); }
}

// ------------------------------------------------------------------ 图纸管理
async function listDrawings(selectId) {
  const j = await api("/api/drawings"); const sel = $("#dwgSel"); sel.replaceChildren();
  const add = (v, t) => { const o = document.createElement("option"); o.value = v; o.textContent = t; sel.appendChild(o); };
  if (!j.items.length) add("", "（还没有图纸）"); j.items.forEach((d) => add(d.drawing_id, `${d.title || "通用图"} · ${d.drawing_id.slice(4, 10)} · ${d.entities} 个图元`));
  if (selectId && !j.items.some((d) => d.drawing_id === selectId)) add(selectId, selectId);
  sel.value = selectId || (j.items[0] && j.items[0].drawing_id) || ""; return j.items;
}
async function openDrawing(id) {
  if (st.pfdOn) pfdShow(false);
  st.id = id; st.rev = -1; st.firstLoad = null; st.sel = new Set(); st.cand = new Set(); localStorage.setItem("zhihui.cad.last", id); location.hash = id;
  st.vessel = null; st.vesselErr = ""; st.vTok++; st.eqView = null; st.eqViewUser = false; st.autoDone = null; st.vpMsg = null; st.sheetKey = ""; st.vaDraft = { purpose: "", dn: "", loc: "" };     // 换图：设备图相关状态全部作废
  await listDrawings(id); await refresh(true);
}
async function newDrawing(tpl, title) { const r = await api("/api/drawings", { method: "POST", body: { template_name: tpl, title } }); await openDrawing(r.drawing_id); log(`已新建图纸（样板 ${tpl}）`, "ok"); return r.drawing_id; }
$("#dwgSel").addEventListener("change", (e) => e.target.value && openDrawing(e.target.value));
const TPLS = [["hg_equipment_a1", "化工设备图 A1（装配图、管口表、明细栏、标题栏；参数由口令生成）"], ["gb_a3_mech", "国标 A3 机械图（图层、线型、标注样式齐全）"], ["gb_a4_mech", "国标 A4 机械图"], ["gb_a2_mech", "国标 A2 机械图"], ["gb_a3_arch", "国标 A3 建筑图"], ["iso_a3", "ISO A3"], ["blank_a3", "空白 A3"], ["blank_a4", "空白 A4"], ["blank_a2", "空白 A2"]];
$("#tplSel").innerHTML = TPLS.map(([v, t]) => `<option value="${v}">${t}</option>`).join("");
$("#btnNew").addEventListener("click", () => { $("#tplSel").value = st.ws === "equip" ? "hg_equipment_a1" : "gb_a3_mech"; $("#dlgNew").showModal(); });
$("#dlgNew").addEventListener("close", async (e) => { if ($("#dlgNew").returnValue === "ok") await newDrawing($("#tplSel").value, $("#tplTitle").value.trim()); });
// 新建一张化工设备图（样板 hg_equipment_a1）并切到“化工设备图”工作空间；失败不回退去用别的图（设备绝不画进用户的草图）
async function newEquipDrawing(title) {
  try {
    const r = await api("/api/drawings", { method: "POST", body: { template_name: "hg_equipment_a1", title: title || "" } });
    await openDrawing(r.drawing_id); setWs("equip"); log("已新建化工设备图（样板 hg_equipment_a1）", "ok");
    return r.drawing_id;
  } catch (e) { addMsg(`<span class="err">新建化工设备图失败</span>：${esc(e.message)}。你原来的图纸没有被改动。`, "q"); log("新建化工设备图失败：" + e.message, "err"); return null; }
}
// ------------------------------------------------------------------ 化工流程图：同一个对话框里提交，结果显示在中间（不再是单独的工作空间）
const PFD_STAGE = {
  router: "正在理解你的需求", knowledge: "正在查找相似工艺", symbol: "图元库里没有，正在检索并生成示意图元", planner: "正在规划设备与连接",
  capability: "正在检查能画哪些内容", confirmation: "等你确认方案", builder: "正在绘制流程图", editor: "正在按你的要求修改", layout_fixer: "正在整理排版",
  reviewer: "正在逐项核对你的要求", repairer: "正在修复发现的问题", export: "正在导出 DXF / PNG / PDF", reporter: "正在生成交付报告",
};
const PFD = { task: null, after: 0, ready: null, arts: [], prompt: "", msg: null, timer: null };
const pfdNew = (t) => /^\s*(请)?(帮我)?(画|绘制|生成|新建|做一张)/.test(t) || (t.match(/→/g) || []).length >= 2;     // “画一个…/A → B → C” 是新图，不是改上一版
function pfdProgress(text) {
  if (!PFD.msg || !document.body.contains(PFD.msg)) PFD.msg = addMsg("", "plan");
  PFD.msg.innerHTML = `<b>化工流程图</b> <span class="sub">${esc(text)}</span>`;
}
async function pfdSend(text, fromSwitch = false) {
  switchPane("chat"); if (!fromSwitch) { /* 原话已经在 chat() 里显示过 */ }
  const fd = new FormData(); fd.append("prompt", text);
  const cont = !!PFD.ready && st.pfdOn && !pfdNew(text);
  if (cont) fd.append("parent_id", PFD.ready);
  let j;
  try {
    const r = await fetch("/api/tasks", { method: "POST", body: fd }); j = await r.json();
    if (!r.ok) throw new Error(j.detail || r.status);
  } catch (e) { addMsg(`<span class="err">流程图任务提交失败</span>：${esc(e.message)}`, "q"); return; }
  if (j.mode !== "pfd") { startPoll(j.id); return; }                        // 服务器按通用绘图 / 设备图处理：走通用的进度显示
  PFD.task = j.id; PFD.after = 0; PFD.prompt = text; PFD.msg = null; st.busy = true; smsg("正在画流程图…");
  pfdProgress(cont ? "在上一版流程图上修改，排队中…" : "排队中…"); pfdPoll();
}
async function pfdPoll() {
  clearTimeout(PFD.timer); const tid = PFD.task; if (!tid) return;
  let t;
  try { t = await api(`/api/tasks/${tid}?after=${PFD.after}`); } catch { PFD.timer = setTimeout(pfdPoll, 1500); return; }
  for (const ev of t.events) {
    PFD.after = ev.id; const stg = ev.role || ev.stage;
    if (ev.kind === "stage" && PFD_STAGE[stg]) pfdProgress(PFD_STAGE[stg]);
    else if (ev.kind === "status" && ev.status === "REPAIRING") pfdProgress("核对发现问题，正在自动修复…");
    else if (ev.kind === "assistant" && ev.text) addMsg(esc(ev.text));
    else if (ev.kind === "switch" && (ev.mode === "equipment" || ev.handoff === "equipment")) {   // 流程图链路认出是设备图请求：原话改走设备图入口
      clearTimeout(PFD.timer); PFD.task = null; st.busy = false; smsg(""); if (PFD.msg) PFD.msg.remove();
      addMsg("这句话是在要化工设备图，改按设备图处理…"); pfdShow(false); chat(ev.prompt || PFD.prompt, { mode: "equipment" }).catch((e) => log(e.message, "err")); return;
    }
  }
  if (["READY", "READY_WITH_WARNINGS", "FAILED", "CANCELLED", "WAITING_CONFIRMATION"].includes(t.status) && (t.result || t.status === "CANCELLED")) { pfdDone(t); return; }
  PFD.timer = setTimeout(pfdPoll, 700);
}
function pfdLinks(arts) {
  return ["dxf", "pdf", "png"].map((f) => arts.find((a) => a.format === f)).filter(Boolean).map((a) => `<a href="${a.url}" target="_blank" download>${a.format.toUpperCase()}</a>`).join(" · ");
}
function pfdDone(t) {
  st.busy = false; smsg(""); if (PFD.msg) { PFD.msg.remove(); PFD.msg = null; }
  const r = t.result || {}; const arts = r.artifacts || [];
  if (arts.some((a) => a.format === "png" || a.format === "svg")) { PFD.arts = arts; pfdShow(true); }
  if (t.status === "FAILED" || t.status === "CANCELLED") { addMsg(`<span class="err">流程图没能完成</span>：${esc(r.message || t.status)}<div class="sub">原图没有被改动，可以换个说法再试。</div>`, "q"); return; }
  if (t.status === "WAITING_CONFIRMATION") {
    const q = r.question || {}, opts = (q.options || []).filter((o) => !(r.spec && o === "确认当前方案"));
    let html = r.spec
      ? `<b>流程图方案</b>：${r.spec.equipment.length} 台设备、${(r.spec.streams || []).length} 条连接。${esc(q.question || "")}<div class="opts"><button type="button" data-ok>确认方案，开始画</button><button type="button" data-edit>逐项核对方案</button>${opts.map((o, i) => `<button type="button" data-o="${i}">${esc(o)}</button>`).join("")}</div>`
      : `${esc(q.question || r.message || "需要你确认")}<div class="opts">${opts.map((o, i) => `<button type="button" data-o="${i}">${esc(o)}</button>`).join("")}</div><div class="sub">也可以直接在下面输入回答。</div>`;
    const d = addMsg(html, "q");
    d.querySelector("[data-ok]")?.addEventListener("click", async () => {
      d.querySelectorAll("button").forEach((x) => (x.disabled = true));
      try { await api(`/api/tasks/${t.id}/confirm`, { method: "POST", body: r.spec }); pfdProgress("已确认方案，正在绘制…"); st.busy = true; pfdPoll(); } catch (e) { addMsg(`<span class="err">确认失败</span>：${esc(e.message)}`, "q"); }
    });
    d.querySelector("[data-edit]")?.addEventListener("click", () => window.open(`index.html#/task/${t.id}`, "_blank"));     // 设备、连接逐项修改用流程图的方案编辑器（新标签页）
    d.querySelectorAll("[data-o]").forEach((b) => b.addEventListener("click", () => { d.querySelectorAll("button").forEach((x) => (x.disabled = true)); st.pickAnswer = null; replyTask(t.id, opts[+b.dataset.o]); }));
    st.pickAnswer = t.id; setPrompt("请回答："); return;
  }
  PFD.ready = t.id; st.pickAnswer = null; setPrompt();
  const v = r.validation || {}, facts = [];
  if (v.stats) facts.push(`设备 ${v.stats.equipment} 台 · 连接 ${v.stats.connections} 条`);
  if (v.passed !== undefined) facts.push(v.passed ? "图面检查通过" : "图面检查有问题");
  addMsg(`<span class="${t.status === "READY" ? "ok" : "warn"}">工艺流程图已生成</span>${facts.length ? "（" + esc(facts.join("，")) + "）" : ""}。${esc((r.message || "").slice(0, 160))}<div class="sub">下载：${pfdLinks(arts) || "无"}。接着说就能改，比如“把 P-101 向右移动 800”。</div>`);
}
// 中间区域显示流程图（和图纸二选一；上方标签可以来回切）
function pfdShow(on) {
  st.pfdOn = !!on && PFD.arts.length > 0;
  document.body.classList.toggle("pfdview", st.pfdOn);
  $("#pfdWrap").hidden = !st.pfdOn;
  if (st.pfdOn) {
    const a = PFD.arts.find((x) => x.format === "svg") || PFD.arts.find((x) => x.format === "png");
    if (a && $("#imgPfd").dataset.u !== a.url) { $("#imgPfd").src = a.url; $("#imgPfd").dataset.u = a.url; }
    $("#pfdMsg").innerHTML = `化工流程图 · ${esc(PFD.prompt.slice(0, 40))}${PFD.prompt.length > 40 ? "…" : ""} · ${pfdLinks(PFD.arts)}`;
  }
  renderTabs();
  if (!st.pfdOn) applyEqUi();
}
document.querySelectorAll("#wsSub button").forEach((b) => b.addEventListener("click", () => {
  pfdShow(false); setWs("general"); op("manage_workspace_ui", { workspace: b.dataset.ws }, { quiet: true });
}));
$("#btnExport").addEventListener("click", async () => {
  if (!st.id) return;
  if (st.ws === "equip" && eqHasVessel()) { exportVessel(); return; }       // 化工设备图：导出 DXF 和 PDF（PDF 从布局「设备图」出图）
  try { const r = await api(`/api/drawings/${st.id}/export`, { method: "POST", body: { formats: ["dxf", "pdf", "png"] } });
    const d = addMsg("已导出：" + r.files.map((f) => `<a href="${f.url}" target="_blank">${f.format.toUpperCase()}</a>`).join(" · ")); switchPane("chat"); log("已导出 " + r.files.map((f) => f.format).join("、"), "ok"); if (r.warnings?.length) log("注意：" + r.warnings.map((w) => w.message).join("；"), "warn"); } catch (e) { log("导出失败：" + e.message, "err"); }
});
$("#btnHelp").addEventListener("click", async () => {
  const r = await api(`/api/drawings/${st.id || "dwg_none"}/command`, { method: "POST", body: { text: "help" } }).catch(() => ({ help: "命令帮助需要先打开一张图纸。" }));
  $("#helpBody").textContent = "一个对话框说就行：要工艺流程图就出流程图（显示在中间，上方“工艺流程图”标签可以和图纸来回切）；要设备图就新建一张化工设备图（右侧「设备参数」里管口表可直接改）；其余是通用绘图（左边工具、命令行、图层、特性）。\n化工设备图口令举例：画一台立式储罐的设备图，内径1600，筒体高2400…；把进料口转到90度；筒体加长500；上封头再加一个DN50的放空口；出一张管口方位图；导出 DXF 和 PDF。\n设备图里的标准数据（封头、法兰、人孔、支座等）目前按公开资料录入，尚未与标准原文逐项核对，图上和面板里都标了“未核对”，只能用于方案阶段。\n\n" + (r.help || "") + "\n\n快捷键：F3 对象捕捉 · F7 栅格 · F8 正交 · F9 捕捉 · F10 极轴 · F12 动态输入 · 空格+拖动 / 中键拖动 平移 · 滚轮缩放 · 双击空白 全部显示 · Del 删除 · Ctrl+Z / Ctrl+Y 撤销重做 · Esc 取消\n选择：从左往右拖是窗选（完全在框内），从右往左拖是交叉选（碰到就选）；选中后出现夹点，拖动夹点就是拉伸。\n语音：点 🎤 说话，识别结果先填进输入框，改好再发送；勾上“说完自动发送”就不用再点。";
  $("#dlgHelp").showModal();
});
const checkLLM = () => api("/api/health").then((h) => { st.llm = h; const b = $("#health"); b.textContent = h.llm === "mock" ? "测试模式（未接入大模型）" : h.key_missing ? "未接入大模型：请点 ⚙ 设置" : h.model; b.style.color = (h.llm === "mock" || h.key_missing) ? "#e0a000" : ""; return h; }).catch(() => ({}));
window.addEventListener("zhsettings:saved", checkLLM);
api("/api/health").then((h) => { st.llm = h; if (h.key_missing) log("还没有接入大模型：自然语言口令需要大模型，请点右上角「⚙ 设置」填写 API Key（AutoCAD 命令如 L 0,0 @100,0 不受影响）。", "warn"); $("#health").textContent = h.llm === "mock" ? "测试模式（未接入大模型）" : h.key_missing ? "未接入大模型：请点 ⚙ 设置" : h.model; $("#health").title = `agent=${h.agent_mode} mcp=${h.mcp}`; }).catch(() => { $("#health").textContent = "未连接"; });
api("/api/asr/status").then((a) => { asrLocal = !!a.available; }).catch(() => {});

// ------------------------------------------------------------------ 工作空间切换（化工流程图 / 化工设备图 / 通用绘图）
const EQ_LAYOUT = "设备图", EQ_ORI = "管口方位图";
const EX_VERT = "画一台立式储罐的设备图，内径1600，筒体高2400，标准椭圆封头，壁厚8，材料Q345R，设计压力0.6MPa，设计温度80℃，人孔DN500，进料口DN80，出料口DN80，两个液位计口DN25，四个耳式支座";
const EX_HORZ = "画一个卧式储罐装配图，内径2000，切线长6000，壁厚10，两个鞍座，管口有进料DN100、出料DN100、人孔DN500";
function hasLayout(n) { return !!st.sc && st.sc.layouts.some((l) => l.name === n); }
function isEquipScene(sc) { return !!sc && (sc.workspace === "chem_equipment" || sc.layouts.some((l) => l.name === EQ_LAYOUT)); }       // 化工设备图样板，或已经出过布局「设备图」
function eqHasVessel() { return !!st.id && (!!st.vessel || hasLayout(EQ_LAYOUT)); }
// 设备口令能不能直接发给当前这张图：已有设备参数，或者是刚新建、还没画任何东西的设备图样板；否则要先新建一张
function eqTarget(rt) { return !!st.id && ((rt && rt.has_vessel === true) || eqHasVessel() || (!!st.sc && st.sc.workspace === "chem_equipment" && (st.sc.entity_count || 0) === 0)); }
function sheetOn() { return st.ws === "equip" && !$("#sheetWrap").hidden; }
function syncWsButtons() {
  document.querySelectorAll("#wsSeg button").forEach((b) => b.classList.toggle("on", b.dataset.ws === st.ws));
  const sw = st.sc && st.sc.workspace;
  document.querySelectorAll("#wsSub button").forEach((b) => b.classList.toggle("on", st.ws === "general" && b.dataset.ws === sw));
  $("#stabs button[data-p=vessel]").hidden = st.ws !== "equip";
}
function setWs(ws) {
  const was = st.ws; st.ws = ws; if (ws !== "pfd") st.prevWs = ws;
  document.body.classList.toggle("equip", ws === "equip");
  syncWsButtons();
  if (ws !== "equip" && !$("#pane-vessel").hidden) switchPane("chat");
  renderTabs(); applyEqUi();
  if (ws === "equip" && was !== "equip") { renderVessel(); if (!st.busy && eqHasVessel()) switchPane("vessel"); }
}
// 换了一张图（打开 / 新建 / 任务里新建）：设备图自动进“化工设备图”，在“化工设备图”里打开了别的图就回“通用绘图”；每张图只自动切一次
function autoWs() {
  if (!st.sc || st.autoDone === st.id) return;
  st.autoDone = st.id;
  if (isEquipScene(st.sc) && st.ws === "general") setWs("equip");
  else if (!isEquipScene(st.sc) && st.ws === "equip") setWs("general");
}
function afterScene() {
  autoWs(); syncWsButtons();
  if (isEquipScene(st.sc)) loadVessel(); else if (st.vessel) { st.vessel = null; renderVessel(); }
  applyEqUi();
}
document.addEventListener("click", (e) => { const b = e.target.closest && e.target.closest("[data-ex]"); if (!b) return; cmdin.value = b.dataset.ex; cmdin.focus(); });     // 例子口令：填进输入框，不自动发送

// ---- 预览：布局出图（服务器渲染）/ 模型空间（SVG）
function eqViews() {
  const names = (st.sc ? st.sc.layouts : []).map((l) => l.name), first = [EQ_LAYOUT, EQ_ORI].filter((n) => names.includes(n));
  return [...first, ...names.filter((n) => !first.includes(n))].map((n) => ({ key: n, label: n })).concat([{ key: "Model", label: "模型空间" }]);
}
function eqView() {
  const keys = eqViews().map((v) => v.key);
  if (st.eqViewUser && keys.includes(st.eqView)) return st.eqView;
  return keys.includes(EQ_LAYOUT) ? EQ_LAYOUT : "Model";
}
function renderEqTabs(b) {
  if (!eqHasVessel()) return;
  eqViews().forEach((v) => {
    const x = document.createElement("button"); x.type = "button"; x.textContent = v.label; x.classList.toggle("on", eqView() === v.key);
    x.title = v.key === "Model" ? "模型空间：各视图按 1:1 画在一起，可点选、拖夹点（改生成的图元会让它脱离参数，建议改右边的参数）" : `布局「${v.key}」的出图预览（服务器渲染，和导出的 PDF 一致）`;
    x.onclick = () => pickEqView(v.key); b.appendChild(x);
  });
}
async function pickEqView(key) {
  st.eqView = key; st.eqViewUser = true;
  if (key === "Model" && st.space !== "Model") { await op("switch_model_and_layout_space", { target_space: "Model" }, { quiet: true }); return; }     // 模型空间要画 SVG，当前空间得是 Model
  renderTabs(); applyEqUi();
}
function applyEqUi() {
  const eq = st.ws === "equip", has = eq && !!st.sc && eqHasVessel(), empty = eq && !has;
  $("#equipEmpty").hidden = !empty; if (empty) renderEquipEmpty();
  const sheet = has && eqView() !== "Model";
  $("#sheetWrap").hidden = !sheet;
  show3d();                                                                  // 先恢复通用视图对 SVG / 视图按钮的设置，再按设备图的需要覆盖
  if (sheet || empty) svg.style.visibility = "hidden";
  if (empty) $("#viewctl").hidden = true;
  if (sheet) { if (sv.fit) sheetFit(); loadSheetImage(); }
  updateHud(); renderVesselTab();
}
const sv = { s: 1, x: 0, y: 0, nw: 0, nh: 0, fit: true };
const swrap = $("#sheetWrap"), simg = $("#imgSheet");
function sheetApply() { simg.style.transform = `translate(${sv.x}px, ${sv.y}px) scale(${sv.s})`; }
function sheetFit() {
  const W = swrap.clientWidth, H = swrap.clientHeight; if (!sv.nw || W < 10 || H < 10) return;
  sv.s = Math.min((W - 24) / sv.nw, (H - 24) / sv.nh); sv.x = (W - sv.nw * sv.s) / 2; sv.y = (H - sv.nh * sv.s) / 2; sv.fit = true; sheetApply();
}
function sheetZoomAt(f, px, py) { if (!sv.nw) return; const s0 = sv.s; sv.s = Math.min(Math.max(s0 * f, 0.02), 24); const k = sv.s / s0; sv.x = px - (px - sv.x) * k; sv.y = py - (py - sv.y) * k; sv.fit = false; sheetApply(); }
function sheetOnResize() { if (sv.fit && !swrap.hidden) sheetFit(); }
swrap.addEventListener("pointerdown", (e) => { if (e.button > 1) return; swrap.setPointerCapture(e.pointerId); swrap._d = { x: e.clientX, y: e.clientY, ox: sv.x, oy: sv.y }; swrap.classList.add("drag"); });
swrap.addEventListener("pointermove", (e) => { const d = swrap._d; if (!d) return; sv.x = d.ox + e.clientX - d.x; sv.y = d.oy + e.clientY - d.y; sv.fit = false; sheetApply(); });
const sheetUp = () => { swrap._d = null; swrap.classList.remove("drag"); };
swrap.addEventListener("pointerup", sheetUp); swrap.addEventListener("pointercancel", sheetUp);
swrap.addEventListener("wheel", (e) => { e.preventDefault(); const r = swrap.getBoundingClientRect(); sheetZoomAt(e.deltaY < 0 ? 1.15 : 1 / 1.15, e.clientX - r.left, e.clientY - r.top); }, { passive: false });
swrap.addEventListener("dblclick", sheetFit);
async function loadSheetImage() {
  const name = eqView(), id = st.id, rev = st.sc && st.sc.revision, key = `${id}|${name}|${rev}`;
  if (st.sheetKey === key) return;
  st.sheetKey = key; const tok = ++st.sheetTok, msg = $("#sheetMsg"); msg.textContent = "正在渲染布局预览…";
  try {
    const r = await fetch(`/api/drawings/${id}/render?fmt=png&space=${encodeURIComponent(name)}&_=${rev}`);
    if (!r.ok) { let m = ""; try { const j = await r.json(); m = j.detail || j.message || ""; } catch { /* 非 JSON */ } throw new Error(m || `HTTP ${r.status}`); }
    const blob = await r.blob(); if (tok !== st.sheetTok) return;
    const url = URL.createObjectURL(blob), old = simg.dataset.blob, same = st.sheetFor === `${id}|${name}`;
    simg.onload = () => { sv.nw = simg.naturalWidth; sv.nh = simg.naturalHeight; if (!same || sv.fit) sheetFit(); else sheetApply(); st.sheetFor = `${id}|${name}`; msg.textContent = ""; };
    simg.src = url; simg.dataset.blob = url; if (old) URL.revokeObjectURL(old);
  } catch (e) { if (tok === st.sheetTok) { st.sheetKey = ""; msg.textContent = "布局预览渲染失败：" + e.message; } }
}
function renderEquipEmpty() {
  const box = $("#equipEmpty"), fresh = !!st.id && !!st.sc && st.sc.workspace === "chem_equipment", key = fresh ? "fresh" : st.id ? "other" : "none";
  if (box.dataset.k === key) return;
  box.dataset.k = key;
  const msg = fresh ? "这张图还没有设备参数。在下面的输入框里说出设备参数（点例子会填进输入框），系统按参数生成主视图、管口方位图、管口表、设计数据表、明细栏和标题栏。"
    : st.id ? "当前图纸不是化工设备图。在下面直接说要画什么设备，系统会新建一张设备图，你现在的图纸不会被改动。" : "还没有图纸。在下面直接说要画什么设备，系统会新建一张设备图。";
  box.innerHTML = `<div class="card"><h3>化工设备图</h3><div class="sub">${msg}</div>
    <div class="examples"><button type="button" data-ex="${esc(EX_VERT)}"><b>立式储罐设备图</b><span>参数齐全，直接出装配图</span></button><button type="button" data-ex="${esc(EX_HORZ)}"><b>卧式储罐装配图</b><span>鞍座、切线长</span></button><button type="button" data-ex="画一个立式储罐"><b>只说设备名</b><span>系统一次问清图种和必填参数</span></button></div>
    ${fresh ? "" : '<div class="row"><button class="btn accent" id="eqNew" type="button">＋ 新建化工设备图</button></div>'}</div>`;
  const nb = box.querySelector("#eqNew"); if (nb) nb.onclick = () => newEquipDrawing("");
}

// ------------------------------------------------------------------ “设备参数”面板（P0：参数只读，管口表可改；改动直接调 vessel_define，不经过大模型）
const LOC_CN = { shell: "筒体", top_head: "上封头", bottom_head: "下封头", left_head: "左封头", right_head: "右封头" };
const KIND_CN = { vertical_vessel: "立式容器", horizontal_vessel: "卧式容器", hx_fixed_tubesheet: "固定管板换热器", stirred_reactor_jacketed: "夹套搅拌反应釜", tray_column: "板式塔", packed_column: "填料塔", hx_floating_head: "浮头换热器", hx_u_tube: "U 形管换热器" };
const NKIND_CN = { pipe: "接管", manhole: "人孔", handhole: "手孔", sight_glass: "视镜", level_gauge: "液位计口" };
const SUP_CN = { lug: "耳式支座", leg: "支腿", skirt: "裙座", saddle: "鞍座", ring: "圈座" };
const SRC_CN = { user: ["用户", "s-user"], rule: ["默认", "s-rule"], table: ["查表", "s-table"], calc: ["计算", "s-calc"], pfd: ["流程图", "s-user"] };
const FGROUPS = [["基本", /^(kind|tag|name)$/], ["筒体", /^shell\./], ["封头", /^heads/], ["支座", /^supports/], ["设计数据", /^design\./], ["图纸", /^drawing\./]];
const PURPOSES = ["进料口", "出料口", "放空口", "排净口", "液位计口", "人孔", "手孔", "安全阀口", "压力表口", "温度计口", "回流口", "备用口", "视镜"];
const fmtVal = (v) => (v === null || v === undefined || v === "" ? "待填" : typeof v === "number" ? String(+v.toFixed(4)) : typeof v === "boolean" ? (v ? "是" : "否") : typeof v === "object" ? JSON.stringify(v) : String(v));
const isEmpty = (v) => v === null || v === undefined || v === "";
const srcKind = (s) => String(s || "").split(":")[0];
const kindOf = (t) => (/人孔/.test(t) ? "manhole" : /手孔/.test(t) ? "handhole" : /视镜|视孔/.test(t) ? "sight_glass" : /液位计|液面计/.test(t) ? "level_gauge" : null);     // 和 Planner 的翻译规则一致：人孔/手孔/视镜/液位计 → kind
function srcBadge(src, value, unverified) {                  // user→用户、rule→默认、table→查表、calc→计算、pending/空值→待填；unverified 另加“未核对”
  let h = "";
  const k = srcKind(src);
  if (isEmpty(value) || k === "pending") h += '<span class="badge-src s-pending">待填</span>';
  else if (SRC_CN[k]) h += `<span class="badge-src ${SRC_CN[k][1]}" title="${esc(src)}">${SRC_CN[k][0]}</span>`;
  if (unverified) h += '<span class="badge-src s-unverified" title="这一项来自尚未与标准原文核对的数据，只能用于方案阶段">未核对</span>';
  return h;
}

async function loadVessel() {
  const id = st.id, tok = ++st.vTok;
  try {
    const j = await api(`/api/drawings/${id}/vessel`);
    if (tok !== st.vTok || id !== st.id) return;
    st.vessel = j && j.vessel !== undefined ? j.vessel : (j && j.kind ? j : null); st.vesselErr = "";
  } catch (e) { if (tok !== st.vTok || id !== st.id) return; st.vessel = null; st.vesselErr = e.message; }      // 接口没好或出错：面板显示“暂无参数”，不影响其他功能
  renderVessel(); renderTabs(); applyEqUi();
}
function renderVesselTab() {
  const t = $("#stabs button[data-p=vessel]"), v = st.vessel, nPend = v && v.pending ? v.pending.length : 0, nErr = v && v.check && v.check.errors ? v.check.errors.length : 0;
  t.textContent = "设备参数" + (nPend ? `·缺${nPend}项` : nErr ? `·${nErr}错` : "");
}
function setVpMsg(cls, html) { st.vpMsg = html ? { cls, html } : null; const m = $("#vpMsg"); if (m) paintVpMsg(m); }
function paintVpMsg(m) { m.innerHTML = st.vpMsg ? `<div class="vp-msg ${st.vpMsg.cls}">${st.vpMsg.html}</div>` : ""; }
function nozzleRow(n, vert) {
  const src = n.sources || {}, mk = n.mark || "";
  const cls = (f) => { const k = srcKind(src[f]); return k && k !== "user" ? " src-" + k : (isEmpty(n[f]) ? " src-pending" : ""); };
  const ttl = (f) => (src[f] ? `来源：${(SRC_CN[srcKind(src[f])] || ["", ""])[0] || "待填"}（${src[f]}）` : "");
  const FN = { dn: "DN", orientation_deg: "方位（度）", projection_mm: "伸出长度（mm）" };
  const inp = (f, min) => `<input type="number" step="any" ${min != null ? `min="${min}"` : ""} data-mark="${esc(mk)}" data-f="${f}" value="${isEmpty(n[f]) ? "" : esc(String(+(+n[f]).toFixed(4)))}" class="${cls(f).trim()}" title="${esc(ttl(f))}" aria-label="管口 ${esc(mk)} 的${FN[f]}" ${mk ? "" : 'disabled data-perm="1"'} />`;
  const detail = n.location === "shell" ? (isEmpty(n.axial_mm) ? "" : `轴向 ${fmtVal(n.axial_mm)}`) : (isEmpty(n.radial_mm) ? "" : `径向 ${fmtVal(n.radial_mm)}`);
  const std = [n.std_part, n.flange_type, n.facing].filter((x) => !isEmpty(x)).join(" ");
  return `<tr><td class="mk" title="${esc(std)}">${esc(mk || "?")}</td><td title="${esc(NKIND_CN[n.kind] || "")}">${esc(n.purpose || n.purpose_text || NKIND_CN[n.kind] || "—")}</td><td>${inp("dn", 1)}</td>
    <td title="${esc(ttl("location"))}">${esc(LOC_CN[n.location] || "待定")}${detail ? `<div class="sub">${esc(detail)}</div>` : ""}</td><td>${inp("orientation_deg")}</td><td>${inp("projection_mm", 0)}</td><td title="${esc(ttl("pn"))}">${esc(isEmpty(n.pn) ? "待定" : fmtVal(n.pn))}</td>
    <td>${mk ? `<button class="del" type="button" data-act="del" data-mark="${esc(mk)}" title="删除管口 ${esc(mk)}（可以撤销）">✕</button>` : ""}</td></tr>`;
}
function fieldsHtml(fields, supports) {
  const rows = (fields || []).filter((f) => !/^nozzles/.test(f.path || "")), groups = new Map([...FGROUPS.map((g) => [g[0], []]), ["其他", []]]);
  (supports || []).forEach((s, i) => { if (!isEmpty(s.height_mm)) rows.push({ path: `supports[${i}].height_mm`, label: `支座${i + 1}高度（mm）`, value: s.height_mm, source: (s.sources || {}).height_mm || "" }); });     // fields 里没有支腿/裙座高度，从 supports 补一行
  rows.forEach((f) => { const g = FGROUPS.find((x) => x[1].test(f.path || "")); groups.get(g ? g[0] : "其他").push(f); });
  let h = "";
  groups.forEach((list, name) => {
    if (!list.length) return;
    h += `<h4>${name}</h4><table class="vp-tbl"><tbody>${list.map((f) => {
      const pend = f.source === "pending" || isEmpty(f.value), val = f.path === "kind" ? (KIND_CN[f.value] || f.value) : /^supports\[\d+\]\.kind$/.test(f.path) ? (SUP_CN[f.value] || f.value) : f.value;
      return `<tr><td class="lb" title="${esc(f.path)}">${esc(f.label || f.path)}</td><td class="val">${pend ? '<span class="sub">—</span>' : esc(fmtVal(val))}${srcBadge(pend ? "pending" : f.source, pend ? null : val, f.unverified)}</td></tr>`;
    }).join("")}</tbody></table>`;
  });
  return h;
}
function renderVessel() {
  const p = $("#pane-vessel"), v = st.vessel; renderVesselTab();
  if (!v) { p.innerHTML = `<div class="empty">暂无参数<br/><span class="sub">${esc(!st.id ? "未打开图纸" : st.vesselErr ? "（" + st.vesselErr + "）" : "这张图还没有设备参数，在下面说出要画的设备")}</span></div>`; return; }
  const top = p.scrollTop, rec = v.record || {}, ck = v.check, errs = ck && ck.errors ? ck.errors : [], wrn = ck && ck.warnings ? ck.warnings : [], pend = v.pending || [], fields = v.fields || [];
  const vert = v.kind !== "horizontal_vessel", unver = fields.filter((f) => f.unverified).length, noz = v.nozzles || [], sup = v.supports || [], vw = v.warnings || [], d = st.vaDraft;
  const locs = vert ? [["top_head", "上封头"], ["bottom_head", "下封头"], ["shell", "筒体"]] : [["left_head", "左封头"], ["right_head", "右封头"], ["shell", "筒体"]];
  const chips = [rec.den_main ? `<span class="vp-chip">主视图 1:${esc(fmtVal(rec.den_main))}</span>` : "", rec.den_end ? `<span class="vp-chip">${vert ? "方位图" : "左视图"} 1:${esc(fmtVal(rec.den_end))}</span>` : "",
    ck ? `<span class="vp-chip ${errs.length ? "bad" : "good"}" title="设备图自检">自检 ${errs.length} 个错误${wrn.length ? "、" + wrn.length + " 条警告" : ""}</span>` : "",
    pend.length ? `<span class="vp-chip bad">缺 ${pend.length} 项</span>` : "", unver ? `<span class="vp-chip warn" title="标准数据尚未与标准原文核对">未核对 ${unver} 项</span>` : ""].join("");
  p.innerHTML = `<div class="vp-pane">
    <div class="vp-head"><b>${esc(v.summary || "化工设备")}</b><div class="sub">${esc(KIND_CN[v.kind] || v.kind || "")}${v.tag ? " · 位号 " + esc(v.tag) : ""}${v.name ? " · " + esc(v.name) : ""}</div></div>
    <div class="vp-chips">${chips}</div>
    <div class="vp-acts"><button class="btn accent" type="button" data-act="regen" title="按当前参数重新排版，把生成的图元写回图纸">重新生成</button><button class="btn" type="button" data-act="check" title="必问项、管口重叠/越界、表与视图一致、尺寸链、布局、比例、字高">自检</button><button class="btn" type="button" data-act="export" title="导出 DXF 和 PDF（PDF 从布局「设备图」出图；自检有错误时不能导出）">导出 DXF/PDF</button><button class="btn" type="button" data-act="orient" title="${vert ? "另出一张管口方位图（俯视）" : "卧式容器出左视图"}">另出管口方位图</button></div>
    <div id="vpMsg"></div>
    ${pend.length ? `<h4>待填项 <small>${pend.length}</small></h4><ul class="vp-list">${pend.map((x) => `<li>${esc(x.label || x.path)}${(x.options || []).length ? `<span class="sub">（例如 ${esc(x.options.slice(0, 3).join(" / "))}）</span>` : ""}</li>`).join("")}</ul><div class="sub">在下面的输入框里补充就行。</div>` : ""}
    ${vw.length ? `<details ${vw.length <= 3 ? "open" : ""}><summary>警告 ${vw.length} 条</summary><ul class="vp-list">${vw.map((w) => `<li>${esc(w)}</li>`).join("")}</ul></details>` : ""}
    <h4>管口表 <small>${noz.length} 个</small></h4>
    ${noz.length ? `<table class="vp-tbl"><colgroup><col /><col /><col class="c-dn" /><col /><col class="c-ori" /><col class="c-proj" /><col /><col /></colgroup><thead><tr><th>符号</th><th>用途</th><th>DN</th><th>位置</th><th>方位°</th><th>伸出</th><th>PN</th><th></th></tr></thead><tbody>${noz.map((n) => nozzleRow(n, vert)).join("")}</tbody></table>` : '<div class="sub">还没有管口。</div>'}
    <datalist id="vaPurposes">${PURPOSES.map((x) => `<option value="${x}"></option>`).join("")}</datalist>
    <div class="vp-add"><input id="vaPurpose" list="vaPurposes" placeholder="用途，如 放空口" aria-label="新管口的用途" value="${esc(d.purpose)}" /><input id="vaDn" type="number" min="1" step="any" placeholder="DN" aria-label="新管口的 DN" value="${esc(d.dn)}" /><select id="vaLoc" aria-label="新管口的位置"><option value="">位置：默认</option>${locs.map(([k, t]) => `<option value="${k}" ${d.loc === k ? "selected" : ""}>${t}</option>`).join("")}</select><button class="btn" type="button" data-act="add">＋ 加管口</button></div>
    <div class="vp-legend">改 DN / 方位 / 伸出长度：改完回车或点别处就会按新参数重新出图（可以撤销）。黄底 = 程序按规则补的默认值，红框 = 待填。符号 a/b/c 稳定不变，方位 0° 朝上、顺时针（卧式从左封头端看）。</div>
    ${fieldsHtml(fields, sup)}
    <div class="vp-legend">来源：<span class="badge-src s-user">用户</span>你说的　<span class="badge-src s-rule">默认</span>程序按规则补的，待你确认　<span class="badge-src s-table">查表</span>按标准表　<span class="badge-src s-calc">计算</span>程序算的　<span class="badge-src s-pending">待填</span>缺值　<span class="badge-src s-unverified">未核对</span>标准数据尚未与标准原文核对，仅供方案阶段。</div></div>`;
  paintVpMsg($("#vpMsg")); p.scrollTop = top; syncVesselLock();
}
function syncVesselLock() { const lock = st.busy || st.eqBusy; $("#pane-vessel").querySelectorAll("button, input, select").forEach((x) => { if (!x.dataset.perm) x.disabled = lock; }); }
const busyMsg = () => setVpMsg("", "智能体或上一个操作还在处理，稍后再试。");
const errHtml = (r) => `<span class="sub">[${esc(r.error_code || "ERR")}]</span> ${esc(r.message || "操作失败")}${r.hint && !String(r.message || "").includes(r.hint) ? `<div class="sub">${esc(r.hint)}</div>` : ""}${(r.pending || []).length ? `<ul>${r.pending.map((x) => `<li>${esc(x.label || x.path)}</li>`).join("")}</ul>` : ""}`;
function okHtml(r) {
  let h = esc(r.summary || "完成"); const ws = r.warnings || [], ds = r.defaults_used || [];
  if (ws.length || ds.length) {                         // 警告和“按默认”的清单默认折叠，要看再点开
    h += `<details><summary>${ws.length ? ws.length + " 条警告" : ""}${ws.length && ds.length ? "、" : ""}${ds.length ? ds.length + " 条默认值说明" : ""}</summary>`;
    if (ws.length) h += `<ul>${ws.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>`;
    if (ds.length) h += `<div class="sub">${ds.map((x) => esc(typeof x === "string" ? x : JSON.stringify(x))).join("<br/>")}</div>`;
    h += "</details>";
  }
  return h;
}
async function vop(tool, args) {                           // 面板按钮 → 直接调函数；智能体在跑或上一个操作没完成时不接新的
  if (st.eqBusy || st.busy) return null;
  st.eqBusy = true; syncVesselLock();
  try { return await op(tool, args, { quiet: true, refresh: false, warn: false }); }       // 警告在面板的提示里看，不再刷命令行日志
  catch (e) { return { ok: false, error_code: "HTTP", message: e.message }; }
  finally { st.eqBusy = false; syncVesselLock(); }
}
async function patchVessel(args) {                         // vessel_define(op=patch)：成功后刷新预览和面板；失败把 message 原样给用户，面板回到服务器上的值
  const r = await vop("vessel_define", { op: "patch", ...args });
  if (!r) { busyMsg(); renderVessel(); return false; }
  if (!r.ok) { setVpMsg("err", errHtml(r)); renderVessel(); return false; }
  setVpMsg("ok", okHtml(r)); await refresh(true); return true;
}
async function nozzleEdit(i) {
  const f = i.dataset.f, mark = i.dataset.mark, raw = i.value.trim(), num = Number(raw), name = { dn: "DN", orientation_deg: "方位", projection_mm: "伸出长度" }[f];
  if (raw === "" || !Number.isFinite(num)) { setVpMsg("err", `管口 ${esc(mark)} 的${name}要填数字`); renderVessel(); return; }
  if ((f === "dn" && num <= 0) || (f === "projection_mm" && num < 0)) { setVpMsg("err", `管口 ${esc(mark)} 的${name}不能是 ${esc(raw)}`); renderVessel(); return; }
  await patchVessel({ set: { [`nozzles[${mark}].${f}`]: num } });
}
async function vesselDel(mark) {
  const n = ((st.vessel && st.vessel.nozzles) || []).find((x) => x.mark === mark);
  if (!confirm(`删除管口 ${mark}${n ? "（" + (n.purpose || n.purpose_text || "") + " DN" + fmtVal(n.dn) + "）" : ""}？删除后图和管口表会重新生成，可以撤销。`)) return;
  await patchVessel({ remove_nozzles: [mark] });
}
async function vesselAdd() {
  const d = st.vaDraft, dn = Number(d.dn), purpose = d.purpose.trim();
  if (!purpose) { setVpMsg("err", "请填管口用途，例如 放空口"); return; }
  if (d.dn === "" || !Number.isFinite(dn) || dn <= 0) { setVpMsg("err", "请填管口公称尺寸 DN（数字）"); return; }
  const nz = { dn, purpose_text: purpose }, kind = kindOf(purpose);
  if (kind) nz.kind = kind;
  if (d.loc) nz.location = d.loc;                          // 没选位置就不写，由程序按规则定
  if (await patchVessel({ upsert_nozzles: [nz] })) { st.vaDraft = { purpose: "", dn: "", loc: "" }; renderVessel(); }
}
async function vesselRegen() {
  const r = await vop("vessel_generate_drawing", { views: ["main"] });         // 注意：参数 {} 时函数认为“参数没变”不会重排，所以明确要求重排主视图（已有的管口方位图随之保留）
  if (!r) return busyMsg();
  if (!r.ok) { setVpMsg("err", errHtml(r)); return; }
  setVpMsg("ok", okHtml(r)); await refresh(true);
}
async function vesselOrient() {
  const r = await vop("vessel_generate_drawing", { views: ["orientation"] });
  if (!r) return busyMsg();
  if (!r.ok) { setVpMsg("err", errHtml(r)); return; }
  st.eqView = EQ_ORI; st.eqViewUser = true;                                    // 出完直接看这一张（没有这个布局时 eqView() 自动回到「设备图」）
  setVpMsg("ok", okHtml(r)); await refresh(true);
}
async function vesselCheck() {
  const r = await vop("vessel_check", {});
  if (!r) return busyMsg();
  if (!r.ok) { setVpMsg("err", errHtml(r)); return; }
  const d = r.data || {}, es = d.errors || [], ws = d.warnings || [], li = (x) => `<li>${x.code && !String(x.message || "").startsWith(x.code) ? `<b>${esc(x.code)}</b> ` : ""}${esc(x.message || (typeof x === "string" ? x : JSON.stringify(x)))}</li>`;
  setVpMsg(es.length ? "err" : "ok", `${esc(r.summary || "自检完成")}${es.length ? `<ul>${es.map(li).join("")}</ul>` : ""}${ws.length ? `<details ${es.length ? "" : "open"}><summary>警告 ${ws.length} 条</summary><ul>${ws.map(li).join("")}</ul></details>` : ""}`);
}
async function exportVessel() {
  if (!st.id) return;
  if (st.busy || st.eqBusy) return busyMsg();
  st.eqBusy = true; syncVesselLock(); setVpMsg("", "导出中…（DXF 和 PDF，PDF 从布局「设备图」出图）");
  try {
    const r = await api(`/api/drawings/${st.id}/export`, { method: "POST", body: { formats: ["dxf", "pdf"] } });
    const links = (r.files || []).map((f) => `<a href="${esc(f.url)}" target="_blank" rel="noopener" download>${esc(String(f.format).toUpperCase())}</a>`).join(" · "), ws = (r.warnings || []).map((w) => `<li>${esc(w.message || w)}</li>`).join("");
    setVpMsg(ws ? "" : "ok", `已导出：<span class="vp-files">${links}</span>${ws ? `<ul>${ws}</ul>` : ""}`);
    addMsg(`已导出：${links}`); log("已导出 " + (r.files || []).map((f) => f.format).join("、"), "ok");
  } catch (e) { setVpMsg("err", "导出失败：" + esc(e.message)); log("导出失败：" + e.message, "err"); }      // 自检有错误时服务器拒绝导出交付件，原因原样显示
  finally { st.eqBusy = false; syncVesselLock(); }
}
{ // 面板事件（事件代理，面板重绘不丢）
  const vp = $("#pane-vessel");
  vp.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-act]"); if (!b || b.disabled) return;
    ({ regen: vesselRegen, check: vesselCheck, export: exportVessel, orient: vesselOrient, add: vesselAdd, del: () => vesselDel(b.dataset.mark) })[b.dataset.act]?.();
  });
  vp.addEventListener("change", (e) => { const i = e.target; if (i.tagName === "INPUT" && i.dataset.f && i.dataset.mark) nozzleEdit(i); });
  vp.addEventListener("input", (e) => { const i = e.target; if (i.id === "vaPurpose") st.vaDraft.purpose = i.value; else if (i.id === "vaDn") st.vaDraft.dn = i.value; else if (i.id === "vaLoc") st.vaDraft.loc = i.value; });
  vp.addEventListener("keydown", (e) => { if (e.key !== "Enter" || e.target.tagName !== "INPUT") return; if (e.target.dataset.f) e.target.blur(); else if (e.target.id === "vaPurpose" || e.target.id === "vaDn") vesselAdd(); });
}

// ------------------------------------------------------------------ 启动
buildTools(); setToolButtons(); syncWsButtons(); renderVessel();
(async () => {
  const items = await listDrawings().catch(() => []);
  const tm = location.hash.match(/^#task=(.+)$/);        // 带着通用绘图任务进来：接着显示进度，图纸在 drawing 事件里打开
  if (tm) { history.replaceState(null, "", location.pathname); startPoll(decodeURIComponent(tm[1])); return; }
  const want = location.hash.slice(1) || localStorage.getItem("zhihui.cad.last");
  if (want && (items.some((d) => d.drawing_id === want) || /^dwg_/.test(want))) { try { await openDrawing(want); return; } catch { /* 图纸已不存在 */ } }
  if (items[0]) await openDrawing(items[0].drawing_id); else log("还没有图纸：直接说，例如“画一台立式储罐的设备图，内径1600…”或“画精馏工段的工艺流程图”；也可以点“＋ 新建”。");
})();
window.__cad = { st, op, refresh, setWs, loadVessel };
})();
