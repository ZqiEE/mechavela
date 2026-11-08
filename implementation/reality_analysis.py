"""Receipt-bound SI analysis of two actual electromechanical exchanges.

No solver, budget initialization, design edits or repair decisions occur here.
The standalone worker must be launched by the existing Stage provider. The
reference mapping is deliberately limited to the declared maxon/disk test.
"""
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import sys
import xml.etree.ElementTree as ET

REQUEST_SCHEMA = "rlsok.coupling-analysis-request/1"
RUN_SCHEMA = "rlsok.reality-analysis-run/1"
RESULT_SCHEMA = "rlsok.coupling-analysis-result/1"


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1048576), b""):
            h.update(block)
    return h.hexdigest()


def write(path, value):
    Path(path).write_text(json.dumps(value, indent=2, allow_nan=False), encoding="utf-8")


def binding(path):
    path = Path(path).resolve()
    if path.is_symlink() or not path.is_file():
        raise ValueError("REGULAR_ANALYSIS_INPUT_REQUIRED")
    return {"path": str(path), "sha256": sha(path)}


def bound(value):
    if set(value) != {"path", "sha256"} or binding(value["path"]) != value:
        raise ValueError("ANALYSIS_INPUT_IDENTITY_CHANGED")
    return Path(value["path"])


def close(actual, expected, name):
    if type(actual) not in (int, float) or not math.isfinite(actual) or not math.isclose(actual, expected, rel_tol=1e-10, abs_tol=1e-13):
        raise ValueError("PHYSICAL_PARAMETER_MISMATCH: " + name)


def validate(request):
    if set(request) != {"schema", "implementation_root", "implementation_bindings", "motor_parameters", "previous", "current", "limits"} or request["schema"] != REQUEST_SCHEMA:
        raise ValueError("EXACT_COUPLING_ANALYSIS_REQUEST_REQUIRED")
    for key in ("previous", "current"):
        if set(request[key]) != {"electrical_run", "mechanical_run"}:
            raise ValueError("TWO_COMPLETE_NATIVE_EXCHANGES_REQUIRED")
    if any(request["previous"][key] == request["current"][key] for key in ("electrical_run", "mechanical_run")):
        raise ValueError("DISTINCT_SUCCESSIVE_NATIVE_EXCHANGES_REQUIRED")
    limits = request["limits"]
    expected = {"stop_seconds": .1, "minimum_angle_rad": .25, "maximum_current_A": .5,
                "voltage_residual_V": .001, "velocity_residual_rad_per_s": .01}
    if set(limits) != set(expected):
        raise ValueError("EXACT_DECLARED_ACCEPTANCE_REQUIRED")
    for name, value in expected.items():
        close(limits[name], value, name)
    bound(request["motor_parameters"])
    root = Path(request["implementation_root"]).resolve()
    required = [root / "implementation" / name for name in
                ("reality.py", "reality_coupling.py", "reality_linux.py", "completion.py")]
    if any(request["implementation_bindings"].get(str(path)) != sha(path) for path in required):
        raise ValueError("FROZEN_VERIFICATION_IMPLEMENTATION_REQUIRED")
    for name, digest in request["implementation_bindings"].items():
        if sha(name) != digest:
            raise ValueError("VERIFICATION_IMPLEMENTATION_CHANGED")
    return root


