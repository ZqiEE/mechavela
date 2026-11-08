"""Bounded child process: load the ngspice shared library, run one analysis.

Standalone (stdlib only, runs under ``python -I``). Writes, in the current
directory: result.json and vectors.json.gz. Mirrors the ctypes calling pattern
already used by implementation/reality_worker.py, so the same KiCad
ngspice.dll works on Windows and libngspice.so on Linux.

The analysis card is built here from the frozen job (op / tran / ac); the
netlist itself is data only (validated by reality_worker.validate_spice before
this process starts). Physics is computed by ngspice; this file only copies
the native vectors out.
"""
import ctypes as ct
import gzip
import json
import math
import os
from pathlib import Path
import re
import sys
import traceback

MAX_POINTS = 2_000_000


class VectorInfo(ct.Structure):
    _fields_ = [("name", ct.c_char_p), ("type", ct.c_int), ("flags", ct.c_short),
                ("real", ct.POINTER(ct.c_double)), ("complex", ct.c_void_p), ("length", ct.c_int)]


class NgComplex(ct.Structure):
    _fields_ = [("re", ct.c_double), ("im", ct.c_double)]


def write(name, value):
    Path(name).write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")


def analysis_command(analysis):
    kind = analysis["kind"]
    if kind == "op":
        return "op"
    if kind == "tran":
        command = "tran {:.17g} {:.17g}".format(analysis["step_seconds"], analysis["stop_seconds"])
        if "start_seconds" in analysis or "max_step_seconds" in analysis:
            command += " {:.17g}".format(analysis.get("start_seconds", 0))
        if "max_step_seconds" in analysis:
            command += " {:.17g}".format(analysis["max_step_seconds"])
        if analysis.get("uic"):
            command += " uic"
        return command
    if kind == "ac":
        return "ac {} {:d} {:.17g} {:.17g}".format(analysis["sweep"], analysis["points"],
                                                   analysis["start_hz"], analysis["stop_hz"])
    raise ValueError("UNSUPPORTED_ANALYSIS: " + str(kind))


