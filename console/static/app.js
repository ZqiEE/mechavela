"use strict";
// RLSOK console. Talks only to the local server (127.0.0.1); every POST carries X-RLSOK (CSRF guard).

const TXT = window.RLSOK_TEXT;
let lang = "zh";
try { lang = localStorage.getItem("rlsok.lang") || (navigator.language.startsWith("zh") ? "zh" : "en"); } catch (e) { /* storage blocked */ }
if (!TXT[lang]) lang = "en";
const T = (k, vars) => {
  let s = k.split(".").reduce((o, p) => (o ? o[p] : undefined), TXT[lang]);
  if (s === undefined) s = k.split(".").reduce((o, p) => (o ? o[p] : undefined), TXT.en) ?? k;
  if (vars && typeof s === "string") for (const [a, b] of Object.entries(vars)) s = s.replace(`{${a}}`, b);
  return s;
};
const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fileUrl = (p) => "/file?path=" + encodeURIComponent(p);
const fmt = (x) => (typeof x !== "number" ? esc(x ?? "—") : Math.abs(x) >= 1e4 || (x !== 0 && Math.abs(x) < 1e-3) ? x.toExponential(3) : String(+x.toFixed(4)));
async function api(path, body) {
  const opts = body === undefined ? {} : { method: "POST", body: JSON.stringify(body), headers: { "X-RLSOK": "1", "Content-Type": "application/json" } };
  const r = await fetch(path, opts);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}
function toast(msg) { const t = $("#toast"); t.textContent = msg; t.hidden = false; clearTimeout(toast.t); toast.t = setTimeout(() => { t.hidden = true; }, 3500); }

const PIPES = [["", "p_auto"], ["circuit", "p_circuit"], ["bird", "p_bird"], ["robot_bench:g1_velocity_flat", "p_g1"], ["robot_bench:open_duck_mini_v2", "p_duck"], ["train_measure", "p_train"]];
let pipeChoice = "", selected = null, tab = "robots", evidence = null, lastRoute = null, settings = null, sysinfo = null, exData = null;

// ------------------------------------------------------------------ i18n
function applyI18n() {
  document.documentElement.lang = lang === "zh" ? "zh-CN" : "en";
  document.querySelectorAll("[data-t]").forEach((e) => { e.textContent = T(e.dataset.t); });
  $("#sentence").placeholder = T("askPh");
  $("#lang").textContent = lang === "zh" ? "EN" : "中文";
  $("#pipe-pick").innerHTML = PIPES.map(([v, k]) => `<option value="${v}" ${v === pipeChoice ? "selected" : ""}>${esc(T(k))}</option>`).join("");
  $("#pipe-pick").setAttribute("aria-label", T("routePick"));
  $("#welcome-skip") && ($("#welcome-skip").textContent = T("wSkip"));
}

// ------------------------------------------------------------------ tips (dismissable, remembered)
function tips() {
  let gone = {};
  try { gone = JSON.parse(localStorage.getItem("rlsok.tips") || "{}"); } catch (e) { /* ignore */ }
  document.querySelectorAll("[data-tip]").forEach((el) => {
    el.hidden = !!gone[el.dataset.tip];
    el.querySelector(".x")?.addEventListener("click", () => {
      el.hidden = true; gone[el.dataset.tip] = 1;
      try { localStorage.setItem("rlsok.tips", JSON.stringify(gone)); } catch (e) { /* ignore */ }
    });
  });
}

// ------------------------------------------------------------------ first open: one question (UX_SPEC 7.1)
let exReady = Promise.resolve();
function openWelcome() { $("#welcome").hidden = false; }
async function welcomePick(w) {
  $("#welcome").hidden = true;
  await api("/api/settings", { onboarded: true }).catch(() => {});
  await exReady;                         // the welcome opens before the example list has loaded
  if (w === "bird") {
    const e = exData?.examples.find((x) => x.id === "bird_r2");
    if (e) { selected = "replay:" + e.replay; lastSig = ""; await refreshJobs(); $("#detail").scrollIntoView({ block: "start" }); }
  } else if (w === "circuit") {
    $("#sentence").value = T("w2Ex"); updateRoute();
    if (!exData?.ai_ready) openSetup(0); else $("#sentence").focus();
  } else if (w === "robot") {
    selected = "duck:"; lastSig = ""; await refreshJobs(); $("#detail").scrollIntoView({ block: "start" });
  }
}

// ------------------------------------------------------------------ settings (AI / PC / training), opened when needed
let step = 0;
function openSetup(at = 0) { step = at; $("#setup").hidden = false; renderSetup(); }
function closeSetup() { $("#setup").hidden = true; }
async function renderSetup() {
  settings = await api("/api/settings").catch(() => settings);
  const steps = [T("sAi"), T("sPc"), T("sTrain")];
  $("#setup-steps").innerHTML = steps.map((s, i) => `<li class="${i === step ? "on" : i < step ? "done" : ""}"><span>${i + 1}</span>${esc(s)}</li>`).join("");
  $("#setup-back").hidden = step === 0;
  $("#setup-next").textContent = step === 2 ? T("done") : T("next");
  const body = $("#setup-body");
  if (step === 0) { detected = (await api("/api/ai/detect").catch(() => null))?.clis || []; body.innerHTML = setupAi(); }
  if (step === 1) { body.innerHTML = `<p class="note">${esc(T("running"))}</p>`; sysinfo = await api("/api/system").catch(() => null); body.innerHTML = setupPc(); }
  if (step === 2) { if (!sysinfo) sysinfo = await api("/api/system").catch(() => null); body.innerHTML = setupTrain(); }
  bindSetup();
}
function card(group, value, title, desc, checked) {
  return `<label class="choice ${checked ? "on" : ""}"><input type="radio" name="${group}" value="${value}" ${checked ? "checked" : ""}><b>${esc(title)}</b><span>${esc(desc)}</span></label>`;
}
// command-line AIs found on this PC (Claude Code, Codex): offered first, because they need no API key
let detected = [];
const cliName = (id) => T("cliName." + id);
function setupAi() {
  const a = settings.ai, p = a.provider || (detected.length ? "command" : "anthropic");
  const found = detected.length ? `<div class="callout ok"><h4>${esc(T("aiFound", { n: detected.map((c) => cliName(c.id)).join(", ") }))}</h4>
    <p class="note">${esc(T("aiFoundD"))}</p><div class="btns">${detected.map((c) => `<button class="btn primary" type="button" data-cli="${esc(c.id)}">${esc(T("aiUse", { n: cliName(c.id) }))}</button>`).join("")}</div></div>` : "";
  return `<h2 id="setup-title">${esc(T("sAi"))}</h2><p class="note">${esc(T("aiIntro"))}</p>${found}
    <div class="choices">${card("prov", "anthropic", T("aiClaude"), T("aiClaudeD"), p === "anthropic")}${card("prov", "openai", T("aiOpenai"), T("aiOpenaiD"), p === "openai")}
      ${card("prov", "command", T("aiCmd"), T("aiCmdD"), p === "command")}${card("prov", "exchange", T("aiPaste"), T("aiPasteD"), p === "exchange")}</div>
    <div class="fields" id="ai-fields"></div>`;
}
function aiFields(p) {
  const a = settings.ai;
  if (p === "anthropic" || p === "openai") {
    const models = null; // Exact model IDs come from the user's provider account.
    return `<label>${esc(T("key"))}<input id="ai-key" type="password" autocomplete="off" placeholder="${a.has_key && a.provider === p ? "••••••••" : ""}"></label>
      <label>${esc(T("model"))}${models ? `<select id="ai-model">${models.map((m) => `<option ${m === a.model ? "selected" : ""}>${m}</option>`).join("")}</select>` : `<input id="ai-model" type="text" value="${esc(a.model || "")}">`}</label>
      ${p === "openai" ? `<label>${esc(T("baseUrl"))}<input id="ai-base" type="text" value="${esc(a.base_url || "")}" placeholder="https://api.openai.com/v1"></label>` : ""}
      <div class="btns"><button id="ai-test" class="btn" type="button">${esc(T("test"))}</button><span id="ai-msg" class="note"></span></div>
      <p class="note">${esc(T("keyHelp"))} ${esc(T("keyLinks"))}</p>
      <p class="keynote">${esc(T("keyNote", { path: settings.secrets_file }))}</p>`;
  }
  if (p === "command") return `<label>${esc(T("cmd"))}<input id="ai-cmd" type="text" value="${esc(a.command || detected[0]?.command || "")}" placeholder="claude -p --output-format json"></label>
      <div class="btns"><button id="ai-test" class="btn" type="button">${esc(T("test"))}</button><span id="ai-msg" class="note"></span></div>`;
  return "";
}
function capRow(c) {
  const [title, desc] = T("cap." + c.id);
  const fix = c.fix && !c.ok ? `<button class="btn" type="button" data-fix="${c.fix}">${esc(T("fix." + c.fix))}</button>` : "";
  return `<div class="cap ${c.ok ? "ok" : ""}"><span class="mark">${c.ok ? "✓" : "—"}</span><div><b>${esc(title)}</b><p>${esc(c.ok ? desc : T("why." + c.why) || desc)}</p></div>${fix}</div>`;
}
function setupPc() {
  if (!sysinfo) return `<p class="note">—</p>`;
  const s = sysinfo, g = s.gpu;
  return `<h2 id="setup-title">${esc(T("sPc"))}</h2><p class="note">${esc(T("pcIntro"))}</p>
    <div class="bigrow"><div>${esc(T("gpu"))}<b class="sm">${esc(g.present ? `${g.name.replace("NVIDIA GeForce ", "")} · ${g.vram_gb} GB` : T("gpuNone"))}</b></div>
      <div>${esc(T("ram"))}<b>${esc(s.ram_gb)} GB</b></div><div>${esc(T("disk"))}<b>${esc(s.disk_free_gb)} GB</b></div><div>${esc(T("cpu"))}<b class="sm">${esc(s.cpu)}</b></div></div>
    <div class="caps-list">${s.can.map(capRow).join("")}</div>
    <button id="pc-refresh" class="btn ghost" type="button">${esc(T("pcRefresh"))}</button>`;
}
function setupTrain() {
  const t = settings.train, w = t.where || (sysinfo?.can.find((c) => c.id === "train_local")?.ok ? "local" : "cpu"), c = t.cloud;
  return `<h2 id="setup-title">${esc(T("sTrain"))}</h2><p class="note">${esc(T("trainIntro"))}</p>
    <div class="choices">${card("where", "local", T("tLocal"), T("tLocalD"), w === "local")}${card("where", "cloud", T("tCloud"), T("tCloudD"), w === "cloud")}${card("where", "cpu", T("tCpu"), T("tCpuD"), w === "cpu")}</div>
    <div class="fields" id="cloud-fields" ${w === "cloud" ? "" : "hidden"}>
      <p class="keynote">${esc(T("cloudGuide"))}</p><span class="warnbox">${esc(T("cloudBeta"))}</span>
      <label>${esc(T("cloudHost"))}<input id="c-host" type="text" value="${esc(c.host)}" placeholder="203.0.113.10"></label>
      <div class="row2"><label>${esc(T("cloudUser"))}<input id="c-user" type="text" value="${esc(c.user)}"></label><label>${esc(T("cloudPort"))}<input id="c-port" type="text" value="${esc(c.port)}"></label></div>
      <label>${esc(T("cloudKey"))}<input id="c-key" type="text" value="${esc(c.key_path)}" placeholder="C:\\Users\\you\\.ssh\\id_ed25519"></label>
      <div class="btns"><button id="c-test" class="btn" type="button">${esc(T("test"))}</button><span id="c-msg" class="note"></span></div></div>`;
}
async function saveAiFromForm() {
  const p = document.querySelector("input[name=prov]:checked")?.value;
  if (!p) return;
  const patch = { ai: { provider: p } };
  if ($("#ai-model")) patch.ai.model = $("#ai-model").value.trim();
  if ($("#ai-base")) patch.ai.base_url = $("#ai-base").value.trim();
  if ($("#ai-cmd")) patch.ai.command = $("#ai-cmd").value.trim();
  await api("/api/settings", patch);
  const key = $("#ai-key")?.value.trim();
  if (key) { await api("/api/secret", { name: p, value: key }); $("#ai-key").value = ""; }
}
function errText(code, detail) { const e = T("err." + code); return (Array.isArray(e) ? e[0] + " " + e[2] : code) + (detail ? " — " + detail : ""); }
function bindSetup() {
  const prov = () => document.querySelector("input[name=prov]:checked")?.value;
  if ($("#ai-fields")) { $("#ai-fields").innerHTML = aiFields(prov()); }
  document.querySelectorAll("input[name=prov]").forEach((r) => r.addEventListener("change", () => {
    document.querySelectorAll(".choice").forEach((c) => c.classList.toggle("on", c.querySelector("input").checked));
    $("#ai-fields").innerHTML = aiFields(prov()); bindSetup();
  }));
  $("#ai-test")?.addEventListener("click", async () => {
    $("#ai-msg").textContent = T("testing");
    try { await saveAiFromForm(); const r = await api("/api/ai/test", {}); $("#ai-msg").textContent = r.ok ? "✓ " + T("testOk") : errText(r.code, r.detail); }
    catch (e) { $("#ai-msg").textContent = e.message; }
  });
  document.querySelectorAll("[data-cli]:not([data-bound])").forEach((b) => {
    b.dataset.bound = "1";               // bindSetup runs again when the provider changes
    b.addEventListener("click", async () => {
      const c = detected.find((x) => x.id === b.dataset.cli);
      await api("/api/settings", { ai: { provider: "command", command: c.command } }).catch(() => {});
      await renderSetup();
      $("#ai-test")?.click();            // a real round trip right away, so the user sees it works
    });
  });
  document.querySelectorAll("input[name=where]").forEach((r) => r.addEventListener("change", () => {
    document.querySelectorAll(".choice").forEach((c) => c.classList.toggle("on", c.querySelector("input").checked));
    $("#cloud-fields").hidden = r.value !== "cloud";
  }));
  $("#c-test")?.addEventListener("click", async () => {
    $("#c-msg").textContent = T("testing");
    await api("/api/settings", { train: { cloud: { host: $("#c-host").value.trim(), user: $("#c-user").value.trim(), port: +$("#c-port").value || 22, key_path: $("#c-key").value.trim() } } });
    const r = await api("/api/cloud/test", {}).catch((e) => ({ ok: false, code: "cloud_ssh_failed", detail: e.message }));
    $("#c-msg").textContent = r.ok ? `✓ ${r.gpu}` : errText(r.code);
  });
  $("#pc-refresh")?.addEventListener("click", async () => { sysinfo = await api("/api/system?refresh=1").catch(() => sysinfo); $("#setup-body").innerHTML = setupPc(); bindSetup(); });
  document.querySelectorAll("[data-fix]").forEach((b) => b.addEventListener("click", () => doAction(b.dataset.fix)));
}
async function setupNext() {
  if (step === 0) await saveAiFromForm().catch(() => {});
  if (step === 2) {
    const w = document.querySelector("input[name=where]:checked")?.value || "cpu";
    const patch = { train: { where: w }, onboarded: true };
    if (w === "cloud") patch.train.cloud = { host: $("#c-host").value.trim(), user: $("#c-user").value.trim(), port: +$("#c-port").value || 22, key_path: $("#c-key").value.trim() };
    await api("/api/settings", patch);
    closeSetup(); loadCaps(); renderExamples(); return;
  }
  step += 1; renderSetup();
}

