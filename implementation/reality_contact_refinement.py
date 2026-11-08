"""Versioned, read-only contact refinement assessment; never starts a solver.

R2 remains immutable. Reuse its sealed 50/25 us runs as evidence, with the
original arithmetic and thresholds, while assessing a new 25/12.5 us scope.
"""
import argparse
import copy
import json
from pathlib import Path
import re

from implementation.reality_contact_validation import analyze_case, finite_tree, load_dynamics, read, sha


LABELS = ("contact_50us", "contact_25us", "contact_12p5us", "contact_pgs_25us",
          "contact_pgs_12p5us", "mass_double_pgs_25us", "reverse_torque_pgs_25us")
NEW_LABELS = LABELS[2:]
REQUIRED_LABELS = LABELS[3:]
NATIVE_CAPS = {"contact_12p5us": {"cpu": 16, "wall": 20},
               "contact_pgs_25us": {"cpu": 12, "wall": 10},
               "contact_pgs_12p5us": {"cpu": 16, "wall": 20},
               "mass_double_pgs_25us": {"cpu": 8, "wall": 10},
               "reverse_torque_pgs_25us": {"cpu": 8, "wall": 10}}
BASELINE_CASES = {"contact_50us": "contact_50us", "contact_25us": "contact_25us",
                 "contact_12p5us": "contact_25us", "contact_pgs_25us": "contact_25us",
                 "contact_pgs_12p5us": "contact_25us", "mass_double_pgs_25us": "mass_double_25us",
                 "reverse_torque_pgs_25us": "reverse_torque_25us"}
KKT_LIMITS = {"force_feasibility_N": 1e-12, "dual_feasibility_m_per_s2": 1e-8,
              "complementarity_N_m_per_s2": 1e-8, "optimal_force_error_N": 1e-8,
              "AR_identity_per_kg": 1e-8, "b_identity_m_per_s2": 1e-8,
              "generalized_force_identity_Nm": 1e-8, "smooth_acceleration_identity_Nm": 1e-8}
STOP_POLICY = {"diagnostic_case": "contact_12p5us",
               "allowed_diagnostic_native_outcomes": ["PASS_SCOPED_SIMULATION/exit0", "FAIL/DESIGN_VIOLATION/exit2"],
               "diagnostic_physics_failure": "PRESERVE_AND_CONTINUE_FIXED_PGS_RECIPE",
               "all_tool_resource_identity_failures": "STOP",
               "pgs_positive_required": "PASS_SCOPED_SIMULATION/exit0",
               "pgs_negative_required": "FAIL/DESIGN_VIOLATION/exit2",
               "automatic_retry": False}
PAIR_METRICS = (
    ("final_angle_rad", "max_convergence_angle_difference_rad"),
    ("final_velocity_rad_per_s", "max_convergence_velocity_difference_rad_per_s"),
    ("peak_contact_force_N", "max_convergence_force_relative_difference"),
    ("contact_angular_impulse_Nms", "max_convergence_contact_impulse_relative_difference"),
)


