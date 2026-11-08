"""Prompts for the external AI in the bird design loop. Facts come only from the platform's computations."""
from __future__ import annotations

import json

STATUS = """## Status of the engines (be honest with the user about this)
- Forces of DelFly-like X-wings: validated on public free-flight data, cycle averages only, 11-15 Hz, 0.25-1.4 m/s.
- Conventional two-wing ornithopters (wings_per_side = 1): classical quasi-steady strip theory with ideal feathering,
  NO validation data yet; it is likely optimistic (no unsteady losses).
- Pitch dynamics: failed validation; the flight simulation uses an assumed pitch lag.
So every design result is UNVERIFIED. Do not claim otherwise in your reply."""

SPEC_FORMAT = """## Reply format
Reply with ONE JSON object in a ```json fenced block:
{
  "interpretation": "what the user wants physically, with every value you had to assume",
  "max_mass_g": 450,                       // flying mass limit
  "min_endurance_min": 8,                  // at cruise
  "cruise_speed_mps": 6.0,
  "budget": {"value": 3000, "currency": "CNY"},   // the user's budget; use the user's currency
  "printer_bed_mm": [256, 256, 256],       // the user's 3D printer (assume a common 220-256 mm bed if not given)
  "required_categories": ["camera", "vtx", "flight_controller"],   // from: motor esc battery servo flight_controller gps camera vtx
  "assumptions": ["..."]
}
Never weaken a number the user stated. If the request is impossible as stated, still write the honest spec."""

DESIGN_FORMAT = """## Reply format
Reply with ONE JSON object in a ```json fenced block:
{
  "author": "your model name",
  "diagnosis": "for revisions: why the previous numbers failed (cite them)",
  "changes": "what you changed and why",
  "wing": {"semi_span_m": 0.55, "root_chord_m": 0.22, "tip_chord_m": 0.12},
  "wings_per_side": 1,                                   // 1 = conventional ornithopter, 2 = X-wing clap-and-fling
  "kinematics": {"amplitude_deg": 30, "bias_deg": 5, "dihedral_deg": 5, "upstroke_load": 0.3},
  "body_cda_m2": 0.003,                                  // fuselage + tail drag area estimate
  "parts": [{"id": "<part id from the library>", "qty": 1}],
  "drive": {"motor_id": "<id>", "battery_id": "<id>", "gear_ratio": 40},
  "print": {"material": "PLA", "fill_fraction": 0.35},
  "cad_code": "from build123d import *\\n...\\nparts = {\\"gearbox_frame\\": frame, \\"fuselage\\": body}"
}
CAD rules (checked before running; a violation rejects the round):
- Python with build123d only (import build123d / from build123d import *, and math). Millimetres.
- Assign `parts = {name: Part}` with every printed part (frame, gearbox, crank, fuselage, tail mounts...). Wings are
  carbon rod + Mylar film and are NOT printed; the platform computes their mass from the wing size.
- No file access, no other imports, no while-loops, no names starting with "_", no path-like strings.
- Each part must fit the printer bed in some orientation."""


def _library_table(lib: dict) -> str:
    rows = []
    for p in lib["parts"].values():
        pr = p.get("price") or {}
        price = f"{pr.get('value')} {pr.get('currency')}" if pr.get("value") is not None else "no price"
        rows.append(f"- {p['id']} | {p['category']} | {p['mass_g']} g | {json.dumps(p.get('specs', {}), ensure_ascii=False)} | ref. price {price}")
    return "\n".join(rows)


def spec_prompt(requirement, previous_error=None):
    text = ["# RLSOK bird task - step 1: draft the frozen design spec", "", "## User requirement (verbatim)", requirement, "",
            "Turn the requirement into measurable limits. The platform adds its own checks (level-flight trim, motor speed and",
            "power margins, 60 s closed-loop flight, printability, required parts, reference-price budget).", "", STATUS, SPEC_FORMAT]
    if previous_error:
        text += ["", "## Your previous spec draft was rejected:", previous_error, "Fix it."]
    return "\n".join(text)


def design_prompt(requirement, spec, lib, round_number, max_rounds, history, notes=None):
    text = [f"# RLSOK bird task - design round {round_number} of {max_rounds}", "", "## User requirement (verbatim)", requirement, "",
            "## Frozen spec (cannot be changed)", "```json", json.dumps(spec, ensure_ascii=False, indent=1), "```", "",
            "## Parts library (id | category | mass | specs | reference price, prices are snapshots, not guaranteed)",
            _library_table(lib), "", STATUS]
    for h in history:
        text.append(f"\n### Round {h['round']}: {h['status']}")
        if h.get("changes"):
            text.append("Your change: " + h["changes"][:500])
        if h.get("reason"):
            text.append("Platform message: " + h["reason"][:1200])
        for c in h.get("checks") or []:
            act = "None" if c["actual"] is None else f"{c['actual']:.4g}"
            text.append(f"- {c['id']}: actual={act} {c['op']} {c['value']} {c['unit']} -> {'PASS' if c['passed'] else 'FAIL'}")
        if h.get("numbers"):
            text.append("Computed: " + json.dumps(h["numbers"], ensure_ascii=False)[:1500])
    text.append("\nDiagnose from these numbers and submit a revised design." if history else "\nSubmit the initial design.")
    if notes:
        text += ["", "## Notes from the user, added during the run (guidance only; the frozen spec decides PASS/FAIL)"]
        text += [f"- (before round {n['for_round']}) {n['text']}" for n in notes]
    text.append(DESIGN_FORMAT)
    return "\n".join(text)
