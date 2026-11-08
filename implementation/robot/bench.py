"""Benchmark runner: applies release/ROBOT_BENCHMARK_CRITERIA.json to real runs.

Writes everything to one evidence directory (kept under 50 MB):
  run.json            machine-readable result with labels and file hashes
  nominal.mp4         simulation video of the nominal schedule
  trace_nominal.json  per-control-step trace (velocity, height, tilt, temperatures)
  thermal_long.json   long-walk temperature curves (robots with a library motor)
  charts/*.png        tracking + temperature charts
  upstream_reference.json  the upstream code's log used for the cross-check
"""

from __future__ import annotations

import hashlib
import json
import platform
import shutil
import sys
import time
from pathlib import Path

import mujoco
import numpy as np

from . import crosscheck
from . import replay as RP
from .adapters import ADAPTERS, RobotAdapter
from .labels import Status, label
from .randomization import PRESETS
from .rollout import RolloutConfig, Segment, run

ROOT = Path(__file__).resolve().parents[2]
CRITERIA = ROOT / "release" / "ROBOT_BENCHMARK_CRITERIA.json"
EVIDENCE_LIMIT = 50 * 1024 * 1024


def load_criteria() -> dict:
    return json.loads(CRITERIA.read_text(encoding="utf-8"))


def schedule_for(key: str, criteria: dict | None = None) -> list[Segment]:
    c = criteria or load_criteria()
    return [Segment(s[0], s[1], s[2], s[3], s[4]) for s in c["schedules"][key]]


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _chart_tracking(trace: dict, path: Path, title: str):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    t = np.array(trace["t"])
    cmd = np.array(trace["cmd"])
    fig, axes = plt.subplots(3, 1, figsize=(8, 6), sharex=True)
    for ax, key, i, unit in zip(axes, ("vx", "vy", "wz"), range(3), ("m/s", "m/s", "rad/s")):
        ax.plot(t, trace[key], color="#2563eb", lw=1.0, label="measured")
        ax.plot(t, cmd[:, i], color="#111827", lw=1.2, ls="--", label="command")
        ax.set_ylabel(f"{key} [{unit}]")
        ax.grid(alpha=0.3)
    axes[0].legend(loc="upper right", fontsize=8)
    axes[-1].set_xlabel("time [s]")
    fig.suptitle(title, fontsize=10)
    fig.tight_layout()
    fig.savefig(path, dpi=90)
    plt.close(fig)


def _chart_temperature(trace: dict, joints: list[str], t_max: float, path: Path, title: str):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    t = np.array(trace["t"])
    temp = np.array(trace["temp"])
    fig, ax = plt.subplots(figsize=(8, 4))
    order = np.argsort(-temp[-1])
    for j in order[:6]:
        ax.plot(t, temp[:, j], lw=1.2, label=joints[j])
    ax.axhline(t_max, color="#dc2626", ls="--", lw=1, label=f"limit {t_max:.0f} °C (assumed)")
    ax.set_xlabel("time [s]")
    ax.set_ylabel("winding temp [°C]")
    ax.set_title("first-order model; R_th and tau are ASSUMED (unverified)", fontsize=8)
    ax.grid(alpha=0.3)
    ax.legend(fontsize=7, ncol=2)
    fig.suptitle(title, fontsize=10)
    fig.tight_layout()
    fig.savefig(path, dpi=90)
    plt.close(fig)


def _walking_segments(segs: list[dict]) -> list[tuple[dict, str, float]]:
    out = []
    for s in segs:
        c = s["cmd"]
        if any(abs(x) > 1e-9 for x in c):
            axis = ("vx", "vy", "wz")[int(np.argmax(np.abs(c)))]
            out.append((s, axis, c[("vx", "vy", "wz").index(axis)]))
    return out


