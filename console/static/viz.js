// RLSOK simulation viewer: 3-D replay of bird missions and robot rollouts, animated circuit waveforms.
// Everything shown comes from the simulation's own files (rlsok.replay/1, circuit wave JSON). Every view carries a
// fixed "simulation" badge so a replay is never mistaken for camera footage of real hardware.
import * as THREE from "three";
import { OrbitControls } from "./vendor/OrbitControls.js";
import { STLLoader } from "./vendor/STLLoader.js";

THREE.Object3D.DEFAULT_UP.set(0, 0, 1);            // z up, like MuJoCo and the bird simulator

const SPEEDS = [0.25, 0.5, 1, 2, 4];
const PHASE_COL = { climb: "#5b8def", cruise1: "#2fa36b", turn: "#c9821f", cruise2: "#2fa36b", descent: "#8a6bd1", flare: "#d0565c" };
const W = { zh: {
  badge: "仿真画面", badgeSub: "不是真机拍摄", badgeCirc: "仿真波形 · 不是示波器实测", fpv: "机载摄像头（仿真）",
  play: "播放", pause: "暂停", follow: "跟随", orbit: "环绕", ground: "地面观察", birdview: "鸟的视角", speed: "速度", run: "任务",
  live: "仿真进行中 · 已完成 {n}/{m}", liveWait: "仿真进行中 · 正在计算第一组…", passed: "完成", failed: "未通过",
  h: "高度", v: "空速", att: "姿态角", bat: "电池", freq: "扑动频率", pitch: "俯仰", bank: "坡度", ref: "参考",
  phases: { climb: "爬升", cruise1: "平飞", turn: "转弯", cruise2: "平飞", descent: "下降", flare: "进近着地" },
  runs: { nominal: "无风、标准情况", wind_headwind2: "逆风 2 m/s", wind_crosswind2: "侧风 2 m/s", wind_gusts: "阵风" },
  lag: "姿态反应慢：俯仰 {p} 秒、坡度 {b} 秒", osc: "姿态会来回摆：频率 {w}、阻尼 {z}", noData: "正在载入回放…", round: "第 {n} 轮", drag: "拖动旋转 · 滚轮缩放",
  vx: "前进速度", cmd: "指令", z: "躯干高度", roll: "横滚", seed: "种子", wave: "波形", t: "时间" },
 en: {
  badge: "Simulation", badgeSub: "not real footage", badgeCirc: "Simulated waveform · not a scope measurement", fpv: "On-board camera (simulated)",
  play: "Play", pause: "Pause", follow: "Follow", orbit: "Orbit", ground: "Ground view", birdview: "Bird's view", speed: "Speed", run: "Run",
  live: "Simulation running · {n}/{m} done", liveWait: "Simulation running · computing the first run…", passed: "completed", failed: "did not pass",
  h: "Height", v: "Airspeed", att: "Attitude", bat: "Battery", freq: "Flap frequency", pitch: "pitch", bank: "bank", ref: "reference",
  phases: { climb: "Climb", cruise1: "Cruise", turn: "Turn", cruise2: "Cruise", descent: "Descent", flare: "Approach + touchdown" },
  runs: { nominal: "No wind, baseline", wind_headwind2: "Headwind 2 m/s", wind_crosswind2: "Crosswind 2 m/s", wind_gusts: "Gusts" },
  lag: "Slow attitude response: pitch {p} s, bank {b} s", osc: "Swinging attitude: frequency {w}, damping {z}", noData: "Loading the replay…", round: "Round {n}", drag: "drag to rotate · wheel to zoom",
  vx: "forward speed", cmd: "command", z: "torso height", roll: "roll", seed: "seed", wave: "waveform", t: "time" } };

const lang = () => (document.documentElement.lang || "zh").startsWith("zh") ? "zh" : "en";
const tr = (k, vars) => { let s = k.split(".").reduce((o, p) => (o ? o[p] : undefined), W[lang()]) ?? k; if (vars) for (const [a, b] of Object.entries(vars)) s = s.replace(`{${a}}`, b); return s; };
const fileUrl = (base, rel) => "/file?path=" + encodeURIComponent(base + "/" + rel);
const f32 = (b64) => { const bin = atob(b64), u = new Uint8Array(bin.length); for (let i = 0; i < bin.length; i++) u[i] = bin.charCodeAt(i); return new Float32Array(u.buffer); };
const u32 = (b64) => { const bin = atob(b64), u = new Uint8Array(bin.length); for (let i = 0; i < bin.length; i++) u[i] = bin.charCodeAt(i); return new Uint32Array(u.buffer); };
const getJSON = async (url) => { const r = await fetch(url, { cache: "no-store" }); if (!r.ok) throw new Error(r.status); return r.json(); };
const el = (tag, cls, html) => { const e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; };
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const cssv = (v, d) => getComputedStyle(document.documentElement).getPropertyValue(v).trim() || d;

function runLabel(name) {
  const m = /^lag_pitch([\d.]+)_bank([\d.]+)$/.exec(name);
  if (m) return tr("lag", { p: m[1], b: m[2] });
  const o = /^osc_wn([\d.]+)_zeta([\d.]+)$/.exec(name);
  if (o) return tr("osc", { w: o[1], z: o[2] });
  const s = /^seed[_-]?(\d+)$/.exec(name);
  if (s) return `${tr("seed")} ${s[1]}`;
  const k = tr("runs." + name); return k.startsWith("runs.") ? name : k;
}

