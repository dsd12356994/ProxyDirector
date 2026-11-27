"use strict";

const paths = {
  chat: '<path d="M20 11a7 7 0 0 1-7 7H8l-4 3V7a3 3 0 0 1 3-3h7a6 6 0 0 1 6 7Z"/>',
  frame: '<path d="M8 4H4v4m12-4h4v4M4 16v4h4m12-4v4h-4"/><rect x="8" y="8" width="8" height="8" rx="1"/>',
  film: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M7 5v14m10-14v14M3 10h4m10 0h4M3 14h4m10 0h4"/>',
  history: '<path d="M4 10a8 8 0 1 1 1.5 7M3 5v5h5m4-3v5l3 2"/>',
  panel: '<rect x="3" y="5" width="18" height="14" rx="3"/><path d="M15 5v14"/>',
  menu: '<path d="M4 6h16M4 12h16M4 18h16"/>',
  close: '<path d="m6 6 12 12m0-12L6 18"/>',
  aperture: '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3"/>',
  sliders:
    '<path d="M4 8h7m4 0h5M4 16h3m4 0h9"/><circle cx="13" cy="8" r="2"/><circle cx="9" cy="16" r="2"/>',
  send: '<path d="M12 19V5m-6 6 6-6 6 6"/>',
  play: '<path d="M7 4v16l13-8Z"/>',
  pause: '<path d="M8 5v14m8-14v14"/>',
  arrow: '<path d="M5 12h14m-6-6 6 6-6 6"/>',
};
function icon(name) {
  return (
    '<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round">' +
    paths[name] +
    "</svg>"
  );
}
document
  .querySelectorAll("[data-icon]")
  .forEach((el) => el.insertAdjacentHTML("afterbegin", icon(el.dataset.icon)));

function makeElement(tag, className, content) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (content !== undefined) el.textContent = content;
  return el;
}

function heroNode() {
  const hero = makeElement("div", "chat-hero");
  hero.id = "chat-hero";
  hero.innerHTML =
    '<div class="hero-copy"><div class="project-tag"><i></i> Current project</div>' +
    '<h1>Mouse orbit</h1><p>Build a 10-second orbit around a wired mouse from four reference photos.</p>' +
    '<button class="hero-action" data-open="lab">Open Framing Lab ' +
    icon("arrow") +
    "</button></div>" +
    '<figure class="specimen"><img alt="Front view of the wired mouse" src="/api/views/view_1.png">' +
    '<figcaption><span class="view-caption">Front view</span><div class="view-switch" role="group" aria-label="Choose a reference view"></div></figcaption></figure>' +
    '<div class="briefs"><h2>Try a shot</h2><div class="chips"></div></div>';
  const name = ["Front", "Right", "Back", "Left"];
  const img = hero.querySelector("img");
  name.forEach((title, i) => {
    const b = makeElement("button", "", "0" + (i + 1));
    b.type = "button";
    b.ariaLabel = title + " view";
    b.setAttribute("aria-pressed", i === 0);
    b.onclick = () => {
      img.src = "/api/views/view_" + (i + 1) + ".png";
      img.alt = title + " view of the wired mouse";
      hero.querySelector(".view-caption").textContent = title + " view";
      hero.querySelectorAll(".view-switch button").forEach((x) => x.setAttribute("aria-pressed", x === b));
    };
    hero.querySelector(".view-switch").append(b);
  });
  for (const prompt of ["Standard orbit", "35° elevation", "Counterclockwise orbit"]) {
    const b = makeElement("button", "chip", prompt);
    b.onclick = () => {
      if ($("#btn-send").disabled) return;
      $("#c-input").value = prompt;
      $("#c-input").focus();
    };
    hero.querySelector(".chips").append(b);
  }
  hero.querySelector("[data-open]").onclick = () => goView("lab");
  return hero;
}