// ------------------------------------------------------------------ one-click actions
async function doAction(a) {
  if (a === "connect_ai") return openSetup(0);
  if (a === "sac_help") { await api("/api/open", { what: "sac_help" }).catch(() => {}); return; }
  if (a === "reinstall") { window.open("https://rlsok.com/#download", "_blank", "noopener"); return; }
  if (a === "copy_log") {
    const j = selected && !selected.includes(":") ? await api("/api/jobs/" + selected).catch(() => null) : null;
    const text = j ? `RLSOK job ${j.id} (${j.pipeline})\n${JSON.stringify(j.error)}\n${j.log_tail || ""}` : "";
    try { await navigator.clipboard.writeText(text); toast(T("copied")); } catch (e) { toast(text.slice(-200)); }
    return;
  }
  if (a === "open_setup" || a === "use_cloud" || a === "cloud") return openSetup(a === "connect_ai" ? 0 : 2);
  if (a === "retry") { const j = selected && !selected.startsWith("run:") ? await api("/api/jobs/" + selected).catch(() => null) : null; if (j) return startJob(j.pipeline, j.sentence, j.opts); return; }
  if (["install_train", "install_mjlab", "install_cad", "install_wsl", "duck_assets", "train_measure", "train_full"].includes(a)) {
    const j = await api("/api/install", { what: a }); selected = j.id; lastSig = ""; closeSetup(); refreshJobs(); $("#detail").scrollIntoView({ behavior: "smooth" });
  }
}

// ------------------------------------------------------------------ examples
async function renderExamples() {
  exData = await api("/api/examples").catch(() => null);
  const box = $("#ex-cards");
  if (!exData) { box.innerHTML = ""; return; }
  const manual = settings?.ai?.provider === "exchange";
  const order = ["bird_r2", "buck", "duck"];                       // at most three on the home page, the bird first
  const shown = order.map((id) => exData.examples.find((e) => e.id === id)).filter(Boolean);
  box.innerHTML = shown.map((e) => {
    const [title, desc] = T("ex." + e.id);
    const assets = e.needs.includes("duck_assets"), aiEx = e.needs.includes("ai");
    // the main button ("see the result") never needs setup; say what running it yourself needs
    const runNeed = assets ? T("ex.duck")[2] : aiEx && !exData.ai_ready ? T("exRunNeedAi") : "";
    const need = (e.replay ? T("exLookFree") : "") + (runNeed ? (e.replay ? T("sep") : "") + runNeed : "");
    return `<article class="ex" data-id="${e.id}"><canvas class="thumb" data-thumb="${e.id}" aria-hidden="true"></canvas>
      <div class="ex-body"><h3>${esc(title)}</h3><p>${esc(desc)}</p>
      <div class="ex-meta"><span class="tagline">${esc(need)}</span></div>
      <div class="btns">${e.replay ? `<button class="btn primary" data-replay="${e.id}">${esc(T("exLook"))}</button>` : ""}
        ${assets ? `<button class="btn" data-act="duck_assets">${esc(T("getDuck"))}</button>` : `<button class="btn" data-run="${e.id}">${esc(T("exRun"))}</button>`}</div></div></article>`;
  }).join("");
  box.querySelectorAll("[data-run]").forEach((b) => b.addEventListener("click", async () => {
    b.disabled = true; b.innerHTML = `<span class="spinner"></span> ${esc(T("starting"))}`;
    const r = await api("/api/examples/run", { id: b.dataset.run }).catch((e) => ({ error: e.message }));
    b.disabled = false; renderExamples();
    if (r.blocked) return handleBlocked(r);
    if (r.error) return toast(r.error);
    if (r.id) { toast(T("started")); selected = r.id; lastSig = ""; await refreshJobs(); $("#detail").scrollIntoView({ behavior: "smooth", block: "start" }); }
  }));
  box.querySelectorAll("[data-act]").forEach((b) => b.addEventListener("click", () => doAction(b.dataset.act)));
  box.querySelectorAll("[data-replay]").forEach((b) => b.addEventListener("click", () => {
    const e = exData.examples.find((x) => x.id === b.dataset.replay);
    selected = (e.id === "duck" ? "duck:" : e.kind === "bird" || e.id === "g1" ? "replay:" : "run:") + e.replay; lastSig = ""; refreshJobs(); $("#detail").scrollIntoView({ behavior: "smooth", block: "start" });
  }));
  drawThumbs();
}
async function drawThumbs() {
  // thumbnails are drawn from the same real records the examples replay
  for (const cv of document.querySelectorAll("canvas[data-thumb]")) {
    const id = cv.dataset.thumb, e = exData.examples.find((x) => x.id === id);
    try {
      if (id === "buck" || id === "r4") {
        const w = await api(`/api/run/wave?dir=${encodeURIComponent(e.replay)}&round=1`);
        lineChart(cv, w.series[0].x, [{ y: w.series[0].y, color: css("--ink") }], w.xunit, "", [], false, true);
      } else if (id === "duck") {
        const d = await api("/api/duck-training"); const c = (d.attempt2_300m?.curve || []).filter((r) => r[1] != null);
        if (c.length > 1) lineChart(cv, c.map((r) => r[0] / 1e6), [{ y: c.map((r) => r[1]), color: css("--signal") }], "", "", [], false, true);
      } else if (id === "bird_r2") {
        const idx = await api("/api/run/replay?dir=" + encodeURIComponent(e.replay));
        const run = await api(fileUrl(idx.base + "/" + idx.runs[0].file));
        const t = b64f32(run.channels.t), h = b64f32(run.channels.h);
        lineChart(cv, Array.from(t), [{ y: Array.from(h), color: css("--signal") }], "", "", [], false, true);
      } else if (id === "g1" && evidence) {
        const r = evidence.robots.find((x) => x.robot === "g1_velocity_flat" && x.files["trace_nominal.json"]);
        if (r) { const tr = await api(fileUrl(r.files["trace_nominal.json"])); lineChart(cv, tr.t, [{ y: tr.vx, color: css("--ink") }, { y: tr.cmd.map((c) => c[0]), color: css("--muted") }], "", "", [], false, true); }
      }
    } catch (err) { /* thumbnail is decorative; the card still works */ }
  }
}

