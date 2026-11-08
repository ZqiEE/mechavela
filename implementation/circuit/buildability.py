"""Is a passing circuit something you can actually build? (2026-10-21 customer walkthrough)

A design can pass every frozen check and still not be buildable as simulated, e.g. when it is driven by ideal
pulse sources inside the DUT (open loop, fixed duty, no real controller) or when start-up overshoot and load
steps were never checked. This module only reads the frozen spec and the final DUT; it never changes a verdict.
The console shows "仅理想仿真通过，不能直接制作" instead of "已验证" when any reason applies.
"""
from __future__ import annotations

TIME_VARYING = ("pulse(", "pwl(", "sin(", "exp(", "sffm(", "am(")


def is_power_spec(spec: dict) -> bool:
    cond = spec.get("operating_conditions") or {}
    return spec.get("analysis", {}).get("kind") == "tran" and any("load" in str(k).lower() for k in cond)


def _metric(spec, check):
    return (spec.get("metrics") or {}).get(check.get("metric"), {})


def has_startup_overshoot_check(spec: dict) -> bool:
    stop = float(spec.get("analysis", {}).get("stop_seconds") or 0)
    for c in spec.get("checks", []):
        m = _metric(spec, c)
        if m.get("stat") in ("max", "abs_max", "pp") and c.get("op") in ("le", "lt", "between"):
            w = m.get("window")
            if not w or float(w[0]) <= 0.05 * stop:
                return True
    return False


def has_load_step(spec: dict) -> bool:
    bench = [str(x).strip().lower() for x in spec.get("testbench", []) if not str(x).strip().lower().startswith("xdut")]
    varying = [x for x in bench[1:] if any(t in x.replace(" ", "") for t in TIME_VARYING)]   # bench[0] = main supply
    checked = any(_metric(spec, c).get("stat") in ("min", "max", "pp") and _metric(spec, c).get("window")
                  for c in spec.get("checks", []) if c.get("id") != "startup_overshoot")
    return bool(varying) and checked


def ideal_sources(dut_lines) -> list[str]:
    return [str(x).split()[0] for x in dut_lines or [] if str(x).strip()[:1].upper() == "V"]


def assess(spec: dict, dut_lines) -> dict:
    """{'grade': 'BUILDABLE_IN_SIM' | 'IDEAL_ONLY', 'reasons': [...]} - reasons are stable codes for the UI."""
    reasons = []
    src = ideal_sources(dut_lines)
    if src:
        reasons.append({"code": "ideal_sources", "detail": ", ".join(src)})
    if is_power_spec(spec):
        if not has_startup_overshoot_check(spec):
            reasons.append({"code": "no_startup_overshoot_check"})
        if not has_load_step(spec):
            reasons.append({"code": "no_load_step_check"})
    return {"grade": "IDEAL_ONLY" if reasons else "BUILDABLE_IN_SIM", "reasons": reasons}


def require_power_checks(spec: dict) -> None:
    """New specs only (frozen after 2026-10-21): power converters must check start-up overshoot and a load step."""
    if not is_power_spec(spec):
        return
    missing = []
    if not has_startup_overshoot_check(spec):
        missing.append("startup_overshoot (stat max over a window starting at 0)")
    if not has_load_step(spec):
        missing.append("load_step (a PULSE/PWL load step in the testbench + a min/max check over a window covering it)")
    if missing:
        raise ValueError("POWER_SPEC_MISSING_CHECKS: " + "; ".join(missing))
