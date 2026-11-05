/* DSAI4207 Agent Console 前端逻辑（原生 JS，轮询 2.5s） */
"use strict";

const $ = (s) => document.querySelector(s);
const state = { overview: null, runs: [], selected: null, detail: null };
let lastSig = "";

function renderAll() {
  // 数据无变化就不重渲染：避免轮询打断视频播放、避免点击与刷新赛跑
  const sig = JSON.stringify([state.overview, state.runs, state.detail]);
  if (sig === lastSig) return;
  lastSig = sig;
  renderTopbar(); renderBackendSelect(); renderRunlist(); renderFlow(); renderDetail();
}

/* ── 流程节点定义（上排免费 / 闸门 / 下排付费）── */
const FLOW = [
  { k: "llm_plan",  label: "LLM 规划",   n: 1, free: true },
  { k: "frozen",    label: "冻结照片",   n: 2, free: true, always: true },
  { k: "tripo_model", label: "Tripo 建模", n: 3, free: true },
  { k: "glb_check", label: "GLB 检查",   n: 4, free: true },
  { k: "blender_render", label: "渲染+审计", n: 5, free: true, loop: true },
  { k: "gate",      label: "闸门",       gate: true },
  { k: "prompt",    label: "提示词",     n: 6 },
  { k: "seedance",  label: "Seedance",   n: 7, paid: true },
  { k: "ledger",    label: "账本",       n: 8 },
];

function fmtUSD(x) { return "$" + (x || 0).toFixed(2); }
function toast(msg, ms = 3200) {
  const d = document.createElement("div"); d.textContent = msg;
  $("#toast").appendChild(d); setTimeout(() => d.remove(), ms);
}

/* ── 数据拉取 ── */
async function jget(url) { const r = await fetch(url); if (!r.ok) throw new Error(url); return r.json(); }
async function refresh() {
  try {
    state.overview = await jget("/api/overview");
    state.runs = await jget("/api/runs");
    if (state.selected) {
      try { state.detail = await jget("/api/runs/" + state.selected); }
      catch { state.selected = null; state.detail = null; }
    }
    renderAll();
  } catch (e) { console.warn(e); }
}

/* ── 渲染 ── */
function renderTopbar() {
  const o = state.overview; if (!o) return;
  $("#budget-pill").textContent = `预算 ${fmtUSD(o.budget.spent_usd)} / $${o.budget.max_usd.toFixed(0)}`;
  const pct = Math.min(100, o.budget.spent_usd / o.budget.max_usd * 100);
  $("#budget-pill").className = "badge mono " + (pct > 80 ? "b-err" : pct > 50 ? "b-warn" : "b-ok");
  $("#glb-pill").textContent = "GLB " + (o.frozen_glb_sha256 || "—");
  $("#key-status").innerHTML =
    `<span class="keydot"><i class="dot ${o.runway_key ? "on" : ""}"></i>Runway</span>` +
    `<span class="keydot" style="margin-left:12px"><i class="dot ${o.tripo_key ? "on" : ""}"></i>Tripo</span>`;
}

function renderBackendSelect() {
  const o = state.overview; if (!o) return;
  const sel = $("#f-backend");
  const cur = sel.value;
  sel.innerHTML = Object.entries(o.backends).map(([name, b]) =>
    `<option value="${name}">${name === "mock" ? "mock（本地脚本，免 LLM）" : name + " · " + b.model}</option>`).join("");
  if ([...sel.options].some(op => op.value === cur)) sel.value = cur;
}

function renderRunlist() {
  const el = $("#runlist");
  $("#runs-count").textContent = state.runs.length + " 次";
  if (!state.runs.length) { el.innerHTML = `<div class="empty">还没有运行记录</div>`; return; }
  el.innerHTML = state.runs.map(r => {
    const failed = r.status === "done" && r.exit_code !== 0;
    const dot = r.status === "running" ? "running" : failed ? "failed" : "done";
    const badge = r.status === "running" ? '<span class="badge b-info">运行中</span>'
      : failed ? '<span class="badge b-err">失败</span>' : "";
    return `<div class="runrow ${r.id === state.selected ? "sel" : ""}" data-id="${r.id}">
      <div class="t"><i class="sdot ${dot}"></i>${r.backend}<span style="color:var(--faint)">·</span>${r.condition}<span style="color:var(--faint)">·</span>s${r.seed} ${badge}</div>
      <div class="cost mono">${fmtUSD(r.cost_usd)}</div>
      <div class="meta mono">${r.id}</div>
      <div class="meta">${r.framing ? (r.framing.passed ? "取景通过" : "取景未通过") : ""}</div>
    </div>`;
  }).join("");
  el.querySelectorAll(".runrow").forEach(node =>
    node.onclick = () => { state.selected = node.dataset.id; refresh(); });
}

