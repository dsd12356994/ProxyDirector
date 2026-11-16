/* ProxyDirector 导演台 —— 对话为主界面的 Agent 客户端（原生 JS） */
"use strict";

const $ = (s) => document.querySelector(s);
const state = {
  view: "chat",
  overview: null, runs: [], selected: null, detail: null,
  chat: { sid: null, runId: null, backend: "mock", seen: 0, busy: false, nodes: new Map() },
};
let lastSig = "";

function fmtUSD(x) { return "$" + (x || 0).toFixed(2); }
function toast(msg, ms = 3200) {
  const d = document.createElement("div"); d.textContent = msg;
  $("#toast").appendChild(d); setTimeout(() => d.remove(), ms);
}
async function jget(url) { const r = await fetch(url); if (!r.ok) throw new Error(url); return r.json(); }
async function jpost(url, body) {
  const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
                               body: JSON.stringify(body || {}) });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || r.status);
  return j;
}

/* ── 视图切换 ─────────────────────────── */
function switchView(v) {
  state.view = v;
  $("#view-chat").style.display = v === "chat" ? "grid" : "none";
  $("#view-runs").style.display = v === "runs" ? "grid" : "none";
  $("#nav-chat").classList.toggle("active", v === "chat");
  $("#nav-runs").classList.toggle("active", v === "runs");
}
$("#nav-chat").onclick = () => switchView("chat");
$("#nav-runs").onclick = () => { switchView("runs"); refresh(); };

/* ── 侧栏 ─────────────────────────────── */
function renderSidebar() {
  const o = state.overview; if (!o) return;
  $("#key-status").innerHTML =
    `<span class="statline"><i class="dot ${o.kling_key ? "on" : ""}"></i>Kling${o.video_backend === "kling" ? " · 当前" : ""}</span>` +
    `<span class="statline"><i class="dot ${o.runway_key ? "on" : ""}"></i>Runway</span>` +
    `<span class="statline"><i class="dot ${o.tripo_key ? "on" : ""}"></i>Tripo</span>`;
  $("#budget-pill").textContent = `LLM 已用 ${fmtUSD(o.budget.spent_usd)} / $${o.budget.max_usd.toFixed(0)}`;
  $("#glb-pill").textContent = `GLB ${(o.frozen_glb_sha256 || "").slice(0, 12)}`;
}

function renderSessions(list) {
  const el = $("#sessions");
  el.innerHTML = (list || []).map(s =>
    `<div class="sess mono ${s.sid === state.chat.sid ? "active" : ""} ${s.busy ? "busy" : ""}" data-sid="${s.sid}">` +
    `${s.backend} · ${s.run_id.slice(9, 17)}</div>`).join("");
  el.querySelectorAll(".sess").forEach(n => n.onclick = () => openSession(n.dataset.sid));
}

/* ── 对话 ─────────────────────────────── */
const TOOL_LABEL = {
  get_run_context: "读取任务上下文", plan_orbit: "渲染环绕并取景审计",
  latest_framing_status: "查看审计明细", build_seedance_prompt: "生成成片提示词",
  submit_seedance: "提交成片生成", stop: "停止",
};

async function newSession(quiet) {
  const backend = $("#c-backend").value || "mock";
  const seed = parseInt($("#c-seed").value) || 4207;
  const j = await jpost("/api/chat/new", { backend, seed });
  state.chat = { sid: j.sid, runId: j.run_id, backend, seen: 0, busy: false, nodes: new Map() };
  $("#chatlog").innerHTML = "";
  $("#chat-hero") && $("#chatlog").appendChild(heroNode());
  $("#c-runid").textContent = j.run_id;
  $("#rail-runid").textContent = j.run_id;
  $("#rail-video").style.display = "none";
  $("#rail-empty").style.display = "block";
  $("#rail-stats").textContent = "";
  $("#rail-open").style.display = "none";
  if (!quiet) toast("新会话已就绪");
  pollSessions();
}

