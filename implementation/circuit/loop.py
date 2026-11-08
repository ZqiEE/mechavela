"""Circuit closed loop orchestrator (resumable).

requirement -> AI spec draft -> platform validation/freeze (optional human
approval) -> AI DUT -> native ngspice -> checks + power audit -> ±tol sweep
(native) -> feedback -> ... until PASS, AI-declared infeasible (only after a
simulated attempt), or the iteration / simulation budget is exhausted.

The loop never edits designs and never writes a measured value that did not
come from a native solver run in this run directory.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import platform
import sys
import uuid

from implementation import solver_interface as SI
from implementation.circuit import agents as A
from implementation.circuit import buildability as B
from implementation.circuit import metrics as M
from implementation.circuit import netlist as N
from implementation.circuit import prompts as P
from implementation.circuit import spec as S
from implementation.circuit import tolerance as T

SCHEMA_RUN = "rlsok.circuit-loop-run/1"
SPEC_ATTEMPTS = 3
AUDIT_FRACTION, AUDIT_FLOOR_W = 0.02, 1e-6

ACHIEVED = "ACHIEVED"
NOT_ITER = "NOT_ACHIEVED_ITERATION_LIMIT"
NOT_INFEASIBLE = "NOT_ACHIEVED_AI_DECLARED_INFEASIBLE"
NOT_SPEC = "NOT_ACHIEVED_SPEC_REJECTED"
NOT_BUDGET = "NOT_ACHIEVED_SIMULATION_BUDGET"
FINAL_STATUSES = (ACHIEVED, NOT_ITER, NOT_INFEASIBLE, NOT_SPEC, NOT_BUDGET)


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class BudgetExhausted(Exception):
    pass


class CircuitLoop:
    def __init__(self, directory, agent: A.Agent, adapter=None):
        self.dir = Path(directory).resolve()
        self.agent = agent
        self.adapter = adapter or SI.get("ngspice")
        self.run_info = SI.read_json(self.dir / "run.json")
        self.settings = self.run_info["settings"]

    # ------------------------------------------------------------------ setup
    @classmethod
    def create(cls, directory, requirement, agent, max_iterations=6, tolerance=0.05, mc_samples=20, seed=1,
               spec_file=None, adapter=None, caps=None, agent_config=None, spec_review=False,
               max_native_simulations=600):
        directory = Path(directory).resolve()
        if directory.exists() and any(directory.iterdir()):
            raise ValueError("RUN_DIRECTORY_NOT_EMPTY (never overwrite evidence): " + str(directory))
        if not isinstance(requirement, str) or not requirement.strip():
            raise ValueError("REQUIREMENT_TEXT_REQUIRED")
        if not 1 <= max_iterations <= 30:
            raise ValueError("MAX_ITERATIONS_1_TO_30")
        if not 0.05 <= tolerance <= 0.5:
            raise ValueError("TOLERANCE_MUST_BE_AT_LEAST_0.05")
        if not 0 <= mc_samples <= 150:
            raise ValueError("MONTE_CARLO_SAMPLES_0_TO_150")
        if not 10 <= max_native_simulations <= 5000:
            raise ValueError("MAX_NATIVE_SIMULATIONS_10_TO_5000")
        directory.mkdir(parents=True, exist_ok=True)
        adapter = adapter or SI.get("ngspice")
        identity = adapter.identity(directory / "tool-probe").as_dict()
        caps = caps or SI.ResourceCaps(wall_seconds=120, cpu_seconds=120, memory_bytes=1 << 30)
        info = {"schema": SCHEMA_RUN, "run_id": uuid.uuid4().hex, "created_utc": now(),
                "requirement": requirement,
                "settings": {"max_iterations": max_iterations, "tolerance_relative": tolerance,
                             "monte_carlo_samples": mc_samples, "seed": seed, "solver": adapter.name,
                             "caps_per_simulation": caps.as_dict(), "max_native_simulations": max_native_simulations,
                             "spec_source": "user file" if spec_file else "external AI draft",
                             "spec_review_required": bool(spec_review)},
                "tools": {"solver": identity, "python": sys.version.split()[0], "platform": platform.platform(),
                          "rlsok_files": _source_hashes()},
                "agent": agent.describe(),
                "agent_config": agent_config or {"kind": agent.kind}}
        SI.write_json(directory / "run.json", info)
        loop = cls(directory, agent, adapter)
        if spec_file:
            draft = SI.read_json(spec_file)
            draft = {k: v for k, v in draft.items() if k in S.SPEC_FIELDS}
            loop._freeze_spec(draft, "user file " + Path(spec_file).name, None)
            loop._state("design")
        else:
            loop._state("spec")
        return loop

    # ----------------------------------------------------------------- driver
    def run(self):
        """Advance as far as possible. Returns the final report, or a waiting status."""
        state = SI.read_json(self.dir / "state.json")
        if state["phase"] == "done":
            return SI.read_json(self.dir / "final" / "report.json")
        try:
            if state["phase"] == "spec":
                if not self._spec_phase():
                    return self._finish(NOT_SPEC)
                self._state("design")
            if self.settings.get("spec_review_required") and not (self.dir / "spec" / "approval.json").is_file():
                self._state("awaiting_spec_approval")
                return {"status": "AWAITING_SPEC_APPROVAL", "spec": str(self.dir / "spec" / "spec.json"),
                        "next": "review spec.json, then run `approve-spec` and `resume`"}
            self._state("design")
            spec = SI.read_json(self.dir / "spec" / "spec.json")
            for number in range(1, self.settings["max_iterations"] + 1):
                record = self._design_round(spec, number)
                if record["status"] == "PASS":
                    return self._finish(ACHIEVED)
                if record["status"] == "AI_DECLARED_INFEASIBLE":
                    return self._finish(NOT_INFEASIBLE)
            return self._finish(NOT_ITER)
        except A.AwaitingAI as waiting:
            return {"status": "AWAITING_AI", "prompt": waiting.prompt_path, "response": waiting.response_path,
                    "next": "the external AI writes the response file, then run `resume`"}
        except A.AgentUnavailable as error:
            # Nothing was decided; the saved prompt is re-sent unchanged on resume.
            with (self.dir / "ai-unavailable.log").open("a", encoding="utf-8") as log:
                log.write("%s %s\n" % (now(), error))
            return {"status": "AWAITING_AI_UNAVAILABLE", "reason": str(error),
                    "next": "the external AI could not answer; run `resume` when it is available"}
        except BudgetExhausted:
            return self._finish(NOT_BUDGET)

    def approve_spec(self, by, note=""):
        spec_path = self.dir / "spec" / "spec.json"
        if not spec_path.is_file():
            raise ValueError("NO_FROZEN_SPEC_TO_APPROVE")
        if (self.dir / "spec" / "approval.json").is_file():
            raise ValueError("SPEC_ALREADY_APPROVED")
        if not isinstance(by, str) or not by.strip():
            raise ValueError("APPROVER_NAME_REQUIRED")
        SI.write_json(self.dir / "spec" / "approval.json", {
            "schema": "rlsok.circuit-spec-approval/1", "approved_by": by.strip()[:200], "note": str(note)[:2000],
            "approved_utc": now(), "spec_sha256": SI.sha256_file(spec_path),
            "identity_note": "Name is a recorded statement, not cryptographic authentication."})

    def _state(self, phase):
        SI.write_json(self.dir / "state.json", {"phase": phase, "updated_utc": now()})

    def _ask(self, exchange_dir, prompt):
        exchange_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = exchange_dir / "prompt.md"
        if prompt_path.is_file():
            saved = prompt_path.read_text(encoding="utf-8")
            if saved != prompt:
                # The saved prompt is what the AI was (or is being) asked; never replace it. This happens after an
                # RLSOK upgrade changes prompt wording while a job waits for a reply. Recorded, not silent.
                with (exchange_dir / "prompt-regenerated-differs.log").open("a", encoding="utf-8") as log:
                    log.write("%s kept the saved prompt (sha256 %s); a regenerated one differed\n"
                              % (now(), SI.sha256_file(prompt_path)))
                prompt = saved
        else:
            prompt_path.write_text(prompt, encoding="utf-8")
        text = self.agent.complete(prompt, exchange_dir)
        response_path = exchange_dir / "response.md"
        if not response_path.is_file():
            response_path.write_text(text, encoding="utf-8")
        SI.write_json(exchange_dir / "exchange.json", {
            "agent": self.agent.describe(), "agent_reported": self.agent.last_meta, "completed_utc": now(),
            "prompt_sha256": SI.sha256_file(prompt_path), "response_sha256": SI.sha256_file(response_path)})
        return text

    # ------------------------------------------------------------------- spec
    def _spec_phase(self):
        error = None
        for attempt in range(1, SPEC_ATTEMPTS + 1):
            folder = self.dir / "spec" / ("attempt-%02d" % attempt)
            validation = folder / "validation.json"
            if validation.is_file():
                result = SI.read_json(validation)
                if result["ok"]:
                    return True
                error = result["error"]
                continue
            prompt = P.spec_prompt(self.run_info["requirement"], self.run_info["tools"]["solver"]["version"],
                                   self.settings["tolerance_relative"], error)
            text = self._ask(folder, prompt)
            try:
                draft = A.extract_json(text)
                self._freeze_spec(draft, "external AI draft (attempt %d)" % attempt, folder)
                SI.write_json(validation, {"ok": True})
                return True
            except (ValueError, KeyError, TypeError, IndexError) as exc:
                error = str(exc)
                SI.write_json(validation, {"ok": False, "error": error})
        return False

    def _freeze_spec(self, draft, source, folder):
        body = S.validate_spec(draft, self.adapter)
        if not source.startswith("user file"):
            B.require_power_checks(body)      # AI-drafted power specs must cover start-up overshoot and a load step
        audited = body["analysis"]["kind"] in ("tran", "op")
        spec = {"schema": S.SCHEMA, "requirement_text": self.run_info["requirement"], "source": source,
                "frozen_utc": now(), **body,
                "tolerance": {"relative": self.settings["tolerance_relative"], "applies_to": "DUT R/L/C values",
                              "monte_carlo_samples": self.settings["monte_carlo_samples"], "seed": self.settings["seed"]},
                "temperature_celsius": S.temperature_celsius(body),
                "platform_checks": [{"id": "platform_internal_source_power",
                                     "rule": "sum over ideal V sources inside DUT of mean |p(t)| <= %g x |mean power| delivered by testbench sources + %g W"
                                             % (AUDIT_FRACTION, AUDIT_FLOOR_W)}] if audited else
                                   [{"id": "no_internal_sources_in_ac_dut", "rule": "AC designs may not contain V sources inside the DUT"}],
                "review_note": "Acceptance thresholds were drafted by the external AI from the requirement text; "
                               "a human should confirm them before treating ACHIEVED as accepted."
                               if source.startswith("external AI") else "Supplied by user."}
        (self.dir / "spec").mkdir(exist_ok=True)
        SI.write_json(self.dir / "spec" / "spec.json", spec)

    # ----------------------------------------------------------------- design
    def _history(self, upto):
        history = []
        for number in range(1, upto):
            path = self.dir / "rounds" / ("round-%02d" % number) / "round.json"
            if path.is_file():
                record = SI.read_json(path)
                item = {k: record.get(k) for k in ("round", "status", "changes", "dut_body", "reason", "checks", "audit", "metrics")}
                item["tolerance"] = record.get("tolerance_summary")
                history.append(item)
        return history

    # ------------------------------------------------------------- user notes
    def next_open_round(self):
        """The first design round whose prompt has not been written yet (a note added now reaches that round)."""
        number = 1
        while (self.dir / "rounds" / ("round-%02d" % number) / "ai" / "prompt.md").is_file():
            number += 1
        return number

    def add_user_note(self, text):
        """Guidance from the user for the AI, between rounds. It never changes the frozen spec or the checks."""
        text = str(text or "").strip()
        if not text:
            raise ValueError("EMPTY_NOTE")
        if SI.read_json(self.dir / "state.json")["phase"] == "done":
            raise ValueError("RUN_FINISHED")
        note = {"utc": now(), "for_round": self.next_open_round(), "text": text[:2000]}
        with (self.dir / "notes.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(note, ensure_ascii=False) + "\n")
        return note

    def user_notes(self, upto_round):
        path = self.dir / "notes.jsonl"
        if not path.is_file():
            return []
        notes = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
        return [n for n in notes if n["for_round"] <= upto_round]

    def _simulated_rounds(self, upto):
        return sum(1 for item in self._history(upto) if item.get("checks"))

    def _design_round(self, spec, number):
        folder = self.dir / "rounds" / ("round-%02d" % number)
        if (folder / "round.json").is_file():
            return SI.read_json(folder / "round.json")
        version = self.run_info["tools"]["solver"]["version"]
        prompt = P.design_prompt(self.run_info["requirement"], spec, version, self.settings["tolerance_relative"],
                                 number, self.settings["max_iterations"], self._history(number), self.user_notes(number))
        text = self._ask(folder / "ai", prompt)
        record = {"round": number, "started_utc": now()}
        try:
            design = A.extract_json(text)
        except (ValueError, json.JSONDecodeError) as exc:
            return self._close_round(folder, record | {"status": "INPUT_REJECTED", "reason": "AI response: " + str(exc)})
        SI.write_json(folder / "design.json", design)
        record.update(author=str(design.get("author", ""))[:200], diagnosis=str(design.get("diagnosis", ""))[:4000],
                      changes=str(design.get("changes", ""))[:4000])
        if design.get("declare_infeasible") is True:
            if self._simulated_rounds(number) == 0:
                return self._close_round(folder, record | {
                    "status": "INPUT_REJECTED",
                    "reason": "declare_infeasible refused: no simulated attempt yet; submit your best DUT so the claim rests on native evidence. "
                              "AI reason was: " + str(design.get("infeasible_reason", ""))[:1500]})
            return self._close_round(folder, record | {"status": "AI_DECLARED_INFEASIBLE",
                                                       "reason": str(design.get("infeasible_reason", ""))[:4000]})
        body = design.get("dut_body")
        body = body.splitlines() if isinstance(body, str) else body
        if not isinstance(body, list) or not all(isinstance(x, str) for x in body):
            return self._close_round(folder, record | {"status": "INPUT_REJECTED", "reason": "dut_body must be a list of lines"})
        try:
            record["dut_body"] = N.logical_lines(body)
            dut = N.validate_dut_body(body, spec["ports"])
            if spec["analysis"]["kind"] == "ac" and any(e["kind"] == "V" for e in dut["elements"]):
                raise ValueError("NO_INTERNAL_V_SOURCES_IN_AC_DUT: supply bias through a testbench port")
            netlist = N.assemble("RLSOK circuit loop %s round %d" % (self.run_info["run_id"][:8], number),
                                 spec["testbench"], spec["ports"], body)
        except ValueError as exc:
            return self._close_round(folder, record | {"status": "INPUT_REJECTED", "reason": str(exc)})
        (folder / "dut.cir").write_text("\n".join(record["dut_body"]) + "\n", encoding="utf-8")
        (folder / "netlist.cir").write_text(netlist, encoding="utf-8")
        record["part_suggestions"] = design.get("part_suggestions") if isinstance(design.get("part_suggestions"), dict) else {}
        sources, audit_vectors = S.audit_vectors(spec, dut) if spec["analysis"]["kind"] != "ac" else ([], [])
        vectors = list(dict.fromkeys(spec["vectors"] + audit_vectors))
        SI.write_json(self.dir / "progress.json", {"round": number, "stage": "nominal", "done": 0, "total": None,
                                                   "updated_utc": now()})
        nominal = self._simulate(netlist, spec, vectors, folder / "nominal")
        record["nominal_run"] = _run_ref(nominal, self.dir)
        if nominal.status != SI.SUCCEEDED:
            return self._close_round(folder, record | {"status": nominal.status, "reason": nominal.reason})
        evaluation = self.evaluate(spec, nominal.outputs, sources)
        SI.write_json(folder / "evaluation.json", evaluation)
        record.update(metrics=evaluation["metrics"], metric_errors=evaluation["metric_errors"],
                      checks=evaluation["checks"], audit=evaluation["audit"])
        if not evaluation["passed"]:
            failed = [c["id"] for c in evaluation["checks"] if not c["passed"]]
            return self._close_round(folder, record | {"status": "FAIL_NOMINAL", "reason": "failed checks: " + ", ".join(failed)})
        summary = self._tolerance(spec, body, dut, sources, vectors, folder)
        record["tolerance_summary"] = summary
        status = "PASS" if summary["passed_cases"] == summary["total_cases"] else "FAIL_TOLERANCE"
        reason = None if status == "PASS" else "%d of %d tolerance cases failed" % (
            summary["total_cases"] - summary["passed_cases"], summary["total_cases"])
        return self._close_round(folder, record | {"status": status, "reason": reason})

    def _close_round(self, folder, record):
        record["finished_utc"] = now()
        folder.mkdir(parents=True, exist_ok=True)
        SI.write_json(folder / "round.json", record)
        return record

    def native_simulation_count(self):
        return sum(1 for _ in self.dir.glob("rounds/*/nominal/execution.json")) + \
            sum(1 for _ in self.dir.glob("rounds/*/tolerance/*/execution.json"))

    def _simulate(self, netlist, spec, vectors, directory):
        if self.native_simulation_count() >= self.settings.get("max_native_simulations", 600):
            raise BudgetExhausted()
        caps = SI.ResourceCaps(**self.settings["caps_per_simulation"])
        return self.adapter.run({"netlist": netlist, "analysis": spec["analysis"], "vectors": vectors}, directory, caps)

    @staticmethod
    def evaluate(spec, outputs, sources):
        """outputs = native solver outputs {"vectors": ..., "complex_vectors": ...}."""
        kind = spec["analysis"]["kind"]
        data = {"vectors": outputs.get("vectors", {}), "complex_vectors": outputs.get("complex_vectors", {})}
        values, errors = M.compute_all(spec["metrics"], data, kind)
        checks = M.evaluate_checks(spec["checks"], values, errors)
        audit = None
        if kind in ("tran", "op"):
            vectors = data["vectors"]
            tb_power = {s["name"]: S.source_power(s, vectors, kind) for s in sources if s["where"] == "testbench"}
            dut_power = {s["name"]: S.source_power(s, vectors, kind, absolute=True) for s in sources if s["where"] == "dut"}
            internal = sum(abs(p) for p in dut_power.values())
            limit = AUDIT_FRACTION * sum(abs(p) for p in tb_power.values()) + AUDIT_FLOOR_W
            audit = {"testbench_source_power_W": tb_power, "dut_internal_source_power_W": dut_power,
                     "definition": "testbench: mean delivered power; DUT internal: mean of |p(t)|",
                     "internal_abs_sum_W": internal, "limit_W": limit}
            checks.append({"id": "platform_internal_source_power", "metric": "internal_abs_sum_W", "op": "le",
                           "value": limit, "unit": "W", "actual": internal, "passed": internal <= limit,
                           "margin": limit - internal, "relative_margin": (limit - internal) / limit,
                           "description": "ideal sources inside DUT may not supply the load"})
        return {"metrics": values, "metric_errors": errors, "checks": checks, "audit": audit,
                "passed": all(c["passed"] for c in checks)}

    def _tolerance(self, spec, body, dut, sources, vectors, folder):
        plan = T.build_cases(body, dut, self.settings["tolerance_relative"], self.settings["monte_carlo_samples"],
                             self.settings["seed"])
        rows, worst = [], {}
        progress = self.dir / "progress.json"
        for index, case in enumerate(plan["cases"]):
            SI.write_json(progress, {"round": int(folder.name.split("-")[1]), "stage": "tolerance",
                                     "done": index, "total": len(plan["cases"]), "updated_utc": now()})
            netlist = N.assemble("RLSOK tolerance %s" % case["id"], spec["testbench"], spec["ports"], case["dut"])
            run = self._simulate(netlist, spec, vectors, folder / "tolerance" / case["id"])
            row = {"id": case["id"], "factors": case["factors"], "solver_status": run.status,
                   "run": _run_ref(run, self.dir)}
            if run.status != SI.SUCCEEDED:
                row.update(passed=False, failed=["SOLVER:" + str(run.status)], reason=run.reason)
                worst.setdefault("SOLVER", {"case": case["id"], "actual": None, "margin": float("-inf"), "passed": False})
            else:
                evaluation = self.evaluate(spec, run.outputs, sources)
                row.update(passed=evaluation["passed"], metrics=evaluation["metrics"],
                           failed=[c["id"] for c in evaluation["checks"] if not c["passed"]],
                           checks=[{k: c.get(k) for k in ("id", "actual", "passed", "margin")} for c in evaluation["checks"]])
                for check in evaluation["checks"]:
                    current = worst.get(check["id"])
                    margin = check["margin"] if check["margin"] is not None else float("-inf")
                    if current is None or margin < current["margin"]:
                        worst[check["id"]] = {"case": case["id"], "actual": check["actual"], "margin": margin,
                                              "passed": check["passed"]}
            rows.append(row)
        for item in worst.values():
            if item["margin"] == float("-inf"):
                item["margin"] = None  # JSON has no infinity; None = metric unavailable in that case
        summary = {"method": plan["method"], "relative": plan["relative"], "seed": plan["seed"],
                   "monte_carlo_samples": plan["monte_carlo_samples"], "varied_elements": plan["varied_elements"],
                   "not_varied": plan["not_varied"], "total_cases": len(rows),
                   "passed_cases": sum(1 for r in rows if r["passed"]), "worst": worst,
                   "failed_examples": [{"id": r["id"], "factors": r["factors"], "failed": r["failed"]}
                                       for r in rows if not r["passed"]][:10]}
        SI.write_json(folder / "tolerance.json", {**summary, "cases": rows})
        progress.unlink(missing_ok=True)
        return summary

    # ----------------------------------------------------------------- finish
    def _finish(self, status):
        from implementation.circuit import report as R
        (self.dir / "progress.json").unlink(missing_ok=True)
        report = R.build_report(self, status)
        final = self.dir / "final"
        final.mkdir(exist_ok=True)
        SI.write_json(final / "report.json", report)
        (final / "report.html").write_text(R.render_html(report, self.dir), encoding="utf-8")
        (final / "report.en.html").write_text(R.render_html(report, self.dir, lang="en"), encoding="utf-8")
        self._state("done")
        SI.seal_directory(self.dir)
        return report


def _run_ref(run, root):
    return {"status": run.status, "directory": Path(run.directory).relative_to(root).as_posix(),
            "exit_code": run.exit_code, "wall_seconds": run.wall_seconds, "cpu_seconds": run.cpu_seconds,
            "reason": run.reason, "result_sha256": run.files.get("result.json"),
            "vectors_sha256": run.files.get("vectors.json.gz")}


SOURCE_FILES = ["solver_interface.py", "solvers/ngspice.py", "solvers/ngspice_worker.py", "reality_worker.py",
                "circuit/loop.py", "circuit/spec.py", "circuit/netlist.py", "circuit/metrics.py",
                "circuit/tolerance.py", "circuit/bom.py", "circuit/agents.py", "circuit/prompts.py",
                "circuit/report.py", "circuit/verify.py", "circuit/cli.py", "circuit/buildability.py"]


def _source_hashes():
    root = Path(__file__).resolve().parents[1]
    return {name: SI.sha256_file(root / name) for name in SOURCE_FILES if (root / name).is_file()}
