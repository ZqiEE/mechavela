"""RMA (Rapid Motor Adaptation) phase 2: regress the randomised physics from observation history.

Phase 1 (playground_train with job.rma=True) trains a teacher whose policy sees
``state`` + e, where e are the randomised physics parameters.  Here the teacher is
rolled out in randomised environments; an adaptation module (ridge regression on
the last H ``state`` observations, closed form) predicts e from history only.

Reported: per-parameter R^2 on held-out environments.  Deploying the student
(teacher + predicted e) in the RLSOK CPU evaluator is NOT_SUPPORTED yet, so RMA
results never count towards level 2.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from ..labels import Status, label

HISTORY = 30
RIDGE = 1e-2


def collect(env, randomizer, make_inference_fn, params, n_envs=256, n_steps=300, seed=0):
    import jax
    from mujoco_playground import wrapper

    rng = jax.random.PRNGKey(seed + 12345)
    keys = jax.random.split(rng, n_envs)
    venv = wrapper.wrap_for_brax_training(env, episode_length=n_steps + 1, action_repeat=1,
                                          randomization_fn=lambda m: randomizer(m, keys))
    policy = make_inference_fn(params, deterministic=True)
    state = jax.jit(venv.reset)(jax.random.split(jax.random.PRNGKey(seed), n_envs))

    def step(carry, _):
        st, r = carry
        r, k = jax.random.split(r)
        act, _ = policy(st.obs, k)
        st = venv.step(st, act)
        return (st, r), (st.obs["state"], st.obs["rma_params"], st.done)

    (_, _), (obs, e, done) = jax.jit(lambda s: jax.lax.scan(step, (s, rng), None, length=n_steps))(state)
    return np.asarray(obs), np.asarray(e), np.asarray(done)


def windows(obs, e, done, h=HISTORY):
    T, N, D = obs.shape
    X, Y, G = [], [], []
    alive = np.cumsum(done, axis=0) == 0
    for t in range(h, T):
        ok = alive[t]
        X.append(obs[t - h:t].transpose(1, 0, 2).reshape(N, h * D)[ok])
        Y.append(e[t][ok])
        G.append(np.arange(N)[ok])
    return np.concatenate(X), np.concatenate(Y), np.concatenate(G)


def ridge_fit(X, Y, lam=RIDGE):
    mu, sd = X.mean(0), X.std(0) + 1e-6
    Xs = (X - mu) / sd
    ym = Y.mean(0)
    A = Xs.T @ Xs + lam * len(Xs) * np.eye(Xs.shape[1])
    W = np.linalg.solve(A, Xs.T @ (Y - ym))
    return {"mu": mu, "sd": sd, "W": W, "ym": ym}


def ridge_predict(m, X):
    return ((X - m["mu"]) / m["sd"]) @ m["W"] + m["ym"]


def r2(y, p):
    ss = ((y - y.mean(0)) ** 2).sum(0)
    return np.where(ss > 1e-12, 1 - ((y - p) ** 2).sum(0) / np.maximum(ss, 1e-12), np.nan)


def fit_adapter(env, randomizer, make_inference_fn, params, out: Path, names, seed=0, n_envs=256, n_steps=300):
    obs, e, done = collect(env, randomizer, make_inference_fn, params, n_envs, n_steps, seed)
    X, Y, G = windows(obs, e, done)
    train = G < int(0.8 * n_envs)
    m = ridge_fit(X[train], Y[train])
    score = r2(Y[~train], ridge_predict(m, X[~train]))
    np.savez(Path(out) / "rma_adapter.npz", history=HISTORY, **m)
    res = {"history": HISTORY, "ridge_lambda": RIDGE, "train_windows": int(train.sum()),
           "test_windows": int((~train).sum()), "heldout_envs": int(n_envs - int(0.8 * n_envs)),
           "r2": {n: (None if np.isnan(v) else float(v)) for n, v in zip(names, score)},
           "label": label(Status.UNVERIFIED, "教师策略在训练分布随机化下的 rollout；留出环境上的 R^2",
                          reason="学生策略在 RLSOK 评测器中的部署评测尚未实现（NOT_SUPPORTED），不参与第二级判定")}
    (Path(out) / "rma_adapter.json").write_text(json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")
    return res
