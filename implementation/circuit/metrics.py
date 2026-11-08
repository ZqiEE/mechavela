"""Measured quantities from native solver vectors and acceptance checks.

Only reads arrays produced by ngspice. Supported operations are a fixed list
(no eval of user text):

transient (window = [t0, t1] seconds)
    final, initial, min, max, mean, pp, rms, abs_max
    freq   - mean frequency from rising crossings of a threshold
    duty   - fraction of window time above the threshold
operating point
    value
AC sweep (window = [f0, f1] Hz)
    db_at, phase_at (needs at_hz), max_db, min_db
    f_cross_db - first frequency where the magnitude crosses level_db
                 (absolute) or ref-drop (drop_db below the value at ref_hz)

plus the product of two vectors ("multiply"), a constant "scale", and
derived ratio / difference of two already computed metrics.
"""
from __future__ import annotations

import bisect
import math

TRAN_STATS = ("final", "initial", "min", "max", "mean", "pp", "rms", "abs_max", "freq", "duty")
OP_STATS = ("value", "final")
AC_STATS = ("db_at", "phase_at", "max_db", "min_db", "f_cross_db")
STATS = tuple(dict.fromkeys(TRAN_STATS + OP_STATS + AC_STATS))
STATS_BY_KIND = {"tran": TRAN_STATS, "op": OP_STATS, "ac": AC_STATS}
DERIVED = ("ratio", "difference")
OPS = ("ge", "le", "between")


def _interp(t, y, x):
    i = bisect.bisect_left(t, x)
    if i <= 0:
        return y[0]
    if i >= len(t):
        return y[-1]
    t0, t1 = t[i - 1], t[i]
    if t1 == t0:
        return y[i]
    return y[i - 1] + (y[i] - y[i - 1]) * (x - t0) / (t1 - t0)


def windowed(t, y, window):
    """Samples inside [a, b] plus interpolated endpoints."""
    a, b = window
    if not t[0] - 1e-15 <= a < b <= t[-1] * (1 + 1e-9) + 1e-15:
        raise ValueError("WINDOW_OUTSIDE_SIMULATION: [%g, %g] vs [%g, %g]" % (a, b, t[0], t[-1]))
    a, b = max(a, t[0]), min(b, t[-1])
    i0, i1 = bisect.bisect_right(t, a), bisect.bisect_left(t, b)
    ts = [a] + t[i0:i1] + [b]
    ys = [_interp(t, y, a)] + y[i0:i1] + [_interp(t, y, b)]
    return ts, ys


def trapezoid(ts, ys):
    # math.fsum is exactly rounded, so results are identical on every Python version
    # (built-in sum() of floats changed to compensated summation in Python 3.12).
    return math.fsum((ts[i + 1] - ts[i]) * (ys[i + 1] + ys[i]) / 2 for i in range(len(ts) - 1))


def _crossings(ts, ys, level):
    """Times of rising crossings of level (linear interpolation between samples)."""
    times = []
    for i in range(len(ts) - 1):
        if ys[i] < level <= ys[i + 1]:
            dy = ys[i + 1] - ys[i]
            times.append(ts[i] + (ts[i + 1] - ts[i]) * ((level - ys[i]) / dy if dy else 0))
    return times


def _threshold(definition, ys):
    if "threshold" in definition:
        return float(definition["threshold"])
    return (max(ys) + min(ys)) / 2


