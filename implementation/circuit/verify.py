"""Independent re-check of a finished circuit-loop run directory.

Does not run any solver and does not trust recorded numbers: every metric and
check is recomputed from the sealed native vectors with the frozen spec, every
simulated netlist is regenerated from the recorded DUT text, and every file is
compared against the sha256 index. Any mismatch is reported as a failure.
"""
from __future__ import annotations

import gzip
import json
import math
from pathlib import Path

from implementation import solver_interface as SI
from implementation.circuit import bom as B
from implementation.circuit import netlist as N
from implementation.circuit import spec as S
from implementation.circuit import tolerance as T


# Reconstruction only: accept every element letter any loop version offered, so a run made
# under an earlier whitelist (e.g. with JFETs) is still re-derived instead of re-policed.
RECONSTRUCT_ELEMENTS = tuple(dict.fromkeys(N.DEFAULT_DUT_ELEMENTS + ("J",)))


class Checker:
    def __init__(self):
        self.items = []

    def check(self, name, ok, detail=""):
        self.items.append({"check": name, "ok": bool(ok), "detail": str(detail)[:500]})
        return ok

    @property
    def ok(self):
        return all(item["ok"] for item in self.items)


REL_TOL, ABS_TOL = 1e-9, 1e-15


def _same(a, b):
    """Structural equality; floats within 1e-9 relative (last-digit differences between
    Python versions / summation orders), everything else - incl. every pass/fail flag - exact."""
    if isinstance(a, bool) or isinstance(b, bool) or a is None or b is None:
        return a is b or a == b and type(a) is type(b)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(a, b, rel_tol=REL_TOL, abs_tol=ABS_TOL)
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    return a == b


def _native_outputs(run_dir):
    with gzip.open(run_dir / "vectors.json.gz", "rt", encoding="utf-8") as stream:
        data = json.load(stream)
    return {"vectors": data["vectors"], "complex_vectors": data.get("complex_vectors", {})}


def _native_ok(c, label, run_dir, version):
    execution = SI.read_json(run_dir / "execution.json")
    result = SI.read_json(run_dir / "result.json")
    c.check(label + ": native exit 0, no limit, quiescent",
            execution["exit_code"] == 0 and not execution["limit_event"] and execution["all_job_processes_quiescent"],
            {k: execution.get(k) for k in ("exit_code", "limit_event", "all_job_processes_quiescent")})
    c.check(label + ": solver SUCCEEDED with recorded version", result.get("status") == "SUCCEEDED"
            and result.get("native_version") == version, result.get("native_version"))