// ------------------------------------------------------------------ entry
let routeTimer;
async function updateRoute() {
  const s = $("#sentence").value.trim();
  let pipeline = pipeChoice.split(":")[0], robot = pipeChoice.split(":")[1];
  if (!pipeline && s) { lastRoute = await api("/api/route?s=" + encodeURIComponent(s)).catch(() => null); pipeline = lastRoute?.pipeline; robot = lastRoute?.robot; }
  const note = $("#route-note");
  $("#route-edit").hidden = !s && !pipeChoice;
  if (!s && !pipeChoice) { note.textContent = ""; return; }
  if (!pipeline) { note.textContent = T("routeNone"); return; }
  note.textContent = T("routeTo") + T("pipe." + pipeline) + (robot ? " · " + robotName(robot) : "") + (pipeChoice ? "" : T("routeKw"));
  if (pipeline === "robot_bench") note.textContent += " " + T("robotLimit", { r: robotName(robot || "g1_velocity_flat") });
  if (pipeline === "bird") note.textContent += " " + T("birdNote");
}
const robotName = (k) => { const n = T("robots." + k); return typeof n === "string" && !n.startsWith("robots.") ? n : k; };
const jobTitle = (j) => j.sentence ? j.sentence.split("\n")[0] : T("pipe." + j.pipeline) + (j.opts?.robot ? " · " + robotName(j.opts.robot) : "");
function rejText(err) {
  const code = String(err || "").split(":")[0].trim();
  const known = T("rej.codes." + code);
  return typeof known === "string" && !known.startsWith("rej.codes.") ? known : T("rej.other", { e: String(err || "").slice(0, 300) });
}
// "failed checks: a, b" -> readable names
const reasonText = (r) => /^failed checks: /.test(r || "") ? T("failedChecks") + r.replace(/^failed checks: /, "").split(", ").map(checkName).join("、") : (r || "");
function checkName(id) { const n = T("checkName." + id); return typeof n === "string" && !n.startsWith("checkName.") ? n : id; }
async function startJob(pipeline, sentence, opts) {
  try {
    const j = await api("/api/start", { sentence, pipeline, opts: opts || {} });
    if (j.blocked) return handleBlocked(j);
    selected = j.id; lastSig = ""; await refreshJobs();
  }
  catch (e) { $("#route-note").textContent = e.message; }
}
async function submit(ev) {
  ev.preventDefault();
  const sentence = $("#sentence").value.trim();
  let [pipeline, robot] = pipeChoice.split(":");
  if (!pipeline) { await updateRoute(); pipeline = lastRoute?.pipeline; robot = lastRoute?.robot; }
  if (!pipeline) { $("#route-note").textContent = T("routeNone"); return; }
  if ((pipeline === "circuit" || pipeline === "bird") && exData && !exData.ai_ready) return doAction("connect_ai");
  startJob(pipeline, sentence, { robot });
}

// ------------------------------------------------------------------ simulation viewer (viz.js + three.js)
// One viewer element per record, kept across the detail re-renders (every 5 s while a job runs): the element is
// moved into the new placeholder instead of being rebuilt, so playback, camera and WebGL state survive.
const vizRoots = new Map();
const jobState = {};
const vizSlot = (key) => `<div class="viz-slot" data-key="${esc(key)}"></div>`;
function attachViz(src) {
  const slot = [...document.querySelectorAll(".viz-slot")].find((e) => e.dataset.key === src.key);
  if (!slot) return;
  let root = vizRoots.get(src.key);
  if (!root) {
    for (const [k, r] of vizRoots) if (!document.contains(r)) { window.RLSOKViz?.unmount(r); vizRoots.delete(k); }
    root = document.createElement("div"); vizRoots.set(src.key, root);
    const go = () => window.RLSOKViz.mount(root, src);
    window.RLSOKViz ? go() : document.addEventListener("rlsok-viz-ready", go, { once: true });
  }
  slot.append(root);
}
const replaySrc = (key, kind, indexUrl, jobId) => ({ key, kind, indexUrl: () => indexUrl, live: () => !!jobId && ["running", "awaiting_ai"].includes(jobState[jobId]) });
function circuitSrc(key, rounds, waveUrl) { return { key, kind: "circuit", rounds: () => rounds, waveUrl }; }
const waveRounds = (tl) => (tl?.rounds || []).filter((r) => r.has_wave).map((r) => ({ round: r.round, passed: r.status === "PASS" }));
const b64f32 = (b) => { const s = atob(b), u = new Uint8Array(s.length); for (let i = 0; i < s.length; i++) u[i] = s.charCodeAt(i); return new Float32Array(u.buffer); };

// ------------------------------------------------------------------ jobs list + detail
const stateCls = (s) => ({ finished: "ok", running: "run", awaiting_ai: "wait" }[s] || "");
const seenStates = {};
async function refreshJobs() {
  const jobs = await api("/api/jobs").catch(() => []);
  // a finished download / install changes what the example cards can do: redraw them once
  let refreshEx = false;
  let refreshRec = false;
  for (const j of jobs) {
    const was = seenStates[j.id];
    if (was && was !== j.state && j.state === "finished" && /duck_assets|install/.test(j.pipeline)) refreshEx = true;
    if (was && was !== j.state && ["finished", "failed", "stopped"].includes(j.state)) refreshRec = true;
    seenStates[j.id] = j.state;
    jobState[j.id] = j.state;
  }
  if (refreshEx) { renderExamples(); loadCaps(); }
  if (refreshRec) loadEvidence();
  const box = $("#jobs");
  if (!jobs.length) box.innerHTML = `<p class="empty">${esc(T("noJobs"))}</p>`;
  else box.innerHTML = jobs.slice(0, 20).map((j) => `<button class="job" data-id="${esc(j.id)}" aria-current="${j.id === selected}">
      <span class="t">${esc(jobTitle(j))}</span>
      <span class="m"><span>${esc(T("pipe." + j.pipeline))} · ${new Date(j.started * 1000).toLocaleTimeString()}</span>${T("state." + j.state) ? pill4(j.state === "failed" ? "fail" : "prog") : ""}</span></button>`).join("");
  box.querySelectorAll(".job").forEach((b) => b.addEventListener("click", () => { selected = b.dataset.id; lastSig = ""; refreshJobs(); }));
  if (!selected && jobs.length) selected = jobs[0].id;
  await renderDetail();
}
function renderDetailEmpty() { $("#detail").innerHTML = `<div class="emptystate"><p>${esc(T("noDetail"))}</p></div>`; }

let lastSig = "";
async function renderDetail() {
  if (!selected) return renderDetailEmpty();
  if (selected.startsWith("run:")) return renderPastRun(selected.slice(4));
  if (selected.startsWith("duck:")) return renderDuckRecord();
  if (selected.startsWith("replay:")) return renderReplayRecord(selected.slice(7));
  const [job, tl] = await Promise.all([api("/api/jobs/" + selected).catch(() => null), api(`/api/jobs/${selected}/timeline`).catch(() => null)]);
  if (!job) return renderDetailEmpty();
  const running = job.state === "running";
  const sig = JSON.stringify([lang, job.state, (job.log_tail || "").length, tl && JSON.stringify(tl).length, running ? Math.floor(Date.now() / 5000) : 0]);
  if (sig === lastSig) return;
  lastSig = sig;
  const keep = $("#reply")?.value;
  let runs = null;
  if (job.pipeline === "bird" && tl?.final) {
    const idx = await api(`/api/jobs/${job.id}/replay`).catch(() => null);
    if (idx?.runs?.length) runs = { p: idx.runs.filter((r) => r.passed).length, n: idx.runs.length };
  }
  const s4 = status4(job, tl, { runs });
  let html = phead(jobTitle(job), s4) + `<div class="dbody">`;
  if (job.pipeline === "circuit" || job.pipeline === "bird") html += stepBar(tl);
  const nWave = waveRounds(tl).length;
  if (job.pipeline === "bird" || job.pipeline === "robot_bench") html += vizSlot(`${job.pipeline}:${job.id}`);
  else if (job.pipeline === "circuit" && nWave) html += vizSlot(`circ:${job.id}:${nWave}`);
  if (job.state === "failed") html += errorCard(job.error?.code || "unknown", job.error);
  if (job.state === "stopped") html += `<div class="callout"><h4>${esc(T("stoppedT"))}</h4><p>${esc(T("stoppedP"))}</p>
    <div class="btns"><button class="btn primary" data-act="retry">${esc(T("act.retry"))}</button></div></div>`;
  if (running) {
    // measured 2026-10-21: G1 test 20-50 s; one circuit round with 30 tolerance cases about 7 min
    const mins = { robot_bench: 1, circuit: 7, bird: 3, duck_assets: 1, install_train: 20, install_mjlab: 20, install_cad: 4, train_measure: 20, train_full: 380 }[job.pipeline] || 2;
    const pg = tl?.progress;
    html += `<div class="callout run"><h4><span class="spinner"></span>${esc(T("running"))}</h4><p>${esc(T("elapsed", { s: Math.round(Date.now() / 1000 - job.started), n: mins }))}</p>
      ${pg ? `<p class="lbl">${esc(pg.stage === "tolerance" ? T("progTol", { r: pg.round, d: pg.done, t: pg.total }) : T("progNominal", { r: pg.round }))}</p>
        ${pg.total ? `<div class="progress" aria-hidden="true"><i style="width:${(100 * pg.done / pg.total).toFixed(0)}%"></i></div>` : ""}` : ""}
      ${mins >= 20 ? `<p>${esc(T("bgNote"))}</p>` : ""}<div class="btns"><button class="btn" id="stop-job" type="button">${esc(T("stop"))}</button></div></div>`;
  } else if (job.state === "awaiting_ai") {
    html += `<div class="btns top"><button class="btn" id="stop-job" type="button">${esc(T("stop"))}</button></div>`;
  }
  if ((job.pipeline === "circuit" || job.pipeline === "bird") && tl) html += circuitNums(tl) + aiTimeline(tl) + circuitView(job, tl);
  else if (job.pipeline === "robot_bench" && job.result) html += keyNums([["seeds", { s: (job.result.labels?.seeds?.reason || "10/10").split(" ")[0] }]]) + `<details class="tech"><summary>${esc(T("techT"))}</summary>${robotCard(job.result, true)}</details>`;
  else if (job.pipeline.startsWith("train") && tl) html += trainView(tl);
  html += `<details class="tech"><summary>${esc(T("logT"))}</summary><pre class="prompt">${esc(job.log_tail)}</pre></details></div>`;
  $("#detail").innerHTML = html;
  if (keep && $("#reply")) $("#reply").value = keep;
  bindDetail(job, tl);
  if (job.pipeline === "bird" || job.pipeline === "robot_bench")
    attachViz(replaySrc(`${job.pipeline}:${job.id}`, job.pipeline === "bird" ? "bird" : "robot", `/api/jobs/${job.id}/replay`, job.id));
  else if (job.pipeline === "circuit" && nWave)
    attachViz(circuitSrc(`circ:${job.id}:${nWave}`, waveRounds(tl), (r) => `/api/jobs/${job.id}/wave?round=${r}`));
}
async function renderReplayRecord(dir) {
  const sig = JSON.stringify([lang, "replay", dir]);
  if (sig === lastSig) return;
  lastSig = sig;
  const idx = await api("/api/run/replay?dir=" + encodeURIComponent(dir)).catch(() => null);
  if (!idx) return renderDetailEmpty();
  const ex = exData?.examples.find((e) => e.replay === dir);
  const title = ex ? T("ex." + ex.id)[0] : dir.split(/[\\/]/).pop();
  const key = "replay:" + dir;
  const runs = (idx.runs || []).filter((r) => r.file || r.passed !== undefined), np = runs.filter((r) => r.passed).length;
  const bird = idx.kind === "bird";
  const s4 = bird ? { st: np === runs.length ? "sim" : "fail", say: T(np === runs.length ? "say.sim_bird" : "say.fail_bird", { p: np, n: runs.length }) }
    : { st: "sim", say: T("say.sim_robot", { s: runs.length || 10 }) };
  const nums = bird ? keyNums([["flights", { p: np, n: runs.length }], ...(idx.cruise_speed_mps ? [["cruise", { v: idx.cruise_speed_mps }]] : [])])
    : keyNums([["seeds", { s: `${np || runs.length}/${runs.length}` }]]);
  $("#detail").innerHTML = phead(title, s4) + `<div class="dbody"><p class="replay">${esc(T(bird ? "replaySimBadge" : "replayBadge"))}</p>
    <div class="pmain">${vizSlot(key)}${nums}</div>${ex?.package ? `<div id="guide"></div>` : ""}
    <div class="nextbar"><p class="lbl">${esc(T("nextT"))}</p><div class="btns">
      ${ex && ex.run ? `<button class="btn primary" id="rerun">${esc(T("exRun"))}</button>` : ""}
      <button class="btn" type="button" data-up="1">${esc(T("upReal"))}</button></div></div>
    <details class="tech"><summary>${esc(T("techT"))}</summary><p class="lbl">${esc(T("vizWhat"))}</p><ul class="data">${(idx.label?.reasons || []).map((r) => `<li>${esc(r)}</li>`).join("")}</ul></details></div>`;
  $("#detail").querySelectorAll("[data-up]").forEach((b) => b.addEventListener("click", openUploads));
  attachViz(replaySrc(key, idx.kind, "/api/run/replay?dir=" + encodeURIComponent(dir), null));
  if (ex?.package) guideBird(ex).then((h) => { const g = $("#guide"); if (g) g.innerHTML = h; }).catch(() => {});
  $("#rerun")?.addEventListener("click", async () => {
    const r = await api("/api/examples/run", { id: ex.id }).catch(() => ({}));
    if (r.blocked) return handleBlocked(r);
    if (r.id) { selected = r.id; lastSig = ""; refreshJobs(); }
  });
}

