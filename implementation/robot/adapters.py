"""Robot adapters: how a published policy talks to a MuJoCo model.

Each adapter reproduces the observation / action convention of the upstream
deployment code it names (file + function), so that the policy sees what it
was trained on.  Adapters never contain results; they only map state <-> I/O.
"""

from __future__ import annotations

import json
import math
import os
import pickle
from dataclasses import dataclass, field
from pathlib import Path

import mujoco
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "robot_assets"
# Assets a user fetched themselves from upstream (never shipped with the package), e.g. Open Duck.
USER_ASSETS = Path(os.environ["RLSOK_USER_ASSETS"]) if os.environ.get("RLSOK_USER_ASSETS") else None
DUCK_COMMITS = {"Open_Duck_Playground": "b9be205ac64488c23504ca42e5ec790337adeec3",
                "Open_Duck_Mini": "b23317a485b3cec7d8417f352478778b3475173c"}


def duck_dir() -> Path:
    if USER_ASSETS and (USER_ASSETS / "INTERNAL_open_duck_mini_v2" / "xmls").exists():
        return USER_ASSETS / "INTERNAL_open_duck_mini_v2"
    return ASSETS / "INTERNAL_open_duck_mini_v2"

_INTEGRATOR = {"euler": mujoco.mjtIntegrator.mjINT_EULER,
               "implicitfast": mujoco.mjtIntegrator.mjINT_IMPLICITFAST,
               "implicit": mujoco.mjtIntegrator.mjINT_IMPLICIT,
               "rk4": mujoco.mjtIntegrator.mjINT_RK4}
_CONE = {"pyramidal": mujoco.mjtCone.mjCONE_PYRAMIDAL, "elliptic": mujoco.mjtCone.mjCONE_ELLIPTIC}
_SOLVER = {"newton": mujoco.mjtSolver.mjSOL_NEWTON, "cg": mujoco.mjtSolver.mjSOL_CG,
           "pgs": mujoco.mjtSolver.mjSOL_PGS}


def quat_rotate_inv(q: np.ndarray, v: np.ndarray) -> np.ndarray:
    """Rotate world vector v into the frame of unit quaternion q (w, x, y, z)."""
    w, x, y, z = q
    r = np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
        [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
        [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)],
    ])
    return r.T @ v


def yaw_of(q: np.ndarray) -> float:
    w, x, y, z = q
    return math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))


@dataclass
class PolicyIO:
    """Mutable per-episode controller state (actions history etc.)."""
    step: int = 0
    last_action: np.ndarray | None = None
    hist: list = field(default_factory=list)
    extra: dict = field(default_factory=dict)