// ---------------------------------------------------------------- shared shell: stage, badge, controls, charts
class Shell {
  constructor(root, opts) {
    this.root = root; this.opts = opts;
    root.classList.add("viz");
    root.innerHTML = "";
    this.stage = el("div", "viz-stage");
    this.badge = el("div", "viz-badge", `<b>${esc(opts.circuit ? tr("badgeCirc") : tr("badge"))}</b>${opts.circuit ? "" : `<span>${esc(tr("badgeSub"))}</span>`}`);
    this.liveTag = el("div", "viz-live"); this.liveTag.hidden = true;
    this.hud = el("div", "viz-hud");
    this.msg = el("div", "viz-msg"); this.msg.hidden = true;
    this.stage.append(this.badge, this.liveTag, this.hud, this.msg);
    const c = el("div", "viz-ctrl");
    this.btnPlay = el("button", "viz-play"); this.btnPlay.type = "button";
    this.time = el("span", "viz-time");
    const barWrap = el("div", "viz-barwrap");
    this.phaseBar = el("div", "viz-phases");
    this.bar = el("input"); this.bar.type = "range"; this.bar.min = 0; this.bar.max = 1000; this.bar.value = 0; this.bar.className = "viz-bar";
    this.bar.setAttribute("aria-label", tr("t"));
    barWrap.append(this.phaseBar, this.bar);
    this.speed = el("select", "viz-speed"); this.speed.setAttribute("aria-label", tr("speed"));
    this.speed.innerHTML = SPEEDS.map((s) => `<option value="${s}" ${s === 1 ? "selected" : ""}>${s}×</option>`).join("");
    this.views = el("div", "viz-views");
    c.append(this.btnPlay, this.time, barWrap, this.speed, this.views);
    this.runsBox = el("div", "viz-runs");
    this.charts = el("div", "viz-charts");
    root.append(this.stage, c, this.runsBox, this.charts);
    this.playing = false; this.t = 0; this.T = 1; this.rate = 1; this.last = 0; this.dragging = false;
    this.btnPlay.addEventListener("click", () => this.setPlaying(!this.playing));
    this.bar.addEventListener("input", () => { this.dragging = true; this.seek(this.T * this.bar.value / 1000); });
    this.bar.addEventListener("change", () => { this.dragging = false; });
    this.speed.addEventListener("change", () => { this.rate = +this.speed.value; });
    root.tabIndex = 0;
    root.addEventListener("keydown", (e) => { if (e.key === " " && e.target === root) { e.preventDefault(); this.setPlaying(!this.playing); } });
    this.setPlaying(false);
    this.loop = this.loop.bind(this);
    this.alive = true; requestAnimationFrame(this.loop);
  }
  setPlaying(p) { this.playing = p; this.btnPlay.textContent = p ? "❚❚" : "▶"; this.btnPlay.title = tr(p ? "pause" : "play"); this.btnPlay.setAttribute("aria-label", this.btnPlay.title); this.last = performance.now(); }
  seek(t) { this.t = Math.max(0, Math.min(this.T, t)); this.frame(this.t); }
  setPhases(segs, T) {
    this.phaseBar.innerHTML = (segs || []).map(([t0, ph], i) => {
      const t1 = i + 1 < segs.length ? segs[i + 1][0] : T;
      return `<i style="left:${(100 * t0 / T).toFixed(2)}%;width:${(100 * (t1 - t0) / T).toFixed(2)}%;background:${PHASE_COL[ph] || "#888"}" title="${esc(tr("phases." + ph))}"></i>`;
    }).join("");
  }
  setRuns(runs, cur, onPick) {
    this.runsBox.innerHTML = runs.map((r, i) => `<button type="button" data-i="${i}" aria-pressed="${i === cur}" class="${r.passed === false ? "bad" : r.passed ? "ok" : ""}">
      <span class="dot"></span>${esc(r.label)}</button>`).join("");
    this.runsBox.querySelectorAll("button").forEach((b) => b.addEventListener("click", () => onPick(+b.dataset.i)));
  }
  setViews(list, cur, onPick) {
    this.views.innerHTML = list.map(([k, label]) => `<button type="button" data-k="${k}" aria-pressed="${k === cur}">${esc(label)}</button>`).join("");
    this.views.querySelectorAll("button").forEach((b) => b.addEventListener("click", () => { onPick(b.dataset.k); if (b.isConnected) this.views.querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", String(x === b))); }));
  }
  setLive(text) { this.liveTag.hidden = !text; this.liveTag.textContent = text || ""; }
  loop(now) {
    if (!this.alive) return;
    const dt = Math.min(0.1, (now - this.last) / 1000); this.last = now;
    if (this.playing && !this.dragging && this.root.isConnected) {
      this.t += dt * this.rate;
      if (this.t >= this.T) { this.t = this.T; this.setPlaying(false); }
    }
    if (this.root.isConnected) {
      try { this.frame(this.t); } catch (e) { if (!this.warned) { console.error(e); this.warned = true; } }
      if (!this.dragging) this.bar.value = String(Math.round(1000 * this.t / (this.T || 1)));
    }
    requestAnimationFrame(this.loop);
  }
  dispose() { this.alive = false; }
}

// ---------------------------------------------------------------- mini charts with a playhead
class Charts {
  constructor(box) { this.box = box; this.items = []; }
  set(defs) {
    this.box.innerHTML = ""; this.items = [];
    for (const d of defs) {
      const card = el("div", "viz-chart"), head = el("div", "viz-chart-h", `<span>${esc(d.title)}</span><b></b>`), cv = el("canvas");
      card.append(head, cv); this.box.append(card);
      this.items.push({ d, cv, val: head.querySelector("b"), bg: null, w: 0 });
    }
  }
  draw(t, tAxis) {
    for (const it of this.items) {
      const r = it.cv.getBoundingClientRect(); if (r.width < 10) continue;
      const dpr = Math.min(devicePixelRatio || 1, 2), Wd = r.width, H = r.height;
      if (!it.bg || it.w !== Wd) {
        it.w = Wd; it.cv.width = Wd * dpr; it.cv.height = H * dpr;
        const off = document.createElement("canvas"); off.width = it.cv.width; off.height = it.cv.height;
        const g = off.getContext("2d"); g.setTransform(dpr, 0, 0, dpr, 0, 0);
        const all = it.d.lines.flatMap((l) => Array.from(l.y).filter(Number.isFinite));
        let y0 = Math.min(...all), y1 = Math.max(...all); if (!isFinite(y0)) { y0 = 0; y1 = 1; } if (y1 - y0 < 1e-6) { y0 -= 1; y1 += 1; }
        const pad = (y1 - y0) * 0.1; y0 -= pad; y1 += pad;
        it.sx = (x) => 4 + (x - tAxis[0]) / ((tAxis[tAxis.length - 1] - tAxis[0]) || 1) * (Wd - 8);
        it.sy = (y) => 4 + (1 - (y - y0) / (y1 - y0)) * (H - 8);
        g.fillStyle = cssv("--panel", "#151918"); g.fillRect(0, 0, Wd, H);
        g.strokeStyle = cssv("--line", "#2a302d"); g.lineWidth = 1;
        for (let k = 1; k < 4; k++) { const y = 4 + k * (H - 8) / 4; g.beginPath(); g.moveTo(4, y); g.lineTo(Wd - 4, y); g.stroke(); }
        for (const l of it.d.lines) {
          g.strokeStyle = l.color; g.lineWidth = 1.4; g.setLineDash(l.dash ? [4, 3] : []); g.beginPath();
          let started = false;
          for (let i = 0; i < l.y.length; i++) { if (!Number.isFinite(l.y[i])) continue; const X = it.sx(tAxis[i]), Y = it.sy(l.y[i]); started ? g.lineTo(X, Y) : g.moveTo(X, Y); started = true; }
          g.stroke();
        }
        g.setLineDash([]); g.fillStyle = cssv("--muted", "#8a958f"); g.font = "10px " + cssv("--mono", "monospace");
        const dg = y1 - y0 < 0.5 ? 3 : y1 - y0 < 5 ? 2 : 1; g.fillText(`${y1.toFixed(dg)}`, 6, 13); g.fillText(`${y0.toFixed(dg)}`, 6, H - 6);
        it.bg = off;
      }
      const ctx = it.cv.getContext("2d"); ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.drawImage(it.bg, 0, 0); ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      const X = it.sx(t); ctx.strokeStyle = cssv("--ink", "#e9e6dc"); ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(X, 2); ctx.lineTo(X, H - 2); ctx.stroke();
      const vals = it.d.lines.filter((l) => !l.dash).map((l) => { const v = sampleAt(l.y, tAxis, t); return Number.isFinite(v) ? `${l.name ? l.name + " " : ""}${v.toFixed(l.digits ?? 1)}` : "—"; });
      it.val.textContent = vals.join(" · ") + (it.d.unit ? " " + it.d.unit : "");
    }
  }
}
function sampleAt(y, tA, t) {
  if (!y || !y.length) return NaN;
  let lo = 0, hi = tA.length - 1;
  if (t <= tA[0]) return y[0]; if (t >= tA[hi]) return y[hi];
  while (hi - lo > 1) { const m = (lo + hi) >> 1; (tA[m] <= t ? (lo = m) : (hi = m)); }
  const a = (t - tA[lo]) / (tA[hi] - tA[lo] || 1); return y[lo] * (1 - a) + y[hi] * a;
}
function idxAt(tA, t) { let lo = 0, hi = tA.length - 1; if (t <= tA[0]) return [0, 0, 0]; if (t >= tA[hi]) return [hi, hi, 0]; while (hi - lo > 1) { const m = (lo + hi) >> 1; (tA[m] <= t ? (lo = m) : (hi = m)); } return [lo, hi, (t - tA[lo]) / (tA[hi] - tA[lo] || 1)]; }

// ---------------------------------------------------------------- 3-D world shared by bird and robot replays
function makeWorld(container, opts) {
  const renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: false });
  renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 2));
  renderer.shadowMap.enabled = true; renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  container.prepend(renderer.domElement);
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(opts.sky || 0xbcd4ea);
  scene.fog = new THREE.Fog(opts.sky || 0xbcd4ea, opts.fogNear || 60, opts.fogFar || 420);
  scene.add(new THREE.HemisphereLight(0xe8f1ff, 0x5a6b4a, 1.1));
  const sun = new THREE.DirectionalLight(0xffffff, 1.6); sun.position.set(30, -20, 60); sun.castShadow = true;
  sun.shadow.mapSize.set(2048, 2048); const sc = sun.shadow.camera; sc.left = sc.bottom = -opts.shadowR; sc.right = sc.top = opts.shadowR; sc.near = 1; sc.far = 200;
  scene.add(sun, sun.target);
  // ground: procedural checker so motion and height read clearly
  const cv = document.createElement("canvas"); cv.width = cv.height = 256; const g = cv.getContext("2d");
  g.fillStyle = opts.groundA || "#6f9a55"; g.fillRect(0, 0, 256, 256); g.fillStyle = opts.groundB || "#65904d"; g.fillRect(0, 0, 128, 128); g.fillRect(128, 128, 128, 128);
  const tex = new THREE.CanvasTexture(cv); tex.wrapS = tex.wrapT = THREE.RepeatWrapping; tex.anisotropy = 8; tex.colorSpace = THREE.SRGBColorSpace;
  const G = opts.groundSize || 3000; tex.repeat.set(G / (opts.tile || 10), G / (opts.tile || 10));
  const ground = new THREE.Mesh(new THREE.PlaneGeometry(G, G), new THREE.MeshLambertMaterial({ map: tex }));
  ground.receiveShadow = true; scene.add(ground);
  const camera = new THREE.PerspectiveCamera(45, 16 / 9, 0.02, 2000);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true; controls.dampingFactor = 0.12; controls.minDistance = opts.minDist || 0.3; controls.maxDistance = opts.maxDist || 300;
  controls.maxPolarAngle = Math.PI * 0.495;
  return { renderer, scene, camera, controls, sun };
}
function resize(world, box) {
  const r = box.getBoundingClientRect(); const w = Math.max(200, r.width), h = Math.max(160, r.height);
  if (world.w !== w || world.h !== h) { world.w = w; world.h = h; world.renderer.setSize(w, h, false); world.camera.aspect = w / h; world.camera.updateProjectionMatrix(); }
}