function nodeState(node, r) {
  if (!r) return "";
  const st = r.stages || {};
  const running = r.status === "running";
  switch (node.k) {
    case "llm_plan": case "frozen": case "glb_check": case "ledger":
      return (r.status === "done" && (r.exit_code ?? 0) === 0) || running ? "done" : "";
    case "tripo_model": return st.tripo_model ? "done" : (running ? "" : "");
    case "blender_render":
      if (st.blender_render) return st.blender_render.status === "passed" ? "done" : "failed";
      return running ? "active" : "";
    case "prompt": return st.prompt ? "done" : "";
    case "seedance":
      if (st.seedance) {
        const s = st.seedance.status;
        if (s === "downloaded") return "paid-done";
        if (s === "dry_run_frozen") return "done";
        if (s && s.startsWith("terminal")) return "failed";
        return "active";
      }
      return (running && st.prompt) ? "active" : "";
    default: return "";
  }
}

function renderFlow() {
  const r = state.runs.find(x => x.id === state.selected) || null;
  $("#flow-note").textContent = r
    ? (r.status === "running" ? "运行中…" : `exit ${r.exit_code ?? 0} · 花费 ${fmtUSD(r.cost_usd)}`)
    : "未选择运行";
  $("#flow").innerHTML = FLOW.map(node => {
    if (node.gate) return `<div class="fgate"><span class="g">⏸</span>闸门<br>人工确认</div>`;
    const s = nodeState(node, r);
    const cls = s === "paid-done" ? "paid-done" : s;
    const loop = node.loop ? ' <span style="color:var(--faint)" title="LLM 免费循环：未通过可调参重渲">↺</span>' : "";
    return `<div class="fnode ${cls}"><span class="n">${node.n}</span>${node.label}${loop}</div>`;
  }).join('<span class="farrow">→</span>');
}

function marginBar(label, v, need) {
  const pct = Math.min(100, v * 100);
  const cls = v >= need ? "" : v >= need * 0.7 ? "warn" : "bad";
  return `<div class="bar-row"><span>${label}</span>
    <div class="bar"><i class="${cls}" style="width:${pct}%"></i></div>
    <span class="mono">${(v * 100).toFixed(1)}%</span></div>`;
}

