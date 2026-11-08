"""brax PPO training on mujoco_playground / Open_Duck_Playground environments (needs jax).

Only adapter code lives here: the environments, domain randomisation and PPO are
the upstream ones.  What RLSOK adds:
  * an AI reward (reward_api) replacing the env's reward terms, each logged;
  * an optional RMA teacher observation (policy sees the randomised physics);
  * reduced ``num_envs`` for small GPUs (batch_size and num_minibatches are kept,
    so each PPO update still uses the upstream sample count);
  * live progress (progress.jsonl, status.json), ONNX export and a train record.
"""

from __future__ import annotations

import functools
import inspect
import json
import os
import platform
import sys
import time
from pathlib import Path

import numpy as np

from . import export as X
from .backends import UPSTREAM_STEPS, TrainJob, duck_feature_spec, duck_repo, gpu_info
from .reward_api import compile_reward

DUCK_PPO_CONFIG = "BerkeleyHumanoidJoystickFlatTerrain"   # what Open_Duck_Playground's runner uses


# ----------------------------------------------------------------------------- reward injection
def _set_scales(env, names):
    from ml_collections import config_dict

    rc = env._config.reward_config
    with rc.unlocked() if hasattr(rc, "unlocked") else _null():
        rc.scales = config_dict.create(**{k: 1.0 for k in names})


class _null:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def install_duck_reward(env, rfn):
    import jax.numpy as jp

    orig = env._get_reward

    def _get_reward(data, action, info, metrics, done, first_contact, contact):
        up = orig(data, action, info, metrics, done, first_contact, contact)
        f = {f"up_{k}": v for k, v in up.items()}
        f.update(
            command=info["command"], local_linvel=env.get_local_linvel(data), gyro=env.get_gyro(data),
            gravity=env.get_gravity(data), accelerometer=env.get_accelerometer(data),
            actuator_force=data.actuator_force, action=action, last_action=info["last_act"],
            joint_qpos=env.get_actuator_joints_qpos(data.qpos), joint_qvel=env.get_actuator_joints_qvel(data.qvel),
            default_pose=env._default_actuator, contact=contact.astype(jp.float32),
            first_contact=jp.asarray(first_contact).astype(jp.float32), feet_air_time=info["feet_air_time"],
            root_height=data.qpos[env._floating_base_qpos_addr + 2], done=jp.asarray(done).astype(jp.float32))
        out = rfn(f, jp)
        return {k: jp.nan_to_num(jp.asarray(v, jp.float32)) for k, v in out.items()}

    env._get_reward = _get_reward
    _set_scales(env, rfn.components)


def install_generic_reward(env, rfn):
    import jax.numpy as jp

    orig = env._get_reward
    sig = inspect.signature(orig)
    if not {"data", "action"} <= set(sig.parameters):
        raise NotImplementedError(f"{type(env).__name__}._get_reward has no (data, action) arguments")

    def _get_reward(*args, **kw):
        a = sig.bind(*args, **kw).arguments
        up = orig(*args, **kw)
        data, info = a["data"], a.get("info") or {}
        f = {f"up_{k}": v for k, v in up.items()}
        f.update(qpos=data.qpos, qvel=data.qvel, actuator_force=data.actuator_force, action=a["action"])
        if "command" in info:
            f["command"] = info["command"]
        out = rfn(f, jp)
        return {k: jp.nan_to_num(jp.asarray(v, jp.float32)) for k, v in out.items()}

    env._get_reward = _get_reward
    _set_scales(env, rfn.components)


# ----------------------------------------------------------------------------- RMA teacher observation
def rma_param_fn(env):
    """Return (fn(mjx_model) -> e, names).  e = randomised physics relative to the nominal model,
    covering every field Open_Duck_Playground's domain_randomize changes."""
    import jax.numpy as jp

    m = env._mj_model
    has_fl = np.asarray(env.mjx_model.dof_hasfrictionloss)
    dof_id = np.where(has_fl)[0]
    jnt_id = m.dof_jntid[dof_id]
    dof_addr = np.array([a for a in m.jnt_dofadr if a in set(dof_id.tolist())])
    joint_addr = m.jnt_qposadr[jnt_id]
    ipos0 = m.body_ipos[1].copy()
    mass0 = m.body_mass.copy()
    kp0 = m.actuator_gainprm[:, 0].copy()
    fl0 = np.where(m.dof_frictionloss[dof_addr] > 0, m.dof_frictionloss[dof_addr], 1.0)
    arm0 = np.where(m.dof_armature[dof_addr] > 0, m.dof_armature[dof_addr], 1.0)
    q00 = m.qpos0[joint_addr].copy()
    names = (["floor_friction", "torso_com_dx", "torso_com_dy", "torso_com_dz", "torso_mass_ratio",
              "total_mass_ratio"] + [f"kp_ratio_{i}" for i in range(m.nu)]
             + ["frictionloss_ratio_mean", "armature_ratio_mean"] + [f"qpos0_offset_{i}" for i in range(len(joint_addr))])

    def fn(M):
        return jp.concatenate([
            M.geom_friction[0, 0:1], M.body_ipos[1] - ipos0, (M.body_mass[1] / mass0[1])[None],
            (M.body_mass.sum() / mass0.sum())[None], M.actuator_gainprm[:, 0] / kp0,
            jp.mean(M.dof_frictionloss[dof_addr] / fl0)[None], jp.mean(M.dof_armature[dof_addr] / arm0)[None],
            M.qpos0[joint_addr] - q00]).astype(jp.float32)

    return fn, names


