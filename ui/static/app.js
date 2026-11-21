/* ProxyDirector studio client */
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

/* ── Workspace navigation ─────────────────────────── */
const viewTitles = { chat: "Director Chat", lab: "Framing Lab", evidence: "Case Replay", runs: "Run History" };
function switchView(v) {
  if (!(v in viewTitles)) v = "chat";
  state.view = v;
  for (const key of Object.keys(viewTitles)) {
    $("#view-" + key).style.display = v === key ? "grid" : "none";
    const nav = $("#nav-" + key);
    nav.classList.toggle("active", v === key);
    if (v === key) nav.setAttribute("aria-current", "page"); else nav.removeAttribute("aria-current");
  }
  $("#page-title").textContent = viewTitles[v];
  $("#info-toggle").hidden = v !== "chat";
  document.body.classList.remove("nav-open");
  pauseStudyMedia();
  if (v === "lab") openLab();
  if (v === "evidence") openEvidence();
  if (v === "runs") refresh();
  if (v === "chat" && state.chat.sid) pollChat();
  if (location.hash.slice(1) !== v) history.replaceState(null, "", "#" + v);
}
Object.keys(viewTitles).forEach(k => { $("#nav-"+k).onclick = () => switchView(k); });
$("#menu").onclick = () => document.body.classList.toggle("nav-open");
$("#scrim").onclick = () => document.body.classList.remove("nav-open");
function setInspector(open) {
  $("#rail").hidden = !open;
  $("#info-toggle").setAttribute("aria-expanded", open);
  $("#view-chat").classList.toggle("inspect", open);
}
$("#info-toggle").onclick = () => setInspector($("#rail").hidden);
$("#close-info").onclick = () => setInspector(false);
document.addEventListener("keydown", e => {
  if (e.key === "Escape") {
    setInspector(false); document.body.classList.remove("nav-open");
    $("#chat-settings").open = false;
  }
});
function escapeHTML(value) {
  return String(value ?? "").replace(/[&<>"']/g, c => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;" }[c]));
}

/* ── Sidebar ─────────────────────────────── */
function renderSidebar() {
  const o = state.overview; if (!o) return;
  $("#key-status").innerHTML =
    `<span class="statline"><i class="dot ${o.kling_key ? "on" : ""}"></i>Kling${o.video_backend === "kling" ? " · active" : ""}</span>` +
    `<span class="statline"><i class="dot ${o.runway_key ? "on" : ""}"></i>Runway</span>` +
    `<span class="statline"><i class="dot ${o.tripo_key ? "on" : ""}"></i>Tripo</span>`;
  $("#budget-pill").textContent = `Recorded spend ${fmtUSD(o.budget.spent_usd)} / $${o.budget.max_usd.toFixed(0)}`;
  $("#glb-pill").textContent = `GLB ${(o.frozen_glb_sha256 || "").slice(0, 12)}`;
}

function renderSessions(list) {
  const el = $("#sessions");
  el.replaceChildren();
  const entries = (list || []).filter(s => s.n_events || s.busy);
  if (!entries.length) el.append(makeElement("p", "sessions-empty", "No recent chats"));
  for (const session of entries) {
    const b = makeElement("button", "sess", session.title || ((session.backend === "mock" ? "Local presets" : session.backend) + " · " +
      new Date(session.created * 1000).toLocaleTimeString("en-US", {hour:"2-digit",minute:"2-digit"})));
    b.classList.toggle("active", session.sid === state.chat.sid);
    b.onclick = () => openSession(session.sid);
    el.append(b);
  }
}

/* ── Chat ─────────────────────────────── */
const TOOL_LABEL = {
  get_run_context: "Read shot brief", plan_orbit: "Render and check the orbit",
  latest_framing_status: "Inspect framing report", build_seedance_prompt: "Build video prompt",
  submit_seedance: "Prepare video request", stop: "Stop",
};