const lab = { bounds: null, data: null, revision: 0, videoFrames: 300, fps: 30, started: false };
const pct = (x) => (x * 100).toFixed(2) + "%";
function drawFrame(index) {
  const box = lab.bounds?.[index];
  if (!box) return;
  const zoom = +$("#lab-zoom").value;
  const margin = +$("#lab-margin").value;
  const [l, t, r, b] = box.map((v) => 0.5 + zoom * (v - 0.5));
  Object.assign($("#bounds").style, { left: pct(l), top: pct(t), width: pct(r - l), height: pct(b - t) });
  $("#bounds").classList.toggle("out", Math.min(l, t, 1 - r, 1 - b) < 0);
  $("#safe-line").style.inset = pct(margin);
  $("#lab-video").style.transform = "scale(" + zoom + ")";
  $("#lab-frame").value = index;
  $("#lab-time").textContent = (index / lab.fps).toFixed(2) + " s";
}
async function audit() {
  const revision = ++lab.revision;
  const zoom = +$("#lab-zoom").value,
    margin = +$("#lab-margin").value;
  $("#zoom-value").textContent = zoom.toFixed(2) + "×";
  $("#margin-value").textContent = Math.round(margin * 100) + "%";
  for (const [id, preset] of [["#lab-reset", 1], ["#lab-buffer", 1.08], ["#lab-push", 1.15]])
    $(id).setAttribute("aria-pressed", Math.abs(zoom - preset) < 0.005);
  drawFrame(+$("#lab-frame").value);
  $("#worst").disabled = true;
  try {
    const data = await jget("/api/framing-lab?" + new URLSearchParams({ zoom, margin }));
    if (revision !== lab.revision) return;
    lab.data = data;
    lab.fps = data.fps;
    lab.videoFrames = data.video_frames;
    $("#lab-verdict").textContent = data.cropped
      ? "Subject cropped"
      : data.passed
        ? "Margin holds throughout"
        : "Subject fits, margin too small";
    $("#lab-verdict").dataset.status = data.cropped ? "out" : data.passed ? "ok" : "warn";
    $("#lab-result").textContent =
      `${data.failed_poses} of ${data.pose_count} poses fall below the safety margin. ` +
      `The subject is clipped in ${data.cropped_poses} of them. ` +
      `The smallest margin is ${pct(data.worst.margin)} at the ${data.worst.edge} edge (${data.worst.time_s.toFixed(2)} s).`;
    $("#worst").disabled = false;
    const first = $("#first-crop");
    first.hidden = data.first_cropped_frame === null;
  } catch {
    if (revision !== lab.revision) return;
    lab.data = null;
    $("#lab-verdict").textContent = "Audit unavailable";
    $("#lab-result").textContent = "Reconnect to the local server and reopen the lab.";
    $("#first-crop").hidden = true;
  }
}
function seekPose(index) {
  const video = $("#lab-video");
  video.pause();
  drawFrame(index);
  // Closure has its own audited pose, while its image is the opening frame.
  if (video.readyState >= 1) video.currentTime = (index % lab.videoFrames) / lab.fps + 0.0001;
}
function pauseStudyMedia() {
  document.querySelectorAll(".study video").forEach((v) => v.pause());
}
async function openLab() {
  const video = $("#lab-video");
  if (!lab.started) {
    lab.started = true;
    video.src = "/api/framing-lab/video";
    function onFrame(_, metadata) {
      if (!video.paused) drawFrame(Math.min(lab.videoFrames - 1, Math.round(metadata.mediaTime * lab.fps)));
      video.requestVideoFrameCallback(onFrame);
    }
    if (video.requestVideoFrameCallback) video.requestVideoFrameCallback(onFrame);
    else
      video.ontimeupdate = () => {
        if (!video.paused) drawFrame(Math.min(299, Math.floor(video.currentTime * lab.fps)));
      };
    video.onended = () => seekPose(lab.videoFrames);
    for (const event of ["play", "pause"])
      video.addEventListener(event, () => {
        $("#lab-play").innerHTML = icon(video.paused ? "play" : "pause");
        $("#lab-play").ariaLabel = video.paused ? "Play reference clip" : "Pause reference clip";
      });
    $("#lab-play").onclick = () => {
      if (!video.paused) {
        video.pause();
        return;
      }
      if (+$("#lab-frame").value === lab.videoFrames) seekPose(0);
      video.play().catch(() => toast("Couldn’t play the reference clip. Check the local video file."));
    };
    $("#lab-frame").oninput = (e) => seekPose(+e.target.value);
    $("#lab-zoom").oninput = $("#lab-margin").oninput = audit;
    $("#lab-reset").onclick = () => {
      $("#lab-zoom").value = 1;
      $("#lab-margin").value = 0.05;
      seekPose(0);
      audit();
    };
    $("#lab-buffer").onclick = () => {
      $("#lab-zoom").value = 1.08;
      audit();
    };
    $("#lab-push").onclick = () => {
      $("#lab-zoom").value = 1.15;
      audit();
    };
    $("#worst").onclick = () => {
      if (lab.data) seekPose(lab.data.worst.frame);
    };
    $("#first-crop").onclick = () => {
      if (lab.data?.first_cropped_frame != null) seekPose(lab.data.first_cropped_frame);
    };
  }
  try {
    if (!lab.bounds) lab.bounds = await jget("/api/framing-lab/bounds");
    await audit();
  } catch {
    $("#lab-verdict").textContent = "Couldn’t load the frame bounds. Reopen the lab.";
  }
}