function heroNode() {
  const div = document.createElement("div");
  div.className = "chat-hero"; div.id = "chat-hero";
  div.innerHTML = `<h2>对导演说一句话，它来拍</h2>
    <p>LLM 只做镜头决策：规划环绕 → 本地渲染审计 → 过闸门 → 生成成片。镜头永远受取景闸门约束。</p>
    <div class="chips">
      <button class="chip">拍一条 35° 俯角、更紧凑的环绕</button>
      <button class="chip">换逆时针方向再来一条</button>
      <button class="chip">俯角高一点，物体再小一点</button>
    </div>`;
  div.querySelectorAll(".chip").forEach(c => c.onclick = () => { $("#c-input").value = c.textContent; send(); });
  return div;
}

async function openSession(sid) {
  let j;
  try {
    j = await jget(`/api/chat/${sid}/events`);
  } catch (e) {
    // 会话已失效（如服务重启清空内存）：回退到新会话，而不是留下空白死屏
    toast("该会话已失效（服务重启过），已为你开启新会话");
    await newSession(true);
    return;
  }
  state.chat = { sid, runId: j.run_id, backend: j.backend, seen: 0, busy: j.busy, nodes: new Map() };
  $("#chatlog").innerHTML = "";
  $("#c-runid").textContent = j.run_id;
  $("#rail-runid").textContent = j.run_id;
  switchView("chat");
  pollChat(true);
  pollSessions();
}

async function send() {
  const text = $("#c-input").value.trim();
  if (!text) return;
  if (!state.chat.sid) await newSession(true);
  const dry = $("#c-dry").checked;
  const ack = $("#c-ack").checked;
  if (!dry && !ack) { toast("真实扣费需勾选“授权付费”（或保持 dry-run）"); return; }
  if (!dry) {
    const o = state.overview;
    const q = o.video_backend === "kling" && o.kling
      ? `Kling ${o.kling.model} ${o.kling.duration}s ≈ ${o.kling.credits_per_video} 积分/条`
      : "Runway 480p ≈ $3.00/条";
    if (!confirm(`本轮将真实扣费：${q}。确认继续？`)) return;
  }
  try {
    await jpost(`/api/chat/${state.chat.sid}/send`, { text, dry_run: dry, ack_paid: ack });
    $("#c-input").value = "";
    state.chat.busy = true;
    $("#senddot").classList.add("busy");
    pollChat();
  } catch (e) { toast("发送失败：" + e.message); }
}
$("#btn-send").onclick = send;
$("#c-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
});

function upsertNode(ev) {
  const log = $("#chatlog");
  const hero = $("#chat-hero"); if (hero) hero.remove();
  const map = state.chat.nodes;
  if (ev.kind === "user") {
    const n = bubble("user", "你", ev.text); log.appendChild(n.el);
    log.scrollTop = log.scrollHeight;
  } else if (ev.kind === "mode") {
    const n = document.createElement("div");
    n.className = "modeline-chip";
    n.textContent = ev.dry_run ? "· 本轮 dry-run（不触网不扣费）·"
      : `· 本轮真实扣费（已授权）·`;
    if (!ev.dry_run) n.style.color = "#FF8A82";
    log.appendChild(n);
    $("#rail-mode").innerHTML = ev.dry_run
      ? '<span class="badge b-info">dry-run</span> 不触网不扣费'
      : '<span class="badge b-err">真实扣费</span> 已授权本轮付费调用';
  } else if (ev.kind === "assistant_text") {
    let n = map.get("assistant");
    if (!n) { n = bubble("agent", "DIRECTOR", ""); map.set("assistant", n); log.appendChild(n.el); }
    n.body.textContent = ev.text;
    log.scrollTop = log.scrollHeight;
  } else if (ev.kind === "tool_call") {
    const n = document.createElement("div");
    n.className = "toolcard";
    n.innerHTML = `<div class="tline"><span class="spin"></span>` +
      `<span class="tname">${TOOL_LABEL[ev.tool] || ev.tool}</span>` +
      `<span class="targs">${(ev.args || "").replace(/\s+/g, " ")}</span></div>` +
      `<div class="tresult" style="display:none"></div>`;
    map.set("tc" + ev.id, n); log.appendChild(n);
    log.scrollTop = log.scrollHeight;
  } else if (ev.kind === "tool_result") {
    // 就近更新最近一个未完成的同名工具卡
    let target = null;
    for (const [k, node] of map) {
      if (k.startsWith("tc") && node.dataset.tool === ev.tool && !node.classList.contains("done")) target = node;
    }
    if (!target) {
      for (const [k, node] of map) { if (k.startsWith("tc") && !node.classList.contains("done")) target = node; }
    }
    if (target) {
      target.classList.add("done");
      const refused = !ev.ok;
      if (refused) target.classList.add("refused");
      if ((ev.output || "").includes("积分")) target.classList.add("paid");
      target.dataset.tool = ev.tool;
      const r = target.querySelector(".tresult");
      r.style.display = "block";
      r.textContent = (ev.output || "").slice(0, 400);
      log.scrollTop = log.scrollHeight;
    }
  } else if (ev.kind === "done") {
    const n = bubble("agent", "DIRECTOR", ev.output);
    n.el.classList.add("final");
    log.appendChild(n.el);
    map.delete("assistant");
    log.scrollTop = log.scrollHeight;
    updateRail();
  } else if (ev.kind === "error") {
    const n = bubble("agent error", "错误", ev.message);
    log.appendChild(n.el);
    log.scrollTop = log.scrollHeight;
  }
}