class RobotAdapter:
    key: str = ""
    title: str = ""
    internal_only: bool = False
    ctrl_dt: float = 0.02
    sim_dt: float = 0.002
    motor_id: str | None = None          # motor library id for thermal/electrical model
    fall_height: float = 0.0
    fall_tilt_rad: float = 1.0
    camera_distance: float = 1.0
    command_limits: dict = {}
    upstream: dict = {}

    def __init__(self, policy_path: Path | None = None):
        self.policy_path = Path(policy_path) if policy_path else self.default_policy()
        self.model = self.build_model()
        m = self.model
        self.joint_names = self.policy_joint_names()
        self.jnt_ids = [mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_JOINT, n) for n in self.joint_names]
        if min(self.jnt_ids) < 0:
            raise ValueError(f"{self.key}: joint missing in model")
        self.qadr = np.array([m.jnt_qposadr[j] for j in self.jnt_ids])
        self.dadr = np.array([m.jnt_dofadr[j] for j in self.jnt_ids])
        act_by_joint = {int(m.actuator_trnid[a, 0]): a for a in range(m.nu)}
        self.act_ids = np.array([act_by_joint[j] for j in self.jnt_ids])
        free = [j for j in range(m.njnt) if m.jnt_type[j] == mujoco.mjtJoint.mjJNT_FREE]
        self.free_jnt = free[0]
        self.root_body = int(m.jnt_bodyid[self.free_jnt])
        self.fq = m.jnt_qposadr[self.free_jnt]
        self.fv = m.jnt_dofadr[self.free_jnt]
        self.n = len(self.joint_names)
        self._session = None

    # ---------------------------------------------------------------- hooks
    def default_policy(self) -> Path: raise NotImplementedError
    def build_model(self) -> mujoco.MjModel: raise NotImplementedError
    def policy_joint_names(self) -> list[str]: raise NotImplementedError
    def reset(self, d: mujoco.MjData, io: PolicyIO): raise NotImplementedError
    def observe(self, d, io: PolicyIO, cmd: np.ndarray, enc_bias) -> np.ndarray: raise NotImplementedError
    def targets(self, action: np.ndarray, io: PolicyIO) -> np.ndarray: raise NotImplementedError
    def pre_policy(self, io: PolicyIO): pass

    # ---------------------------------------------------------------- shared
    def session(self):
        if self._session is None:
            import onnxruntime as ort

            so = ort.SessionOptions()
            so.intra_op_num_threads = 1
            so.inter_op_num_threads = 1
            self._session = ort.InferenceSession(str(self.policy_path), so,
                                                 providers=["CPUExecutionProvider"])
            self._in = self._session.get_inputs()[0].name
        return self._session

    def act(self, obs: np.ndarray) -> np.ndarray:
        s = self.session()
        out = s.run(None, {self._in: obs.astype(np.float32)[None, :]})[0][0]
        return np.asarray(out, dtype=np.float64)

    def joint_q(self, d):
        return d.qpos[self.qadr]

    def joint_dq(self, d):
        return d.qvel[self.dadr]

    def base_quat(self, d):
        return d.qpos[self.fq + 3:self.fq + 7]

    def base_pos(self, d):
        return d.qpos[self.fq:self.fq + 3]

    def gravity_dir(self) -> np.ndarray:
        g = self.model.opt.gravity
        return g / np.linalg.norm(g)

    def tilt(self, d) -> float:
        pg = quat_rotate_inv(self.base_quat(d), self.gravity_dir())
        return math.acos(max(-1.0, min(1.0, -pg[2])))

    def fallen(self, d) -> bool:
        return self.base_pos(d)[2] < self.fall_height or self.tilt(d) > self.fall_tilt_rad

    def heading_velocity(self, d) -> tuple[float, float, float]:
        """(vx, vy) in the yaw-aligned frame, wz about world z."""
        v = d.qvel[self.fv:self.fv + 3]          # world-frame linear velocity (free joint)
        w_b = d.qvel[self.fv + 3:self.fv + 6]    # body-frame angular velocity
        q = self.base_quat(d)
        yaw = yaw_of(q)
        c, s = math.cos(yaw), math.sin(yaw)
        w, x, y, z = q
        rz = np.array([2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)])
        return c * v[0] + s * v[1], -s * v[0] + c * v[1], float(rz @ w_b)

    def apply_targets(self, d, tgt: np.ndarray):
        d.ctrl[self.act_ids] = tgt

    def describe(self) -> dict:
        m = self.model
        return {
            "key": self.key, "title": self.title, "internal_only": self.internal_only,
            "ctrl_dt": self.ctrl_dt, "sim_dt": float(m.opt.timestep),
            "joints": self.joint_names, "nu": int(m.nu), "nq": int(m.nq),
            "total_mass_kg": float(m.body_subtreemass[0]),
            "root_mass_kg": float(m.body_mass[self.root_body]),
            "policy": self.policy_path.relative_to(ROOT).as_posix()
            if self.policy_path.is_relative_to(ROOT) else str(self.policy_path),
            "motor_id": self.motor_id, "upstream": self.upstream,
            "command_limits": self.command_limits,
            "solver": {"integrator": int(m.opt.integrator), "cone": int(m.opt.cone),
                       "iterations": int(m.opt.iterations), "ls_iterations": int(m.opt.ls_iterations)},
        }