def _netlist(path, motor):
    """Validate this explicit series topology, retaining only the BEMF statement."""
    logical = []
    for raw in path.read_text(encoding="utf-8-sig").splitlines()[1:]:
        text = raw.strip()
        if not text or text.startswith("*"):
            continue
        if text.startswith("+"):
            if not logical:
                raise ValueError("SPICE_CONTINUATION_WITHOUT_SOURCE")
            logical[-1] += " " + text[1:].strip()
        else:
            logical.append(text)
    rows = {}
    for line in logical:
        name = line.split()[0].lower()
        if name in rows:
            raise ValueError("DUPLICATE_MOTOR_CIRCUIT_ELEMENT")
        rows[name] = line
    if set(rows) != {"vdrive", "rdriver", "rarm", "larm", "vsense", "vback", ".end"}:
        raise ValueError("DECLARED_SERIES_MOTOR_TOPOLOGY_REQUIRED")
    expected = {"rdriver": ("supply", "terminal", None), "rarm": ("terminal", "winding", motor["terminal_resistance_ohm"]),
                "larm": ("winding", "currentnode", motor["terminal_inductance_H"]), "vsense": ("currentnode", "emf", 0.0)}
    for name, (a, b, value) in expected.items():
        parts = rows[name].split()
        if len(parts) != 4 or [x.lower() for x in parts[1:3]] != [a, b]:
            raise ValueError("MOTOR_CIRCUIT_ORIENTATION_CHANGED")
        number = float(parts[3])
        if value is not None:
            close(number, value, name)
        elif not math.isfinite(number) or number <= 0:
            raise ValueError("POSITIVE_FINITE_DRIVER_RESISTANCE_REQUIRED")
    normalized = re.sub(r"\s+", " ", rows["vdrive"]).lower()
    if normalized != "vdrive supply 0 pulse(0 3 0 1n 1n 1 2)":
        raise ValueError("DECLARED_3V_DRIVE_AND_RISE_TIME_REQUIRED")
    if rows["vback"].split()[:3] != ["Vback", "emf", "0"]:
        raise ValueError("OPPOSING_BACK_EMF_ORIENTATION_REQUIRED")
    return rows, float(rows["rdriver"].split()[3])


def _model(trajectory, model_path, result, motor, parameters):
    load = parameters["designed_load"]
    for key, value in {"mass_kg": .02, "radius_m": .1, "thickness_m": .01, "gear_ratio": 1.0}.items():
        close(load[key], value, "load." + key)
    axial = load["mass_kg"] * load["radius_m"] ** 2 / 2
    transverse = load["mass_kg"] * (3 * load["radius_m"] ** 2 + load["thickness_m"] ** 2) / 12
    close(load["axial_inertia_kg_m2"], axial, "declared disk inertia")
    close(load["transverse_inertia_kg_m2"], transverse, "declared transverse inertia")
    body = trajectory["model"]["bodies"]["load"]
    joint = trajectory["model"]["joints"]["shaft"]
    close(body["mass_kg"], load["mass_kg"], "native load mass")
    for actual, expected in zip(body["principal_inertia_kg_m2"], [transverse, transverse, axial]):
        close(actual, expected, "native inertia")
    if len(body["principal_inertia_kg_m2"]) != 3 or joint["type"] != "hinge" or joint["axis_body"] != [0, 0, 1]:
        raise ValueError("DECLARED_HINGE_AND_INERTIA_REQUIRED")
    close(joint["armature_kg_m2"], motor["rotor_inertia_kg_m2"], "native rotor inertia")
    close(joint["damping_Nm_s_per_rad"], 0, "ideal damping")
    close(joint["frictionloss_Nm"], 0, "ideal friction")
    xml = ET.fromstring(model_path.read_text(encoding="utf-8-sig"))
    dt = float(xml.find("option").attrib["timestep"])
    close(dt, result["metrics"]["timestep_seconds"], "native timestep")
    if not 0 < dt <= .0001:
        raise ValueError("FROZEN_TIMESTEP_EXCEEDS_REFERENCE_RESOLUTION")
    return dt