function bubble(cls, who, text) {
  const el = document.createElement("div");
  el.className = "msg " + cls;
  const w = document.createElement("div"); w.className = "who"; w.textContent = who;
  const b = document.createElement("div"); b.className = "body"; b.textContent = text;
  el.append(w, b);
  return { el, body: b };
}

async function pollChat(force) {
  if (!state.chat.sid) return;
  try {
    const j = await jget(`/api/chat/${state.chat.sid}/events?since=${state.chat.seen}`);
    for (const ev of j.events) upsertNode(ev);
    state.chat.seen += j.events.length;
    const wasBusy = state.chat.busy;
    state.chat.busy = j.busy;
    $("#senddot").classList.toggle("busy", j.busy);
    if (wasBusy && !j.busy) { refresh(); updateRail(); pollSessions(); }
  } catch { /* 会话可能已清 */ }
  if (state.chat.busy || force) {
    clearTimeout(pollChat._t);
    pollChat._t = setTimeout(() => { if (state.view === "chat" && (state.chat.busy || force === true)) pollChat(state.chat.busy); }, 900);
  }
}

async function updateRail() {
  if (!state.chat.runId) return;
  try {
    const d = await jget(`/api/runs/${state.chat.runId}`);
    if (d.video) {
      $("#rail-video").src = d.video; $("#rail-video").style.display = "block";
      $("#rail-empty").style.display = "none";
    }
    const fr = d.framing;
    $("#rail-stats").textContent = fr
      ? `FOV ${fr.fov_vertical_degrees.toFixed(1)}°  边距 ${(Math.min(...fr.min_margins_lbrt) * 100).toFixed(1)}%\n` +
        `${fr.plan.elevation_degrees}°  r${fr.plan.radius}  ${fr.plan.direction === "cw" ? "顺时针" : "逆时针"}\n` +
        `本运行花费 ${d.ledger.some(r => r.kling_credits) ? d.ledger.filter(r => r.kling_credits).reduce((a, r) => a + r.kling_credits, 0) + " 积分" : fmtUSD(d.cost_usd)}`
      : "";
    $("#rail-runid").textContent = d.id;
    const btn = $("#rail-open"); btn.style.display = "block";
    btn.onclick = () => { state.selected = d.id; switchView("runs"); refresh(); };
  } catch { /* run 可能尚无产物 */ }
}

async function pollSessions() {
  try { renderSessions(await jget("/api/chat/sessions")); } catch { }
}

/* ── 流程节点（运行记录视图）──────────── */
const FLOW = [
  { k: "llm_plan", label: "LLM 规划", n: 1 },
  { k: "frozen", label: "冻结照片", n: 2 },
  { k: "tripo_model", label: "Tripo 建模", n: 3 },
  { k: "glb_check", label: "GLB 检查", n: 4 },
  { k: "blender_render", label: "渲染+审计", n: 5, loop: true },
  { k: "gate", label: "闸门", gate: true },
  { k: "prompt", label: "提示词", n: 6, paid: true },
  { k: "seedance", label: "成片生成", n: 7, paid: true },
  { k: "ledger", label: "账本", n: 8, paid: true },
];

