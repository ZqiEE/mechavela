"""Replay files for the console's simulation viewer (schema rlsok.replay/1, kind "robot").

    <run>/replay/index.json   label, planned runs, finished runs
    <run>/replay/scene.json   every visible geom of the compiled MuJoCo model (meshes as base64 float32/uint32)
    <run>/replay/run-NN.json  world pose of every body at 25 Hz, plus tracking/height/tilt series from the trace

Geometry comes from the compiled model itself (m.mesh_vert / m.mesh_face are already scaled and centred), so any
MuJoCo robot works without reading its asset files. Written while the benchmark runs; files replaced atomically.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import mujoco
import numpy as np

SCHEMA = "rlsok.replay/1"
RATE_HZ = 25
GEOM = {mujoco.mjtGeom.mjGEOM_MESH: "mesh", mujoco.mjtGeom.mjGEOM_BOX: "box", mujoco.mjtGeom.mjGEOM_SPHERE: "sphere",
        mujoco.mjtGeom.mjGEOM_CAPSULE: "capsule", mujoco.mjtGeom.mjGEOM_CYLINDER: "cylinder",
        mujoco.mjtGeom.mjGEOM_ELLIPSOID: "ellipsoid"}


def _b64(a, dt) -> str:
    return base64.b64encode(np.ascontiguousarray(a, dtype=dt).tobytes()).decode()


def _write(p: Path, obj) -> None:
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, p)


def scene(m) -> dict:
    """Visible geoms of bodies other than the world (the viewer draws its own floor)."""
    meshes, geoms = {}, []
    for g in range(m.ngeom):
        if m.geom_bodyid[g] == 0 or m.geom_type[g] not in GEOM or m.geom_group[g] >= 3:
            continue
        rgba = m.mat_rgba[m.geom_matid[g]] if m.geom_matid[g] >= 0 else m.geom_rgba[g]
        if rgba[3] <= 0.01:
            continue
        e = {"body": int(m.geom_bodyid[g]), "type": GEOM[m.geom_type[g]], "size": m.geom_size[g].round(5).tolist(),
             "pos": m.geom_pos[g].round(6).tolist(), "quat": m.geom_quat[g].round(6).tolist(), "rgba": np.round(rgba, 3).tolist()}
        if e["type"] == "mesh":
            mid = int(m.geom_dataid[g])
            if mid not in meshes:
                va, vn, fa, fn = m.mesh_vertadr[mid], m.mesh_vertnum[mid], m.mesh_faceadr[mid], m.mesh_facenum[mid]
                meshes[mid] = {"id": mid, "v": _b64(m.mesh_vert[va:va + vn], "<f4"), "f": _b64(m.mesh_face[fa:fa + fn], "<u4")}
            e["mesh"] = mid
        geoms.append(e)
    return {"schema": SCHEMA, "nbody": int(m.nbody), "meshes": list(meshes.values()), "geoms": geoms}


def start(rdir: Path, m, root_body: int, planned: list[str], label: dict) -> None:
    rdir.mkdir(parents=True, exist_ok=True)
    _write(rdir / "scene.json", scene(m))
    _write(rdir / "index.json", {"schema": SCHEMA, "kind": "robot", "label": label, "scene": "scene.json", "root": root_body,
                                 "rate_hz": RATE_HZ, "planned": planned, "runs": [], "live": True})


def add_run(rdir: Path, name: str, res, root_body: int) -> None:
    if not res.poses or not res.poses["t"]:
        return
    idx = json.loads((rdir / "index.json").read_text(encoding="utf-8"))
    P = res.poses
    tr = res.trace
    t = np.asarray(tr.get("t", []), dtype=float)
    cmd = np.asarray(tr.get("cmd", [[0, 0, 0]] * len(t)), dtype=float)

    def on_pose_time(y):           # trace is per control step; resample onto the pose clock
        return np.interp(P["t"], t, np.asarray(y, dtype=float)) if len(t) > 1 else np.zeros(len(P["t"]))
    series = [
        {"zh": "前进速度", "en": "Forward speed", "unit": "m/s",
         "lines": [{"zh": "实际", "en": "actual", "y": _b64(on_pose_time(tr["vx"]), "<f4")},
                   {"zh": "指令", "en": "command", "y": _b64(on_pose_time(cmd[:, 0]), "<f4"), "dash": True}]},
        {"zh": "躯干高度", "en": "Torso height", "unit": "m", "lines": [{"zh": "", "en": "", "y": _b64(on_pose_time(tr["z"]), "<f4")}]},
        {"zh": "倾斜角", "en": "Tilt", "unit": "°", "lines": [{"zh": "", "en": "", "y": _b64(np.degrees(on_pose_time(tr["tilt"])), "<f4")}]},
    ]
    if tr.get("temp_max"):
        series.append({"zh": "最热电机温度（假设热参数）", "en": "Hottest motor (assumed thermal model)", "unit": "°C",
                       "lines": [{"zh": "", "en": "", "y": _b64(on_pose_time(tr["temp_max"]), "<f4")}]})
    n = len(idx["runs"])
    fname = f"run-{n:02d}.json"
    _write(rdir / fname, {"schema": SCHEMA, "name": name, "nbody": int(np.asarray(P["pos"]).shape[1]), "root": root_body,
                          "t": _b64(P["t"], "<f4"), "pos": _b64(np.asarray(P["pos"]), "<f4"), "quat": _b64(np.asarray(P["quat"]), "<f4"),
                          "series": series, "fell": res.fell, "fall_time": res.fall_time})
    idx["runs"].append({"name": name, "file": fname, "passed": not res.fell, "duration_s": float(P["t"][-1])})
    _write(rdir / "index.json", idx)


def finish(rdir: Path) -> None:
    p = rdir / "index.json"
    if p.exists():
        idx = json.loads(p.read_text(encoding="utf-8"))
        idx["live"] = False
        _write(p, idx)


def label_for(key: str) -> dict:
    return {"status": "UNVERIFIED",
            "reasons": ["simulation in RLSOK MuJoCo: the official policy on the nominal model; not footage of a real robot",
                        "a real robot's behaviour is not verified by this replay"]}