def analyze(request, output=None):
    root = validate(request)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from implementation import reality, reality_coupling as coupling
    parameter_path = bound(request["motor_parameters"])
    motor, reference = coupling.motor_parameters(parameter_path)
    parameters = read(parameter_path)
    stop = request["limits"]["stop_seconds"]
    evidence = {"schema": "rlsok.coupling-analysis-evidence/1", "motor": reference, "sources": {}, "files": {}}
    waves, traces, circuits, time_steps, model_hashes, identities, electrical_steps = {}, {}, {}, [], [], [], []
    for pair_name in ("previous", "current"):
        pair = request[pair_name]
        for engine, field in (("ngspice", "electrical_run"), ("mujoco", "mechanical_run")):
            directory = Path(pair[field]).resolve()
            verification = reality.verify(directory)
            if verification["engineering_acceptance"] not in ("PASS_SCOPED_SIMULATION", "FAIL"):
                raise ValueError("COMPLETE_NATIVE_NUMERICAL_RESULT_REQUIRED")
            manifest, result = read(directory / "run.json"), read(directory / "result.json")
            identity = manifest["session"].get("identity", manifest["session"])
            if not isinstance(identity.get("goal"), str) or not identity["goal"] or type(identity.get("version")) is not int:
                raise ValueError("NATIVE_COMPLETION_IDENTITY_REQUIRED")
            identities.append((identity["goal"], identity["version"]))
            if result["engine"] != engine or request["motor_parameters"]["sha256"] not in [b["sha256"] for b in manifest["inputs"].values()]:
                raise ValueError("SHARED_MOTOR_PARAMETER_IDENTITY_REQUIRED")
            prefix = pair_name + "-" + engine
            evidence["sources"][prefix] = {"file_index": binding(directory / "files.json"), "receipt": binding(directory / "execution.json"), "verification": verification}
            index = read(directory / "files.json")["files"]
            # Deliver full, byte-identical source evidence as collision-free flat files.
            for number, (name, digest) in enumerate(sorted({**index, "files.json": sha(directory / "files.json")}.items())):
                source = directory / name
                suffix = source.suffix if re.fullmatch(r"\.[A-Za-z0-9]{1,12}", source.suffix) else ".bin"
                flat = prefix + "-" + str(number).zfill(4) + suffix
                if sha(source) != digest:
                    raise ValueError("SOURCE_CHANGED_DURING_ANALYSIS")
                evidence["files"][flat] = {"source": binding(source), "relative_source": name, "source_run": prefix}
                if output is not None:
                    shutil.copyfile(source, Path(output) / flat)
                    if sha(Path(output) / flat) != digest:
                        raise ValueError("EVIDENCE_COPY_CHANGED")
            if engine == "ngspice":
                native_analysis = manifest["request"]["parameters"]["analysis"]
                if native_analysis["kind"] != "transient":
                    raise ValueError("NATIVE_TRANSIENT_REQUIRED")
                close(native_analysis["stop_seconds"], stop, "circuit duration")
                electrical_steps.append(native_analysis["step_seconds"])
                circuits[pair_name] = _netlist(directory / "project" / manifest["request"]["project"]["entry"], motor)
                actual, _ = coupling.load_verified_artifact(directory, "native/vectors.json", engine)
                traces[pair_name] = actual["vectors"]
            else:
                artifact = "trajectory.json" if "trajectory.json" in index else "native/trajectory.json"
                trajectory, _ = coupling.load_verified_artifact(directory, artifact, engine)
                model_path = directory / "project" / manifest["request"]["project"]["entry"]
                time_steps.append(_model(trajectory, model_path, result, motor, parameters))
                close(trajectory["frames"][-1]["time_seconds"], stop, "mechanical duration")
                model_hashes.append(sha(model_path))
                expected_torque = coupling.current_to_torque(pair["electrical_run"], parameter_path, stop_seconds=stop)
                schedule = read(directory / "project" / manifest["request"]["parameters"]["torque_schedule_file"])
                if schedule != expected_torque["schedule"]:
                    raise ValueError("MECHANICAL_TORQUE_NOT_FROM_PAIRED_NATIVE_CURRENT")
                waves[pair_name] = coupling.trajectory_to_back_emf(directory, parameter_path, stop_seconds=stop,
                                    source_name="Vback", positive_node="emf", negative_node="0", relative_artifact=artifact)
                if pair_name == "current":
                    final_angle = trajectory["frames"][-1]["joints"]["shaft"]["position_rad"]
    if len(set(identities)) != 1:
        raise ValueError("NATIVE_RUNS_BELONG_TO_DIFFERENT_COMPLETION_IDENTITIES")
    if (model_hashes[0] != model_hashes[1] or time_steps[0] != time_steps[1] or electrical_steps[0] != electrical_steps[1]
            or circuits["previous"][1] != circuits["current"][1]):
        raise ValueError("DESIGN_OR_TIMESTEP_CHANGED_WITHIN_CONVERGENCE_PAIR")
    expected_bemf = " ".join(line.lstrip("+ ") for line in waves["previous"]["source_statement"].splitlines())
    if re.sub(r"\s+", " ", circuits["current"][0]["vback"]) != expected_bemf:
        raise ValueError("CURRENT_CIRCUIT_NOT_DRIVEN_BY_PREVIOUS_NATIVE_BACK_EMF")
    residual = coupling.coupling_residual(waves["previous"], waves["current"], stop_seconds=stop,
                voltage_tolerance_V=request["limits"]["voltage_residual_V"], angular_velocity_tolerance_rad_per_s=request["limits"]["velocity_residual_rad_per_s"])
    current = traces["current"]["vsense#branch"]
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in [*current, final_angle]):
        raise ValueError("FINITE_NATIVE_ACCEPTANCE_NUMBERS_REQUIRED")
    metrics = {"final_angle_rad": final_angle, "maximum_absolute_current_A": max(abs(v) for v in current),
               "voltage_residual_V": residual["max_voltage_difference_V"], "velocity_residual_rad_per_s": residual["max_angular_velocity_difference_rad_per_s"],
               "driver_resistance_ohm": circuits["current"][1], "mechanical_timestep_seconds": time_steps[0], "stop_seconds": stop}
    checks = [{"metric": key, "op": op, "value": request["limits"][limit], "actual": metrics[key],
               "passed": metrics[key] >= request["limits"][limit] if op == "ge" else metrics[key] <= request["limits"][limit]}
              for key, op, limit in (("final_angle_rad", "ge", "minimum_angle_rad"), ("maximum_absolute_current_A", "le", "maximum_current_A"),
                                   ("voltage_residual_V", "le", "voltage_residual_V"), ("velocity_residual_rad_per_s", "le", "velocity_residual_rad_per_s"))]
    result = {"schema": RESULT_SCHEMA, "engine": "derived-electromechanical-analysis", "engineering_acceptance": "PASS_SCOPED_SIMULATION" if all(c["passed"] for c in checks) else "FAIL",
              "scope": "Declared 3 V maxon118743 ideal direct-drive disk model; two actual solver exchanges and explicit SI coupling consistency.",
              "metrics": metrics, "checks": checks, "coupling_residual": residual, "assumptions": parameters["assumptions"],
              "source_acceptance": {name: value["verification"]["engineering_acceptance"] for name, value in evidence["sources"].items()},
              "time_endpoint_policy": coupling.ENDPOINT_POLICY,
              "acceptance_method": "Independently recomputed physical checks over sealed native traces; source PASS/FAIL flags and original strict floating-point checks are preserved, never rewritten.",
              "limitations": ["Successive waveform convergence only; no timestep convergence or measured hardware validation.", "Manufacturer constants and explicitly idealized disk/contact model only."],
              "professional_solver_executions_launched": 0, "user_goal_achieved": False}
    if output is not None:
        write(Path(output) / "analysis-evidence.json", evidence)
        write(Path(output) / "result.json", result)
    return result, evidence