function resetChat() {
  clearTimeout(pollChat._t);
  state.chat = { sid: null, runId: null, backend: $("#c-backend").value || "mock", seen: 0, busy: false, nodes: new Map() };
  $("#chatlog").replaceChildren(heroNode());
  $("#c-input").value = "";
  $("#rail-runid").textContent = "No run yet";
  $("#rail-video").pause(); $("#rail-video").removeAttribute("src"); $("#rail-video").load(); $("#rail-video").style.display = "none";
  $("#rail-empty").style.display = "block"; $("#rail-stats").textContent = ""; $("#rail-open").style.display = "none";
  $("#c-dry").checked = true; $("#c-ack").checked = false; updateMode();
  $("#senddot").classList.remove("busy"); $("#btn-send").disabled = false;
}
async function newSession() {
  const chat = state.chat;
  const backend = $("#c-backend").value || "mock";
  const seed = Number($("#c-seed").value || 4207);
  if (!Number.isInteger(seed) || seed < 0) throw new Error("Enter a nonnegative integer for the run label.");
  const j = await jpost("/api/chat/new", { backend, seed });
  if (chat !== state.chat) return;
  Object.assign(chat, { sid: j.sid, runId: j.run_id, backend });
  $("#rail-runid").textContent = j.run_id;
}
function updateMode() {
  const local = $("#c-backend").value === "mock";
  const dry = $("#c-dry").checked;
  $("#c-ack").disabled = dry;
  if (dry) $("#c-ack").checked = false;
  const desc = (local ? "Local director" : "Cloud director") + (dry ? " · Preview" : " · Live generation");
  $("#c-input").placeholder = local ? "Try Standard orbit, 35° elevation, or Counterclockwise orbit…" : "Describe the camera angle, orbit direction, and safety margin…";
  $("#compose-mode").textContent = desc;
  $("#rail-mode").textContent = desc;
}
$("#btn-newchat").onclick = () => { resetChat(); switchView("chat"); pollSessions(); $("#c-input").focus(); };
$("#c-backend").onchange = () => {
  if (state.chat.sid) resetChat();
  updateMode();
};
$("#c-seed").onchange = () => { if (state.chat.sid) resetChat(); };
$("#c-dry").onchange = updateMode;

async function openSession(sid) {
  let j;
  try {
    j = await jget(`/api/chat/${sid}/events`);
  } catch (e) {
    // A server restart can clear in-memory chats.
    toast("This chat has expired. A new chat is ready.");
    resetChat();
    return;
  }
  resetChat();
  state.chat = { sid, runId: j.run_id, backend: j.backend, seen: 0, busy: j.busy, nodes: new Map() };
  $("#c-backend").value = j.backend;
  if (j.seed !== undefined) $("#c-seed").value = j.seed;
  updateMode(); $("#rail-runid").textContent = j.run_id;
  switchView("chat");
  updateRail();
  pollSessions();
}

async function send() {
  const text = $("#c-input").value.trim();
  if (!text || state.chat.busy || $("#btn-send").disabled) return;
  const dry = $("#c-dry").checked;
  const ack = $("#c-ack").checked;
  if (!dry && !ack) { toast("Select “Authorize paid generation” or keep “Freeze video request” on."); return; }
  if (!dry) {
    const o = state.overview;
    if (!o) { toast("The studio is loading service settings. Try again in a moment."); return; }
    const q = o.video_backend === "kling" && o.kling
      ? `Kling ${o.kling.model} ${o.kling.duration}s ≈ ${o.kling.credits_per_video} credits per clip`
      : "Runway 480p ≈ $3.00 per clip";
    if (!confirm(`Estimated video charge: ${q}. Authorize this run?`)) return;
  }
  const chat = state.chat;
  $("#btn-send").disabled = true;
  $("#chat-settings").open = false;
  try {
    if (!chat.sid) await newSession();
    if (chat !== state.chat) return;
    await jpost(`/api/chat/${chat.sid}/send`, { text, dry_run: dry, ack_paid: ack });
    if (chat !== state.chat) return;
    $("#c-input").value = "";
    $("#c-ack").checked = false;
    state.chat.busy = true;
    $("#senddot").classList.add("busy");
    pollChat();
  } catch (e) {
    if (chat === state.chat) $("#btn-send").disabled = false;
    toast("Could not send: " + e.message);
  }
}
$("#btn-send").onclick = send;
$("#c-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing && e.keyCode !== 229) { e.preventDefault(); send(); }
});

