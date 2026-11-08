"""Cross-domain native simulation entry using existing resource controls.

This is a solver bridge, not a new physics engine or an engineering approval.
Existing GPU/CAD/PCB ledgers and completion contracts are not modified.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "toolchain-cache" / "reality-engines.lock.json"
STATUS = ROOT / "toolchain-cache" / "reality-runtime-status.json"
WORKER = ROOT / "implementation" / "reality_worker.py"


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1048576), b""):
            digest.update(block)
    return digest.hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")


def relative(name):
    path = PurePosixPath(name)
    if not isinstance(name, str) or not name or "\\" in name or ":" in name or path.is_absolute() or str(path) != name or any(part in (".", "..") for part in path.parts):
        raise ValueError("PROJECT_RELATIVE_PATH_REQUIRED")
    return name


def number(value, minimum, maximum):
    if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError("FINITE_BOUNDED_NUMBER_REQUIRED")
    return value


def tool(engine):
    lock = read(LOCK)
    if engine not in lock["engines"]:
        raise ValueError("SOLVER_NOT_INTEGRATED: " + str(engine))
    runtime = lock["engines"][engine]
    for path, digest in {**lock["common_files"], **runtime["files"]}.items():
        if not Path(path).is_file() or sha(path) != digest:
            raise ValueError("TOOL_IDENTITY_CHANGED: " + path)
    return runtime, lock


def runtime_status(engine):
    """Recorded execution evidence, never infer operability from installation."""
    item = read(STATUS).get(engine, {}) if STATUS.is_file() else {}
    if not item:
        return {"state": "NOT_VALIDATED", "reason": "No native execution evidence"}
    for binding in item.get("evidence", []):
        if not Path(binding["path"]).is_file() or sha(binding["path"]) != binding["sha256"]:
            raise ValueError("RUNTIME_EVIDENCE_CHANGED")
    return item


def engines():
    rows = []
    definitions = {"mujoco": ("mechanical-dynamics", ["rigid bodies", "joints", "contact forces", "trajectory"]),
                   "ngspice": ("circuit-dynamics", ["SPICE operating point", "transient voltage/current", "waveforms"])}
    for engine, (domain, capabilities) in definitions.items():
        try:
            runtime, _ = tool(engine)
            status = runtime_status(engine)
            state, reason, version = status["state"], status.get("reason"), runtime.get("version", "READ_FROM_NATIVE_RUN")
        except (OSError, ValueError, KeyError) as error:
            state, reason, version = "UNAVAILABLE", str(error), None
        row = {"engine": engine, "domain": domain, "state": state, "version": version,
               "capabilities": capabilities, "reason": reason,
               "backends": {"windows": {"state": state, "reason": reason}}}
        if engine == "mujoco":
            linux = runtime_status("mujoco_linux")
            row["backends"]["linux_workflow"] = linux
            if linux["state"] == "AVAILABLE_SCOPED":
                row.update(state="AVAILABLE_SCOPED", version=linux["version"],
                           reason=linux.get("reason"),
                           session_schema="rlsok.reality-linux-session/1")
        rows.append(row)
    coupling = runtime_status("electromechanical_coupling")
    return {"schema": "rlsok.reality-engines/1", "engines": rows,
            "existing_routes": {"GPU": "existing atoms/engineering CLI", "CAD geometry": "existing CAD engineering adapter",
                                "PCB rules and fabrication files": "atoms.ps1 pcb-run"},
            "scope": "Extensible platform; availability is not engineering acceptance or support for every domain.",
            "capability_gaps": {"structural_FEA": "NOT_INTEGRATED", "aerodynamics_CFD": "NOT_INTEGRATED",
                                "thermal_analysis": "NOT_INTEGRATED", "manufacturing_tolerances": "NOT_INTEGRATED",
                                "electromechanical_coupling": coupling["state"]},
            "coupling_evidence": coupling,
            "request_contract": {"inputs": ["project.entry", "project.files", "parameters", "checks", "resources", "scope"],
                                 "engineering_parameters": "Declare material and operating-condition files in project.files; only solver-supported models are accepted.",
                                 "native_results": ["numeric metrics", "time series", "failed checks", "model assumptions", "sealed execution receipts"],
                                 "unsupported_physics": "NOT_INTEGRATED; never PASS without a model and actual tool evidence"},
            "execution_launched": 0}


def validate(request):
    if set(request) != {"engine", "scope", "project", "parameters", "checks", "resources"}:
        raise ValueError("EXACT_SIMULATION_REQUEST_SCHEMA_REQUIRED")
    if request["engine"] not in ("mujoco", "ngspice"):
        raise ValueError("SOLVER_NOT_INTEGRATED")
    if not isinstance(request["scope"], str) or not request["scope"].strip():
        raise ValueError("MEASURABLE_SCOPE_REQUIRED")
    project = request["project"]
    if set(project) != {"directory", "entry", "files"} or not isinstance(project["files"], list) or not 1 <= len(project["files"]) <= 128:
        raise ValueError("EXPLICIT_PROJECT_FILES_REQUIRED")
    if len(set(project["files"])) != len(project["files"]) or project["entry"] not in project["files"]:
        raise ValueError("UNIQUE_FILES_AND_DECLARED_ENTRY_REQUIRED")
    for name in project["files"]:
        relative(name)
    cap = request["resources"]
    if set(cap) != {"cpu_seconds", "wall_seconds", "memory_bytes", "disk_bytes"}:
        raise ValueError("EXPLICIT_COMPLETE_RUN_CAPS_REQUIRED")
    number(cap["cpu_seconds"], .1, 300)
    number(cap["wall_seconds"], .1, 300)
    number(cap["memory_bytes"], 67108864, 4294967296)
    number(cap["disk_bytes"], 1048576, 1073741824)
    if type(cap["memory_bytes"]) is not int or type(cap["disk_bytes"]) is not int:
        raise ValueError("INTEGER_BYTE_CAPS_REQUIRED")
    checks = request["checks"]
    if not isinstance(checks, list) or not 1 <= len(checks) <= 64:
        raise ValueError("EXPLICIT_MEASURED_CHECKS_REQUIRED")
    for check in checks:
        if set(check) != {"metric", "op", "value"} or not isinstance(check["metric"], str) or not check["metric"] or check["op"] not in ("eq", "ge", "le"):
            raise ValueError("INVALID_MEASURED_CHECK")
        number(check["value"], -1e100, 1e100)
    params = request["parameters"]
    if request["engine"] == "mujoco":
        if set(params) - {"steps", "sample_every", "bodies", "joints", "controls", "torque_schedule_file", "render"} or not {"steps", "sample_every", "bodies"} <= set(params):
            raise ValueError("MJCF_STEPS_SAMPLING_BODIES_REQUIRED")
        for key in ("steps", "sample_every"):
            if type(params[key]) is not int or not 1 <= params[key] <= 100000:
                raise ValueError("BOUNDED_INTEGER_STEPS_REQUIRED")
        if params["steps"] // params["sample_every"] > 10000:
            raise ValueError("TRAJECTORY_SAMPLE_LIMIT")
        if not isinstance(params["bodies"], list) or not 1 <= len(params["bodies"]) <= 64 or any(not isinstance(n, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", n) for n in params["bodies"]):
            raise ValueError("NAMED_BODY_LIST_REQUIRED")
        if "render" in params and type(params["render"]) is not bool:
            raise ValueError("BOOLEAN_RENDER_REQUIRED")
        controls = params.get("controls", [])
        if not isinstance(controls, list) or len(controls) > 4096:
            raise ValueError("BOUNDED_CONSTANT_CONTROLS_REQUIRED")
        for value in controls:
            number(value, -1e9, 1e9)
        if "joints" in params and (not isinstance(params["joints"], list) or len(params["joints"]) > 64 or any(not isinstance(n, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", n) for n in params["joints"])):
            raise ValueError("NAMED_JOINT_LIST_REQUIRED")
        if "torque_schedule_file" in params:
            name = relative(params["torque_schedule_file"])
            if name not in project["files"] or "controls" in params:
                raise ValueError("DECLARED_TORQUE_SCHEDULE_WITHOUT_CONSTANT_CONTROLS_REQUIRED")
    else:
        if set(params) != {"analysis", "vectors"} or not isinstance(params["vectors"], list) or not 1 <= len(params["vectors"]) <= 64:
            raise ValueError("SPICE_ANALYSIS_AND_VECTORS_REQUIRED")
        if any(not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9_().#-]{1,96}", v) for v in params["vectors"]):
            raise ValueError("INVALID_SPICE_VECTOR")
        analysis = params["analysis"]
        if analysis == {"kind": "op"}:
            return
        if set(analysis) != {"kind", "step_seconds", "stop_seconds"} or analysis["kind"] != "transient":
            raise ValueError("OP_OR_TRANSIENT_REQUIRED")
        step = number(analysis["step_seconds"], 1e-12, 1e6)
        stop = number(analysis["stop_seconds"], step, 1e6)
        if stop / step > 10000:
            raise ValueError("TRANSIENT_POINT_LIMIT")


def run(request_path, output, session_path=None):
    """Run through the existing Stage ledger and confirmed Completion identity."""
    if session_path is None:
        raise ValueError("CONFIRMED_COMPLETION_AND_AUTHORIZED_SESSION_REQUIRED")
    session_kind = read(session_path).get("schema")
    if session_kind == "rlsok.reality-linux-session/1":
        from implementation.reality_linux import windows_run, linux_run
        import os
        return windows_run(request_path, output, session_path) if os.name == "nt" else linux_run(request_path, output, session_path)
    started, cpu_start = time.monotonic(), time.process_time()
    request_path, output = Path(request_path).resolve(), Path(output).resolve()
    request = read(request_path)
    validate(request)
    status = runtime_status(request["engine"])
    if status["state"] == "BLOCKED":
        raise ValueError(status["reason"])
    runtime, lock = tool(request["engine"])
    sys.path.insert(0, str(ROOT))
    from implementation.completion import Completion, bound_file, safe_name
    session = read(session_path)
    if set(session) != {"schema", "base", "engine_root", "completion_directory", "goal", "version", "design", "requirement", "stage", "label", "worker"} or session["schema"] != "rlsok.reality-session/1":
        raise ValueError("EXACT_EXISTING_ENGINE_SESSION_REQUIRED")
    safe_name(session["stage"])
    safe_name(session["label"])
    engine = Path(session["engine_root"]).resolve()
    if output != engine / "jobs" / session["label"]:
        raise ValueError("OUTPUT_MUST_MATCH_EXISTING_STAGE_JOB")
    worker = bound_file(session["worker"])
    service = Completion(session["completion_directory"])
    try:
        decision = service.evaluate(session["goal"], session["version"], session["design"])
        if decision["decision"] == "DONE" or decision["execution_state"] in ("PAUSED", "BLOCKED"):
            return {"status": "AUTOMATIC_STOP", "launched": 0, "completion": decision}
        row, contract = service.contract(session["goal"], session["version"])
        if not row["confirmation"] or decision["superseded_contract"]:
            raise ValueError("CURRENT_CONFIRMED_CONTRACT_REQUIRED")
        service.approval_chain(session["goal"], session["version"])
        requirement = next((r for r in contract["requirements"] if r["id"] == session["requirement"]), None)
        if requirement is None:
            raise ValueError("REQUIREMENT_NOT_IN_CONFIRMED_CONTRACT")
        design = service.design(session["goal"], session["design"])
        if requirement["kind"] != "engine_artifact" or requirement["tools"].get(str(worker)) != session["worker"]["sha256"]:
            raise ValueError("FROZEN_SOLVER_WORKER_REQUIRED")
    finally:
        service.db.close()
    project = Path(request["project"]["directory"])
    project = (request_path.parent / project).resolve() if not project.is_absolute() else project.resolve()
    if output.is_relative_to(project):
        raise ValueError("RUN_OUTPUT_MUST_BE_OUTSIDE_SOURCE_PROJECT")
    sources, total = {}, 0
    for name in request["project"]["files"]:
        path = project / name
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(project):
            raise ValueError("DECLARED_REGULAR_PROJECT_FILE_REQUIRED: " + name)
        total += path.stat().st_size
        sources[name] = {"path": str(path), "sha256": sha(path)}
    if total > min(request["resources"]["disk_bytes"] // 2, 67108864):
        raise ValueError("INPUT_FILES_EXCEED_RUN_CAP")
    request_binding = {"path": str(request_path), "sha256": sha(request_path)}
    required_bindings = [design[name] for name in requirement["inputs"]]
    if any(binding not in required_bindings for binding in [*sources.values(), request_binding]):
        raise ValueError("COMPLETE_DESIGN_AND_REQUEST_BINDINGS_REQUIRED")
    if any(binding not in [*sources.values(), request_binding] for binding in required_bindings):
        raise ValueError("REQUIREMENT_INPUTS_MUST_MATCH_RUN")
    # Preflight data-only validation happens before starting a native solver.
    from implementation.reality_worker import validate_mjcf, validate_spice
    entry = project / request["project"]["entry"]
    if request["engine"] == "mujoco":
        validate_mjcf(entry.read_text(encoding="utf-8-sig"), project)
    else:
        validate_spice(entry.read_text(encoding="utf-8-sig"))
    tools = {**lock["common_files"], **runtime["files"], str(worker): sha(worker),
             str(ROOT / "implementation/pcb_cli.py"): sha(ROOT / "implementation/pcb_cli.py"),
             str(Path(__file__).resolve()): sha(__file__), str(LOCK): sha(LOCK)}
    for path, digest in requirement["tools"].items():
        if tools.get(path) != digest:
            raise ValueError("CONTRACT_TOOL_IDENTITY_MISMATCH")
        bound_file({"path": path, "sha256": digest})
    if requirement["tools"].get(runtime["library"]) != tools[runtime["library"]]:
        raise ValueError("FROZEN_NATIVE_SOLVER_REQUIRED")
    from implementation.pcb_cli import Stage
    cap = request["resources"]
    stage = Stage(session["stage"], session["label"], {"cpu": cap["cpu_seconds"], "wall": cap["wall_seconds"]},
                  {Path(b["path"]): b["sha256"] for b in [*sources.values(), request_binding]},
                  {Path(p): h for p, h in tools.items()}, command="reality-run", engine=engine,
                  base=Path(session["base"]), recipe=worker, memory_cap=cap["memory_bytes"],
                  disk_cap=cap["disk_bytes"], path_formatter=lambda p: str(Path(p).resolve()),
                  result_updater=lambda _stage, _receipt: None)
    stage.begin()
    try:
        snapshot = output / "project"
        snapshot.mkdir()
        for name, binding in sources.items():
            destination = snapshot / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(binding["path"], destination)
            if sha(destination) != binding["sha256"]:
                raise ValueError("SOURCE_CHANGED_DURING_SNAPSHOT")
        invocation = {**request, "runtime": runtime}
        invocation_path = output / "invocation.json"
        write(invocation_path, invocation)
        manifest = {"schema": "rlsok.reality-run/2", "request": request, "inputs": sources, "tools": tools,
                    "request_source": request_binding, "session": session,
                    "worker": session["worker"], "python": lock["python"],
                    "engine_library": runtime["library"],
                    "invocation_sha256": sha(invocation_path), "run_scope": "Existing Stage reservation and Completion identity; independent authorized session only.",
                    "isolation_scope": "Windows Job Object resources/process tree, allowlisted environment and validated data-only inputs. No filesystem/network isolation claim.",
                    "acceptance_scope": "Declared simulation checks only; completion contract is separate."}
        write(output / "run.json", manifest)
        argv = [lock["python"], "-I", "-B", str(stage.recipe), str(invocation_path), str(snapshot)]
        import os
        environment = {key: os.environ[key] for key in ("SystemRoot", "WINDIR") if key in os.environ}
        temp = output / "temp"
        temp.mkdir()
        environment.update({"TEMP": str(temp), "TMP": str(temp), "HOME": str(temp), "USERPROFILE": str(temp),
                            "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"})
        try:
            native_execution = stage.call(argv, "native", environment=environment)
        except Exception as error:
            write(output / "controller-error.json", {"status": "EVIDENCE_INSUFFICIENT", "reason": str(error),
                                                    "resource_cost": "UNKNOWN", "no_automatic_retry": True})
            stage.finish(1, str(error), "TOOL_OR_RESOURCE_ERROR")
            raise
        result_path = output / "native/result.json"
        result = read(result_path) if result_path.is_file() else {"engineering_acceptance": "EVIDENCE_INSUFFICIENT", "reason": "NO_NATIVE_RESULT"}
        if native_execution["limit_event"] or not native_execution["all_job_processes_quiescent"] or native_execution["exit_code"] not in (0, 2):
            result = {**result, "engineering_acceptance": "EVIDENCE_INSUFFICIENT",
                      "failure_kind": native_execution["limit_event"] or "TOOL_OR_MODEL_ERROR"}
        write(output / "result.json", result)
        for name in ("vectors.json", "trajectory.json", "model.json"):
            source = output / "native" / name
            if source.is_file():
                shutil.copyfile(source, output / name)
        execution = stage.finish(native_execution["exit_code"], result.get("reason"), result.get("failure_kind"))
        seals = {str(path.relative_to(output)).replace("\\", "/"): sha(path) for path in output.rglob("*") if path.is_file()}
        write(output / "files.json", {"schema": "rlsok.reality-files/1", "files": seals})
        return {"output": str(output), "result": result, "execution": execution,
                "status": "EXECUTION_COMPLETE" if execution["state"] == "SUCCEEDED" else "EXECUTION_FAILED",
                "files_sha256": sha(output / "files.json")}
    except BaseException as error:
        ledger = read(engine / "resources.json")
        if ledger["stages"][session["stage"]]["state"] == "RUNNING":
            write(output / "controller-error.json", {"status": "EVIDENCE_INSUFFICIENT", "reason": str(error),
                                                    "native_calls_returned": len(stage.calls),
                                                    "accounting_unknown": stage.accounting_unknown, "no_automatic_retry": True})
            stage.finish(1, str(error), "PREPARATION_OR_TOOL_ERROR")
        raise



def verify(directory):
    directory = Path(directory).resolve()
    if (directory / "run.json").is_file() and read(directory / "run.json").get("schema") == "rlsok.reality-analysis-run/1":
        from implementation.reality_analysis import verify_analysis
        return verify_analysis(directory)
    if (directory / "run.json").is_file() and read(directory / "run.json").get("schema") == "rlsok.reality-linux-run/1":
        from implementation.reality_linux import verify_linux
        return verify_linux(directory)
    index = read(directory / "files.json")
    required = {"run.json", "invocation.json", "execution.json", "result.json", "native/result.json", "native/stdout.log", "native/stderr.log"}
    actual_files = {path.relative_to(directory).as_posix() for path in directory.rglob("*") if path.is_file() and path != directory / "files.json"}
    if index.get("schema") != "rlsok.reality-files/1" or not required <= set(index["files"]) or set(index["files"]) != actual_files:
        raise ValueError("INCOMPLETE_RUN_FILE_INDEX")
    for name, digest in index["files"].items():
        relative(name)
        path = directory / name
        if path.is_symlink() or not path.resolve().is_relative_to(directory) or sha(path) != digest:
            raise ValueError("RESULT_FILE_CHANGED: " + name)
    manifest, execution, result = (read(directory / name) for name in ("run.json", "execution.json", "result.json"))
    if manifest["schema"] == "rlsok.reality-run/2":
        return verify_stage_run(directory, manifest, execution, result)
    if manifest["schema"] != "rlsok.reality-run/1" or execution["limit_event"] or not execution["all_job_processes_quiescent"]:
        raise ValueError("INCOMPLETE_OR_LIMITED_EXECUTION")
    validate(manifest["request"])
    native = read(directory / "native/result.json")
    if result != native or type(execution["exit_code"]) is not int:
        raise ValueError("NATIVE_RESULT_OR_EXIT_MISMATCH")
    expected_exit = {"PASS_SCOPED_SIMULATION": 0, "FAIL": 2, "EVIDENCE_INSUFFICIENT": 1}.get(result.get("engineering_acceptance"))
    if expected_exit != execution["exit_code"]:
        raise ValueError("NATIVE_RESULT_OR_EXIT_MISMATCH")
    if sha(directory / "invocation.json") != manifest["invocation_sha256"] or execution["caps"] != manifest["request"]["resources"]:
        raise ValueError("INVOCATION_IDENTITY_MISMATCH")
    if result.get("engineering_acceptance") in ("PASS_SCOPED_SIMULATION", "FAIL"):
        if result.get("engine") != manifest["request"]["engine"] or result.get("scope") != manifest["request"]["scope"]:
            raise ValueError("RESULT_SCOPE_OR_ENGINE_MISMATCH")
        measured = result.get("checks", [])
        if len(measured) != len(manifest["request"]["checks"]):
            raise ValueError("MISSING_MEASURED_CHECK")
        for declared, observed in zip(manifest["request"]["checks"], measured):
            metric = result["metrics"][declared["metric"]]
            number(metric, -1e100, 1e100)
            passed = metric >= declared["value"] if declared["op"] == "ge" else metric <= declared["value"] if declared["op"] == "le" else metric == declared["value"]
            if observed != {**declared, "actual": metric, "passed": passed}:
                raise ValueError("MEASURED_CHECK_CHANGED")
        if (result["engineering_acceptance"] == "PASS_SCOPED_SIMULATION") != all(item["passed"] for item in measured):
            raise ValueError("CHECK_STATUS_MISMATCH")
    for name, binding in manifest["inputs"].items():
        if sha(directory / "project" / relative(name)) != binding["sha256"]:
            raise ValueError("DESIGN_SNAPSHOT_CHANGED")
    return {"status": "FILES_AND_NATIVE_RECEIPT_VERIFIED", "engineering_acceptance": result["engineering_acceptance"],
            "scope": manifest["request"]["scope"], "user_goal_achieved": False, "files_sha256": sha(directory / "files.json")}


def verify_stage_run(directory, manifest, execution, result):
    """Verify the existing provider ledger, actual child receipt and raw numbers."""
    from implementation.completion import digest
    request, session = manifest["request"], manifest["session"]
    validate(request)
    engine = Path(session["engine_root"])
    descriptor = read(engine / "cli-plans" / (session["stage"] + ".json"))
    spec = descriptor["spec"]
    if descriptor["descriptor_sha256"] != digest({k: v for k, v in descriptor.items() if k != "descriptor_sha256"}) or descriptor["workflow_plan_sha256"] != sha(engine / "plan.json"):
        raise ValueError("ENGINE_PLAN_OR_DESCRIPTOR_CHANGED")
    plan = read(engine / "plan.json")
    if plan["matrix_sha256"] != sha(engine / "matrix.json") or plan["authorization_sha256"] != sha(Path(session["base"]) / "authorization.json"):
        raise ValueError("ENGINE_AUTHORIZATION_OR_MATRIX_CHANGED")
    stage = read(engine / "resources.json")["stages"][session["stage"]]
    cap = {"cpu": request["resources"]["cpu_seconds"], "wall": request["resources"]["wall_seconds"]}
    if stage["cap"] != cap or spec["stage_cap"] != cap or len(spec["jobs"]) != 1 or spec["jobs"][0]["cap"] != cap or spec["stage"] != session["stage"]:
        raise ValueError("FROZEN_REQUEST_CAP_MISMATCH")
    if stage["state"] != "SETTLED" or stage["cli_plan_sha256"] != descriptor["descriptor_sha256"] or len(stage["jobs"]) != 1:
        raise ValueError("UNSETTLED_ENGINE_STAGE")
    if stage["jobs"][0]["receipt_sha256"] != sha(directory / "execution.json") or stage["jobs"][0]["label"] != session["label"]:
        raise ValueError("STAGE_RECEIPT_IDENTITY_MISMATCH")
    calls = execution["actual_native_calls"]
    if len(calls) != 1 or not execution["all_job_processes_quiescent"]:
        raise ValueError("ONE_QUIESCENT_NATIVE_CALL_REQUIRED")
    native = calls[0]
    job = spec["jobs"][0]
    expected_binding = {"stage": session["stage"], "label": session["label"], "cap": spec["stage_cap"], "payload_sha256": job["payload_sha256"]}
    expected_argv = [manifest["python"], "-I", "-B", job["payload"], str(directory / "invocation.json"), str(directory / "project")]
    if execution["execution_binding"] != expected_binding or native["argv"] != expected_argv or native["limit_event"] or not native["all_job_processes_quiescent"]:
        raise ValueError("NATIVE_EXECUTION_BINDING_MISMATCH")
    if stage["jobs"][0]["cost"] != execution["controller_cost"] or stage["jobs"][0]["state"] != execution["state"]:
        raise ValueError("LEDGER_COST_OR_STATE_MISMATCH")
    if job["payload_sha256"] != sha(directory / "verification-recipe.py") or job["payload_sha256"] != manifest["worker"]["sha256"]:
        raise ValueError("FROZEN_WORKER_IDENTITY_MISMATCH")
    expected_tools = {p: h for p, h in manifest["tools"].items() if p != manifest["worker"]["path"]}
    expected_tools[job["payload"]] = job["payload_sha256"]
    if spec["identity_hashes"] != expected_tools:
        raise ValueError("STAGE_TOOL_IDENTITY_MISMATCH")
    for name, hash_value in execution["artifact_sha256"].items():
        if sha(directory / relative(name)) != hash_value:
            raise ValueError("STAGE_ARTIFACT_CHANGED")
    invocation = read(directory / "invocation.json")
    if sha(directory / "invocation.json") != manifest["invocation_sha256"] or {k: v for k, v in invocation.items() if k != "runtime"} != request:
        raise ValueError("INVOCATION_IDENTITY_MISMATCH")
    if invocation["runtime"]["library"] != manifest["engine_library"] or any(manifest["tools"].get(p) != h for p, h in invocation["runtime"]["files"].items()):
        raise ValueError("NATIVE_RUNTIME_BINDING_MISMATCH")
    binds = {b["source"]: b["sha256"] for b in job["binds"]}
    if set(manifest["inputs"]) != set(request["project"]["files"]):
        raise ValueError("DESIGN_FILE_SET_MISMATCH")
    for name, binding in manifest["inputs"].items():
        if sha(directory / "project" / relative(name)) != binding["sha256"] or binds.get(binding["path"]) != binding["sha256"]:
            raise ValueError("DESIGN_SNAPSHOT_CHANGED")
    if binds.get(manifest["request_source"]["path"]) != manifest["request_source"]["sha256"]:
        raise ValueError("REQUEST_NOT_BOUND_TO_STAGE")
    raw_result = read(directory / "native/result.json")
    if result != raw_result or type(native["exit_code"]) is not int or execution["exit_code"] != native["exit_code"]:
        raise ValueError("NATIVE_RESULT_OR_EXIT_MISMATCH")
    acceptance = result.get("engineering_acceptance")
    if acceptance == "PASS_SCOPED_SIMULATION" and execution["state"] != "SUCCEEDED":
        raise ValueError("NATIVE_SUCCESS_REQUIRES_SUCCESSFUL_STAGE")
    if {"PASS_SCOPED_SIMULATION": 0, "FAIL": 2, "EVIDENCE_INSUFFICIENT": 1}.get(acceptance) != native["exit_code"]:
        raise ValueError("NATIVE_RESULT_OR_EXIT_MISMATCH")
    if acceptance in ("PASS_SCOPED_SIMULATION", "FAIL"):
        if result["engine"] != request["engine"] or result["scope"] != request["scope"]:
            raise ValueError("RESULT_SCOPE_OR_ENGINE_MISMATCH")
        measured = []
        for check in request["checks"]:
            value = number(result["metrics"][check["metric"]], -1e100, 1e100)
            passed = value >= check["value"] if check["op"] == "ge" else value <= check["value"] if check["op"] == "le" else value == check["value"]
            measured.append({**check, "actual": value, "passed": passed})
        if measured != result["checks"] or (acceptance == "PASS_SCOPED_SIMULATION") != all(c["passed"] for c in measured):
            raise ValueError("MEASURED_CHECK_CHANGED")
        if request["engine"] == "ngspice":
            vector_data = read(directory / "native/vectors.json")
            expected_names = set(request["parameters"]["vectors"]) | ({"time"} if request["parameters"]["analysis"]["kind"] == "transient" else set())
            vectors = vector_data["vectors"]
            if set(vectors) != expected_names or len({len(v) for v in vectors.values()}) != 1:
                raise ValueError("RAW_VECTOR_COVERAGE_MISMATCH")
            for name, values in vectors.items():
                if not values or any(type(v) not in (int, float) or not math.isfinite(v) for v in values):
                    raise ValueError("INVALID_RAW_VECTOR")
                for suffix, value in {"initial": values[0], "final": values[-1], "min": min(values), "max": max(values)}.items():
                    if result["metrics"][name + "." + suffix] != value:
                        raise ValueError("RAW_VECTOR_METRIC_MISMATCH")
            if "time" in vectors and (any(b <= a for a, b in zip(vectors["time"], vectors["time"][1:])) or not math.isclose(vectors["time"][-1], request["parameters"]["analysis"]["stop_seconds"], rel_tol=1e-7)):
                raise ValueError("INCOMPLETE_OR_NONMONOTONIC_TIME")
    return {"status": "FILES_AND_NATIVE_RECEIPT_VERIFIED", "engineering_acceptance": acceptance,
            "scope": request["scope"], "user_goal_achieved": False, "files_sha256": sha(directory / "files.json")}


def completion_check(requirement, source, inputs):
    """Read a native run into the existing completion contract; no new execution."""
    if source != {"root": source["root"], "stage": "REALITY", "label": "native", "artifact": "result.json"}:
        raise ValueError("EXACT_REALITY_EVIDENCE_SOURCE_REQUIRED")
    directory = Path(source["root"]).resolve()
    if read(directory / "run.json").get("schema") == "rlsok.reality-analysis-run/1":
        from implementation.reality_analysis import completion_analysis_check
        return completion_analysis_check(requirement, source, inputs)
    if read(directory / "run.json").get("schema") == "rlsok.reality-linux-run/1":
        from implementation.reality_linux import completion_linux_check
        return completion_linux_check(requirement, source, inputs)
    verify(directory)
    manifest, execution, result = (read(directory / name) for name in ("run.json", "execution.json", "result.json"))
    from implementation.completion import assertion, bound_file
    bindings = list(manifest["inputs"].values()) + [manifest["request_source"]]
    for name in requirement["inputs"]:
        bound_file(inputs[name])
        if inputs[name] not in bindings:
            raise ValueError("RECEIPT_BELONGS_TO_DIFFERENT_DESIGN")
    # The complete original request freezes solver parameters and measured checks.
    if manifest["request_source"] not in [inputs[name] for name in requirement["inputs"]]:
        raise ValueError("FROZEN_SIMULATION_REQUEST_REQUIRED")
    if manifest["schema"] != "rlsok.reality-run/2":
        raise ValueError("HISTORICAL_UNLEDGERED_PROBE_IS_NOT_COMPLETION_EVIDENCE")
    mandatory_tools = {manifest["worker"]["path"], manifest["engine_library"]}
    if not mandatory_tools <= set(requirement["tools"]):
        raise ValueError("FROZEN_NATIVE_ADAPTER_AND_SOLVER_REQUIRED")
    for path, digest in requirement["tools"].items():
        if manifest["tools"].get(path) != digest:
            raise ValueError("RECEIPT_TOOL_MISMATCH")
        bound_file({"path": path, "sha256": digest})
    binding = {"evidence": {"path": (directory / "result.json").as_posix(), "sha256": sha(directory / "result.json")},
               "receipt_sha256": sha(directory / "execution.json"),
               "verification_recipe": manifest["worker"],
               "execution_identity": {**execution["execution_binding"],
                                      "binds": [{"source": b["path"], "sha256": b["sha256"]} for b in bindings],
                                      "tool_identity_hashes": requirement["tools"]},
               "additional_evidence": {name: {"path": str(directory / name), "sha256": value}
                                       for name, value in execution["artifact_sha256"].items()
                                       if name in ("vectors.json", "trajectory.json", "model.json", "native-stdout.log", "native-stderr.log", "run.json", "invocation.json")}}
    acceptance = result["engineering_acceptance"]
    if acceptance == "EVIDENCE_INSUFFICIENT":
        return {"state": "EVIDENCE_INSUFFICIENT", "reason": result.get("reason"), **binding}
    if acceptance == "FAIL":
        return {"state": "FAILED", "failure_kind": "DESIGN_VIOLATION", "reason": "Native measured checks failed", **binding}
    rule = requirement["rule"]
    if rule["format"] != "json":
        raise ValueError("NATIVE_RESULT_REQUIRES_MEASURED_JSON_RULE")
    passed = all(assertion(result, item) for item in rule["assertions"])
    return {"state": "PASS" if passed else "FAILED", **binding}


def main(argv=None):
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("engines", "run", "verify"))
    parser.add_argument("--request")
    parser.add_argument("--out")
    parser.add_argument("--directory")
    parser.add_argument("--session")
    args = parser.parse_args(argv)
    try:
        if args.command == "engines":
            value = engines()
        elif args.command == "verify":
            if not args.directory:
                raise ValueError("--directory required")
            value = verify(args.directory)
        else:
            if not args.request or not args.out:
                raise ValueError("--request and a new --out directory required")
            value = run(args.request, args.out, args.session)
        print(json.dumps(value, ensure_ascii=True, allow_nan=False))
        if args.command != "run" or value.get("status") == "AUTOMATIC_STOP":
            return 0
        return 0 if value["result"]["engineering_acceptance"] == "PASS_SCOPED_SIMULATION" and value["execution"]["state"] == "SUCCEEDED" and type(value["execution"]["exit_code"]) is int and value["execution"]["exit_code"] == 0 else 2
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
        print(json.dumps({"status": "REJECTED", "reason": str(error), "user_goal_achieved": False}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
