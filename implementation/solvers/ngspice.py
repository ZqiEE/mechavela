"""ngspice adapter for the generic solver interface.

The native shared library (KiCad's ngspice.dll on Windows, libngspice.so on
Linux) is loaded only inside ``ngspice_worker.py`` running as a bounded child
process. This module validates inputs, launches the worker and reads back the
worker's files. It never computes circuit physics itself.
"""
from __future__ import annotations

import gzip
import json
import os
from pathlib import Path
import sys

from implementation.solver_interface import (MODEL_REJECTED, RESOURCE_LIMIT, SUCCEEDED, TOOL_ERROR,
                                             ResourceCaps, SolverAdapter, SolverRun, ToolIdentity,
                                             read_json, run_bounded, seal_directory, sha256_file,
                                             write_json)

ROOT = Path(__file__).resolve().parents[2]
WORKER = Path(__file__).resolve().with_name("ngspice_worker.py")
LOCK = ROOT / "toolchain-cache" / "reality-engines.lock.json"
SIMULATION_FAILED = "SIMULATION_FAILED"  # model accepted, analysis did not converge/finish
LINUX_CANDIDATES = ("/opt/ngspice46/lib/libngspice.so", "/usr/lib/x86_64-linux-gnu/libngspice.so.0",
                    "/usr/lib/libngspice.so.0", "/usr/local/lib/libngspice.so")
MAX_POINTS = 2_000_000


def locate_library():
    """Return (path, expected_sha256 or None, source)."""
    explicit = os.environ.get("RLSOK_NGSPICE_LIBRARY")
    if explicit:
        return Path(explicit), None, "RLSOK_NGSPICE_LIBRARY"
    if LOCK.is_file():
        lock = read_json(LOCK)
        engine = lock.get("engines", {}).get("ngspice")
        if engine and Path(engine["library"]).is_file():
            return Path(engine["library"]), engine["files"].get(engine["library"]), "toolchain-cache/reality-engines.lock.json"
    for candidate in LINUX_CANDIDATES:
        if Path(candidate).is_file():
            return Path(candidate), None, "standard install path"
    raise ValueError("NGSPICE_LIBRARY_NOT_FOUND: set RLSOK_NGSPICE_LIBRARY")


def provenance():
    """How the native library was obtained (never inferred from its file name)."""
    library, expected, source = locate_library()
    record = {"library_source": source, "expected_sha256": expected}
    explicit = os.environ.get("RLSOK_NGSPICE_PROVENANCE")
    if explicit and Path(explicit).is_file():
        record["record"] = {"path": str(Path(explicit).resolve()), "sha256": sha256_file(explicit),
                            "content": read_json(explicit)}
    elif source == "toolchain-cache/reality-engines.lock.json":
        record["record"] = {"path": str(LOCK), "sha256": sha256_file(LOCK),
                            "content": read_json(LOCK)["engines"]["ngspice"].get("source")}
    return record


def validate_analysis(analysis):
    if not isinstance(analysis, dict):
        raise ValueError("ANALYSIS_OBJECT_REQUIRED")
    kind = analysis.get("kind")
    if kind == "op":
        if set(analysis) != {"kind"}:
            raise ValueError("OP_TAKES_NO_PARAMETERS")
    elif kind == "tran":
        allowed = {"kind", "step_seconds", "stop_seconds", "start_seconds", "max_step_seconds", "uic"}
        if set(analysis) - allowed or not {"step_seconds", "stop_seconds"} <= set(analysis):
            raise ValueError("TRAN_REQUIRES_STEP_AND_STOP")
        for key in ("step_seconds", "stop_seconds", "start_seconds", "max_step_seconds"):
            if key in analysis:
                value = analysis[key]
                if type(value) not in (int, float) or not 0 <= value <= 1e4:
                    raise ValueError("TRAN_TIME_OUT_OF_RANGE: " + key)
        if not 0 < analysis["step_seconds"] <= analysis["stop_seconds"]:
            raise ValueError("TRAN_STEP_STOP_ORDER")
        if "max_step_seconds" in analysis and analysis["max_step_seconds"] <= 0:
            raise ValueError("TRAN_MAX_STEP_POSITIVE")
        if analysis["stop_seconds"] / min(analysis["step_seconds"], analysis.get("max_step_seconds", analysis["step_seconds"])) > MAX_POINTS:
            raise ValueError("TRAN_POINT_LIMIT")
        if "uic" in analysis and type(analysis["uic"]) is not bool:
            raise ValueError("UIC_BOOLEAN")
    elif kind == "ac":
        if set(analysis) != {"kind", "sweep", "points", "start_hz", "stop_hz"}:
            raise ValueError("AC_REQUIRES_sweep_points_start_hz_stop_hz")
        if analysis["sweep"] not in ("dec", "oct", "lin"):
            raise ValueError("AC_SWEEP_DEC_OCT_LIN")
        if type(analysis["points"]) is not int or not 1 <= analysis["points"] <= 10000:
            raise ValueError("AC_POINTS_1_TO_10000")
        for key in ("start_hz", "stop_hz"):
            if type(analysis[key]) not in (int, float) or not 0 < analysis[key] <= 1e12:
                raise ValueError("AC_FREQUENCY_OUT_OF_RANGE: " + key)
        if not analysis["start_hz"] < analysis["stop_hz"]:
            raise ValueError("AC_START_LT_STOP")
    else:
        raise ValueError("ANALYSIS_KIND_MUST_BE_op_tran_OR_ac")


def locate_python():
    explicit = os.environ.get("RLSOK_WORKER_PYTHON")
    if explicit:
        return explicit
    if os.name == "nt" and LOCK.is_file():
        return read_json(LOCK)["python"]
    return sys.executable