def install_rma(env):
    import jax.numpy as jp

    fn, names = rma_param_fn(env)
    orig = env._get_obs

    def _get_obs(*args, **kw):
        o = dict(orig(*args, **kw))
        e = fn(env.mjx_model)
        o["rma_params"] = e
        o["rma_teacher"] = jp.concatenate([o["state"], e])
        return o

    env._get_obs = _get_obs
    return names


# ----------------------------------------------------------------------------- env construction
def build_envs(job: TrainJob, rfn):
    if job.backend == "playground_duck":
        repo = duck_repo()
        if repo is None:
            raise RuntimeError("Open_Duck_Playground source not found (set RLSOK_OPEN_DUCK_PLAYGROUND)")
        os.chdir(repo)                      # upstream loads its reference-motion pickle by relative path
        if str(repo) not in sys.path:
            sys.path.insert(0, str(repo))
        from playground.common import randomize
        from playground.open_duck_mini_v2 import joystick

        task = job.task or "flat_terrain"
        env = joystick.Joystick(task=task, config=joystick.default_config())
        eval_env = joystick.Joystick(task=task, config=joystick.default_config())
        rma_names = None
        for e in (env, eval_env):
            if rfn:
                install_duck_reward(e, rfn)
            if job.rma:
                rma_names = install_rma(e)
        return env, eval_env, randomize.domain_randomize, DUCK_PPO_CONFIG, rma_names
    from mujoco_playground import registry

    name = job.task or job.robot
    env = registry.load(name)
    eval_env = registry.load(name)
    if rfn:
        for e in (env, eval_env):
            install_generic_reward(e, rfn)
    if job.rma:
        raise NotImplementedError("RMA teacher observation is implemented for playground_duck only")
    return env, eval_env, registry.get_domain_randomizer(name), name, None


def generic_feature_spec(name: str) -> dict:
    import jax
    from mujoco_playground import registry

    env = registry.load(name)
    m = env.mj_model
    spec = {f"up_{k}": ((), "float32") for k in env._config.reward_config.scales}
    spec.update(qpos=((m.nq,), "float32"), qvel=((m.nv,), "float32"), actuator_force=((m.nu,), "float32"),
                action=((m.nu,), "float32"))
    st = jax.jit(env.reset)(jax.random.PRNGKey(0))
    if "command" in st.info:
        spec["command"] = (tuple(np.shape(st.info["command"])), "float32")
    return spec


# ----------------------------------------------------------------------------- training
def _versions() -> dict:
    out = {"python": sys.version.split()[0], "platform": platform.platform()}
    for mod in ("jax", "jaxlib", "brax", "mujoco", "mujoco_playground", "flax"):
        try:
            out[mod] = __import__(mod).__version__
        except Exception:  # noqa: BLE001
            out[mod] = None
    try:
        import jax
        out["jax_devices"] = [str(d) for d in jax.devices()]
    except Exception:  # noqa: BLE001
        pass
    return out


def _scalar_metrics(metrics) -> dict:
    out = {}
    for k, v in metrics.items():
        try:
            a = np.asarray(v)
            if a.size == 1:
                out[k] = float(a)
        except (TypeError, ValueError):
            continue
    return out


