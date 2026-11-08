"""SI waveform exchange for the sourced maxon 118743 reference experiment.

This module never runs a solver, approves a design, or modifies a design. Its
public converters require a receipt-verified native run, and return data plus
provenance to freeze into the next solver's declared project files. The motor
reference is a first supported mapping, not the platform's domain boundary.
"""
import bisect
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re


MOTOR_SOURCE = "https://www.maxongroup.com/maxon/view/product/118743"
MOTOR_VALUES = {
    "terminal_resistance_ohm": 2.18,
    "terminal_inductance_H": 0.000238,
    "torque_constant_Nm_per_A": 0.0235,
    "back_emf_constant_V_per_rad_per_s": 0.0235,
    "rotor_inertia_kg_m2": 0.00000108,
    "rated_voltage_V": 12.0,
}
SPICE_UNITS = "Native SPICE SI units: time=s, node voltage=V, branch current=A."
ENDPOINT_POLICY = ("A final timestamp within min(1e-12 s, 1e-10 * stop_seconds) of the requested stop "
                   "is normalized to that stop with its sampled value unchanged; values are never extrapolated.")


def _number(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("FINITE_NUMERIC_SI_VALUE_REQUIRED")
    return float(value)


def _read_bound(path, expected=None):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("REGULAR_EVIDENCE_FILE_REQUIRED")
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if expected is not None and digest != expected:
        raise ValueError("SOURCE_HASH_MISMATCH")
    return json.loads(data.decode("utf-8-sig")), {"path": str(path.resolve()), "sha256": digest}


def _verify_run(directory):
    # No caller-supplied 'verified=True' and no solver execution in this path.
    from .reality import verify
    return verify(directory)


def load_verified_artifact(directory, relative_artifact, engine):
    directory = Path(directory).resolve()
    relative = PurePosixPath(relative_artifact)
    if (not relative_artifact or "\\" in relative_artifact or ":" in relative_artifact
            or relative.is_absolute() or str(relative) != relative_artifact
            or any(part in (".", "..") for part in relative.parts)):
        raise ValueError("SAFE_RUN_RELATIVE_ARTIFACT_REQUIRED")
    verification = _verify_run(directory)
    if (verification.get("status") != "FILES_AND_NATIVE_RECEIPT_VERIFIED"
            or verification.get("engineering_acceptance") not in ("PASS_SCOPED_SIMULATION", "FAIL")):
        raise ValueError("COMPLETE_NATIVE_NUMERICAL_RUN_REQUIRED")
    index, index_binding = _read_bound(directory / "files.json", verification["files_sha256"])
    artifact_path = directory / relative_artifact
    if not artifact_path.resolve().is_relative_to(directory):
        raise ValueError("ARTIFACT_OUTSIDE_VERIFIED_RUN")
    if relative_artifact not in index.get("files", {}):
        raise ValueError("ARTIFACT_NOT_SEALED_IN_VERIFIED_RUN")
    value, binding = _read_bound(artifact_path, index["files"][relative_artifact])
    if value.get("engine") != engine:
        raise ValueError("SOURCE_ENGINE_MISMATCH")
    return value, {"run_root": str(directory), "file_index": index_binding,
                   "artifact": binding, "relative_artifact": relative_artifact,
                   "engineering_acceptance": verification["engineering_acceptance"]}


def motor_parameters(path, expected_sha256=None):
    """Validate the declared reference and SI conversions; never fetch a URL."""
    value, binding = _read_bound(path, expected_sha256)
    motor = value.get("motor", {})
    if (value.get("schema") != "rlsok.engineering-parameters/1"
            or motor.get("manufacturer") != "maxon" or motor.get("part_number") != "118743"
            or motor.get("source") != MOTOR_SOURCE):
        raise ValueError("SUPPORTED_SOURCED_MOTOR_REFERENCE_REQUIRED")
    for name, expected in MOTOR_VALUES.items():
        if not math.isclose(_number(motor.get(name)), expected, rel_tol=1e-12, abs_tol=0.0):
            raise ValueError("MOTOR_PARAMETER_OR_SI_CONVERSION_MISMATCH: " + name)
    if not isinstance(motor.get("parameter_provenance"), str) or not motor["parameter_provenance"].strip():
        raise ValueError("MOTOR_PARAMETER_PROVENANCE_REQUIRED")
    assumptions = value.get("assumptions")
    if not isinstance(assumptions, list) or not assumptions or any(not isinstance(x, str) or not x.strip() for x in assumptions):
        raise ValueError("EXPLICIT_MOTOR_MODEL_ASSUMPTIONS_REQUIRED")
    return motor, {"file": binding, "manufacturer_source": MOTOR_SOURCE,
                   "part_number": "118743", "parameters_SI": dict(MOTOR_VALUES),
                   "back_emf_assumption": "Ke=Kt in reciprocal SI ideal DC motor model; not a separate measurement.",
                   "assumptions": assumptions}


def _series(times, values, stop_seconds, limit=25000):
    stop = _number(stop_seconds)
    if stop <= 0 or not isinstance(times, list) or not isinstance(values, list) or not 2 <= len(times) <= limit or len(times) != len(values):
        raise ValueError("BOUNDED_MATCHED_TIME_SERIES_REQUIRED")
    times, values = [_number(x) for x in times], [_number(x) for x in values]
    if times[0] != 0.0 or any(right <= left for left, right in zip(times, times[1:])):
        raise ValueError("STRICTLY_INCREASING_TIME_FROM_ZERO_REQUIRED")
    # Repeated native integration steps can finish a few ulps below the exact
    # requested duration. Canonicalize only the final timestamp, not its value.
    # The relative bound prevents a sub-picosecond but materially incomplete
    # interval from being silently accepted as floating-point roundoff.
    tolerance = min(1e-12, stop * 1e-10)
    if abs(times[-1] - stop) <= tolerance:
        times[-1] = stop
        if times[-1] <= times[-2]:
            raise ValueError("ENDPOINT_NORMALIZATION_WOULD_COLLAPSE_TIME_GRID")
    if times[-1] < stop:
        raise ValueError("SOURCE_TIME_SERIES_DOES_NOT_COVER_INTERVAL")
    # Retain every source point in the requested interval and interpolate only
    # its endpoint. No silent decimation or extrapolation of solver output.
    end = bisect.bisect_left(times, stop)
    if times[end] == stop:
        return times[:end + 1], values[:end + 1]
    final = _interpolate(times, values, stop)
    return times[:end] + [stop], values[:end] + [final]


def _interpolate(times, values, point):
    index = bisect.bisect_left(times, point)
    if index < len(times) and times[index] == point:
        return values[index]
    if index == 0 or index == len(times):
        raise ValueError("NO_WAVEFORM_EXTRAPOLATION")
    ratio = (point - times[index - 1]) / (times[index] - times[index - 1])
    return _number(values[index - 1] + ratio * (values[index] - values[index - 1]))


def current_to_torque(directory, parameter_file, *, stop_seconds, actuator="motor",
                      current_vector="vsense#branch", relative_artifact="native/vectors.json"):
    """Return an exact worker torque schedule and separate frozen provenance."""
    if not isinstance(actuator, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", actuator):
        raise ValueError("NAMED_MOTOR_ACTUATOR_REQUIRED")
    # Native ngspice stores a voltage-source branch current as vname#branch.
    # Keep the explicit i(Vname) request spelling compatible; node voltages and
    # arbitrary device vectors must never be interpreted as amperes here.
    if not isinstance(current_vector, str) or not re.fullmatch(r"(?:[Vv][A-Za-z0-9_]+#branch|i\([Vv][A-Za-z0-9_]+\))", current_vector):
        raise ValueError("EXPLICIT_SPICE_BRANCH_CURRENT_REQUIRED")
    native, source = load_verified_artifact(directory, relative_artifact, "ngspice")
    if native.get("schema") != "rlsok.circuit-vectors/1" or native.get("units") != SPICE_UNITS:
        raise ValueError("EXPLICIT_NATIVE_SPICE_SI_UNITS_REQUIRED")
    vectors = native.get("vectors", {})
    times, current = _series(vectors.get("time"), vectors.get(current_vector), stop_seconds)
    if len(times) > 10001:
        raise ValueError("TORQUE_SAMPLE_LIMIT_REQUIRES_EXPLICIT_RESAMPLING")
    motor, reference = motor_parameters(parameter_file)
    torque = [_number(motor["torque_constant_Nm_per_A"] * value) for value in current]
    return {"schedule": {"schema": "rlsok.torque-schedule/1", "units": {"time": "s", "torque": "N*m"},
                         "time_seconds": times, "actuators": {actuator: torque}},
            "provenance": {"schema": "rlsok.coupling-provenance/1", "source": source, "motor": reference,
                           "mapping": {"input": current_vector, "input_unit": "A", "output": actuator,
                                       "output_unit": "N*m", "equation": "torque_Nm = Kt_Nm_per_A * current_A"},
                           "interval_seconds": [0.0, times[-1]], "time_endpoint_policy": ENDPOINT_POLICY,
                           "engineering_acceptance": "NOT_EVALUATED",
                           "solver_executions_launched": 0}}


def trajectory_to_back_emf(directory, parameter_file, *, stop_seconds, joint="shaft", source_name="Vbemf",
                           positive_node="back_emf", negative_node="0", relative_artifact="native/trajectory.json"):
    """Return a self-contained PWL voltage source, waveform and provenance.

    Circuit orientation must put this source in series opposing positive motor
    current. Its voltage is V(positive_node)-V(negative_node)=Ke*shaft_speed.
    """
    if not isinstance(joint, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", joint):
        raise ValueError("NAMED_HINGE_JOINT_REQUIRED")
    if (not isinstance(source_name, str) or not re.fullmatch(r"[Vv][A-Za-z0-9_]{1,63}", source_name)
            or any(not isinstance(n, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,64}", n) for n in (positive_node, negative_node))
            or positive_node.lower() == negative_node.lower()):
        raise ValueError("SAFE_DISTINCT_SPICE_SOURCE_AND_NODES_REQUIRED")
    native, source = load_verified_artifact(directory, relative_artifact, "mujoco")
    units = native.get("units", {})
    if (native.get("schema") != "rlsok.mechanical-trajectory/2" or units.get("time") != "s"
            or units.get("joint_velocity") != "rad/s" or units.get("joint_torque") != "N*m"
            or native.get("model", {}).get("joints", {}).get(joint, {}).get("type") != "hinge"):
        raise ValueError("SI_HINGE_TRAJECTORY_REQUIRED")
    frames = native.get("frames")
    if not isinstance(frames, list) or not 2 <= len(frames) <= 10001:
        raise ValueError("BOUNDED_MECHANICAL_FRAMES_REQUIRED")
    try:
        times, speed = _series([f["time_seconds"] for f in frames],
                               [f["joints"][joint]["velocity_rad_per_s"] for f in frames], stop_seconds, 10001)
    except (KeyError, TypeError) as error:
        raise ValueError("JOINT_VELOCITY_FRAME_MISSING") from error
    motor, reference = motor_parameters(parameter_file)
    voltage = [_number(motor["back_emf_constant_V_per_rad_per_s"] * value) for value in speed]
    lines = [source_name + " " + positive_node + " " + negative_node + " PWL("]
    for index in range(0, len(times), 4):
        lines.append("+ " + " ".join("{:.17g} {:.17g}".format(t, v) for t, v in zip(times[index:index + 4], voltage[index:index + 4])))
    lines.append("+ )")
    return {"source_statement": "\n".join(lines) + "\n",
            "waveform": {"schema": "rlsok.back-emf-waveform/1", "units": {"time": "s", "voltage": "V", "angular_velocity": "rad/s"},
                         "time_seconds": times, "voltage_V": voltage, "angular_velocity_rad_per_s": speed},
            "provenance": {"schema": "rlsok.coupling-provenance/1", "source": source, "motor": reference,
                           "mapping": {"input": joint, "input_unit": "rad/s", "output": source_name, "output_unit": "V",
                                       "equation": "back_emf_V = Ke_V_per_rad_per_s * angular_velocity_rad_per_s",
                                       "voltage_orientation": [positive_node, negative_node],
                                       "required_circuit_orientation": "Series source opposing positive motor current."},
                           "interval_seconds": [0.0, times[-1]], "time_endpoint_policy": ENDPOINT_POLICY,
                           "engineering_acceptance": "NOT_EVALUATED",
                           "solver_executions_launched": 0}}


def coupling_residual(previous, current, *, stop_seconds, voltage_tolerance_V, angular_velocity_tolerance_rad_per_s):
    """Compare successive native waveforms; this is not an acceptance gate."""
    tolerances = [_number(voltage_tolerance_V), _number(angular_velocity_tolerance_rad_per_s)]
    if any(value <= 0 for value in tolerances):
        raise ValueError("POSITIVE_EXPLICIT_RESIDUAL_TOLERANCES_REQUIRED")
    try:
        sources = [item["provenance"]["source"]["file_index"] for item in (previous, current)]
        references = [item["provenance"]["motor"] for item in (previous, current)]
        if sources[0]["sha256"] == sources[1]["sha256"]:
            raise ValueError("SAME_RUN_IS_NOT_A_SUCCESSIVE_ITERATION")
        if references[0] != references[1]:
            raise ValueError("MOTOR_REFERENCE_CHANGED_BETWEEN_ITERATIONS")
        series = []
        for item in (previous, current):
            waveform = item["waveform"]
            if waveform["schema"] != "rlsok.back-emf-waveform/1" or waveform["units"] != {"time": "s", "voltage": "V", "angular_velocity": "rad/s"}:
                raise ValueError("EXPLICIT_RESIDUAL_SI_UNITS_REQUIRED")
            times, volts = _series(waveform["time_seconds"], waveform["voltage_V"], stop_seconds, 10001)
            _, speed = _series(waveform["time_seconds"], waveform["angular_velocity_rad_per_s"], stop_seconds, 10001)
            ke = references[0]["parameters_SI"]["back_emf_constant_V_per_rad_per_s"]
            if any(not math.isclose(v, _number(ke) * w, rel_tol=1e-12, abs_tol=1e-15) for v, w in zip(volts, speed)):
                raise ValueError("BACK_EMF_MAPPING_CHANGED")
            series.append((times, volts, speed))
    except (KeyError, TypeError) as error:
        raise ValueError("COMPLETE_SUCCESSIVE_EXCHANGE_RECORDS_REQUIRED") from error
    points = sorted(set(series[0][0] + series[1][0]))
    residuals = [max(abs(_interpolate(series[0][0], series[0][column], t)
                         - _interpolate(series[1][0], series[1][column], t)) for t in points) for column in (1, 2)]
    within = all(value <= tolerance for value, tolerance in zip(residuals, tolerances))
    return {"schema": "rlsok.coupling-residual/1", "status": "RESIDUAL_WITHIN_TOLERANCES" if within else "RESIDUAL_EXCEEDS_TOLERANCES",
            "max_voltage_difference_V": residuals[0], "max_angular_velocity_difference_rad_per_s": residuals[1],
            "voltage_tolerance_V": tolerances[0], "angular_velocity_tolerance_rad_per_s": tolerances[1],
            "within_declared_tolerances": within, "comparison_time_points": len(points),
            "source_file_indices": sources, "interval_seconds": [0.0, _number(stop_seconds)],
            "engineering_acceptance": "NOT_EVALUATED", "solver_executions_launched": 0,
            "limitations": ["Successive piecewise-linear waveform difference only.",
                            "No timestep convergence, stability, engineering target, or physical model validation is inferred."]}