class NgspiceAdapter(SolverAdapter):
    name = "ngspice"
    domain = "circuit"
    state = "AVAILABLE"

    def __init__(self):
        self.library, self.expected_sha, self.library_source = locate_library()
        self._identity = None

    def describe(self):
        return {"solver": self.name, "domain": self.domain, "state": self.state,
                "library": str(self.library), "library_source": self.library_source,
                "analyses": ["op", "tran", "ac"], "inputs": "self-contained netlist (no .control/.include/file sources)",
                "provenance": provenance()}

    # -- identity ---------------------------------------------------------
    def library_sha(self):
        digest = sha256_file(self.library)
        if self.expected_sha and digest != self.expected_sha:
            raise ValueError("TOOL_IDENTITY_CHANGED: " + str(self.library))
        return digest

    def identity(self, probe_directory=None) -> ToolIdentity:
        """Version is read from the native library itself by a probe run."""
        if self._identity:
            return self._identity
        import tempfile
        base = Path(probe_directory) if probe_directory else Path(tempfile.mkdtemp(prefix="ngspice-probe-"))
        job = {"netlist": "version probe\nR1 a 0 1k\nV1 a 0 1\n.end\n", "analysis": {"kind": "op"}, "vectors": ["v(a)"]}
        run = self.run(job, base / "probe", ResourceCaps(wall_seconds=30, cpu_seconds=30), _skip_identity=True)
        if run.status != SUCCEEDED:
            raise ValueError("NGSPICE_PROBE_FAILED: " + str(run.reason))
        version = run.outputs["native_version"]
        self._identity = ToolIdentity(solver="ngspice", version=version, binary=str(self.library),
                                      binary_sha256=self.library_sha(), backend="shared-library (ctypes)",
                                      evidence="ngspice 'version' command output in native probe run",
                                      provenance=provenance())
        return self._identity

    # -- validation -------------------------------------------------------
    def validate(self, job):
        from implementation.reality_worker import validate_spice
        if set(job) - {"netlist", "analysis", "vectors"} or not {"netlist", "analysis", "vectors"} <= set(job):
            raise ValueError("NGSPICE_JOB_SCHEMA: netlist, analysis, vectors required")
        validate_spice(job["netlist"])  # existing project filter: data-only netlist
        analysis = job["analysis"]
        validate_analysis(analysis)
        vectors = job["vectors"]
        import re
        if not isinstance(vectors, list) or not 1 <= len(vectors) <= 128 or any(
                not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9_().#:-]{1,96}", v) for v in vectors):
            raise ValueError("INVALID_VECTOR_LIST")

    # -- execution --------------------------------------------------------
    def run(self, job, directory, caps: ResourceCaps, _skip_identity=False) -> SolverRun:
        directory = Path(directory)
        try:
            self.validate(job)
        except ValueError as error:
            directory.mkdir(parents=True, exist_ok=False)
            write_json(directory / "rejected.json", {"status": MODEL_REJECTED, "reason": str(error), "stage": "preflight"})
            seal_directory(directory)
            return SolverRun("ngspice", MODEL_REJECTED, str(directory), None, 0.0, None, None, {}, reason=str(error),
                             files=read_json(directory / "files.json")["files"])
        library_sha = self.library_sha()
        staging = directory.parent / (directory.name + ".input")
        staging.mkdir(parents=True, exist_ok=False)
        (staging / "netlist.cir").write_text(job["netlist"], encoding="utf-8")
        write_json(staging / "job.json", {**job, "library": str(self.library), "library_sha256": library_sha})
        env = {"OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "PYTHONIOENCODING": "utf-8",
               "PYTHONPATH": str(ROOT)}
        for key in ("SystemRoot", "WINDIR", "PATH", "LD_LIBRARY_PATH"):
            if key in os.environ:
                env[key] = os.environ[key]
        argv = [locate_python(), "-I", "-B", str(WORKER), str(staging / "job.json")]
        execution = run_bounded(argv, directory, caps, env=env)
        # move inputs inside the sealed run directory
        for name in ("netlist.cir", "job.json"):
            (staging / name).replace(directory / ("input-" + name))
        staging.rmdir()
        execution["inputs_relocated"] = {"from": str(staging), "to": ["input-netlist.cir", "input-job.json"],
                                         "note": "worker read the job from the staging path in argv; files moved into this sealed run directory unchanged"}
        write_json(directory / "execution.json", execution)
        result_path = directory / "result.json"
        result = read_json(result_path) if result_path.is_file() else None
        if execution["limit_event"]:
            status, reason = RESOURCE_LIMIT, execution["limit_event"]
        elif result is None:
            status, reason = TOOL_ERROR, "NO_WORKER_RESULT (exit %s)" % execution["exit_code"]
        else:
            status, reason = result["status"], result.get("reason")
            if status == SUCCEEDED and execution["exit_code"] != 0:
                status, reason = TOOL_ERROR, "NONZERO_EXIT_WITH_SUCCESS_RESULT"
        outputs = {}
        if status == SUCCEEDED:
            with gzip.open(directory / "vectors.json.gz", "rt", encoding="utf-8") as stream:
                data = json.load(stream)
            outputs["vectors"] = data["vectors"]
            outputs["complex_vectors"] = data.get("complex_vectors", {})
            outputs["native_version"] = result["native_version"]
            outputs["point_count"] = result["point_count"]
        files = seal_directory(directory)
        tool = None if _skip_identity else self.identity().as_dict()
        return SolverRun("ngspice", status, str(directory), execution["exit_code"], execution["wall_seconds"],
                         execution.get("cpu_seconds"), tool, outputs, reason=reason,
                         limit_event=execution["limit_event"], files=files)
