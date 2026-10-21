/* 外壳：设置对话框（API Key、模型、接口地址，保存后立即生效）和嵌入模式。
   入口只有一个——通用 CAD 页面（/）；“化工流程图”是它里面的一个工作空间（iframe 嵌入 index.html?embed=1）。 */
(function () {
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // ---------------------------------------------------------------- 设置对话框
  const PROVIDER_ENV = { deepseek: ["DEEPSEEK_API_KEY", "DEEPSEEK_MODEL", "DEEPSEEK_BASE_URL"], qwen: ["DASHSCOPE_API_KEY", "QWEN_MODEL", "QWEN_BASE_URL"], glm: ["ZHIPUAI_API_KEY", "GLM_MODEL", "GLM_BASE_URL"], openai: ["OPENAI_API_KEY", "OPENAI_MODEL", "OPENAI_BASE_URL"] };
  let modal = null, data = null;
  const q = (sel) => modal.querySelector(sel);

  function build() {
    if (modal) return;
    modal = document.createElement("div");
    modal.className = "zhs-modal"; modal.hidden = true; modal.id = "settingsModal";
    modal.innerHTML = `<div class="zhs-bg" data-close></div>
      <div class="zhs-card" role="dialog" aria-modal="true" aria-labelledby="zhsTitle">
        <div class="zhs-head"><h2 id="zhsTitle">设置：大模型 API</h2><button class="zhs-btn" type="button" data-close>关闭</button></div>
        <div class="zhs-body" id="settingsBody"></div>
        <div class="zhs-foot"><div class="zhs-msg" id="settingsMsg"></div><button class="zhs-btn primary" id="saveSettings" type="button">保存</button></div>
      </div>`;
    document.body.appendChild(modal);
    modal.querySelectorAll("[data-close]").forEach((el) => (el.onclick = close));
    q("#saveSettings").onclick = () => save();
    q("#settingsBody").addEventListener("click", onBodyClick);
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !modal.hidden) close(); });
  }
  function close() { if (modal) modal.hidden = true; }
  async function open() {
    build(); modal.hidden = false; q("#settingsMsg").textContent = "读取中…"; q("#settingsBody").innerHTML = "";
    try {
      const r = await fetch("/api/settings"); const j = await r.json();
      if (!r.ok) throw new Error(j.detail || r.status);
      data = j; q("#settingsMsg").textContent = ""; draw();
    } catch (e) { q("#settingsMsg").innerHTML = `<span class="zhs-err">${esc(e.message)}</span>`; }
  }
  function draw() {
    const s = data, opt = (list, v, names = {}) => list.map((x) => `<option value="${esc(x)}" ${x === v ? "selected" : ""}>${esc(names[x] ?? x)}</option>`).join("");
    const pname = Object.fromEntries(s.providers.map((p) => [p.id, p.label]));
    q("#settingsBody").innerHTML = `
      <section><h3>大模型</h3><div class="zhs-grid">
        <label>主模型<select data-k="ZHIHUI_LLM_PRIMARY">${opt(s.choices.ZHIHUI_LLM_PRIMARY, s.ZHIHUI_LLM_PRIMARY, pname)}</select></label>
        <label>备用模型（主模型失败时自动切换）<select data-k="ZHIHUI_LLM_BACKUP">${opt(s.choices.ZHIHUI_LLM_BACKUP, s.ZHIHUI_LLM_BACKUP, { "": "不用备用", ...pname })}</select></label>
        <label>联网检索生成图元<select data-k="ZHIHUI_SYMBOL_SEARCH">${opt(s.choices.ZHIHUI_SYMBOL_SEARCH, s.ZHIHUI_SYMBOL_SEARCH, { auto: "自动（有 DeepSeek Key 时开启）", on: "开启", off: "关闭" })}</select></label>
      </div><p class="zhs-sub">本系统所有自然语言口令都必须经过大模型：先在下面填 API Key，保存后立即生效。</p></section>
      ${s.providers.map((p) => `<section><h3>${esc(p.label)} ${p.key_set ? `<small class="zhs-ok">已设置 Key ${esc(p.key_hint)}</small>` : '<small class="zhs-warn">未设置 Key</small>'}</h3>
        <div class="zhs-grid">
          <label>API Key<input type="password" autocomplete="off" spellcheck="false" data-key="${esc(p.id)}" placeholder="${p.key_set ? "已设置，留空表示不修改" : "粘贴你的 API Key"}"></label>
          <label>模型名<input data-k="${p.id}.model" value="${esc(p.model)}" placeholder="默认 ${esc(p.model_default)}"></label>
          <label>接口地址<input data-k="${p.id}.base" value="${esc(p.base_url)}" placeholder="默认 ${esc(p.base_default)}"></label>
        </div>
        <div class="zhs-row"><button type="button" class="zhs-btn" data-test="${esc(p.id)}">测试连接</button>${p.key_set ? `<button type="button" class="zhs-link" data-clear="${esc(p.id)}">清除 Key</button>` : ""}<span class="zhs-sub" data-result="${esc(p.id)}"></span></div></section>`).join("")}
      <p class="zhs-sub">Key 只保存在运行服务这台电脑的 <code>app/.env</code> 里，页面上不会再显示完整 Key。保存后立即生效，不用重启。</p>`;
  }
  function collect() {
    const values = {}, body = q("#settingsBody");
    body.querySelectorAll("select[data-k]").forEach((el) => (values[el.dataset.k] = el.value));
    body.querySelectorAll("input[data-key]").forEach((el) => { if (el.value.trim()) values[PROVIDER_ENV[el.dataset.key][0]] = el.value.trim(); });
    body.querySelectorAll("input[data-k]").forEach((el) => { const [pid, f] = el.dataset.k.split("."); values[PROVIDER_ENV[pid][f === "model" ? 1 : 2]] = el.value.trim(); });
    return values;
  }
  async function save(clear = []) {
    q("#settingsMsg").textContent = "保存中…";
    try {
      const r = await fetch("/api/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ values: collect(), clear }) });
      const j = await r.json();
      if (!r.ok) throw new Error(j.detail || r.status);
      data = j.settings; draw(); refreshHealth();
      window.dispatchEvent(new CustomEvent("zhsettings:saved"));
      q("#settingsMsg").innerHTML = `<span class="zhs-ok">已保存${j.changed.length ? "（" + j.changed.length + " 项改动），立即生效" : "，没有改动"}</span>`;
      return true;
    } catch (e) { q("#settingsMsg").innerHTML = `<span class="zhs-err">${esc(e.message)}</span>`; return false; }
  }
  async function onBodyClick(e) {
    const b = e.target.closest("button"); if (!b) return;
    if (b.dataset.clear) { if (confirm("清除这个 Key？之后用它的任务会失败，直到重新填写。")) await save([PROVIDER_ENV[b.dataset.clear][0]]); return; }
    if (b.dataset.test) {
      const pid = b.dataset.test, out = q(`[data-result="${pid}"]`);
      out.textContent = "先保存，再测试…";
      if (!(await save())) return;
      const box = q(`[data-result="${pid}"]`); box.textContent = "连接中…";
      try {
        const j = await fetch("/api/settings/test", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ provider: pid }) }).then((r) => r.json());
        box.innerHTML = `<span class="${j.ok && j.model_listed !== false ? "zhs-ok" : "zhs-err"}">${esc(j.message || j.detail || "")}</span>`;
      } catch (err) { box.innerHTML = `<span class="zhs-err">${esc(err.message)}</span>`; }
    }
  }
  async function refreshHealth() {
    try {
      const h = await fetch("/api/health").then((r) => r.json());
      const b = document.getElementById("btnSettings"); if (b) b.classList.toggle("alert", !!h.key_missing);
    } catch { /* 后端没起时静默 */ }
  }

  // ---------------------------------------------------------------- 嵌入模式
  // 通用 CAD 页面是唯一的入口；“化工流程图”是它里面的一个工作空间，用 iframe 嵌入 index.html?embed=1（隐藏自己的页头）。
  const embed = new URLSearchParams(location.search).has("embed");
  if (embed) document.documentElement.classList.add("embed");

  const btn = document.getElementById("btnSettings");
  if (btn) { btn.onclick = open; refreshHealth(); }
  window.ZhShell = { embed, openSettings: open, refreshHealth };
})();
