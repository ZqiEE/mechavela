"""Generic native-solver interface for RLSOK engineering loops.

Every engineering domain (circuit/ngspice today; CalculiX, OpenFOAM, MuJoCo
later) plugs in through the same three calls:

    adapter.identity()                -> ToolIdentity (what binary, which version, sha256)
    adapter.validate(job)             -> raises ValueError for unsafe / malformed input
    adapter.run(job, directory, caps) -> SolverRun (raw native evidence, never invented)

Rules shared by all adapters:
* Physics is computed only by the native solver, in a separate bounded child
  process. Python here only prepares inputs and reads the solver's own outputs.
* A run that did not finish is never reported as a design result: status is
  TOOL_ERROR / MODEL_REJECTED / RESOURCE_LIMIT, distinct from design FAIL.
* Every file a run produces is sealed with sha256 in ``files.json``.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
from typing import Any, Callable, Dict, List, Optional

SCHEMA_RUN = "rlsok.solver-run/1"

# Execution states. Design acceptance is decided by the caller from metrics,
# never by the adapter, so a SUCCEEDED solver run can still be a design FAIL.
SUCCEEDED = "SUCCEEDED"            # native solver finished, outputs read back
MODEL_REJECTED = "MODEL_REJECTED"  # solver (or pre-flight validation) rejected the model
TOOL_ERROR = "TOOL_ERROR"          # solver crashed / missing vectors / unreadable output
RESOURCE_LIMIT = "RESOURCE_LIMIT"  # wall/cpu/memory cap hit; result unknown
NOT_INTEGRATED = "NOT_INTEGRATED"  # adapter registered as planned only


def sha256_file(path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_json(path, value) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def seal_directory(directory) -> Dict[str, str]:
    """sha256 of every file under directory (except this directory's own files.json).

    Nested files.json of earlier sealed sub-runs are included, so one root
    index covers every byte of the run tree.
    """
    directory = Path(directory)
    seals = {}
    for path in sorted(directory.rglob("*")):
        if path.is_file() and path != directory / "files.json":
            seals[path.relative_to(directory).as_posix()] = sha256_file(path)
    write_json(directory / "files.json", {"schema": "rlsok.files/1", "files": seals})
    return seals


@dataclasses.dataclass
class ResourceCaps:
    wall_seconds: float = 60.0
    cpu_seconds: float = 60.0
    memory_bytes: int = 1 << 30

    def as_dict(self):
        return dataclasses.asdict(self)


@dataclasses.dataclass
class ToolIdentity:
    solver: str
    version: str                 # version string reported by the native tool itself
    binary: str                  # absolute path of the native library / executable
    binary_sha256: str
    backend: str                 # e.g. "shared-library", "executable"
    platform: str = dataclasses.field(default_factory=lambda: platform.platform())
    python: str = dataclasses.field(default_factory=lambda: sys.version.split()[0])
    evidence: Optional[str] = None  # where the version string came from
    provenance: Optional[dict] = None  # how the binary was obtained (lock file, build record ...)

    def as_dict(self):
        return dataclasses.asdict(self)


@dataclasses.dataclass
class SolverRun:
    solver: str
    status: str
    directory: str
    exit_code: Optional[int]
    wall_seconds: float
    cpu_seconds: Optional[float]
    tool: Optional[dict]
    outputs: Dict[str, Any]          # adapter-specific native outputs (vectors, metrics...)
    reason: Optional[str] = None
    limit_event: Optional[str] = None
    files: Dict[str, str] = dataclasses.field(default_factory=dict)

    def as_dict(self):
        value = dataclasses.asdict(self)
        value["schema"] = SCHEMA_RUN
        return value


class SolverAdapter:
    """Base class: one per native tool."""

    name = "abstract"
    domain = "abstract"
    state = NOT_INTEGRATED

    def identity(self) -> ToolIdentity:
        raise NotImplementedError

    def validate(self, job: dict) -> None:
        raise NotImplementedError

    def run(self, job: dict, directory, caps: ResourceCaps) -> SolverRun:
        raise NotImplementedError

    def describe(self) -> dict:
        return {"solver": self.name, "domain": self.domain, "state": self.state}


class PlannedAdapter(SolverAdapter):
    """Registered so the interface lists it, but it never runs or passes."""

    def __init__(self, name, domain, note):
        self.name, self.domain, self.note = name, domain, note

    def identity(self):
        raise ValueError(NOT_INTEGRATED + ": " + self.name)

    def validate(self, job):
        raise ValueError(NOT_INTEGRATED + ": " + self.name)

    def run(self, job, directory, caps):
        raise ValueError(NOT_INTEGRATED + ": " + self.name)

    def describe(self):
        return {"solver": self.name, "domain": self.domain, "state": NOT_INTEGRATED, "note": self.note}


_REGISTRY: Dict[str, Callable[[], SolverAdapter]] = {}


def register(name: str, factory: Callable[[], SolverAdapter]) -> None:
    _REGISTRY[name] = factory


def get(name: str) -> SolverAdapter:
    if name not in _REGISTRY:
        raise ValueError(NOT_INTEGRATED + ": " + name)
    return _REGISTRY[name]()


def registry() -> List[dict]:
    rows = []
    for name, factory in sorted(_REGISTRY.items()):
        try:
            rows.append(factory().describe())
        except Exception as error:  # describe must never hide a broken adapter
            rows.append({"solver": name, "state": "UNAVAILABLE", "reason": str(error)})
    return rows


# --- bounded child process -------------------------------------------------

def run_bounded(argv: List[str], directory, caps: ResourceCaps, env: Optional[dict] = None) -> dict:
    """Run a trusted worker with resource caps; stdout/stderr go to files.

    Windows: reuse the project's Job Object runner (implementation/windows_process.py).
    POSIX: rlimits (CPU, address space) + wall timeout, process group kill.
    """
    directory = Path(directory)
    if os.name == "nt":
        from implementation import windows_process
        result = windows_process.run(argv, directory, caps.cpu_seconds, caps.wall_seconds,
                                     memory_cap=caps.memory_bytes, env=env)
        result["resource_provider"] = result.get("resource_provider", "Windows Job Object")
        return result
    directory.mkdir(parents=True, exist_ok=False)
    import resource
    import signal

    def limits():
        os.setsid()
        cpu = max(1, int(caps.cpu_seconds + 0.999))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 1))
        resource.setrlimit(resource.RLIMIT_AS, (caps.memory_bytes, caps.memory_bytes))

    def group_alive(pgid):
        try:
            os.killpg(pgid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    start = time.monotonic()
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    limit_event = None
    with (directory / "stdout.log").open("wb") as out, (directory / "stderr.log").open("wb") as err:
        process = subprocess.Popen([str(a) for a in argv], cwd=str(directory), stdin=subprocess.DEVNULL,
                                   stdout=out, stderr=err, env=env, preexec_fn=limits)
        try:
            code = process.wait(timeout=caps.wall_seconds)
        except subprocess.TimeoutExpired:
            limit_event = "WALL_LIMIT"
            os.killpg(process.pid, signal.SIGKILL)
            code = process.wait()
        # The worker never spawns children; anything left in its process group
        # is killed so no native code outlives the recorded run.
        if group_alive(process.pid):
            os.killpg(process.pid, signal.SIGKILL)
        deadline = time.monotonic() + 10
        while group_alive(process.pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        quiescent = not group_alive(process.pid)
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu = (after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime)
    if limit_event is None and code in (-signal.SIGXCPU, -signal.SIGKILL) and cpu >= caps.cpu_seconds * 0.9:
        limit_event = "CPU_LIMIT"
    if limit_event is None and code == -signal.SIGKILL:
        limit_event = "KILLED_BY_SIGNAL (likely memory limit)"
    return {"argv": [str(a) for a in argv], "exit_code": code, "cpu_seconds": cpu,
            "wall_seconds": time.monotonic() - start,
            "memory_peak_bytes": after.ru_maxrss * 1024,
            "memory_peak_note": "POSIX ru_maxrss of children: largest child RSS seen by this controller so far (upper bound for this run)",
            "limit_event": limit_event, "all_job_processes_quiescent": quiescent,
            "resource_provider": "POSIX rlimit CPU/AS + wall timeout + process-group kill; not cgroup",
            "stdout": str(directory / "stdout.log"), "stderr": str(directory / "stderr.log")}


def _register_defaults():
    from implementation.solvers.ngspice import NgspiceAdapter
    register("ngspice", NgspiceAdapter)
    register("calculix", lambda: PlannedAdapter("calculix", "structural-fea/solid-thermal",
                                                 "Inputs prepared in reality_solid_inputs.py; no native run integrated here yet."))
    register("openfoam", lambda: PlannedAdapter("openfoam", "cfd", "Planned; no adapter."))
    register("mujoco", lambda: PlannedAdapter("mujoco", "rigid-body-dynamics",
                                               "Existing runs go through reality.py / reality_linux.py; not yet wrapped by this interface."))


_register_defaults()
