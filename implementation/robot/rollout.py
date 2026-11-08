"""Closed-loop policy rollout in RLSOK's own MuJoCo (CPU).

Everything recorded here is measured from the running simulation; there are
no stored results.  Optional pieces: domain randomisation sample, action
latency, base pushes, BAM servo actuation (identified motor dynamics instead
of the MJCF position servo), first-order motor thermal model, MP4 video.
"""

from __future__ import annotations

import collections
import copy
import math
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import mujoco
import numpy as np

from . import thermal as thermal_mod
from .adapters import PolicyIO, RobotAdapter
from .motors import MotorLibrary, bam_model, import_bam
from .randomization import DRSpec, Randomizer, Sample, for_robot


@dataclass
class Segment:
    duration: float
    vx: float = 0.0
    vy: float = 0.0
    wz: float = 0.0
    name: str = ""

    def cmd(self):
        return np.array([self.vx, self.vy, self.wz])


@dataclass
class RolloutConfig:
    schedule: list[Segment]
    seed: int = 0
    dr: DRSpec | None = None
    actuation: str = "mjcf"           # mjcf | bam
    bam_model: str = "m6"
    thermal: bool = True
    video_path: Path | None = None
    video_size: tuple[int, int] = (480, 360)
    video_fps: int = 25
    transient_s: float = 1.0          # ignore this long after each command change
    stop_on_fall: bool = True
    init_noise: float = 0.0           # rad, uniform joint offset at reset
    record_every: int = 1             # control steps between trace samples
    record_qpos: bool = False         # full generalized position per control step (cross-checks)
    replay_hz: float | None = None    # world pose of every body at this rate, for the console's 3-D replay


@dataclass
class RolloutResult:
    robot: str
    seed: int
    duration_s: float
    survived_s: float
    fell: bool
    fall_time: float | None
    segments: list[dict]
    trace: dict
    thermal: dict | None
    torque: dict
    dr_sample: dict | None
    wall_s: float
    realtime_factor: float
    video: str | None
    actuation: str
    notes: list[str] = field(default_factory=list)
    poses: dict | None = None          # {t, pos (n, nbody, 3), quat (n, nbody, 4)} when replay_hz is set

    def summary(self) -> dict:
        d = {k: getattr(self, k) for k in ("robot", "seed", "duration_s", "survived_s", "fell",
                                           "fall_time", "segments", "torque", "dr_sample", "wall_s",
                                           "realtime_factor", "video", "actuation", "notes")}
        if self.thermal:
            d["thermal"] = {k: v for k, v in self.thermal.items() if k != "trace"}
        return d


class DelayLine:
    """Action latency: at substep `now` the motor sees the newest target that was
    issued at or before `now - delay` substeps (or `initial` if none yet)."""

    def __init__(self, delay_steps: int, initial):
        self.delay = int(delay_steps)
        self.initial = initial
        self.q: collections.deque = collections.deque()

    def push(self, step: int, value):
        self.q.append((step, value))

    def get(self, now: int):
        cut = now - self.delay
        while len(self.q) > 1 and self.q[1][0] <= cut:
            self.q.popleft()
        if self.q and self.q[0][0] <= cut:
            return self.q[0][1]
        return self.initial