def verify_run(directory, replay=False):
    from implementation.circuit.loop import CircuitLoop, FINAL_STATUSES
    directory = Path(directory).resolve()
    c = Checker()
    index_path = directory / "files.json"
    if not c.check("files.json present", index_path.is_file()):
        return {"status": "VERIFICATION_FAILED", "checks": c.items}
    index = SI.read_json(index_path)["files"]
    actual = {p.relative_to(directory).as_posix() for p in directory.rglob("*") if p.is_file() and p != index_path}
    c.check("file set equals sealed index", set(index) == actual,
            {"unsealed": sorted(actual - set(index))[:10], "missing": sorted(set(index) - actual)[:10]})
    changed = [name for name, digest in index.items() if (directory / name).is_file() and SI.sha256_file(directory / name) != digest]
    c.check("every sealed file unchanged (sha256)", not changed, changed[:10])

    run = SI.read_json(directory / "run.json")
    report = SI.read_json(directory / "final" / "report.json")
    version = run["tools"]["solver"]["version"]
    c.check("final status is a known terminal status", report["status"] in FINAL_STATUSES, report["status"])
    c.check("report bound to this run", report["run_id"] == run["run_id"] and report["requirement"] == run["requirement"])
    _native_ok(c, "tool probe", directory / "tool-probe" / "probe", version)

    # AI exchanges: prompt/response are the hashed ones
    for exchange in sorted(directory.rglob("exchange.json")):
        record = SI.read_json(exchange)
        folder = exchange.parent
        c.check("AI exchange hashes %s" % folder.relative_to(directory).as_posix(),
                SI.sha256_file(folder / "prompt.md") == record["prompt_sha256"]
                and SI.sha256_file(folder / "response.md") == record["response_sha256"])

    spec_path = directory / "spec" / "spec.json"
    if not spec_path.is_file():
        c.check("no spec => status NOT_ACHIEVED_SPEC_REJECTED", report["status"] == "NOT_ACHIEVED_SPEC_REJECTED")
        return {"status": "VERIFIED" if c.ok else "VERIFICATION_FAILED", "run_status": report["status"], "checks": c.items}
    spec = SI.read_json(spec_path)
    c.check("report premises bound to frozen spec", report["premises"]["spec_sha256"] == SI.sha256_file(spec_path))
    if (directory / "spec" / "approval.json").is_file():
        approval = SI.read_json(directory / "spec" / "approval.json")
        c.check("spec approval refers to the frozen spec", approval["spec_sha256"] == SI.sha256_file(spec_path))

    rounds = [SI.read_json(p) for p in sorted((directory / "rounds").glob("round-*/round.json"))]
    c.check("round numbering contiguous from 1", [r["round"] for r in rounds] == list(range(1, len(rounds) + 1)))
    c.check("round count within iteration limit", len(rounds) <= run["settings"]["max_iterations"])
    for record in rounds:
        label = "round %d" % record["round"]
        folder = directory / "rounds" / ("round-%02d" % record["round"])
        if not record.get("nominal_run"):
            c.check(label + ": no simulation => not PASS", record["status"] != "PASS", record["status"])
            continue
        dut = N.validate_dut_body(record["dut_body"], spec["ports"], RECONSTRUCT_ELEMENTS)
        expected_netlist = N.assemble("RLSOK circuit loop %s round %d" % (run["run_id"][:8], record["round"]),
                                      spec["testbench"], spec["ports"], record["dut_body"])
        nominal = folder / "nominal"
        if (nominal / "rejected.json").is_file():
            # Pre-flight rejection: the netlist never reached the native solver.
            rejected = SI.read_json(nominal / "rejected.json")
            c.check(label + ": pre-flight rejection recorded and round not PASS",
                    rejected.get("status") == record["nominal_run"]["status"] and record["status"] != "PASS",
                    rejected.get("reason"))
            c.check(label + ": recorded netlist == frozen testbench + recorded DUT",
                    (folder / "netlist.cir").read_text(encoding="utf-8") == expected_netlist)
            continue
        c.check(label + ": simulated netlist == frozen testbench + recorded DUT",
                (nominal / "input-netlist.cir").read_text(encoding="utf-8") == expected_netlist
                and (folder / "netlist.cir").read_text(encoding="utf-8") == expected_netlist)
        job = SI.read_json(nominal / "input-job.json")
        c.check(label + ": analysis == frozen spec analysis", job["analysis"] == spec["analysis"])
        if record["nominal_run"]["status"] != SI.SUCCEEDED:
            c.check(label + ": failed solver run => not PASS", record["status"] != "PASS")
            continue
        _native_ok(c, label + " nominal", nominal, version)
        sources = S.audit_vectors(spec, dut)[0] if spec["analysis"]["kind"] != "ac" else []
        recomputed = CircuitLoop.evaluate(spec, _native_outputs(nominal), sources)
        recorded = SI.read_json(folder / "evaluation.json")
        c.check(label + ": metrics/checks recomputed from native vectors match record",
                _same(recomputed, recorded) and _same(recomputed["checks"], record["checks"]))
        expected_status = "FAIL_NOMINAL" if not recomputed["passed"] else None
        if expected_status:
            c.check(label + ": status consistent with checks", record["status"] == expected_status, record["status"])
            continue
        tolerance = SI.read_json(folder / "tolerance.json")
        plan = T.build_cases(record["dut_body"], dut, run["settings"]["tolerance_relative"],
                             run["settings"]["monte_carlo_samples"], run["settings"]["seed"])
        c.check(label + ": tolerance case set regenerated", [x["id"] for x in plan["cases"]] == [x["id"] for x in tolerance["cases"]])
        all_passed = True
        for case, row in zip(plan["cases"], tolerance["cases"]):
            case_dir = folder / "tolerance" / case["id"]
            netlist = N.assemble("RLSOK tolerance %s" % case["id"], spec["testbench"], spec["ports"], case["dut"])
            ok_netlist = (case_dir / "input-netlist.cir").read_text(encoding="utf-8") == netlist
            if row["solver_status"] == SI.SUCCEEDED:
                _native_ok(c, label + " " + case["id"], case_dir, version)
                again = CircuitLoop.evaluate(spec, _native_outputs(case_dir), sources)
                ok_eval = again["passed"] == row["passed"] and _same(again["metrics"], row["metrics"])
            else:
                ok_eval = row["passed"] is False
            all_passed &= bool(row["passed"])
            c.check(label + " " + case["id"] + ": netlist and evaluation reproduced", ok_netlist and ok_eval)
        expected = "PASS" if all_passed else "FAIL_TOLERANCE"
        c.check(label + ": status consistent with tolerance sweep", record["status"] == expected, record["status"])

    passed_rounds = [r for r in rounds if r["status"] == "PASS"]
    if report["status"] == "ACHIEVED":
        c.check("ACHIEVED requires the last round to PASS", rounds and rounds[-1]["status"] == "PASS" and report["final_round"] == rounds[-1]["round"])
    else:
        c.check("not ACHIEVED => no PASS round", not passed_rounds)
    if report.get("final_round"):
        record = rounds[report["final_round"] - 1]
        stem = "final" if report["achieved"] else "best_attempt_NOT_ACCEPTED"
        netlist = (directory / "rounds" / ("round-%02d" % record["round"]) / "netlist.cir").read_text(encoding="utf-8")
        delivered = (directory / "final" / (stem + "_netlist.cir")).read_text(encoding="utf-8")
        c.check("delivered netlist == simulated netlist of chosen round", delivered.split("\n", 1)[1] == netlist)
        dut = N.validate_dut_body(record["dut_body"], spec["ports"], RECONSTRUCT_ELEMENTS)
        bom = B.build_bom(dut, spec["tolerance"]["relative"], record.get("part_suggestions"))
        # Every field the run recorded must match the BOM regenerated from the delivered DUT
        # (fields added by later RLSOK versions are not required of older runs).
        recorded_bom = report["bom"]
        consistent = len(bom) == len(recorded_bom) and all(
            all(_same(row.get(key), value) for key, value in old.items()) for row, old in zip(bom, recorded_bom))
        c.check("BOM regenerated from delivered DUT", consistent)
    if replay and report.get("final_round"):
        _replay(c, directory, run, spec, rounds[report["final_round"] - 1])
    return {"status": "VERIFIED" if c.ok else "VERIFICATION_FAILED", "run_status": report["status"],
            "checks_total": len(c.items), "checks_failed": [i for i in c.items if not i["ok"]], "checks": c.items}