let evidenceData,
  selectedCase = "compare";
function mediaFigure(media, caption) {
  const figure = makeElement("figure", "case-figure");
  if (media) {
    const video = makeElement("video");
    video.controls = true;
    video.preload = "metadata";
    video.muted = true;
    video.playsInline = true;
    video.src = media.url;
    figure.append(video);
  } else {
    figure.append(makeElement("div", "empty", "This video is missing from the local archive."));
  }
  figure.append(makeElement("figcaption", "", caption));
  return figure;
}
function displayCase(id) {
  selectedCase = id;
  const all = evidenceData.cases;
  const data = all.find((c) => c.id === id);
  const compare = id === "compare";
  $("#case-tabs")
    .querySelectorAll("button")
    .forEach((b) => b.setAttribute("aria-pressed", b.dataset.id === id));
  const root = $("#case-detail");
  root.replaceChildren();
  const heading = makeElement("div", "case-heading");
  heading.append(
    makeElement("h2", "", compare ? "Fixed baseline and DeepSeek" : data.title),
    makeElement(
      "p",
      "",
      compare ? "Two archived Kling clips share the same Tripo mesh and orbit settings." : data.subtitle,
    ),
  );
  root.append(heading);
  const films = makeElement("div", "case-films");
  if (compare) {
    films.append(
      mediaFigure(all.find((c) => c.id === "baseline")?.media.output, "Fixed baseline"),
      mediaFigure(all.find((c) => c.id === "deepseek")?.media.output, "DeepSeek director"),
    );
  } else {
    films.append(
      mediaFigure(data.media.reference, "Blender reference"),
      mediaFigure(data.media.output, data.media.output?.label || "Generated clip"),
    );
  }
  root.append(films);
  const videos = [...films.querySelectorAll("video")];
  if (videos.length === 2) {
    const controls = makeElement("div", "paired-controls");
    const play = makeElement("button", "btn", "Play both from start");
    play.onclick = async () => {
      videos.forEach((v) => {
        v.currentTime = 0;
      });
      try {
        await Promise.all(videos.map((v) => v.play()));
      } catch {
        videos.forEach((v) => v.pause());
        toast("The clips are still loading. Try again in a moment.");
      }
    };
    const pause = makeElement("button", "btn ghost", "Pause both");
    pause.onclick = () => videos.forEach((v) => v.pause());
    controls.append(play, pause);
    root.append(controls);
  }
  const facts = makeElement("div", "case-facts");
  const metrics = compare
    ? [
        { label: "Archived clips", value: evidenceData.batch.total },
        ...evidenceData.batch.conditions.map((c) => ({ label: c.label, value: c.count })),
      ]
    : data.metrics;
  for (const m of metrics) {
    const n = makeElement("div");
    n.append(makeElement("span", "", m.label), makeElement("strong", "", m.value));
    facts.append(n);
  }
  root.append(facts);
  const notes = compare
    ? [
        evidenceData.batch.seed_note,
        evidenceData.batch.blind_scoring,
      ]
    : data.findings;
  notes.forEach((n) => root.append(makeElement("p", "case-note", n)));
}
async function openEvidence() {
  if (evidenceData) return;
  try {
    evidenceData = await jget("/api/evidence");
    const tabs = $("#case-tabs");
    tabs.replaceChildren();
    const labels = {
      compare: "Paired outputs",
      "early-tripo": "Early failure",
      compact: "Tighter framing",
      baseline: "Fixed baseline",
      deepseek: "DeepSeek",
    };
    for (const id of ["compare", ...evidenceData.cases.map((c) => c.id)]) {
      const b = makeElement("button", "", labels[id]);
      b.dataset.id = id;
      b.onclick = () => displayCase(id);
      tabs.append(b);
    }
    displayCase(selectedCase);
  } catch {
    evidenceData = null;
    $("#case-detail").textContent = "Couldn’t load the archive. Reconnect to the local server and try again.";
  }
}