// ------------------------------------------------------------------ guided example: idea -> AI design -> checks -> files
function parseCsv(text) {
  const rows = []; let row = [], cell = "", q = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (q) { if (c === '"' && text[i + 1] === '"') { cell += '"'; i++; } else if (c === '"') q = false; else cell += c; }
    else if (c === '"') q = true;
    else if (c === ",") { row.push(cell); cell = ""; }
    else if (c === "\n" || c === "\r") { if (c === "\r" && text[i + 1] === "\n") i++; row.push(cell); rows.push(row); row = []; cell = ""; }
    else cell += c;
  }
  if (cell || row.length) { row.push(cell); rows.push(row); }
  const [head, ...body] = rows.filter((r) => r.some((x) => x.trim()));
  return body.map((r) => Object.fromEntries(head.map((h, k) => [h, r[k] ?? ""])));
}
const hostOf = (u) => { try { return new URL(u).hostname.replace(/^www\./, ""); } catch (e) { return ""; } };
async function guideBird(ex) {
  const pk = ex.package, f = (n) => fileUrl(pk.dir + "/" + n);
  const [bomTxt, ev, sum, prn] = await Promise.all([fetch(f("BOM.csv")).then((r) => (r.ok ? r.text() : "")).catch(() => ""),
    api(f("evaluation.json")).catch(() => null), api(f("SUMMARY.json")).catch(() => null), api(f("PRINT_SETTINGS.json")).catch(() => null)]);
  const bom = bomTxt ? parseCsv(bomTxt.replace(/^﻿/, "")) : [];
  const checks = ev?.checks || [], np = checks.filter((c) => c.passed).length;
  const total = Object.entries(sum?.reference_total || {}).map(([c, v]) => `${v} ${c}`).join(" + ");
  const g = (k, v) => esc(T("guide." + k, v));
  const checkRows = checks.map((c) => `<tr><td>${esc(checkName(c.id))}</td><td>${fmt(c.actual)} ${esc(c.unit || "")}</td><td>${esc(limitText(c))}</td>
    <td class="${c.passed ? "ok" : "bad"}">${esc(c.passed ? T("pass") : T("fail"))}</td></tr>`).join("");
  const bomRows = bom.map((b) => `<tr><td>${esc(b.item)}</td><td class="nw">${esc(b.qty)}${b.unit && b.unit !== "pcs" ? " " + esc(b.unit) : ""}</td><td class="nw">${b.reference_price_each ? esc(`${b.reference_price_each} ${b.currency}`) : "—"}</td>
    <td>${b.source ? `<a href="${esc(b.source)}" target="_blank" rel="noopener">${esc(hostOf(b.source))}</a> · ${esc(b.retrieved)}` : esc(T("guide.noPrice"))}</td></tr>`).join("");
  const parts = (prn?.parts || []).map((p) => `${p.part.replace(/_/g, " ")} (${p.bbox_mm.map((x) => Math.round(x)).join("×")} mm)`).join(" · ");
  return `<div class="guide"><p class="lbl">${g("title")}</p><ol class="gsteps">
    <li><b>${g("s1")}</b><p class="quote">${g("idea")}</p><p class="note">${g("ideaNote")}</p></li>
    <li><b>${g("s2")}</b><p>${g("design")}</p></li>
    <li><b>${g("s3")}</b><p>${g("checks", { p: np, n: checks.length })}</p>
      <details class="more"><summary>${g("checksAll")}</summary><div class="tblwrap"><table class="checks"><thead><tr><th>${esc(T("check"))}</th><th>${esc(T("actual"))}</th><th>${esc(T("limit"))}</th><th>${esc(T("result"))}</th></tr></thead><tbody>${checkRows}</tbody></table></div></details></li>
    <li><b>${g("s4")}</b><p>${g("files")}</p>
      <p class="lbl">${g("bomT", { n: bom.length })}</p><div class="tblwrap"><table class="checks"><thead><tr><th>${g("part")}</th><th>${g("qty")}</th><th>${g("price")}</th><th>${g("source")}</th></tr></thead><tbody>${bomRows}</tbody></table></div>
      <p class="note">${g("priceNote", { t: total || "—", u: (sum?.unpriced_items || []).length })}</p>
      ${parts ? `<p class="lbl">${g("printT", { n: prn.parts.length })}</p><p class="note">${esc(parts)}</p>` : ""}
      <div class="btns"><a class="btn primary" href="${esc(pk.url)}" target="_blank" rel="noopener">${g("dlPkg", { mb: (pk.bytes / 1e6).toFixed(1) })}</a>
        <a class="btn" href="${f("BOM.csv")}" download="rlsok-bird-r2-bom.csv">${g("dlBom")}</a>
        <a class="btn" href="${f("flight_params.json")}" download="rlsok-bird-r2-flight-params.json">${g("dlParams")}</a></div>
      <p class="note">SHA256 ${esc(pk.sha256)}</p></li></ol>
    <div class="callout warn"><h4>${g("riskT")}</h4><p>${g("risk")}</p></div></div>`;
}
// a run that cannot start yet says why ({blocked: ...}); the CAD add-on is a 100 MB download, so ask first
function handleBlocked(r) {
  if (r.blocked === "install_cad") { if (confirm(T("cadAsk"))) doAction("install_cad"); return; }
  return doAction(r.blocked === "no_ai" ? "connect_ai" : r.blocked);
}
function errorCard(code, info) {
  const e = T("err." + code);
  let [what, why, how, act] = Array.isArray(e) ? e : T("err.unknown");
  why = (why || "").replace("{last}", (info && info.last) || "—");
  return `<div class="callout err"><p class="lbl">${esc(T("what"))}</p><h4>${esc(what)}</h4>
    ${why ? `<p class="lbl">${esc(T("whyT"))}</p><p>${esc(why)}</p>` : ""}<p class="lbl">${esc(T("howT"))}</p><p>${esc(how)}</p>
    <div class="btns">${act ? `<button class="btn primary" data-act="${act}">${esc(T("act." + act))}</button>` : ""}
      <button class="btn" data-act="copy_log">${esc(T("act.copy_log"))}</button></div></div>`;
}
// one status per result: hw | sim | prog | fail (+ one plain sentence)
function status4(job, tl, extra = {}) {
  if (["running", "queued"].includes(job.state)) return { st: "prog", say: tl?.status?.num_timesteps ? T("say.train", { p: Math.round(100 * (tl.status.steps || 0) / tl.status.num_timesteps) }) : T("say.running") };
  if (job.state === "awaiting_ai") return { st: "prog", say: T("say.waiting") };
  if (job.state === "stopped") return { st: "prog", say: T("say.stopped") };
  if (job.state === "failed") return { st: "fail", say: T("say.error") };
  if ((job.pipeline === "circuit" || job.pipeline === "bird") && tl?.final) {
    const f = tl.final, n = tl.rounds.length;
    if (job.pipeline === "bird" && extra.runs) return { st: f.achieved ? "sim" : "fail", say: T(f.achieved ? "say.sim_bird" : "say.fail_bird", extra.runs) };
    if (f.achieved) return { st: "sim", say: T(f.buildability?.grade === "IDEAL_ONLY" ? "say.sim_ideal" : "say.sim_circuit", { r: f.final_round || n }) };
    return { st: "fail", say: /INFEASIBLE/.test(f.status) ? T("say.fail_infeasible") : T("say.fail_circuit", { n }) };
  }
  if (job.pipeline === "robot_bench" && job.result) {
    const ok = job.result.level1.status === "VERIFIED";
    return { st: ok ? "sim" : "fail", say: ok ? T("say.sim_robot", { s: (job.result.labels?.seeds?.reason || "10/10").split(" ")[0].split("/")[1] || 10 }) : T("say.fail_robot") };
  }
  if (job.pipeline.startsWith("train") && tl?.level2) return { st: tl.level2.label.status === "VERIFIED" ? "sim" : "fail", say: tl.level2.label.reason || "" };
  return { st: null, say: "" };
}
const pill4 = (st) => (st ? `<span class="st ${st}">${esc(T("st." + st))}</span>` : "");
function phead(title, s) { return `<div class="phead"><h3>${esc(title)}</h3>${pill4(s.st)}${s.say ? `<p class="say">${esc(s.say)}</p>` : ""}</div>`; }
function stepBar(tl) {
  const rs = tl?.rounds || [], n = rs.length, last = rs[n - 1] || {};
  const sim = rs.some((r) => r.checks.length), judged = !!tl?.final || (last.status && last.status !== "AWAITING_AI");
  const items = [["design", n > 0], ["sim", sim], ["judge", judged]];
  return `<ol class="pbar">${items.map(([k, ok]) => `<li class="${ok ? "on" : ""}">${esc(T("steps." + k))}</li>`).join("")}<li class="${n > 1 ? "on" : ""}">${esc(T("steps.revise", { n: Math.max(n, 1) }))}</li></ol>`;
}
function keyNums(items) {
  return `<div class="knums"><p class="lbl">${esc(T("kn.title"))}</p>${items.map(([k, vars]) => { const [b, d] = T("kn." + k); return `<div><b>${esc(String(b).replace(/\{(\w+)\}/g, (m, x) => vars[x] ?? m))}</b><span>${esc(d)}</span></div>`; }).join("")}</div>`;
}
function circuitNums(tl) {
  const rs = tl?.rounds || [], tol = [...rs].reverse().find((r) => r.tolerance && r.tolerance.total);
  const items = [["rounds", { n: rs.length }]];
  if (tl?.final?.native_simulations) items.push(["sims", { n: tl.final.native_simulations }]);
  if (tol) items.push([tol.tolerance.passed === tol.tolerance.total ? "tol" : "tolBad", { p: tol.tolerance.passed, t: tol.tolerance.total }]);
  return keyNums(items);
}
function aiTimeline(tl) {
  const rs = (tl?.rounds || []).filter((r) => r.diagnosis || r.changes || r.reason);
  if (!rs.length) return "";
  return `<div class="aitl"><p class="lbl">${esc(T("aiT"))}</p><ol>${rs.map((r) => `<li><b>${esc(T("aiRound", { n: r.round }))}</b>${pill4(r.status === "PASS" ? "sim" : r.status && r.status !== "AWAITING_AI" ? "fail" : "prog")}
    ${(r.changes || r.diagnosis) ? clip(r.changes || r.diagnosis) : `<p class="note">${esc(reasonText(r.reason) || T("aiNone"))}</p>`}</li>`).join("")}</ol></div>`;
}
// long AI text: the first lines, the rest one click away
function clip(t) {
  if (t.length <= 280) return `<p class="quote data">${esc(t)}</p>`;
  return `<p class="quote data">${esc(t.slice(0, 280))}…</p><details class="tech"><summary>${esc(T("more"))}</summary><p class="quote data">${esc(t)}</p></details>`;
}
function verdictOf(job, tl) {
  if (job.state === "running") return { key: "RUNNING", cls: "" };
  if (job.state === "awaiting_ai") return { key: "WAITING", cls: "" };
  if (job.state === "stopped") return { key: "STOPPED", cls: "" };
  if ((job.pipeline === "circuit" || job.pipeline === "bird") && tl?.final) {
    if (tl.final.achieved && tl.final.label?.status === "UNVERIFIED") return { key: "SIM_UNVERIFIED", cls: "" };
    if (tl.final.achieved && tl.final.buildability?.grade === "IDEAL_ONLY") return { key: "IDEAL_ONLY", cls: "" };
    if (tl.final.achieved) return { key: "VERIFIED", cls: "ok" };
    return { key: /INFEASIBLE/.test(tl.final.status) ? "INFEASIBLE" : "FAILED", cls: "bad" };
  }
  if (job.pipeline === "robot_bench" && job.result) return { key: job.result.level1.status, cls: job.result.level1.status === "VERIFIED" ? "ok" : "bad" };
  if (job.pipeline.startsWith("train") && tl?.level2) return { key: tl.level2.label.status, cls: tl.level2.label.status === "VERIFIED" ? "ok" : "" };
  if (job.state === "failed") return { key: "FAILED", cls: "bad" };
  return { key: "DONE", cls: "" };
}
function limitText(c) {
  if (c.op === "between") return `${fmt(c.min)} … ${fmt(c.max)} ${c.unit || ""}`;
  return `${{ le: "≤", ge: "≥", lt: "<", gt: ">" }[c.op] || c.op} ${fmt(c.value)} ${c.unit || ""}`;
}
function circuitView(job, tl, replay = false) {
  let h = "";
  if (replay) h += `<p class="replay">${esc(T("replayBadge"))}</p>`;
  const autoAi = exData?.ai_ready && settings?.ai?.provider !== "exchange";
  if (tl.awaiting && job.state === "awaiting_ai") {
    const n = tl.rounds.length ? Math.max(...tl.rounds.map((r) => r.round)) : 1;
    const rej = tl.awaiting.previous_rejection;
    if (rej) h += `<div class="callout err"><h4>${esc(T("rej.title"))}</h4><p>${esc(rejText(rej.error))}</p>
      <p class="note">${esc(rej.attempt ? T("rej.spec", { a: rej.attempt, n: rej.of }) : T("rej.round"))}</p></div>`;
    h += `<div class="callout wait"><h4>${esc(tl.awaiting.stage === "spec" ? T("nowWaitSpec") : T("nowWaitRound", { n }))}</h4><p>${esc(T(autoAi ? "howWait" : "howWaitManual"))}</p>
      ${autoAi ? `<div class="btns"><button class="btn primary" id="auto-ai" type="button">${esc(T("autoAi"))}</button></div>` : ""}
      <p class="note">${esc(T("manual.what"))}</p>
      <pre class="prompt" id="prompt">${esc(tl.awaiting.prompt)}</pre>
      <div class="btns"><button class="btn primary" id="copy" type="button">${esc(T("copy"))}</button>
        <span class="note">${esc(T("manual.open"))}</span>
        <a class="btn" href="https://claude.ai/new" target="_blank" rel="noopener">Claude ↗</a>
        <a class="btn" href="https://chatgpt.com/" target="_blank" rel="noopener">ChatGPT ↗</a>
        <a class="btn" href="https://chat.deepseek.com/" target="_blank" rel="noopener">DeepSeek ↗</a></div>
      <p class="note">${esc(T("manual.tip"))}</p>
      <textarea id="reply" class="reply" aria-label="AI reply"></textarea><div class="btns"><button class="btn primary" id="send" type="button">${esc(T("submit"))}</button></div></div>`;
  } else if (tl.final) {
    const infeas = /INFEASIBLE/.test(tl.final.status);
    const b = tl.final.buildability;
    if (tl.final.achieved && tl.final.label?.status === "UNVERIFIED") h += `<div class="callout warn"><h4>${esc(T("simUnv.title"))}</h4><p>${esc(lang === "zh" ? tl.final.conclusion : "")}</p>
      <p class="lbl">${esc(T("whyT"))}</p><ul>${(tl.final.label.reasons || []).map((r) => `<li>${esc(r)}</li>`).join("")}</ul>
      <p class="lbl">${esc(T("next"))}</p><p>${esc(T("simUnv.next"))}</p></div>`;
    else if (tl.final.achieved && b?.grade === "IDEAL_ONLY") h += `<div class="callout warn"><h4>${esc(T("ideal.title"))}</h4><p>${esc(T("ideal.lead"))}</p>
      <ul>${b.reasons.map((r) => `<li>${esc(T("ideal." + r.code, { d: r.detail || "" }))}</li>`).join("")}</ul>
      <p class="lbl">${esc(T("next"))}</p><p>${esc(T("ideal.next"))}</p><p class="note">${esc(lang === "zh" ? tl.final.conclusion : "")} · ${esc(T("sims"))}: ${esc(tl.final.native_simulations)}</p></div>`;
    else if (tl.final.achieved) h += `<div class="callout ok"><h4>${esc(T("doneAll"))}</h4><p>${esc(lang === "zh" ? tl.final.conclusion : "")}</p><p class="lbl">${esc(T("sims"))}: ${esc(tl.final.native_simulations)}</p>
      <div class="btns"><a class="btn" href="${fileUrl(lang === "en" && tl.final.report_html_en ? tl.final.report_html_en : tl.final.report_html)}" target="_blank" rel="noopener">${esc(T("report"))}</a></div></div>`;
    else {
      const last = tl.rounds[tl.rounds.length - 1] || {};
      h += `<div class="callout"><h4>${esc(T("v." + (infeas ? "INFEASIBLE" : "FAILED")))}</h4><p>${esc(lang === "zh" ? tl.final.conclusion : "")}</p>
        <p class="lbl">${esc(T("cause"))}</p><p class="quote">${esc(last.infeasible_reason || last.reason || "")}</p>
        <p class="lbl">${esc(T("next"))}</p><p>${esc(infeas ? T("nextInfeasible") : T("nextMaxIter"))}</p>
        <div class="btns"><a class="btn" href="${fileUrl(lang === "en" && tl.final.report_html_en ? tl.final.report_html_en : tl.final.report_html)}" target="_blank" rel="noopener">${esc(T("report"))}</a></div></div>`;
    }
    if (!replay && tl.final.downloads?.length) {
      h += `<div class="callout"><h4>${esc(T("dl.title"))}</h4><div class="btns">${tl.final.downloads.map((k) =>
        `<a class="btn" href="/api/jobs/${encodeURIComponent(job.id)}/download?what=${k}&lang=${lang}" download>${esc(T("dl." + k))}</a>`).join("")}</div></div>
        <div class="callout"><h4>${esc(T("cont.title"))}</h4><p class="note">${esc(T("cont.hint"))}</p>
        <textarea id="cont-text" class="reply short" placeholder="${esc(T("cont.ph"))}"></textarea><div class="btns"><button class="btn primary" id="cont-go" type="button">${esc(T("cont.go"))}</button></div></div>`;
    }
  }
  if (!replay && (job.pipeline === "circuit" || job.pipeline === "bird") && ["running", "awaiting_ai"].includes(job.state)) {
    h += `<div class="callout"><h4>${esc(T("noteT"))}</h4><p class="note">${esc(T("noteHint"))}</p>
      ${(tl.notes || []).length ? `<p class="lbl">${esc(T("notesList"))}</p><ul>${tl.notes.map((n) => `<li>${esc(T("round", { n: n.for_round }))}：${esc(n.text)}</li>`).join("")}</ul>` : ""}
      <textarea id="note-text" class="reply short" placeholder="${esc(T("notePh"))}"></textarea><div class="btns"><button class="btn" id="note-add" type="button">${esc(T("noteAdd"))}</button></div></div>`;
  }
  h += `<details class="tech"><summary>${esc(T("techT"))}</summary><ol class="tl">`;
  if (tl.spec) h += `<li class="ok"><h4>${esc(T("spec"))}</h4><div class="tblwrap"><table class="checks"><thead><tr><th>${esc(T("check"))}</th><th>${esc(T("limit"))}</th></tr></thead><tbody>` +
      tl.spec.checks.map((c) => `<tr><td title="${esc(c.id + (c.description ? " · " + c.description : ""))}">${esc(checkName(c.id))}</td><td>${esc(limitText(c))}</td></tr>`).join("") + `</tbody></table></div></li>`;
  for (const r of tl.rounds) {
    const passed = r.status === "PASS", sim = r.checks.length > 0, rejected = /REJECTED/.test(r.status), infeas = r.status === "AI_DECLARED_INFEASIBLE";
    const tolBad = r.tolerance && r.tolerance.worst_fail && r.tolerance.worst_fail.length;
    const rs = T("rs." + r.status);
    h += `<li class="${passed ? "ok" : r.status && r.status !== "AWAITING_AI" ? "bad" : ""}"><h4>${esc(T("round", { n: r.round }))} <span class="state ${passed ? "ok" : ""}" title="${esc(r.status)}">${esc(typeof rs === "string" && !rs.startsWith("rs.") ? rs : r.status)}</span>
        <span class="note">${esc(r.author || "")}</span></h4>
      <div class="stages"><span class="stg ok">${esc(T("sDesign"))} ✓</span><span class="stg ${sim ? "ok" : rejected ? "bad" : ""}">${esc(T("sSim"))} ${sim ? "✓" : rejected ? "✗" : "…"}</span>
        ${r.tolerance ? `<span class="stg ${tolBad ? "bad" : "ok"}">${esc(T("sTol"))} ${tolBad ? "✗" : "✓"}</span>` : ""}
        <span class="stg ${passed ? "ok" : sim || rejected || infeas ? "bad" : ""}">${esc(T("sJudge"))} ${passed ? "✓" : sim || rejected || infeas ? "✗" : "…"}</span></div>`;
    if (!passed && (r.reason || tolBad || r.failed_checks.length)) {
      let cause = reasonText(r.reason);
      if (tolBad) { const w = r.tolerance.worst_fail[0]; const ck = (tl.spec?.checks || []).find((c) => c.id === w.check); cause = T("tolFail", { k: w.check, c: w.case, a: `${fmt(w.actual)} ${ck?.unit || ""}`.trim(), l: ck ? limitText(ck) : "" }); }
      h += `<div class="callout"><p class="lbl">${esc(T("cause"))}</p><p class="quote">${esc(cause)}</p><p class="lbl">${esc(T("next"))}</p><p>${esc(rejected ? T("nextReject") : infeas ? T("nextInfeasible") : T("nextFix"))}</p></div>`;
    }
    if (r.checks.length) h += `<div class="tblwrap"><table class="checks"><thead><tr><th>${esc(T("check"))}</th><th>${esc(T("actual"))}</th><th>${esc(T("limit"))}</th><th>${esc(T("result"))}</th></tr></thead><tbody>` +
        r.checks.map((c) => `<tr><td title="${esc(c.id + (c.description ? " · " + c.description : ""))}">${esc(checkName(c.id))}</td><td>${fmt(c.actual)} ${esc(c.unit || "")}</td><td>${esc(limitText(c))}</td><td class="${c.passed ? "ok" : "bad"}">${esc(c.passed ? T("pass") : T("fail"))}</td></tr>`).join("") + `</tbody></table></div>`;
    if (r.has_wave) h += `<p class="note">${esc(T(job.pipeline === "bird" ? "waveBird" : "wave"))}</p><canvas class="chart" data-wave="${r.round}"></canvas>`;
    if (r.diagnosis || r.changes) h += `<details class="more"><summary>${esc(T("diag"))} / ${esc(T("changes"))}</summary>${r.diagnosis ? `<p class="quote">${esc(r.diagnosis)}</p>` : ""}${r.changes ? `<p class="quote">${esc(r.changes)}</p>` : ""}</details>`;
    h += `</li>`;
  }
  return h + `</ol></details>`;
}
// the files a finished example produced (read-only copies of the real run); a design that did not pass offers only its report
function exampleDownloads(dir, kinds, achieved) {
  const ks = kinds.filter((k) => (achieved ? ["netlist", "bom", "report"] : ["report"]).includes(k));
  return ks.length ? `<div class="callout"><h4>${esc(T("dl.title"))}</h4><div class="btns">${ks.map((k) =>
    `<a class="btn" href="/api/examples/download?dir=${encodeURIComponent(dir)}&what=${k}&lang=${lang}" download>${esc(T("dl." + k))}</a>`).join("")}</div></div>` : "";
}
async function renderPastRun(dir) {
  const tl = await api("/api/run/timeline?dir=" + encodeURIComponent(dir)).catch(() => null);
  if (!tl) return renderDetailEmpty();
  const sig = JSON.stringify([lang, dir]);
  if (sig === lastSig) return;
  lastSig = sig;
  const name = dir.split(/[\\/]/).slice(-1)[0];
  const ex = exData?.examples.find((e) => e.replay === dir);
  const job = { id: "run:" + dir, pipeline: "circuit", state: "finished" };
  const title = ex ? T("ex." + ex.id)[0] : (evidence?.circuits || []).find((c) => c.dir === dir)?.requirement || name;
  $("#detail").innerHTML = phead(title, status4(job, tl)) + `<div class="dbody">${stepBar(tl)}
    <div class="pmain">${waveRounds(tl).length ? vizSlot("circrun:" + dir) : "<div></div>"}${circuitNums(tl)}</div>${aiTimeline(tl)}
    ${ex && ex.run ? `<div class="nextbar"><p class="lbl">${esc(T("nextT"))}</p><div class="btns"><button class="btn primary" id="rerun">${esc(T("exRun"))}</button></div></div>` : ""}${circuitView(job, tl, true)}
    ${ex && tl.final?.downloads?.length ? exampleDownloads(dir, tl.final.downloads, tl.final.achieved) : ""}</div>`;
  if (waveRounds(tl).length) attachViz(circuitSrc("circrun:" + dir, waveRounds(tl), (r) => `/api/run/wave?dir=${encodeURIComponent(dir)}&round=${r}`));
  $("#rerun")?.addEventListener("click", async () => {
    const r = await api("/api/examples/run", { id: ex.id }).catch(() => ({}));
    if (r.blocked) return handleBlocked(r);
    if (r.id) { selected = r.id; lastSig = ""; refreshJobs(); }
  });
  drawWaves((rnd) => `/api/run/wave?dir=${encodeURIComponent(dir)}&round=${rnd}`);
}
async function renderDuckRecord() {
  const sig = JSON.stringify([lang, "duck"]);
  if (sig === lastSig) return;
  lastSig = sig;
  const d = await api("/api/duck-training").catch(() => ({}));
  const ex = exData?.examples.find((e) => e.id === "duck");
  const att = [["attempt1_150m", "1.5"], ["attempt2_300m", "3"]].filter(([k]) => d[k]);
  let h = phead(T("ex.duck")[0], { st: "sim", say: T("say.sim_duck") }) + `<div class="dbody">
    <p class="replay">${esc(T("replayBadge"))}</p>
    <div class="nextbar"><div class="btns">${ex?.needs.includes("duck_assets") ? `<button class="btn primary" data-act="duck_assets">${esc(T("getDuck"))}</button>` : `<button class="btn primary" id="duck-run">${esc(T("exRun"))}</button>`}</div></div>
    <p class="lbl">${esc(T("attempts"))}</p>`;
  let tech = "";
  for (const [k, n] of att) {
    const a = d[k], ok = a.label.status === "VERIFIED";
    const segs = Object.values(a.checks.tracking.segments), np = segs.filter((s) => s.pass).length;
    h += `<div class="callout"><h4>${lang === "zh" ? `第 ${k.endsWith("300m") ? 2 : 1} 次 · ${n} 亿步` : `Attempt ${k.endsWith("300m") ? 2 : 1} · ${n === "3" ? "300M" : "150M"} steps`} ${pill4(ok ? "sim" : "fail")}</h4>
      <p>${esc(lang === "zh" ? `五个动作里 ${np} 个的跟踪误差在允许范围内。` : `${np} of ${segs.length} motions tracked within the allowed error.`)}</p>
      <canvas class="chart" data-curve="${k}"></canvas></div>`;
    tech += `<p class="lbl">${esc(T("attempts"))} ${k}</p><p class="data">seeds ${esc(a.checks.seeds.value)} · DR ${esc(a.checks.dr_train_preset.value)} · tracking ${np}/${segs.length}</p>`;
  }
  h += `<p class="note">${esc(T("duckNote"))}</p><details class="tech"><summary>${esc(T("techT"))}</summary>${tech}</details>`;
  $("#detail").innerHTML = h + `</div>`;
  for (const [k] of att) { const c = d[k].curve.filter((r) => r[1] != null); if (c.length > 1) lineChart($(`canvas[data-curve=${k}]`), c.map((r) => r[0] / 1e6), [{ y: c.map((r) => r[1]), color: css("--ink") }], "M steps", ""); }
  $("#detail").querySelectorAll("[data-act]").forEach((b) => b.addEventListener("click", () => doAction(b.dataset.act)));
  $("#duck-run")?.addEventListener("click", async () => { const r = await api("/api/examples/run", { id: "duck" }); if (r.id) { selected = r.id; lastSig = ""; refreshJobs(); } });
}
function trainView(tl) {
  if (!tl || !tl.status) return "";
  const s = tl.status, est = tl.estimate;
  const pct = s.num_timesteps ? Math.min(100, 100 * (s.steps || 0) / s.num_timesteps) : 0;
  let h = `<div class="bigrow"><div>${esc(T("trainSteps"))}<b>${(s.steps || 0).toLocaleString()} / ${(s.num_timesteps || 0).toLocaleString()}</b></div>
    <div>${esc(T("sps"))}<b>${s.steps_per_s ? Math.round(s.steps_per_s).toLocaleString() : "—"}</b></div>
    <div>${esc(T("eta"))}<b>${s.eta_s ? (s.eta_s / 3600).toFixed(1) + " " + T("hours") : "—"}</b></div>
    ${est ? `<div>full run<b>${(est.estimate_hours_for_full_run || 0).toFixed(1)} ${esc(T("hours"))}</b></div>` : ""}</div>
    <div class="progress" aria-hidden="true"><i style="width:${pct.toFixed(1)}%"></i></div>`;
  if (tl.level2) h += `<div class="callout ${tl.level2.label.status === "VERIFIED" ? "ok" : ""}"><h4>${esc(T("v." + tl.level2.label.status))}</h4><p>${esc(tl.level2.label.reason || "")}</p></div>`;
  return h + `<p class="note">${esc(T("curve"))}</p><canvas class="chart" id="train-curve"></canvas>`;
}
function drawWaves(urlFor) {
  document.querySelectorAll("canvas[data-wave]").forEach(async (cv) => {
    const w = await api(urlFor(cv.dataset.wave)).catch(() => null);
    if (!w || !w.series.length) return;
    const lims = []; for (const c of w.limits) { if (c.min != null) lims.push(c.min); if (c.max != null) lims.push(c.max); if (c.value != null) lims.push(c.value); }
    lineChart(cv, w.series[0].x, [{ y: w.series[0].y, color: css("--ink") }], w.xunit, w.series[0].unit, lims, w.xunit === "Hz");
  });
}
function bindDetail(job, tl) {
  $("#detail").querySelectorAll("[data-act]").forEach((b) => b.addEventListener("click", () => doAction(b.dataset.act)));
  $("#copy")?.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(tl.awaiting.prompt); $("#copy").textContent = T("copied"); }
    catch (e) { const r = document.createRange(); r.selectNodeContents($("#prompt")); getSelection().removeAllRanges(); getSelection().addRange(r); }
  });
  $("#send")?.addEventListener("click", async () => {
    const text = $("#reply").value.trim(); if (!text) return;
    try { await api("/api/respond", { job: job.id, text }); lastSig = ""; refreshJobs(); }
    catch (e) { toast(e.message === "NO_JSON_IN_REPLY" ? T("noJson") : e.message); }
  });
  $("#stop-job")?.addEventListener("click", async () => {
    if (!confirm(T("stopConfirm"))) return;
    $("#stop-job").disabled = true; $("#stop-job").textContent = T("stopping");
    try { await api(`/api/jobs/${job.id}/stop`, {}); } catch (e) { toast(e.message); }
    lastSig = ""; setTimeout(refreshJobs, 800);
  });
  $("#note-add")?.addEventListener("click", async () => {
    const text = $("#note-text").value.trim(); if (!text) return;
    try { const n = await api(`/api/jobs/${job.id}/note`, { text }); toast(T("noteAdded", { n: n.for_round })); $("#note-text").value = ""; lastSig = ""; refreshJobs(); }
    catch (e) { toast(e.message); }
  });
  $("#cont-go")?.addEventListener("click", async () => {
    const text = $("#cont-text").value.trim(); if (!text) return $("#cont-text").focus();
    try { const j = await api(`/api/jobs/${job.id}/continue`, { text }); selected = j.id; lastSig = ""; toast(T("started")); refreshJobs(); }
    catch (e) { toast(e.message); }
  });
  $("#auto-ai")?.addEventListener("click", async () => { try { await api("/api/continue-with-ai", { job: job.id }); lastSig = ""; refreshJobs(); } catch (e) { toast(e.message); } });
  drawWaves((rnd) => `/api/jobs/${job.id}/wave?round=${rnd}`);
  const tc = $("#train-curve");
  if (tc && tl?.curve?.length > 1) { const c = tl.curve.filter((r) => r[1] != null); lineChart(tc, c.map((r) => r[0] / 1e6), [{ y: c.map((r) => r[1]), color: css("--ink") }], "M steps", ""); }
  if (job.result) hydrateRobot($("#detail"), job.result);
}