# ============================================================== Unitree G1
class G1Velocity(RobotAdapter):
    """unitree_rl_mjlab G1 29-DoF flat velocity task, deployed policy v0.

    Observation (98) = base_ang_vel(imu gyro) 3, projected_gravity 3, command 3,
    gait phase 2, joint_pos - default 29, joint_vel 29, last_action 29
    - src/tasks/velocity/velocity_env_cfg.py actor terms, mdp/observations.py:phase,
    deploy/robots/g1/config/policy/velocity/v0/params/deploy.yaml (scales = 1).
    Action: target = default + scale * a, per deploy.yaml; PD = the exported
    mjlab actuators (kp/kv/effort limits from g1_constants.py).
    """

    key = "g1_velocity_flat"
    title = "Unitree G1 29-DoF velocity (unitree_rl_mjlab)"
    ctrl_dt = 0.02
    motor_id = None
    fall_height = 0.35
    fall_tilt_rad = 1.0
    camera_distance = 3.0
    command_limits = {"vx": [-0.5, 1.0], "vy": [-0.5, 0.5], "wz": [-1.0, 1.0]}
    phase_period = 0.6

    def __init__(self, policy_path=None):
        self.dir = ASSETS / "g1_velocity_flat"
        import yaml

        self.deploy = yaml.safe_load((self.dir / "deploy.yaml").read_text(encoding="utf-8"))
        self.sim_cfg = json.loads((self.dir / "model" / "sim_cfg.json").read_text(encoding="utf-8"))
        self.upstream = {"repo": "unitree_rl_mjlab", "commit": self.sim_cfg.get("upstream_commit"),
                         "mjlab": self.sim_cfg.get("mjlab_version"), "license": "Apache-2.0"}
        super().__init__(policy_path)
        self.default = np.array(self.deploy["default_joint_pos"], float)
        self.scale = np.array(self.deploy["actions"]["JointPositionAction"]["scale"], float)
        self.offset = np.array(self.deploy["actions"]["JointPositionAction"]["offset"], float)
        self.gyro_adr = m_sensor_adr(self.model, "robot/imu_ang_vel")
        self.foot_geoms = [g for g in range(self.model.ngeom)
                           if "foot" in (self.model.geom(g).name or "") and "collision" in self.model.geom(g).name]
        self.floor_geoms = [mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_GEOM, "terrain")]

    def default_policy(self):
        return ASSETS / "g1_velocity_flat" / "policy.onnx"

    def build_model(self):
        m = mujoco.MjModel.from_xml_path(str(self.dir / "model" / "scene.xml"))
        c = self.sim_cfg["mujoco_cfg"]
        m.opt.timestep = c["timestep"]
        m.opt.integrator = _INTEGRATOR[c["integrator"]]
        m.opt.cone = _CONE[c["cone"]]
        m.opt.solver = _SOLVER[c["solver"]]
        m.opt.impratio = c["impratio"]
        m.opt.iterations = c["iterations"]
        m.opt.tolerance = c["tolerance"]
        m.opt.ls_iterations = c["ls_iterations"]
        m.opt.ls_tolerance = c["ls_tolerance"]
        m.opt.ccd_iterations = c["ccd_iterations"]
        m.opt.gravity[:] = c["gravity"]
        self.sim_dt = c["timestep"]
        if abs(self.sim_cfg["decimation"] * c["timestep"] - self.ctrl_dt) > 1e-12:
            raise ValueError("decimation * timestep != deploy step_dt")
        return m

    def policy_joint_names(self):
        # deploy.yaml joint_ids_map is identity over the robot's joint order.
        js = [n for n in self.sim_cfg["joints"] if not n.endswith("floating_base_joint")]
        ids = self.deploy["joint_ids_map"]
        return [js[i] for i in ids]

    def reset(self, d, io):
        key = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_KEY, "init_state")
        mujoco.mj_resetDataKeyframe(self.model, d, key)
        d.qpos[self.qadr] = self.default
        d.ctrl[self.act_ids] = self.default
        mujoco.mj_forward(self.model, d)
        io.step = 0
        io.last_action = np.zeros(self.n)

    def observe(self, d, io, cmd, enc_bias):
        gyro = d.sensordata[self.gyro_adr:self.gyro_adr + 3]
        pg = quat_rotate_inv(self.base_quat(d), self.gravity_dir())
        c3 = np.asarray(cmd[:3], float)
        gp = (io.step * self.ctrl_dt) % self.phase_period / self.phase_period
        phase = np.array([math.sin(2 * math.pi * gp), math.cos(2 * math.pi * gp)])
        if np.linalg.norm(c3) < 0.1:
            phase[:] = 0.0
        q = self.joint_q(d) + (0 if enc_bias is None else enc_bias)
        return np.concatenate([gyro, pg, c3, phase, q - self.default, self.joint_dq(d), io.last_action])

    def targets(self, action, io):
        io.last_action = action.copy()
        return self.offset + self.scale * action


def m_sensor_adr(m, name):
    sid = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_SENSOR, name)
    if sid < 0:
        raise ValueError(f"sensor {name} missing")
    return int(m.sensor_adr[sid])