// ---------------------------------------------------------------- bird
function wingShape(semi, root, tip) {
  // R1 outline, same as its cutting template (x chord-wise forward, y span-wise)
  const pts = [[0, 0], [0.45 * semi, 0.02], [0.85 * semi, -0.01], [semi, -0.05], [0.97 * semi, -0.05 - 0.6 * tip],
    [0.8 * semi, -0.03 - tip], [0.45 * semi, -0.03 - 0.5 * (root + tip)], [0, -root]];
  const s = new THREE.Shape(); pts.forEach(([sp, ch], i) => (i ? s.lineTo(ch, sp) : s.moveTo(ch, sp))); s.closePath();
  return new THREE.ShapeGeometry(s);
}
function featherGeo(len, width) {
  // one primary / tail feather: rounded root, widest at 40 %, pointed tip; along +y, centred on x = 0
  const s = new THREE.Shape(), w = width / 2;
  s.moveTo(0, 0); s.bezierCurveTo(w, 0.02 * len, w, 0.4 * len, w * 0.7, 0.75 * len); s.quadraticCurveTo(w * 0.2, 0.98 * len, 0, len);
  s.quadraticCurveTo(-w * 0.2, 0.98 * len, -w * 0.7, 0.75 * len); s.bezierCurveTo(-w, 0.4 * len, -w, 0.02 * len, 0, 0);
  return new THREE.ShapeGeometry(s, 8);
}
function birdWing(w, colors) {
  // planform stations [r/semi, LE x, chord, eff]: arm + hand membrane up to hand_from, then separated primaries
  const g = new THREE.Group(), semi = w.semi_span_m, P = w.planform, st = P.stations, hand = P.hand_from ?? 0.76;
  const at = (r) => { let k = 0; while (k < st.length - 2 && st[k + 1][0] < r) k++; const a = (r - st[k][0]) / (st[k + 1][0] - st[k][0] || 1);
    return [st[k][1] + (st[k + 1][1] - st[k][1]) * a, st[k][2] + (st[k + 1][2] - st[k][2]) * a]; };
  const rs = []; for (let r = 0; r <= hand + 1e-6; r += 0.02) rs.push(Math.min(r, hand));
  const s = new THREE.Shape();
  rs.forEach((r, i) => { const [le] = at(r); i ? s.lineTo(le, r * semi) : s.moveTo(le, r * semi); });
  for (let i = rs.length - 1; i >= 0; i--) { const [le, c] = at(rs[i]); s.lineTo(le - c, rs[i] * semi); }
  s.closePath();
  const membrane = new THREE.Mesh(new THREE.ShapeGeometry(s), new THREE.MeshStandardMaterial({ color: colors.wing, roughness: 0.85, side: THREE.DoubleSide }));
  membrane.castShadow = true; g.add(membrane);
  // darker trailing-edge band (secondaries) for a bird-like look
  const band = new THREE.Shape(); rs.forEach((r, i) => { const [le, c] = at(r); const x = le - c * 0.78; i ? band.lineTo(x, r * semi) : band.moveTo(x, r * semi); });
  for (let i = rs.length - 1; i >= 0; i--) { const [le, c] = at(rs[i]); band.lineTo(le - c, rs[i] * semi); } band.closePath();
  const bm = new THREE.Mesh(new THREE.ShapeGeometry(band), new THREE.MeshStandardMaterial({ color: colors.edge, roughness: 0.85, side: THREE.DoubleSide }));
  bm.position.z = 0.0008; g.add(bm);
  // primaries: fanned "fingers" from the hand, longest at the leading edge
  const n = P.primaries || 5, [leH, cH] = at(hand), tipLen = (1 - hand) * semi + 0.05;
  for (let k = 0; k < n; k++) {
    const f = (k + 0.5) / n, len = tipLen * (1.05 - 0.13 * k), width = Math.max(0.028, cH / n * 1.25);
    const fm = new THREE.Mesh(featherGeo(len, width), new THREE.MeshStandardMaterial({ color: k < 2 ? colors.tip : colors.edge, roughness: 0.8, side: THREE.DoubleSide }));
    fm.position.set(leH - f * cH * 0.9, hand * semi - 0.03, 0.0005 * k); fm.rotation.z = THREE.MathUtils.degToRad(4 + 7 * k); fm.castShadow = true; g.add(fm);
  }
  const spar = new THREE.Mesh(new THREE.CylinderGeometry(0.002, 0.002, semi * hand, 6), new THREE.MeshStandardMaterial({ color: 0x222222 }));
  spar.position.set(at(hand / 2)[0] - 0.004, semi * hand / 2, 0.001); g.add(spar);
  return g;
}
function fanTail(t, color, edge) {
  const g = new THREE.Group(), n = t.feathers || 7, spread = THREE.MathUtils.degToRad(t.spread_deg || 70), L = t.chord_m || 0.16, notch = t.notch ?? 0.15;
  for (let k = 0; k < n; k++) {
    const a = -spread / 2 + spread * k / (n - 1), mid = Math.abs(k - (n - 1) / 2) / ((n - 1) / 2);
    const len = L * (1 - notch * (1 - mid));                                 // shallow notch: central feathers shorter
    const fm = new THREE.Mesh(featherGeo(len, Math.max(0.035, L * 0.3)), new THREE.MeshStandardMaterial({ color: k % 2 ? color : edge, roughness: 0.85, side: THREE.DoubleSide }));
    fm.rotation.z = Math.PI / 2 + a; fm.position.z = 0.0006 * k; fm.castShadow = true; g.add(fm);
  }
  return g;
}
export async function buildBird(rig, base, resolveMesh=null, strict=false) {
  const bird = new THREE.Group();
  const mat = (c) => new THREE.MeshStandardMaterial({ color: c, roughness: 0.75, metalness: 0.05, side: THREE.DoubleSide });
  const loader = new STLLoader();
  const parts = {};
  for (const m of rig.meshes || []) {
    try {
      const response=await fetch(resolveMesh?resolveMesh(m):fileUrl(base,m.file));if(!response.ok)throw Error('Native CAD mesh unavailable');const buf=await response.arrayBuffer();
      const geo = loader.parse(buf); geo.scale(0.001, 0.001, 0.001); geo.computeBoundingBox(); geo.computeVertexNormals();
      parts[m.name] = geo;
    } catch (e) { if(strict)throw e; /* Historical replay retains its existing fallback. */ }
  }
  const w = rig.wing, amp = THREE.MathUtils.degToRad(rig.amplitude_deg || 30), bias = THREE.MathUtils.degToRad(rig.bias_deg || 0);
  const dihedral = THREE.MathUtils.degToRad(rig.dihedral_deg || 0);
  const wings = [];
  const A = rig.assembly;
  if (A && A.meshes) {
    // ---- assembled bird-like shape (R2 and later): parts at their positions, gull-like colours
    const C = { body: 0xf1efe9, back: 0xc9cdd2, head: 0xf4f2ec, beak: 0xe2b33b, wing: 0xc6cbd1, edge: 0x8f969e, tip: 0x2b2d30, tail: 0xe9e7e1 };
    const col = { head: C.head, chest: C.body, rear_body: C.back };
    for (const [name, pos] of Object.entries(A.meshes)) {
      if (!parts[name]) continue;
      const m = new THREE.Mesh(parts[name], mat(col[name] ?? C.body)); m.position.set(pos[0] / 1000, pos[1] / 1000, pos[2] / 1000); m.castShadow = true; bird.add(m);
    }
    if (parts.head) { const hb = parts.head.boundingBox; bird.userData.nose = A.meshes.head[0] / 1000 + hb.max.x; } else bird.userData.nose = 0.2;
    // beak tint: a small cone over the printed beak so the colour reads at a distance
    const beak = new THREE.Mesh(new THREE.ConeGeometry(0.0092, 0.036, 16), mat(C.beak)); beak.rotation.z = -Math.PI / 2;
    beak.position.set(A.meshes.head[0] / 1000 + 0.058, 0, A.meshes.head[2] / 1000 + 0.002); bird.add(beak);
    for (const s of [1, -1]) { const eye = new THREE.Mesh(new THREE.SphereGeometry(0.0045, 12, 10), mat(0x161616)); eye.position.set(A.meshes.head[0] / 1000 + 0.036, s * 0.019, A.meshes.head[2] / 1000 + 0.016); bird.add(eye); }
    const [wx, wy, wz] = A.wing_root_mm.map((v) => v / 1000);
    for (const side of [1, -1]) {
      const pivot = new THREE.Group(); pivot.position.set(wx, side * wy, wz);
      const wg = birdWing(w, C); wg.scale.y = side; pivot.add(wg);
      pivot.userData = { side, k: 0 }; bird.add(pivot); wings.push(pivot);
    }
    const [tx, ty, tz] = A.tail_root_mm.map((v) => v / 1000);
    const tail = fanTail(rig.tail || {}, C.tail, C.edge); tail.position.set(tx, ty, tz); bird.add(tail);
    if (A.camera_mm) bird.userData.cam = A.camera_mm.map((v) => v / 1000);
  } else {
    // ---- R1 layout: body_mid centred, head stacked in front
    let front = 0.095, back = -0.095, halfW = 0.032, top = 0.029;
    if (parts.body_mid) {
      const b = parts.body_mid.boundingBox, c = new THREE.Vector3(); b.getCenter(c); parts.body_mid.translate(-c.x, -c.y, -c.z);
      front = (b.max.x - b.min.x) / 2; back = -front; halfW = (b.max.y - b.min.y) / 2; top = (b.max.z - b.min.z) / 2;
      const body = new THREE.Mesh(parts.body_mid, mat(0x8d867b)); body.castShadow = true; bird.add(body);
    } else {
      const body = new THREE.Mesh(new THREE.SphereGeometry(1, 24, 16), mat(0x8d867b)); body.scale.set(0.1, 0.032, 0.029); body.castShadow = true; bird.add(body);
    }
    if (parts.head) {
      const b = parts.head.boundingBox, c = new THREE.Vector3(); b.getCenter(c); parts.head.translate(front - b.min.x, -c.y, -c.z);
      const head = new THREE.Mesh(parts.head, mat(0x9b958a)); head.castShadow = true; bird.add(head);
      bird.userData.nose = front + (b.max.x - b.min.x);
    } else bird.userData.nose = front + 0.06;
    const nps = rig.wings_per_side || 1;
    for (const side of [1, -1]) for (let k = 0; k < nps; k++) {
      const pivot = new THREE.Group(); pivot.position.set(front * 0.55 - k * 0.02, side * halfW, top * 0.7 + k * 0.01);
      const m = new THREE.Mesh(wingShape(w.semi_span_m, w.root_chord_m, w.tip_chord_m), new THREE.MeshStandardMaterial({ color: 0xb7ab95, roughness: 0.9, side: THREE.DoubleSide, transparent: true, opacity: 0.92 }));
      m.scale.y = side; m.castShadow = true; pivot.add(m);
      const spar = new THREE.Mesh(new THREE.CylinderGeometry(0.002, 0.002, w.semi_span_m * 0.85, 6), mat(0x222222));
      spar.position.set(0.002, side * w.semi_span_m * 0.425, 0.001); pivot.add(spar);
      pivot.userData = { side, k }; bird.add(pivot); wings.push(pivot);
    }
    const tail = rig.tail || {}, ts = tail.span_m || 0.22, tc = tail.chord_m || 0.12;
    const boom = new THREE.Mesh(new THREE.CylinderGeometry(0.003, 0.003, 0.2, 6), mat(0x1d1d1d)); boom.rotation.z = Math.PI / 2; boom.position.set(back - 0.1, 0, 0.005); bird.add(boom);
    const fs = new THREE.Shape(); fs.moveTo(0, ts / 2); fs.lineTo(0, -ts / 2); fs.lineTo(-tc, -ts * 0.3); fs.lineTo(-tc * 1.15, 0); fs.lineTo(-tc, ts * 0.3); fs.closePath();
    const fan = new THREE.Mesh(new THREE.ShapeGeometry(fs), mat(0x7d7366)); fan.position.set(back - 0.2, 0, 0.01); fan.castShadow = true; bird.add(fan);
  }
  bird.userData.flap = (phase) => { for (const p of wings) { const a = bias + dihedral + amp * Math.sin(phase + p.userData.k * Math.PI); p.rotation.x = p.userData.side * a; } };
  return bird;
}

