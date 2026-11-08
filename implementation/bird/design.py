"""Evaluate one AI-proposed bird design against a frozen spec: CAD, mass, power, endurance, trim and flight.

Design JSON (written by the external AI):
  wing: {semi_span_m, root_chord_m, tip_chord_m}, wings_per_side: 1 | 2,
  kinematics: {amplitude_deg, bias_deg, dihedral_deg},
  cruise_speed_mps, parts: [{id, qty}] from release/bird/parts_library.json,
  drive: {motor_id, gear_ratio, battery_id}, cad_code: build123d code that assigns parts={...} (mm),
  print: {material: PLA|PETG, fill_fraction}
Spec (frozen before the first design):
  max_mass_g, min_endurance_min, cruise_speed_mps, budget {value, currency}, printer_bed_mm [x, y, z],
  required_categories (e.g. ["camera", "vtx", "flight_controller"])

Every result is labelled UNVERIFIED: the force model is validated only for DelFly-like X-wings, and a bird designed
here is a different configuration. Nothing in this module changes that label.
"""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path

from implementation.bird import aero as A
from implementation.bird import cad_sandbox as C
from implementation.bird import flight as F

ROOT = Path(__file__).resolve().parents[2]
LIBRARY = ROOT / "release" / "bird" / "parts_library.json"
DENSITY = {"PLA": 1.24, "PETG": 1.27}          # g/cm3
LABEL = {"status": "UNVERIFIED",
         "reasons": ["X-wing force model validated only for DelFly-like X-wings at 11-15 Hz and 0.25-1.4 m/s (usage/bird-aero-validation-20261021)",
                     "two-wing model (DeLaurier + stroke-plane tilt) passed the UVLM cross-check and the SmartBird point but FAILED the Eye-Bird slow-flight check (usage/bird-aero-two-wing-20261022)",
                     "pitch dynamics are assumed: attitude validation failed four times (usage/bird-attitude-validation-20261021)",
                     "power chain calibrated on one public DelFly II endurance flight (shaft-to-electric 0.27, release/bird/parts_library.json endurance_calibration): endurance is an upper bound; wiring mass is an assumption"]}
SPEC_KEYS = {"max_mass_g", "min_endurance_min", "cruise_speed_mps", "budget", "printer_bed_mm", "required_categories"}


def load_library(local: Path | None = None) -> dict:
    lib = json.loads(LIBRARY.read_text(encoding="utf-8"))
    parts = {p["id"]: p for p in lib["parts"]}
    if local and local.exists():                        # user / their AI may override prices locally
        for p in json.loads(local.read_text(encoding="utf-8")).get("parts", []):
            parts[p["id"]] = {**parts.get(p["id"], {}), **p, "local_override": True}
    return {"parts": parts, "eff": lib["assumed_efficiencies"], "policy": lib["price_policy"],
            "endurance_calibration": lib.get("endurance_calibration")}


def validate_spec(spec: dict) -> dict:
    missing = SPEC_KEYS - set(spec)
    if missing:
        raise ValueError("SPEC_MISSING: " + ", ".join(sorted(missing)))
    for k in ("max_mass_g", "min_endurance_min", "cruise_speed_mps"):
        if not isinstance(spec[k], (int, float)) or spec[k] <= 0:
            raise ValueError(f"SPEC_BAD_NUMBER: {k}")
    if not (isinstance(spec["printer_bed_mm"], list) and len(spec["printer_bed_mm"]) == 3):
        raise ValueError("SPEC_PRINTER_BED_NEEDS_3_NUMBERS")
    return {k: spec[k] for k in SPEC_KEYS | {"interpretation", "assumptions"} if k in spec}