function nodeState(node, r) {
  if (!r) return "";
  const st = r.stages || {};
  const running = r.status === "running";
  switch (node.k) {
    case "llm_plan": case "frozen": case "glb_check": case "ledger":
      return (r.status === "done" && (r.exit_code ?? 0) === 0) || running ? "done" : "";
    case "tripo_model": return st.tripo_model ? "done" : "";
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
  const nodeHtml = node => {
    const s = nodeState(node, r);
    const cls = s === "paid-done" ? "paid-done" : s;
    const loop = node.loop ? ' <span style="color:var(--faint)" title="免费循环：未通过可调参重渲">↺</span>' : "";
    return `<div class="fnode ${cls}"><span class="n">${node.n}</span>${node.label}${loop}</div>`;
  };
  $("#flow").innerHTML =
    FLOW.filter(n => !n.gate && !n.paid).map(nodeHtml).join("") +
    `<div class="fgate"><span class="g">⏸</span><b>GATE</b>人工确认</div>` +
    FLOW.filter(n => n.paid).map(nodeHtml).join("");
}

/* ── 运行记录 ─────────────────────────── */
function renderRunlist() {
  const el = $("#runlist");
  $("#runs-count").textContent = state.runs.length + " 次";
  if (!state.runs.length) { el.innerHTML = `<div class="empty">还没有运行</div>`; return; }
  el.innerHTML = state.runs.map(r => {
    const failed = r.status === "done" && r.exit_code !== 0;
    const dot = r.status === "running" ? "running" : failed ? "failed" : "done";
    return `<div class="runrow ${r.id === state.selected ? "sel" : ""}" data-id="${r.id}">
      <div class="t"><i class="sdot ${dot}"></i>${r.backend}<span style="color:var(--faint)">/</span>${r.condition}<span style="color:var(--faint)">/</span>s${r.seed}</div>
      <div class="cost mono">${r.cost_usd ? fmtUSD(r.cost_usd) : "—"}</div>
      <div class="meta">${r.id}</div>
      <div class="meta">${r.framing ? (r.framing.passed ? "取景通过" : "取景未过") : ""}</div>
    </div>`;
  }).join("");
  el.querySelectorAll(".runrow").forEach(n =>
    n.onclick = () => { state.selected = n.dataset.id; refresh(); });
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
  if (!d) { el.innerHTML = `<div class="empty">从左侧 take log 选一条</div>`; return; }
  const fr = d.framing;
  const stats = fr ? `
    <div class="stats">
      <div class="stat"><div class="k">垂直 FOV</div>
        <div class="v">${fr.fov_vertical_degrees.toFixed(2)}°</div>
        <div class="hint">焦距 ${fr.lens_mm.toFixed(1)}mm · ${fr.vertices.toLocaleString()} 顶点</div></div>
      <div class="stat"><div class="k">最小安全边距</div>
        <div class="v" style="color:${fr.passed ? "var(--ok)" : "var(--err)"}">${(Math.min(...fr.min_margins_lbrt) * 100).toFixed(1)}%</div>
        <div class="bars">${marginBar("左", fr.min_margins_lbrt[0], fr.plan.margin)}
        ${marginBar("下", fr.min_margins_lbrt[1], fr.plan.margin)}
        ${marginBar("右", fr.min_margins_lbrt[2], fr.plan.margin)}
        ${marginBar("上", fr.min_margins_lbrt[3], fr.plan.margin)}</div></div>
      <div class="stat"><div class="k">参考视频</div>
        <div class="v">${fr.frames}<small>帧</small></div>
        <div class="hint">${(fr.frames / 30).toFixed(1)}s · 渲染 ${fr.render_seconds}s</div></div>
      <div class="stat"><div class="k">侧面顺序</div>
        <div class="v" style="font-size:13.5px; line-height:1.9">${fr.side_sequence.join(" → ")}</div>
        <div class="hint">${fr.plan.direction === "cw" ? "顺时针" : "逆时针"} · 俯视 ${fr.plan.elevation_degrees}°</div></div>
    </div>` : "";
  const videoCard = d.video ? `
    <div class="card"><div class="card-h"><h2>参考视频（粗模环绕）</h2>
      <span class="badge ${fr && fr.passed ? "b-ok" : "b-err"}">${fr && fr.passed ? "取景审计通过" : "取景审计未通过"}</span></div>
      <div class="card-b">
        <div class="monitor"><span class="corners"></span>
          <video class="big" controls preload="metadata" src="${d.video}"></video>
        </div>
        <div class="frames-strip">${(d.frames || []).map(f => `<img src="${f}">`).join("")}</div>
      </div></div>` : "";
  const ledgerRows = (d.ledger || []).map(r => `
    <tr><td>${(r.stage || "")}</td><td>${r.status || ""}</td>
    <td class="r mono">${r.kling_credits ? r.kling_credits + "积分" : r.cost_usd ? fmtUSD(r.cost_usd) : "—"}</td></tr>`).join("");
  el.innerHTML = `
    ${stats}
    <div class="detail-grid">
      <div class="col" style="gap:14px">
        ${videoCard}
        <div class="card"><div class="card-h"><h2>运行日志</h2></div>
          <div class="card-b"><div class="log-box">${(d.log_tail || "（无输出）").split("\n").slice(-14).join("\n")}</div></div></div>
      </div>
      <div class="col" style="gap:14px">
        <div class="card"><div class="card-h"><h2>成片提示词</h2>
          ${d.prompt_text ? '<span class="badge b-ok">已生成</span>' : '<span class="badge b-mut">未生成</span>'}</div>
          <div class="card-b"><div class="prompt-box">${(d.prompt_text || "（取景通过后生成）")}</div></div></div>
        ${d.frozen_request ? `<div class="card"><div class="card-h"><h2>冻结请求</h2>
          <span class="badge ${d.frozen_request.provider === "kling" ? "b-paid" : d.frozen_request.estimated_usd >= 4 ? "b-paid" : "b-warn"}">${d.frozen_request.provider === "kling" ? `${d.frozen_request.model} · ${d.frozen_request.estimated_kling_credits}积分` : `${d.frozen_request.quality} · ${fmtUSD(d.frozen_request.estimated_usd)}`}</span></div>
          <div class="card-b"><table>
            ${d.frozen_request.provider === "kling" ? `
            <tr><td>模型/模式</td><td class="mono">${d.frozen_request.model} · 全能参考（feature_video + 4×refer_image）</td></tr>
            <tr><td>分辨率/时长</td><td class="mono">${d.frozen_request.resolution} · ${d.frozen_request.duration}s · ${d.frozen_request.aspect_ratio} · audio ${d.frozen_request.audio}</td></tr>
            <tr><td>预估扣费</td><td class="mono">${d.frozen_request.estimated_kling_credits} 积分（输入10s+输出10s）</td></tr>` : `
            <tr><td>模式</td><td class="mono">${d.frozen_request.model} / ${d.frozen_request.mode}</td></tr>
            <tr><td>比例/时长</td><td class="mono">${d.frozen_request.ratio} · ${d.frozen_request.duration}s</td></tr>
            <tr><td>预估扣费</td><td class="mono">${d.frozen_request.estimated_credits} credits</td></tr>`}
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

/* ── 表单模式（运行记录视图内）────────── */
$("#f-dry").onchange = (e) => $("#paid-warn").classList.toggle("show", !e.target.checked);

$("#btn-run").onclick = () => {
  const spec = {
    backend: $("#f-backend").value, condition: $("#f-condition").value,
    seed: parseInt($("#f-seed").value) || 4207, dry_run: $("#f-dry").checked,
    ack_paid: false, style: stylePayload(),
  };
  if (!spec.dry_run) { openPayModal(spec); return; }
  launch(spec);
};

let expandedStyle = null;
$("#btn-expand").onclick = async () => {
  const idea = $("#f-style").value.trim();
  if (!idea) { toast("先输入一句风格想法"); return; }
  const btn = $("#btn-expand");
  btn.disabled = true; btn.textContent = "扩写中…";
  try {
    const j = await jpost("/api/expand_prompt", {
      idea, backend: $("#f-backend").value === "mock" ? null : $("#f-backend").value });
    expandedStyle = j;
    const s = j.spec;
    const lines = [
      s.visual_style && `风格: ${s.visual_style}`,
      s.lighting && `灯光: ${s.lighting}`,
      s.background && `背景: ${s.background}`,
      s.mood && `氛围: ${s.mood}`,
      ...(s.extra_notes || []).map(n => `· ${n}`),
    ].filter(Boolean);
    $("#expand-result").textContent = lines.join("\n");
    $("#expand-box").style.display = "block";
    toast("风格块已生成（非镜头维度）");
  } catch (e) { toast("扩写失败：" + e.message); }
  finally { btn.disabled = false; btn.textContent = "AI 扩写"; }
};

function stylePayload() {
  const idea = $("#f-style").value.trim();
  if (!idea) return "";
  if ($("#expand-box").style.display !== "none" && !$("#f-use-style").checked) return "";
  return idea;
}

function openPayModal(spec) {
  const o = state.overview;
  let quote;
  if (o.video_backend === "kling" && o.kling) {
    quote = `Kling <b>${o.kling.model}</b> ${o.kling.resolution} · ${o.kling.duration}s ≈ <b>${o.kling.credits_per_video} 积分</b>/条；` +
      `Tripo 缓存未命中时 $0.20。LLM 已花费 ${fmtUSD(o.budget.spent_usd)} / $${o.budget.max_usd}。`;
  } else {
    const q = o.qualities;
    quote = `Seedance <b>${fmtUSD(q["720p"].usd)}</b>/条(720p) 或 <b>${fmtUSD(q["480p"].usd)}</b>/条(480p)；` +
      `当前已花费 ${fmtUSD(o.budget.spent_usd)} / $${o.budget.max_usd}。`;
  }
  $("#pay-quote").innerHTML = quote;
  $("#pay-overlay").classList.add("show");
  $("#pay-ack").checked = false; $("#pay-go").disabled = true;
  $("#pay-go").onclick = () => { $("#pay-overlay").classList.remove("show"); launch({ ...spec, ack_paid: true }); };
}
$("#pay-ack").onchange = (e) => $("#pay-go").disabled = !e.target.checked;
$("#pay-cancel").onclick = () => $("#pay-overlay").classList.remove("show");

async function launch(spec) {
  try {
    const j = await jpost("/api/runs", spec);
    toast(`已发起 ${j.run_id}（${spec.dry_run ? "dry-run" : "真实扣费"}）`);
    setTimeout(refresh, 800);
  } catch (e) { toast("发起失败：" + e.message); }
}

/* ── 轮询与渲染 ───────────────────────── */
function renderBackendSelect() {
  const o = state.overview; if (!o) return;
  for (const sel of [$("#c-backend"), $("#f-backend")]) {
    const cur = sel.value;
    sel.innerHTML = Object.entries(o.backends).map(([name, b]) =>
      `<option value="${name}">${name === "mock" ? "mock（本地脚本）" : name}</option>`).join("");
    if ([...sel.options].some(op => op.value === cur)) sel.value = cur;
  }
}

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

function renderAll() {
  const sig = JSON.stringify([state.overview, state.runs, state.detail]);
  if (sig === lastSig) return;
  lastSig = sig;
  renderSidebar(); renderBackendSelect(); renderRunlist(); renderFlow(); renderDetail();
}

/* ── 启动 ─────────────────────────────── */
(async function init() {
  await refresh();
  await newSession(true);
  const urlSid = new URLSearchParams(location.search).get("chat");
  if (urlSid) openSession(urlSid);
  setInterval(refresh, 2500);
  setInterval(pollSessions, 5000);
  setInterval(() => { if (state.chat.busy) pollChat(); }, 1000);
})();
