"""Replay files for the console's simulation viewer (schema rlsok.replay/1, kind "bird").

    <eval>/replay/index.json   rig (CAD meshes, wing/tail numbers, flap kinematics), label, planned runs, finished runs
    <eval>/replay/run-NN.json  one mission run: 25 Hz channels as base64 float32, phase segments, events, judgement

Written while the evaluation runs (index first, then one file per finished run), so the viewer can show the bird
before the sweep ends. Every file is replaced atomically. The flap angle is not stored: the viewer computes it from
the logged flap phase and the design's amplitude/bias, the same kinematics the force model uses.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import numpy as np

SCHEMA = "rlsok.replay/1"
RATE_HZ = 25
CHANNELS = ("t", "x", "y", "h", "chi", "pitch", "bank", "freq", "flap_phase", "V", "battery_pct", "p_elec_w", "alt_ref")


def _write(p: Path, obj) -> None:
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, p)


def f32(a) -> str:
    return base64.b64encode(np.asarray(a, dtype="<f4").tobytes()).decode()


def start(rdir: Path, design: dict, cad: dict, label: dict, planned: list[str], meta: dict) -> None:
    rdir.mkdir(parents=True, exist_ok=True)
    k = design.get("kinematics", {})
    rig = {"wing": design["wing"], "wings_per_side": int(design.get("wings_per_side", 1)),
           "amplitude_deg": k.get("amplitude_deg", 30), "bias_deg": k.get("bias_deg", 5), "dihedral_deg": k.get("dihedral_deg", 0),
           "tail": design.get("tail", {}), "assembly": design.get("assembly"),
           # printed parts drawn from the CAD STL files next to this folder (mm); the rest from the numbers above.
           # With an "assembly" the listed parts are placed at their positions; otherwise head + body_mid are stacked.
           "meshes": [{"name": n, "file": f"../cad/{n}.stl", "bbox_mm": v["bbox_mm"]} for n, v in cad.items()
                      if n in ((design.get("assembly") or {}).get("meshes") or ("head", "body_mid"))]}
    _write(rdir / "index.json", {"schema": SCHEMA, "kind": "bird", "label": label, "rig": rig, "rate_hz": RATE_HZ,
                                 "channels": list(CHANNELS), "planned": planned, "runs": [], "live": True, **meta})


def add_run(rdir: Path, name: str, run: dict, judged: dict) -> None:
    idx = json.loads((rdir / "index.json").read_text(encoding="utf-8"))
    log = run["log"]
    t = np.asarray(log["t"])
    step = max(1, int(round(1.0 / (RATE_HZ * (t[1] - t[0]))))) if len(t) > 1 else 1
    sel = slice(0, None, step)
    ch = {c: f32(np.asarray(log[c], dtype=float)[sel]) for c in CHANNELS}
    phases, segs = log["phase"][sel], []
    for i, ph in enumerate(phases):
        if not segs or segs[-1][1] != ph:
            segs.append([float(t[sel][i]), ph])
    n = len(idx["runs"])
    fname = f"run-{n:02d}.json"
    _write(rdir / fname, {"schema": SCHEMA, "name": name, "n": int(len(t[sel])), "channels": ch, "phases": segs,
                          "events": run["events"], "gains": run["gains"], "passed": judged["passed"], "rules": judged["rules"],
                          "numbers": judged["numbers"]})
    idx["runs"].append({"name": name, "file": fname, "passed": judged["passed"],
                        "duration_s": float(t[-1]) if len(t) else 0.0})
    _write(rdir / "index.json", idx)


def finish(rdir: Path, verdict: str) -> None:
    p = rdir / "index.json"
    if p.exists():
        idx = json.loads(p.read_text(encoding="utf-8"))
        idx["live"] = False
        idx["verdict"] = verdict
        _write(p, idx)
