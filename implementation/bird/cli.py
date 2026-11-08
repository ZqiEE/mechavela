"""RLSOK bird design loop - command line (used by the console; also usable by an external AI in a terminal).

    python -m implementation.bird.cli run --requirement "..." --out runs/bird --agent exchange
    python -m implementation.bird.cli resume --out runs/bird

Exit codes: 0 finished (spec met in simulation, UNVERIFIED), 2 not achieved, 3 waiting for the AI, 1 rejected input.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from implementation import solver_interface as SI  # noqa: E402
from implementation.circuit import cli as CC  # noqa: E402  (agent wiring is shared)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="rlsok-bird")
    sub = ap.add_subparsers(dest="command", required=True)
    for name in ("run", "resume"):
        p = sub.add_parser(name)
        p.add_argument("--out", required=True)
        p.add_argument("--agent", choices=("command", "exchange", "anthropic", "openai"))
        p.add_argument("--agent-command"); p.add_argument("--model"); p.add_argument("--base-url")
        p.add_argument("--local-parts", help="local parts/price overrides JSON")
        if name == "run":
            p.add_argument("--requirement", required=True)
            p.add_argument("--max-iterations", type=int, default=6)
    a = ap.parse_args(argv)
    from implementation.bird.loop import BirdLoop
    try:
        local = Path(a.local_parts) if a.local_parts else None
        if a.command == "run":
            agent, cfg = CC._agent(a)
            loop = BirdLoop.create(a.out, a.requirement, agent, a.max_iterations, cfg, local)
        else:
            agent, _ = CC._agent(a, SI.read_json(Path(a.out) / "run.json").get("agent_config"))
            loop = BirdLoop(a.out, agent, local)
        rep = loop.run()
        print(json.dumps({k: rep.get(k) for k in ("status", "achieved", "conclusion", "final_round", "prompt", "response")},
                         ensure_ascii=False, indent=1))
        s = rep.get("status", "")
        return 3 if s.startswith("AWAITING") else (0 if rep.get("achieved") else 2)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as e:
        print(json.dumps({"status": "REJECTED", "reason": str(e)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
