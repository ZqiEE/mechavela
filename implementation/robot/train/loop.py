"""Eureka-style reward design loop: external AI writes reward(f, xp) -> train -> evaluate -> feedback.

Resumable: every iteration lives in <out>/iter-NN/{ai,train,level2.json}; finished
steps are never re-run.  The external AI is any provider from implementation.circuit.agents
(exchange / command such as `claude -p` / anthropic / openai).
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path

from ..labels import Status, label
from .backends import UPSTREAM_STEPS, TrainJob, get
from .reward_api import RewardRejected, compile_reward, describe_features

CODE_RE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.S)


def extract_code(text: str) -> str:
    m = CODE_RE.findall(text)
    if not m:
        raise RewardRejected("reply must contain the reward function in one ```python fenced block")
    return m[-1].strip() + "\n"


def _reward_feedback(train_dir: Path) -> dict:
    rows = [json.loads(x) for x in (train_dir / "progress.jsonl").read_text(encoding="utf-8").splitlines() if x]
    if not rows:
        return {}
    last = rows[-1]
    comp = {k.split("reward/", 1)[1]: v for k, v in last.items() if "episode_reward/" in k or "/reward/" in k}
    return {"steps": last.get("steps"), "eval_episode_reward": last.get("eval/episode_reward"),
            "eval_episode_length": last.get("eval/avg_episode_length"), "components_last_eval": comp,
            "eval_reward_curve": [(r["steps"], r.get("eval/episode_reward")) for r in rows]}


def _level2_feedback(l2: dict) -> dict:
    c = l2["checks"]
    return {"seeds_survived": c["seeds"]["value"], "dr_train_preset_survived": c["dr_train_preset"]["value"],
            "tracking": {k: {kk: round(vv, 4) if isinstance(vv, float) else vv for kk, vv in v.items()}
                         for k, v in c["tracking"]["segments"].items()},
            "label": l2["label"]["status"]}


def prompt(task: str, spec: dict, notes: dict, history: list[dict], i: int, n: int) -> str:
    hist = json.dumps(history, indent=1, ensure_ascii=False) if history else "(first iteration)"
    return f"""# RLSOK robot reward design - iteration {i} of {n}

## Task
{task}

## What you write
One Python function, returned in a single ```python fenced block:

```python
def reward(f, xp):
    # f: dict of features (below); xp: array module (jax.numpy during training)
    ...
    return {{"name": scalar_expression, ...}}
```

Rules (enforced by an AST whitelist; a violating reply is rejected and still counts as an iteration):
- only assignments and one final `return {{...}}`; no imports, loops, if/else, lambdas, comprehensions,
  and/or (use xp.where, xp.logical_and);
- read features only as f["name"] with a literal name; call only xp.<fn> from: abs exp log log1p sqrt square
  sum mean max min clip where tanh cos sin maximum minimum dot sign zeros_like ones_like logical_and logical_or
  logical_not array linalg.norm any all prod arctan2; xp.pi; builtins abs min max float;
- every component must be a scalar. Components are summed with weight 1 (you own the scaling), then the
  environment multiplies by ctrl_dt=0.02 and clips the per-step total to [0, 10000] (upstream behaviour),
  so a negative total gives 0: keep a positive survival/tracking term.
- each component is logged separately and reported back to you after training.

## Features
{describe_features(spec, notes)}

## Evaluation (pre-registered level-2 criteria, applied after training)
- 10 seeds of the fixed command schedule: >= 9 must not fall;
- per walking segment: |mean velocity - command| <= official-policy error + 0.05 on the commanded axis;
- training-distribution domain randomisation: >= 16/20 episodes without a fall.

## History (previous iterations: your reward, training curve, per-component values, evaluation)
{hist}
"""


def run_loop(task: str, base: TrainJob, out: Path, agent, max_iterations: int = 3) -> dict:
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    backend = get(base.backend)
    spec = backend.feature_spec(base.robot)
    from .backends import DUCK_FEATURE_NOTES
    notes = DUCK_FEATURE_NOTES if base.backend == "playground_duck" else {}
    history, result = [], None
    for i in range(1, max_iterations + 1):
        it = out / f"iter-{i:02d}"
        ai = it / "ai"
        ai.mkdir(parents=True, exist_ok=True)
        rec_path = it / "iteration.json"
        if rec_path.exists():
            rec = json.loads(rec_path.read_text(encoding="utf-8"))
            history.append(rec["feedback"])
            result = rec
            if rec.get("level2_status") == "VERIFIED":
                break
            continue
        p = prompt(task, spec, notes, history, i, max_iterations)
        if not (ai / "prompt.md").exists():
            (ai / "prompt.md").write_text(p, encoding="utf-8")
        text = agent.complete((ai / "prompt.md").read_text(encoding="utf-8"), ai)   # may raise AwaitingAI
        (ai / "response.raw.md").write_text(text, encoding="utf-8")
        try:
            src = extract_code(text)
            rfn = compile_reward(src, spec)
        except RewardRejected as e:
            rec = {"iteration": i, "status": "REJECTED", "reason": str(e),
                   "feedback": {"iteration": i, "rejected": str(e)}}
            rec_path.write_text(json.dumps(rec, indent=2, ensure_ascii=False), encoding="utf-8")
            history.append(rec["feedback"])
            continue
        job = replace(base, out=str(it / "train"), reward_source=src)
        train_rec = backend.train(job)
        from .level2 import judge
        l2 = judge(base.robot, Path(job.out) / "policy_final.onnx", trained_steps=job.num_timesteps,
                   upstream_steps=UPSTREAM_STEPS[base.robot], out=it / "level2.json", train_record=None)
        fb = {"iteration": i, "reward_source": src, "reward_sha256": rfn.sha256,
              "training": _reward_feedback(Path(job.out)), "evaluation": _level2_feedback(l2)}
        rec = {"iteration": i, "status": "EVALUATED", "level2_status": l2["label"]["status"],
               "train_wall_s": train_rec.get("wall_s"), "agent": agent.describe(),
               "agent_meta": getattr(agent, "last_meta", None), "feedback": fb}
        rec_path.write_text(json.dumps(rec, indent=2, ensure_ascii=False), encoding="utf-8")
        history.append(fb)
        result = rec
        if rec["level2_status"] == "VERIFIED":
            break
    summary = {"schema": "rlsok.robot-reward-loop/1", "task": task, "base_job": base.to_json(),
               "iterations": len(history), "final": result,
               "label": label(Status[result["level2_status"]] if result and result.get("level2_status") else
                              Status.NOT_RUN, "Eureka 式奖励迭代", reason="见各轮 iteration.json")}
    (out / "loop_summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return summary