def main():
    job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    messages, exits = [], []
    status = "TOOL_ERROR"
    try:
        library = job["library"]
        folder = os.add_dll_directory(str(Path(library).parent)) if os.name == "nt" else None
        # Isolated startup directory: no user/global spinit or scripts are read.
        startup = Path.cwd() / "spice-startup"
        startup.mkdir()
        (startup / ".spiceinit").write_text("* RLSOK isolated native run\n", encoding="ascii")
        os.environ["SPICE_USERINIT_DIR"] = str(startup)
        os.environ["SPICE_SCRIPTS"] = str(startup)
        lib = ct.CDLL(library)
        send_type = ct.CFUNCTYPE(ct.c_int, ct.c_char_p, ct.c_int, ct.c_void_p)
        exit_type = ct.CFUNCTYPE(ct.c_int, ct.c_int, ct.c_bool, ct.c_bool, ct.c_int, ct.c_void_p)

        @send_type
        def send(message, _ident, _user):
            text = message.decode("utf-8", "replace")
            messages.append(text)
            print(text, flush=True)
            return 0

        @send_type
        def status_cb(_text, _ident, _user):
            return 0

        @exit_type
        def leave(code, _immediate, _quit, _ident, _user):
            exits.append(code)
            return 0

        lib.ngSpice_Init.argtypes = [send_type, send_type, exit_type, ct.c_void_p, ct.c_void_p, ct.c_void_p, ct.c_void_p]
        lib.ngSpice_Command.argtypes = [ct.c_char_p]
        lib.ngSpice_Circ.argtypes = [ct.POINTER(ct.c_char_p)]
        lib.ngGet_Vec_Info.argtypes = [ct.c_char_p]
        lib.ngGet_Vec_Info.restype = ct.POINTER(VectorInfo)
        if lib.ngSpice_Init(send, status_cb, leave, None, None, None, None) != 0:
            raise RuntimeError("NGSPICE_INIT_FAILED")
        lib.ngSpice_Command(b"set nomoremode")
        lib.ngSpice_Command(b"version")
        version_line = next((m for m in messages if "ngspice-" in m.lower()), None)
        found = re.search(r"ngspice-(\S+)", version_line or "", re.IGNORECASE)
        version = found.group(1) if found else "UNKNOWN"
        lines = job["netlist"].splitlines()
        encoded = [line.encode("utf-8") for line in lines]
        circuit = (ct.c_char_p * (len(encoded) + 1))(*encoded, None)
        before = len(messages)
        if lib.ngSpice_Circ(circuit) != 0 or exits or any(
                m.lower().startswith(("stderr error", "stderr fatal")) for m in messages[before:]):
            status = "MODEL_REJECTED"
            raise ValueError("NGSPICE_MODEL_REJECTED: " + " | ".join(m for m in messages[before:] if m.lower().startswith("stderr"))[:2000])
        analysis = job["analysis"]
        command = analysis_command(analysis)
        print("RLSOK analysis command: " + command, flush=True)
        before = len(messages)
        code = lib.ngSpice_Command(command.encode("ascii"))
        errors = [m for m in messages[before:] if m.lower().startswith(("stderr error", "stderr fatal"))
                  or "timestep too small" in m.lower() or "simulation(s) aborted" in m.lower()
                  or "no convergence" in m.lower()]
        if code != 0 or exits or errors:
            status = "SIMULATION_FAILED"
            raise RuntimeError("NGSPICE_ANALYSIS_FAILED: " + " | ".join(errors)[:2000])
        axis = {"op": [], "tran": ["time"], "ac": ["frequency"]}[analysis["kind"]]
        vectors, complex_vectors = {}, {}
        for name in dict.fromkeys(axis + job["vectors"]):
            pointer = lib.ngGet_Vec_Info(name.encode("ascii"))
            if not pointer:
                status = "MODEL_REJECTED"
                raise ValueError("MISSING_SOLVER_VECTOR: " + name)
            vector = pointer.contents
            if not 0 < vector.length <= MAX_POINTS:
                raise ValueError("BOUNDED_VECTOR_REQUIRED: " + name)
            if vector.real:
                values = [vector.real[i] for i in range(vector.length)]
                if not all(math.isfinite(v) for v in values):
                    status = "SIMULATION_FAILED"
                    raise ValueError("NONFINITE_SOLVER_RESULT: " + name)
                vectors[name] = values
            elif vector.complex:
                data = ct.cast(vector.complex, ct.POINTER(NgComplex))
                re_part = [data[i].re for i in range(vector.length)]
                im_part = [data[i].im for i in range(vector.length)]
                if not all(math.isfinite(v) for v in re_part + im_part):
                    status = "SIMULATION_FAILED"
                    raise ValueError("NONFINITE_SOLVER_RESULT: " + name)
                if name == "frequency":
                    vectors[name] = re_part  # the AC sweep axis is stored as complex with zero imaginary part
                else:
                    complex_vectors[name] = {"re": re_part, "im": im_part}
            else:
                raise ValueError("EMPTY_SOLVER_VECTOR: " + name)
        lengths = {len(v) for v in vectors.values()} | {len(v["re"]) for v in complex_vectors.values()}
        if len(lengths) != 1:
            raise ValueError("SOLVER_VECTOR_LENGTH_MISMATCH")
        with gzip.open("vectors.json.gz", "wt", encoding="utf-8") as stream:
            json.dump({"schema": "rlsok.circuit-vectors/3", "engine": "ngspice", "analysis_command": command,
                       "units": "SPICE SI: time s, frequency Hz, node voltage V, branch current A "
                                "(positive into the + terminal of a V source); AC vectors complex (re, im)",
                       "vectors": vectors, "complex_vectors": complex_vectors}, stream)
        write("result.json", {"status": "SUCCEEDED", "native_version": version, "version_line": version_line,
                              "analysis_command": command, "point_count": lengths.pop()})
        if folder:
            folder.close()
        return 0
    except Exception as error:
        write("result.json", {"status": status, "reason": str(error), "native_messages_tail": messages[-40:]})
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
