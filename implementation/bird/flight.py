"""Longitudinal flight simulation of a flapping-wing bird with a PX4-style cascaded controller.

Point-mass translation in the vertical plane (x forward, h up) driven by the cycle-averaged forces of aero.cycle()
(wings + lumped airframe term), and pitch that follows its command through a first-order lag. The pitch lag
replaces a pitch-dynamics model because that model FAILED validation twice (usage/bird-attitude-validation-20261021);
the default lag 0.55 s is the median measured on DelFly II configuration-2 flights, an assumption for any other bird.

Controller (PX4-like structure): altitude -> climb-rate -> flap frequency (P + I); speed -> pitch (P), around the
level-flight trim found by trim().

STATUS: UNVERIFIED. Every summary carries LABEL with the reasons.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import math

import numpy as np

from implementation.bird import aero as A

G = 9.81
LABEL = {"status": "UNVERIFIED",
         "reasons": ["pitch dynamics are assumed: the attitude model failed validation four times (usage/bird-attitude-validation-20261021)",
                     "forces verified only for DelFly-like X-wings at 11-15 Hz, 0.25-1.4 m/s, for the cycle-average level (usage/bird-aero-validation-20261021)"]}


@dataclass
class Gains:
    alt_p: float = 1.2              # (m/s) per m
    climb_p: float = 2.0            # Hz per (m/s)
    climb_i: float = 1.5            # Hz per m
    freq_min: float = 4.0
    freq_max: float = 20.0
    speed_p: float = 0.35           # rad per (m/s)
    pitch_lag_s: float = 0.55       # ASSUMED: median measured DelFly II time constant


@dataclass
class Bird:
    flapper: A.Flapper
    mass: float                     # kg, flying mass
    gains: Gains = field(default_factory=Gains)

    def describe(self):
        return {"flapper": self.flapper.describe(), "mass": self.mass, "gains": asdict(self.gains)}


class ForceTable:
    """Cycle-mean body forces (X, Z) on a (freq, V, alpha) grid with trilinear interpolation."""

    def __init__(self, fl: A.Flapper, freqs=None, speeds=None, alphas=None):
        self.f = np.asarray(freqs if freqs is not None else np.linspace(4, 20, 17))
        self.v = np.asarray(speeds if speeds is not None else np.linspace(0, 4, 17))
        self.a = np.asarray(alphas if alphas is not None else np.linspace(0.0, math.pi / 2, 19))
        X = np.zeros((len(self.f), len(self.v), len(self.a))); Z = np.zeros_like(X); Pw = np.zeros_like(X)
        for i, f in enumerate(self.f):
            for j, v in enumerate(self.v):
                for k, a in enumerate(self.a):
                    m = A.cycle(fl, f, v, a, n_t=160, n_r=16)["mean"]
                    X[i, j, k], Z[i, j, k], Pw[i, j, k] = m["X"], m["Z"], m["shaft_power_w_no_recovery"]
        self.X, self.Z, self.P = X, Z, Pw

    def __call__(self, f, v, a, power=False):
        def idx(grid, x):
            x = min(max(x, grid[0]), grid[-1])
            i = max(min(int(np.searchsorted(grid, x) - 1), len(grid) - 2), 0)
            return i, (x - grid[i]) / (grid[i + 1] - grid[i])
        (i, ti), (j, tj), (k, tk) = idx(self.f, f), idx(self.v, v), idx(self.a, a)
        out = []
        for T in ((self.X, self.Z, self.P) if power else (self.X, self.Z)):
            c = T[i:i + 2, j:j + 2, k:k + 2]
            c = c[0] * (1 - ti) + c[1] * ti
            c = c[0] * (1 - tj) + c[1] * tj
            out.append(float(c[0] * (1 - tk) + c[1] * tk))
        return out


class ConventionalTable:
    """Cycle-mean thrust/lift (flight-path axes) of a conventional ornithopter on a (freq, V, wing alpha) grid,
    minus body drag CdA. kind = "path": callers rotate by the flight-path angle, not by the body pitch."""
    kind = "path"

    def __init__(self, wing: "A.ConventionalWing", body_cda_m2: float, freqs, speeds, alphas):
        import copy as _c
        self.f, self.v, self.a = (np.asarray(x, float) for x in (freqs, speeds, alphas))
        self.X = np.zeros((len(self.f), len(self.v), len(self.a))); self.Z = np.zeros_like(self.X); self.P = np.zeros_like(self.X)
        for k, a in enumerate(self.a):
            w = _c.deepcopy(wing); w.alpha_target = float(a)
            for i, f in enumerate(self.f):
                for j, v in enumerate(self.v):
                    m = A.conventional_cycle(w, f, v, n_t=120, n_r=12)["mean"]
                    self.X[i, j, k] = m["thrust"] - 0.5 * A.RHO_AIR * v ** 2 * body_cda_m2
                    self.Z[i, j, k] = m["lift"]
                    self.P[i, j, k] = max(m["shaft_power_w"], 0.0)

    __call__ = ForceTable.__call__


def earth_forces(table, freq, V, pitch, gamma):
    """Horizontal and vertical force for body pitch `pitch` and flight-path angle `gamma` (alpha = pitch - gamma)."""
    X, Z = table(freq, V, max(pitch - gamma, 0.0))
    if getattr(table, "kind", "body") == "path":          # X = thrust along the path, Z = lift normal to it
        return X * math.cos(gamma) - Z * math.sin(gamma), X * math.sin(gamma) + Z * math.cos(gamma)
    return X * math.cos(pitch) + Z * math.sin(pitch), X * math.sin(pitch) - Z * math.cos(pitch)


def trim(bird: Bird, table: ForceTable, speed: float) -> dict:
    """Level flight: pitch and flap frequency that balance weight and drag. Reports the residual (fraction of weight)."""
    W = bird.mass * G
    best = None
    th_hi = float(table.a[-1]) if getattr(table, "kind", "body") == "path" else math.radians(89)
    for f in np.linspace(table.f[0], table.f[-1], 161):
        for th in np.linspace(0.0, th_hi, 179):
            fx, fh = earth_forces(table, f, speed, th, 0.0)
            err = math.hypot(fx, fh - W) / W
            if best is None or err < best[0]:
                best = (err, f, th, fx, fh)
    err, f, th, fx, fh = best
    return {"speed": speed, "freq": float(f), "pitch": float(th), "horizontal_N": float(fx), "vertical_N": float(fh),
            "weight_N": W, "residual": float(err), "feasible": bool(err < 0.03)}


def simulate(bird: Bird, table: ForceTable, duration=30.0, dt=0.005, alt_ref=1.5, speed_ref=0.6, gust=None) -> dict:
    """Closed-loop flight from the level-flight trim. gust(t) -> (wind_x, wind_h) [m/s]."""
    g, m = bird.gains, bird.mass
    tr = trim(bird, table, speed_ref)
    x, h, vx, vh, th = 0.0, alt_ref, speed_ref, 0.0, tr["pitch"]
    climb_int = 0.0
    log = {k: [] for k in ("t", "x", "h", "vx", "vh", "pitch", "freq", "airspeed")}
    for n in range(int(duration / dt)):
        t = n * dt
        wx, wh = gust(t) if gust else (0.0, 0.0)
        ax_, ah_ = vx - wx, vh - wh                      # air-relative velocity
        V = math.hypot(ax_, ah_)
        gamma = math.atan2(ah_, ax_) if V > 1e-6 else 0.0
        climb_ref = g.alt_p * (alt_ref - h)
        climb_int += (alt_ref - h) * dt
        freq = min(max(tr["freq"] + g.climb_p * (climb_ref - vh) + g.climb_i * climb_int, g.freq_min), g.freq_max)
        th_hi = float(table.a[-1]) if getattr(table, "kind", "body") == "path" else math.radians(88)
        th_cmd = min(max(tr["pitch"] - g.speed_p * (speed_ref - vx), 0.0 if th_hi < 1 else math.radians(5)), th_hi)
        th += (th_cmd - th) * dt / g.pitch_lag_s
        fx, fh = earth_forces(table, freq, V, th, gamma)
        vx += fx / m * dt
        vh += (fh / m - G) * dt
        x += vx * dt; h += vh * dt
        if n % 10 == 0:
            for k, v in (("t", t), ("x", x), ("h", h), ("vx", vx), ("vh", vh), ("pitch", th), ("freq", freq), ("airspeed", V)):
                log[k].append(v)
        if h < 0 or not math.isfinite(h):
            break
    t_arr, h_arr = np.array(log["t"]), np.array(log["h"])
    last = t_arr >= t_arr[-1] - 5
    return {"log": log, "summary": {
        "trim": tr, "completed_s": float(t_arr[-1]), "crashed": bool(h_arr.min() < 0.05),
        "alt_error_rms_last5s_m": float(np.sqrt(np.mean((h_arr[last] - alt_ref) ** 2))),
        "speed_last5s_mps": float(np.mean(np.array(log["vx"])[last])),
        "freq_last5s_hz": float(np.mean(np.array(log["freq"])[last])),
        "freq_max_hz": float(np.max(log["freq"])), "gains": asdict(g), "label": LABEL}}


def delfly_v2_calibrated() -> A.Flapper:
    """DelFly II slow configuration with the attempt-2 estimates (usage/bird-aero-validation-20261021/attempt2)."""
    import copy
    fl = copy.deepcopy(A.DELFLY_II_SLOW)
    fl.coef.Cl_per_mps = 0.8026288091777797
    fl.airframe = A.Airframe(area=0.979e-2 + 0.275e-2, CL=19.62206830641258, CD0=17.619900878528323)
    return fl