def required_input_bindings(request):
    """Bindings for existing Completion design registration and Stage admission."""
    values = {"motor_parameters": request["motor_parameters"]}
    for pair in ("previous", "current"):
        for kind in ("electrical", "mechanical"):
            directory = Path(request[pair][kind + "_run"])
            values[pair + "_" + kind + "_index"] = binding(directory / "files.json")
            if pair == "current":
                for index, value in enumerate(read(directory / "run.json")["inputs"].values()):
                    values["current_" + kind + "_design_" + str(index)] = value
    # Identical parameter files may be shared by both engineering projects.
    unique = {}
    for key, value in values.items():
        if value not in unique.values():
            unique[key] = value
    return unique


def verify_analysis(directory):
    """Verify the existing Stage receipt and independently recompute derived checks."""
    from implementation.completion import digest
    directory = Path(directory).resolve()
    manifest, receipt, result = (read(directory / name) for name in ("run.json", "execution.json", "result.json"))
    if manifest["schema"] != RUN_SCHEMA:
        raise ValueError("ANALYSIS_RUN_SCHEMA_REQUIRED")
    index = read(directory / "files.json")
    actual_files = {p.relative_to(directory).as_posix() for p in directory.rglob("*") if p.is_file() and p != directory / "files.json"}
    if set(index["files"]) != actual_files or any(sha(directory / n) != h for n, h in index["files"].items()):
        raise ValueError("ANALYSIS_FILE_SEAL_CHANGED")
    session = manifest["session"]
    engine = Path(session["engine_root"])
    plan = read(engine / "plan.json")
    descriptor = read(engine / "cli-plans" / (session["stage"] + ".json"))
    stage = read(engine / "resources.json")["stages"][session["stage"]]
    spec = descriptor["spec"]
    job = spec["jobs"][0]
    if (descriptor["descriptor_sha256"] != digest({k:v for k,v in descriptor.items() if k != "descriptor_sha256"})
            or descriptor["workflow_plan_sha256"] != sha(engine / "plan.json") or plan["matrix_sha256"] != sha(engine / "matrix.json")
            or plan["authorization_sha256"] != sha(Path(session["base"]) / "authorization.json")
            or stage["state"] != "SETTLED" or stage["cli_plan_sha256"] != descriptor["descriptor_sha256"]
            or len(stage["jobs"]) != 1 or stage["jobs"][0]["receipt_sha256"] != sha(directory / "execution.json")):
        raise ValueError("ANALYSIS_STAGE_IDENTITY_MISMATCH")
    calls = receipt["actual_native_calls"]
    expected_exit = {"PASS_SCOPED_SIMULATION":0, "FAIL":2}.get(result.get("engineering_acceptance"))
    if (len(calls) != 1 or not calls[0]["all_job_processes_quiescent"] or calls[0]["limit_event"]
            or not receipt["all_job_processes_quiescent"] or receipt["exit_code"] != expected_exit or calls[0]["exit_code"] != expected_exit
            or stage["jobs"][0]["cost"] != receipt["controller_cost"] or stage["jobs"][0]["state"] != receipt["state"]):
        raise ValueError("ANALYSIS_NATIVE_RECEIPT_INCOMPLETE")
    expected_binding = {"stage":session["stage"], "label":session["label"], "cap":spec["stage_cap"], "payload_sha256":job["payload_sha256"]}
    if (receipt["execution_binding"] != expected_binding or stage["cap"] != spec["stage_cap"] or job["cap"] != stage["cap"]
            or len(spec["jobs"]) != 1 or stage["jobs"][0]["label"] != session["label"]
            or job["payload_sha256"] != manifest["worker"]["sha256"] or sha(directory / "verification-recipe.py") != job["payload_sha256"]):
        raise ValueError("ANALYSIS_WORKER_BINDING_MISMATCH")
    expected_tools = {p:h for p,h in manifest["tools"].items() if p != manifest["worker"]["path"]}
    expected_tools[job["payload"]] = job["payload_sha256"]
    if spec["identity_hashes"] != expected_tools:
        raise ValueError("ANALYSIS_STAGE_TOOL_IDENTITY_MISMATCH")
    if calls[0]["argv"] != [manifest["python"], "-I", "-B", job["payload"], manifest["request_source"]["path"]]:
        raise ValueError("ANALYSIS_INVOCATION_CHANGED")
    for name, value in receipt["artifact_sha256"].items():
        if sha(directory / name) != value:
            raise ValueError("ANALYSIS_STAGE_ARTIFACT_CHANGED")
    request_path = bound(manifest["request_source"])
    request = read(request_path)
    if any(value not in manifest["inputs"].values() for value in required_input_bindings(request).values()):
        raise ValueError("FINAL_DESIGN_AND_NATIVE_INDEX_BINDINGS_REQUIRED")
    if request != manifest["request"] or result != read(directory / "native/result.json"):
        raise ValueError("ANALYSIS_REQUEST_OR_RESULT_CHANGED")
    expected_result, evidence = analyze(request)
    if result != expected_result or read(directory / "analysis-evidence.json") != evidence:
        raise ValueError("DERIVED_NUMERICAL_CHECK_MISMATCH")
    for name, value in evidence["files"].items():
        if receipt["artifact_sha256"].get(name) != value["source"]["sha256"] or sha(directory / name) != value["source"]["sha256"]:
            raise ValueError("ANALYSIS_DELIVERED_SOURCE_EVIDENCE_CHANGED")
    binds = {item["source"]:item["sha256"] for item in job["binds"]}
    for value in [*manifest["inputs"].values(), manifest["request_source"]]:
        bound(value)
        if binds.get(value["path"]) != value["sha256"]:
            raise ValueError("ANALYSIS_DESIGN_NOT_BOUND_TO_STAGE")
    return {"status":"FILES_AND_NATIVE_RECEIPT_VERIFIED", "engineering_acceptance":result["engineering_acceptance"],
            "scope":result["scope"], "user_goal_achieved":False, "files_sha256":sha(directory / "files.json")}


