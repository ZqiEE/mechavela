"""Domain randomisation spec, sampler and MuJoCo (CPU) application.

The spec is plain data so the same ranges go into training backends and into
the verification report.  Categories required by the platform owner:
friction, mass, motor torque, latency, ground.
"""

from __future__ import annotations

import copy
import math
from dataclasses import asdict, dataclass, field

import mujoco
import numpy as np


@dataclass
class Range:
    lo: float
    hi: float
    op: str = "scale"   # scale | add | abs

    def sample(self, rng: np.random.Generator, size=None):
        return rng.uniform(self.lo, self.hi, size=size)

    @property
    def identity(self) -> bool:
        return (self.op == "scale" and self.lo == self.hi == 1.0) or (
            self.op == "add" and self.lo == self.hi == 0.0)


@dataclass
class DRSpec:
    name: str = "none"
    floor_friction: Range | None = None          # abs, sliding friction of the ground
    foot_friction: Range | None = None           # abs, robot foot geoms (G1 upstream style)
    link_mass: Range | None = None               # scale, every body independently
    torso_mass: Range | None = None              # add kg to the root body
    torso_com: Range | None = None               # add m to the root body COM (each axis)
    motor_strength: Range | None = None          # scale actuator kp + force limit (motor torque)
    joint_frictionloss: Range | None = None      # scale
    joint_armature: Range | None = None          # scale
    latency_s: Range | None = None               # abs seconds of action delay
    ground_slope_deg: Range | None = None        # abs, plane tilt via gravity rotation
    encoder_bias: Range | None = None            # add rad to observed joint angles
    push_interval_s: Range | None = None         # abs, time between pushes
    push_velocity: float = 0.0                   # m/s magnitude of base velocity kick
    source: str = ""
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return asdict(self)

    def table(self) -> list[dict]:
        zh = {
            "floor_friction": ("地面摩擦系数", "绝对值"),
            "foot_friction": ("脚底摩擦系数", "绝对值"),
            "link_mass": ("各连杆质量", "倍数"),
            "torso_mass": ("躯干附加质量 (kg)", "加"),
            "torso_com": ("躯干质心偏移 (m, 每轴)", "加"),
            "motor_strength": ("电机力矩/刚度", "倍数"),
            "joint_frictionloss": ("关节静摩擦", "倍数"),
            "joint_armature": ("转子惯量 armature", "倍数"),
            "latency_s": ("控制延迟 (s)", "绝对值"),
            "ground_slope_deg": ("地面坡度 (°)", "绝对值"),
            "encoder_bias": ("编码器零偏 (rad)", "加"),
            "push_interval_s": ("外力推搡间隔 (s)", "绝对值"),
        }
        rows = []
        for k, (title, op) in zh.items():
            r = getattr(self, k)
            if r is not None:
                rows.append({"key": k, "名称": title, "方式": op, "下限": r.lo, "上限": r.hi})
        if self.push_velocity:
            rows.append({"key": "push_velocity", "名称": "推搡速度 (m/s)", "方式": "绝对值",
                         "下限": 0.0, "上限": self.push_velocity})
        return rows


PRESETS: dict[str, DRSpec] = {
    "none": DRSpec(name="none", source="无随机化（名义模型）"),
    # Copied from Open_Duck_Playground playground/common/randomize.py (commit in MANIFEST).
    "open_duck_upstream": DRSpec(
        name="open_duck_upstream",
        floor_friction=Range(0.5, 1.0, "abs"),
        joint_frictionloss=Range(0.9, 1.1),
        joint_armature=Range(1.0, 1.05),
        torso_com=Range(-0.05, 0.05, "add"),
        link_mass=Range(0.9, 1.1),
        torso_mass=Range(-0.1, 0.1, "add"),
        motor_strength=Range(0.9, 1.1),
        source="Open_Duck_Playground/playground/common/randomize.py（训练时所用范围）",
        notes=["上游还对 qpos0 加 ±0.03 rad 抖动；这里以编码器零偏实现同等效果的观测误差"],
    ),
    # Copied from unitree_rl_mjlab src/tasks/velocity/velocity_env_cfg.py events.
    "g1_upstream": DRSpec(
        name="g1_upstream",
        foot_friction=Range(0.3, 1.6, "abs"),
        torso_com=Range(-0.05, 0.05, "add"),
        encoder_bias=Range(-0.015, 0.015, "add"),
        push_interval_s=Range(5.0, 6.0, "abs"),
        push_velocity=0.5,
        source="unitree_rl_mjlab velocity_env_cfg.py events（训练时所用范围）",
    ),
    # RLSOK robustness check: wider than training, covers all five required categories.
    "rlsok_stress": DRSpec(
        name="rlsok_stress",
        floor_friction=Range(0.4, 1.2, "abs"),
        link_mass=Range(0.85, 1.15),
        torso_mass=Range(-0.1, 0.2, "add"),
        motor_strength=Range(0.8, 1.1),
        joint_frictionloss=Range(0.8, 1.3),
        latency_s=Range(0.0, 0.02, "abs"),
        ground_slope_deg=Range(0.0, 3.0, "abs"),
        source="RLSOK 压力测试：比训练范围更宽，用于检验策略在训练分布外的稳健性",
    ),
}


