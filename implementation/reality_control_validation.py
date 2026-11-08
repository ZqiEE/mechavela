"""Read-only checks for a finite native MuJoCo position-servo benchmark.

No solver, controller, repair, ledger or authorization is implemented here.
Closed-form responses are independent references for already sealed native
traces. A correctly detected negative remains an engineering FAIL.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

CASES = {"critical": (.01, .002, "PASS_SCOPED_SIMULATION"),
         "underdamped": (.01, .0004, "FAIL"),
         "critical_fast": (.04, .004, "PASS_SCOPED_SIMULATION")}
CONDITIONS = {"inertia_kg_m2": .0001, "target_rad": .2, "duration_s": 2.,
              "native_timestep_s": .0001, "sample_every_steps": 10,
              "integrator": "implicitfast", "gravity_m_per_s2": [0, 0, 0]}
ACCEPTANCE = {"maximum_overshoot_fraction": .05, "settled_by_s": .65,
              "settling_error_fraction": .02, "maximum_position_reference_error_rad": .001,
              "maximum_velocity_reference_error_rad_per_s": .02,
              "maximum_torque_law_error_Nm": 1e-8,
              "critical_fast_to_critical_t50_ratio_min": .48,
              "critical_fast_to_critical_t50_ratio_max": .52}
SOURCE = "https://mujoco.readthedocs.io/en/stable/XMLreference.html#actuator-position"


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def sha(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1048576), b""):
            result.update(block)
    return result.hexdigest()


def finite(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("FINITE_CONTROL_NUMBER_REQUIRED")
    return float(value)


def close(actual, expected, name, tolerance=1e-12):
    if not math.isclose(finite(actual), expected, rel_tol=tolerance, abs_tol=tolerance):
        raise ValueError("CONTROL_MODEL_MISMATCH: " + name)


def check_case(case):
    if case.get("schema") != "rlsok.control-case/1" or case.get("case_id") not in CASES:
        raise ValueError("DECLARED_CONTROL_BENCHMARK_CASE_REQUIRED")
    kp, kv, expected = CASES[case["case_id"]]
    close(case["kp_Nm_per_rad"], kp, "kp")
    close(case["kv_Nm_s_per_rad"], kv, "kv")
    if (case["shared_conditions"] != CONDITIONS or case["acceptance"] != ACCEPTANCE
            or case["expected_engineering_acceptance"] != expected or case["official_source"] != SOURCE
            or not isinstance(case.get("assumptions"), list) or not case["assumptions"]):
        raise ValueError("FROZEN_CONTROL_CONDITIONS_AND_ACCEPTANCE_REQUIRED")
    return kp, kv


def reference_state(time_seconds, inertia, kp, kv, target):
    """Closed-form rest-to-step response; never used to generate solver evidence."""
    time_seconds = finite(time_seconds)
    inertia, kp, kv, target = map(finite, (inertia, kp, kv, target))
    if time_seconds < 0 or min(inertia, kp, target) <= 0 or kv < 0:
        raise ValueError("POSITIVE_REFERENCE_PARAMETERS_REQUIRED")
    natural = math.sqrt(kp / inertia)
    ratio = kv / (2 * math.sqrt(kp * inertia))
    if math.isclose(ratio, 1., rel_tol=1e-12):
        decay = math.exp(-natural * time_seconds)
        return (target * (1 - (1 + natural * time_seconds) * decay),
                target * natural * natural * time_seconds * decay)
    if not 0 <= ratio < 1:
        raise ValueError("REFERENCE_ONLY_SUPPORTS_DECLARED_CRITICAL_AND_UNDERDAMPED_CASES")
    damped = natural * math.sqrt(1 - ratio * ratio)
    decay = math.exp(-ratio * natural * time_seconds)
    phase = damped * time_seconds
    return (target * (1 - decay * (math.cos(phase) + ratio * natural / damped * math.sin(phase))),
            target * natural * natural / damped * decay * math.sin(phase))


def _numbers(text):
    return [float(value) for value in text.split()]


def validate_model_xml(path, case):
    kp, kv = check_case(case)
    root = ET.fromstring(Path(path).read_text(encoding="utf-8-sig"))
    if root.tag != "mujoco" or len(root) != 4 or set(child.tag for child in root) != {"compiler", "option", "worldbody", "actuator"}:
        raise ValueError("EXACT_SINGLE_AXIS_CONTROL_MODEL_REQUIRED")
    if root.find("compiler").attrib != {"angle":"radian", "inertiafromgeom":"false"}:
        raise ValueError("EXPLICIT_RADIAN_INERTIA_MODEL_REQUIRED")
    option = root.find("option")
    if set(option.attrib) != {"timestep", "gravity", "integrator"} or option.get("integrator") != "implicitfast" or _numbers(option.get("gravity")) != [0,0,0]:
        raise ValueError("FROZEN_CONTROL_INTEGRATION_CONDITIONS_REQUIRED")
    close(float(option.get("timestep")), .0001, "XML timestep")
    bodies = root.findall(".//body")
    joints = root.findall(".//joint")
    actuators = list(root.find("actuator"))
    if len(bodies) != 1 or len(joints) != 1 or len(actuators) != 1:
        raise ValueError("ONE_BODY_HINGE_POSITION_SERVO_REQUIRED")
    body, joint, actuator = bodies[0], joints[0], actuators[0]
    if list(root.find("worldbody")) != [body] or len(body) != 3 or set(child.tag for child in body) != {"inertial", "joint", "geom"}:
        raise ValueError("NO_EXTRA_WORLD_OR_BODY_ELEMENTS_ALLOWED")
    if body.attrib != {"name":"load"} or joint.attrib != {"name":"shaft", "type":"hinge", "axis":"0 0 1", "armature":"0", "damping":"0", "frictionloss":"0", "limited":"false"}:
        raise ValueError("IDEAL_SINGLE_SHAFT_REQUIRED")
    inertial = body.find("inertial")
    if set(inertial.attrib) != {"pos", "mass", "diaginertia"} or _numbers(inertial.get("pos")) != [0,0,0]:
        raise ValueError("CENTERED_INERTIA_REQUIRED")
    close(float(inertial.get("mass")), .02, "XML disk mass")
    inertia = _numbers(inertial.get("diaginertia"))
    for actual, expected in zip(inertia, [0.00005016666666666667, 0.00005016666666666667, .0001]):
        close(actual, expected, "XML disk inertia")
    if len(inertia) != 3 or len(root.findall(".//geom")) != 1:
        raise ValueError("SINGLE_DECLARED_DISK_REQUIRED")
    geometry = body.find("geom")
    if set(geometry.attrib) != {"name", "type", "size", "contype", "conaffinity", "rgba"} or geometry.get("type") != "cylinder" or _numbers(geometry.get("size")) != [.1,.005] or geometry.get("contype") != "0" or geometry.get("conaffinity") != "0":
        raise ValueError("COLLISION_DISABLED_DISK_REQUIRED")
    expected = {"name":"servo", "joint":"shaft", "gear":"1", "timeconst":"0", "ctrllimited":"false", "forcelimited":"false"}
    if actuator.tag != "position" or set(actuator.attrib) != set(expected) | {"kp", "kv"} or any(actuator.get(k) != v for k,v in expected.items()):
        raise ValueError("NATIVE_UNCLIPPED_POSITION_SERVO_REQUIRED")
    close(float(actuator.get("kp")), kp, "XML kp")
    close(float(actuator.get("kv")), kv, "XML kv")


def validate_native_model(trajectory, case):
    kp, kv = check_case(case)
    if trajectory.get("engine") != "mujoco" or trajectory.get("schema") != "rlsok.mechanical-trajectory/2":
        raise ValueError("NATIVE_MUJOCO_TRAJECTORY_REQUIRED")
    units = trajectory["units"]
    if any(units.get(k) != v for k,v in {"time":"s", "joint_position":"rad", "joint_velocity":"rad/s", "joint_torque":"N*m", "inertia":"kg*m^2"}.items()):
        raise ValueError("EXPLICIT_NATIVE_CONTROL_SI_UNITS_REQUIRED")
    model = trajectory["model"]
    if (model["gravity_m_per_s2"] != [0,0,0] or set(model["bodies"]) != {"load"} or set(model["joints"]) != {"shaft"}
            or set(model["actuators"]) != {"servo"} or model["integrator"] != "implicitfast"):
        raise ValueError("NATIVE_SINGLE_AXIS_MODEL_REQUIRED")
    body, joint = model["bodies"]["load"], model["joints"]["shaft"]
    close(body["mass_kg"], .02, "native mass")
    if body["inertial_position_body_m"] != [0,0,0] or body["inertial_quaternion_body_wxyz"] != [1,0,0,0]:
        raise ValueError("NATIVE_CENTERED_AXIS_ALIGNED_INERTIA_REQUIRED")
    close(model["fluid"]["density_kg_per_m3"], 0., "native fluid density")
    close(model["fluid"]["dynamic_viscosity_Pa_s"], 0., "native fluid viscosity")
    if model["fluid"]["wind_world_m_per_s"] != [0,0,0]:
        raise ValueError("NO_CONTROL_BENCHMARK_WIND_REQUIRED")
    if len(body["principal_inertia_kg_m2"]) != 3:
        raise ValueError("NATIVE_PRINCIPAL_INERTIA_REQUIRED")
    for actual, expected in zip(body["principal_inertia_kg_m2"], [0.00005016666666666667,0.00005016666666666667,.0001]):
        close(actual, expected, "native disk inertia")
    if joint["type"] != "hinge" or joint["axis_body"] != [0,0,1]:
        raise ValueError("NATIVE_Z_HINGE_REQUIRED")
    for name in ("armature_kg_m2", "damping_Nm_s_per_rad", "frictionloss_Nm"):
        close(joint[name], 0., "native " + name)
    actuator = model["actuators"]["servo"]
    exact = {"transmission":"joint", "joint":"shaft", "gear":[1,0,0,0,0,0], "gain_type":"fixed", "bias_type":"affine",
             "dynamics_type":"none", "control_limited":False, "force_limited":False, "control_unit":"rad", "force_unit":"N*m"}
    if any(actuator.get(k) != v for k,v in exact.items()):
        raise ValueError("COMPILED_NATIVE_POSITION_SERVO_METADATA_REQUIRED")
    for values, expected in ((actuator["gain_parameters"], [kp,0,0]), (actuator["bias_parameters"], [0,-kp,-kv])):
        if not isinstance(values, list) or len(values) < 3:
            raise ValueError("COMPILED_NATIVE_GAIN_BIAS_PARAMETERS_REQUIRED")
        for index, value in enumerate(values):
            close(value, expected[index] if index < 3 else 0., "native gain/bias")


def _crossing(times, values, threshold):
    for index in range(1, len(times)):
        if values[index-1] < threshold <= values[index]:
            return times[index-1] + (times[index]-times[index-1]) * (threshold-values[index-1]) / (values[index]-values[index-1])
    raise ValueError("STEP_RESPONSE_NEVER_REACHED_HALF_TARGET")


def inspect_trace(trajectory, result, case):
    """Pure numerical checks; this function alone is not native receipt evidence."""
    kp, kv = check_case(case)
    validate_native_model(trajectory, case)
    close(result["metrics"]["timestep_seconds"], .0001, "reported timestep")
    frames = trajectory["frames"]
    if not isinstance(frames, list) or len(frames) != 2001:
        raise ValueError("COMPLETE_2001_FRAME_CONTROL_TRACE_REQUIRED")
    times, positions, velocities, errors_q, errors_v, errors_torque = [], [], [], [], [], []
    for index, frame in enumerate(frames):
        time = finite(frame["time_seconds"])
        close(time, index * .001, "sample time", tolerance=1e-9)
        joint, actuator = frame["joints"]["shaft"], frame["actuators"]["servo"]
        q, velocity = finite(joint["position_rad"]), finite(joint["velocity_rad_per_s"])
        torque = finite(joint["actuator_torque_Nm"])
        close(actuator["control_rad"], .2, "constant native position command")
        close(actuator["force_Nm"], torque, "native force and joint torque", tolerance=1e-10)
        close(joint["passive_torque_Nm"], 0, "passive torque", tolerance=1e-10)
        close(joint["constraint_torque_Nm"], 0, "constraint torque", tolerance=1e-10)
        if frame["contacts"] != 0:
            raise ValueError("UNMODELED_CONTACT_IN_CONTROL_BENCHMARK")
        expected_q, expected_v = reference_state(time, .0001, kp, kv, .2)
        times.append(time); positions.append(q); velocities.append(velocity)
        errors_q.append(abs(q-expected_q)); errors_v.append(abs(velocity-expected_v))
        errors_torque.append(abs(torque - (kp*(.2-q)-kv*velocity)))
    close(positions[0], 0, "rest initial position")
    close(velocities[0], 0, "rest initial velocity")
    close(result["metrics"]["joint.shaft.final_position_rad"], positions[-1], "final position")
    close(result["metrics"]["joint.shaft.final_velocity_rad_per_s"], velocities[-1], "final velocity")
    native_maximum = finite(result["metrics"]["joint.shaft.max_position_rad"])
    if native_maximum + 1e-12 < max(positions):
        raise ValueError("NATIVE_ALL_STEP_EXTREMUM_BELOW_SAMPLED_MAXIMUM")
    settling_error = max(abs(q-.2) for t,q in zip(times,positions) if t >= .65-1e-9)
    performance = {"overshoot_fraction":max(0., (native_maximum-.2)/.2), "maximum_error_after_065s_rad":settling_error,
                   "final_position_rad":positions[-1], "final_velocity_rad_per_s":velocities[-1],
                   "first_half_target_time_seconds":_crossing(times,positions,.1)}
    reference_errors = {"maximum_position_error_rad":max(errors_q), "maximum_velocity_error_rad_per_s":max(errors_v), "maximum_torque_law_error_Nm":max(errors_torque)}
    matched = (reference_errors["maximum_position_error_rad"] <= .001 and reference_errors["maximum_velocity_error_rad_per_s"] <= .02
               and reference_errors["maximum_torque_law_error_Nm"] <= 1e-8)
    accepted = performance["overshoot_fraction"] <= .05 and settling_error <= .004 and .196 <= positions[-1] <= .204
    state = ("PASS_SCOPED_SIMULATION" if accepted else "FAIL") if matched else "EVIDENCE_INSUFFICIENT"
    return {"schema":"rlsok.control-validation/1", "case_id":case["case_id"],
            "reference_validation":"MATCHED" if matched else "MISMATCH", "engineering_acceptance":state,
            "expected_case_outcome":case["expected_engineering_acceptance"], "expected_case_outcome_matched":state == case["expected_engineering_acceptance"],
            "metrics":performance, "reference_errors":reference_errors,
            "control_model":{"J_kg_m2":.0001,"kp_Nm_per_rad":kp,"kv_Nm_s_per_rad":kv,"step_target_rad":.2,
                             "natural_frequency_rad_per_s":math.sqrt(kp/.0001),"damping_ratio":kv/(2*math.sqrt(kp*.0001))},
            "assumptions":case["assumptions"], "professional_solver_executions_launched":0,
            "scope":"Finite ideal native MuJoCo continuous position-servo trace; no general stability or hardware certification.",
            "time_sampling":"2001 native frames at 1 ms; native all-integration-step maximum position retained; settling is checked at recorded frames.",
            "user_goal_achieved":False}


def _verify_native(directory):
    from implementation.reality import verify
    return verify(directory)


def validate_run(directory):
    directory = Path(directory).resolve()
    verification = _verify_native(directory)
    if verification.get("status") != "FILES_AND_NATIVE_RECEIPT_VERIFIED" or verification.get("engineering_acceptance") not in ("PASS_SCOPED_SIMULATION", "FAIL"):
        raise ValueError("VERIFIED_NATIVE_CONTROL_RUN_REQUIRED")
    manifest = read(directory/"run.json")
    request = manifest["request"]
    if request["engine"] != "mujoco" or request["project"]["entry"] != "model.xml" or set(request["project"]["files"]) != {"model.xml","control-case.json"}:
        raise ValueError("BOUND_CONTROL_MODEL_AND_CASE_REQUIRED")
    if request["parameters"] != {"steps":20000,"sample_every":10,"bodies":["load"],"joints":["shaft"],"controls":[.2],"render":False}:
        raise ValueError("FROZEN_CONTROL_EXECUTION_PARAMETERS_REQUIRED")
    case_path, model_path = directory/"project/control-case.json", directory/"project/model.xml"
    case = read(case_path)
    validate_model_xml(model_path, case)
    index = read(directory/"files.json")["files"]
    artifact = "trajectory.json" if "trajectory.json" in index else "native/trajectory.json"
    if artifact not in index or sha(directory/artifact) != index[artifact]:
        raise ValueError("SEALED_NATIVE_CONTROL_TRAJECTORY_REQUIRED")
    result = inspect_trace(read(directory/artifact), read(directory/"result.json"), case)
    result["source_native_acceptance"] = verification["engineering_acceptance"]
    result["evidence"] = {name:{"path":str(path),"sha256":sha(path)} for name,path in {
        "native_file_index":directory/"files.json","native_receipt":directory/"execution.json",
        "native_result":directory/"result.json","native_trace":directory/artifact,"model":model_path,"case":case_path}.items()}
    result["evidence_kind"] = "READ_ONLY_REVIEW_OF_RECEIPT_VERIFIED_NATIVE_TRAJECTORY"
    return result


def compare_runs(critical, underdamped, critical_fast):
    reports = [validate_run(path) for path in (critical,underdamped,critical_fast)]
    if [report["case_id"] for report in reports] != ["critical","underdamped","critical_fast"]:
        raise ValueError("ONE_DISTINCT_RUN_PER_DECLARED_CONTROL_CASE_REQUIRED")
    if len({report["evidence"]["native_file_index"]["sha256"] for report in reports}) != 3:
        raise ValueError("DISTINCT_NATIVE_CONTROL_CASE_RECEIPTS_REQUIRED")
    ratio = reports[2]["metrics"]["first_half_target_time_seconds"] / reports[0]["metrics"]["first_half_target_time_seconds"]
    passed = all(r["reference_validation"] == "MATCHED" and r["expected_case_outcome_matched"] for r in reports) and .48 <= ratio <= .52
    return {"schema":"rlsok.control-benchmark-comparison/1", "status":"PASS_LIMITED_CONTROL_BENCHMARK" if passed else "NOT_PROVEN",
            "critical_fast_to_critical_t50_ratio":ratio,"required_ratio":[.48,.52],"cases":reports,
            "scope":"Three ideal native position-servo cases only; negative engineering FAIL is retained. No general stability/hardware certification.",
            "professional_solver_executions_launched":0,"user_goal_achieved":False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run')
    parser.add_argument('--critical')
    parser.add_argument('--underdamped')
    parser.add_argument('--critical-fast')
    args = parser.parse_args(argv)
    try:
        if args.run and not any((args.critical,args.underdamped,args.critical_fast)):
            result = validate_run(args.run)
        elif not args.run and all((args.critical,args.underdamped,args.critical_fast)):
            result = compare_runs(args.critical,args.underdamped,args.critical_fast)
        else:
            parser.error('Use --run alone, or all three --critical/--underdamped/--critical-fast paths.')
    except (OSError, ValueError, KeyError, TypeError) as error:
        result = {"schema":"rlsok.control-validation/1","status":"REJECTED",
                  "reason":str(error),"professional_solver_executions_launched":0,"user_goal_achieved":False}
    print(json.dumps(result,allow_nan=False))
    return 0 if result.get('status') == 'PASS_LIMITED_CONTROL_BENCHMARK' or result.get('engineering_acceptance') == 'PASS_SCOPED_SIMULATION' else 2


if __name__ == '__main__':
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0,str(root))
    raise SystemExit(main())