def train(job: TrainJob) -> dict:
    out = Path(job.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    cwd = os.getcwd()
    spec = duck_feature_spec() if job.backend == "playground_duck" else None
    rfn = None
    if job.reward_source:
        if spec is None:
            spec = generic_feature_spec(job.task or job.robot)
        rfn = compile_reward(job.reward_source, spec)
        (out / "reward.py").write_text(job.reward_source, encoding="utf-8")

    import jax
    from brax.training.agents.ppo import networks as ppo_networks
    from brax.training.agents.ppo import train as ppo
    from mujoco_playground import wrapper
    from mujoco_playground.config import locomotion_params

    env, eval_env, randomizer, ppo_name, rma_names = build_envs(job, rfn)
    os.chdir(cwd)
    cfg = locomotion_params.brax_ppo_config(ppo_name)
    params = cfg.to_dict()
    nf = dict(params.pop("network_factory"))
    if job.rma:
        nf["policy_obs_key"] = "rma_teacher"
    upstream = {"num_envs": params["num_envs"], "num_timesteps": params["num_timesteps"]}
    params["num_timesteps"] = int(job.num_timesteps)
    if job.num_envs:
        params["num_envs"] = int(job.num_envs)
    if job.num_evals:
        params["num_evals"] = int(job.num_evals)
    params["seed"] = int(job.seed)
    if (params["batch_size"] * params["num_minibatches"]) % params["num_envs"]:
        raise ValueError(f"batch_size*num_minibatches={params['batch_size'] * params['num_minibatches']} "
                         f"must be divisible by num_envs={params['num_envs']}")
    params.update(job.extra.get("ppo_overrides") or {})

    t0 = time.time()
    prog_path = out / "progress.jsonl"
    prog_path.write_text("", encoding="utf-8")
    state = {"last": None}

    def status(stage, **kw):
        s = {"stage": stage, "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "wall_s": round(time.time() - t0, 1), "num_timesteps": params["num_timesteps"], **kw}
        (out / "status.json").write_text(json.dumps(s, ensure_ascii=False), encoding="utf-8")

    def progress(num_steps, metrics):
        now = time.time() - t0
        row = {"steps": int(num_steps), "wall_s": round(now, 1), **_scalar_metrics(metrics)}
        if state["last"] is not None and num_steps > state["last"][0]:
            row["steps_per_s"] = (num_steps - state["last"][0]) / max(now - state["last"][1], 1e-9)
        state["last"] = (num_steps, now)
        with open(prog_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        sps = row.get("steps_per_s")
        eta = (params["num_timesteps"] - num_steps) / sps if sps else None
        status("training", steps=int(num_steps), steps_per_s=sps, eta_s=eta,
               eval_reward=row.get("eval/episode_reward"))
        print(json.dumps({"steps": int(num_steps), "wall_s": row["wall_s"], "steps_per_s": sps,
                          "eval_reward": row.get("eval/episode_reward")}), flush=True)

    obs_key = nf.get("policy_obs_key", "state")

    def save_params(current_step, make_policy, p):
        net = X.extract(p, obs_key)
        X.save_net(net, out / "policy_latest.pkl")
        if not job.rma:
            X.to_onnx(net, out / "policy_latest.onnx")
        status("training", checkpoint_step=int(current_step))

    status("compiling", ppo=params, upstream=upstream)
    train_fn = functools.partial(
        ppo.train, **params,
        network_factory=functools.partial(ppo_networks.make_ppo_networks, **nf),
        randomization_fn=randomizer, progress_fn=progress, policy_params_fn=save_params)
    make_inference_fn, final_params, _ = train_fn(environment=env, eval_env=eval_env,
                                                  wrap_env_fn=wrapper.wrap_for_brax_training)
    wall = time.time() - t0
    net = X.extract(final_params, obs_key)
    X.save_net(net, out / "policy_final.pkl")
    export = {"obs_key": obs_key}
    if not job.rma:
        X.to_onnx(net, out / "policy_final.onnx")
        export["onnx"] = str(out / "policy_final.onnx")
        export["max_abs_onnx_vs_numpy"] = X.check_onnx(net, out / "policy_final.onnx")
    rma = None
    if job.rma:
        from . import rma as R
        rma = R.fit_adapter(env, randomizer, make_inference_fn, final_params, out, rma_names, seed=job.seed)
    upstream_steps = UPSTREAM_STEPS.get(job.robot, upstream["num_timesteps"])
    rec = {
        "schema": "rlsok.robot-train/1", "job": job.to_json(), "backend": job.backend,
        "ppo_config_source": f"mujoco_playground locomotion_params.brax_ppo_config({ppo_name!r})",
        "ppo": params, "network": nf, "upstream": upstream, "upstream_steps": upstream_steps,
        "smoke": params["num_timesteps"] < upstream_steps,
        "reward": rfn.describe() if rfn else {"source": "environment default (upstream)"},
        "wall_s": wall, "mean_steps_per_s": params["num_timesteps"] / wall if wall else None,
        "versions": _versions(), "gpu": gpu_info(), "export": export, "rma": rma,
    }
    (out / "train_record.json").write_text(json.dumps(rec, indent=2, ensure_ascii=False, default=str),
                                           encoding="utf-8")
    status("finished", wall_s=round(wall, 1))
    return rec