def _tran_stat(stat, definition, t, y):
    if stat == "final":
        return y[-1]
    if stat == "initial":
        return y[0]
    window = definition.get("window") or [t[0], t[-1]]
    ts, ys = windowed(t, y, window)
    if stat == "min":
        return min(ys)
    if stat == "max":
        return max(ys)
    if stat == "abs_max":
        return max(abs(v) for v in ys)
    if stat == "pp":
        return max(ys) - min(ys)
    if stat == "mean":
        return trapezoid(ts, ys) / (ts[-1] - ts[0])
    if stat == "rms":
        return math.sqrt(trapezoid(ts, [v * v for v in ys]) / (ts[-1] - ts[0]))
    if stat in ("freq", "duty"):
        level = _threshold(definition, ys)
        if max(ys) - min(ys) <= 0:
            raise ValueError("NO_OSCILLATION_IN_WINDOW")
        if stat == "freq":
            rising = _crossings(ts, ys, level)
            if len(rising) < 3:
                raise ValueError("FEWER_THAN_3_RISING_EDGES_IN_WINDOW (%d)" % len(rising))
            return (len(rising) - 1) / (rising[-1] - rising[0])
        # duty: time above level / window length, linear interpolation at crossings
        above = 0.0
        for i in range(len(ts) - 1):
            y0, y1, dt = ys[i], ys[i + 1], ts[i + 1] - ts[i]
            if y0 >= level and y1 >= level:
                above += dt
            elif y0 >= level or y1 >= level:
                fraction = (y0 - level) / (y0 - y1) if y0 >= level else (y1 - level) / (y1 - y0)
                above += dt * fraction
        return above / (ts[-1] - ts[0])
    raise ValueError("UNKNOWN_TRAN_STAT: " + stat)


def _db(re_part, im_part):
    magnitude = math.hypot(re_part, im_part)
    if magnitude <= 0:
        return -400.0
    return 20 * math.log10(magnitude)


def _log_interp(f, y, x):
    """Interpolate y over log10(frequency)."""
    if not f[0] * (1 - 1e-9) <= x <= f[-1] * (1 + 1e-9):
        raise ValueError("FREQUENCY_OUTSIDE_SWEEP: %g not in [%g, %g]" % (x, f[0], f[-1]))
    lf = [math.log10(v) for v in f]
    return _interp(lf, y, math.log10(x))


def _ac_stat(stat, definition, f, re_part, im_part):
    db = [_db(a, b) for a, b in zip(re_part, im_part)]
    if stat == "db_at":
        return _log_interp(f, db, float(definition["at_hz"]))
    if stat == "phase_at":
        phase = [math.degrees(math.atan2(b, a)) for a, b in zip(re_part, im_part)]
        # unwrap so interpolation does not jump across ±180°
        for i in range(1, len(phase)):
            while phase[i] - phase[i - 1] > 180:
                phase[i] -= 360
            while phase[i] - phase[i - 1] < -180:
                phase[i] += 360
        return _log_interp(f, phase, float(definition["at_hz"]))
    window = definition.get("window") or [f[0], f[-1]]
    lo, hi = window
    if not f[0] * (1 - 1e-9) <= lo < hi <= f[-1] * (1 + 1e-9):
        raise ValueError("WINDOW_OUTSIDE_SWEEP: [%g, %g] vs [%g, %g]" % (lo, hi, f[0], f[-1]))
    inside = [d for x, d in zip(f, db) if lo <= x <= hi] + [_log_interp(f, db, lo), _log_interp(f, db, hi)]
    if stat == "max_db":
        return max(inside)
    if stat == "min_db":
        return min(inside)
    if stat == "f_cross_db":
        if "level_db" in definition:
            level = float(definition["level_db"])
        else:
            level = _log_interp(f, db, float(definition["ref_hz"])) - float(definition["drop_db"])
        lf = [math.log10(x) for x in f]
        for i in range(len(f) - 1):
            if not (lo <= f[i + 1] and f[i] <= hi):
                continue
            a, b = db[i] - level, db[i + 1] - level
            if a == 0:
                return f[i]
            if a * b < 0:
                return 10 ** (lf[i] + (lf[i + 1] - lf[i]) * a / (a - b))
        raise ValueError("NO_CROSSING_OF_%.6g_dB_IN_WINDOW" % level)
    raise ValueError("UNKNOWN_AC_STAT: " + stat)