def completion_analysis_check(requirement, source, inputs):
    from implementation.completion import assertion, bound_file
    if source != {"root":source["root"], "stage":"REALITY", "label":"native", "artifact":"result.json"}:
        raise ValueError("EXACT_REALITY_EVIDENCE_SOURCE_REQUIRED")
    directory = Path(source["root"]).resolve()
    verify_analysis(directory)
    manifest, receipt, result = (read(directory / name) for name in ("run.json", "execution.json", "result.json"))
    bindings = [*manifest["inputs"].values(), manifest["request_source"]]
    supplied = [inputs[name] for name in requirement["inputs"]]
    if len(supplied) != len(bindings) or any(value not in supplied for value in bindings):
        raise ValueError("COMPLETE_ANALYSIS_DESIGN_BINDINGS_REQUIRED")
    for value in supplied:
        bound_file(value)
    if requirement["tools"].get(manifest["worker"]["path"]) != manifest["worker"]["sha256"]:
        raise ValueError("FROZEN_ANALYSIS_WORKER_REQUIRED")
    for path, value in requirement["tools"].items():
        bound_file({"path":path, "sha256":value})
        if manifest["tools"].get(path) != value:
            raise ValueError("ANALYSIS_TOOL_IDENTITY_CHANGED")
    binding_value = {"evidence":{"path":str(directory / "result.json"), "sha256":sha(directory / "result.json")},
                     "receipt_sha256":sha(directory / "execution.json"), "verification_recipe":manifest["worker"],
                     "execution_identity":{**receipt["execution_binding"], "binds":[{"source":b["path"], "sha256":b["sha256"]} for b in bindings], "tool_identity_hashes":requirement["tools"]},
                     "additional_evidence":{name:{"path":str(directory/name), "sha256":value} for name,value in receipt["artifact_sha256"].items()
                                            if name not in ("result.json", "verification-recipe.py")}}
    passed = result["engineering_acceptance"] == "PASS_SCOPED_SIMULATION" and requirement["rule"]["format"] == "json" and all(assertion(result, item) for item in requirement["rule"]["assertions"])
    return {"state":"PASS" if passed else "FAILED", **binding_value}


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        raise ValueError("ONE_FROZEN_ANALYSIS_REQUEST_REQUIRED")
    result, _ = analyze(read(args[0]), Path.cwd())
    print(json.dumps(result, allow_nan=False))
    return 0 if result["engineering_acceptance"] == "PASS_SCOPED_SIMULATION" else 2


if __name__ == "__main__":
    raise SystemExit(main())
