"""RLSOK circuit closed loop - command line (for people and for external AI).

    python -m implementation.circuit solvers
    python -m implementation.circuit run --requirement "把 12V 降到 5V，负载电流 500mA" --out runs/buck \
        --agent command --agent-command "claude -p --tools \"\" --output-format json"
    python -m implementation.circuit resume --out runs/buck
    python -m implementation.circuit approve-spec --out runs/buck --by "Name" --note "thresholds OK"
    python -m implementation.circuit verify --out runs/buck

Exit codes: 0 ACHIEVED / verified, 2 honestly NOT achieved, 3 waiting (AI response
or spec approval), 1 rejected input or tool error.
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
from implementation.circuit import agents as A  # noqa: E402


def _agent(args, stored=None):
    config = dict(stored or {})
    for key in ("kind", "command", "model", "base_url"):
        value = getattr(args, {"kind": "agent", "command": "agent_command"}.get(key, key), None)
        if value:
            config[key] = value
    if not config.get("kind"):
        raise ValueError("--agent required (command, exchange, anthropic, openai)")
    agent = A.make_agent(config["kind"], model=config.get("model"), command=config.get("command"),
                         base_url=config.get("base_url"))
    return agent, {k: v for k, v in config.items() if k in ("kind", "command", "model", "base_url")}


def _summary(report, directory):
    if report.get("status", "").startswith("AWAITING"):
        return report
    return {"status": report["status"], "achieved": report["achieved"], "conclusion": report["conclusion"],
            "final_round": report["final_round"], "iterations": [(i["round"], i["status"]) for i in report["iterations"]],
            "native_simulations": report["native_simulation_count"],
            "report_html": str(Path(directory) / "final" / "report.html"),
            "report_json": str(Path(directory) / "final" / "report.json")}


def _exit(report):
    status = report.get("status", "")
    if status.startswith("AWAITING"):
        return 3
    return 0 if report.get("achieved") else 2


def main(argv=None):
    parser = argparse.ArgumentParser(prog="rlsok-circuit", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("solvers", help="list solver adapters behind the generic interface")
    for name in ("run", "resume"):
        p = sub.add_parser(name)
        p.add_argument("--out", required=True, help="run directory (new for run; existing for resume)")
        p.add_argument("--agent", choices=("command", "exchange", "anthropic", "openai"))
        p.add_argument("--agent-command")
        p.add_argument("--model")
        p.add_argument("--base-url")
        if name == "run":
            group = p.add_mutually_exclusive_group(required=True)
            group.add_argument("--requirement")
            group.add_argument("--requirement-file")
            p.add_argument("--spec", help="optional human-written spec JSON (skips the AI spec draft)")
            p.add_argument("--max-iterations", type=int, default=6)
            p.add_argument("--tolerance", type=float, default=0.05)
            p.add_argument("--mc-samples", type=int, default=20)
            p.add_argument("--seed", type=int, default=1)
            p.add_argument("--review-spec", action="store_true", help="pause for human approval of the frozen spec")
            p.add_argument("--max-sims", type=int, default=600, help="whole-run native simulation budget")
            p.add_argument("--sim-wall", type=float, default=120.0)
            p.add_argument("--sim-cpu", type=float, default=120.0)
            p.add_argument("--sim-memory-mib", type=int, default=1024)
    p = sub.add_parser("approve-spec")
    p.add_argument("--out", required=True)
    p.add_argument("--by", required=True)
    p.add_argument("--note", default="")
    p = sub.add_parser("verify")
    p.add_argument("--out", required=True)
    p.add_argument("--replay", action="store_true", help="also re-simulate the delivered netlist with the local solver")
    args = parser.parse_args(argv)
    try:
        if args.command == "solvers":
            value = {"schema": "rlsok.solver-registry/1", "solvers": SI.registry(),
                     "note": "AVAILABLE means the native tool can be launched here; it is not engineering acceptance."}
            print(json.dumps(value, ensure_ascii=False, indent=1))
            return 0
        if args.command == "verify":
            from implementation.circuit.verify import verify_run
            value = verify_run(args.out, replay=args.replay)
            brief = {k: v for k, v in value.items() if k != "checks"}
            print(json.dumps(brief, ensure_ascii=False, indent=1))
            return 0 if value["status"] == "VERIFIED" else 1
        from implementation.circuit.loop import CircuitLoop
        if args.command == "approve-spec":
            loop = CircuitLoop(args.out, A.ExchangeAgent())
            loop.approve_spec(args.by, args.note)
            print(json.dumps({"status": "SPEC_APPROVED", "next": "run `resume`"}))
            return 0
        if args.command == "run":
            requirement = args.requirement if args.requirement else Path(args.requirement_file).read_text(encoding="utf-8").strip()
            agent, config = _agent(args)
            caps = SI.ResourceCaps(wall_seconds=args.sim_wall, cpu_seconds=args.sim_cpu,
                                   memory_bytes=args.sim_memory_mib * 1024 * 1024)
            loop = CircuitLoop.create(args.out, requirement, agent, max_iterations=args.max_iterations,
                                      tolerance=args.tolerance, mc_samples=args.mc_samples, seed=args.seed,
                                      spec_file=args.spec, caps=caps, agent_config=config,
                                      spec_review=args.review_spec, max_native_simulations=args.max_sims)
        else:
            stored = SI.read_json(Path(args.out) / "run.json").get("agent_config")
            agent, _ = _agent(args, stored)
            loop = CircuitLoop(args.out, agent)
        report = loop.run()
        print(json.dumps(_summary(report, loop.dir), ensure_ascii=False, indent=1))
        return _exit(report)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
        print(json.dumps({"status": "REJECTED", "reason": str(error)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
