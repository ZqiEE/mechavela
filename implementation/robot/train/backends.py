"""Training backend registry.

Three backends, each a thin adapter around a mature trainer (none is reimplemented):

* ``playground``       - generic: any mujoco_playground locomotion env + brax PPO,
                         optional AI reward override (reward_api) and RMA teacher.
* ``playground_duck``  - INTERNAL ONLY: Open_Duck_Playground's own Joystick env and
                         PPO config (Open_Duck_Playground has no licence file).
* ``mjlab``            - unitree_rl_mjlab's own ``scripts/train.py`` (rsl_rl PPO on
                         MuJoCo Warp).  Needs CUDA for real training.

``availability()`` only checks what is installed; it never claims a backend works
before a run has produced evidence.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

# Upstream step counts used to decide SMOKE vs full runs.
UPSTREAM_STEPS = {
    "open_duck_mini_v2": 150_000_000,   # Open_Duck_Playground runner.py default
    "g1_velocity_flat": 30_000 * 24 * 4096,  # mjlab G1 velocity rl_cfg max_iterations x steps x envs
}

DUCK_UPSTREAM_TERMS = ("tracking_lin_vel", "tracking_ang_vel", "torques", "action_rate", "alive",
                       "imitation", "stand_still")
DUCK_UPSTREAM_SCALES = {"tracking_lin_vel": 2.5, "tracking_ang_vel": 6.0, "torques": -1.0e-3,
                        "action_rate": -0.5, "stand_still": -0.2, "alive": 20.0, "imitation": 1.0}


@dataclass
class TrainJob:
    backend: str
    robot: str
    out: str
    num_timesteps: int
    num_envs: int | None = None
    seed: int = 0
    num_evals: int | None = None
    reward_source: str | None = None
    rma: bool = False
    task: str | None = None              # backend-specific task / env name
    extra: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return asdict(self)


def _has(mod: str) -> bool:
    try:
        return importlib.util.find_spec(mod) is not None
    except (ImportError, ValueError):
        return False


def gpu_info() -> dict:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return {"available": False, "reason": "nvidia-smi not found"}
    try:
        out = subprocess.run([exe, "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"],
                             capture_output=True, text=True, timeout=20).stdout.strip()
    except (OSError, subprocess.SubprocessError) as e:
        return {"available": False, "reason": str(e)}
    return {"available": bool(out), "gpus": [x.strip() for x in out.splitlines()]}


def duck_repo() -> Path | None:
    cands = [os.environ.get("RLSOK_OPEN_DUCK_PLAYGROUND"),
             str(Path.home() / "rlsok" / "thirdparty" / "Open_Duck_Playground"),
             r"D:\RLSOK\thirdparty\_dl_open_duck_playground\Open_Duck_Playground-b9be205ac64488c23504ca42e5ec790337adeec3",
             "/mnt/d/RLSOK/thirdparty/_dl_open_duck_playground/"
             "Open_Duck_Playground-b9be205ac64488c23504ca42e5ec790337adeec3"]
    for c in cands:
        if c and (Path(c) / "playground" / "open_duck_mini_v2" / "joystick.py").exists():
            return Path(c)
    return None


def mjlab_repo() -> Path | None:
    for c in (os.environ.get("RLSOK_UNITREE_RL_MJLAB"), str(Path.home() / "rlsok" / "thirdparty" / "unitree_rl_mjlab")):
        if c and (Path(c) / "scripts" / "train.py").exists():
            return Path(c)
    return None


class Backend:
    name = ""
    title = ""
    internal_only = False
    robots: tuple[str, ...] = ()
    supports_reward_override = False
    supports_rma = False

    def availability(self) -> dict:
        raise NotImplementedError

    def describe(self) -> dict:
        return {"name": self.name, "title": self.title, "internal_only": self.internal_only,
                "robots": list(self.robots), "reward_override": self.supports_reward_override,
                "rma": self.supports_rma, "availability": self.availability()}

    def feature_spec(self, robot: str) -> dict:
        raise NotImplementedError

    def train(self, job: TrainJob) -> dict:
        raise NotImplementedError


def duck_feature_spec(nu: int = 14) -> dict:
    spec = {f"up_{k}": ((), "float32") for k in DUCK_UPSTREAM_TERMS}
    spec.update({
        "command": ((7,), "float32"), "local_linvel": ((3,), "float32"), "gyro": ((3,), "float32"),
        "gravity": ((3,), "float32"), "accelerometer": ((3,), "float32"),
        "actuator_force": ((nu,), "float32"), "action": ((nu,), "float32"), "last_action": ((nu,), "float32"),
        "joint_qpos": ((nu,), "float32"), "joint_qvel": ((nu,), "float32"), "default_pose": ((nu,), "float32"),
        "contact": ((2,), "float32"), "first_contact": ((2,), "float32"), "feet_air_time": ((2,), "float32"),
        "root_height": ((), "float32"), "done": ((), "float32"),
    })
    return spec


DUCK_FEATURE_NOTES = {
    "up_tracking_lin_vel": "upstream exp(-err / 0.01), err = (cmd_vx - vx)^2 + max(|vy - cmd_vy| - 0.1, 0)-based "
                           "term; unscaled (upstream weight 2.5)",
    "up_tracking_ang_vel": "upstream exp(-(cmd_wz - wz)^2 / 0.01), unscaled (upstream weight 6.0)",
    "up_torques": "upstream sum(actuator_force^2), unscaled (upstream weight -1e-3)",
    "up_action_rate": "upstream sum (a - a_prev)^2, unscaled (upstream weight -0.5)",
    "up_alive": "upstream constant 1 per step (upstream weight 20)",
    "up_imitation": "upstream reference-motion imitation reward, unscaled (upstream weight 1.0)",
    "up_stand_still": "upstream cost when command is ~0, unscaled (upstream weight -0.2)",
    "command": "[vx, vy, wz, neck_pitch, head_pitch, head_yaw, head_roll] command",
    "local_linvel": "torso linear velocity in the body frame (m/s)",
    "gyro": "body-frame angular velocity (rad/s)",
    "gravity": "gravity direction in body frame (upright = [0,0,-1])",
    "contact": "foot contact flags (1 = in contact) [left, right]",
    "first_contact": "1 on the step a foot touches down after being in the air",
    "feet_air_time": "seconds each foot has been in the air",
    "root_height": "torso height (m)",
    "done": "1 if the episode terminates this step (fall)",
}


class PlaygroundDuck(Backend):
    name = "playground_duck"
    title = "Open_Duck_Playground Joystick + brax PPO (upstream config)"
    internal_only = True
    robots = ("open_duck_mini_v2",)
    supports_reward_override = True
    supports_rma = True

    def availability(self) -> dict:
        missing = [m for m in ("jax", "brax", "mujoco_playground", "mujoco") if not _has(m)]
        repo = duck_repo()
        if repo is None:
            missing.append("Open_Duck_Playground source (set RLSOK_OPEN_DUCK_PLAYGROUND)")
        return {"available": not missing, "missing": missing, "repo": str(repo) if repo else None,
                "gpu": gpu_info(), "licence_note": "Open_Duck_Playground has no licence: internal benchmark only"}

    def feature_spec(self, robot: str) -> dict:
        return duck_feature_spec()

    def train(self, job: TrainJob) -> dict:
        from . import playground_train
        return playground_train.train(job)


class Playground(Backend):
    name = "playground"
    title = "mujoco_playground locomotion env + brax PPO (generic)"
    robots = ("any mujoco_playground locomotion env",)
    supports_reward_override = True
    supports_rma = False

    def availability(self) -> dict:
        missing = [m for m in ("jax", "brax", "mujoco_playground", "mujoco") if not _has(m)]
        return {"available": not missing, "missing": missing, "gpu": gpu_info()}

    def feature_spec(self, robot: str) -> dict:
        from . import playground_train
        return playground_train.generic_feature_spec(robot)

    def train(self, job: TrainJob) -> dict:
        from . import playground_train
        return playground_train.train(job)


class Mjlab(Backend):
    name = "mjlab"
    title = "unitree_rl_mjlab scripts/train.py (rsl_rl PPO on MuJoCo Warp)"
    robots = ("g1_velocity_flat",)
    supports_reward_override = False  # reward terms are mjlab config objects; weights only (extra.reward_weights)
    supports_rma = False

    def availability(self) -> dict:
        missing = [m for m in ("mjlab", "mujoco_warp", "torch") if not _has(m)]
        repo = mjlab_repo()
        if repo is None:
            missing.append("unitree_rl_mjlab source (set RLSOK_UNITREE_RL_MJLAB)")
        g = gpu_info()
        return {"available": not missing, "missing": missing, "repo": str(repo) if repo else None, "gpu": g,
                "note": "without CUDA only a tiny CPU smoke run is possible; real training needs a >=16 GB GPU"}

    def command(self, job: TrainJob) -> list[str]:
        import sys
        repo = mjlab_repo()
        task = job.task or "Unitree-G1-Flat"
        envs = job.num_envs or 4096
        iters = max(1, job.num_timesteps // (24 * envs))
        cmd = [sys.executable, str(repo / "scripts" / "train.py"), task, f"--env.scene.num-envs={envs}",
               f"--agent.max-iterations={iters}", f"--agent.seed={job.seed}"]
        for term, w in (job.extra.get("reward_weights") or {}).items():
            cmd.append(f"--env.rewards.{term}.weight={float(w)}")
        return cmd

    def train(self, job: TrainJob) -> dict:
        av = self.availability()
        if not av["available"]:
            return {"status": "NOT_AVAILABLE", "missing": av["missing"]}
        out = Path(job.out)
        out.mkdir(parents=True, exist_ok=True)
        cmd = self.command(job)
        with open(out / "mjlab_train.log", "w", encoding="utf-8") as log:
            p = subprocess.run(cmd, cwd=mjlab_repo(), stdout=log, stderr=subprocess.STDOUT)
        return {"status": "FINISHED" if p.returncode == 0 else "FAILED", "returncode": p.returncode,
                "command": cmd, "log": str(out / "mjlab_train.log")}


BACKENDS: dict[str, Backend] = {b.name: b for b in (Playground(), PlaygroundDuck(), Mjlab())}


def get(name: str) -> Backend:
    if name not in BACKENDS:
        raise KeyError(f"unknown backend {name!r}; available: {sorted(BACKENDS)}")
    return BACKENDS[name]