function renderDetail() {
  const el = $("#detail");
  const d = state.detail;
  if (!d) { el.innerHTML = `<div class="card"><div class="empty">选择左侧一次运行查看详情</div></div>`; return; }
  const fr = d.framing;
  const stats = fr ? `
    <div class="stats">
      <div class="stat"><div class="k">垂直 FOV</div>
        <div class="v">${fr.fov_vertical_degrees.toFixed(2)}°</div>
        <div class="hint mono">焦距 ${fr.lens_mm.toFixed(1)}mm · ${fr.vertices.toLocaleString()} 顶点</div></div>
      <div class="stat"><div class="k">最小安全边距</div>
        <div class="v" style="color:${fr.passed ? "var(--emerald)" : "var(--rose)"}">${(Math.min(...fr.min_margins_lbrt) * 100).toFixed(1)}%</div>
        <div class="bars">${marginBar("左", fr.min_margins_lbrt[0], fr.plan.margin)}
        ${marginBar("下", fr.min_margins_lbrt[1], fr.plan.margin)}
        ${marginBar("右", fr.min_margins_lbrt[2], fr.plan.margin)}
        ${marginBar("上", fr.min_margins_lbrt[3], fr.plan.margin)}</div></div>
      <div class="stat"><div class="k">参考视频</div>
        <div class="v">${fr.frames}<small>帧</small></div>
        <div class="hint mono">${(fr.frames / 30).toFixed(1)}s · 渲染 ${fr.render_seconds}s</div></div>
      <div class="stat"><div class="k">侧面顺序</div>
        <div class="v" style="font-size:14.5px; line-height:1.9">${fr.side_sequence.join(" → ")}</div>
        <div class="hint">${fr.plan.direction === "cw" ? "顺时针" : "逆时针"} · 俯视 ${fr.plan.elevation_degrees}°</div></div>
    </div>` : "";

  const videoCard = d.video ? `
    <div class="card"><div class="card-h"><h2>参考视频（粗模）</h2>
      <span class="badge ${fr && fr.passed ? "b-ok" : "b-err"}">${fr && fr.passed ? "取景审计通过" : "取景审计未通过"}</span></div>
      <div class="card-b">
        <video controls preload="metadata" src="${d.video}"></video>
        <div class="frames-strip">${(d.frames || []).map(f => `<img src="${f}">`).join("")}</div>
      </div></div>` : "";

  const ledgerRows = (d.ledger || []).map(r => `
    <tr><td>${(r.stage || "")}</td><td>${r.status || ""}</td>
    <td class="r mono">${r.cost_usd ? fmtUSD(r.cost_usd) : "—"}</td></tr>`).join("");

  el.innerHTML = `
    ${stats}
    <div class="detail-grid">
      <div class="col">
        ${videoCard}
        <div class="card"><div class="card-h"><h2>运行日志</h2></div>
          <div class="card-b"><div class="log-box mono">${(d.log_tail || "（无输出）").split("\n").slice(-14).join("\n")}</div></div></div>
      </div>
      <div class="col">
        <div class="card"><div class="card-h"><h2>Seedance 提示词</h2>
          ${d.prompt_text ? '<span class="badge b-ok">已生成</span>' : '<span class="badge b-mut">未生成</span>'}</div>
          <div class="card-b"><div class="prompt-box mono">${(d.prompt_text || "（取景通过后生成）")}</div></div></div>
        ${d.frozen_request ? `<div class="card"><div class="card-h"><h2>冻结请求</h2>
          <span class="badge ${d.frozen_request.estimated_usd >= 4 ? "b-paid" : "b-warn"}">${d.frozen_request.quality} · ${fmtUSD(d.frozen_request.estimated_usd)}</span></div>
          <div class="card-b"><table>
            <tr><td>模式</td><td class="mono">${d.frozen_request.model} / ${d.frozen_request.mode}</td></tr>
            <tr><td>比例 / 时长</td><td class="mono">${d.frozen_request.ratio} · ${d.frozen_request.duration}s · seed ${d.frozen_request.seed}</td></tr>
            <tr><td>预估扣费</td><td class="mono">${d.frozen_request.estimated_credits} credits ≈ ${fmtUSD(d.frozen_request.estimated_usd)}</td></tr>
            <tr><td>请求哈希</td><td class="mono" style="font-size:10.5px">${d.frozen_request.request_sha256.slice(0, 24)}…</td></tr>
          </table></div></div>` : ""}
        <div class="card"><div class="card-h"><h2>账本（本次运行）</h2>
          <span class="badge b-mut mono">花费 ${fmtUSD(d.cost_usd)}</span></div>
          <div class="card-b" style="padding:0">
            <table><tr><th>阶段</th><th>状态</th><th class="r">成本</th></tr>${ledgerRows}</table>
          </div></div>
      </div>
    </div>`;
}

/* ── 新建运行 ── */
$("#f-dry").onchange = (e) => $("#paid-warn").classList.toggle("show", !e.target.checked);

$("#btn-run").onclick = () => {
  const spec = {
    backend: $("#f-backend").value, condition: $("#f-condition").value,
    seed: parseInt($("#f-seed").value) || 4207, dry_run: $("#f-dry").checked, ack_paid: false,
  };
  if (!spec.dry_run) { openPayModal(spec); return; }
  launch(spec);
};

function openPayModal(spec) {
  const q = state.overview.qualities;
  $("#pay-quote").innerHTML =
    `Seedance <b>${fmtUSD(q["720p"].usd)}</b>/条(720p) 或 <b>${fmtUSD(q["480p"].usd)}</b>/条(480p，10 秒)；` +
    `Tripo 缓存未命中时 $0.20。当前已花费 ${fmtUSD(state.overview.budget.spent_usd)} / $${state.overview.budget.max_usd}。`;
  $("#pay-overlay").classList.add("show");
  $("#pay-ack").checked = false; $("#pay-go").disabled = true;
  $("#pay-go").onclick = () => { $("#pay-overlay").classList.remove("show"); launch({ ...spec, ack_paid: true }); };
}
$("#pay-ack").onchange = (e) => $("#pay-go").disabled = !e.target.checked;
$("#pay-cancel").onclick = () => $("#pay-overlay").classList.remove("show");

async function launch(spec) {
  try {
    const r = await fetch("/api/runs", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(spec) });
    const j = await r.json();
    if (!r.ok) { toast("发起失败：" + (j.detail || "")); return; }
    toast(`已发起 ${j.run_id}（${j.note}）`);
    state.selected = j.run_id;
    setTimeout(refresh, 400);
  } catch (e) { toast("发起失败：" + e.message); }
}

/* ── 轮询 ── */
refresh();
setInterval(refresh, 2500);