class BirdViewer {
  constructor(root, src) {
    this.src = src; this.shell = new Shell(root, {}); this.shell.frame = (t) => this.frame(t);
    this.runs = []; this.cur = 0; this.data = null; this.view = "follow"; this.swap = false;
    this.fpvBox = el("div", "viz-fpv", `<span>${esc(tr("fpv"))}</span><em>${esc(tr("badge"))}</em>`);
    this.fpvBox.title = tr("birdview");
    this.fpvBox.addEventListener("click", () => { this.swap = !this.swap; this.syncViews(); });
    this.shell.stage.append(this.fpvBox);
    this.shell.hud.textContent = tr("drag");
    this.charts = new Charts(this.shell.charts);
    this.init();
  }
  syncViews() { this.shell.setViews([["follow", tr("follow")], ["ground", tr("ground")], ["bird", tr("birdview")]], this.swap ? "bird" : this.view, (k) => this.setView(k)); }
  setView(k) {
    if (k === "bird") { this.swap = true; } else { this.swap = false; this.view = k; this.followInit = false; }
    this.syncViews();
  }
  async init() {
    let idx;
    try { idx = await getJSON(this.src.indexUrl()); } catch (e) { this.shell.msg.hidden = false; this.shell.msg.textContent = tr("noData"); this.retry(); return; }
    this.idx = idx; this.shell.msg.hidden = true;
    this.world = makeWorld(this.shell.stage, { shadowR: 6, tile: 10 });
    this.trees();
    this.bird = await buildBird(idx.rig, idx.base); this.world.scene.add(this.bird);
    this.shadowBlob();
    this.fpvCam = new THREE.PerspectiveCamera(72, 4 / 3, 0.03, 900);
    this.fpvCam.up.set(0, 0, 1);
    this.bird.add(this.fpvCam);
    const cp = this.bird.userData.cam || [this.bird.userData.nose + 0.02, 0, 0];
    this.fpvCam.position.set(cp[0], cp[1], cp[2]); this.fpvCam.lookAt(new THREE.Vector3(cp[0] + 5, cp[1], cp[2] - 0.9));      // ahead, 10 deg down
    this.path = new THREE.Line(new THREE.BufferGeometry(), new THREE.LineBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0.55 }));
    this.world.scene.add(this.path);
    this.syncViews();
    await this.applyIndex(idx);
    if (idx.live) this.poll();
  }
  retry() { if (!this.src.live()) return; clearTimeout(this.rt); this.rt = setTimeout(() => this.init(), 2500); }
  trees() {
    const trunk = new THREE.InstancedMesh(new THREE.CylinderGeometry(0.12, 0.16, 3, 6), new THREE.MeshLambertMaterial({ color: 0x6b4f35 }), 160);
    const crown = new THREE.InstancedMesh(new THREE.ConeGeometry(1.4, 4, 8), new THREE.MeshLambertMaterial({ color: 0x2f6b34 }), 160);
    const m = new THREE.Matrix4(), q = new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(1, 0, 0), Math.PI / 2);
    let seed = 7; const rnd = () => ((seed = (seed * 16807) % 2147483647) / 2147483647);
    for (let i = 0; i < 160; i++) {
      const x = -60 + rnd() * 700, y = (rnd() < 0.5 ? -1 : 1) * (12 + rnd() * 90), s = 0.7 + rnd() * 0.8;
      m.compose(new THREE.Vector3(x, y, 1.5 * s), q, new THREE.Vector3(s, s, s)); trunk.setMatrixAt(i, m);
      m.compose(new THREE.Vector3(x, y, (3 + 2) * s), q, new THREE.Vector3(s, s, s)); crown.setMatrixAt(i, m);
    }
    trunk.castShadow = crown.castShadow = true; this.world.scene.add(trunk, crown);
    const pad = new THREE.Mesh(new THREE.CircleGeometry(0.6, 32), new THREE.MeshBasicMaterial({ color: 0xf0c419 })); pad.position.z = 0.01; this.world.scene.add(pad);
  }
  shadowBlob() {
    const blob = new THREE.Mesh(new THREE.CircleGeometry(0.35, 24), new THREE.MeshBasicMaterial({ color: 0x000000, transparent: true, opacity: 0.18, depthWrite: false }));
    blob.position.z = 0.015; this.blob = blob; this.world.scene.add(blob);
  }
  async applyIndex(idx) {
    this.idx = idx;
    const planned = idx.planned || [];
    const done = idx.runs || [];
    this.runs = planned.length ? planned.map((n) => { const r = done.find((x) => x.name === n); return { name: n, label: runLabel(n), passed: r ? r.passed : undefined, file: r?.file }; })
      : done.map((r) => ({ name: r.name, label: runLabel(r.name), passed: r.passed, file: r.file }));
    this.shell.setRuns(this.runs, this.cur, (i) => this.pick(i));
    this.shell.setLive(idx.live ? (done.length ? tr("live", { n: done.length, m: planned.length || done.length }) : tr("liveWait")) : "");
    if (!this.data && this.runs[this.cur]?.file) await this.pick(this.cur, true);
  }
  async poll() {
    clearTimeout(this.pt);
    this.pt = setTimeout(async () => {
      if (!this.shell.alive) return;
      try { const idx = await getJSON(this.src.indexUrl()); await this.applyIndex(idx); if (!idx.live) return; } catch (e) { /* index being rewritten */ }
      this.poll();
    }, 2000);
  }
  async pick(i, autoplay) {
    const r = this.runs[i]; if (!r?.file) return;
    this.cur = i; this.shell.setRuns(this.runs, i, (k) => this.pick(k));
    const d = await getJSON(fileUrl(this.idx.base, r.file));
    const C = {}; for (const [k, v] of Object.entries(d.channels)) C[k] = f32(v);
    this.data = { C, d }; const t = C.t; this.shell.T = t[t.length - 1]; this.shell.t = 0;
    this.shell.setPhases(d.phases, this.shell.T);
    const pts = []; for (let k = 0; k < t.length; k++) pts.push(new THREE.Vector3(C.x[k], C.y[k], Math.max(C.h[k], 0.02)));
    this.path.geometry.dispose(); this.path.geometry = new THREE.BufferGeometry().setFromPoints(pts);
    const deg = (a) => Float32Array.from(a, (v) => v * 180 / Math.PI);
    this.charts.set([
      { title: tr("h"), unit: "m", lines: [{ y: C.h, color: "#2fa36b" }, { y: C.alt_ref, color: "#8a958f", dash: true }] },
      { title: tr("v"), unit: "m/s", lines: [{ y: C.V, color: "#5b8def" }] },
      { title: `${tr("att")} (${tr("pitch")} / ${tr("bank")})`, unit: "°", lines: [{ y: deg(C.pitch), color: "#c9821f", name: tr("pitch") }, { y: deg(C.bank), color: "#d0565c", name: tr("bank") }] },
      { title: tr("bat"), unit: "%", lines: [{ y: C.battery_pct, color: "#e5b94e", digits: 1 }] },
      { title: tr("freq"), unit: "Hz", lines: [{ y: C.freq, color: "#8a6bd1", digits: 2 }] }]);
    this.followInit = false;
    if (autoplay !== false) this.shell.setPlaying(true);
  }
  state(t) {
    const C = this.data.C, [i, j, a] = idxAt(C.t, t), L = (k) => C[k][i] * (1 - a) + C[k][j] * a;
    let dchi = C.chi[j] - C.chi[i]; if (dchi > Math.PI) dchi -= 2 * Math.PI; if (dchi < -Math.PI) dchi += 2 * Math.PI;
    const ph = this.data.d.phases.filter((p) => p[0] <= t + 1e-6).pop()?.[1];
    return { x: L("x"), y: L("y"), h: Math.max(L("h"), 0.03), chi: C.chi[i] + dchi * a, pitch: L("pitch"), bank: L("bank"), flap: L("flap_phase"), V: L("V"), bat: L("battery_pct"), freq: L("freq"), phase: ph };
  }
  frame(t) {
    if (!this.world || !this.bird) return;
    resize(this.world, this.shell.stage);
    const W_ = this.world; let s = null;
    if (this.data) {
      s = this.state(t);
      this.bird.position.set(s.x, s.y, s.h);
      this.bird.rotation.set(s.bank, -s.pitch, s.chi, "ZYX");
      this.bird.userData.flap(s.flap);
      this.blob.position.set(s.x, s.y, 0.015); this.blob.scale.setScalar(1 + s.h * 0.08); this.blob.material.opacity = Math.max(0.05, 0.22 - s.h * 0.008);
      W_.sun.position.set(s.x + 30, s.y - 20, 60); W_.sun.target.position.set(s.x, s.y, 0);
      this.shell.time.textContent = `${t.toFixed(1)} / ${this.shell.T.toFixed(1)} s`;
      this.shell.hud.innerHTML = `<b>${esc(tr("phases." + s.phase))}</b> · ${esc(tr("h"))} ${s.h.toFixed(1)} m · ${esc(tr("v"))} ${s.V.toFixed(1)} m/s · ${esc(tr("bat"))} ${Number.isFinite(s.bat) ? s.bat.toFixed(1) : "—"} %`;
      this.charts.draw(t, this.data.C.t);
    } else { this.bird.position.set(0, 0, 1.5); this.bird.userData.flap(performance.now() / 300); }
    const target = this.bird.position;
    const cam = W_.camera, ctl = W_.controls;
    if (this.view === "follow") {
      if (!this.followInit) { const back = new THREE.Vector3(-3.2, -1.2, 1.0).applyAxisAngle(new THREE.Vector3(0, 0, 1), s ? s.chi : 0); cam.position.copy(target).add(back); ctl.target.copy(target); this.followInit = true; this.prevT = target.clone(); }
      const d = target.clone().sub(this.prevT || target); cam.position.add(d); ctl.target.copy(target); this.prevT = target.clone();
    } else if (this.view === "ground") {
      cam.position.set(-6, -10, 1.7); ctl.target.copy(target);
    }
    ctl.update();
    const r = W_.renderer, Wd = W_.w, Hd = W_.h;
    const mainCam = this.swap ? this.fpvCam : cam, insetCam = this.swap ? cam : this.fpvCam;
    this.fpvCam.aspect = this.swap ? Wd / Hd : 4 / 3; this.fpvCam.updateProjectionMatrix();
    r.setScissorTest(false); r.setViewport(0, 0, Wd, Hd); r.render(W_.scene, mainCam);
    const iw = Math.round(Math.min(360, Wd * 0.32)), ih = Math.round(iw * 0.75), m = 10;
    if (document.documentElement.dataset.film !== "1") {    // film mode (website clip): main view only, no inset
      if (insetCam === cam) { cam.aspect = iw / ih; cam.updateProjectionMatrix(); }
      r.setScissorTest(true); r.setScissor(Wd - iw - m, m, iw, ih); r.setViewport(Wd - iw - m, m, iw, ih); r.render(W_.scene, insetCam); r.setScissorTest(false);
      if (insetCam === cam) { cam.aspect = Wd / Hd; cam.updateProjectionMatrix(); }
    }
    Object.assign(this.fpvBox.style, { width: iw + "px", height: ih + "px", right: m + "px", bottom: m + "px" });
    this.fpvBox.querySelector("span").textContent = this.swap ? tr(this.view === "ground" ? "ground" : "follow") : tr("fpv");
  }
}

