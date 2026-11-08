"""Read-only, stdlib numerical qualification of sealed native contact runs.

This never starts a solver. Receipt verification is delegated to the existing
Linux verifier; the physical checks below independently use every saved step.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import re


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read(path):
    def invalid(value):
        raise ValueError("NONFINITE_JSON_NUMBER: " + value)
    return json.loads(Path(path).read_text(encoding="utf-8-sig"), parse_constant=invalid)


def finite_tree(value):
    if type(value) is bool:
        raise ValueError("BOOLEAN_IN_RAW_DYNAMICS")
    if type(value) in (float, int):
        if not math.isfinite(value):
            raise ValueError("NONFINITE_RAW_DYNAMICS")
    elif isinstance(value, dict):
        for item in value.values():
            finite_tree(item)
    elif isinstance(value, list):
        for item in value:
            finite_tree(item)


def load_dynamics(path, case):
    path = Path(path)
    if path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError("BOUNDED_DYNAMICS_REQUIRED")
    with path.open(encoding="utf-8") as stream:
        records = [json.loads(line) for line in stream]
    if len(records) != case["steps"] + 3:
        raise ValueError("EXACT_FULL_STEP_RECORD_COUNT_REQUIRED")
    header, *rows, footer = records
    if (header.get("record") != "header" or header.get("schema") != "rlsok.mechanical-dynamics/1"
            or header.get("engine") != "mujoco" or header.get("engine_version") != "3.16.0"
            or header.get("nv") != 1 or header.get("nq") != 1 or header.get("integrator") != "rk4"
            or header.get("steps") != case["steps"] or header.get("timestep_seconds") != case["timestep_seconds"]
            or header.get("dofs") != [{"index": 0, "joint": "shaft", "joint_type": "hinge"}]
            or footer != {"record": "footer", "complete": True, "state_count": len(rows), "steps": case["steps"]}):
        raise ValueError("EXACT_NATIVE_DYNAMICS_METADATA_REQUIRED")
    for index, row in enumerate(rows):
        finite_tree(row)
        if (row.get("record") != "state" or row.get("step") != index
                or abs(row["time_seconds"] - index * case["timestep_seconds"]) > 1e-10
                or (index and row["time_seconds"] <= rows[index - 1]["time_seconds"])):
            raise ValueError("EVERY_NATIVE_STEP_TIME_REQUIRED")
        if (any(len(row[key]) != 1 for key in ("qpos", "qvel", "qacc", "generalized_momentum"))
                or len(row["mass_matrix"]) != 1 or len(row["mass_matrix"][0]) != 1
                or set(row["generalized_forces"]) != {"actuator", "passive", "constraint", "bias", "applied", "smooth", "fluid"}
                or any(len(vector) != 1 for vector in row["generalized_forces"].values())):
            raise ValueError("ONE_DOF_RAW_DYNAMICS_REQUIRED")
    return header, rows


def trapezoid(rows, value):
    return math.fsum((b["time_seconds"] - a["time_seconds"]) * (value(a) + value(b)) / 2
                     for a, b in zip(rows, rows[1:]))


def contact_torque(contact, shaft):
    """Native contact wrench acts positively on geom2; lug must be one side."""
    names = contact["geom_names"]
    if set(names) != {"stop_lug", "positive_stop"}:
        raise ValueError("UNEXPECTED_CONTACT_PAIR")
    sign = 1.0 if names[1] == "stop_lug" else -1.0
    frame, local = contact["contact_frame_rows_world"], contact["force_contact_frame_N"]
    torque_local = contact["torque_contact_frame_Nm"]
    if len(frame) != 9 or len(local) != 3 or len(torque_local) != 3 or len(contact["position_m"]) != 3:
        raise ValueError("EXPLICIT_NATIVE_CONTACT_FRAME_REQUIRED")
    world = [sign * math.fsum(frame[3 * j + i] * local[j] for j in range(3)) for i in range(3)]
    torque_z = sign * math.fsum(frame[3 * j + 2] * torque_local[j] for j in range(3))
    position = contact["position_m"]
    return (position[0] - shaft[0]) * world[1] - (position[1] - shaft[1]) * world[0] + torque_z


def analyze_rows(rows, case, plan):
    """Pure arithmetic. Caller must validate completeness and provenance first."""
    limits, geometry = plan["limits"], plan["geometry"]
    checks = []

    def check(name, value, bound, relation="le"):
        passed = value <= bound if relation == "le" else value >= bound if relation == "ge" else value == bound
        checks.append({"name": name, "value": value, "relation": relation, "bound": bound, "passed": bool(passed)})

    force = lambda row, term: row["generalized_forces"][term][0]
    inertia, drive = case["shaft_inertia_kg_m2"], case["drive_torque_Nm"]
    first, last = rows[0], rows[-1]
    check("initial_angle_zero", abs(first["qpos"][0]), 1e-14)
    check("initial_velocity_zero", abs(first["qvel"][0]), 1e-14)
    check("constant_native_inertia", max(abs(row["mass_matrix"][0][0] / inertia - 1) for row in rows), limits["max_mass_matrix_relative_error"])
    check("prescribed_torque", max(abs(force(row, "actuator") - drive) for row in rows), 1e-12)
    check("no_unmodeled_passive_or_applied_force", max(abs(force(row, name)) for row in rows for name in ("passive", "bias", "applied", "fluid")), 1e-12)
    check("no_cartesian_applied_wrench", max(abs(value) for row in rows for vector in row["body_applied_wrenches_force_then_torque"] for value in vector), 0)
    check("smooth_force_decomposition", max(abs(force(row, "smooth") - (force(row, "actuator") + force(row, "passive") + force(row, "applied") - force(row, "bias"))) for row in rows), 1e-12)
    check("instantaneous_native_equation", max(abs(row["mass_matrix"][0][0] * row["qacc"][0] - force(row, "smooth") - force(row, "constraint")) for row in rows), limits["max_instantaneous_equation_residual_Nm"])
    check("native_generalized_momentum", max(abs(row["generalized_momentum"][0] - inertia * row["qvel"][0]) for row in rows), 1e-12)
    contact_impulse = trapezoid(rows, lambda row: force(row, "constraint"))
    drive_impulse = trapezoid(rows, lambda row: force(row, "actuator"))
    delta_momentum = inertia * (last["qvel"][0] - first["qvel"][0])
    expected_contact_impulse = delta_momentum - drive_impulse
    scale = max(abs(expected_contact_impulse), abs(drive_impulse), 1e-15)
    impulse_residual = abs(contact_impulse - expected_contact_impulse) / scale
    check("full_step_impulse_balance", impulse_residual, limits["max_impulse_relative_residual"])
    step_residuals = [inertia * (b["qvel"][0] - a["qvel"][0]) -
                      (b["time_seconds"] - a["time_seconds"]) / 2 *
                      (force(a, "smooth") + force(a, "constraint") + force(b, "smooth") + force(b, "constraint"))
                      for a, b in zip(rows, rows[1:])]
    mapping = max(abs(math.fsum(contact_torque(contact, geometry["shaft_world_m"]) for contact in row["contact_data"]) - force(row, "constraint")) for row in rows)
    check("contact_wrench_to_shaft_torque", mapping, limits["max_force_torque_mapping_residual_Nm"])
    forces = [math.sqrt(math.fsum(value * value for value in contact["force_contact_frame_N"])) for row in rows for contact in row["contact_data"]]
    peak = max(forces, default=0.0)
    check("contact_force_upper_bound", peak, limits["max_contact_force_N"])
    check("frictionless_contact_wrench", max((abs(value) for row in rows for contact in row["contact_data"] for value in contact["force_contact_frame_N"][1:] + contact["torque_contact_frame_Nm"]), default=0), 1e-12)
    contact_indices = [i for i, row in enumerate(rows) if row["contact_data"]]
    first_contact = None
    if case["expected_contact"]:
        check("positive_contact_exists", bool(contact_indices), True, "eq")
        check("contact_force_lower_bound", peak, limits["min_contact_force_N"], "ge")
        check("positive_target_angle_lower", last["qpos"][0], 1., "ge")
        check("positive_target_angle_upper", last["qpos"][0], 1.2)
        analytic = math.sqrt(2 * inertia * geometry["analytical_first_contact_angle_rad"] / drive)
        bracketed = False
        if contact_indices and contact_indices[0] > 0:
            index = contact_indices[0]
            before, after = rows[index - 1]["time_seconds"], rows[index]["time_seconds"]
            epsilon = limits["contact_time_bracket_extra_seconds"]
            bracketed = before - epsilon <= analytic <= after + epsilon
            first_contact = {"analytic_seconds": analytic, "preceding_no_contact_seconds": before, "first_contact_seconds": after}
        check("geometric_first_contact_bracket", bracketed, True, "eq")
    else:
        check("negative_no_contact", len(contact_indices), 0, "eq")
        check("negative_analytic_angle", max(abs(row["qpos"][0] - .5 * drive / inertia * row["time_seconds"] ** 2) for row in rows), limits["max_negative_position_error_rad"])
        check("negative_analytic_velocity", max(abs(row["qvel"][0] - drive / inertia * row["time_seconds"]) for row in rows), limits["max_negative_velocity_error_rad_per_s"])
        check("negative_misses_same_positive_target", last["qpos"][0] < 1, True, "eq")
    return {"checks": checks, "passed": all(c["passed"] for c in checks),
            "measurements": {"final_angle_rad": last["qpos"][0], "final_velocity_rad_per_s": last["qvel"][0],
                             "peak_contact_force_N": peak, "contact_angular_impulse_Nms": contact_impulse,
                             "expected_contact_angular_impulse_Nms": expected_contact_impulse,
                             "drive_angular_impulse_Nms": drive_impulse, "angular_momentum_change_Nms": delta_momentum,
                             "full_step_impulse_relative_residual": impulse_residual,
                             "maximum_single_step_impulse_absolute_residual_Nms": max(map(abs, step_residuals), default=0),
                             "sum_absolute_single_step_impulse_residuals_Nms": math.fsum(map(abs, step_residuals)),
                             "contact_force_torque_max_residual_Nm": mapping, "first_contact": first_contact}}


def analyze_case(directory, label, plan, fixture_root, expected_worker_sha256):
    from implementation.reality_linux import verify_linux
    directory, fixture_root = Path(directory).resolve(), Path(fixture_root).resolve()
    verified = verify_linux(directory)
    index, execution, result, manifest = (read(directory / name) for name in ("files.json", "execution.json", "result.json", "run.json"))
    if (sha(directory / "verification-recipe.py") != expected_worker_sha256
            or manifest["session"]["worker"]["sha256"] != expected_worker_sha256):
        raise ValueError("APPROVED_STAGE_WORKER_BINDING_REQUIRED")
    for name in ("dynamics.jsonl", "trajectory.json", "result.json", "verification-recipe.py"):
        if sha(directory / name) != index["files"].get(name) or sha(directory / name) != execution["artifact_sha256"].get(name):
            raise ValueError("NATIVE_RECEIPT_MUST_BIND_DYNAMICS_AND_WORKER")
    for name in ("load.xml", "constant-torque.json"):
        expected = plan["fixture_sha256"][label][name]
        if sha(fixture_root / label / name) != expected or sha(directory / "project" / name) != expected:
            raise ValueError("FROZEN_QUALIFICATION_MODEL_CHANGED")
    request = read(fixture_root / label / "request.json")
    actual = json.loads(json.dumps(manifest["request"]))
    actual["project"]["directory"] = request["project"]["directory"]
    if actual != request or sha(fixture_root / label / "request.json") != plan["fixture_sha256"][label]["request.json"]:
        raise ValueError("FROZEN_QUALIFICATION_REQUEST_CHANGED")
    case = plan["cases"][label]
    header, rows = load_dynamics(directory / "dynamics.jsonl", case)
    report = analyze_rows(rows, case, plan)
    expected_acceptance = case["expected_native_acceptance"]
    report["checks"].append({"name": "expected_original_native_acceptance", "value": result["engineering_acceptance"],
                              "relation": "eq", "bound": expected_acceptance, "passed": result["engineering_acceptance"] == expected_acceptance})
    report["checks"].append({"name": "expected_native_exit_code", "value": execution["exit_code"], "relation": "eq",
                              "bound": 0 if case["expected_contact"] else 2,
                              "passed": execution["exit_code"] == (0 if case["expected_contact"] else 2)})
    report["passed"] = all(check["passed"] for check in report["checks"])
    report.update(label=label, run=str(directory), engine_version=header["engine_version"],
                  receipt_verification=verified, state_count=len(rows),
                  files_sha256=sha(directory / "files.json"), dynamics_sha256=sha(directory / "dynamics.jsonl"))
    return report


def analyze_phase(runs, plan_path, expected_worker_sha256, expected_plan_sha256):
    plan_path = Path(plan_path).resolve()
    if (any(not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None
            for value in (expected_worker_sha256, expected_plan_sha256))
            or sha(plan_path) != expected_plan_sha256):
        raise ValueError("APPROVED_STAGE_WORKER_AND_PLAN_HASHES_REQUIRED")
    plan = read(plan_path)
    if plan.get("schema") != "rlsok.contact-qualification-plan/1" or set(runs) != set(plan["cases"]):
        raise ValueError("EXACT_FOUR_FROZEN_CONTACT_CASES_REQUIRED")
    cases = {label: analyze_case(directory, label, plan, plan_path.parent, expected_worker_sha256) for label, directory in runs.items()}
    coarse, fine = (cases[label]["measurements"] for label in ("contact_50us", "contact_25us"))
    limits = plan["limits"]
    checks = []
    for metric, limit in (("final_angle_rad", "max_convergence_angle_difference_rad"),
                          ("final_velocity_rad_per_s", "max_convergence_velocity_difference_rad_per_s"),
                          ("peak_contact_force_N", "max_convergence_force_relative_difference"),
                          ("contact_angular_impulse_Nms", "max_convergence_contact_impulse_relative_difference")):
        difference = abs(coarse[metric] - fine[metric])
        if "relative" in limit:
            difference /= max(abs(fine[metric]), 1e-15)
        checks.append({"name": limit, "value": difference, "relation": "le", "bound": limits[limit], "passed": difference <= limits[limit]})
    passed = all(case["passed"] for case in cases.values()) and all(check["passed"] for check in checks)
    return {"schema": "rlsok.contact-qualification-result/1", "status": "PASS_SCOPED_CONTACT_QUALIFICATION" if passed else "FAIL",
            "plan_sha256": sha(plan_path), "expected_worker_sha256": expected_worker_sha256,
            "cases": cases, "convergence_checks": checks,
            "solver_executions_by_this_analysis": 0, "assumptions": plan["assumptions"],
            "limitations": ["End-state trapezoid quadrature is independent of the native RK4 internal stages and is only accepted within the frozen tolerance.",
                            "This does not validate measured contact materials, hardware, friction, structural strength or unrestricted geometry."]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", required=True)
    parser.add_argument("--runs", required=True, help="JSON mapping exact four case labels to existing native job directories")
    parser.add_argument("--worker-sha256", required=True, help="Worker hash frozen in the approved phase and analysis Stage")
    parser.add_argument("--plan-sha256", required=True, help="Qualification plan hash frozen in the approved phase and analysis Stage")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    try:
        report = analyze_phase(read(args.runs), args.plan, args.worker_sha256, args.plan_sha256)
    except (ValueError, KeyError, OSError, TypeError) as error:
        report = {"schema": "rlsok.contact-qualification-result/1", "status": "EVIDENCE_INSUFFICIENT", "reason": str(error), "solver_executions_by_this_analysis": 0}
    Path(args.out).write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return 0 if report["status"] == "PASS_SCOPED_CONTACT_QUALIFICATION" else 2


if __name__ == "__main__":
    raise SystemExit(main())