def _replay(c, directory, run, spec, record):
    """Re-simulate the delivered netlist with the solver available now; compare recomputed metrics."""
    import math
    import tempfile
    from implementation.circuit.loop import CircuitLoop
    adapter = SI.get(run["settings"]["solver"])
    folder = directory / "rounds" / ("round-%02d" % record["round"])
    job = SI.read_json(folder / "nominal" / "input-job.json")
    scratch = Path(tempfile.mkdtemp(prefix="rlsok-replay-"))
    result = adapter.run({k: job[k] for k in ("netlist", "analysis", "vectors")}, scratch / "replay",
                         SI.ResourceCaps(**run["settings"]["caps_per_simulation"]))
    identity = adapter.identity(scratch / "probe").as_dict()
    same_tool = identity["binary_sha256"] == run["tools"]["solver"]["binary_sha256"]
    if not c.check("replay: native re-simulation SUCCEEDED", result.status == SI.SUCCEEDED, result.reason):
        return
    dut = N.validate_dut_body(record["dut_body"], spec["ports"], RECONSTRUCT_ELEMENTS)
    sources = S.audit_vectors(spec, dut)[0] if spec["analysis"]["kind"] != "ac" else []
    again = CircuitLoop.evaluate(spec, result.outputs, sources)
    recorded = SI.read_json(folder / "evaluation.json")
    # Same binary: last-digit agreement. Different build/version: 1e-3 relative, with a 1e-6
    # absolute floor so metrics that are ~0 (e.g. sub-microvolt ripple) are not judged by noise.
    rel, floor = (1e-9, 1e-12) if same_tool else (1e-3, 1e-6)
    worst, outside = 0.0, []
    for name, value in recorded["metrics"].items():
        other = again["metrics"].get(name)
        if other is None:
            outside.append(name + " missing")
            continue
        worst = max(worst, abs(other - value) / max(abs(value), 1e-12))
        if not math.isclose(other, value, rel_tol=rel, abs_tol=floor):
            outside.append("%s %.6g vs %.6g" % (name, other, value))
    verdicts = [c["passed"] for c in again["checks"]] == [c["passed"] for c in recorded["checks"]]
    c.check("replay: metrics reproduced (rel %g / abs %g, %s); every check verdict identical"
            % (rel, floor, "same solver binary" if same_tool else "DIFFERENT solver binary %s" % identity["binary_sha256"][:12]),
            not outside and verdicts, {"outside_tolerance": outside[:10], "max_relative_difference": worst})