def hash_value(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def validate_plan(plan_path, worker_sha256, plan_sha256):
    """Bind the recipe to R2 and permit only the declared refinement changes."""
    path = Path(plan_path).resolve()
    if not hash_value(worker_sha256) or not hash_value(plan_sha256) or sha(path) != plan_sha256:
        raise ValueError("APPROVED_REFINEMENT_WORKER_AND_PLAN_HASHES_REQUIRED")
    plan = read(path)
    if (plan.get("schema") != "rlsok.contact-refinement-plan/1"
            or set(plan.get("cases", {})) != set(LABELS)
            or plan.get("new_native_cases") != list(NEW_LABELS)
            or plan.get("required_current_cases") != list(REQUIRED_LABELS)
            or set(plan.get("reused_runs", {})) != set(LABELS[:2])
            or plan.get("worker_sha256") != worker_sha256
            or plan.get("native_caps") != NATIVE_CAPS
            or plan.get("stop_policy") != STOP_POLICY
            or plan.get("observer_status") != "SOURCE_PREPARED_NOT_RUN"):
        raise ValueError("EXACT_VERSIONED_SEVEN_CASE_FIVE_NEW_RUN_SCOPE_REQUIRED")
    baseline = plan["baseline_plan"]
    if not hash_value(baseline["sha256"]) or sha(baseline["path"]) != baseline["sha256"]:
        raise ValueError("SEALED_R2_PLAN_CHANGED")
    original = read(baseline["path"])
    baseline_inputs = plan["baseline_inputs"]
    if sha(baseline_inputs["path"]) != baseline_inputs["sha256"]:
        raise ValueError("SEALED_R2_INPUTS_CHANGED")
    original_inputs = read(baseline_inputs["path"])
    baseline_worker = original_inputs["frozen_sources"]["reality_worker.py"]
    if (original_inputs["files"].get(baseline_worker) != plan["baseline_worker_sha256"]
            or sha(baseline_worker) != plan["baseline_worker_sha256"]):
        raise ValueError("SEALED_R2_WORKER_BINDING_REQUIRED")
    if (original.get("schema") != "rlsok.contact-qualification-plan/1"
            or plan["limits"] != original["limits"] or plan["geometry"] != original["geometry"]
            or plan["limits"]["max_convergence_force_relative_difference"] != .05
            or plan["limits"]["max_convergence_contact_impulse_relative_difference"] != .05
            or plan["limits"]["max_impulse_relative_residual"] != .05):
        raise ValueError("UNCHANGED_R2_THRESHOLDS_AND_CONTACT_GEOMETRY_REQUIRED")
    for label in LABELS[:2]:
        if (plan["cases"][label] != original["cases"][label]
                or plan["fixture_sha256"][label] != original["fixture_sha256"][label]):
            raise ValueError("ORIGINAL_R2_CASE_BYTES_AND_PARAMETERS_REQUIRED")
    if plan.get("scalar_kkt_limits") != KKT_LIMITS:
        raise ValueError("EXACT_DIMENSIONED_SCALAR_KKT_THRESHOLDS_REQUIRED")
    if plan.get("pgs_solver_options") != {"solver": "PGS", "tolerance": 1e-12, "iterations": 100}:
        raise ValueError("FROZEN_PGS_SOLVER_OPTIONS_REQUIRED")
    for label in NEW_LABELS:
        expected_case = copy.deepcopy(original["cases"][BASELINE_CASES[label]])
        if label in ("contact_12p5us", "contact_pgs_12p5us"):
            expected_case.update(timestep_seconds=.0000125, steps=32000)
        if plan["cases"][label] != expected_case:
            raise ValueError("UNCHANGED_PHYSICS_AND_FIXED_DURATION_REQUIRED")
    for label in LABELS:
        if set(plan["fixture_sha256"][label]) != {"load.xml", "constant-torque.json", "request.json"}:
            raise ValueError("COMPLETE_CASE_FILE_HASHES_REQUIRED")
        for name, expected in plan["fixture_sha256"][label].items():
            if not hash_value(expected) or sha(path.parent / label / name) != expected:
                raise ValueError("FROZEN_REFINEMENT_FIXTURE_CHANGED")
    baseline_root = Path(baseline["path"]).parent
    for label in NEW_LABELS:
        source = baseline_root / BASELINE_CASES[label]
        target = path.parent / label
        expected_xml = (source / "load.xml").read_bytes()
        if label in ("contact_12p5us", "contact_pgs_12p5us"):
            old_token, new_token = b'timestep="0.000025"', b'timestep="0.0000125"'
            if expected_xml.count(old_token) != 1:
                raise ValueError("EXPLICIT_ORIGINAL_TIMESTEP_REQUIRED")
            expected_xml = expected_xml.replace(old_token, new_token)
        if label in REQUIRED_LABELS:
            old_token = b'integrator="RK4"'
            if expected_xml.count(old_token) != 1:
                raise ValueError("UNCHANGED_NATIVE_RK4_REQUIRED")
            expected_xml = expected_xml.replace(old_token, old_token + b' solver="PGS" tolerance="1e-12" iterations="100"')
        if ((target / "load.xml").read_bytes() != expected_xml
                or (target / "constant-torque.json").read_bytes() != (source / "constant-torque.json").read_bytes()):
            raise ValueError("ONLY_DECLARED_NUMERICAL_SOLVER_AND_TIMESTEP_CHANGES_ALLOWED")
        request = read(source / "request.json")
        request["parameters"]["steps"] = plan["cases"][label]["steps"]
        request["resources"]["cpu_seconds"] = NATIVE_CAPS[label]["cpu"]
        request["resources"]["wall_seconds"] = NATIVE_CAPS[label]["wall"]
        request["scope"] = plan["new_request_scopes"][label]
        if read(target / "request.json") != request:
            raise ValueError("ONLY_STEP_COUNT_FROZEN_RESOURCE_CAPS_AND_SCOPE_MAY_CHANGE")
    for binding in plan["reused_runs"].values():
        if (not Path(binding["directory"]).is_absolute()
                or set(binding["files"]) != {"files.json", "execution.json", "dynamics.jsonl"}
                or any(not hash_value(value) for value in binding["files"].values())):
            raise ValueError("EXACT_SEALED_R2_RUN_IDENTITIES_REQUIRED")
    return plan


def pair_checks(cases, coarse_label, fine_label, limits):
    """Use the unchanged R2 denominator: abs(finer value), floored at 1e-15."""
    coarse = cases[coarse_label]["measurements"]
    fine = cases[fine_label]["measurements"]
    checks = []
    for metric, limit in PAIR_METRICS:
        numerator = abs(coarse[metric] - fine[metric])
        denominator = max(abs(fine[metric]), 1e-15) if "relative" in limit else None
        value = numerator / denominator if denominator is not None else numerator
        checks.append({"name": limit, "metric": metric, "coarse": coarse[metric], "fine": fine[metric],
                       "numerator": numerator, "denominator": denominator, "value": value,
                       "relation": "le", "bound": limits[limit], "passed": value <= limits[limit]})
    return {"cases": [coarse_label, fine_label], "checks": checks,
            "status": "PASS" if all(item["passed"] for item in checks) else "FAIL"}


def compiled_observer_check(header, case, pgs):
    """Compare native compiled settings to the frozen, bounded XML recipe."""
    expected_options = {"timestep": case["timestep_seconds"], "integrator": 1,
                        "solver": 0 if pgs else 2, "iterations": 100,
                        "tolerance": 1e-12 if pgs else 1e-8,
                        "gravity": [0., 0., -9.81], "wind": [0., 0., 0.],
                        "density": 0., "viscosity": 0., "noslip_iterations": 0,
                        "disableflags": 0, "enableflags": 0, "disableactuator": 0}
    options = header["native_options"]
    finite_tree(options)
    keys = {"timestep", "impratio", "tolerance", "ls_tolerance", "noslip_tolerance",
            "ccd_tolerance", "sleep_tolerance", "density", "viscosity", "o_margin",
            "integrator", "cone", "jacobian", "solver", "iterations", "ls_iterations",
            "noslip_iterations", "ccd_iterations", "disableflags", "enableflags",
            "disableactuator", "sdf_initpoints", "sdf_iterations", "gravity", "wind",
            "magnetic", "o_solref", "o_solimp", "o_friction"}
    expected_geoms = [{"index": i, "condim": 1, "solref": [.002, 1.],
                       "solimp": [.95, .99, .001, .5, 2.], "margin": 0., "gap": 0.,
                       "friction": [0., 0., 0.]} for i in range(4)]
    if (header.get("contact_observer_schema") != "rlsok.contact-solver-observer/1"
            or header.get("solver") != ("pgs" if pgs else "newton")
            or set(options) != keys
            or header.get("solver_iterations") != options["iterations"]
            or header.get("solver_tolerance") != options["tolerance"]
            or header.get("solver_ls_iterations") != options["ls_iterations"]
            or header.get("solver_ls_tolerance") != options["ls_tolerance"]
            or header.get("solver_stat_columns") != ["improvement", "gradient", "lineslope", "nactive", "nchange", "neval", "nupdate"]):
        raise ValueError("EXACT_NATIVE_COMPILED_OBSERVER_METADATA_REQUIRED")
    checks = [{"name": "native_option_" + key, "value": options[key], "bound": value,
               "relation": "eq", "passed": options[key] == value} for key, value in expected_options.items()]
    checks.append({"name": "compiled_contact_geoms", "value": header["compiled_contact_geoms"],
                   "bound": expected_geoms, "relation": "eq", "passed": header["compiled_contact_geoms"] == expected_geoms})
    return {"status": "PASS" if all(c["passed"] for c in checks) else "FAIL", "checks": checks,
            "native_options": options,
            "scope": "Checks every compiled geom contact parameter and all physical/solver options changed or relied upon by this recipe; remaining native options are retained verbatim."}


def observation_summary(header, rows):
    """Expose the diagnostic observations without treating them as acceptance."""
    def residual(row):
        forces = row["generalized_forces"]
        return row["mass_matrix"][0][0] * row["qacc"][0] - forces["smooth"][0] - forces["constraint"][0]

    def snapshot(row):
        if row is None:
            return None
        native = row["scalar_constraint"]
        return {"step": row["step"], "time_seconds": row["time_seconds"],
                "instantaneous_equation_residual_Nm": residual(row),
                "native_solver_niter": native["solver_niter"],
                "native_solver_last_stat_iteration": native["solver_last_stat_iteration"],
                "native_solver_last_stat": native["solver_last_stat"],
                "native_scalar_constraint": native}

    maximum = max(rows, key=lambda row: abs(residual(row)))
    first = next((row for row in rows if row["scalar_constraint"]["nefc"]), None)
    return {"role": "native_observation_diagnosis_not_independent_physics_pass",
            "solver": header["solver"], "solver_stat_columns": header["solver_stat_columns"],
            "state_count": len(rows), "first_contact": snapshot(first),
            "maximum_instantaneous_equation_residual": snapshot(maximum),
            "maximum_native_solver_niter": max(row["scalar_constraint"]["solver_niter"] for row in rows),
            "limitations": ["Only final iteration statistics at saved step-end states are available.",
                            "A Newton force-equation residual alone does not identify the termination cause."]}


def scalar_pgs_kkt(header, rows, limits):
    """Check the native scalar QP, independently of dualFinish force balance.

    AR already includes R. In this fixed-axis, frictionless normal constraint,
    AR has units 1/kg, b and g have m/s^2, and the unknown force has N.
    """
    if (header.get("contact_observer_schema") != "rlsok.contact-solver-observer/1"
            or header.get("solver") != "pgs" or header.get("solver_iterations") != 100
            or header.get("solver_tolerance") != 1e-12 or limits != KKT_LIMITS):
        raise ValueError("FROZEN_NATIVE_PGS_OBSERVER_AND_OPTIONS_REQUIRED")
    maxima = {name: {"value": 0.0, "step": 0} for name in KKT_LIMITS}
    constrained_rows = 0

    def retain(name, value, step):
        if value > maxima[name]["value"]:
            maxima[name] = {"value": value, "step": step}

    for row in rows:
        native = row["scalar_constraint"]
        nefc = native["nefc"]
        if (type(nefc) is not int or nefc not in (0, 1)
                or any(native[name] != 0 for name in ("ne", "nf", "nl", "efm_active"))
                or native["ncon"] not in (0, 1) or native["nisland"] not in (0, 1)
                or native["AR_available"] != 1 or not isinstance(native["AR"], list)
                or len(native["qacc_smooth"]) != 1):
            raise ValueError("NATIVE_SCALAR_RIGID_FRICTIONLESS_PGS_SCOPE_REQUIRED")
        for key in ("type", "id", "J", "AR", "R", "D", "b", "force", "aref", "pos", "margin", "vel"):
            if not isinstance(native[key], list) or len(native[key]) != nefc:
                raise ValueError("EXACT_NATIVE_SCALAR_CONSTRAINT_ARRAYS_REQUIRED")
        step = row["step"]
        mass = row["mass_matrix"][0][0]
        smooth = row["generalized_forces"]["smooth"][0]
        qconstraint = row["generalized_forces"]["constraint"][0]
        if mass <= 0:
            raise ValueError("POSITIVE_NATIVE_INERTIA_REQUIRED")
        retain("smooth_acceleration_identity_Nm", abs(mass * native["qacc_smooth"][0] - smooth), step)
        if nefc == 0:
            retain("generalized_force_identity_Nm", abs(qconstraint), step)
            continue
        if native["type"] != [5] or native["id"] != [0] or native["ncon"] != 1:
            raise ValueError("SINGLE_NATIVE_FRICTIONLESS_CONTACT_ROW_REQUIRED")
        constrained_rows += 1
        jacobian, matrix, regularizer, b, force, aref = (native[key][0] for key in ("J", "AR", "R", "b", "force", "aref"))
        if matrix <= 0 or regularizer <= 0 or native["D"][0] <= 0:
            raise ValueError("POSITIVE_NATIVE_SCALAR_QP_AND_REGULARIZATION_REQUIRED")
        derived_matrix = jacobian * jacobian / mass + regularizer
        derived_b = jacobian * native["qacc_smooth"][0] - aref
        gradient = matrix * force + b
        retain("AR_identity_per_kg", abs(matrix - derived_matrix), step)
        retain("b_identity_m_per_s2", abs(b - derived_b), step)
        retain("generalized_force_identity_Nm", abs(qconstraint - jacobian * force), step)
        retain("force_feasibility_N", max(0.0, -force), step)
        retain("dual_feasibility_m_per_s2", max(0.0, -gradient), step)
        retain("complementarity_N_m_per_s2", abs(force * gradient), step)
        retain("optimal_force_error_N", abs(force - max(0.0, -b / matrix)), step)
    checks = [{"name": name, **maximum, "relation": "le", "bound": limits[name],
               "passed": maximum["value"] <= limits[name]} for name, maximum in maxima.items()]
    return {"schema": "rlsok.scalar-pgs-kkt-check/1", "status": "PASS" if all(c["passed"] for c in checks) else "FAIL",
            "state_count": len(rows), "constrained_state_count": constrained_rows, "checks": checks,
            "native_coefficients_required": True,
            "formulas": {"AR": "J^2/M + R (native efc_AR already includes R)",
                         "b": "J*qacc_smooth - aref", "g": "native_AR*native_force + native_b",
                         "scalar_optimum_N": "max(0, -native_b/native_AR)"},
            "limitations": ["Only exported native step-end states are checked; RK4 internal substage records are not exported.",
                            "PGS dualFinish enforces acceleration/force balance algebraically; that identity alone is not contact qualification."]}


def history_review(reference):
    """A history diagnosis is a separately bound review, never a retroactive PASS."""
    if reference is None:
        return {"status": "EVIDENCE_INSUFFICIENT", "reason": "R1_IMPULSE_HISTORY_REVIEW_NOT_PROVIDED",
                "old_failures_reclassified": False}
    if (not isinstance(reference, dict) or set(reference) != {"path", "sha256"}
            or not hash_value(reference["sha256"]) or sha(reference["path"]) != reference["sha256"]):
        raise ValueError("EXACT_IMPULSE_HISTORY_REVIEW_REFERENCE_REQUIRED")
    report = read(reference["path"])
    diagnostic = report.get("schema") == "rlsok.contact-physics-diagnosis/1"
    preserved = report.get("old_failure_preserved") is True if diagnostic else report.get("old_failures_reclassified") is False
    if (report.get("schema") not in ("rlsok.contact-impulse-history-review/1", "rlsok.contact-physics-diagnosis/1")
            or report.get("status") not in ("REVIEWED_WITH_LIMITATIONS", "EVIDENCE_INSUFFICIENT")
            or not preserved or not isinstance(report.get("conclusion"), (str, dict)) or not report["conclusion"]
            or not isinstance(report.get("source_bindings"), dict) or not report["source_bindings"]):
        raise ValueError("EXPLICIT_BOUND_R1_IMPULSE_HISTORY_REVIEW_REQUIRED")
    for name, binding in report["source_bindings"].items():
        if diagnostic:
            if (not isinstance(binding, dict) or set(binding) != {"path", "sha256", "bytes"}
                    or type(binding["bytes"]) is not int or binding["bytes"] < 0
                    or Path(binding["path"]).stat().st_size != binding["bytes"]):
                raise ValueError("EXPLICIT_IMPULSE_HISTORY_SOURCE_IDENTITY_REQUIRED")
            path, digest = binding["path"], binding["sha256"]
        else:
            path, digest = name, binding
        if not hash_value(digest) or sha(path) != digest:
            raise ValueError("IMPULSE_HISTORY_SOURCE_CHANGED")
    return {"status": report["status"], "reference": reference,
            "review": report, "old_failures_reclassified": False}


def compose_report(cases, plan, history):
    """Pure aggregation; callers must first verify every native case and history."""
    legacy = pair_checks(cases, "contact_50us", "contact_25us", plan["limits"])
    newton_diagnostic = pair_checks(cases, "contact_25us", "contact_12p5us", plan["limits"])
    refined = pair_checks(cases, "contact_pgs_25us", "contact_pgs_12p5us", plan["limits"])
    case_checks = [{"case": label, "passed": cases[label]["passed"]} for label in REQUIRED_LABELS]
    negative_checks = [item for item in case_checks if item["case"] in REQUIRED_LABELS[-2:]]
    kkt_checks = [{"case": label, "passed": cases[label].get("pgs_kkt", {}).get("status") == "PASS"}
                  for label in REQUIRED_LABELS]
    impulse = {}
    for label in LABELS:
        check = next(item for item in cases[label]["checks"] if item["name"] == "full_step_impulse_balance")
        impulse[label] = {"check": check, "status": "PASS" if check["passed"] else "FAIL",
                          "required_for_refined_acceptance": label in REQUIRED_LABELS,
                          "denominator": "max(abs(I*delta_omega - drive_impulse), abs(drive_impulse), 1e-15)"}
    numerical_pass = (refined["status"] == "PASS" and all(item["passed"] for item in case_checks)
                      and all(item["passed"] for item in kkt_checks))
    historical_failure_preserved = legacy["status"] == "FAIL"
    if not numerical_pass or not historical_failure_preserved:
        status = "FAIL"
    elif history["status"] != "REVIEWED_WITH_LIMITATIONS":
        status = "EVIDENCE_INSUFFICIENT"
    else:
        status = "PASS_SCOPED_REFINED_CONTACT_QUALIFICATION"
    return {"schema": "rlsok.contact-refinement-result/1", "status": status,
            "numerical_refinement_status": "PASS" if numerical_pass else "FAIL", "cases": cases,
            "numerical_recipes": {
                "legacy_convergence_50_25": {**legacy, "role": "historical_failure_recheck_only",
                    "required_for_refined_acceptance": False, "original_failure_preserved": historical_failure_preserved},
                "newton_diagnostic_convergence_25_12p5": {**newton_diagnostic,
                    "role": "single_variable_timestep_diagnosis_only", "required_for_refined_acceptance": False},
                "refined_convergence_pgs_25_12p5": {**refined, "role": "current_pgs_convergence_acceptance",
                    "required_for_refined_acceptance": True},
                "refined_case_checks": {"status": "PASS" if all(item["passed"] for item in case_checks) else "FAIL",
                    "checks": case_checks, "required_for_refined_acceptance": True},
                "native_scalar_pgs_kkt": {"status": "PASS" if all(item["passed"] for item in kkt_checks) else "FAIL",
                    "checks": kkt_checks, "required_for_refined_acceptance": True},
                "independent_impulse_balances": {"cases": impulse, "old_checks_and_denominators_unchanged": True},
                "negative_controls": {"status": "PASS" if all(item["passed"] for item in negative_checks) else "FAIL",
                    "checks": negative_checks, "native_engineering_acceptance_required": "FAIL"}},
            "three_level_newton_diagnostic_trend": [{"case": label, "timestep_seconds": plan["cases"][label]["timestep_seconds"],
                "measurements": cases[label]["measurements"], "individual_case_passed": cases[label]["passed"]}
                for label in LABELS[:3]],
            "pgs_refinement_trend": [{"case": label, "timestep_seconds": plan["cases"][label]["timestep_seconds"],
                "measurements": cases[label]["measurements"], "individual_case_passed": cases[label]["passed"]}
                for label in REQUIRED_LABELS[:2]],
            "impulse_history_review": history, "old_r2_qualification": "FAIL_PRESERVED",
            "old_failures_reclassified": False, "solver_executions_by_this_analysis": 0,
            "platform_goal_achieved": False, "assumptions": plan["assumptions"],
            "limitations": ["Only the PGS 25/12.5 us pair and four PGS cases qualify the current scope; the Newton triple is diagnosis only.",
                "All Newton per-case and pair failures remain explicit; a newly observed diagnostic failure does not relabel any historical failure.",
                "PGS dualFinish makes force/acceleration balance an algebraic identity; independent per-step scalar KKT, impulse and refinement checks are required.",
                "Endpoint trapezoid quadrature is not exact RK4 substage integration; sampled peaks are not continuous-time maxima.",
                "No hardware/material calibration, friction, strength, or unrestricted-geometry qualification is implied."]}


def analyze_refinement(runs, plan_path, expected_worker_sha256, expected_plan_sha256, impulse_history_review=None):
    plan_path = Path(plan_path).resolve()
    plan = validate_plan(plan_path, expected_worker_sha256, expected_plan_sha256)
    if (not isinstance(runs, dict) or set(runs) != set(LABELS)
            or any(not isinstance(path, str) for path in runs.values())
            or len({str(Path(path).resolve()).casefold() for path in runs.values()}) != len(LABELS)):
        raise ValueError("EXACT_SEVEN_DISTINCT_NATIVE_EVIDENCE_RUNS_REQUIRED")
    for label, binding in plan["reused_runs"].items():
        if Path(runs[label]).resolve() != Path(binding["directory"]).resolve():
            raise ValueError("ORIGINAL_SEALED_R2_RUN_LOCATION_REQUIRED")
        for name, expected in binding["files"].items():
            if sha(Path(runs[label]) / name) != expected:
                raise ValueError("SEALED_R2_NATIVE_EVIDENCE_CHANGED")
    history = history_review(impulse_history_review)
    cases = {}
    for label in LABELS:
        worker_hash = plan["baseline_worker_sha256"] if label in LABELS[:2] else expected_worker_sha256
        case = analyze_case(runs[label], label, plan, plan_path.parent, worker_hash)
        native_result = read(Path(runs[label]) / "result.json")
        if (native_result.get("engineering_acceptance") not in ("PASS_SCOPED_SIMULATION", "FAIL")
                or native_result.get("failure_kind") not in (None, "DESIGN_VIOLATION")):
            raise ValueError("TOOL_OR_MODEL_FAILURE_IS_NOT_A_CONTINUABLE_PHYSICS_DIAGNOSIS")
        case["role"] = "current_pgs_acceptance" if label in REQUIRED_LABELS else "newton_diagnostic_only"
        case["expected_worker_sha256"] = worker_hash
        if label in NEW_LABELS:
            header, rows = load_dynamics(Path(runs[label]) / "dynamics.jsonl", plan["cases"][label])
            case["compiled_observer_check"] = compiled_observer_check(header, plan["cases"][label], label in REQUIRED_LABELS)
            if case["compiled_observer_check"]["status"] != "PASS":
                raise ValueError("NATIVE_COMPILED_RECIPE_MISMATCH")
            case["observer_summary"] = observation_summary(header, rows)
            if label in REQUIRED_LABELS:
                case["pgs_kkt"] = scalar_pgs_kkt(header, rows, plan["scalar_kkt_limits"])
            case["passed"] = case["passed"] and case["compiled_observer_check"]["status"] == "PASS"
            del rows
        cases[label] = case
    result = compose_report(cases, plan, history)
    result.update(plan_sha256=sha(plan_path), expected_worker_sha256=expected_worker_sha256)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--runs", required=True)
    parser.add_argument("--worker-sha256", required=True)
    parser.add_argument("--plan-sha256", required=True)
    parser.add_argument("--history-review", help="JSON file containing an explicit path/sha256 review reference")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    try:
        result = analyze_refinement(read(args.runs), args.plan, args.worker_sha256, args.plan_sha256,
                                    read(args.history_review) if args.history_review else None)
    except (ValueError, KeyError, OSError, TypeError, StopIteration) as error:
        result = {"schema": "rlsok.contact-refinement-result/1", "status": "EVIDENCE_INSUFFICIENT",
                  "reason": str(error), "solver_executions_by_this_analysis": 0, "platform_goal_achieved": False}
    Path(args.out).write_text(json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return 0 if result["status"] == "PASS_SCOPED_REFINED_CONTACT_QUALIFICATION" else 2


if __name__ == "__main__":
    raise SystemExit(main())
