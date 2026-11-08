"""Level-2 "training reproduction" judgement (release/ROBOT_BENCHMARK_CRITERIA.json).

The trained policy and the official policy are evaluated in the SAME run with the
same RLSOK evaluator, schedule and seeds, so the official error is measured, not
copied from an older file.  Per walking segment the error on the commanded axis
is the mean over seeds of |segment mean velocity - command|.

Runs shorter than the upstream step count are labelled SMOKE and never pass.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np

from ..adapters import ADAPTERS
from ..bench import CRITERIA, load_criteria, schedule_for
from ..labels import Status, label
from ..randomization import PRESETS
from ..rollout import RolloutConfig, run

TRACK_MARGIN = 0.05
SEEDS = 10
SEED_MIN = 9
DR_EPISODES = 20
DR_MIN = 16


def _walking(segs):
    out = []
    for i, s in enumerate(segs):
        c = s["cmd"]
        if any(abs(x) > 1e-9 for x in c):
            ax = ("vx", "vy", "wz")[int(np.argmax(np.abs(c)))]
            out.append((i, s["name"], ax, c[("vx", "vy", "wz").index(ax)]))
    return out


def evaluate_policy(robot: str, policy: Path | None, seeds: int = SEEDS) -> dict:
    adapter = ADAPTERS[robot](policy)
    sched = schedule_for(robot)
    rows, errs = [], {}
    for sd in range(1, seeds + 1):
        r = run(adapter, RolloutConfig(schedule=sched, seed=sd, init_noise=0.05, thermal=False))
        rows.append({"seed": sd, "fell": r.fell, "fall_time": r.fall_time})
        if not r.fell:
            for i, name, ax, c in _walking(r.segments):
                errs.setdefault(f"{i}:{name}:{ax}", []).append(abs(r.segments[i][f"{ax}_mean"] - c))
    return {"policy": str(adapter.policy_path), "rows": rows, "survived": sum(not x["fell"] for x in rows),
            "tracking_err": {k: float(np.mean(v)) for k, v in errs.items()}}


def evaluate_dr(robot: str, policy: Path | None, preset: str, episodes: int = DR_EPISODES) -> dict:
    adapter = ADAPTERS[robot](policy)
    sched = schedule_for(robot)
    fell = [run(adapter, RolloutConfig(schedule=sched, seed=1000 + ep, dr=PRESETS[preset], thermal=False)).fell
            for ep in range(episodes)]
    return {"preset": preset, "episodes": episodes, "survived": sum(not f for f in fell)}


def judge(robot: str, trained_policy: Path, *, trained_steps: int, upstream_steps: int,
          out: Path | None = None, train_record: dict | None = None) -> dict:
    crit = load_criteria()
    smoke = trained_steps < upstream_steps
    train_preset = next(p for p in crit["robustness"]["presets"][robot] if p.endswith("_upstream"))
    off = evaluate_policy(robot, None)
    me = evaluate_policy(robot, trained_policy)
    dr = evaluate_dr(robot, trained_policy, train_preset)
    seg = {}
    for k, e_off in off["tracking_err"].items():
        e_me = me["tracking_err"].get(k)
        seg[k] = {"official_err": e_off, "trained_err": e_me, "limit": e_off + TRACK_MARGIN,
                  "pass": e_me is not None and e_me <= e_off + TRACK_MARGIN}
    checks = {
        "seeds": {"value": f"{me['survived']}/{SEEDS}", "pass": me["survived"] >= SEED_MIN},
        "tracking": {"segments": seg, "pass": bool(seg) and all(v["pass"] for v in seg.values())},
        "dr_train_preset": {"value": f"{dr['survived']}/{dr['episodes']}", "preset": train_preset,
                            "pass": dr["survived"] >= DR_MIN},
    }
    all_pass = all(c["pass"] for c in checks.values())
    if smoke:
        lab = label(Status.SMOKE, f"训练 {trained_steps:,} 步 < 上游 {upstream_steps:,} 步",
                    reason="冒烟训练不参与第二级判定" + ("（各项数值已记录）" if all_pass else ""))
    else:
        lab = label(Status.VERIFIED if all_pass else Status.FAILED,
                    f"{robot}：RLSOK 训练引擎从零训练 {trained_steps:,} 步，同一评测器与官方策略对比",
                    reason="三项判定全部通过" if all_pass else
                    "未通过：" + "、".join(k for k, c in checks.items() if not c["pass"]))
    sha = lambda p: hashlib.sha256(Path(p).read_bytes()).hexdigest()  # noqa: E731
    res = {"schema": "rlsok.robot-level2/1", "robot": robot,
           "criteria_file": {"path": "release/ROBOT_BENCHMARK_CRITERIA.json", "sha256": sha(CRITERIA)},
           "interpretation": "每个行走段误差 = 10 个种子中未摔倒者 |段平均速度 - 命令| 的平均；"
                             "官方策略在同一次运行中用同一评测器重新测量",
           "trained_policy": {"path": str(trained_policy), "sha256": sha(trained_policy)},
           "trained_steps": trained_steps, "upstream_steps": upstream_steps,
           "train_record": train_record, "official": off, "trained": me, "dr": dr,
           "checks": checks, "label": lab}
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")
    return res