// ---------------------------------------------------------------- robot (MuJoCo bodies)
class RobotViewer {
  constructor(root, src) {
    this.src = src; this.shell = new Shell(root, {}); this.shell.frame = (t) => this.frame(t);
    this.charts = new Charts(this.shell.charts); this.cur = 0; this.data = null; this.view = "follow";
    this.shell.hud.textContent = tr("drag");
    this.init();
  }
  async init() {
    let idx;
    try { idx = await getJSON(this.src.indexUrl()); } catch (e) { this.shell.msg.hidden = false; this.shell.msg.textContent = tr("noData"); if (this.src.live()) setTimeout(() => this.init(), 2500); return; }
    this.idx = idx; this.shell.msg.hidden = true;
    this.world = makeWorld(this.shell.stage, { shadowR: 3, tile: 1, groundA: "#d9d6cc", groundB: "#c9c5ba", sky: 0xd8e2ea, fogNear: 15, fogFar: 80, minDist: 0.4, maxDist: 30 });
    const scene = await getJSON(fileUrl(idx.base, idx.scene));
    this.bodies = []; const meshes = {};
    for (const m of scene.meshes) { const g = new THREE.BufferGeometry(); g.setAttribute("position", new THREE.BufferAttribute(f32(m.v), 3)); g.setIndex(new THREE.BufferAttribute(u32(m.f), 1)); g.computeVertexNormals(); meshes[m.id] = g; }
    for (let b = 0; b < scene.nbody; b++) { const grp = new THREE.Group(); this.world.scene.add(grp); this.bodies.push(grp); }
    for (const gm of scene.geoms) {
      let geo = null; const s = gm.size;
      if (gm.type === "mesh") geo = meshes[gm.mesh];
      else if (gm.type === "box") geo = new THREE.BoxGeometry(2 * s[0], 2 * s[1], 2 * s[2]);
      else if (gm.type === "sphere") geo = new THREE.SphereGeometry(s[0], 16, 12);
      else if (gm.type === "capsule") { geo = new THREE.CapsuleGeometry(s[0], 2 * s[1], 6, 12); geo.rotateX(Math.PI / 2); }
      else if (gm.type === "cylinder") { geo = new THREE.CylinderGeometry(s[0], s[0], 2 * s[1], 16); geo.rotateX(Math.PI / 2); }
      else if (gm.type === "ellipsoid") { geo = new THREE.SphereGeometry(1, 16, 12); geo.scale(s[0], s[1], s[2]); }
      if (!geo) continue;
      const [r, g, b, a] = gm.rgba;
      const mesh = new THREE.Mesh(geo, new THREE.MeshStandardMaterial({ color: new THREE.Color(r, g, b), roughness: 0.6, metalness: 0.1, transparent: a < 1, opacity: a }));
      mesh.position.set(...gm.pos); mesh.quaternion.set(gm.quat[1], gm.quat[2], gm.quat[3], gm.quat[0]);
      mesh.castShadow = true; this.bodies[gm.body].add(mesh);
    }
    this.shell.setViews([["follow", tr("follow")], ["orbit", tr("orbit")]], this.view, (k) => { this.view = k; this.followInit = false; });
    await this.applyIndex(idx);
    if (idx.live) this.poll();
  }
  async applyIndex(idx) {
    this.idx = idx;
    this.runs = (idx.runs || []).map((r) => ({ ...r, label: runLabel(r.name) }));
    this.shell.setRuns(this.runs, this.cur, (i) => this.pick(i));
    this.shell.setLive(idx.live ? tr("live", { n: this.runs.length, m: idx.planned?.length || this.runs.length }) : "");
    if (!this.data && this.runs[this.cur]) await this.pick(this.cur, true);
  }
  async poll() { clearTimeout(this.pt); this.pt = setTimeout(async () => { if (!this.shell.alive) return; try { const i = await getJSON(this.src.indexUrl()); await this.applyIndex(i); if (!i.live) return; } catch (e) { /* retry */ } this.poll(); }, 2000); }
  async pick(i, autoplay) {
    const r = this.runs[i]; if (!r) return; this.cur = i; this.shell.setRuns(this.runs, i, (k) => this.pick(k));
    const d = await getJSON(fileUrl(this.idx.base, r.file));
    const t = f32(d.t), pos = f32(d.pos), quat = f32(d.quat);
    this.data = { t, pos, quat, nb: d.nbody, root: d.root ?? 1 };
    // camera distance follows the robot's size: initial torso height (G1 about 0.78 m, Open Duck about 0.16 m)
    this.size = Math.max(0.4, Math.min(1.5, pos[3 * this.data.root + 2] / 0.78));
    this.world.controls.minDistance = 0.4 * this.size;
    this.shell.T = t[t.length - 1]; this.shell.t = 0; this.shell.setPhases([], this.shell.T);
    this.charts.set((d.series || []).map((s) => ({ title: lang() === "zh" ? s.zh : s.en, unit: s.unit, lines: s.lines.map((l, k) => ({ y: f32(l.y), color: ["#5b8def", "#8a958f", "#c9821f", "#d0565c"][k % 4], dash: l.dash, name: lang() === "zh" ? l.zh : l.en, digits: 2 })) })));
    this.followInit = false; if (autoplay !== false) this.shell.setPlaying(true);
  }
  frame(t) {
    if (!this.world) return; resize(this.world, this.shell.stage);
    if (this.data) {
      const D = this.data, [i, j, a] = idxAt(D.t, t), q1 = new THREE.Quaternion(), q2 = new THREE.Quaternion();
      for (let b = 0; b < D.nb; b++) {
        const p = 3 * (i * D.nb + b), p2 = 3 * (j * D.nb + b), o = 4 * (i * D.nb + b), o2 = 4 * (j * D.nb + b);
        this.bodies[b].position.set(D.pos[p] * (1 - a) + D.pos[p2] * a, D.pos[p + 1] * (1 - a) + D.pos[p2 + 1] * a, D.pos[p + 2] * (1 - a) + D.pos[p2 + 2] * a);
        q1.set(D.quat[o + 1], D.quat[o + 2], D.quat[o + 3], D.quat[o]); q2.set(D.quat[o2 + 1], D.quat[o2 + 2], D.quat[o2 + 3], D.quat[o2]);
        this.bodies[b].quaternion.copy(q1.slerp(q2, a));
      }
      this.shell.time.textContent = `${t.toFixed(1)} / ${this.shell.T.toFixed(1)} s`;
      this.charts.draw(t, D.t);
      const root = this.bodies[D.root].position, cam = this.world.camera, ctl = this.world.controls;
      if (this.view === "follow") {
        if (!this.followInit) { const k = this.size || 1; cam.position.set(root.x - 1.6 * k, root.y - 2.2 * k, root.z + 0.6 * k); this.prev = root.clone(); this.followInit = true; }
        cam.position.add(root.clone().sub(this.prev)); this.prev = root.clone();
      }
      ctl.target.copy(root); this.world.sun.position.set(root.x + 4, root.y - 3, 8); this.world.sun.target.position.copy(root);
    }
    this.world.controls.update();
    this.world.renderer.render(this.world.scene, this.world.camera);
  }
}