def wing_mass_kg(w: dict, lib) -> float:
    rod = lib["parts"]["mat_carbon_rod_2mm_per_m"]["mass_g"] / 1000          # kg per metre
    film = lib["parts"]["mat_mylar_23um_per_m2"]["mass_g"] / 1000           # kg per m2
    if w.get("planform"):                                   # bird planform: outline area (film covers the feathers too)
        st = w["planform"]["stations"]
        area = sum((st[i + 1][0] - st[i][0]) * (st[i][2] + st[i + 1][2]) / 2 for i in range(len(st) - 1)) * w["semi_span_m"]
        n_prim = int(w["planform"].get("primaries", 0))
        rods = w["semi_span_m"] + 2 * w["root_chord_m"] + n_prim * 0.12   # spar, two stiffeners, one 120 mm rod per primary
    else:
        area = w["semi_span_m"] * (w["root_chord_m"] + w["tip_chord_m"]) / 2
        rods = w["semi_span_m"] + 2 * w["root_chord_m"]      # leading-edge spar + two stiffeners (assumed)
    return area * film + rods * rod


def aero_table(design: dict, lib: dict):
    """Cycle-averaged force table for a design: (table, model name, X-wing flapper or None, conventional wing or None)."""
    w = design["wing"]
    k = design.get("kinematics", {})
    body_cda = float(design.get("body_cda_m2", 0.003))         # fuselage + tail drag area (AI's estimate, default ASSUMED)
    fl = cw = None
    if int(design.get("wings_per_side", 1)) == 2:
        # DelFly-like X-wing: clap-and-fling model with the literature coefficients. The empirical airframe term of
        # validation attempt 2 is NOT carried over: it is specific to DelFly II and not physical.
        fl = copy.deepcopy(A.DELFLY_II_SLOW)
        fl.wing = A.Wing(semi_span=w["semi_span_m"], root_chord=w["root_chord_m"], tip_chord=w["tip_chord_m"], mass=wing_mass_kg(w, lib))
        fl.kin.amp = math.radians(k.get("amplitude_deg", 19.5)); fl.kin.bias = math.radians(k.get("bias_deg", 19.5))
        fl.kin.dihedral = math.radians(k.get("dihedral_deg", 12))
        fl.coef.Cl_per_mps = 0.8026288091777797
        table = F.ForceTable(fl, freqs=list(range(2, 22, 2)), speeds=[0, 0.5, 1, 1.5, 2, 3, 4, 6, 8],
                             alphas=[0, 0.2, 0.4, 0.6, 0.8, 1.0, 1.2, 1.4, 1.57])
        return table, "x_wing_clap_and_fling", fl, None
    pf = tuple((s[0], s[2], s[3]) for s in w["planform"]["stations"]) if w.get("planform") else None
    # two-wing model (2026-10-23, attempt F): DeLaurier modified strip theory, stroke plane tilting with the body (trim
    # angle = body pitch to the path), leading-edge-vortex lift (Polhamus) above Strouhal 0.3. On never-used E-Flap
    # flight logs it predicted the flap frequency within 8 % (median) and passed the UVLM cross-check; it missed the
    # registered SmartBird band by 1.6 points (release/bird/AERO_EFLAP_FLIGHT_CHECK_F.json), so the formal verdict is
    # FAILED and results stay UNVERIFIED. aero_model "strip" keeps the older model.
    if k.get("aero_model", "delaurier_tilt") == "strip":
        cw = A.ConventionalWing(semi_span=w["semi_span_m"], root_chord=w["root_chord_m"], tip_chord=w["tip_chord_m"], planform=pf,
                                stroke_amp=math.radians(k.get("amplitude_deg", 30)), stroke_bias=math.radians(k.get("bias_deg", 5)),
                                upstroke_load=float(k.get("upstroke_load", 0.3)))
        alphas = (0, 2, 4, 6, 8, 10, 12, 14, 16)
    else:
        cw = A.ConventionalWing(semi_span=w["semi_span_m"], root_chord=w["root_chord_m"], tip_chord=w["tip_chord_m"], planform=pf,
                                stroke_amp=math.radians(k.get("amplitude_deg", 30)), stroke_bias=math.radians(k.get("bias_deg", 5)),
                                lift_model="delaurier", tilt_trim=True, incidence=math.radians(k.get("incidence_deg", 0.0)),
                                twist_fraction=float(k.get("twist_fraction", 0.55)),     # 0.55: fitted on Eye-Bird (attempt F)
                                post_stall="polhamus_st")
        alphas = (0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 24, 28)
    table = F.ConventionalTable(cw, body_cda, freqs=[1, 2, 3, 4, 5, 6, 7, 8, 10, 12],
                                speeds=[0, 1, 2, 3, 4, 5, 6, 7, 8, 10], alphas=[math.radians(a) for a in alphas])
    return table, "conventional_strip_theory", None, cw