// ------------------------------------------------------------------ charts
function css(v) { return getComputedStyle(document.documentElement).getPropertyValue(v).trim() || "#888"; }
function fitCanvas(cv) {
  const dpr = Math.min(devicePixelRatio || 1, 2), r = cv.getBoundingClientRect();
  const W = Math.max(120, r.width), H = Math.max(60, r.height);
  cv.width = W * dpr; cv.height = H * dpr;
  const ctx = cv.getContext("2d"); ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return [ctx, W, H];
}
function lineChart(cv, x, series, xunit, yunit = "", limits = [], logx = false, bare = false) {
  if (!cv || !x || x.length < 2) return;
  const [ctx, W, H] = fitCanvas(cv), L = bare ? 4 : 56, R = bare ? 4 : 10, Tp = bare ? 6 : 10, B = bare ? 6 : 28;
  const X = (v) => (logx ? Math.log10(Math.max(v, 1e-12)) : v);
  const skip = !logx && xunit === "ms" ? x[0] + 0.08 * (x[x.length - 1] - x[0]) : -Infinity;
  const ys = series.flatMap((s) => s.y.filter((_, k) => x[k] >= skip)).concat(limits).filter(Number.isFinite);
  let y0 = Math.min(...ys), y1 = Math.max(...ys);
  if (y1 - y0 < 1e-9) { y0 -= 1; y1 += 1; }
  const pad = (y1 - y0) * 0.08; y0 -= pad; y1 += pad;
  const x0 = X(x[0]), x1 = X(x[x.length - 1]);
  const px = (v) => L + ((X(v) - x0) / (x1 - x0 || 1)) * (W - L - R), py = (v) => Tp + (1 - (v - y0) / (y1 - y0)) * (H - Tp - B);
  ctx.fillStyle = css("--bg"); ctx.fillRect(0, 0, W, H);
  if (!bare) {
    ctx.strokeStyle = css("--line"); ctx.fillStyle = css("--muted"); ctx.font = "11px " + css("--mono"); ctx.lineWidth = 1;
    for (let k = 0; k <= 4; k++) { const v = y0 + ((y1 - y0) * k) / 4; ctx.beginPath(); ctx.moveTo(L, py(v)); ctx.lineTo(W - R, py(v)); ctx.stroke(); ctx.fillText(v.toFixed(Math.abs(y1 - y0) < 2 ? 3 : 1) + (yunit ? " " + yunit : ""), 2, py(v) + 4); }
    ctx.fillText(`${x[0].toPrecision(3)} – ${x[x.length - 1].toPrecision(3)} ${xunit}${logx ? " (log)" : ""}`, L, H - 8);
    ctx.setLineDash([5, 4]); ctx.strokeStyle = css("--muted");
    for (const l of limits) { ctx.beginPath(); ctx.moveTo(L, py(l)); ctx.lineTo(W - R, py(l)); ctx.stroke(); }
    ctx.setLineDash([]);
  }
  ctx.save(); ctx.beginPath(); ctx.rect(L, Tp, W - L - R, H - Tp - B); ctx.clip();
  for (const s of series) { ctx.strokeStyle = s.color; ctx.lineWidth = 1.5; ctx.beginPath(); s.y.forEach((v, k) => (k ? ctx.lineTo(px(x[k]), py(v)) : ctx.moveTo(px(x[k]), py(v)))); ctx.stroke(); }
  ctx.restore();
}
function traj3d(cv, xs, ys, zs, ts) {
  if (!cv || !xs) return;
  const [ctx, W, H] = fitCanvas(cv);
  const cx = (Math.min(...xs) + Math.max(...xs)) / 2, cy = (Math.min(...ys) + Math.max(...ys)) / 2;
  const span = Math.max(Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys), 0.2), zScale = 3;
  let yaw = -0.7, pitch = 0.55, drag = null;
  const proj = (x, y, z) => { const X = (x - cx) / span, Y = (y - cy) / span, Z = (z * zScale) / span; const x1 = X * Math.cos(yaw) - Y * Math.sin(yaw), y1 = X * Math.sin(yaw) + Y * Math.cos(yaw); return [W / 2 + x1 * W * 0.7, H / 2 + (y1 * Math.cos(pitch) - Z * Math.sin(pitch)) * H * 0.9]; };
  function draw() {
    ctx.fillStyle = css("--bg"); ctx.fillRect(0, 0, W, H); ctx.strokeStyle = css("--line"); ctx.lineWidth = 1;
    for (let g = -0.5; g <= 0.5001; g += 0.125) { let a = proj(cx + g * span, cy - 0.5 * span, 0), b = proj(cx + g * span, cy + 0.5 * span, 0); ctx.beginPath(); ctx.moveTo(...a); ctx.lineTo(...b); ctx.stroke(); a = proj(cx - 0.5 * span, cy + g * span, 0); b = proj(cx + 0.5 * span, cy + g * span, 0); ctx.beginPath(); ctx.moveTo(...a); ctx.lineTo(...b); ctx.stroke(); }
    ctx.strokeStyle = css("--ink"); ctx.lineWidth = 2; ctx.beginPath(); xs.forEach((x, k) => { const p = proj(x, ys[k], zs[k]); k ? ctx.lineTo(...p) : ctx.moveTo(...p); }); ctx.stroke();
    ctx.fillStyle = css("--muted"); ctx.font = "11px " + css("--mono"); ctx.fillText(`grid ${(span / 8).toFixed(2)} m · z ×${zScale} · ${ts[0].toFixed(0)}–${ts[ts.length - 1].toFixed(0)} s`, 6, H - 6);
  }
  cv.onpointerdown = (e) => { drag = [e.clientX, e.clientY, yaw, pitch]; cv.setPointerCapture(e.pointerId); };
  cv.onpointermove = (e) => { if (!drag) return; yaw = drag[2] + (e.clientX - drag[0]) * 0.01; pitch = Math.max(0.05, Math.min(1.5, drag[3] + (e.clientY - drag[1]) * 0.01)); draw(); };
  cv.onpointerup = () => { drag = null; };
  draw();
}

