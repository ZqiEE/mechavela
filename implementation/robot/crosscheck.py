"""Cross-check RLSOK's adapters against logs produced by the upstream code.

The upstream logs are produced by scripts/robot_upstream_*_reference.py, which
run the projects' own code (Open Duck MjInfer loop; mjlab ManagerBasedRlEnv on
MuJoCo Warp).  Here RLSOK recomputes the same quantities with its own engine.
"""

from __future__ import annotations

import json
from pathlib import Path

import mujoco
import numpy as np

from .adapters import G1Velocity, OpenDuckMiniV2, PolicyIO
from .rollout import RolloutConfig, Segment, run


def duck(ref_path: Path) -> dict:
    ref = json.loads(Path(ref_path).read_text())
    a = OpenDuckMiniV2()
    r = run(a, RolloutConfig(schedule=[Segment(*s) for s in ref["schedule"]],
                             record_qpos=True, thermal=False))
    mine_q = np.array(r.trace["qpos"])[:-1]
    ref_q = np.array(ref["qpos"])[1:]          # upstream logs at policy time = our previous sample
    dq = float(np.abs(mine_q - ref_q).max())
    da = float(np.abs(np.array(r.trace["action"]) - np.array(ref["action"])).max())
    return {
        "kind": "bit_exact_trajectory",
        "upstream": "Open_Duck_Playground mujoco_infer.MjInfer loop (headless)",
        "upstream_mujoco": ref.get("mujoco"), "steps": len(ref["t"]),
        "max_abs_qpos_diff": dq, "max_abs_action_diff": da,
        "pass": dq == 0.0 and da == 0.0,
        "criterion": "同一 MuJoCo 版本下逐步状态与动作完全一致（差值 = 0）",
    }


def g1(ref_path: Path, obs_tol: float = 1e-4, vel_tol: float = 0.05) -> dict:
    ref = json.loads(Path(ref_path).read_text())
    a = G1Velocity()
    m = a.model
    d = mujoco.MjData(m)
    io = PolicyIO()
    a.reset(d, io)
    obs_err = []
    act_err = []
    for k in range(len(ref["t"])):
        d.qpos[:] = ref["qpos"][k]
        d.qvel[:] = ref["qvel"][k]
        mujoco.mj_forward(m, d)
        io.step = ref["episode_length"][k]
        io.last_action = np.array(ref["action"][k - 1]) if k else np.zeros(a.n)
        o = a.observe(d, io, np.array(ref["cmd"][k]), None)
        obs_err.append(float(np.abs(o - np.array(ref["obs"][k])).max()))
        act_err.append(float(np.abs(a.act(o) - np.array(ref["action"][k])).max()))
    r = run(a, RolloutConfig(schedule=[Segment(*s) for s in ref["schedule"]], thermal=False))
    lv = np.array(ref["root_lin_vel_b"])
    av = np.array(ref["root_ang_vel_b"])
    t = np.array(ref["t"])
    rows = []
    start = 0.0
    worst = 0.0
    for s, seg in zip(ref["schedule"], r.segments):
        sel = (t > start + 1.0) & (t < start + s[0])
        start += s[0]
        up = {"vx": float(lv[sel, 0].mean()), "vy": float(lv[sel, 1].mean()), "wz": float(av[sel, 2].mean())}
        me = {"vx": seg["vx_mean"], "vy": seg["vy_mean"], "wz": seg["wz_mean"]}
        diff = {k: abs(up[k] - me[k]) for k in up}
        worst = max(worst, *diff.values())
        rows.append({"cmd": list(s[1:]), "mjlab_warp": up, "rlsok_mujoco_c": me, "abs_diff": diff})
    return {
        "kind": "observation_identity_and_trajectory_statistics",
        "upstream": "unitree_rl_mjlab unitree_g1_flat_env_cfg(play=True) on mjlab 1.2.0 / MuJoCo Warp (CPU)",
        "upstream_device": ref.get("device"), "steps": len(ref["t"]),
        "upstream_terminated": ref.get("terminated_at"),
        "max_obs_diff": max(obs_err), "max_action_diff_from_identical_state": max(act_err),
        "segments": rows, "max_segment_mean_velocity_diff": worst,
        "rlsok_fell": r.fell,
        "pass": max(obs_err) < obs_tol and worst < vel_tol and not r.fell and ref.get("terminated_at") is None,
        "criterion": f"在 mjlab 的同一状态上 RLSOK 观测误差 < {obs_tol}（float32 量级）；"
                     f"两个引擎各段平均速度差 < {vel_tol} m/s 或 rad/s；两边都不摔倒。"
                     "Warp(float32,GPU 内核) 与 MuJoCo C(float64) 不是逐位一致的，只比较统计量",
    }