class _Video:
    """MP4 via ffmpeg when available; otherwise an animated WebP via Pillow (no extra binary needed,
    plays in any browser). WebP keeps every 2nd frame at 360x270 to bound memory and size."""

    def __init__(self, m, path: Path, size, fps, distance, body):
        self.mode = "ffmpeg" if shutil.which("ffmpeg") else None
        if self.mode is None:
            try:
                import PIL.Image  # noqa: F401
                self.mode = "webp"
            except ImportError:
                self.mode = None
        self.ok = self.mode is not None
        self.path = path if self.mode != "webp" else Path(path).with_suffix(".webp")
        self.size = size
        self.fps = fps
        self.frames = []
        self.n = 0
        if not self.ok:
            return
        if os.name != "nt":
            os.environ.setdefault("MUJOCO_GL", "osmesa")
        w, h = size
        m.vis.global_.offwidth = max(m.vis.global_.offwidth, w)
        m.vis.global_.offheight = max(m.vis.global_.offheight, h)
        self.r = mujoco.Renderer(m, h, w)
        self.cam = mujoco.MjvCamera()
        self.cam.type = mujoco.mjtCamera.mjCAMERA_TRACKING
        self.cam.trackbodyid = body
        self.cam.distance = distance
        self.cam.elevation = -15
        self.cam.azimuth = 120
        path.parent.mkdir(parents=True, exist_ok=True)
        if self.mode == "webp":
            return
        self.p = subprocess.Popen(
            ["ffmpeg", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
             "-s", f"{w}x{h}", "-r", str(fps), "-i", "-", "-c:v", "libx264", "-preset", "veryfast",
             "-crf", "30", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(path)],
            stdin=subprocess.PIPE)

    def frame(self, d):
        if not self.ok:
            return
        self.r.update_scene(d, self.cam)
        # software GL: shadows/reflections cost >10x and add nothing to the evidence
        self.r.scene.flags[mujoco.mjtRndFlag.mjRND_SHADOW] = 0
        self.r.scene.flags[mujoco.mjtRndFlag.mjRND_REFLECTION] = 0
        if self.mode == "webp":
            self.n += 1
            if self.n % 2:
                from PIL import Image
                self.frames.append(Image.fromarray(self.r.render()).resize((360, 270)))
            return
        self.p.stdin.write(self.r.render().tobytes())

    def close(self):
        if not self.ok:
            return None
        self.r.close()
        if self.mode == "webp":
            if not self.frames:
                return None
            self.frames[0].save(self.path, save_all=True, append_images=self.frames[1:], loop=0,
                                duration=int(2000 / self.fps), quality=55, method=4)
            self.frames = []
            return str(self.path)
        self.p.stdin.close()
        self.p.wait()
        return str(self.path) if self.p.returncode == 0 else None


class _BamActuation:
    """Replace the MJCF position servos by BAM's identified servo model."""

    def __init__(self, adapter: RobotAdapter, m, d, model_name: str, notes: list):
        rec = MotorLibrary().get(adapter.motor_id, model_name)
        import_bam()
        from bam.mujoco import MujocoController

        names = [m.actuator(a).name for a in adapter.act_ids]
        for a in adapter.act_ids:   # torque motors: ctrl == joint torque
            m.actuator_gaintype[a] = mujoco.mjtGain.mjGAIN_FIXED
            m.actuator_biastype[a] = mujoco.mjtBias.mjBIAS_NONE
            m.actuator_gainprm[a, :] = 0.0
            m.actuator_gainprm[a, 0] = 1.0
            m.actuator_biasprm[a, :] = 0.0
            m.actuator_ctrllimited[a] = 0
            m.actuator_forcelimited[a] = 0
        self.names = names
        self.ctl = MujocoController(bam_model(rec), names, m, d)
        self.record = rec
        notes.append(f"执行器 = BAM {rec.motor_id}/{rec.model} 辨识模型（替换 MJCF 位置伺服）")

    def set_targets(self, tgt):
        self.ctl.q_target[:] = tgt

    def update(self):
        self.ctl.update()


def run(adapter: RobotAdapter, cfg: RolloutConfig) -> RolloutResult:
    """Run one episode.  The adapter's model is restored afterwards (BAM mode and
    randomisation work on a private copy) so episodes never contaminate each other."""
    nominal = adapter.model
    adapter.model = copy.deepcopy(nominal)
    try:
        return _run(adapter, cfg)
    finally:
        adapter.model = nominal