def run_benchmark(key: str, out: Path, *, reference: Path | None = None, seeds: int | None = None,
                  dr_episodes: int | None = None, long_walk_s: float = 300.0,
                  policy: Path | None = None, policy_label: str = "official") -> dict:
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "charts").mkdir(exist_ok=True)
    crit = load_criteria()
    t_start = time.time()
    adapter: RobotAdapter = ADAPTERS[key](policy)
    sched = schedule_for(key, crit)
    seeds = seeds or 10
    dr_episodes = dr_episodes or crit["robustness"]["episodes_per_preset"]
    result: dict = {
        "benchmark": key, "policy_label": policy_label, "robot": adapter.describe(),
        "criteria_file": {"path": "release/ROBOT_BENCHMARK_CRITERIA.json", "sha256": _sha(CRITERIA)},
        "environment": {"python": sys.version.split()[0], "mujoco": mujoco.__version__,
                        "platform": platform.platform(), "machine": platform.machine()},
        "internal_only": adapter.internal_only,
        "sections": {},
    }
    S = result["sections"]

    # 1. engine consistency against the upstream code ------------------------------
    if reference is not None and Path(reference).exists() and policy_label == "official":
        shutil.copy2(reference, out / "upstream_reference.json")
        cc = crosscheck.duck(out / "upstream_reference.json") if key == "open_duck_mini_v2" \
            else crosscheck.g1(out / "upstream_reference.json")
        cc["label"] = label(Status.VERIFIED if cc["pass"] else Status.FAILED,
                            "官方已训练策略、平地、名义模型（无随机化）",
                            f"RLSOK MuJoCo {mujoco.__version__} CPU",
                            reason="证明 RLSOK 引擎与上游实现一致；不证明 RLSOK 能训练出同等策略")
        S["engine_consistency"] = cc
    else:
        S["engine_consistency"] = {"label": label(Status.NOT_RUN, reason="没有上游参考日志")}

    # 2. nominal run with video ---------------------------------------------------
    # console 3-D replay: scene now, each run as it finishes (nominal + the seed runs)
    RP.start(out / "replay", adapter.model, adapter.root_body, ["nominal"] + [f"seed-{sd}" for sd in range(1, (seeds or 10) + 1)],
             RP.label_for(key))
    nom = run(adapter, RolloutConfig(schedule=sched, seed=0, video_path=out / "nominal.mp4", replay_hz=RP.RATE_HZ))
    RP.add_run(out / "replay", "nominal", nom, adapter.root_body)
    (out / "trace_nominal.json").write_text(json.dumps(nom.trace), encoding="utf-8")
    _chart_tracking(nom.trace, out / "charts" / "tracking_nominal.png",
                    f"{key}: velocity tracking (nominal model, RLSOK MuJoCo)")
    walk = []
    for s, axis, c in _walking_segments(nom.segments):
        walk.append({"segment": s["name"], "axis": axis, "cmd": c,
                     "mean": s.get(f"{axis}_mean"), "abs_err": s.get(f"{axis}_abs_err"),
                     "rmse": s.get(f"{axis}_rmse")})
    S["nominal"] = {"summary": nom.summary(), "tracking": walk,
                    "label": label(Status.FAILED if nom.fell else Status.VERIFIED,
                                   "名义模型、平地、单次运行；跟踪误差是策略本身的水平",
                                   reason="摔倒" if nom.fell else "")}

    # 3. seeds with initial joint noise --------------------------------------------
    seed_rows = []
    for sd in range(1, seeds + 1):
        r = run(adapter, RolloutConfig(schedule=sched, seed=sd, init_noise=0.05, thermal=False, replay_hz=RP.RATE_HZ))
        RP.add_run(out / "replay", f"seed-{sd}", r, adapter.root_body)
        seed_rows.append({"seed": sd, "fell": r.fell, "fall_time": r.fall_time,
                          "tracking": {f"{s['name']}": {a: s.get(f"{a}_abs_err") for a in ("vx", "vy", "wz")}
                                       for s in r.segments}})
    RP.finish(out / "replay")
    survived = sum(not r["fell"] for r in seed_rows)
    S["seeds"] = {"n": seeds, "survived": survived, "rows": seed_rows,
                  "label": label(Status.VERIFIED if survived == seeds else Status.FAILED,
                                 f"{seeds} 个种子，初始关节扰动 ±0.05 rad，名义模型",
                                 reason=f"{survived}/{seeds} 不摔倒")}

    # 4. robustness under domain randomisation ---------------------------------------
    rob = {}
    for preset in crit["robustness"]["presets"][key]:
        spec = PRESETS[preset]
        rows = []
        for ep in range(dr_episodes):
            r = run(adapter, RolloutConfig(schedule=sched, seed=1000 + ep, dr=spec, thermal=False))
            rows.append({"episode": ep, "fell": r.fell, "fall_time": r.fall_time, "dr": r.dr_sample})
        ok = sum(not x["fell"] for x in rows)
        is_train = preset.endswith("_upstream")
        if is_train:
            lab = label(Status.VERIFIED if ok >= 18 else Status.FAILED,
                        f"训练分布随机化 {preset}，{dr_episodes} 回合",
                        reason=f"{ok}/{dr_episodes} 不摔倒（判定线 18/20）")
        else:
            lab = label(Status.UNVERIFIED, f"压力测试 {preset}（比训练分布更宽），只记录不判定",
                        reason=f"{ok}/{dr_episodes} 不摔倒")
        rob[preset] = {"spec": spec.to_json(), "table": spec.table(), "survived": ok,
                       "episodes": dr_episodes, "rows": rows, "label": lab}
    S["robustness"] = rob

    # 5. motor model / thermal ------------------------------------------------------
    if adapter.motor_id:
        bam_run = run(adapter, RolloutConfig(schedule=sched, seed=0, actuation="bam"))
        S["bam_actuation"] = {
            "summary": bam_run.summary(),
            "label": label(Status.UNVERIFIED if not bam_run.fell else Status.FAILED,
                           "执行器换成 BAM 辨识的 STS3215 模型（m6），策略训练时并没用这个模型",
                           "检验策略对“更真实的舵机模型”是否敏感；真机表现仍需实测",
                           reason="摔倒" if bam_run.fell else "未摔倒"),
        }
        longs = [Segment(long_walk_s, sched[1].vx, 0.0, 0.0, "long_walk")]
        lw = run(adapter, RolloutConfig(schedule=longs, seed=0, actuation="bam", record_every=25))
        (out / "thermal_long.json").write_text(json.dumps(
            {"t": lw.trace["t"], "temp": lw.trace.get("temp"), "joints": adapter.joint_names,
             "thermal": lw.thermal}), encoding="utf-8")
        if lw.thermal and lw.trace.get("temp"):
            _chart_temperature(lw.trace, adapter.joint_names, lw.thermal["params"]["t_max"],
                               out / "charts" / "temperature_long_walk.png",
                               f"{key}: {long_walk_s:.0f} s walk at vx={sched[1].vx} (BAM servo model)")
        S["thermal"] = {
            "long_walk_s": long_walk_s, "fell": lw.fell, "survived_s": lw.survived_s,
            "thermal": lw.thermal,
            "label": label(Status.ASSUMED, "电流/铜损来自 BAM 辨识的 kt、R（已验证来源）",
                           "热阻和热时间常数为假设值：温度绝对值不可信，只能比较相对发热",
                           reason="上传实测温度日志后可用 thermal.fit 校准"),
        }
    else:
        S["thermal"] = {"label": label(Status.NOT_AVAILABLE,
                                       reason="该机器人的电机不在电机库（无 kt/R 辨识数据），不做温度预测")}

    # 6. training reproduction (level 2) -------------------------------------------
    S["training_reproduction"] = {"label": label(
        Status.NOT_RUN,
        reason="需要按上游规模训练（GPU）。在完成并通过 level2 判定之前，人形设计不得标“已验证”")}

    # files & size budget ----------------------------------------------------------
    files = {}
    total = 0
    for f in sorted(p for p in out.rglob("*") if p.is_file() and p.name != "run.json"):
        files[f.relative_to(out).as_posix()] = {"sha256": _sha(f), "bytes": f.stat().st_size}
        total += f.stat().st_size
    result["files"] = files
    result["evidence_bytes"] = total
    result["evidence_within_limit"] = total < EVIDENCE_LIMIT
    result["wall_s"] = time.time() - t_start
    lvl1 = [S["engine_consistency"]["label"]["status"], S["nominal"]["label"]["status"],
            S["seeds"]["label"]["status"]]
    result["level1"] = label(
        Status.VERIFIED if all(x == "VERIFIED" for x in lvl1) else Status.FAILED,
        "引擎一致性 + 官方策略在 RLSOK 中名义运行 + 10 种子",
        reason="这一级证明引擎正确，不等于训练复现成功")
    result["level2"] = S["training_reproduction"]["label"]
    (out / "run.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return result