def for_robot(spec: DRSpec, root_mass_kg: float) -> DRSpec:
    """The stress preset's torso payload (-0.1…+0.2) is relative: -10 %…+20 % of the
    root body mass, so a 0.6 kg duck and a 30 kg humanoid get the same relative load."""
    s = copy.deepcopy(spec)
    if s.name == "rlsok_stress" and s.torso_mass is not None:
        s.torso_mass = Range(-0.1 * root_mass_kg, 0.2 * root_mass_kg, "add")
        s.notes = s.notes + [f"躯干附加质量 = 根刚体质量 {root_mass_kg:.3f} kg 的 −10%…+20%"]
    return s


@dataclass
class Sample:
    values: dict
    latency_s: float = 0.0
    encoder_bias: np.ndarray | None = None
    gravity: np.ndarray | None = None

    def to_json(self) -> dict:
        d = dict(self.values)
        d["latency_s"] = self.latency_s
        if self.encoder_bias is not None:
            d["encoder_bias"] = self.encoder_bias.tolist()
        if self.gravity is not None:
            d["gravity"] = self.gravity.tolist()
        return d


class Randomizer:
    """Applies a DRSpec to a MuJoCo model, always starting from the nominal arrays."""

    FIELDS = ("geom_friction", "body_mass", "body_ipos", "actuator_gainprm", "actuator_biasprm",
              "actuator_forcerange", "dof_frictionloss", "dof_armature")

    def __init__(self, model: mujoco.MjModel, *, root_body: int, floor_geoms: list[int],
                 foot_geoms: list[int], actuated_dofs: list[int]):
        self.model = model
        self.root_body = root_body
        self.floor_geoms = floor_geoms
        self.foot_geoms = foot_geoms
        self.actuated_dofs = np.asarray(actuated_dofs)
        self.nominal = {f: getattr(model, f).copy() for f in self.FIELDS}
        self.nominal_gravity = model.opt.gravity.copy()

    def restore(self):
        for f, v in self.nominal.items():
            getattr(self.model, f)[:] = v
        self.model.opt.gravity[:] = self.nominal_gravity

    def apply(self, spec: DRSpec, rng: np.random.Generator) -> Sample:
        m = self.model
        self.restore()
        v: dict = {}
        if spec.floor_friction is not None:
            mu = float(spec.floor_friction.sample(rng))
            m.geom_friction[self.floor_geoms, 0] = mu
            v["floor_friction"] = mu
        if spec.foot_friction is not None:
            mu = float(spec.foot_friction.sample(rng))
            m.geom_friction[self.foot_geoms, 0] = mu
            v["foot_friction"] = mu
        if spec.link_mass is not None:
            s = spec.link_mass.sample(rng, size=m.nbody)
            s[0] = 1.0
            m.body_mass[:] = self.nominal["body_mass"] * s
            v["link_mass_scale_mean"] = float(s[1:].mean())
        if spec.torso_mass is not None:
            dm = float(spec.torso_mass.sample(rng))
            m.body_mass[self.root_body] = max(1e-3, m.body_mass[self.root_body] + dm)
            v["torso_mass_add"] = dm
        if spec.torso_com is not None:
            d = spec.torso_com.sample(rng, size=3)
            m.body_ipos[self.root_body] = self.nominal["body_ipos"][self.root_body] + d
            v["torso_com_offset"] = d.tolist()
        if spec.motor_strength is not None:
            s = spec.motor_strength.sample(rng, size=m.nu)
            m.actuator_gainprm[:, 0] = self.nominal["actuator_gainprm"][:, 0] * s
            m.actuator_biasprm[:, 1] = self.nominal["actuator_biasprm"][:, 1] * s
            m.actuator_forcerange[:] = self.nominal["actuator_forcerange"] * s[:, None]
            v["motor_strength_mean"] = float(s.mean())
            v["motor_strength_min"] = float(s.min())
        if spec.joint_frictionloss is not None:
            s = spec.joint_frictionloss.sample(rng, size=len(self.actuated_dofs))
            m.dof_frictionloss[self.actuated_dofs] = self.nominal["dof_frictionloss"][self.actuated_dofs] * s
            v["frictionloss_scale_mean"] = float(s.mean())
        if spec.joint_armature is not None:
            s = spec.joint_armature.sample(rng, size=len(self.actuated_dofs))
            m.dof_armature[self.actuated_dofs] = self.nominal["dof_armature"][self.actuated_dofs] * s
            v["armature_scale_mean"] = float(s.mean())
        sample = Sample(values=v)
        if spec.latency_s is not None:
            sample.latency_s = float(spec.latency_s.sample(rng))
        if spec.encoder_bias is not None:
            sample.encoder_bias = spec.encoder_bias.sample(rng, size=len(self.actuated_dofs))
        if spec.ground_slope_deg is not None:
            slope = math.radians(float(spec.ground_slope_deg.sample(rng)))
            heading = float(rng.uniform(-math.pi, math.pi))
            g = float(np.linalg.norm(self.nominal_gravity))
            # A tilted plane under gravity == level plane under tilted gravity.
            gvec = np.array([g * math.sin(slope) * math.cos(heading),
                             g * math.sin(slope) * math.sin(heading),
                             -g * math.cos(slope)])
            m.opt.gravity[:] = gvec
            sample.gravity = gvec
            v["ground_slope_deg"] = math.degrees(slope)
            v["slope_heading_rad"] = heading
        return sample