// ------------------------------------------------------------------ robot cards
function labelPill(l) {
  if (!l) return "";
  const st = l.status === "FAILED" ? "fail" : ["VERIFIED", "UNVERIFIED", "SIM_UNVERIFIED", "IDEAL_ONLY"].includes(l.status) ? "sim" : "prog";
  return pill4(st);
}
function robotCard(r, inJob = false) {
  const labs = [["Level 1", r.level1], ["Level 2", r.level2]].concat(Object.entries(r.labels).filter(([k]) => k !== "training_reproduction"));
  return `<div data-dir="${esc(r.dir)}">${r.internal_only ? `<span class="warnbox">${esc(T("internal"))}</span>` : ""}
    ${!inJob && r.files["replay/index.json"] ? vizSlot("lib:" + r.dir) : ""}
    <div class="media">${r.files["nominal.mp4"] ? `<div><h5>${esc(T("video"))}</h5><video controls muted playsinline preload="metadata" src="${fileUrl(r.files["nominal.mp4"])}"></video></div>`
      : r.files["nominal.webp"] ? `<div><h5>${esc(T("video"))}</h5><img class="anim" alt="" src="${fileUrl(r.files["nominal.webp"])}"></div>` : ""}
      <div><h5>${esc(T("traj3d"))}</h5><canvas class="chart c3d"></canvas></div><div><h5>${esc(T("tracking"))}</h5><canvas class="chart ctrack"></canvas></div>
      ${r.files["thermal_long.json"] ? `<div><h5>${esc(T("thermal"))}</h5><canvas class="chart ctherm"></canvas></div>` : ""}</div>
    <div class="labels">${labs.map(([n, l]) => `<div class="lab"><span>${esc(n)}</span>${labelPill(l)}<span class="why">${esc([(l?.conditions || []).join("; "), l?.reason].filter(Boolean).join(" — "))}</span></div>`).join("")}</div>
    <p class="note">${esc(T("platform"))}: ${esc(r.platform)}</p></div>`;
}
async function hydrateRobot(root, r) {
  const el = root.querySelector(`[data-dir="${CSS.escape(r.dir)}"]`);
  if (!el) return;
  if (el.querySelector(".viz-slot")) attachViz(replaySrc("lib:" + r.dir, "robot", "/api/run/replay?dir=" + encodeURIComponent(r.dir), null));
  if (r.files["trace_nominal.json"]) {
    const tr = await api(fileUrl(r.files["trace_nominal.json"])).catch(() => null);
    if (tr) { lineChart(el.querySelector(".ctrack"), tr.t, [{ y: tr.vx, color: css("--ink") }, { y: tr.cmd.map((c) => c[0]), color: css("--muted") }], "s", "m/s"); traj3d(el.querySelector(".c3d"), tr.x, tr.y, tr.z, tr.t); }
  }
  if (r.files["thermal_long.json"]) {
    const th = await api(fileUrl(r.files["thermal_long.json"])).catch(() => null);
    if (th?.temp) { const last = th.temp[th.temp.length - 1]; const j = last.indexOf(Math.max(...last)); lineChart(el.querySelector(".ctherm"), th.t, [{ y: th.temp.map((row) => row[j]), color: css("--ink") }], "s", "°C"); }
  }
}