function upsertNode(ev) {
  const log = $("#chatlog");
  const hero = $("#chat-hero"); if (hero) hero.remove();
  const map = state.chat.nodes;
  if (ev.kind === "user") {
    const n = bubble("user", "You", ev.text); log.appendChild(n.el);
    log.scrollTop = log.scrollHeight;
  } else if (ev.kind === "mode") {
    state.chat.liveTurn = !ev.dry_run;
    const n = document.createElement("div");
    n.className = "modeline-chip";
    n.textContent = ev.dry_run ? (state.chat.backend === "mock" ? "Local director · Preview only" : "Cloud director · Preview only")
      : `Paid video generation authorized`;
    if (!ev.dry_run) n.style.color = "var(--err)";
    log.appendChild(n);
    $("#rail-mode").innerHTML = ev.dry_run
      ? 'Video request will be frozen'
      : '<span class="badge b-err">Live generation</span> Authorized for this turn';
  } else if (ev.kind === "assistant_text") {
    let n = map.get("assistant");
    if (!n) { n = bubble("agent", "DIRECTOR", ""); map.set("assistant", n); log.appendChild(n.el); }
    n.body.textContent = ev.text;
    log.scrollTop = log.scrollHeight;
  } else if (ev.kind === "tool_call") {
    const n = document.createElement("details");
    n.className = "toolcard";
    n.innerHTML = `<summary class="tline"><span class="spin"></span>` +
      `<span class="tname">${escapeHTML(TOOL_LABEL[ev.tool] || ev.tool)}</span>` +
      `<span class="trace-status">Working</span></summary>` +
      `<div class="targs">${escapeHTML(ev.args || "")}</div><div class="tresult"></div>`;
    n.dataset.tool = ev.tool;
    map.set("tc" + ev.id, n); log.appendChild(n);
    log.scrollTop = log.scrollHeight;
  } else if (ev.kind === "tool_result") {
    // Match the most recent outstanding call with the same tool name.
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
      if (ev.tool === "submit_seedance" && state.chat.liveTurn) target.classList.add("paid");
      target.dataset.tool = ev.tool;
      const r = target.querySelector(".tresult");
      target.querySelector(".trace-status").textContent = refused ? "Refused" : "Complete";
      if (refused) target.open = true;
      r.textContent = ev.output || "";
      log.scrollTop = log.scrollHeight;
    }
  } else if (ev.kind === "done") {
    const n = bubble("agent", "Director", ev.output);
    n.el.classList.add("final");
    log.appendChild(n.el);
    map.delete("assistant");
    log.scrollTop = log.scrollHeight;
  } else if (ev.kind === "error") {
    const n = bubble("agent error", "Error", ev.message);
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

async function pollChat() {
  const chat = state.chat;
  if (!chat.sid || chat.polling) return;
  chat.polling = true;
  try {
    const j = await jget(`/api/chat/${chat.sid}/events?since=${chat.seen}`);
    if (chat !== state.chat) return;
    for (const ev of j.events) upsertNode(ev);
    chat.seen += j.events.length;
    chat.busy = j.busy;
    $("#btn-send").disabled = j.busy;
    $("#senddot").classList.toggle("busy", j.busy);
    if (!j.busy && j.events.length) { refresh(); updateRail(); pollSessions(); }
  } catch { /* Retry a running session on its next poll. */ }
  finally { chat.polling = false; }
  if (chat === state.chat && chat.busy) {
    clearTimeout(pollChat._t); pollChat._t = setTimeout(pollChat, 750);
  }
}

async function updateRail() {
  if (!state.chat.runId) return;
  try {
    const d = await jget(`/api/runs/${state.chat.runId}`);
    if (d.id !== state.chat.runId) return;
    if (d.video) {
      if ($("#rail-video").getAttribute("src") !== d.video) $("#rail-video").src = d.video;
      $("#rail-video").style.display = "block";
      $("#rail-empty").style.display = "none";
      const final = [...document.querySelectorAll("#chatlog .final")].pop();
      if (final && !final.querySelector("button")) {
        const preview = makeElement("button", "hero-action preview-action", "View latest reference clip");
        preview.onclick = () => setInspector(true);
        final.append(preview);
      }
    }
    const fr = d.framing;
    $("#rail-stats").textContent = fr
      ? `FOV ${fr.fov_vertical_degrees.toFixed(1)}° · Min. margin ${(Math.min(...fr.min_margins_lbrt) * 100).toFixed(1)}%\n` +
        `${fr.plan.elevation_degrees}°  r${fr.plan.radius}  ${fr.plan.direction === "cw" ? "Clockwise" : "Counterclockwise"}\n` +
        `Recorded USD spend ${fmtUSD(d.cost_usd)}`
      : "";
    $("#rail-runid").textContent = d.id;
    const btn = $("#rail-open"); btn.style.display = "block";
    btn.onclick = () => { state.selected = d.id; switchView("runs"); refresh(); };
  } catch { /* The run may still be preparing its first render. */ }
}

async function pollSessions() {
  try { renderSessions(await jget("/api/chat/sessions")); } catch { }
}

/* ── Run stage indicators──────────── */
const FLOW = [
  { k: "llm_plan", label: "Shot plan", n: 1 },
  { k: "frozen", label: "Reference photos", n: 2 },
  { k: "tripo_model", label: "Tripo model", n: 3 },
  { k: "glb_check", label: "Check geometry", n: 4 },
  { k: "blender_render", label: "Render and audit", n: 5, loop: true },
  { k: "gate", label: "Approval", gate: true },
  { k: "prompt", label: "Video prompt", n: 6, paid: true },
  { k: "seedance", label: "Generate video", n: 7, paid: true },
  { k: "ledger", label: "Cost log", n: 8, paid: true },
];

function nodeState(node, r) {
  if (!r) return "";
  const st = r.stages || {};
  const running = r.status === "running";
  switch (node.k) {
    case "llm_plan": return st.llm || st.blender_render ? "done" : "";
    case "frozen": case "glb_check": return st.tripo_model ? "done" : "";
    case "ledger": return Object.keys(st).length ? "done" : "";
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

const runLabels = {running:"Running", ready:"Video archived", dry_run:"Request frozen", failed:"Failed", stopped:"Stopped", partial:"Partial results", idle:"Idle"};
function renderFlow() {
  const r = state.runs.find(x => x.id === state.selected) || null;
  $("#flow-note").textContent = r
    ? (runLabels[r.status] || r.status)
    : "No run selected";
  const nodeHtml = node => {
    const s = nodeState(node, r);
    const cls = s === "paid-done" ? "paid-done" : s;
    const loop = node.loop ? ' <span style="color:var(--faint)" title="Adjust the plan and render again if the framing check fails.">↺</span>' : "";
    return `<div class="fnode ${cls}"><span class="n">${node.n}</span>${node.label}${loop}</div>`;
  };
  $("#flow").innerHTML =
    FLOW.filter(n => !n.gate && !n.paid).map(nodeHtml).join("") +
    `<div class="fgate"><span class="g">⏸</span>Approval</div>` +
    FLOW.filter(n => n.paid).map(nodeHtml).join("");
}

/* ── Run History ─────────────────────────── */
function renderRunlist() {
  const el = $("#runlist");
  $("#runs-count").textContent = state.runs.length + (state.runs.length === 1 ? " run" : " runs");
  if (!state.runs.length) { el.innerHTML = `<div class="empty">No runs yet.</div>`; return; }
  el.innerHTML = state.runs.map(r => {
    const failed = r.status === "failed" || r.status === "stopped";
    const dot = r.status === "running" ? "running" : failed ? "failed" : ["ready","dry_run"].includes(r.status) ? "done" : "";
    return `<div class="runrow ${r.id === state.selected ? "sel" : ""}" data-id="${r.id}">
      <div class="t"><i class="sdot ${dot}"></i>${r.backend}<span style="color:var(--faint)">/</span>${r.condition}<span style="color:var(--faint)">/</span>s${r.seed}</div>
      <div class="cost mono">${r.cost_usd ? fmtUSD(r.cost_usd) : "—"}</div>
      <div class="meta">${r.id}</div>
      <div class="meta">${runLabels[r.status] || ""}</div>
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

function archiveText(text, emptyMessage) {
  if (!text) return escapeHTML(emptyMessage);
  if (!/\p{Script=Han}/u.test(text)) return escapeHTML(text);
  return `<details class="source-text"><summary>View original archived text</summary><div>${escapeHTML(text)}</div></details>`;
}

function renderDetail() {
  const el = $("#detail");
  const d = state.detail;
  if (!d) { el.innerHTML = `<div class="empty">Select a run to inspect its results.</div>`; return; }
  const fr = d.framing;
  const stats = fr ? `
    <div class="stats">
      <div class="stat"><div class="k">Vertical field of view</div>
        <div class="v">${fr.fov_vertical_degrees.toFixed(2)}°</div>
        <div class="hint">Lens ${fr.lens_mm.toFixed(1)} mm · ${fr.vertices.toLocaleString("en-US")} vertices</div></div>
      <div class="stat"><div class="k">Minimum margin</div>
        <div class="v" style="color:${fr.passed ? "var(--ok)" : "var(--err)"}">${(Math.min(...fr.min_margins_lbrt) * 100).toFixed(1)}%</div>
        <div class="bars">${marginBar("L", fr.min_margins_lbrt[0], fr.plan.margin)}
        ${marginBar("B", fr.min_margins_lbrt[1], fr.plan.margin)}
        ${marginBar("R", fr.min_margins_lbrt[2], fr.plan.margin)}
        ${marginBar("T", fr.min_margins_lbrt[3], fr.plan.margin)}</div></div>
      <div class="stat"><div class="k">Reference clip</div>
        <div class="v">${fr.frames}<small> frames</small></div>
        <div class="hint">${(fr.frames / 30).toFixed(1)} s · Render time ${fr.render_seconds}s</div></div>
      <div class="stat"><div class="k">View order</div>
        <div class="v" style="font-size:13.5px; line-height:1.9">${fr.side_sequence.join(" → ")}</div>
        <div class="hint">${fr.plan.direction === "cw" ? "Clockwise" : "Counterclockwise"} · Elevation ${fr.plan.elevation_degrees}°</div></div>
    </div>` : "";
  const videoCard = d.video ? `
    <div class="card"><div class="card-h"><h2>Reference orbit</h2>
      <span class="badge ${fr && fr.passed ? "b-ok" : "b-err"}">${fr && fr.passed ? "Framing passed" : "Framing failed"}</span></div>
      <div class="card-b">
        <div class="monitor"><span class="corners"></span>
          <video class="big" controls preload="metadata" src="${d.video}"></video>
        </div>
        <div class="frames-strip">${(d.frames || []).map(f => `<img src="${f}">`).join("")}</div>
      </div></div>` : "";
  const ledgerRows = (d.ledger || []).map(r => `
    <tr><td>${escapeHTML(r.stage)}</td><td>${escapeHTML(r.status)}</td>
    <td class="r mono">${r.kling_credits ? "Est. " + r.kling_credits + " credits" : r.cost_usd ? fmtUSD(r.cost_usd) : "—"}</td></tr>`).join("");
  el.innerHTML = `
    ${stats}
    <div class="detail-grid">
      <div class="col" style="gap:14px">
        ${videoCard}
        ${d.output_video ? `<div class="card"><div class="card-h"><h2>Archived output</h2></div><div class="card-b"><video class="big" controls playsinline preload="metadata" src="${escapeHTML(d.output_video)}"></video></div></div>` : ""}
        <div class="card"><div class="card-h"><h2>Run log</h2></div>
          <div class="card-b"><div class="log-box">${archiveText((d.log_tail || "").split("\n").slice(-14).join("\n"), "No log output.")}</div></div></div>
      </div>
      <div class="col" style="gap:14px">
        <div class="card"><div class="card-h"><h2>Video prompt</h2>
          ${d.prompt_text ? '<span class="badge b-ok">Ready</span>' : '<span class="badge b-mut">Pending</span>'}</div>
          <div class="card-b"><div class="prompt-box">${archiveText(d.prompt_text, "Available after a passing framing check.")}</div></div></div>
        ${d.frozen_request ? `<div class="card"><div class="card-h"><h2>Frozen request</h2>
          <span class="badge ${d.frozen_request.provider === "kling" ? "b-paid" : d.frozen_request.estimated_usd >= 4 ? "b-paid" : "b-warn"}">${d.frozen_request.provider === "kling" ? `${d.frozen_request.model} · ${d.frozen_request.estimated_kling_credits} credits` : `${d.frozen_request.quality} · ${fmtUSD(d.frozen_request.estimated_usd)}`}</span></div>
          <div class="card-b"><table>
            ${d.frozen_request.provider === "kling" ? `
            <tr><td>Model / mode</td><td class="mono">${d.frozen_request.model} · Omni Reference (1 video and 4 images)</td></tr>
            <tr><td>Resolution / duration</td><td class="mono">${d.frozen_request.resolution} · ${d.frozen_request.duration}s · ${d.frozen_request.aspect_ratio} · audio ${d.frozen_request.audio}</td></tr>
            <tr><td>Estimate</td><td class="mono">${d.frozen_request.estimated_kling_credits} credits (10 s input and 10 s output)</td></tr>` : `
            <tr><td>Mode</td><td class="mono">${d.frozen_request.model} / ${d.frozen_request.mode}</td></tr>
            <tr><td>Aspect ratio / duration</td><td class="mono">${d.frozen_request.ratio} · ${d.frozen_request.duration}s</td></tr>
            <tr><td>Estimate</td><td class="mono">${d.frozen_request.estimated_credits} credits</td></tr>`}
            <tr><td>Request hash</td><td class="mono" style="font-size:10.5px">${d.frozen_request.request_sha256.slice(0, 24)}…</td></tr>
          </table></div></div>` : ""}
        <div class="card"><div class="card-h"><h2>Run cost log</h2>
          <span class="badge b-mut mono">USD spend ${fmtUSD(d.cost_usd)}</span></div>
          <div class="card-b" style="padding:0">
            <table><tr><th>Stage</th><th>Status</th><th class="r">Cost</th></tr>${ledgerRows}</table>
          </div></div>
      </div>
    </div>`;
}

/* ── Comparison run form────────── */
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
  if (!idea) { toast("Enter a style idea first."); return; }
  const btn = $("#btn-expand");
  btn.disabled = true; btn.textContent = "Expanding…";
  try {
    const j = await jpost("/api/expand_prompt", {
      idea, backend: $("#f-backend").value === "mock" ? null : $("#f-backend").value });
    expandedStyle = j;
    const s = j.spec;
    const lines = [
      s.visual_style && `Style: ${s.visual_style}`,
      s.lighting && `Lighting: ${s.lighting}`,
      s.background && `Background: ${s.background}`,
      s.mood && `Mood: ${s.mood}`,
      ...(s.extra_notes || []).map(n => `· ${n}`),
    ].filter(Boolean);
    $("#expand-result").textContent = lines.join("\n");
    $("#expand-box").style.display = "block";
    toast("Appearance settings are ready.");
  } catch (e) { toast("Style expansion failed: " + e.message); }
  finally { btn.disabled = false; btn.textContent = "Expand with AI"; }
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
    quote = `Kling <b>${o.kling.model}</b> ${o.kling.resolution} · ${o.kling.duration}s ≈ <b>${o.kling.credits_per_video} credits</b> per clip. ` +
      `Tripo is estimated at $0.20 on a cache miss. Recorded USD spend ${fmtUSD(o.budget.spent_usd)} / $${o.budget.max_usd}.`;
  } else {
    const q = o.qualities;
    quote = `Seedance <b>${fmtUSD(q["720p"].usd)}</b> per 720p clip or <b>${fmtUSD(q["480p"].usd)}</b> per 480p clip. ` +
      `Recorded USD spend ${fmtUSD(o.budget.spent_usd)} / $${o.budget.max_usd}.`;
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
    state.selected = j.run_id;
    toast(`Started ${j.run_id} (${spec.dry_run ? "preview" : "paid generation"})`);
    setTimeout(refresh, 800);
  } catch (e) { toast("Could not start run: " + e.message); }
}

/* ── Refresh data and views ───────────────────────── */
function renderBackendSelect() {
  const o = state.overview; if (!o) return;
  for (const sel of [$("#c-backend"), $("#f-backend")]) {
    const cur = sel.value;
    sel.innerHTML = Object.entries(o.backends).map(([name, b]) =>
      `<option value="${name}">${name === "mock" ? "Local presets" : name + (b.has_key ? "" : " (key missing)")}</option>`).join("");
    if ([...sel.options].some(op => op.value === cur)) sel.value = cur;
  }
}

async function refresh() {
  if (refresh.busy) { refresh.again = true; return; }
  refresh.busy = true;
  try {
    const [overview, runs] = await Promise.all([jget("/api/overview"), jget("/api/runs")]);
    state.overview = overview; state.runs = runs;
    if (!state.selected && runs.length && state.view === "runs") state.selected = runs[0].id;
    if (state.selected && state.view === "runs") {
      const selected = state.selected;
      const detail = await jget("/api/runs/" + encodeURIComponent(selected));
      if (state.selected === selected) state.detail = detail;
    }
    renderSidebar();
    const optionSig = JSON.stringify(overview.backends);
    if (optionSig !== refresh.options) { renderBackendSelect(); refresh.options = optionSig; }
    const sig = JSON.stringify([runs, state.selected, state.detail]);
    if (state.view === "runs" && sig !== lastSig) {
      lastSig = sig; renderRunlist(); renderFlow(); renderDetail();
    }
  } catch (e) { console.warn(e); }
  finally {
    refresh.busy = false;
    if (refresh.again) { refresh.again = false; refresh(); }
  }
}
(async function init() {
  resetChat();
  switchView(location.hash.slice(1) || "chat");
  await refresh();
  const urlSid = new URLSearchParams(location.search).get("chat");
  if (urlSid) await openSession(urlSid);
  pollSessions();
  setInterval(() => { if (!document.hidden && state.view === "runs") refresh(); }, 4000);
  setInterval(() => { if (!document.hidden && state.view === "chat") pollSessions(); }, 6000);
})();