# ============================================================== Open Duck Mini v2
class OpenDuckMiniV2(RobotAdapter):
    """INTERNAL BENCHMARK ONLY (Disney-character replica; Playground repo unlicensed).

    Reproduces Open_Duck_Playground playground/open_duck_mini_v2/mujoco_infer.py
    (MjInfer.get_obs / run) with BEST_WALK_ONNX from Open_Duck_Mini:
    obs(101) = gyro 3, accelerometer 3 (x + 1.3), command 7, q - default 14,
    dq * 0.05 14, last 3 actions 42, motor_targets 14, feet contacts 2,
    imitation phase [cos, sin] 2.  target = default + 0.25 a, rate-limited to
    5.24 rad/s; sim_dt 0.002, decimation 10.
    """

    key = "open_duck_mini_v2"
    title = "Open Duck Mini v2 (INTERNAL benchmark only)"
    internal_only = True
    ctrl_dt = 0.02
    sim_dt = 0.002
    decimation = 10
    warmup_substeps = 10
    motor_id = "sts3215"
    fall_height = 0.07
    fall_tilt_rad = 1.0
    camera_distance = 0.9
    command_limits = {"vx": [-0.15, 0.15], "vy": [-0.2, 0.2], "wz": [-1.0, 1.0]}
    action_scale = 0.25
    dof_vel_scale = 0.05
    max_motor_velocity = 5.24

    def __init__(self, policy_path=None):
        self.dir = duck_dir()
        man = json.loads((ASSETS / "MANIFEST.json").read_text(encoding="utf-8"))
        self.upstream = {
            "repo": "Open_Duck_Playground + Open_Duck_Mini",
            "commit": {k: man["sources"].get(k, {}).get("commit", DUCK_COMMITS[k])
                       for k in ("Open_Duck_Playground", "Open_Duck_Mini")},
            "license": "Playground: none; Mini: Apache-2.0; design: Disney replica -> internal only",
        }
        super().__init__(policy_path)
        m = self.model
        self.default = m.key("home").ctrl.copy()
        self.gyro_adr = m_sensor_adr(m, "gyro")
        self.acc_adr = m_sensor_adr(m, "accelerometer")
        self.feet = [m.body("foot_assembly").id, m.body("foot_assembly_2").id]
        self.floor_body = m.body("floor").id
        self.floor_geoms = [mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_GEOM, "floor")]
        self.foot_geoms = [g for g in range(m.ngeom) if m.geom_bodyid[g] in self.feet]
        coeffs = pickle.loads((self.dir / "polynomial_coefficients.pkl").read_bytes())
        nb = None
        for v in coeffs.values():   # PolyReferenceMotion keeps the last value it reads
            nb = int(v["period"] * v["fps"])
        self.nb_steps_in_period = nb

    def default_policy(self):
        return duck_dir() / "BEST_WALK_ONNX.onnx"

    def build_model(self):
        m = mujoco.MjModel.from_xml_path(str(self.dir / "xmls" / "scene_flat_terrain.xml"))
        m.opt.timestep = self.sim_dt
        return m

    def policy_joint_names(self):
        m = self.model
        return [m.joint(m.actuator_trnid[a, 0]).name for a in range(m.nu)]

    def reset(self, d, io):
        # Same order as MJInferBase.__init__: one mj_step from qpos0 with zero
        # ctrl, then the home pose and default ctrl are written (qvel/time kept).
        m = self.model
        mujoco.mj_resetData(m, d)
        mujoco.mj_step(m, d)
        d.qpos[:] = m.key("home").qpos
        d.ctrl[:] = self.default
        io.step = 0
        io.extra = {"imitation_i": 0.0, "phase": np.zeros(2),
                    "motor_targets": self.default.copy(), "prev_targets": self.default.copy()}
        io.hist = [np.zeros(self.n)] * 3

    def pre_policy(self, io):
        e = io.extra
        e["imitation_i"] = (e["imitation_i"] + 1.0) % self.nb_steps_in_period
        a = e["imitation_i"] / self.nb_steps_in_period * 2 * np.pi
        e["phase"] = np.array([np.cos(a), np.sin(a)])

    def contacts(self, d):
        m = self.model
        out = []
        for foot in self.feet:
            hit = False
            for i in range(d.ncon):
                c = d.contact[i]
                b1, b2 = m.geom_bodyid[c.geom1], m.geom_bodyid[c.geom2]
                if (b1 == foot and b2 == self.floor_body) or (b2 == foot and b1 == self.floor_body):
                    hit = True
                    break
            out.append(1.0 if hit else 0.0)
        return np.array(out)

    def observe(self, d, io, cmd, enc_bias):
        gyro = d.sensordata[self.gyro_adr:self.gyro_adr + 3].copy()
        acc = d.sensordata[self.acc_adr:self.acc_adr + 3].copy()
        acc[0] += 1.3
        q = self.joint_q(d) + (0 if enc_bias is None else enc_bias)
        c7 = np.zeros(7)
        c7[:len(cmd)] = cmd[:7]
        e = io.extra
        return np.concatenate([gyro, acc, c7, q - self.default, self.joint_dq(d) * self.dof_vel_scale,
                               io.hist[0], io.hist[1], io.hist[2], e["motor_targets"],
                               self.contacts(d), e["phase"]])

    def targets(self, action, io):
        io.hist = [action.copy(), io.hist[0], io.hist[1]]
        e = io.extra
        t = self.default + action * self.action_scale
        lim = self.max_motor_velocity * (self.sim_dt * self.decimation)
        t = np.clip(t, e["prev_targets"] - lim, e["prev_targets"] + lim)
        e["prev_targets"] = t.copy()
        e["motor_targets"] = t.copy()
        return t


ADAPTERS = {G1Velocity.key: G1Velocity, OpenDuckMiniV2.key: OpenDuckMiniV2}