// ---------------------------------------------------------------- circuit: animated waveform
class CircuitViewer {
  constructor(root, src) {
    this.src = src; this.shell = new Shell(root, { circuit: true }); this.shell.frame = (t) => this.frame(t);
    this.cv = el("canvas", "viz-scope"); this.shell.stage.prepend(this.cv); this.shell.stage.classList.add("scope");
    this.shell.T = 6; this.shell.views.remove(); this.shell.charts.remove();
    this.init();
  }
  async init() {
    this.rounds = this.src.rounds();
    this.shell.setRuns(this.rounds.map((r) => ({ label: tr("round", { n: r.round }), passed: r.passed })), this.rounds.length - 1, (i) => this.pick(i));
    await this.pick(this.rounds.length - 1);
  }
  async pick(i) {
    const r = this.rounds[i]; if (!r) return; this.cur = i;
    this.shell.setRuns(this.rounds.map((x) => ({ label: tr("round", { n: x.round }), passed: x.passed })), i, (k) => this.pick(k));
    try { this.w = await getJSON(this.src.waveUrl(r.round)); } catch (e) { this.w = null; }
    this.shell.t = 0; this.shell.setPlaying(true);
  }
  frame(t) {
    const cv = this.cv, rr = cv.getBoundingClientRect(); if (rr.width < 10 || !this.w?.series?.length) return;
    const dpr = Math.min(devicePixelRatio || 1, 2), Wd = rr.width, H = rr.height;
    if (cv.width !== Math.round(Wd * dpr)) { cv.width = Wd * dpr; cv.height = H * dpr; }
    const g = cv.getContext("2d"); g.setTransform(dpr, 0, 0, dpr, 0, 0);
    const s = this.w.series[0], x = s.x, y = s.y, n = x.length, frac = t / this.shell.T;
    const lims = []; for (const c of this.w.limits || []) { if (c.min != null) lims.push(c.min); if (c.max != null) lims.push(c.max); if (c.value != null) lims.push(c.value); }
    const all = y.concat(lims); let y0 = Math.min(...all), y1 = Math.max(...all); const pd = (y1 - y0) * 0.1 || 1; y0 -= pd; y1 += pd;
    const L = 64, R = 16, T0 = 62, B = 30, px = (v) => L + (v - x[0]) / (x[n - 1] - x[0] || 1) * (Wd - L - R), py = (v) => T0 + (1 - (v - y0) / (y1 - y0)) * (H - T0 - B);
    g.fillStyle = "#0d0f0e"; g.fillRect(0, 0, Wd, H);
    g.strokeStyle = "#26302b"; g.lineWidth = 1; g.font = "11px monospace"; g.fillStyle = "#8a958f";
    for (let k = 0; k <= 5; k++) { const v = y0 + (y1 - y0) * k / 5; g.beginPath(); g.moveTo(L, py(v)); g.lineTo(Wd - R, py(v)); g.stroke(); g.fillText(`${v.toPrecision(4)} ${s.unit || ""}`, 4, py(v) + 4); }
    for (let k = 0; k <= 6; k++) { const v = x[0] + (x[n - 1] - x[0]) * k / 6; g.beginPath(); g.moveTo(px(v), T0); g.lineTo(px(v), H - B); g.stroke(); g.fillText(`${v.toPrecision(3)} ${this.w.xunit}`, px(v) - 14, H - 10); }
    g.setLineDash([6, 4]); g.strokeStyle = "#e9e6dc"; for (const l of lims) { g.beginPath(); g.moveTo(L, py(l)); g.lineTo(Wd - R, py(l)); g.stroke(); } g.setLineDash([]);
    const upto = Math.max(1, Math.floor(frac * (n - 1)));
    g.strokeStyle = "rgba(47,210,124,.25)"; g.lineWidth = 4; g.beginPath(); for (let k = 0; k <= upto; k++) k ? g.lineTo(px(x[k]), py(y[k])) : g.moveTo(px(x[k]), py(y[k])); g.stroke();
    g.strokeStyle = "#2fd27c"; g.lineWidth = 1.6; g.stroke();
    const xc = px(x[upto]), yc = py(y[upto]); g.fillStyle = "#2fd27c"; g.beginPath(); g.arc(xc, yc, 4, 0, 7); g.fill();
    g.fillStyle = "#e9e6dc"; g.font = "600 13px monospace"; g.textAlign = "right";
    g.fillText(`${x[upto].toPrecision(4)} ${this.w.xunit}  →  ${y[upto].toPrecision(5)} ${s.unit || ""}`, Wd - R, 24); g.textAlign = "left";
    this.shell.time.textContent = `${(100 * frac).toFixed(0)} %`;
  }
}

// ---------------------------------------------------------------- public API used by app.js
window.RLSOKViz = {
  /** src: {kind: "bird"|"robot", indexUrl(): string, live(): bool} or {kind: "circuit", rounds(): [{round, passed}], waveUrl(r)} */
  mount(root, src) {
    const V = src.kind === "bird" ? BirdViewer : src.kind === "robot" ? RobotViewer : CircuitViewer;
    const v = new V(root, src); root._viz = v; return v;
  },
  unmount(root) { if (root?._viz) { root._viz.shell.dispose(); root._viz.world?.renderer.dispose(); root._viz = null; } },
};
document.dispatchEvent(new Event("rlsok-viz-ready"));
