"""MuJoCo bridge to the existing Linux EngineeringCLI/Workflow.

No budget or authorization is created here. The Windows entry performs current
Completion admission; this runner checks the frozen admission and uses the
existing immutable plan, reservation, cgroup/bwrap, receipt and settlement path.
Read-only verification imports no Linux-only modules and works on Windows.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import sys
import time
import uuid


SESSION_SCHEMA = "rlsok.reality-linux-session/1"
RUN_SCHEMA = "rlsok.reality-linux-run/1"
MEMORY_BYTES = 4 * 1024 ** 3
DISK_BYTES = 1024 ** 3
LOG_BYTES = 8 * 1024 ** 2


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")


def sha(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1048576), b""):
            value.update(block)
    return value.hexdigest()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def linux_path(value):
    """Only explicit absolute drive paths and absolute POSIX paths are mapped."""
    if not isinstance(value, str) or not value or "\x00" in value or value.startswith("//"):
        raise ValueError("ABSOLUTE_SHARED_PATH_REQUIRED")
    if re.match(r"^[A-Za-z]:[\\/]", value):
        path = PureWindowsPath(value)
        if ".." in path.parts:
            raise ValueError("PATH_TRAVERSAL")
        return "/mnt/" + path.drive[0].lower() + "/" + "/".join(path.parts[1:])
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts or "\\" in value or str(path) != value:
        raise ValueError("ABSOLUTE_SHARED_PATH_REQUIRED")
    return value


def host_path(value):
    mapped = linux_path(value)
    if os.name == "nt":
        match = re.fullmatch(r"/mnt/([a-z])/(.+)", mapped)
        if not match:
            raise ValueError("LINUX_ONLY_PATH_NOT_READABLE_ON_WINDOWS")
        return Path(match[1].upper() + ":/" + match[2])
    return Path(mapped)


def relative(value):
    path = PurePosixPath(value)
    if not value or "\\" in value or ":" in value or path.is_absolute() or str(path) != value or ".." in path.parts:
        raise ValueError("PROJECT_RELATIVE_FILE_REQUIRED")
    return value


def binding_path(binding):
    if set(binding) != {"path", "sha256"} or not re.fullmatch(r"[0-9a-f]{64}", binding["sha256"]):
        raise ValueError("EXACT_FILE_BINDING_REQUIRED")
    path = host_path(binding["path"])
    if path.is_symlink() or not path.is_file() or sha(path) != binding["sha256"]:
        raise ValueError("FROZEN_FILE_CHANGED: " + str(path))
    return path


def validate_session(session):
    fields = {"schema", "engine_root", "workflow_plan_sha256", "authorization", "stage", "label", "worker",
              "runtime_lock", "request", "inputs", "identity", "admission", "completion_directory", "bridge", "payload"}
    if set(session) != fields or session["schema"] != SESSION_SCHEMA:
        raise ValueError("EXACT_LINUX_SESSION_REQUIRED")
    linux_path(session["engine_root"])
    linux_path(session["completion_directory"])
    if set(session["bridge"]) != {"base", "engine_root", "distribution"} or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", session["bridge"]["distribution"]):
        raise ValueError("EXACT_EXISTING_WINDOWS_BRIDGE_REQUIRED")
    for name in ("base", "engine_root"):
        linux_path(session["bridge"][name])
    for key in ("stage", "label"):
        if not isinstance(session[key], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", session[key]):
            raise ValueError("SAFE_STAGE_AND_LABEL_REQUIRED")
    if set(session["identity"]) != {"goal", "version", "design", "requirement"}:
        raise ValueError("COMPLETE_COMPLETION_IDENTITY_REQUIRED")
    for name in session["inputs"]:
        relative(name)


def admission_body(session):
    return {key: session[key] for key in ("engine_root", "workflow_plan_sha256", "authorization", "stage", "label",
                                         "worker", "runtime_lock", "request", "inputs", "identity", "completion_directory", "bridge", "payload")}


def _runtime(lock):
    if lock.get("schema") != "rlsok.reality-linux-tool-lock/1" or set(lock) != {"schema", "runtime", "common_files", "archives"}:
        raise ValueError("EXACT_LINUX_TOOL_LOCK_REQUIRED")
    runtime = lock["runtime"]
    if runtime.get("engine") != "mujoco" or runtime.get("library") not in runtime.get("files", {}):
        raise ValueError("FROZEN_NATIVE_MUJOCO_REQUIRED")
    root = PurePosixPath(linux_path(runtime["directory"]))
    files = runtime["files"]
    if not files or any(not PurePosixPath(p).is_relative_to(root) or ".." in PurePosixPath(p).parts for p in files):
        raise ValueError("RUNTIME_FILE_SET_CHANGED")
    if not isinstance(lock["archives"], dict) or not 1 <= len(lock["archives"]) <= 32:
        raise ValueError("FROZEN_OFFICIAL_WHEEL_ARCHIVES_REQUIRED")
    # Avoid thousands of shared-drive metadata calls. Native payload expands
    # these exact archives in bounded tmpfs and verifies every original file.
    for name, value in lock["common_files"].items():
        path = Path(linux_path(name))
        if path.is_symlink() or not path.is_file() or sha(path) != value:
            raise ValueError("LINUX_TOOL_IDENTITY_CHANGED: " + name)
    python = str(Path(sys.executable).resolve())
    if lock["common_files"].get(python) != sha(python):
        raise ValueError("RUNNING_LINUX_PYTHON_NOT_FROZEN")
    return runtime


def _archive_cache(lock, lock_sha256, cache_parent=None):
    """Materialize existing frozen archives once; native payload rehashes all.

    A cache hit trusts no simulation result: immutable payload still checks each
    wheel before extraction and every runtime file before and after the solver.
    No package install, download, altered runtime, or additional setup command.
    """
    start, cpu_start = time.monotonic(), time.process_time()
    parent = Path(cache_parent or "/opt/atoms-g03/reality-archives")
    target = parent / lock_sha256
    archives = [{"source": path, "sha256": value, "name": str(index) + ".whl"}
                for index, (path, value) in enumerate(sorted(lock["archives"].items()))]
    created = False
    if target.exists():
        if target.is_symlink() or not target.is_dir():
            raise ValueError("REGULAR_ARCHIVE_CACHE_REQUIRED")
        marker = read(target / "archive-cache.json")
        if marker.get("runtime_lock_sha256") != lock_sha256 or marker.get("archives") != archives:
            raise ValueError("ARCHIVE_CACHE_IDENTITY_CHANGED")
    else:
        parent.mkdir(parents=True, exist_ok=True)
        temporary = parent / (lock_sha256 + ".partial-" + uuid.uuid4().hex)
        temporary.mkdir()
        sizes, total = {}, 0
        try:
            for item in archives:
                source = Path(item["source"])
                if source.is_symlink() or not source.is_file():
                    raise ValueError("REGULAR_FROZEN_ARCHIVE_REQUIRED")
                value = hashlib.sha256()
                destination = temporary / item["name"]
                with source.open("rb") as src, destination.open("xb") as dst:
                    for block in iter(lambda: src.read(1048576), b""):
                        total += len(block)
                        if total > 268435456:
                            raise ValueError("ARCHIVE_CACHE_DISK_LIMIT")
                        value.update(block)
                        dst.write(block)
                if value.hexdigest() != item["sha256"]:
                    raise ValueError("FROZEN_ARCHIVE_CHANGED_DURING_STAGING")
                sizes[item["name"]] = destination.stat().st_size
                destination.chmod(0o444)
            marker = {"schema": "rlsok.reality-archive-cache/1", "runtime_lock_sha256": lock_sha256,
                      "archives": archives, "sizes": sizes, "archive_bytes": total}
            write(temporary / "archive-cache.json", marker)
            (temporary / "archive-cache.json").chmod(0o444)
            temporary.chmod(0o555)
            temporary.rename(target)
            created = True
        except BaseException:
            # Retain partial material and original evidence; no automatic retry.
            raise
    expected_names = {item["name"] for item in archives} | {"archive-cache.json"}
    if {path.name for path in target.iterdir()} != expected_names:
        raise ValueError("ARCHIVE_CACHE_FILE_SET_CHANGED")
    for item in archives:
        path = target / item["name"]
        if path.is_symlink() or not path.is_file() or path.stat().st_size != marker["sizes"][item["name"]]:
            raise ValueError("ARCHIVE_CACHE_FILE_CHANGED")
    # The existing directory-bind guard requires an explicit content anchor.
    anchor = target / archives[0]["name"]
    if sha(anchor) != archives[0]["sha256"]:
        raise ValueError("ARCHIVE_CACHE_ANCHOR_CHANGED")
    return {"schema": "rlsok.reality-archive-staging/1", "cache_directory": str(target), "created": created,
            "runtime_lock_sha256": lock_sha256, "archive_count": len(archives), "archive_bytes": marker["archive_bytes"],
            "cache_manifest": {"path": str(target / "archive-cache.json"), "sha256": sha(target / "archive-cache.json")},
            "identity_anchor": {"path": str(anchor), "sha256": archives[0]["sha256"]},
            "observed_controller_cost": {"cpu": time.process_time() - cpu_start, "wall": time.monotonic() - start},
            "accounting_scope": "Existing CONTROL_AND_CLEANUP_HOLD; observed minimum, not a new grant or a release of unknown cost.",
            "native_verification": "Frozen payload rehashes every archive and exact runtime files in bounded scratch on every call."}


def _prepared_directory(session):
    identity = digest({"engine_root": linux_path(session["engine_root"]), "workflow_plan_sha256": session["workflow_plan_sha256"]})
    return Path("/opt/atoms-g03/reality-inputs") / identity / session["label"]




def windows_admission(request_path, output, session_path):
    """Current Completion admission; no launch, resource grant, or fake human confirmation."""
    from implementation.completion import Completion, bound_file
    from implementation.reality import validate
    request_path, output, session_path = (Path(value).resolve() for value in (request_path, output, session_path))
    session = read(session_path)
    validate_session(session)
    request = read(request_path)
    validate(request)
    if request["engine"] != "mujoco" or request["parameters"].get("render", False):
        raise ValueError("HEADLESS_LINUX_MUJOCO_ONLY")
    if request["resources"]["memory_bytes"] != MEMORY_BYTES or request["resources"]["disk_bytes"] != DISK_BYTES:
        raise ValueError("EXISTING_WORKFLOW_FIXED_4GIB_MEMORY_1GIB_SCRATCH_REQUIRED")
    if binding_path(session["request"]).resolve() != request_path:
        raise ValueError("REQUEST_SESSION_IDENTITY_MISMATCH")
    engine = host_path(session["engine_root"]).resolve()
    if output != engine / "jobs" / session["label"]:
        raise ValueError("NEW_EXISTING_WORKFLOW_JOB_OUTPUT_REQUIRED")
    if sha(engine / "plan.json") != session["workflow_plan_sha256"]:
        raise ValueError("FROZEN_WORKFLOW_PLAN_CHANGED")
    plan = read(engine / "plan.json")
    if plan["authorization_sha256"] != session["authorization"]["sha256"]:
        raise ValueError("WORKFLOW_AUTHORIZATION_IDENTITY_MISMATCH")
    binding_path(session["authorization"])
    worker = bound_file(session["worker"])
    payload = bound_file(session["payload"])
    lock_path = bound_file(session["runtime_lock"])
    lock = read(lock_path)
    if lock.get("schema") != "rlsok.reality-linux-tool-lock/1" or lock.get("runtime", {}).get("engine") != "mujoco":
        raise ValueError("FROZEN_LINUX_MUJOCO_RUNTIME_REQUIRED")
    project_value = request["project"]["directory"]
    project = Path(project_value)
    project = (request_path.parent / project).resolve() if not project.is_absolute() else project.resolve()
    if output.is_relative_to(project) or set(session["inputs"]) != set(request["project"]["files"]):
        raise ValueError("COMPLETE_PROJECT_OUTSIDE_RUN_OUTPUT_REQUIRED")
    for name, binding in session["inputs"].items():
        path = bound_file(binding)
        if path != (project / relative(name)).resolve() or not path.is_relative_to(project):
            raise ValueError("REQUEST_PROJECT_BINDINGS_MISMATCH")
    identity = session["identity"]
    service = Completion(host_path(session["completion_directory"]))
    try:
        decision = service.evaluate(identity["goal"], identity["version"], identity["design"])
        if decision["decision"] == "DONE" or decision["execution_state"] in ("PAUSED", "BLOCKED"):
            return {"status": "AUTOMATIC_STOP", "launched": 0, "completion": decision}
        row, contract = service.contract(identity["goal"], identity["version"])
        if not row["confirmation"] or decision["superseded_contract"]:
            raise ValueError("CURRENT_CONFIRMED_CONTRACT_REQUIRED")
        service.approval_chain(identity["goal"], identity["version"])
        requirement = next((r for r in contract["requirements"] if r["id"] == identity["requirement"]), None)
        if requirement is None or requirement["kind"] != "engine_artifact":
            raise ValueError("ENGINE_REQUIREMENT_IN_CURRENT_CONTRACT_REQUIRED")
        design = service.design(identity["goal"], identity["design"])
        frozen_inputs = [design[name] for name in requirement["inputs"]]
        run_inputs = [*session["inputs"].values(), session["request"]]
        if len(frozen_inputs) != len(run_inputs) or any(binding not in frozen_inputs for binding in run_inputs):
            raise ValueError("COMPLETE_DESIGN_AND_REQUEST_BINDINGS_REQUIRED")
        allowed_tools = {session["worker"]["path"]: sha(worker), session["runtime_lock"]["path"]: sha(lock_path), session["payload"]["path"]: sha(payload)}
        if set(requirement["tools"]) != set(allowed_tools):
            raise ValueError("EXACT_FROZEN_WORKER_PAYLOAD_AND_RUNTIME_LOCK_REQUIRED")
        for path, value in requirement["tools"].items():
            if allowed_tools.get(path) != value:
                raise ValueError("CONTRACT_TOOL_IDENTITY_MISMATCH")
            bound_file({"path": path, "sha256": value})
    finally:
        service.db.close()
    if output.exists():
        raise ValueError("IMMUTABLE_LINUX_JOB_ALREADY_EXISTS")
    # Each run gets one immutable admission; completion evaluation above is
    # always current even when an existing exact admission is read back.
    admitted = {"schema": "rlsok.reality-linux-admission/1", "status": "ALLOWED", "session": admission_body(session)}
    admission_path = session_path.parent / (session["label"] + "-admission.json")
    if admission_path.exists():
        if read(admission_path) != admitted:
            raise ValueError("IMMUTABLE_ADMISSION_ALREADY_EXISTS")
    else:
        write(admission_path, admitted)
    binding = {"path": str(admission_path), "sha256": sha(admission_path)}
    if session["admission"] not in (None, binding):
        raise ValueError("SESSION_ADMISSION_IDENTITY_CHANGED")
    session["admission"] = binding
    write(session_path, session)
    return {"status": "COMPLETION_ADMITTED_NOT_EXECUTED", "session": session, "request": request}


def windows_run(request_path, output, session_path):
    """One bounded WSL call through existing Stage; its wait time is real cost."""
    if os.name != "nt":
        raise ValueError("WINDOWS_BRIDGE_REQUIRES_WINDOWS_HOST")
    admitted = windows_admission(request_path, output, session_path)
    if admitted["status"] == "AUTOMATIC_STOP":
        return admitted
    from implementation.pcb_cli import Stage
    from implementation.reality import ROOT
    session, request = admitted["session"], admitted["request"]
    bridge = session["bridge"]
    engine, base = (host_path(bridge[key]).resolve() for key in ("engine_root", "base"))
    plan, ledger = read(engine / "plan.json"), read(engine / "resources.json")
    if plan.get("wsl_distribution") != bridge["distribution"]:
        raise ValueError("WSL_DISTRIBUTION_NOT_FROZEN_IN_BRIDGE_PLAN")
    if any(plan["budget"][key] > limit for key, limit in (("cpu", 20), ("wall", 80))):
        raise ValueError("WINDOWS_LINUX_BRIDGE_APPROVED_TOTAL_EXCEEDED")
    remaining = {key: plan["budget"][key] - ledger["spent"][key] - plan["controller_hold"][key]
                 - sum(stage["cap"][key] for stage in ledger["stages"].values() if stage["state"] != "SETTLED") for key in ("cpu", "wall")}
    cap = {"cpu": min(2, remaining["cpu"]), "wall": min(request["resources"]["wall_seconds"] + 2, remaining["wall"])}
    if any(value < 0.1 for value in cap.values()):
        raise ValueError("INSUFFICIENT_REMAINING_WINDOWS_BRIDGE_BUDGET")
    system = Path(os.environ["SystemRoot"])
    wsl = system / "System32/wsl.exe"
    if not wsl.is_file():
        raise ValueError("EXISTING_WSL_EXECUTABLE_UNAVAILABLE")
    recipe = Path(__file__).resolve()
    frozen_paths = [Path(request_path).resolve(), Path(session_path).resolve(), *[binding_path(b) for b in session["inputs"].values()],
                    binding_path(session["worker"]), binding_path(session["payload"]), binding_path(session["runtime_lock"]), binding_path(session["admission"])]
    inputs = {path: sha(path) for path in frozen_paths}
    tools = {path: sha(path) for path in (recipe, ROOT / "implementation/reality.py", ROOT / "implementation/completion.py",
                                        ROOT / "implementation/engineering_cli.py", ROOT / "implementation/complete_validation.py", wsl)}
    label = "wsl_" + hashlib.sha256(str(Path(session_path).resolve()).encode()).hexdigest()[:20]
    limits = read(base / "authorization.json")["limits"]
    stage = Stage(label, label, cap, inputs, tools, command="reality-linux-run", engine=engine, base=base, recipe=recipe,
                  memory_cap=min(MEMORY_BYTES, plan["per_job_memory"]), disk_cap=limits["disk_ceiling_bytes"],
                  path_formatter=lambda p: str(Path(p).resolve()), result_updater=lambda _stage, _receipt: None)
    stage.begin()
    try:
        argv = [str(wsl), "--distribution", bridge["distribution"], "--user", "root", "--cd", linux_path(str(ROOT)),
                "--exec", "/usr/bin/python3", "-B", linux_path(str(stage.recipe)),
                "--request", linux_path(str(Path(request_path).resolve())), "--out", linux_path(str(Path(output).resolve())),
                "--session", linux_path(str(Path(session_path).resolve()))]
        environment = {key: os.environ[key] for key in ("SystemRoot", "WINDIR") if key in os.environ}
        native = stage.call(argv, "wsl", environment=environment)
        execution_path = Path(output) / "execution.json"
        if not execution_path.is_file() or read(execution_path).get("final_cgroup_quiescent") is not True:
            raise RuntimeError("LINUX_FINAL_CGROUP_QUIESCENCE_UNAVAILABLE")
        verified = verify_linux(output)
        receipt = stage.finish(native["exit_code"], None, None if native["exit_code"] == 0 else "LINUX_DESIGN_OR_TOOL_FAILURE")
        return {"status": "EXECUTION_COMPLETE" if native["exit_code"] == 0 else "EXECUTION_FAILED", "output": str(output),
                "execution": read(execution_path), "result": read(Path(output) / "result.json"), "verification": verified,
                "bridge_execution": receipt, "bridge_output": str(stage.job), "files_sha256": sha(Path(output) / "files.json")}
    except BaseException as error:
        # A Windows Job Object proves only the Windows process tree. Losing the
        # WSL reply must never be reported as Linux quiescence or zero cost.
        stage.accounting_unknown = {"reason": str(error), "scope": "Linux final receipt/quiescence or bridge accounting incomplete; retain complete reservation"}
        write(stage.job / "resource-accounting-pending.json", stage.accounting_unknown)
        stage.finish(1, str(error), "LINUX_RECEIPT_OR_BRIDGE_ACCOUNTING_UNKNOWN")
        raise


def linux_run(request_path, output, session_path):
    if os.name == "nt":
        raise ValueError("LINUX_RUN_REQUIRES_AUTHORIZED_LINUX_HOST")
    from implementation.reality import validate
    from implementation.reality_worker import validate_mjcf
    from implementation.engineering_cli import EngineeringCLI
    session = read(session_path)
    validate_session(session)
    request_file = binding_path(session["request"])
    if Path(request_path).resolve() != request_file.resolve():
        raise ValueError("REQUEST_SESSION_IDENTITY_MISMATCH")
    request = read(request_file)
    validate(request)
    if request["engine"] != "mujoco" or request["parameters"].get("render", False):
        raise ValueError("HEADLESS_LINUX_MUJOCO_ONLY")
    cap = request["resources"]
    if cap["memory_bytes"] != MEMORY_BYTES or cap["disk_bytes"] != DISK_BYTES:
        raise ValueError("EXISTING_WORKFLOW_FIXED_4GIB_MEMORY_1GIB_SCRATCH_REQUIRED")
    if set(session["inputs"]) != set(request["project"]["files"]):
        raise ValueError("COMPLETE_FROZEN_PROJECT_REQUIRED")
    engine = host_path(session["engine_root"]).resolve()
    output = Path(output).resolve()
    if output != engine / "jobs" / session["label"] or output.exists():
        raise ValueError("NEW_EXISTING_WORKFLOW_JOB_OUTPUT_REQUIRED")
    worker = binding_path(session["worker"])
    payload = binding_path(session["payload"])
    lock_path = binding_path(session["runtime_lock"])
    authorization = binding_path(session["authorization"])
    admission_path = binding_path(session["admission"])
    admission = read(admission_path)
    if (admission.get("schema") != "rlsok.reality-linux-admission/1" or admission.get("status") != "ALLOWED"
            or admission.get("session") != admission_body(session)):
        raise ValueError("FROZEN_COMPLETION_ADMISSION_REQUIRED")
    if sha(engine / "plan.json") != session["workflow_plan_sha256"]:
        raise ValueError("FROZEN_WORKFLOW_PLAN_CHANGED")
    plan = read(engine / "plan.json")
    if plan.get("authorization_sha256") != sha(authorization):
        raise ValueError("WORKFLOW_AUTHORIZATION_IDENTITY_MISMATCH")
    lock = read(lock_path)
    runtime = _runtime(lock)
    archive_staging = _archive_cache(lock, session["runtime_lock"]["sha256"])
    sources = {name: binding_path(binding) for name, binding in session["inputs"].items()}
    project_value = request["project"]["directory"]
    if re.match(r"^[A-Za-z]:[\\/]", project_value) or project_value.startswith("/"):
        project = host_path(project_value).resolve()
    else:
        project = (request_file.parent / project_value).resolve()
    if any(path.resolve() != (project / name).resolve() for name, path in sources.items()):
        raise ValueError("REQUEST_PROJECT_BINDINGS_MISMATCH")
    if output.is_relative_to(project):
        raise ValueError("OUTPUT_INSIDE_INPUT_PROJECT")
    if sum(path.stat().st_size for path in sources.values()) > 67108864:
        raise ValueError("INPUT_SNAPSHOT_LIMIT")
    validate_mjcf(sources[request["project"]["entry"]].read_text(encoding="utf-8-sig"), project)
    # Immutable per-job preparation; failures retain this directory for review.
    # Shared originals remain frozen by Windows Stage. EngineeringCLI repeatedly
    # verifies these byte-identical local snapshots to avoid shared-drive IO.
    prepared = _prepared_directory(session)
    if any(parent.is_symlink() for parent in [prepared, *prepared.parents]):
        raise ValueError("LOCAL_INPUT_STAGING_SYMLINK_REJECTED")
    prepared.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(worker, prepared / "worker.py")
    shutil.copyfile(request_file, prepared / "request.json")
    shutil.copyfile(session_path, prepared / "session.json")
    shutil.copyfile(lock_path, prepared / "runtime-lock.json")
    shutil.copyfile(admission_path, prepared / "admission.json")
    for name, expected in (("worker.py", session["worker"]["sha256"]), ("request.json", session["request"]["sha256"]),
                           ("runtime-lock.json", session["runtime_lock"]["sha256"]), ("admission.json", session["admission"]["sha256"]),
                           ("session.json", sha(session_path))):
        if sha(prepared / name) != expected:
            raise ValueError("FROZEN_INPUT_CHANGED_DURING_SNAPSHOT")
    for name, path in sources.items():
        target = prepared / "project" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        if sha(target) != session["inputs"][name]["sha256"]:
            raise ValueError("SOURCE_CHANGED_DURING_SNAPSHOT")
    invocation_runtime = {**runtime, "directory": "/work/runtime", "library": "/work/runtime/" + str(Path(runtime["library"]).relative_to(runtime["directory"]))}
    invocation = {**request, "runtime": invocation_runtime}
    write(prepared / "invocation.json", invocation)
    shutil.copyfile(payload, prepared / "payload.py")
    if sha(prepared / "payload.py") != session["payload"]["sha256"]:
        raise ValueError("FROZEN_PAYLOAD_CHANGED_DURING_SNAPSHOT")
    manifest = {"schema": RUN_SCHEMA, "request": request, "request_source": session["request"],
                "inputs": session["inputs"], "session": session, "worker": session["worker"],
                "engine_library": runtime["library"], "runtime_lock": session["runtime_lock"],
                "archive_staging": archive_staging,
                "tools": {**lock["common_files"], **runtime["files"], session["worker"]["path"]: session["worker"]["sha256"],
                          session["runtime_lock"]["path"]: session["runtime_lock"]["sha256"], session["payload"]["path"]: session["payload"]["sha256"]},
                "payload_sha256": sha(prepared / "payload.py"), "invocation_sha256": sha(prepared / "invocation.json"),
                "hard_limits": {"memory_bytes": MEMORY_BYTES, "disk_bytes": DISK_BYTES, "log_bytes": LOG_BYTES, "concurrency": 1},
                "isolation_scope": "Existing Workflow cgroup/bwrap: readonly archives/design, exact-hash runtime materialized in bounded writable scratch, network namespace and bounded process tree.",
                "acceptance_scope": "Declared numerical checks only; Completion contract is separate."}
    write(prepared / "run.json", manifest)
    binds = []
    for source, target in [(prepared / "worker.py", "/solver/worker.py"),
                           *[(prepared / name, "/input/" + name) for name in ("request.json", "session.json", "runtime-lock.json", "admission.json", "invocation.json", "run.json")],
                           *[(prepared / "project" / name, "/project/" + name) for name in sources]]:
        binds.append({"source": str(source), "target": target, "sha256": sha(source)})
    binds.append({"source": archive_staging["cache_directory"], "target": "/archives"})
    cache_bindings = {archive_staging[name]["path"]: archive_staging[name]["sha256"] for name in ("cache_manifest", "identity_anchor")}
    identities = {**lock["common_files"], **cache_bindings,
                  **{str(prepared / name): sha(prepared / name) for name in
                     ("worker.py", "payload.py", "runtime-lock.json", "admission.json", "request.json", "session.json")}}
    for name in sources:
        identities[str(prepared / "project" / name)] = session["inputs"][name]["sha256"]
    budget = {"cpu": cap["cpu_seconds"], "wall": cap["wall_seconds"]}
    spec = {"stage": session["stage"], "stage_cap": budget, "identity_hashes": identities,
            "jobs": [{"label": session["label"], "payload": str(prepared / "payload.py"), "payload_sha256": manifest["payload_sha256"],
                      "binds": binds, "cap": budget}]}
    cli = EngineeringCLI(engine)
    cli.plan(spec)
    cli.reserve(session["stage"])
    result = cli.run(session["stage"], max_jobs=1)
    # Execution and native artifacts are untouched. Additional snapshots provide
    # Windows readback; the original receipt binds native-produced flat files.
    for name in ("project",):
        shutil.copytree(prepared / name, output / name)
    for name in ("request.json", "session.json", "payload.py"):
        shutil.copyfile(prepared / name, output / name)
    execution = read(output / "execution.json")
    # Workflow already hashed its persisted native artifacts and bound them to
    # execution.json. Reuse those exact hashes; Windows verify checks the bytes.
    native_hashes = execution["artifact_sha256"]
    write(output / "files.json", {"schema": "rlsok.reality-files/1", "files": {
        path.relative_to(output).as_posix(): native_hashes[path.name] if path.parent == output and path.name in native_hashes else sha(path)
        for path in output.rglob("*") if path.is_file()}})
    return {**result, "output": str(output), "execution": execution,
            "result": read(output / "result.json") if (output / "result.json").is_file() else {"engineering_acceptance": "EVIDENCE_INSUFFICIENT"},
            "files_sha256": sha(output / "files.json")}


def _measured_checks(request, result):
    acceptance = result.get("engineering_acceptance")
    if acceptance not in ("PASS_SCOPED_SIMULATION", "FAIL"):
        return
    if result.get("engine") != "mujoco" or result.get("scope") != request["scope"]:
        raise ValueError("NATIVE_SCOPE_CHANGED")
    measured = []
    for check in request["checks"]:
        actual = result["metrics"][check["metric"]]
        if type(actual) not in (int, float) or not math.isfinite(actual):
            raise ValueError("FINITE_NUMERICAL_CHECK_REQUIRED")
        passed = actual >= check["value"] if check["op"] == "ge" else actual <= check["value"] if check["op"] == "le" else actual == check["value"]
        measured.append({**check, "actual": actual, "passed": passed})
    if measured != result["checks"] or (acceptance == "PASS_SCOPED_SIMULATION") != all(item["passed"] for item in measured):
        raise ValueError("MEASURED_CHECK_CHANGED")


def verify_linux(directory):
    from implementation.reality import validate
    directory = Path(directory).resolve()
    index = read(directory / "files.json")
    paths = {p.relative_to(directory).as_posix() for p in directory.rglob("*") if p.is_file() and p != directory / "files.json"}
    if index.get("schema") != "rlsok.reality-files/1" or paths != set(index["files"]):
        raise ValueError("EXACT_NATIVE_FILE_INDEX_REQUIRED")
    for name, value in index["files"].items():
        path = directory / relative(name)
        if path.is_symlink() or not path.resolve().is_relative_to(directory) or sha(path) != value:
            raise ValueError("SEALED_NATIVE_FILE_CHANGED")
    manifest, execution, result = (read(directory / name) for name in ("run.json", "execution.json", "result.json"))
    if manifest["schema"] != RUN_SCHEMA:
        raise ValueError("LINUX_NATIVE_RUN_REQUIRED")
    session, request = manifest["session"], manifest["request"]
    validate_session(session)
    validate(request)
    if read(directory / "session.json") != session or read(directory / "request.json") != request:
        raise ValueError("REQUEST_OR_SESSION_SNAPSHOT_CHANGED")
    engine = host_path(session["engine_root"])
    if sha(engine / "plan.json") != session["workflow_plan_sha256"]:
        raise ValueError("WORKFLOW_PLAN_CHANGED")
    plan = read(engine / "plan.json")
    if sha(engine / "matrix.json") != plan["matrix_sha256"] or plan["authorization_sha256"] != session["authorization"]["sha256"]:
        raise ValueError("WORKFLOW_AUTHORIZATION_OR_MATRIX_CHANGED")
    binding_path(session["authorization"])
    descriptor = read(engine / "cli-plans" / (session["stage"] + ".json"))
    if descriptor["descriptor_sha256"] != digest({k: v for k, v in descriptor.items() if k != "descriptor_sha256"}) or descriptor["workflow_plan_sha256"] != session["workflow_plan_sha256"]:
        raise ValueError("CLI_DESCRIPTOR_CHANGED")
    spec = descriptor["spec"]
    cap = {"cpu": request["resources"]["cpu_seconds"], "wall": request["resources"]["wall_seconds"]}
    if spec["stage"] != session["stage"] or spec["stage_cap"] != cap or len(spec["jobs"]) != 1:
        raise ValueError("EXACT_FROZEN_LINUX_STAGE_REQUIRED")
    job = spec["jobs"][0]
    if job["label"] != session["label"] or job["cap"] != cap or job["payload_sha256"] != sha(directory / "payload.py") or job["payload_sha256"] != manifest["payload_sha256"] or job["payload_sha256"] != session["payload"]["sha256"]:
        raise ValueError("PAYLOAD_JOB_IDENTITY_CHANGED")
    stage = read(engine / "resources.json")["stages"][session["stage"]]
    if stage["state"] != "SETTLED" or stage["cli_plan_sha256"] != descriptor["descriptor_sha256"] or stage["cap"] != cap or len(stage["jobs"]) != 1:
        raise ValueError("SETTLED_EXISTING_LEDGER_REQUIRED")
    recorded = stage["jobs"][0]
    expected = {"stage": session["stage"], "label": session["label"], "cap": cap, "payload_sha256": job["payload_sha256"]}
    if recorded["label"] != session["label"] or recorded["receipt_sha256"] != sha(directory / "execution.json") or execution["execution_binding"] != expected:
        raise ValueError("NATIVE_RECEIPT_IDENTITY_CHANGED")
    if recorded["cost"] != execution["controller_cost"] or recorded["state"] != execution["state"]:
        raise ValueError("LEDGER_COST_STATE_CHANGED")
    if manifest["hard_limits"] != {"memory_bytes": MEMORY_BYTES, "disk_bytes": DISK_BYTES, "log_bytes": LOG_BYTES, "concurrency": 1}:
        raise ValueError("EXISTING_WORKFLOW_HARD_LIMITS_CHANGED")
    if request["resources"]["memory_bytes"] != MEMORY_BYTES or request["resources"]["disk_bytes"] != DISK_BYTES:
        raise ValueError("REQUEST_HARD_LIMIT_MISMATCH")
    for key, limit in (("memory_peak_bytes", MEMORY_BYTES), ("disk_bytes", DISK_BYTES)):
        if type(execution[key]) is not int or not 0 <= execution[key] <= limit:
            raise ValueError("NATIVE_RESOURCE_LIMIT_EXCEEDED")
    if (directory / "driver.log").stat().st_size > LOG_BYTES:
        raise ValueError("NATIVE_LOG_LIMIT_EXCEEDED")
    cost = execution["controller_cost"]
    final_cpu = execution.get("final_cgroup_cpu_seconds")
    if (execution.get("final_cgroup_quiescent") is not True or type(final_cpu) not in (int, float) or not math.isfinite(final_cpu)
            or final_cpu < 0 or cost["cpu"] < final_cpu or any(type(cost[k]) not in (int, float) or not math.isfinite(cost[k]) or cost[k] < 0 or cost[k] > cap[k] for k in ("cpu", "wall"))):
        raise ValueError("FINAL_QUIESCENT_WITHIN_BUDGET_RECEIPT_REQUIRED")
    artifacts = execution["artifact_sha256"]
    if not {"driver.log", "run.json", "invocation.json", "runtime-lock.json", "runtime-validation.json", "admission.json", "verification-recipe.py", "result.json"} <= set(artifacts):
        raise ValueError("NATIVE_BOUND_ARTIFACTS_REQUIRED")
    for name, value in artifacts.items():
        if sha(directory / relative(name)) != value:
            raise ValueError("ORIGINAL_NATIVE_ARTIFACT_CHANGED")
    if sha(directory / "verification-recipe.py") != session["worker"]["sha256"] or sha(directory / "runtime-lock.json") != session["runtime_lock"]["sha256"]:
        raise ValueError("NATIVE_WORKER_OR_RUNTIME_IDENTITY_CHANGED")
    if sha(directory / "admission.json") != session["admission"]["sha256"] or read(directory / "admission.json").get("session") != admission_body(session):
        raise ValueError("FROZEN_COMPLETION_ADMISSION_CHANGED")
    invocation = read(directory / "invocation.json")
    if sha(directory / "invocation.json") != manifest["invocation_sha256"] or {k: v for k, v in invocation.items() if k != "runtime"} != request:
        raise ValueError("INVOCATION_IDENTITY_CHANGED")
    binds = {b["target"]: b for b in job["binds"]}
    command = execution["command"]
    if (command[:2] != ["bwrap", "--unshare-all"] or command[-3:] != ["--", "python3", "/payload.py"]
            or not all(flag in command for flag in ("--clearenv", "--die-with-parent", "--cap-drop", "--uid", "--gid"))):
        raise ValueError("ORIGINAL_LINUX_SANDBOX_COMMAND_REQUIRED")
    if any(command[command.index(flag) + 1] != value for flag, value in (("--cap-drop", "ALL"), ("--uid", "65534"), ("--gid", "65534"))):
        raise ValueError("ORIGINAL_SANDBOX_PRIVILEGE_DROP_REQUIRED")
    command_binds = {command[i + 2]: command[i + 1] for i, value in enumerate(command) if value == "--ro-bind"}
    if command_binds.get("/payload.py") != job["payload"] or any(command_binds.get(target) != binding["source"] for target, binding in binds.items()):
        raise ValueError("ORIGINAL_SANDBOX_BINDS_CHANGED")
    for name in ("run.json", "invocation.json", "runtime-lock.json", "admission.json", "request.json", "session.json"):
        if binds["/input/" + name]["sha256"] != sha(directory / name):
            raise ValueError("STAGE_BIND_CHANGED")
    if binds["/solver/worker.py"]["sha256"] != session["worker"]["sha256"]:
        raise ValueError("WORKER_BIND_CHANGED")
    lock = read(directory / "runtime-lock.json")
    runtime = lock["runtime"]
    if manifest["engine_library"] != runtime["library"]:
        raise ValueError("RUNTIME_LIBRARY_CHANGED")
    expected_runtime = {**runtime, "directory": "/work/runtime", "library": "/work/runtime/" + PurePosixPath(runtime["library"]).relative_to(runtime["directory"]).as_posix()}
    if invocation["runtime"] != expected_runtime or lock.get("schema") != "rlsok.reality-linux-tool-lock/1":
        raise ValueError("INVOKED_RUNTIME_CHANGED")
    for path, value in lock["common_files"].items():
        if spec["identity_hashes"].get(path) != value:
            raise ValueError("FROZEN_LINUX_TOOL_CHANGED")
    if "/archives" in binds:
        staging = manifest["archive_staging"]
        expected_cache = "/opt/atoms-g03/reality-archives/" + session["runtime_lock"]["sha256"]
        if staging["cache_directory"] != expected_cache or binds["/archives"] != {"source": expected_cache, "target": "/archives"}:
            raise ValueError("EXACT_FROZEN_ARCHIVE_CACHE_BIND_REQUIRED")
        first_sha = sorted(lock["archives"].items())[0][1]
        if staging["identity_anchor"] != {"path": expected_cache + "/0.whl", "sha256": first_sha}:
            raise ValueError("ARCHIVE_CACHE_ANCHOR_IDENTITY_CHANGED")
        for name in ("cache_manifest", "identity_anchor"):
            binding = staging[name]
            if spec["identity_hashes"].get(binding["path"]) != binding["sha256"]:
                raise ValueError("ARCHIVE_CACHE_DESCRIPTOR_IDENTITY_CHANGED")
        if staging["runtime_lock_sha256"] != session["runtime_lock"]["sha256"] or staging["archive_count"] != len(lock["archives"]):
            raise ValueError("ARCHIVE_CACHE_RUNTIME_LOCK_CHANGED")
    else:
        # Preserve the original successful baseline's explicit individual binds.
        for index, (path, value) in enumerate(sorted(lock["archives"].items())):
            if (spec["identity_hashes"].get(path) != value
                    or binds["/archives/" + str(index) + ".whl"] != {"source": path, "target": "/archives/" + str(index) + ".whl", "sha256": value}):
                raise ValueError("FROZEN_ARCHIVE_BIND_CHANGED")
    runtime_validation = read(directory / "runtime-validation.json")
    if (runtime_validation.get("schema") != "rlsok.native-runtime-validation/1" or runtime_validation.get("status") != "FROZEN_RUNTIME_EXACTLY_VERIFIED"
            or runtime_validation.get("file_count") != len(runtime["files"]) or runtime_validation.get("archives") != lock["archives"]
            or runtime_validation.get("library_sha256") != runtime["files"][runtime["library"]]
            or runtime_validation.get("runtime_lock_sha256") != sha(directory / "runtime-lock.json") or runtime_validation.get("post_execution_verified") is not True):
        raise ValueError("NATIVE_RUNTIME_VERIFICATION_MISSING_OR_CHANGED")
    if set(manifest["inputs"]) != set(request["project"]["files"]) or manifest["inputs"] != session["inputs"] or manifest["request_source"] != session["request"]:
        raise ValueError("DESIGN_INPUT_IDENTITY_CHANGED")
    if sha(directory / "request.json") != session["request"]["sha256"]:
        raise ValueError("FROZEN_REQUEST_BYTES_CHANGED")
    for name, binding in manifest["inputs"].items():
        if sha(directory / "project" / relative(name)) != binding["sha256"] or binds["/project/" + name]["sha256"] != binding["sha256"]:
            raise ValueError("FROZEN_PROJECT_CHANGED")
    acceptance = result.get("engineering_acceptance")
    expected_exit = {"PASS_SCOPED_SIMULATION": 0, "FAIL": 2, "EVIDENCE_INSUFFICIENT": 1}.get(acceptance)
    if type(execution["exit_code"]) is not int or execution["exit_code"] != expected_exit or execution["state"] != ("SUCCEEDED" if expected_exit == 0 else "FAILED"):
        raise ValueError("NATIVE_RESULT_EXIT_MISMATCH")
    _measured_checks(request, result)
    if acceptance in ("PASS_SCOPED_SIMULATION", "FAIL"):
        trajectory = read(directory / "trajectory.json")
        frames = trajectory["frames"]
        times = [frame["time_seconds"] for frame in frames]
        if (trajectory.get("engine") != "mujoco" or not times or times[0] != 0 or any(not math.isfinite(t) for t in times)
                or any(b <= a for a, b in zip(times, times[1:])) or not math.isclose(times[-1], result["metrics"]["simulated_seconds"], rel_tol=1e-9, abs_tol=1e-12)):
            raise ValueError("COMPLETE_NATIVE_TRAJECTORY_REQUIRED")
        for name in request["parameters"].get("joints", []):
            for metric, value in frames[-1]["joints"][name].items():
                if result["metrics"]["joint." + name + ".final_" + metric] != value:
                    raise ValueError("FINAL_JOINT_NUMERICAL_MISMATCH")
    return {"status": "FILES_AND_NATIVE_RECEIPT_VERIFIED", "engineering_acceptance": acceptance,
            "scope": request["scope"], "user_goal_achieved": False, "files_sha256": sha(directory / "files.json")}


def completion_linux_check(requirement, source, inputs):
    from implementation.completion import assertion, bound_file
    if source != {"root": source["root"], "stage": "REALITY", "label": "native", "artifact": "result.json"}:
        raise ValueError("EXACT_REALITY_EVIDENCE_SOURCE_REQUIRED")
    directory = Path(source["root"]).resolve()
    verify_linux(directory)
    manifest, execution, result = (read(directory / name) for name in ("run.json", "execution.json", "result.json"))
    bindings = [*manifest["inputs"].values(), manifest["request_source"]]
    supplied = [inputs[name] for name in requirement["inputs"]]
    if len(supplied) != len(bindings) or any(binding not in supplied for binding in bindings):
        raise ValueError("COMPLETE_RUN_DESIGN_AND_REQUEST_BINDINGS_REQUIRED")
    for binding in supplied:
        bound_file(binding)
    mandatory = {manifest["worker"]["path"], manifest["runtime_lock"]["path"], manifest["session"]["payload"]["path"]}
    if not mandatory <= set(requirement["tools"]):
        raise ValueError("FROZEN_WORKER_PAYLOAD_AND_LINUX_NATIVE_RUNTIME_LOCK_REQUIRED")
    for path, value in requirement["tools"].items():
        if manifest["tools"].get(path) != value:
            raise ValueError("RECEIPT_TOOL_IDENTITY_MISMATCH")
        bound_file({"path": path, "sha256": value})
    binding = {"evidence": {"path": str(directory / "result.json"), "sha256": sha(directory / "result.json")},
               "receipt_sha256": sha(directory / "execution.json"), "verification_recipe": manifest["session"]["payload"],
               "execution_identity": {**execution["execution_binding"], "binds": [{"source": b["path"], "sha256": b["sha256"]} for b in bindings], "tool_identity_hashes": requirement["tools"]},
               "additional_evidence": {name: {"path": str(directory / name), "sha256": value}
                                       for name, value in execution["artifact_sha256"].items()
                                       if name in ("trajectory.json", "model.json", "driver.log", "run.json", "invocation.json", "runtime-lock.json", "runtime-validation.json", "admission.json", "verification-recipe.py")}}
    acceptance = result["engineering_acceptance"]
    if acceptance == "EVIDENCE_INSUFFICIENT":
        return {"state": "EVIDENCE_INSUFFICIENT", "reason": result.get("reason"), **binding}
    if acceptance == "FAIL":
        return {"state": "FAILED", "failure_kind": "DESIGN_VIOLATION", "reason": "Native measured checks failed", **binding}
    if requirement["rule"]["format"] != "json":
        raise ValueError("MEASURED_JSON_RULE_REQUIRED")
    passed = all(assertion(result, item) for item in requirement["rule"]["assertions"])
    return {"state": "PASS" if passed else "FAILED", **binding}


def main(argv=None):
    # The Windows bridge executes a frozen copy with an explicit repository cwd.
    if str(Path.cwd()) not in sys.path:
        sys.path.insert(0, str(Path.cwd()))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--session", required=True)
    args = parser.parse_args(argv)
    value = linux_run(args.request, args.out, args.session)
    print(json.dumps(value, ensure_ascii=False, allow_nan=False))
    return 0 if value["execution"]["exit_code"] == 0 else value["execution"]["exit_code"] or 1


if __name__ == "__main__":
    raise SystemExit(main())