// ------------------------------------------------------------------ library
function renderPanel() {
  document.querySelectorAll(".tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === tab)));
  const p = $("#panel");
  if (!evidence) { p.innerHTML = ""; return; }
  const empty = `<p class="empty">${esc(T("noData"))}</p>`;
  const unfinished = (kind) => {
    const rows = (evidence.unfinished || []).filter((j) => (kind === "circuits" ? j.pipeline === "circuit" : j.pipeline === "robot_bench"));
    return rows.length ? `<p class="lbl">${esc(T("failedRec"))}</p><div class="cards">${rows.map((j) => `<div class="card"><h4><span>${esc(jobTitle(j))}</span>
      <span class="state">${esc(T("state." + j.state))}</span></h4><p class="note">${esc((Array.isArray(T("err." + j.code)) ? T("err." + j.code)[0] : "") || "")}</p>
      <div class="btns"><button class="btn" type="button" data-job="${esc(j.id)}">${esc(T("openJob"))}</button></div></div>`).join("")}</div>` : "";
  };
  const bindUnfinished = () => p.querySelectorAll("[data-job]").forEach((b) => b.addEventListener("click", () => { selected = b.dataset.job; lastSig = ""; refreshJobs(); $("#detail").scrollIntoView({ behavior: "smooth" }); }));
  if (tab === "robots") {
    const duckL2 = duckRecord?.attempt2_300m?.label?.status;
    p.innerHTML = evidence.robots.length ? `<div class="cards">${evidence.robots.map((r, i) => {
      const seeds = r.labels?.seeds?.reason || "", dr = Object.values(r.robustness || {}).map((v) => `${v.survived}/${v.episodes}`).join(" · ");
      // Level 2 is a separate training run; a benchmark record written before it still says NOT_RUN
      const l2 = r.robot === "open_duck_mini_v2" && duckL2 ? duckL2 : r.level2.status;
      const plat = /^Windows/.test(r.platform) ? "Windows" : /^Linux/.test(r.platform) ? "Linux" : "";
      return `<div class="card"><h4><span>${esc(r.title)}${plat ? ` <span class="note">· ${plat}</span>` : ""}</span>${labelPill(r.level1)}</h4>
        <div class="bigrow"><div>Level 2<b class="${l2 === "VERIFIED" ? "ok" : ""}">${esc(T("v." + l2))}</b></div><div>seeds<b class="${/10\/10/.test(seeds) ? "ok" : ""}">${esc(seeds.split(" ")[0] || "—")}</b></div><div>DR<b>${esc(dr || "—")}</b></div></div>
        <details class="more" data-i="${i}"><summary>${esc(T("video"))} · 3D · ${esc(T("tracking"))}</summary>${robotCard(r)}</details></div>`;
    }).join("")}</div>` + unfinished("robots") : (unfinished("robots") || empty);
    bindUnfinished();
    p.querySelectorAll("details[data-i]").forEach((d) => d.addEventListener("toggle", () => { if (d.open && !d.dataset.done) { d.dataset.done = 1; hydrateRobot(d, evidence.robots[+d.dataset.i]); } }));
  } else if (tab === "circuits") {
    p.innerHTML = evidence.circuits.length ? `<div class="cards">${evidence.circuits.map((c) => `<div class="card"><h4><span>${esc(c.requirement)}</span>
        <span class="state ${c.achieved ? "ok" : ""}">${esc(c.achieved ? T("v.VERIFIED") : /INFEASIBLE/.test(c.status) ? T("v.INFEASIBLE") : T("v.FAILED"))}</span></h4>
        <p>${esc(lang === "zh" ? c.conclusion : "")}</p><div class="btns"><button class="btn primary" type="button" data-run="${esc(c.dir)}">${esc(T("rounds"))}</button>
        <a class="btn" href="${fileUrl(c.report_html)}" target="_blank" rel="noopener">${esc(T("report"))}</a></div></div>`).join("")}</div>` + unfinished("circuits") : (unfinished("circuits") || empty);
    bindUnfinished();
    p.querySelectorAll("[data-run]").forEach((b) => b.addEventListener("click", () => { selected = "run:" + b.dataset.run; lastSig = ""; refreshJobs(); $("#detail").scrollIntoView({ behavior: "smooth" }); }));
  } else if (tab === "trains") {
    p.innerHTML = evidence.trains.length ? `<div class="cards">${evidence.trains.map((t, i) => {
      const s = t.status || {}, l2 = t.level2?.label;
      return `<div class="card"><h4><span>${esc(t.dir.split(/[\\/]/).pop())}</span>${l2 ? labelPill({ ...l2, zh: T("v." + l2.status), en: T("v." + l2.status) }) : `<span class="state">${esc(s.stage || "")}</span>`}</h4>
        <p>${esc(T("trainSteps"))}: ${(s.steps ?? s.num_timesteps ?? 0).toLocaleString()} / ${(s.num_timesteps ?? 0).toLocaleString()}</p><canvas class="chart" data-curve="${i}"></canvas></div>`;
    }).join("")}</div>` : empty;
    p.querySelectorAll("canvas[data-curve]").forEach((cv) => { const c = evidence.trains[+cv.dataset.curve].curve.filter((r) => r[1] != null); if (c.length > 1) lineChart(cv, c.map((r) => r[0] / 1e6), [{ y: c.map((r) => r[1]), color: css("--ink") }], "M steps", ""); });
  } else {
    const kinds = Object.entries(T("kinds")).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
    p.innerHTML = `<div class="card"><h4>${esc(T("upTitle"))}</h4><p>${esc(T("upHint"))}</p>
      <form id="up-form" class="fields inline"><label>${esc(T("file"))}<input type="file" id="up-file" required></label><label>${esc(T("kind"))}<select id="up-kind">${kinds}</select></label>
      <label class="grow">${esc(T("note"))}<input type="text" id="up-note" maxlength="500"></label><button class="btn primary">${esc(T("upload"))}</button></form><p id="up-msg" class="note" aria-live="polite"></p></div>` +
      (evidence.uploads.length ? `<div class="tblwrap"><table class="checks"><thead><tr><th>${esc(T("file"))}</th><th>${esc(T("kind"))}</th><th>sha256</th><th></th><th>${esc(T("fit"))}</th></tr></thead><tbody>` +
        evidence.uploads.map((u) => `<tr><td>${esc(u.name)}</td><td>${esc(T("kinds")[u.kind] || u.kind)}</td><td>${esc(u.sha256.slice(0, 16))}…</td><td>${labelPill(u.label)}</td><td>${u.thermal_fit ? esc(u.thermal_fit.status + " " + (u.thermal_fit.reason || JSON.stringify(u.thermal_fit.result || ""))) : ""}</td></tr>`).join("") + "</tbody></table></div>" : "");
    $("#up-form").addEventListener("submit", async (e) => {
      e.preventDefault(); const f = $("#up-file").files[0]; if (!f) return;
      const q = new URLSearchParams({ name: f.name, kind: $("#up-kind").value, note: $("#up-note").value });
      try { const r = await fetch("/api/upload?" + q, { method: "POST", body: await f.arrayBuffer(), headers: { "X-RLSOK": "1" } }); if (!r.ok) throw new Error((await r.json()).error); $("#up-msg").textContent = T("uploaded"); await loadEvidence(); }
      catch (err) { $("#up-msg").textContent = err.message; }
    });
  }
}
function openUploads() { const lib = $(".library"); lib.open = true; tab = "uploads"; renderPanel(); lib.scrollIntoView({ behavior: "smooth", block: "start" }); }
let duckRecord = null;
async function loadEvidence() {
  [evidence, duckRecord] = await Promise.all([api("/api/evidence").catch(() => null), api("/api/duck-training").catch(() => null)]);
  renderPanel();
}
async function loadCaps() {
  const [s, sys] = await Promise.all([api("/api/settings").catch(() => null), api("/api/system").catch(() => null)]);
  settings = s || settings; sysinfo = sys || sysinfo;
  const g = sysinfo?.gpu, aiOk = exData?.ai_ready ?? (settings && settings.ai.provider);
  $("#caps").innerHTML = `<span class="chip ok">${esc(T("capNg"))}</span><span class="chip ${g?.present ? "ok" : ""}">${esc(T("capGpu"))}${g?.present ? " · " + g.vram_gb + " GB" : ""}</span>
    <button class="chip ${aiOk && settings?.ai?.provider !== "exchange" ? "ok" : ""}" type="button" id="chip-ai">${esc(T("capAi"))} · ${esc(T("prov." + (settings?.ai?.provider || "none")))}</button>`;
  $("#chip-ai").addEventListener("click", () => openSetup(0));
}

document.addEventListener("DOMContentLoaded", async () => {
  applyI18n(); tips();
  $("#lang").addEventListener("click", () => { lang = lang === "zh" ? "en" : "zh"; try { localStorage.setItem("rlsok.lang", lang); } catch (e) { /* ignore */ } lastSig = ""; applyI18n(); updateRoute(); refreshJobs(); renderPanel(); loadCaps(); renderExamples(); if (!$("#setup").hidden) renderSetup(); });
  const ta = $("#sentence");
  ta.addEventListener("input", () => { ta.style.height = "auto"; ta.style.height = Math.min(ta.scrollHeight, 180) + "px"; clearTimeout(routeTimer); routeTimer = setTimeout(updateRoute, 250); });
  ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#ask-form").requestSubmit(); } });
  $("#ask-form").addEventListener("submit", submit);
  $("#route-edit").addEventListener("click", () => { const p = $("#pipe-pick"); p.hidden = !p.hidden; if (!p.hidden) p.focus(); });
  $("#pipe-pick").addEventListener("change", (e) => { pipeChoice = e.target.value; $("#pipe-pick").hidden = true; updateRoute(); });
  document.querySelectorAll("[data-w]").forEach((b) => b.addEventListener("click", () => welcomePick(b.dataset.w)));
  $("#welcome-skip").addEventListener("click", async () => { $("#welcome").hidden = true; await api("/api/settings", { onboarded: true }).catch(() => {}); });
  $("#open-setup").addEventListener("click", () => openSetup(0));
  $("#setup-next").addEventListener("click", setupNext);
  $("#setup-back").addEventListener("click", () => { step = Math.max(0, step - 1); renderSetup(); });
  $("#setup-skip").addEventListener("click", async () => { await api("/api/settings", { onboarded: true }).catch(() => {}); closeSetup(); });
  $("#quit").addEventListener("click", async () => { if (!confirm(T("quitConfirm"))) return; await api("/api/shutdown", {}).catch(() => {}); document.body.innerHTML = `<p class="quitmsg">${esc(T("quitDone"))}</p>`; });
  document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => { tab = b.dataset.tab; renderPanel(); }));
  // first open: ask the one question at once; the cards and the library fill in behind it
  settings = await api("/api/settings").catch(() => null);
  if (settings && !settings.onboarded) openWelcome();
  exReady = renderExamples();
  await Promise.all([loadEvidence(), exReady]); loadCaps(); refreshJobs();
  setInterval(() => { refreshJobs(); if (tab === "trains") loadEvidence(); }, 3000);
});