def check(cid, value, op, limit, unit, description):
    ok = {"le": value <= limit, "ge": value >= limit}[op] if value is not None else False
    return {"id": cid, "metric": cid, "op": op, "value": limit, "unit": unit, "actual": value, "passed": bool(ok),
            "margin": (limit - value if op == "le" else value - limit) if value is not None else None, "description": description}


def evaluate(spec: dict, design: dict, out_dir: Path, lib: dict, python: str | None = None) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    parts = lib["parts"]
    eff = lib["eff"]
    # ---- bill of materials
    bom, price_by_ccy, unknown_price = [], {}, []
    for item in design.get("parts", []):
        p = parts.get(item.get("id"))
        if not p:
            raise ValueError("UNKNOWN_PART: " + str(item.get("id")))
        qty = int(item.get("qty", 1))
        pr = p.get("price") or {}
        bom.append({"id": p["id"], "name": p["name"], "category": p["category"], "qty": qty, "mass_g_each": p["mass_g"],
                    "price_each": pr.get("value"), "currency": pr.get("currency"), "source": pr.get("source"),
                    "retrieved": pr.get("retrieved")})
        if pr.get("value") is None:
            unknown_price.append(p["id"])
        else:
            price_by_ccy[pr["currency"]] = price_by_ccy.get(pr["currency"], 0.0) + pr["value"] * qty
    cats = {b["category"] for b in bom}
    # ---- CAD (printed parts)
    cad = C.run(design["cad_code"], out_dir / "cad", python=python)["parts"]
    # viewer: show the printed bird as soon as the CAD exists; mission runs are added as they finish
    from implementation.bird import mission as M, replay as RP
    RP.start(out_dir / "replay", design, cad, {"status": "UNVERIFIED", "reasons": LABEL["reasons"] + M.LABEL["reasons"][:1]},
             [name for name, _, _ in M.cases(second_order=2)], {"cruise_speed_mps": spec["cruise_speed_mps"],
                                                                  "criteria": "release/bird/MISSION_CRITERIA_R3.json"})
    pr_cfg = design.get("print", {"material": "PLA", "fill_fraction": 0.35})
    rho = DENSITY.get(pr_cfg.get("material", "PLA"), 1.24)
    fill = float(pr_cfg.get("fill_fraction", 0.35))
    fill_by_part = pr_cfg.get("fill_by_part", {})          # thin shells print solid; frames use infill
    printed_by_part = {n: v["volume_mm3"] / 1000 * rho * float(fill_by_part.get(n, fill)) for n, v in cad.items()}
    printed_g = sum(printed_by_part.values())
    bed = spec["printer_bed_mm"]
    too_big = [n for n, v in cad.items() if not all(a <= b for a, b in zip(sorted(v["bbox_mm"]), sorted(bed)))]
    # ---- mass
    w = design["wing"]
    n_wings = 2 * int(design.get("wings_per_side", 1))
    wing_g = wing_mass_kg(w, lib) * 1000 * n_wings
    parts_g = sum(b["mass_g_each"] * b["qty"] for b in bom)
    mass_g = (parts_g + printed_g + wing_g) * (1 + eff["wiring_and_misc_mass_fraction"])
    # ---- aerodynamics
    V = spec["cruise_speed_mps"]
    table, model, fl, cw = aero_table(design, lib)
    bird = F.Bird(None, mass=mass_g / 1000)
    tr = F.trim(bird, table, V)
    # controller limits and gains scale with this design's trim (a 3 Hz ornithopter is not a 13 Hz DelFly)
    bird.gains = F.Gains(freq_min=float(table.f[0]), freq_max=float(table.f[-1]), climb_p=0.4 * tr["freq"],
                         climb_i=0.25 * tr["freq"], alt_p=0.8,
                         speed_p=0.05 if model == "conventional_strip_theory" else 0.35)
    if model == "conventional_strip_theory":
        cw2 = copy.deepcopy(cw); cw2.alpha_target = tr["pitch"]
        shaft_w = A.conventional_cycle(cw2, tr["freq"], V)["mean"]["shaft_power_w"]
    else:
        shaft_w = A.cycle(fl, tr["freq"], V, tr["pitch"])["mean"]["shaft_power_w_no_recovery"]
    drive = design.get("drive", {})
    motor = parts.get(drive.get("motor_id"), {})
    batt = parts.get(drive.get("battery_id"), {})
    avionics_w = sum(parts[b["id"]].get("specs", {}).get("power_w_assumed", 0) * b["qty"] for b in bom)
    # shaft -> electric: calibrated on the DelFly II 22.5 min flight (an upper bound on efficiency); old chain as fallback
    eta = eff.get("shaft_to_electric_calibrated", eff["motor"] * eff["gearbox_crank"])
    prop_elec_w = shaft_w / eta
    elec_w = prop_elec_w + avionics_w
    energy_wh = batt.get("specs", {}).get("energy_wh", 0) * eff["battery_usable_fraction"]
    endurance_min = 60 * energy_wh / elec_w if elec_w > 0 else None
    kv = motor.get("specs", {}).get("kv")
    v_batt = batt.get("specs", {}).get("nominal_v", 0)
    rpm_needed = tr["freq"] * float(drive.get("gear_ratio", 1)) * 60
    rpm_avail = kv * v_batt * 0.7 if kv else None         # loaded speed ~70 % of no-load (ASSUMED)
    motor_pmax = motor.get("specs", {}).get("max_power_w_assumed")
    sim = F.simulate(bird, table, duration=60, speed_ref=V, alt_ref=3.0,
                     gust=lambda t: (0.5, 0.0) if 20 < t < 22 else (0.0, 0.0)) if tr["feasible"] else None
    checks = [
        check("trim_feasible", tr["residual"], "le", 0.03, "×weight", "level flight possible at the cruise speed (force balance residual)"),
        check("mass", mass_g, "le", spec["max_mass_g"], "g", "flying mass incl. wiring allowance"),
        check("endurance", endurance_min, "ge", spec["min_endurance_min"], "min", "battery energy / electrical power at cruise (calibrated upper bound)"),
        check("motor_speed_margin", (rpm_avail / rpm_needed) if rpm_avail and rpm_needed else None, "ge", 1.2, "×", "motor speed available / needed"),
        check("motor_power_margin", (motor_pmax / (prop_elec_w * eff["motor"])) if motor_pmax and shaft_w > 0 else None, "ge", 2.0, "×", "motor rated power / needed"),
        check("flight_60s", 1.0 if sim and not sim["summary"]["crashed"] and sim["summary"]["alt_error_rms_last5s_m"] < 0.3 else 0.0,
              "ge", 1.0, "", "60 s closed-loop flight with a 0.5 m/s gust, altitude held within 0.3 m RMS"),
        check("printable", float(len(too_big)), "le", 0.0, "parts", "printed parts that do not fit the printer bed"),
        check("required_parts", float(len(set(spec["required_categories"]) - cats)), "le", 0.0, "missing", "required part categories present"),
    ]
    if model == "conventional_strip_theory" and tr["feasible"]:
        # pitch must still be able to add lift at cruise: +3 deg wing alpha gives >= 15 % more lift (not at the CL_max cap)
        L0 = table(tr["freq"], V, tr["pitch"])[1]
        L3 = table(tr["freq"], V, tr["pitch"] + math.radians(3))[1]
        reserve = (L3 / L0) if L0 > 0 and tr["pitch"] + math.radians(3) <= float(table.a[-1]) + 1e-9 else 1.0
        checks.append(check("pitch_lift_reserve", reserve, "ge", 1.15, "×", "lift at trim alpha + 3 deg / lift at trim (stall margin, pitch authority)"))
    if model == "conventional_strip_theory" and tr["feasible"] and rpm_avail:
        # full registered mission (release/bird/MISSION_CRITERIA_R1.json rules) at this spec's cruise speed
        flim = (float(table.f[0]), min(float(table.f[-1]), rpm_avail / 60.0 / float(drive.get("gear_ratio", 1))))
        mg = M.MissionGains(**design.get("mission_gains", {}))
        sw = M.sweep(table, mass_g / 1000, flim, mg, M.Mission(cruise_speed=V),
                     power={"eta": eta, "avionics_w": avionics_w, "energy_wh": energy_wh} if energy_wh > 0 else None,
                     on_run=lambda name, run, j: RP.add_run(out_dir / "replay", name, run, j), second_order=2)
        RP.finish(out_dir / "replay", sw["verdict"])
        n_ok = sum(r["passed"] for r in sw["runs"].values())
        checks.append(check("mission_sweep", float(n_ok), "ge", float(len(sw["runs"])), "runs",
                            "hand launch, climb 15 m, cruise, 360 deg turn, descent, flare and touchdown; 24 runs: attitude lags, oscillatory attitude incl. the measured E-Flap pitch mode, wind (MISSION_CRITERIA_R3)"))
        mission_out = {"verdict": sw["verdict"], "freq_limits_hz": flim, "gains": F.asdict(mg), "runs": sw["runs"], "label": sw["label"]}
        (out_dir / "mission").mkdir(exist_ok=True)
        (out_dir / "mission" / "mission_result.json").write_text(json.dumps(mission_out, indent=1, default=str), encoding="utf-8")
        (out_dir / "mission" / "nominal_log.json").write_text(json.dumps({k: sw["nominal"][k] for k in ("log", "events", "trim", "gains", "mission")}), encoding="utf-8")
    budget = spec["budget"]
    spend = price_by_ccy.get(budget.get("currency"))
    other_ccy = {c: v for c, v in price_by_ccy.items() if c != budget.get("currency")}
    checks.append(check("budget_reference", spend, "le", budget["value"], budget.get("currency", ""),
                        "sum of reference prices in the budget currency (other currencies listed separately, not converted)"))
    report = {"label": LABEL, "aero_model": model, "mass_g": mass_g, "mass_breakdown_g": {"parts": parts_g, "printed": printed_g, "wings": wing_g,
                                                                      "wiring_misc": mass_g - parts_g - printed_g - wing_g}, "printed_by_part_g": printed_by_part,
              "trim": tr, "shaft_power_w": shaft_w, "electrical_power_w": elec_w, "avionics_w": avionics_w,
              "endurance_min": endurance_min, "rpm_needed": rpm_needed, "rpm_available": rpm_avail,
              "cad": cad, "printed_too_big": too_big, "bom": bom, "price_by_currency": price_by_ccy,
              "prices_in_other_currencies": other_ccy, "parts_without_price": unknown_price, "price_policy": lib["policy"],
              "flight": sim["summary"] if sim else None, "checks": checks, "passed": all(c["passed"] for c in checks)}
    if sim:
        import gzip
        log = sim["log"]
        (out_dir / "nominal").mkdir(exist_ok=True)
        (out_dir / "nominal" / "vectors.json.gz").write_bytes(gzip.compress(json.dumps(
            {"analysis_command": "rlsok.bird.flight 60 s", "vectors": {"time": log["t"], "altitude_m": log["h"],
                                                                         "flap_freq_hz": log["freq"], "speed_mps": log["vx"]},
             "units": {"altitude_m": "m", "flap_freq_hz": "Hz", "speed_mps": "m/s"}}).encode()))
    params = {"RLSOK_BIRD_TRIM_FREQ_HZ": tr["freq"], "RLSOK_BIRD_TRIM_PITCH_DEG": math.degrees(tr["pitch"]),
              **{f"RLSOK_BIRD_{k.upper()}": v for k, v in F.asdict(bird.gains).items()}}
    (out_dir / "flight_params.json").write_text(json.dumps({"note": "RLSOK controller gains for this design (custom, not PX4 stock parameters); UNVERIFIED",
                                                            "params": params}, indent=1), encoding="utf-8")
    return report