def _run(adapter: RobotAdapter, cfg: RolloutConfig) -> RolloutResult:
    m = adapter.model
    rng = np.random.default_rng(cfg.seed)
    notes: list[str] = []
    randomizer = Randomizer(m, root_body=adapter.root_body, floor_geoms=adapter.floor_geoms,
                            foot_geoms=adapter.foot_geoms, actuated_dofs=list(adapter.dadr))
    sample = Sample(values={})
    spec = cfg.dr
    if spec is not None and spec.name != "none":
        spec = for_robot(spec, float(m.body_mass[adapter.root_body]))
        sample = randomizer.apply(spec, rng)
    d = mujoco.MjData(m)
    # Derived constants (subtree masses, invweights) must follow randomised masses.
    # mj_setConst overwrites d.qpos with qpos0, so it (and BAM's controller, which
    # calls it too) must run before the reset below.
    mujoco.mj_setConst(m, d)
    bam = None
    if cfg.actuation == "bam":
        if adapter.motor_id is None:
            raise ValueError(f"{adapter.key}: no motor library id, BAM actuation impossible")
        bam = _BamActuation(adapter, m, d, cfg.bam_model, notes)
        if spec is not None and (spec.joint_frictionloss or spec.joint_armature):
            notes.append("BAM 模式下关节摩擦与 armature 由 BAM 模型每步重算，随机化的这两项不生效")
    io = PolicyIO()
    adapter.reset(d, io)
    if cfg.init_noise:
        d.qpos[adapter.qadr] += rng.uniform(-cfg.init_noise, cfg.init_noise, adapter.n)
        mujoco.mj_forward(m, d)
    if bam is not None:
        bam.ctl.reset()

    th = None
    lib_rec = MotorLibrary().get(adapter.motor_id) if adapter.motor_id else None
    if cfg.thermal and lib_rec is not None and lib_rec.known and lib_rec.resistance:
        tp = thermal_mod.params_for(adapter.motor_id)
        if tp is not None:
            th = thermal_mod.ThermalState(tp, lib_rec.kt, lib_rec.resistance, adapter.n)

    sim_dt = float(m.opt.timestep)
    n_sub = int(round(adapter.ctrl_dt / sim_dt))
    lat_steps = int(round(sample.latency_s / sim_dt))

    push_next = math.inf
    if spec is not None and spec.push_interval_s is not None and spec.push_velocity > 0:
        push_next = float(spec.push_interval_s.sample(rng))

    total = sum(s.duration for s in cfg.schedule)
    n_ctrl = int(round(total / adapter.ctrl_dt))
    seg_bounds = np.cumsum([0.0] + [s.duration for s in cfg.schedule])

    video = None
    if cfg.video_path is not None:
        video = _Video(m, Path(cfg.video_path), cfg.video_size, cfg.video_fps,
                       adapter.camera_distance, adapter.root_body)
    frame_every = max(1, int(round(1.0 / (cfg.video_fps * adapter.ctrl_dt))))

    tr = collections.defaultdict(list)
    tau_abs_max = np.zeros(adapter.n)
    tau_sq_sum = np.zeros(adapter.n)
    sat_count = np.zeros(adapter.n)
    fr = m.actuator_forcerange[adapter.act_ids].copy()
    fr_lim = np.where(fr[:, 1] > fr[:, 0], np.maximum(abs(fr[:, 0]), abs(fr[:, 1])), np.inf)
    n_sub_total = 0
    fell, fall_time = False, None

    # Upstream Open Duck loop steps the sim before its first policy call.
    for _ in range(getattr(adapter, "warmup_substeps", 0)):
        mujoco.mj_step(m, d)

    delay = DelayLine(lat_steps, adapter.default.copy())  # motors hold default before 1st command
    rep_every = max(1, int(round(1.0 / (cfg.replay_hz * adapter.ctrl_dt)))) if cfg.replay_hz else 0
    poses = {"t": [], "pos": [], "quat": []} if rep_every else None
    t0 = time.perf_counter()
    k = 0
    for k in range(n_ctrl):
        t = k * adapter.ctrl_dt
        si = int(np.searchsorted(seg_bounds, t, side="right") - 1)
        si = min(si, len(cfg.schedule) - 1)
        cmd = cfg.schedule[si].cmd()
        adapter.pre_policy(io)
        obs = adapter.observe(d, io, cmd, sample.encoder_bias)
        action = adapter.act(obs)
        tgt = adapter.targets(action, io)
        delay.push(n_sub_total, tgt)
        for _ in range(n_sub):
            applied = delay.get(n_sub_total)
            if bam is not None:
                bam.set_targets(applied)
                bam.update()
            else:
                adapter.apply_targets(d, applied)
            if d.time >= push_next:
                ang = rng.uniform(-math.pi, math.pi)
                d.qvel[adapter.fv:adapter.fv + 2] += spec.push_velocity * np.array([math.cos(ang), math.sin(ang)])
                push_next = d.time + float(spec.push_interval_s.sample(rng))
                tr["push_t"].append(round(d.time, 3))
            mujoco.mj_step(m, d)
            n_sub_total += 1
            tau = d.qfrc_actuator[adapter.dadr]
            a = np.abs(tau)
            tau_abs_max = np.maximum(tau_abs_max, a)
            tau_sq_sum += tau * tau
            sat_count += a >= 0.99 * fr_lim
            if th is not None:
                th.step(tau, sim_dt)
        io.step += 1
        if k % cfg.record_every == 0:
            vx, vy, wz = adapter.heading_velocity(d)
            tr["t"].append(round(d.time, 4))
            tr["vx"].append(vx)
            tr["vy"].append(vy)
            tr["wz"].append(wz)
            tr["cmd"].append(cmd.tolist())
            tr["z"].append(float(adapter.base_pos(d)[2]))
            tr["x"].append(float(adapter.base_pos(d)[0]))
            tr["y"].append(float(adapter.base_pos(d)[1]))
            tr["tilt"].append(adapter.tilt(d))
            if th is not None:
                tr["temp_max"].append(float(th.temp.max()))
                tr["temp"].append(np.round(th.temp, 3).tolist())
            if cfg.record_qpos:
                tr["qpos"].append(d.qpos.tolist())
                tr["action"].append(action.tolist())
        if video is not None and k % frame_every == 0:
            video.frame(d)
        if poses is not None and k % rep_every == 0:
            poses["t"].append(float(d.time)); poses["pos"].append(d.xpos.copy()); poses["quat"].append(d.xquat.copy())
        if not np.all(np.isfinite(d.qpos)):
            fell, fall_time = True, d.time
            notes.append("数值发散（qpos 非有限值）")
            break
        if adapter.fallen(d):
            fell, fall_time = True, d.time
            if cfg.stop_on_fall:
                break
    wall = time.perf_counter() - t0
    video_out = video.close() if video is not None else None

    survived = (fall_time if fell else n_ctrl * adapter.ctrl_dt)
    segs = _segment_metrics(cfg, tr, adapter.ctrl_dt * cfg.record_every)
    names = adapter.joint_names
    torque = {
        "peak_abs_Nm": dict(zip(names, np.round(tau_abs_max, 4).tolist())),
        "rms_Nm": dict(zip(names, np.round(np.sqrt(tau_sq_sum / max(1, n_sub_total)), 4).tolist())),
        "saturation_fraction": dict(zip(names, np.round(sat_count / max(1, n_sub_total), 4).tolist())),
        "limit_Nm": dict(zip(names, [None if not math.isfinite(x) else float(x) for x in fr_lim])),
    }
    thermal = None
    if th is not None:
        thermal = {
            "params": th.p.to_json(),
            "motor": lib_rec.motor_id, "kt": lib_rec.kt, "R20": lib_rec.resistance,
            "final_C": dict(zip(names, np.round(th.temp, 3).tolist())),
            "energy_J": dict(zip(names, np.round(th.energy, 3).tolist())),
            "hottest_joint": names[int(np.argmax(th.temp))],
            "peak_C": float(th.temp.max()),
            "time_to_limit_s_at_mean_power": {
                n: _ttl(th.energy[i] / max(survived, 1e-9), th.p) for i, n in enumerate(names)},
        }
    return RolloutResult(
        robot=adapter.key, seed=cfg.seed, duration_s=total, survived_s=float(survived), fell=fell,
        fall_time=fall_time, segments=segs, trace=dict(tr), thermal=thermal, torque=torque,
        dr_sample=sample.to_json() if sample.values or sample.latency_s else None,
        wall_s=wall, realtime_factor=float(survived / wall) if wall > 0 else 0.0,
        video=video_out, actuation=cfg.actuation, notes=notes, poses=poses,
    )


def _ttl(power, p):
    v = thermal_mod.time_to_limit(float(power), p)
    return None if math.isinf(v) else round(v, 1)


def _segment_metrics(cfg: RolloutConfig, tr: dict, dt: float) -> list[dict]:
    out = []
    t = np.asarray(tr.get("t", []))
    start = 0.0
    for s in cfg.schedule:
        end = start + s.duration
        sel = (t > start + cfg.transient_s) & (t <= end)
        row = {"name": s.name, "cmd": [s.vx, s.vy, s.wz], "t0": start, "t1": end,
               "samples": int(sel.sum())}
        if sel.sum() > 0:
            for key, c in (("vx", s.vx), ("vy", s.vy), ("wz", s.wz)):
                v = np.asarray(tr[key])[sel]
                row[f"{key}_mean"] = float(v.mean())
                row[f"{key}_abs_err"] = float(abs(v.mean() - c))
                row[f"{key}_rmse"] = float(np.sqrt(np.mean((v - c) ** 2)))
        row["complete"] = bool(len(t) and t[-1] >= end - 1e-6)
        out.append(row)
        start = end
    return out