def compute_metric(definition, data, analysis_kind, computed=None):
    """data = {"vectors": {...real...}, "complex_vectors": {...}} from the native run."""
    computed = computed or {}
    if "derived" in definition:
        op, (a, b) = definition["derived"], definition["of"]
        if a not in computed or b not in computed:
            raise ValueError("DERIVED_METRIC_INPUT_MISSING: %s/%s" % (a, b))
        if op == "ratio":
            if computed[b] == 0:
                raise ValueError("RATIO_DENOMINATOR_ZERO")
            return computed[a] / computed[b]
        return computed[a] - computed[b]
    vectors, complex_vectors = data.get("vectors", {}), data.get("complex_vectors", {})
    name = definition["vector"]
    stat = definition["stat"]
    if stat not in STATS_BY_KIND[analysis_kind]:
        raise ValueError("STAT_%s_NOT_VALID_FOR_%s" % (stat, analysis_kind.upper()))
    scale = definition.get("scale", 1.0)
    if analysis_kind == "ac":
        if name not in complex_vectors:
            raise ValueError("COMPLEX_VECTOR_NOT_RECORDED: " + name)
        if definition.get("multiply"):
            raise ValueError("MULTIPLY_NOT_SUPPORTED_FOR_AC")
        vector = complex_vectors[name]
        re_part = [v * scale for v in vector["re"]]
        im_part = [v * scale for v in vector["im"]]
        return _ac_stat(stat, definition, list(vectors["frequency"]), re_part, im_part)
    if name not in vectors:
        raise ValueError("VECTOR_NOT_RECORDED: " + name)
    y = list(vectors[name])
    if definition.get("multiply"):
        other = vectors.get(definition["multiply"])
        if other is None:
            raise ValueError("VECTOR_NOT_RECORDED: " + definition["multiply"])
        y = [p * q for p, q in zip(y, other)]
    y = [v * scale for v in y]
    if analysis_kind == "op":
        return y[0]
    return _tran_stat(stat, definition, list(vectors["time"]), y)


def compute_all(metric_definitions, data, analysis_kind):
    values, errors = {}, {}
    pending = list(metric_definitions.items())
    for _ in range(len(pending) + 1):
        remaining = []
        for name, definition in pending:
            if "derived" in definition and not all(x in values for x in definition["of"]):
                if any(x in errors for x in definition["of"]):
                    errors[name] = "INPUT_METRIC_FAILED"
                else:
                    remaining.append((name, definition))
                continue
            try:
                value = compute_metric(definition, data, analysis_kind, values)
                if not math.isfinite(value):
                    raise ValueError("NONFINITE_METRIC")
                values[name] = value
            except (ValueError, ZeroDivisionError, KeyError) as error:
                errors[name] = str(error)
        pending = remaining
        if not pending:
            break
    for name, _ in pending:
        errors[name] = "UNRESOLVED_DEPENDENCY"
    return values, errors


def evaluate_checks(checks, values, errors):
    rows = []
    for check in checks:
        metric = check["metric"]
        row = {"id": check["id"], "metric": metric, "op": check["op"], "unit": check.get("unit", ""),
               "description": check.get("description", "")}
        if check["op"] == "between":
            row.update(min=check["min"], max=check["max"])
        else:
            row["value"] = check["value"]
        if metric not in values:
            row.update(actual=None, passed=False, margin=None, relative_margin=None,
                       error=errors.get(metric, "METRIC_MISSING"))
            rows.append(row)
            continue
        actual = values[metric]
        if check["op"] == "ge":
            margin = actual - check["value"]
            scale = abs(check["value"])
        elif check["op"] == "le":
            margin = check["value"] - actual
            scale = abs(check["value"])
        else:
            margin = min(actual - check["min"], check["max"] - actual)
            scale = max(abs(check["min"]), abs(check["max"]))
        row.update(actual=actual, passed=margin >= 0, margin=margin,
                   relative_margin=(margin / scale) if scale else None)
        rows.append(row)
    return rows
