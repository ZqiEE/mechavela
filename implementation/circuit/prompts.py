"""Prompt text sent to the external AI. Facts only come from native results."""
from __future__ import annotations

import json

RULES = """
## Platform rules (enforced by RLSOK; a violating reply is rejected and still counts as an iteration)
- Simulator: ngspice {version} (native shared library, batch). Data-only netlists: no .control, .include, .lib, file sources, braces/expressions, .param, .meas, analysis cards.
- The testbench (supplies, loads, stimulus, analysis, thresholds) is FROZEN in the spec. In design rounds you only write the DUT subcircuit body.
- DUT body = the lines inside `.subckt dut <ports>` ... `.ends` (the platform writes those two lines itself).
- Allowed DUT elements: R C L K D Q M S V and `.model` lines. Not allowed: J (JFET), B/E/F/G/H controlled sources, I sources, X subcircuits, .param, `ic=` initial conditions.
  There are no op-amp or IC macromodels: build amplifiers/regulators/oscillators from discrete devices.
- Every D/Q/M/S element must reference a `.model` defined in the DUT body. Use realistic parameters of a real, purchasable part class
  (diode IS/RS/N/CJO/BV/TT; BJT IS/BF/BR/VAF/RB/RC/RE/CJE/CJC/TF; MOSFET: VDMOS or level 1 with VTO/KP/RD/RS/CGSO/CGDO; switch RON/ROFF/VT/VH).
  Do not fake performance with zero-resistance or zero-capacitance ideal models; add series resistance (ESR, winding resistance) where it matters.
- Name every element that only models a parasitic of another part (ESR, ESL, winding or trace resistance, leakage) <R|L|C>PAR_<part>,
  e.g. RPAR_C1 for the ESR of C1, RPAR_L1 for the winding resistance of L1. They are simulated and toleranced, but the BOM lists them as
  properties of their parent part, not as parts to buy.
- Use the DUT ground port for ground; do NOT use node 0 inside the DUT.
- Internal V sources are allowed only as signal generators (PWM/clock/gate drive), not in AC designs. After every transient/op simulation the platform
  measures the mean |p(t)| of each internal source; if their sum exceeds 2% of the power delivered by the testbench sources (+1 µW) the design
  FAILS check `platform_internal_source_power`. You cannot power the load from an ideal source.
- R, L and C values in the DUT will be varied by ±{tol_pct}% (one at a time, two global corners and seeded Monte Carlo); the design must pass ALL checks
  in ALL cases. Semiconductor/switch model parameters are not varied. Prefer standard E12/E24 values.
- Numbers: plain SPICE numbers with suffixes (10k, 4.7u, 1meg). Note `m` = milli, `meg` = mega.
"""

SPEC_FORMAT = """
## Reply format
Reply with ONE JSON object in a ```json fenced block:
{
  "interpretation": "what the requirement means physically, incl. every value you had to assume",
  "ports": ["vin", "vout", "gnd"],            // DUT port order; 2-8 names
  "testbench": [                              // frozen supplies/stimulus/load; node 0 = ground
    "VIN in 0 DC 12",
    "XDUT in out 0 dut",                       // exactly one XDUT instance, nodes in port order, one port on node 0
    "RLOAD out 0 10"                           // loads the requirement implies (R/C/L/I/V allowed; no other X)
  ],
  "analysis": {"kind": "tran", "step_seconds": 1e-6, "stop_seconds": 5e-3, "max_step_seconds": 1e-6},
              // or {"kind": "op"}  or {"kind": "ac", "sweep": "dec", "points": 50, "start_hz": 10, "stop_hz": 1e6}
  "vectors": ["v(out)", "v(in)", "vin#branch"],  // node voltages v(node); current through a V source: <name>#branch (lowercase)
  "metrics": {
    "vout_avg": {"vector": "v(out)", "stat": "mean", "window": [4e-3, 5e-3], "unit": "V"},
    "vout_ripple": {"vector": "v(out)", "stat": "pp", "window": [4e-3, 5e-3], "unit": "V"},
    "pin": {"vector": "v(in)", "multiply": "vin#branch", "scale": -1, "stat": "mean", "window": [4e-3, 5e-3], "unit": "W"},
    "pout": {"vector": "v(out)", "multiply": "v(out)", "scale": 0.1, "stat": "mean", "window": [4e-3, 5e-3], "unit": "W"},
    "efficiency": {"derived": "ratio", "of": ["pout", "pin"]}
  },
  "checks": [
    {"id": "vout_regulation", "metric": "vout_avg", "op": "between", "min": 4.75, "max": 5.25, "unit": "V", "description": "..."},
    {"id": "ripple", "metric": "vout_ripple", "op": "le", "value": 0.05, "unit": "V", "description": "..."}
  ],
  "operating_conditions": {"input_voltage": {"value": 12, "unit": "V"}, "load_current": {"value": 0.5, "unit": "A"}},
  "assumptions": ["e.g. regulation band ±5% because the requirement gave no tolerance"]
}
Statistics:
- tran (optional "window": [t0, t1] seconds): final, initial, min, max, mean, pp, rms, abs_max,
  freq (mean frequency of rising crossings; optional "threshold" in volts, default mid of min/max), duty (fraction of time above threshold)
- op: value
- ac (complex vectors; optional "window": [f0, f1] Hz): db_at / phase_at (need "at_hz"), max_db, min_db,
  f_cross_db (first crossing of "level_db", or of "drop_db" below the value at "ref_hz"). Put "AC 1" on the stimulus source so dB = gain.
Optional "multiply" (product with another recorded vector, tran/op only) and "scale" (constant). Derived: {"derived": "ratio"|"difference", "of": [m1, m2]}.
SPICE branch current of a V source is positive flowing INTO its + node through the source, so a supply that delivers power has a negative #branch current.
Checks must express what the user asked for. Where the user gave no number, choose a conventional engineering limit and state it in assumptions;
never weaken a number the user stated. Measure steady state after start-up (leave settling time before tran windows).
Power supplies / converters / regulators (a DC load in operating_conditions.load_current) MUST also be checked for what breaks real
hardware, or the spec is rejected:
- check id `startup_overshoot`: metric with stat "max" on the output over a window that starts at 0 (start-up included), limit at most
  10% above the nominal output unless the user stated otherwise;
- a load step in the testbench (e.g. `ISTEP out 0 PULSE(0 <half the rated current> <t_step> 1u 1u <long> <long>)` added after steady state)
  and check id `load_step`: output min (stat "min") over a window covering the step stays inside the regulation band.
If the requirement is physically impossible as stated, still write the honest spec of what was asked (do not relax it): the design rounds will show it.
"""

