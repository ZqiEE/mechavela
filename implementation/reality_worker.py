"""Native solver adapters. Physics is computed by MuJoCo or ngspice, never here.

Runs as a child of the existing Windows Job Object runner. Input snapshots are
data, not executable Python. Each process loads exactly one solver.
"""
import ctypes as ct
import json
import math
import os
import re
from pathlib import Path
import sys
import traceback
import xml.etree.ElementTree as ET


def write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")


def finite(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("NONFINITE_SOLVER_RESULT")
    return number


def transient_endpoint_metrics(actual, requested):
    """Retain the raw endpoint; account only for floating-point time rounding.

    This does not change the time vector or extrapolate any physical value.
    The policy is part of the worker version frozen before each new run.
    """
    actual, requested = finite(actual), finite(requested)
    if requested <= 0 or abs(actual - requested) > min(1e-12, 1e-10 * requested):
        raise RuntimeError("INCOMPLETE_TRANSIENT")
    return {"simulated_seconds": requested, "raw_simulated_seconds": actual,
            "endpoint_rounding_tolerance_seconds": min(1e-12, 1e-10 * requested)}


def dynamics_export_supported(nv, nq, steps):
    """Bound optional full-step evidence independently of display sampling."""
    return (0 < nv <= 16 and 0 < nq <= 32
            and (steps + 1) * (nv * nv + 12 * nv + nq) <= 2_000_000)


def actuator_unit_labels(metadata):
    """Recognize units only for the compiled, direct unit-gear hinge model."""
    direct = (metadata["transmission"] == "joint" and metadata.get("joint_type") == "hinge"
              and metadata["gear"] == [1., 0., 0., 0., 0., 0.])
    if not direct:
        return "native", "native"
    gain, bias = metadata["gain_parameters"], metadata["bias_parameters"]
    if metadata["dynamics_type"] == "none" and metadata["gain_type"] == "fixed":
        if metadata["bias_type"] == "none" and gain[0] == 1.:
            return "N*m", "N*m"
        if (metadata["bias_type"] == "affine" and gain[0] > 0 and bias[0] == 0
                and bias[1] == -gain[0] and bias[2] <= 0):
            return "rad", "N*m"
    return "native", "N*m"


class BoundedDynamicsWriter:
    """Stream raw evidence; truncation never yields a complete footer."""
    def __init__(self, stream, header, byte_limit=64 * 1024 * 1024):
        self.stream, self.byte_limit, self.bytes, self.rows = stream, byte_limit, 0, 0
        self.emit({"record": "header", **header})

    def emit(self, value):
        line = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")) + "\n"
        size = len(line.encode("utf-8"))
        if self.bytes + size > self.byte_limit:
            raise ValueError("FULL_STEP_DYNAMICS_EVIDENCE_LIMIT_EXCEEDED")
        self.stream.write(line)
        self.bytes += size

    def state(self, value):
        self.emit({"record": "state", **value})
        self.rows += 1

    def complete(self, steps):
        if self.rows != steps + 1:
            raise ValueError("INCOMPLETE_FULL_STEP_DYNAMICS")
        self.emit({"record": "footer", "complete": True, "state_count": self.rows, "steps": steps})


def validate_mjcf(text, project):
    if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        raise ValueError("XML_ENTITIES_UNSUPPORTED")
    root = ET.fromstring(text)
    if root.tag != "mujoco":
        raise ValueError("MJCF_REQUIRED")
    # User supplied native plugins are outside this data-only integration.
    for element in root.iter():
        if element.tag in ("extension", "plugin", "include"):
            raise ValueError("Inline MJCF required; plugins/includes are not integrated")
        for key in ("file", "meshdir", "texturedir", "assetdir"):
            name = element.get(key)
            if name is not None:
                path = Path(name)
                if path.is_absolute() or ".." in path.parts or ":" in name:
                    raise ValueError("PROJECT_RELATIVE_ASSETS_REQUIRED")
                if not (project / path).resolve().is_relative_to(project.resolve()):
                    raise ValueError("ASSET_OUTSIDE_PROJECT")
    return root


def mechanical(request, project, entry, runtime):
    params = request["parameters"]
    steps, every = params["steps"], params["sample_every"]
    if any(type(value) is not int or not 1 <= value <= 100000 for value in (steps, every)) or steps // every > 10000:
        raise ValueError("BOUNDED_MECHANICAL_STEPS_AND_SAMPLES_REQUIRED")
    if not isinstance(params.get("bodies"), list) or not 1 <= len(params["bodies"]) <= 64:
        raise ValueError("BOUNDED_NAMED_BODY_LIST_REQUIRED")
    joints = params.get("joints", [])
    if not isinstance(joints, list) or len(joints) > 64:
        raise ValueError("BOUNDED_NAMED_HINGE_LIST_REQUIRED")
    for names in (params["bodies"], joints):
        if any(not isinstance(name, str) or not name or len(name) > 64 for name in names) or len(names) != len(set(names)):
            raise ValueError("UNIQUE_NONEMPTY_MECHANICAL_NAMES_REQUIRED")

    def scalar(value):
        if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > 1e9:
            raise ValueError("FINITE_BOUNDED_MECHANICAL_INPUT_REQUIRED")
        return float(value)

    schedule = None
    if "torque_schedule_file" in params:
        if "controls" in params:
            raise ValueError("CONSTANT_CONTROLS_AND_TORQUE_SCHEDULE_ARE_EXCLUSIVE")
        name = params["torque_schedule_file"]
        if not isinstance(name, str) or name not in request["project"]["files"]:
            raise ValueError("TORQUE_SCHEDULE_MUST_BE_DECLARED_PROJECT_FILE")
        path = project / name
        if "\\" in name or ":" in name or Path(name).is_absolute() or ".." in Path(name).parts or not path.resolve().is_relative_to(project.resolve()):
            raise ValueError("PROJECT_RELATIVE_TORQUE_SCHEDULE_REQUIRED")
        schedule = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(schedule, dict) or set(schedule) != {"schema", "units", "time_seconds", "actuators"} or schedule["schema"] != "rlsok.torque-schedule/1" or schedule["units"] != {"time": "s", "torque": "N*m"}:
            raise ValueError("EXPLICIT_SI_TORQUE_SCHEDULE_SCHEMA_REQUIRED")
        times = schedule["time_seconds"]
        if not isinstance(times, list) or not 2 <= len(times) <= 10001:
            raise ValueError("BOUNDED_TORQUE_SCHEDULE_REQUIRED")
        times = [scalar(value) for value in times]
        if times[0] != 0.0 or any(right <= left for left, right in zip(times, times[1:])):
            raise ValueError("STRICTLY_INCREASING_SCHEDULE_FROM_ZERO_REQUIRED")
        if not isinstance(schedule["actuators"], dict) or not 1 <= len(schedule["actuators"]) <= 128:
            raise ValueError("NAMED_ACTUATOR_TORQUE_SERIES_REQUIRED")
        series = {}
        for name, values in schedule["actuators"].items():
            if not isinstance(name, str) or not name or not isinstance(values, list) or len(values) != len(times):
                raise ValueError("MATCHED_ACTUATOR_TIME_AND_TORQUE_SERIES_REQUIRED")
            series[name] = [scalar(value) for value in values]

    # Small simulations do not need BLAS thread pools or their per-thread memory.
    for key in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[key] = "1"
    sys.path.insert(0, runtime["directory"])
    import mujoco as mj
    import numpy as np

    validate_mjcf(entry.read_text(encoding="utf-8-sig"), project)
    model = mj.MjModel.from_xml_path(str(entry))
    if model.nbody > 1024 or model.nq > 4096 or model.ngeom > 4096 or model.nu > 128:
        raise ValueError("MODEL_EXCEEDS_CURRENT_ADAPTER_LIMIT")
    duration = finite(model.opt.timestep) * steps
    if not 0 < duration <= 1e6:
        raise ValueError("FINITE_POSITIVE_BOUNDED_SIMULATION_DURATION_REQUIRED")
    data = mj.MjData(model)
    controls = params.get("controls", [0.0] * model.nu)
    if not isinstance(controls, list) or len(controls) != model.nu:
        raise ValueError("ONE_CONSTANT_CONTROL_PER_ACTUATOR_REQUIRED")
    data.ctrl[:] = [scalar(value) for value in controls]
    body_ids = {name: mj.mj_name2id(model, mj.mjtObj.mjOBJ_BODY, name) for name in params["bodies"]}
    if any(index <= 0 for index in body_ids.values()):
        raise ValueError("NAMED_MOVING_BODY_NOT_FOUND")
    joint_ids = {name: mj.mj_name2id(model, mj.mjtObj.mjOBJ_JOINT, name) for name in joints}
    if any(index < 0 or model.jnt_type[index] != mj.mjtJoint.mjJNT_HINGE for index in joint_ids.values()):
        raise ValueError("NAMED_HINGE_JOINT_REQUIRED_FOR_RADIAN_OUTPUT")
    if (len(model.actuator_trntype) != model.nu or len(data.actuator_force) != model.nu
            or (hasattr(model, "actuator_ctrlnum") and any(int(count) != 1 for count in model.actuator_ctrlnum))
            or (hasattr(model, "actuator_outnum") and any(int(count) != 1 for count in model.actuator_outnum))):
        raise ValueError("SINGLE_INPUT_SINGLE_OUTPUT_ACTUATORS_REQUIRED")
    actuator_names = [mj.mj_id2name(model, mj.mjtObj.mjOBJ_ACTUATOR, index) or "actuator_" + str(index) for index in range(model.nu)]
    if len(set(actuator_names)) != model.nu:
        raise ValueError("UNIQUE_SINGLE_OUTPUT_ACTUATORS_REQUIRED")

    def direct_torque_actuator(index):
        joint = int(model.actuator_trnid[index, 0])
        return (model.actuator_trntype[index] == mj.mjtTrn.mjTRN_JOINT and joint >= 0
                and model.jnt_type[joint] == mj.mjtJoint.mjJNT_HINGE
                and model.actuator_dyntype[index] == mj.mjtDyn.mjDYN_NONE
                and model.actuator_gaintype[index] == mj.mjtGain.mjGAIN_FIXED
                and model.actuator_biastype[index] == mj.mjtBias.mjBIAS_NONE
                and (not hasattr(model, "actuator_delay") or finite(model.actuator_delay[index]) == 0.0)
                and np.array_equal(model.actuator_gear[index], [1.0, 0.0, 0.0, 0.0, 0.0, 0.0])
                and finite(model.actuator_gainprm[index, 0]) == 1.0)

    direct_torque = [direct_torque_actuator(index) for index in range(model.nu)]
    def enum_label(enum, value, prefix):
        return enum(int(value)).name.removeprefix(prefix).lower()

    actuator_properties = {}
    for index, name in enumerate(actuator_names):
        joint = int(model.actuator_trnid[index, 0])
        joint_transmission = model.actuator_trntype[index] == mj.mjtTrn.mjTRN_JOINT
        metadata = {
            "transmission": enum_label(mj.mjtTrn, model.actuator_trntype[index], "mjTRN_"),
            "joint": mj.mj_id2name(model, mj.mjtObj.mjOBJ_JOINT, joint) if joint_transmission else None,
            "joint_type": enum_label(mj.mjtJoint, model.jnt_type[joint], "mjJNT_") if joint_transmission else None,
            "gear": [finite(value) for value in model.actuator_gear[index]],
            "gain_type": enum_label(mj.mjtGain, model.actuator_gaintype[index], "mjGAIN_"),
            "gain_parameters": [finite(value) for value in model.actuator_gainprm[index]],
            "bias_type": enum_label(mj.mjtBias, model.actuator_biastype[index], "mjBIAS_"),
            "bias_parameters": [finite(value) for value in model.actuator_biasprm[index]],
            "dynamics_type": enum_label(mj.mjtDyn, model.actuator_dyntype[index], "mjDYN_"),
            "control_limited": bool(model.actuator_ctrllimited[index]),
            "force_limited": bool(model.actuator_forcelimited[index]),
            "control_range": [finite(value) for value in model.actuator_ctrlrange[index]],
            "force_range": [finite(value) for value in model.actuator_forcerange[index]],
        }
        metadata["control_unit"], metadata["force_unit"] = actuator_unit_labels(metadata)
        actuator_properties[name] = metadata
    if schedule is not None:
        if set(series) != set(actuator_names) or any(mj.mj_id2name(model, mj.mjtObj.mjOBJ_ACTUATOR, index) is None for index in range(model.nu)):
            raise ValueError("SCHEDULE_MUST_NAME_EVERY_MODEL_ACTUATOR_EXACTLY")
        if not all(direct_torque):
            raise ValueError("SCHEDULE_REQUIRES_DIRECT_UNIT_GEAR_HINGE_TORQUE_MOTORS")
        if any(model.jnt_actfrclimited[int(model.actuator_trnid[index, 0])] for index in range(model.nu)):
            raise ValueError("JOINT_ACTUATOR_FORCE_CLIPPING_UNSUPPORTED_FOR_TORQUE_SCHEDULE")
        if times[-1] + 1e-12 < duration:
            raise ValueError("TORQUE_SCHEDULE_MUST_COVER_COMPLETE_SIMULATION")
        for index, name in enumerate(actuator_names):
            for limited, bounds in ((model.actuator_ctrllimited[index], model.actuator_ctrlrange[index]),
                                    (model.actuator_forcelimited[index], model.actuator_forcerange[index])):
                if limited and (min(series[name]) < bounds[0] or max(series[name]) > bounds[1]):
                    raise ValueError("TORQUE_SCHEDULE_WOULD_BE_CLIPPED: " + name)

    def apply_controls():
        if schedule is not None:
            data.ctrl[:] = [np.interp(data.time, times, series[name]) for name in actuator_names]

    apply_controls()
    mj.mj_forward(model, data)
    frames, positions = [], {name: [] for name in body_ids}
    contact_max, force_max = 0, 0.0
    joint_series, actuator_series = {}, {}
    force = np.zeros(6)
    velocity = np.zeros(6)
    body_velocity_series = {}
    audit_enabled = dynamics_export_supported(model.nv, model.nq, steps)
    audit_mass = np.zeros((model.nv, model.nv)) if audit_enabled else None
    dof_properties = [{"index": index, "joint": mj.mj_id2name(model, mj.mjtObj.mjOBJ_JOINT, int(model.dof_jntid[index])),
                       "joint_type": enum_label(mj.mjtJoint, model.jnt_type[int(model.dof_jntid[index])], "mjJNT_")}
                      for index in range(model.nv)]

    def record_extrema(target, name, value):
        value = finite(value)
        if name not in target:
            target[name] = {"initial": value, "final": value, "min": value, "max": value}
        else:
            values = target[name]
            values["final"] = value
            values["min"] = min(values["min"], value)
            values["max"] = max(values["max"], value)
        return value

    def joint_states(record=False):
        states = {}
        for name, index in joint_ids.items():
            q, v = int(model.jnt_qposadr[index]), int(model.jnt_dofadr[index])
            state = {"position_rad": finite(data.qpos[q]), "velocity_rad_per_s": finite(data.qvel[v]),
                     "actuator_torque_Nm": finite(data.qfrc_actuator[v]), "constraint_torque_Nm": finite(data.qfrc_constraint[v]),
                     "passive_torque_Nm": finite(data.qfrc_passive[v])}
            if record:
                for key, value in state.items():
                    record_extrema(joint_series, "joint." + name + "." + key, value)
            states[name] = state
        return states

    def actuator_states(record=False):
        states = {}
        for index, name in enumerate(actuator_names):
            suffix = "Nm" if direct_torque[index] else "native"
            state = {"control_" + suffix: finite(data.ctrl[index]), "force_" + suffix: finite(data.actuator_force[index])}
            metadata = actuator_properties[name]
            if metadata["control_unit"] == "rad":
                state["control_rad"] = finite(data.ctrl[index])
            if metadata["force_unit"] == "N*m":
                state["force_Nm"] = finite(data.actuator_force[index])
            if record:
                for key, value in state.items():
                    record_extrema(actuator_series, "actuator." + name + "." + key, value)
            states[name] = state
        return states

    def observe():
        nonlocal contact_max, force_max
        if any(not np.isfinite(values).all() for values in (data.qpos, data.qvel, data.qacc, data.actuator_force, data.qfrc_constraint)):
            raise ValueError("NONFINITE_SOLVER_STATE")
        if data.ncon > 128:
            raise ValueError("CONTACT_REPORT_LIMIT_EXCEEDED")
        contact_max = max(contact_max, int(data.ncon))
        for contact in range(data.ncon):
            mj.mj_contactForce(model, data, contact, force)
            if not np.isfinite(force).all():
                raise ValueError("NONFINITE_CONTACT_RESULT")
            force_max = max(force_max, finite(np.linalg.norm(force[:3])))
        joint_states(record=True)
        actuator_states(record=True)
        for name, index in body_ids.items():
            mj.mj_objectVelocity(model, data, mj.mjtObj.mjOBJ_BODY, index, velocity, 0)
            for axis, value in zip("xyz", velocity[3:]):
                record_extrema(body_velocity_series, "body." + name + ".velocity_" + axis + "_m_per_s", value)

    def body_states():
        states = {}
        for name, index in body_ids.items():
            position = [finite(x) for x in data.xpos[index]]
            mj.mj_objectVelocity(model, data, mj.mjtObj.mjOBJ_BODY, index, velocity, 0)
            states[name] = {"position_m": position, "quaternion_wxyz": [finite(x) for x in data.xquat[index]],
                            "linear_velocity_world_m_per_s": [finite(value) for value in velocity[3:]],
                            "angular_velocity_world_rad_per_s": [finite(value) for value in velocity[:3]]}
        return states

    def contact_states():
        contacts = []
        for index in range(data.ncon):
            contact = data.contact[index]
            mj.mj_contactForce(model, data, index, force)
            contacts.append({"geom_ids": [int(contact.geom1), int(contact.geom2)],
                             "geom_names": [mj.mj_id2name(model, mj.mjtObj.mjOBJ_GEOM, int(geom)) for geom in (contact.geom1, contact.geom2)],
                             "position_m": [finite(value) for value in contact.pos], "distance_m": finite(contact.dist),
                             "contact_frame_rows_world": [finite(value) for value in contact.frame],
                             "force_contact_frame_N": [finite(value) for value in force[:3]],
                             "torque_contact_frame_Nm": [finite(value) for value in force[3:]]})
        return contacts

    def sample():
        states = body_states()
        for name, state in states.items():
            positions[name].append(state["position_m"])
        frames.append({"time_seconds": finite(data.time), "bodies": states, "joints": joint_states(),
                       "actuators": actuator_states(), "contacts": int(data.ncon), "contact_data": contact_states()})

    def dynamics_state(step):
        # MuJoCo >=3.16 signature. This reads the native matrix, never estimates it.
        mj.mj_fullM(model, data, audit_mass)
        terms = {name: [finite(value) for value in getattr(data, "qfrc_" + name)]
                 for name in ("actuator", "passive", "constraint", "bias", "applied", "smooth", "fluid")}
        return {"step": step, "time_seconds": finite(data.time),
                "qpos": [finite(value) for value in data.qpos], "qvel": [finite(value) for value in data.qvel],
                "qacc": [finite(value) for value in data.qacc],
                "mass_matrix": [[finite(value) for value in row] for row in audit_mass],
                "generalized_momentum": [finite(value) for value in audit_mass @ data.qvel],
                "generalized_forces": terms,
                "body_applied_wrenches_force_then_torque": [[finite(value) for value in row] for row in data.xfrc_applied],
                "bodies": body_states(), "joints": joint_states(), "actuators": actuator_states(),
                "contact_data": contact_states()}

    audit_stream = open("dynamics.jsonl", "w", encoding="utf-8", newline="\n") if audit_enabled else None
    try:
        audit = BoundedDynamicsWriter(audit_stream, {
            "schema": "rlsok.mechanical-dynamics/1", "engine": "mujoco", "engine_version": str(mj.__version__),
            "steps": steps, "nv": model.nv, "nq": model.nq, "dofs": dof_properties,
            "timestep_seconds": finite(model.opt.timestep),
            "integrator": enum_label(mj.mjtIntegrator, model.opt.integrator, "mjINT_"),
            "sampling": "initial and every native step after mj_forward; independent of trajectory sample_every",
            "force_semantics": "instantaneous end-state forces; not RK4 substage averages or exact discrete impulses",
            "momentum_semantics": "M(q)*qvel; simple impulse balance only after independently establishing constant M and coordinate interpretation",
            "units": {"hinge_dof_force": "N*m", "slide_dof_force": "N", "time": "s", "body_velocity": "m/s"},
        }) if audit_enabled else None
        observe()
        sample()
        if audit:
            audit.state(dynamics_state(0))
        for step in range(1, steps + 1):
            # Controls are held over this native step. No replacement integrator.
            apply_controls()
            mj.mj_step(model, data)
            apply_controls()
            mj.mj_forward(model, data)
            observe()
            if audit:
                audit.state(dynamics_state(step))
            if step % every == 0 or step == steps:
                sample()
        if audit:
            audit.complete(steps)
    finally:
        if audit_stream:
            audit_stream.close()
    if any(int(warning.number) for warning in data.warning):
        raise ValueError("SOLVER_WARNING: inspect the model, timestep and native log")
    metrics = {"simulated_seconds": finite(data.time), "steps": steps, "max_contact_count": contact_max,
               "max_contact_force_N": force_max, "timestep_seconds": finite(model.opt.timestep),
               "total_body_mass_kg": finite(mj.mj_getTotalmass(model))}
    metrics["full_step_dynamics_exported"] = int(audit_enabled)
    for series_metrics in (joint_series, actuator_series, body_velocity_series):
        for name, values in series_metrics.items():
            prefix, quantity = name.rsplit(".", 1)
            for aggregate, value in values.items():
                metrics[prefix + "." + aggregate + "_" + quantity] = value
    body_properties = {}
    for name, index in body_ids.items():
        body_properties[name] = {"mass_kg": finite(model.body_mass[index]),
                                 "principal_inertia_kg_m2": [finite(value) for value in model.body_inertia[index]],
                                 "inertial_position_body_m": [finite(value) for value in model.body_ipos[index]],
                                 "inertial_quaternion_body_wxyz": [finite(value) for value in model.body_iquat[index]]}
        metrics["body." + name + ".mass_kg"] = body_properties[name]["mass_kg"]
        for axis, value in zip("xyz", body_properties[name]["principal_inertia_kg_m2"]):
            metrics["body." + name + ".principal_inertia_" + axis + "_kg_m2"] = value
    joint_properties = {}
    for name, index in joint_ids.items():
        dof = int(model.jnt_dofadr[index])
        joint_properties[name] = {"type": "hinge", "axis_body": [finite(value) for value in model.jnt_axis[index]],
                                  "armature_kg_m2": finite(model.dof_armature[dof]),
                                  "damping_Nm_s_per_rad": finite(model.dof_damping[dof]),
                                  "frictionloss_Nm": finite(model.dof_frictionloss[dof])}
        metrics["joint." + name + ".armature_kg_m2"] = joint_properties[name]["armature_kg_m2"]
    for name, points in positions.items():
        for axis, label in enumerate("xyz"):
            prefix = "body." + name + "."
            metrics[prefix + "initial_" + label + "_m"] = points[0][axis]
            metrics[prefix + "final_" + label + "_m"] = points[-1][axis]
            metrics[prefix + "min_" + label + "_m"] = min(p[axis] for p in points)
            metrics[prefix + "max_" + label + "_m"] = max(p[axis] for p in points)
    write("trajectory.json", {"schema": "rlsok.mechanical-trajectory/2", "engine": "mujoco",
                              "units": {"position": "m", "time": "s", "joint_position": "rad", "joint_velocity": "rad/s",
                                        "joint_torque": "N*m", "contact_force": "N", "contact_torque": "N*m",
                                        "mass": "kg", "inertia": "kg*m^2", "non_direct_actuator": "native MJCF actuator units"},
                              "model": {"bodies": body_properties, "joints": joint_properties, "actuators": actuator_properties,
                                        "gravity_m_per_s2": [finite(value) for value in model.opt.gravity],
                                        "integrator": enum_label(mj.mjtIntegrator, model.opt.integrator, "mjINT_"),
                                        "fluid": {"density_kg_per_m3": finite(model.opt.density),
                                                  "dynamic_viscosity_Pa_s": finite(model.opt.viscosity),
                                                  "wind_world_m_per_s": [finite(value) for value in model.opt.wind],
                                                  "geom_native_fluid_parameters": {mj.mj_id2name(model, mj.mjtObj.mjOBJ_GEOM, i) or str(i):
                                                      [finite(value) for value in model.geom_fluid[i]] for i in range(model.ngeom)}}},
                              "sampling": {"body_extrema": "recorded frames", "joint_actuator_contact_extrema": "every native step and initial state",
                                           "full_step_dynamics": "dynamics.jsonl" if audit_enabled else "NOT_EXPORTED_BOUNDED_MODEL_LIMIT",
                                           "torque_schedule": "linear interpolation at step start, held during integration; reported forces evaluated at frame time",
                                           "contact_wrench_frame": "contact frame; first axis is contact normal, other axes are tangents"},
                              "frames": frames})
    if params.get("render", False):
        # This is MuJoCo's renderer showing its computed final state.
        from PIL import Image
        with mj.Renderer(model, height=360, width=640) as renderer:
            camera = mj.MjvCamera()
            mj.mjv_defaultFreeCamera(model, camera)
            renderer.update_scene(data, camera)
            Image.fromarray(renderer.render()).save("simulation.png")
    return metrics, str(mj.__version__), ["Rigid-body/contact simulation of the supplied MJCF only.",
                                        "Torque schedule interpolation is sampled at the integration timestep; timestep convergence requires separate validation.",
                                        "Mass/inertia, friction, material and collision assumptions are those in the supplied model; they are not measured hardware properties.",
                                        "Native fluid forces, when enabled, use MuJoCo's stateless approximate body/ellipsoid models; no resolved flow field or CFD qualification.",
                                        "Full-step end-state force quadrature is not the internal RK4 quadrature; independent timestep and momentum checks are required.",
                                        "No strength, fatigue, thermal or manufacturing qualification."]


class VectorInfo(ct.Structure):
    _fields_ = [("name", ct.c_char_p), ("type", ct.c_int), ("flags", ct.c_short),
                ("real", ct.POINTER(ct.c_double)), ("complex", ct.c_void_p), ("length", ct.c_int)]


def validate_spice(text):
    # ngspice control scripts can launch OS commands; accept circuit data only.
    directives = {".model", ".param", ".subckt", ".ends", ".temp", ".option", ".options",
                  ".ic", ".nodeset", ".global", ".func", ".end", ".title"}
    lines = text.splitlines()
    if len(lines) < 2 or len(lines) > 10000:
        raise ValueError("BOUNDED_SPICE_NETLIST_REQUIRED")
    for line in lines[1:]:
        value = line.strip()
        if value and not value.startswith("*") and (re.search(r"\b(?:file|filesource|external)\b", value, re.IGNORECASE) or "`" in value):
            raise ValueError("EXTERNAL_SPICE_DATA_SOURCE_UNSUPPORTED")
        if not value or value.startswith(("*", "+")):
            continue
        first = value.split()[0].lower()
        if (first.startswith(".") and first not in directives) or (not first.startswith(".") and first[0] not in "rlcvidqmbe fghkxs".replace(" ", "")):
            raise ValueError("SELF_CONTAINED_CIRCUIT_DATA_REQUIRED: " + first)
    if not any(line.strip().lower() == ".end" for line in lines):
        raise ValueError("NETLIST_END_REQUIRED")
    return lines


def electrical(request, project, entry, runtime):
    lines = validate_spice(entry.read_text(encoding="utf-8-sig"))
    folder = os.add_dll_directory(str(Path(runtime["library"]).parent))
    lib = ct.CDLL(runtime["library"])
    messages, exits = [], []
    send_type = ct.CFUNCTYPE(ct.c_int, ct.c_char_p, ct.c_int, ct.c_void_p)
    exit_type = ct.CFUNCTYPE(ct.c_int, ct.c_int, ct.c_bool, ct.c_bool, ct.c_int, ct.c_void_p)

    @send_type
    def send(message, _ident, _user):
        text = message.decode("utf-8", "replace")
        messages.append(text)
        print(text, flush=True)
        return 0

    @send_type
    def status(_text, _ident, _user):
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
    # KiCad's build accesses command-parser state in the pre-init setters.
    # Use ngspice's documented startup-file directory with a local empty file.
    # The global/user KiCad configuration is never edited.
    startup = Path.cwd() / "spice-startup"
    startup.mkdir()
    (startup / ".spiceinit").write_text("* RLSOK isolated native run\n", encoding="ascii")
    os.environ["SPICE_USERINIT_DIR"] = str(startup)
    os.environ["SPICE_SCRIPTS"] = str(startup)
    print("Native initialization: ngSpice_Init", flush=True)
    if lib.ngSpice_Init(send, status, leave, None, None, None, None) != 0:
        raise RuntimeError("NGSPICE_INIT_FAILED")
    lib.ngSpice_Command(b"set nomoremode")
    lib.ngSpice_Command(b"version")
    encoded = [line.encode("utf-8") for line in lines]
    circuit = (ct.c_char_p * (len(encoded) + 1))(*encoded, None)
    if lib.ngSpice_Circ(circuit) != 0 or exits:
        raise ValueError("NGSPICE_MODEL_REJECTED")
    analysis = request["parameters"]["analysis"]
    command = "op" if analysis["kind"] == "op" else "tran {:.17g} {:.17g}".format(analysis["step_seconds"], analysis["stop_seconds"])
    if lib.ngSpice_Command(command.encode("ascii")) != 0 or exits:
        raise RuntimeError("NGSPICE_ANALYSIS_FAILED")
    if any(message.lower().startswith(("stderr error", "stderr fatal")) for message in messages):
        raise RuntimeError("NGSPICE_ERROR: see native stdout.log")
    vectors, metrics = {}, {}
    names = request["parameters"]["vectors"] + (["time"] if analysis["kind"] == "transient" else [])
    for name in dict.fromkeys(names):
        pointer = lib.ngGet_Vec_Info(name.encode("ascii"))
        if not pointer:
            raise ValueError("MISSING_SOLVER_VECTOR: " + name)
        vector = pointer.contents
        if not vector.real or not 0 < vector.length <= 25000 or vector.complex:
            raise ValueError("BOUNDED_REAL_VECTOR_REQUIRED: " + name)
        values = [finite(vector.real[i]) for i in range(vector.length)]
        vectors[name] = values
        metrics.update({name + ".initial": values[0], name + ".final": values[-1],
                        name + ".min": min(values), name + ".max": max(values)})
    lengths = {len(values) for values in vectors.values()}
    if len(lengths) != 1:
        raise ValueError("SOLVER_VECTOR_LENGTH_MISMATCH")
    metrics["sample_count"] = next(iter(lengths))
    if "time" in vectors:
        metrics.update(transient_endpoint_metrics(vectors["time"][-1], analysis["stop_seconds"]))
    write("vectors.json", {"schema": "rlsok.circuit-vectors/1", "engine": "ngspice", "vectors": vectors,
                           "units": "Native SPICE SI units: time=s, node voltage=V, branch current=A."})
    version = next((line for line in messages if "ngspice-" in line.lower()), "UNKNOWN: inspect native version log")
    folder.close()
    return metrics, version, ["Supplied self-contained netlist and chosen analysis only.",
                              "Transient endpoint rounding only: min(1e-12 s, 1e-10 * requested duration); raw timestamps and physical samples remain unchanged.",
                              "Component models and tolerances require domain review; no PCB or real hardware qualification."]


def main():
    sys.dont_write_bytecode = True
    request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    runtime = request["runtime"]
    project = Path(sys.argv[2]).resolve()
    try:
        adapter = {"mujoco": mechanical, "ngspice": electrical}[request["engine"]]
        metrics, version, limits = adapter(request, project, project / request["project"]["entry"], runtime)
        checks = []
        for check in request["checks"]:
            if check["metric"] not in metrics:
                raise ValueError("REQUESTED_METRIC_UNAVAILABLE: " + check["metric"])
            actual, target = metrics[check["metric"]], check["value"]
            passed = actual >= target if check["op"] == "ge" else actual <= target if check["op"] == "le" else actual == target
            checks.append({**check, "actual": actual, "passed": passed})
        passed = all(check["passed"] for check in checks)
        result = {"schema": "rlsok.reality-result/1", "engine": request["engine"], "native_version": version,
                  "scope": request["scope"], "metrics": metrics, "checks": checks,
                  "engineering_acceptance": "PASS_SCOPED_SIMULATION" if passed else "FAIL",
                  "failure_kind": None if passed else "DESIGN_VIOLATION", "limitations": limits,
                  "user_goal_achieved": False}
        write("result.json", result)
        return 0 if passed else 2
    except Exception as error:
        write("result.json", {"schema": "rlsok.reality-result/1", "engine": request["engine"],
                              "engineering_acceptance": "EVIDENCE_INSUFFICIENT", "failure_kind": "TOOL_OR_MODEL_ERROR",
                              "reason": str(error), "user_goal_achieved": False})
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
