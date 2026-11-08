"""3-D mission simulation of a flapping-wing bird: hand launch, climb, cruise, turn, descent, flare, touchdown.

Point mass driven by the cycle-averaged thrust/lift of the design's force table (flight-path axes), with body pitch
and bank that follow their commands through first-order lags. The lags stand in for attitude dynamics because the
attitude model failed validation three times (usage/bird-attitude-validation-20261021); the mission criteria sweep
them over a wide range instead of trusting one value.

Controller (PX4-like structure, per phase): altitude -> climb rate -> flap frequency (PI); airspeed -> pitch (P);
heading -> bank (P, limited). Frame: x east, y north, h up; heading measured counter-clockwise from +x.

STATUS: UNVERIFIED (simulation only). Criteria: release/bird/MISSION_CRITERIA_R1.json.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math

import numpy as np

from implementation.bird import flight as F

G = 9.81
LABEL = {"status": "UNVERIFIED",
         "reasons": ["two-wing aerodynamics: DeLaurier + stroke-plane tilt, not validated (passed UVLM and SmartBird checks, failed Eye-Bird slow flight)",
                     "attitude dynamics are assumed lags / oscillators swept over a range (validation failed four times on DelFly II data)",
                     "simulation only: a real flight needs the built bird and its own flight logs"]}


@dataclass
class MissionGains:
    alt_p: float = 0.8              # (m/s) per m
    climb_max: float = 1.0          # m/s
    sink_max: float = 0.8           # m/s
    climb_p: float = 1.6            # Hz per (m/s)
    climb_i: float = 0.6            # Hz per m
    speed_p: float = 0.06           # rad per (m/s)
    head_p: float = 0.8             # rad bank per rad heading error
    bank_max_deg: float = 25.0
    turn_pitch_ff: float = 1.0      # extra pitch per (1/cos(bank) - 1), rad
    pitch_lag_s: float = 0.55
    bank_lag_s: float = 0.5
    # second-order (oscillatory) attitude response instead of the lag when attitude_wn > 0: theta'' = wn^2 (cmd - theta) - 2 zeta wn theta'
    attitude_wn: float = 0.0
    attitude_zeta: float = 0.5
    # flight-controller inner loop (R2): attitude commands ramp at this rate, and attitude-rate feedback adds damping
    att_rate_limit_dps: float = 15.0
    att_rate_damping_s: float = 0.1
    flare_h: float = 1.5            # m
    flare_pitch_deg: float = 6.0
    descent_pitch_deg: float = 4.0  # lower wing alpha in the descent so it sheds lift when flapping is at its minimum
    descent_speed_add: float = 0.5  # m/s more airspeed allowed in the descent
    # powered flare (> 0): keep flapping and hold this sink rate with the flap frequency, as the E-Flap does on its
    # perching approach (pitch held high, flapping regulates height). 0 = unpowered flare at the minimum frequency.
    flare_sink_mps: float = 0.0
    flare_speed_mps: float = 0.0    # powered flare: hold this approach airspeed with pitch (0 = fixed flare pitch)


@dataclass
class Mission:
    launch_h: float = 1.5
    launch_speed: float = 5.0
    cruise_h: float = 15.0
    cruise_speed: float = 6.0
    cruise1_s: float = 20.0
    turn_deg: float = 360.0
    cruise2_s: float = 10.0
    descent_rate: float = 0.7
    t_max: float = 240.0


def _wrap(a):
    return (a + math.pi) % (2 * math.pi) - math.pi


def fly(table, mass, freq_limits, gains: MissionGains | None = None, mission: Mission | None = None,
        wind=None, dt=0.005, log_every=4, power: dict | None = None):
    """wind(t, phase) -> (wx, wy, wh) m/s. power = {"eta", "avionics_w", "energy_wh" (usable)} adds electrical power and
    battery level to the log (cycle-mean shaft power from the force table). Returns {"log", "events", ...}."""
    g = gains or MissionGains()
    ms = mission or Mission()
    bird = F.Bird(None, mass=mass)
    tr = F.trim(bird, table, ms.cruise_speed)
    tr_flare = F.trim(bird, table, g.flare_speed_mps) if g.flare_speed_mps > 0 else tr
    fmin, fmax = freq_limits
    th_hi = float(table.a[-1])
    p = np.array([0.0, 0.0, ms.launch_h])
    v = np.array([ms.launch_speed, 0.0, 0.0])
    th, ph = tr["pitch"], 0.0
    th_rate = ph_rate = 0.0
    th_prev_cmd, ph_prev_cmd = tr["pitch"], 0.0
    chi_ref = 0.0
    climb_int = 0.0
    phase, t_phase = "climb", 0.0
    turned, chi_prev = 0.0, 0.0
    alt_ref = ms.cruise_h
    ev = {"launch_t": 0.0}
    log = {k: [] for k in ("t", "x", "y", "h", "V", "gamma", "chi", "pitch", "bank", "freq", "phase", "alt_ref", "freq_cmd_raw",
                           "flap_phase", "p_elec_w", "battery_pct")}
    flap_phase, used_wh = 0.0, 0.0
    touchdown = None
    for n in range(int(ms.t_max / dt)):
        t = n * dt
        wx, wy, wh = wind(t, phase) if wind else (0.0, 0.0, 0.0)
        va = v - np.array([wx, wy, wh])
        V = float(np.linalg.norm(va))
        gamma = math.asin(max(-1.0, min(1.0, va[2] / V))) if V > 1e-6 else 0.0
        chi = math.atan2(v[1], v[0])
        # --- phase logic
        if phase == "climb" and p[2] >= ms.cruise_h - 1.0:
            phase, t_phase = "cruise1", t; ev["climb_done_t"] = t
        elif phase == "cruise1" and t - t_phase >= ms.cruise1_s:
            phase, t_phase = "turn", t; turned, chi_prev = 0.0, chi; ev["turn_start_t"] = t
        elif phase == "turn":
            turned += -_wrap(chi - chi_prev); chi_prev = chi          # right turn = heading decreasing
            if turned >= math.radians(ms.turn_deg):
                phase, t_phase = "cruise2", t; ev["turn_done_t"] = t; chi_ref = chi
        elif phase == "cruise2" and t - t_phase >= ms.cruise2_s:
            phase, t_phase = "descent", t; ev["descent_start_t"] = t
        elif phase == "descent" and p[2] <= g.flare_h:
            phase, t_phase = "flare", t; ev["flare_t"] = t
            if g.flare_sink_mps > 0:
                climb_int = 0.0                       # the descent wound the height integrator up: start the flare clean
        # --- references
        if phase == "descent":
            alt_ref = max(0.0, alt_ref - ms.descent_rate * dt)
        elif phase != "flare":
            alt_ref = ms.cruise_h
        if phase == "turn":
            bank_cmd = math.radians(g.bank_max_deg)
        else:
            bank_cmd = max(-math.radians(g.bank_max_deg), min(math.radians(g.bank_max_deg), -g.head_p * _wrap(chi_ref - chi)))
        if phase == "flare":
            if g.flare_sink_mps > 0:
                climb_int = max(-20.0, min(20.0, climb_int + (-g.flare_sink_mps - v[2]) * dt))
                freq_raw = tr["freq"] + g.climb_p * (-g.flare_sink_mps - v[2]) + g.climb_i * climb_int
            else:
                freq_raw = fmin
            if g.flare_speed_mps > 0:
                th_cmd = tr_flare["pitch"] - g.speed_p * (g.flare_speed_mps - V)
            else:
                th_cmd = tr["pitch"] + math.radians(g.flare_pitch_deg)
        else:
            climb_ref = max(-g.sink_max, min(g.climb_max, g.alt_p * (alt_ref - p[2])))
            if phase == "descent":
                climb_ref = max(-g.sink_max, min(g.climb_max, -ms.descent_rate + g.alt_p * (alt_ref - p[2])))
            climb_int = max(-20.0, min(20.0, climb_int + (climb_ref - v[2]) * dt))
            freq_raw = tr["freq"] + g.climb_p * (climb_ref - v[2]) + g.climb_i * climb_int
            v_ref = ms.cruise_speed + (g.descent_speed_add if phase == "descent" else 0.0)
            th_cmd = tr["pitch"] - g.speed_p * (v_ref - V) + g.turn_pitch_ff * (1 / max(math.cos(ph), 0.5) - 1)
            if phase == "descent":
                th_cmd -= math.radians(g.descent_pitch_deg)
        freq = min(max(freq_raw, fmin), fmax)
        th_cmd = min(max(th_cmd, 0.0), th_hi + max(gamma, 0.0))
        if g.att_rate_limit_dps > 0:                # command shaping: no attitude steps
            lim = math.radians(g.att_rate_limit_dps) * dt
            th_cmd = th_prev_cmd + max(-lim, min(lim, th_cmd - th_prev_cmd)); th_prev_cmd = th_cmd
            bank_cmd = ph_prev_cmd + max(-lim, min(lim, bank_cmd - ph_prev_cmd)); ph_prev_cmd = bank_cmd
        if g.attitude_wn > 0:                     # measured flapping-MAV pitch mode is an oscillation (attempt 4 data)
            wn, zt = g.attitude_wn, g.attitude_zeta
            kd = g.att_rate_damping_s                # attitude-rate feedback from the flight controller's gyro
            th_rate += (wn * wn * (th_cmd - kd * th_rate - th) - 2 * zt * wn * th_rate) * dt; th += th_rate * dt
            ph_rate += (wn * wn * (bank_cmd - kd * ph_rate - ph) - 2 * zt * wn * ph_rate) * dt; ph += ph_rate * dt
        else:
            th += (th_cmd - th) * dt / g.pitch_lag_s
            ph += (bank_cmd - ph) * dt / g.bank_lag_s
        # --- forces (path axes): thrust along the air velocity, lift normal to it, tilted by the bank angle
        alpha = min(max(th - gamma, 0.0), th_hi)
        if power:
            T, L, P_shaft = table(freq, V, alpha, power=True)
            p_elec = max(P_shaft, 0.0) / power["eta"] + power["avionics_w"]
            used_wh += p_elec * dt / 3600.0
        else:
            T, L = table(freq, V, alpha)
            p_elec = float("nan")
        flap_phase += 2 * math.pi * freq * dt
        ev_ = va / V if V > 1e-6 else np.array([1.0, 0.0, 0.0])
        up = np.array([0.0, 0.0, 1.0]) - ev_ * ev_[2]
        up /= max(np.linalg.norm(up), 1e-9)
        right = np.cross(ev_, up)
        f = T * ev_ + L * (math.cos(ph) * up + math.sin(ph) * right)
        a = f / mass + np.array([0.0, 0.0, -G])
        v = v + a * dt
        p = p + v * dt
        if n % log_every == 0:
            for k, x in (("t", t), ("x", p[0]), ("y", p[1]), ("h", p[2]), ("V", V), ("gamma", gamma), ("chi", chi),
                         ("pitch", th), ("bank", ph), ("freq", freq), ("phase", phase), ("alt_ref", alt_ref),
                         ("freq_cmd_raw", freq_raw), ("flap_phase", flap_phase), ("p_elec_w", p_elec),
                         ("battery_pct", 100.0 * (1.0 - used_wh / power["energy_wh"]) if power else float("nan"))):
                log[k].append(float(x) if not isinstance(x, str) else x)
        if p[2] <= 0.0:
            touchdown = {"t": t, "vertical_mps": float(-v[2]), "ground_speed_mps": float(math.hypot(v[0], v[1])),
                         "pitch_deg": math.degrees(th), "phase": phase}
            break
        if not np.all(np.isfinite(p)):
            break
    ev["touchdown"] = touchdown
    return {"log": log, "events": ev, "trim": tr, "gains": asdict(g), "mission": asdict(ms), "label": LABEL}


def judge(run: dict, freq_limits) -> dict:
    """Apply release/bird/MISSION_CRITERIA_R1.json pass rules to one run (cruise height/speed from the run's mission)."""
    ms = Mission(**run["mission"])
    L = {k: np.asarray(v) if k != "phase" else v for k, v in run["log"].items()}
    t, h, V = L["t"], L["h"], L["V"]
    ph = np.array(L["phase"])
    ev = run["events"]
    r = {}
    pre = (t >= 1.0) & np.isin(ph, ["climb", "cruise1", "turn", "cruise2"])
    r["no_ground_contact_before_landing"] = bool(pre.any() and h[pre].min() > 0.5)
    r["climb"] = bool(ev.get("climb_done_t") is not None and ev["climb_done_t"] <= 40.0)
    cr = np.isin(ph, ["cruise1", "cruise2"])
    r["cruise"] = bool(cr.any() and math.sqrt(float(np.mean((h[cr] - ms.cruise_h) ** 2))) <= 1.0
                       and abs(float(V[cr].mean()) - ms.cruise_speed) <= 1.0)
    tu = ph == "turn"
    r["turn"] = bool(ev.get("turn_done_t") is not None and ev["turn_done_t"] - ev["turn_start_t"] <= 45.0
                     and tu.any() and float(np.abs(h[tu] - ms.cruise_h).max()) <= 2.0
                     and float(np.degrees(np.abs(L["bank"])).max()) <= 30.0)
    td = ev.get("touchdown")
    r["landing"] = bool(td is not None and td["phase"] == "flare" and td["vertical_mps"] <= 1.0 and td["ground_speed_mps"] <= 7.0)
    raw = L["freq_cmd_raw"]
    # the controller clamps to the limits; the rule fails if it ever needed more than the drive can give in flight
    flying = np.isin(ph, ["climb", "cruise1", "turn", "cruise2", "descent"])
    r["drive_limits"] = bool(float(raw[flying].max()) <= freq_limits[1] + 1e-9)
    r["time_limit"] = bool(td is not None and td["t"] <= 240.0)
    numbers = {"climb_done_s": ev.get("climb_done_t"), "turn_s": (ev["turn_done_t"] - ev["turn_start_t"]) if ev.get("turn_done_t") else None,
               "cruise_alt_rms_m": float(math.sqrt(float(np.mean((h[cr] - ms.cruise_h) ** 2)))) if cr.any() else None,
               "cruise_mean_airspeed_mps": float(V[cr].mean()) if cr.any() else None,
               "turn_alt_dev_max_m": float(np.abs(h[tu] - ms.cruise_h).max()) if tu.any() else None,
               "bank_max_deg": float(np.degrees(np.abs(L["bank"])).max()),
               "freq_needed_max_hz": float(raw[flying].max()), "freq_limit_hz": float(freq_limits[1]),
               "touchdown": td}
    return {"rules": r, "passed": all(r.values()), "numbers": numbers}


def cases(second_order: bool = False):
    """The registered run list: nominal, 12 lag combinations, 3 wind cases. -> [(name, gain overrides, wind)]."""
    out = [("nominal", {}, None)]
    for pl in (0.2, 0.55, 2.0, 5.0):
        for bl in (0.3, 1.0, 3.0):
            out.append((f"lag_pitch{pl}_bank{bl}", {"pitch_lag_s": pl, "bank_lag_s": bl}, None))
    out.append(("wind_headwind2", {}, lambda t, ph: (-2.0, 0.0, 0.0) if ph == "cruise1" else (0.0, 0.0, 0.0)))
    out.append(("wind_crosswind2", {}, lambda t, ph: (0.0, 2.0, 0.0)))

    def make_gust():
        s = {}

        def gust(t, ph):
            if ph == "cruise1" and "c" not in s:
                s["c"] = t
            if ph == "turn" and "u" not in s:
                s["u"] = t
            if "c" in s and s["c"] + 5 <= t < s["c"] + 7:
                return (0.0, 0.0, 1.0)
            if "u" in s and s["u"] + 5 <= t < s["u"] + 7:
                return (1.5, 0.0, 0.0)
            return (0.0, 0.0, 0.0)
        return gust
    out.append(("wind_gusts", {}, make_gust()))
    if second_order:
        # MISSION_CRITERIA_R2: oscillatory attitude response, DelFly II pitch mode (wn 7 rad/s, zeta 0.24-0.55, attitude
        # attempt 4) Froude-scaled to a 0.6 m bird gives about 4.7 rad/s; 3-7 rad/s x zeta 0.2 / 0.5 brackets it
        for wn in (3.0, 5.0, 7.0):
            for zt in (0.2, 0.5):
                out.append((f"osc_wn{wn:g}_zeta{zt:g}", {"attitude_wn": wn, "attitude_zeta": zt}, None))
    if second_order == 2:
        # MISSION_CRITERIA_R3: E-Flap short period measured from the GRVC flight logs (3.6 rad/s, zeta 0.15) and the
        # same mode scaled to a 7 m/s cruise (6.6 rad/s; scaling assumed)
        for wn in (3.6, 6.6):
            out.append((f"osc_wn{wn:g}_zeta0.15", {"attitude_wn": wn, "attitude_zeta": 0.15}, None))
    return out


def sweep(table, mass, freq_limits, gains: MissionGains | None = None, mission: Mission | None = None,
          power: dict | None = None, on_run=None, second_order: bool = False):
    """Fly every registered run. on_run(name, run, judged) is called as each run finishes (live replay files).
    -> {"verdict", "runs": {name: judge}, "nominal": run}."""
    from dataclasses import replace
    base = gains or MissionGains()
    runs, nominal = {}, None
    for name, over, wind in cases(second_order):
        run = fly(table, mass, freq_limits, replace(base, **over), mission=mission, wind=wind, power=power)
        runs[name] = judge(run, freq_limits)
        if on_run:
            on_run(name, run, runs[name])
        if name == "nominal":
            nominal = run
    return {"verdict": "SIM_PASS" if all(r["passed"] for r in runs.values()) else "SIM_FAIL", "runs": runs, "nominal": nominal,
            "label": LABEL}