DESIGN_FORMAT = """
## Reply format
Reply with ONE JSON object in a ```json fenced block:
{
  "author": "your model / agent name",
  "diagnosis": "for revisions: why the previous native results failed (cite the numbers)",
  "changes": "what you changed and the expected physical effect",
  "dut_body": ["R1 vin n1 1k", "...", ".model DMOD D(IS=1e-14 RS=0.05 N=1.05)"],
  "part_suggestions": {"Q1": "e.g. TIP31C class NPN (unverified)"},
  "declare_infeasible": false,
  "infeasible_reason": ""
}
Set declare_infeasible=true only after at least one simulated round, and only if you conclude from physics and the measured numbers that the
frozen spec cannot be met under these rules; explain with numbers. Otherwise always submit a complete DUT.
"""


def spec_prompt(requirement, version, tolerance, previous_error=None):
    text = ["# RLSOK circuit task - step 1: draft the verification spec",
            "", "## User requirement (verbatim)", requirement, "",
            "Turn the requirement into a frozen, measurable verification spec. The testbench must apply the stated input and load conditions.",
            "The DUT is designed in the next step, so describe only ports/testbench/analysis/metrics/checks here.",
            RULES.format(version=version, tol_pct=round(tolerance * 100, 3)), SPEC_FORMAT]
    if previous_error:
        text += ["", "## Your previous spec draft was rejected by the platform validator:", previous_error, "Fix it."]
    return "\n".join(text)


def design_prompt(requirement, spec, version, tolerance, round_number, max_rounds, history, notes=None):
    text = ["# RLSOK circuit task - design round %d of %d" % (round_number, max_rounds),
            "", "## User requirement (verbatim)", requirement, "",
            "## Frozen verification spec (cannot be changed)", "```json",
            json.dumps({k: spec[k] for k in ("ports", "testbench", "analysis", "metrics", "checks", "operating_conditions", "assumptions")},
                       ensure_ascii=False, indent=1), "```",
            RULES.format(version=version, tol_pct=round(tolerance * 100, 3))]
    if history:
        text += ["", "## Native simulation history (facts measured by ngspice; most recent last)"]
        for item in history:
            text.append("### Round %d: %s" % (item["round"], item["status"]))
            if item.get("changes"):
                text.append("Your change: " + item["changes"][:600])
            if item.get("dut_body"):
                text += ["DUT body submitted:", "```", "\n".join(item["dut_body"]), "```"]
            if item.get("reason"):
                text.append("Platform/solver message: " + item["reason"][:1500])
            for check in item.get("checks") or []:
                limit = ("[%s, %s]" % (check.get("min"), check.get("max"))) if check["op"] == "between" else "%s %s" % (check["op"], check.get("value"))
                text.append("- %s: actual=%s limit %s -> %s%s" % (check["id"], _fmt(check.get("actual")), limit,
                                                                   "PASS" if check["passed"] else "FAIL",
                                                                   (" (" + check["error"] + ")") if check.get("error") else ""))
            if item.get("metrics"):
                text.append("All measured metrics: " + ", ".join("%s=%s" % (k, _fmt(v)) for k, v in item["metrics"].items()))
            if item.get("audit"):
                text.append("Power audit: " + json.dumps(item["audit"], ensure_ascii=False))
            if item.get("tolerance"):
                tol = item["tolerance"]
                text.append("Tolerance sweep: %d/%d cases passed." % (tol["passed_cases"], tol["total_cases"]))
                for check_id, worst in tol.get("worst", {}).items():
                    text.append("  worst %s: actual=%s in case %s (%s)" % (check_id, _fmt(worst["actual"]), worst["case"],
                                                                            "PASS" if worst["passed"] else "FAIL"))
                for case in tol.get("failed_examples", [])[:5]:
                    text.append("  failed case %s factors=%s failed=%s" % (case["id"], case["factors"], case["failed"]))
        text.append("")
        text.append("Diagnose from these numbers and submit a revised DUT.")
    else:
        text.append("\nSubmit the initial DUT design.")
    if notes:
        text += ["", "## Notes from the user, added during the run (guidance only: the frozen spec and checks above still decide PASS/FAIL)"]
        text += ["- (before round %d) %s" % (n["for_round"], n["text"]) for n in notes]
    text.append(DESIGN_FORMAT)
    return "\n".join(text)


def _fmt(value):
    return "None" if value is None else "{:.6g}".format(value)
