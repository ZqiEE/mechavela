"""Quasi-steady blade-element aerodynamics for flapping wings, with an optional clap-and-peel (fling) term.

Model structure follows Armanini, Caetano, de Croon, de Visser & Mulder (2016), "Quasi-steady aerodynamic model of
clap-and-fling flapping MAV and validation using free-flight data", Bioinspiration & Biomimetics 11(4) 046002
(eqs. 7-21 there). Only the published equations and coefficient values are used; no code or data from the paper.

Per blade element (radius r, chord c):
    circulation   G = 1/2 CL c |V| + 1/2 CF c^2 dtheta_fling   during peel (from t*=0 until the wing stops un-pitching)
                  G = 1/2 CL c |V| + 1/2 CR c^2 dtheta_w       otherwise
                  CL = Cl sin(2 alpha),  CR = Cr (0.75 - x0)
    forces        dFx_w = [m1 v_z dtheta_w - rho G v_z - m11 a_x] dr - dFvisc_x
                  dFz_w = [-m2 v_x dtheta_w + rho G v_x - m22 a_z] dr - dFvisc_z
                  dFvisc = 1/2 rho c CD |V| (v_x, v_z),  CD = CD0 cos^2 a + CD90 sin^2 a
                  m11 = pi/4 rho b^2,  m22 = pi/4 rho c_eff^2,  m1 = (c / (c_mean R)) M_wing + m22,  m2 = ... + m11
    body frame    dFx_b = dFx_w sin(theta_w) + dFz_w cos(theta_w)
                  dFz_b = (dFx_w cos(theta_w) - dFz_w sin(theta_w)) cos(zeta)
Everything here is a model. Whether it matches reality is decided only by validate_delfly.py against public data.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import math

import numpy as np

RHO_AIR = 1.225  # kg/m^3, ISA sea level


@dataclass
class Wing:
    semi_span: float            # m, root (flap hinge) to tip, one wing
    root_chord: float           # m
    tip_chord: float            # m
    mass: float                 # kg, one wing
    thickness: float = 15e-6    # m, membrane thickness (only enters m11, negligible)
    root_offset: float = 0.0    # m, hinge to first wing section


@dataclass
class Kinematics:
    """Flap angle zeta(t*) from the split-cycle wing-beat formula (eqs. 20-21), passive pitch from eq. 17."""
    amp: float = 0.34           # A_amp [rad]
    split: float = 0.35         # delta (split-cycle parameter)
    bias: float = 0.34          # eta [rad]
    dihedral: float = math.radians(12.0)   # zeta_0 [rad]; ASSUMED (not given in the paper)
    pitch_c0: float = 0.3139    # eq. 17 coefficients (Table 5)
    pitch_czeta: float = -1.1382
    pitch_czdot: float = -0.0179
    pitch_czdot2: float = -0.0002
    pitch_ref_span: float = 0.7  # pitch measured at 70 % span; linear torsion from root (0) to there
    peel_end: float = 0.17       # t* at which the trailing edges have peeled apart (effective chord ramps 0 -> 1)
    interacting: bool = True     # True = X-wing clap-and-peel (DelFly); False = two independent wings (no fling term)


@dataclass
class Coefficients:
    """Average of the per-condition estimates in Table 8 (estimated from WIND-TUNNEL data by the authors)."""
    Cl: float = 1.28
    Cr: float = 1.45
    CF: float = 1.47
    CD0: float = 0.0
    CD90: float = math.pi
    x0: float = 0.0             # chordwise rotation axis position (leading edge)
    Cl_per_mps: float = 0.0     # Cl(V) = Cl + Cl_per_mps * V (validation attempt 2; the paper's Cl rises with airspeed)


@dataclass
class Airframe:
    """Tail + body as one quasi-steady flat surface (lift ~ sin a cos a, drag ~ CD0 + CD90 sin^2 a)."""
    area: float                 # m^2
    CL: float = 0.0
    CD0: float = 0.0
    CD90: float = 1.28          # flat plate normal to the flow


def airframe_forces(af: Airframe, airspeed: float, alpha: float, rho: float = RHO_AIR) -> tuple[float, float]:
    """Body-axis (X forward, Z down) force of the airframe at angle of attack alpha [rad]."""
    q = 0.5 * rho * airspeed ** 2
    L = q * af.area * af.CL * math.sin(alpha) * math.cos(alpha)
    D = q * af.area * (af.CD0 + af.CD90 * math.sin(alpha) ** 2)
    return L * math.sin(alpha) - D * math.cos(alpha), -L * math.cos(alpha) - D * math.sin(alpha)


@dataclass
class Flapper:
    wing: Wing
    kin: Kinematics = field(default_factory=Kinematics)
    coef: Coefficients = field(default_factory=Coefficients)
    n_wings_per_side: int = 2   # 2 = X-wing (upper + lower), 1 = conventional
    rho: float = RHO_AIR
    airframe: Airframe | None = None

    def describe(self) -> dict:
        return {"wing": asdict(self.wing), "kinematics": asdict(self.kin), "coefficients": asdict(self.coef),
                "n_wings_per_side": self.n_wings_per_side, "rho": self.rho,
                "airframe": asdict(self.airframe) if self.airframe else None}


DELFLY_II_SLOW = Flapper(Wing(semi_span=0.137, root_chord=0.085, tip_chord=0.056, mass=0.29e-3))
"""DelFly II slow-flight configuration, geometry from Table 3 of Armanini et al. 2016 (span 274 mm, chords 85/56 mm,
wing mass 0.29 g). Mass 17.4 g is the vehicle, not the wing."""


def flap_angle(tstar: np.ndarray, k: Kinematics) -> np.ndarray:
    """zeta(t*) in [bias-amp, bias+amp]; outstroke (opening) first and longer than the instroke."""
    w = 2 * math.pi
    d = k.split
    sigma = d * w / (w - 2 * d)
    phase = -2 * math.pi * d / (w - 2 * d)
    t1 = math.pi / (w - d)
    up = -k.amp * np.cos((w - d) * tstar) + k.bias
    down = -k.amp * np.cos((w + sigma) * tstar + phase) + k.bias
    return np.where(tstar <= t1, up, down)


def cycle(fl: Flapper, freq: float, airspeed: float, body_pitch: float, n_t: int = 400, n_r: int = 30) -> dict:
    """Forces over one flap cycle at constant flight condition.

    freq [Hz], airspeed [m/s] along the flight path, body_pitch [rad] = angle between body x axis and the
    relative wind (the body angle of attack in the wind tunnel). Returns time histories of body-axis forces X, Z
    (all wings) and their cycle means, plus lift/thrust w.r.t. the relative wind.
    """
    k, c, w = fl.kin, fl.coef, fl.wing
    T = 1.0 / freq
    ts = np.linspace(0.0, 1.0, n_t, endpoint=False)
    dt = T / n_t
    zeta = flap_angle(ts, k)
    zdot = np.gradient(zeta, dt, edge_order=2)
    # periodic derivative (np.gradient is not periodic at the ends)
    zdot[0] = (zeta[1] - zeta[-1]) / (2 * dt); zdot[-1] = (zeta[0] - zeta[-2]) / (2 * dt)
    theta_ref = math.pi / 2 + k.pitch_c0 + k.pitch_czeta * zeta + k.pitch_czdot * zdot + k.pitch_czdot2 * zdot ** 2

    R = w.semi_span
    r = w.root_offset + (np.arange(n_r) + 0.5) / n_r * (R - w.root_offset)
    dr = (R - w.root_offset) / n_r
    chord = w.root_chord + (w.tip_chord - w.root_chord) * (r - w.root_offset) / (R - w.root_offset)
    c_mean = float(np.mean(chord))
    twist = np.clip(r / (k.pitch_ref_span * R), 0.0, None)       # linear torsion, 0 at the root
    theta_w = math.pi / 2 + np.outer(theta_ref - math.pi / 2, twist)   # (n_t, n_r)
    theta_dot = np.gradient(theta_w, dt, axis=0)
    theta_dot[0] = (theta_w[1] - theta_w[-1]) / (2 * dt); theta_dot[-1] = (theta_w[0] - theta_w[-2]) / (2 * dt)

    # fling/peel phase: from t*=0 until the wing stops un-pitching (dtheta_fling = -dtheta_w turns negative)
    fling_rate = -theta_dot
    peel = np.zeros_like(theta_w, dtype=bool)
    if k.interacting and c.CF:
        for j in range(n_r):
            for i in range(n_t):
                if fling_rate[i, j] < 0 and i > 0:
                    break
                peel[i, j] = True
    c_eff = chord[None, :] * np.clip(ts[:, None] / k.peel_end, 0.0, 1.0) if k.interacting else np.broadcast_to(chord, theta_w.shape)
    m11 = math.pi / 4 * fl.rho * w.thickness ** 2
    m22 = math.pi / 4 * fl.rho * c_eff ** 2
    wing_line_mass = chord / (c_mean * R) * w.mass       # kg/m
    m1 = wing_line_mass[None, :] + m22
    m2 = wing_line_mass[None, :] + m11

    V, tb = airspeed, body_pitch
    Fx = np.zeros(n_t); Fz = np.zeros(n_t); P = np.zeros(n_t)
    wings = [("upper", +1)] + ([("lower", -1)] if fl.n_wings_per_side == 2 else [])
    for name, s in wings:
        rz = r[None, :] * zdot[:, None]
        if name == "upper":
            th = tb - theta_w + math.pi / 2
            vx = rz * np.cos(theta_w) - V * np.cos(th)
            vz_d = lambda d: -rz * np.sin(theta_w) - d * chord[None, :] * theta_dot + V * np.sin(th) * np.cos(k.dihedral + zeta)[:, None]  # noqa: E731
        else:
            th = tb + theta_w - math.pi / 2
            vx = rz * np.cos(theta_w) + V * np.cos(th)
            vz_d = lambda d: -rz * np.sin(theta_w) - d * chord[None, :] * theta_dot - V * np.sin(th) * np.cos(k.dihedral - zeta)[:, None]  # noqa: E731
        vz = vz_d(0.0)
        speed = np.hypot(vx, vz)
        alpha = np.arctan2(-vz, vx)
        CL = (c.Cl + c.Cl_per_mps * airspeed) * np.sin(2 * alpha)
        gamma = 0.5 * CL * chord[None, :] * speed + np.where(
            peel, 0.5 * c.CF * chord[None, :] ** 2 * fling_rate, 0.5 * c.Cr * (0.75 - c.x0) * chord[None, :] ** 2 * theta_dot)
        # accelerations for added mass with the mid-chord velocity (d = 0.5), as in the paper
        vx_a, vz_a = vx, vz_d(0.5)
        ax = np.gradient(vx_a, dt, axis=0); az = np.gradient(vz_a, dt, axis=0)
        CD = c.CD0 * np.cos(alpha) ** 2 + c.CD90 * np.sin(alpha) ** 2
        fvx = 0.5 * fl.rho * chord[None, :] * CD * speed * vx
        fvz = 0.5 * fl.rho * chord[None, :] * CD * speed * vz
        dfx = (m1 * vz * theta_dot - fl.rho * gamma * vz - m11 * ax - fvx) * dr
        dfz = (-m2 * vx * theta_dot + fl.rho * gamma * vx - m22 * az - fvz) * dr
        fxb = dfx * np.sin(theta_w) + dfz * np.cos(theta_w)
        fzb = (dfx * np.cos(theta_w) - dfz * np.sin(theta_w)) * np.cos(zeta)[:, None]
        Fx += fxb.sum(axis=1); Fz += fzb.sum(axis=1)
        # power the drive must deliver = -(force on the element) . (element velocity due to flapping)
        ux, uz = rz * np.cos(theta_w), -rz * np.sin(theta_w)
        P += -(dfx * ux + dfz * uz).sum(axis=1)
    Fx *= 2; Fz *= 2; P *= 2                      # left + right side
    if fl.airframe is not None:
        ax_, az_ = airframe_forces(fl.airframe, airspeed, body_pitch, fl.rho)
        Fx = Fx + ax_; Fz = Fz + az_
    lift = Fx * math.sin(tb) - Fz * math.cos(tb)   # eq. 16, w.r.t. the relative wind
    drag = -Fx * math.cos(tb) - Fz * math.sin(tb)
    return {"t_star": ts, "zeta": zeta, "X": Fx, "Z": Fz, "lift": lift, "drag": drag, "power": P,
            "mean": {"X": float(Fx.mean()), "Z": float(Fz.mean()), "lift": float(lift.mean()), "drag": float(drag.mean()),
                     # UNVERIFIED: no power measurements in the validation data
                     "shaft_power_w_no_recovery": float(np.clip(P, 0, None).mean()),
                     "shaft_power_w_full_recovery": float(P.mean())}}


def mean_forces(fl: Flapper, freq: float, airspeed: float, body_pitch: float) -> dict:
    return cycle(fl, freq, airspeed, body_pitch)["mean"]


# ----------------------------------------------------------------------------------------------------------------
# Conventional (two-wing, no clap-and-fling) ornithopter: classical quasi-steady strip theory with ideal feathering.
# Each spanwise strip sees the forward speed plus its flapping velocity; its pitch is set so that the angle of attack
# to that local flow is alpha_target (a feathering wing). Lift: CL = 2 pi AR/(AR+2) alpha, capped at CL_max;
# drag: CD0 + CL^2/(pi e AR). Forces are resolved into thrust (along the flight path) and lift (normal to it).
# NO VALIDATION DATA: results from this model are UNVERIFIED (see release/BIRD_PLAN.md).
# ----------------------------------------------------------------------------------------------------------------
@dataclass
class ConventionalWing:
    semi_span: float
    root_chord: float
    tip_chord: float
    alpha_target: float = math.radians(8)   # feathering keeps the strip at this angle of attack (ASSUMED)
    CL_max: float = 1.0
    CD0: float = 0.03
    oswald: float = 0.7
    stroke_amp: float = math.radians(30)    # flap angle amplitude (half of peak-to-peak)
    stroke_bias: float = math.radians(5)
    upstroke_load: float = 0.3              # upstroke angle of attack as a fraction of the downstroke one (ASSUMED)
    planform: tuple | None = None           # ((r/semi_span, chord_m, effective_fraction), ...) bird wing; None = linear taper
    # UVLM cross-check 2026-10-22 (usage/bird-aero-two-wing-20261022/uvlm): unsteady vortex lattice lift was 0.78 x strip
    # theory at R2 conditions (1.5-3.5 Hz); the SmartBird point check also showed strip theory is optimistic. Conservative
    # correction, UNVERIFIED (simulation-to-simulation, no experiment).
    lift_factor: float = 0.78
    # "attached": the lift curve above. "flat_plate": CN = 2 Cl sin(alpha) per strip (CL = Cl sin 2a, CD = CD0 + 2 Cl
    # sin^2 a), valid past stall for flapping wings (leading-edge vortex); lift_factor is not applied. See
    # release/bird/AERO_EYEBIRD_FLIGHT_CHECK_B.json.
    lift_model: str = "attached"
    Cl_fp: float = 1.28
    # lift_model "delaurier" (see delaurier_cycle): dynamic twist as a fraction of the ideal feathering twist, and
    # DeLaurier's published constants (stall limit on the effective angle, leading-edge suction efficiency,
    # post-stall cross-flow drag coefficient)
    twist_fraction: float = 0.5
    stall_deg: float = 15.0
    suction_eff: float = 0.98
    cd_crossflow: float = 1.98
    # stroke-plane tilt (delaurier only): with tilt_trim the trim angle alpha_target is the BODY pitch to the flight
    # path (the flapping axis tilts with the body, as in slow flight at high body angle), and the wing chord sits at
    # `incidence` to the body. Without it the stroke plane is along the path and alpha_target is the chord angle.
    tilt_trim: bool = False
    incidence: float = 0.0
    stall: bool = True
    # beyond the stall limit: "crossflow" = DeLaurier's separated flow (normal force from cross-flow drag), "polhamus" =
    # the leading-edge vortex stays on the wing and the lost suction turns into normal force (Polhamus 1966 suction
    # analogy: CN = Kp sin a cos a + Kv sin a |sin a|, Kp = 2 pi AR/(AR+2), Kv = pi). CFD of the E-Flap wing (Ruiz,
    # Acosta, Ollero 2022, Zenodo 4917132) shows this vortex lift above Strouhal ~0.3 at high angle of attack.
    post_stall: str = "crossflow"
    # "polhamus_st": vortex lift only when the chord-based Strouhal number f c_mean / V reaches lev_strouhal (the CFD
    # paper's threshold for a leading-edge vortex at high angle of attack); below it, DeLaurier's cross-flow branch
    lev_strouhal: float = 0.3


def chord_at(w: "ConventionalWing", r):
    """Effective chord (m) at span position r (m). A bird planform gives (r/semi_span, outline chord, effective fraction):
    the fraction is < 1 where primary feathers are separated by slots (gaps carry no lift in this model)."""
    r = np.asarray(r, dtype=float)
    if not w.planform:
        return w.root_chord + (w.tip_chord - w.root_chord) * r / w.semi_span
    st = np.asarray(w.planform, dtype=float)
    return np.interp(r / w.semi_span, st[:, 0], st[:, 1] * st[:, 2])


def wing_area_one(w: "ConventionalWing") -> float:
    """Effective area of ONE wing (m^2)."""
    r = np.linspace(0, w.semi_span, 401)
    return float(np.trapezoid(chord_at(w, r), r)) if hasattr(np, "trapezoid") else float(np.trapz(chord_at(w, r), r))


def conventional_cycle(w: ConventionalWing, freq: float, airspeed: float, n_t: int = 200, n_r: int = 20,
                       rho: float = RHO_AIR) -> dict:
    """Cycle-mean thrust (along the flight path), lift (normal to it) and shaft power of BOTH wings."""
    if w.lift_model == "delaurier":
        return delaurier_cycle(w, freq, airspeed, n_t=n_t, n_r=n_r, rho=rho)
    span = 2 * w.semi_span
    area = 2 * wing_area_one(w)
    AR = span ** 2 / area
    a_slope = 2 * math.pi * AR / (AR + 2)
    ts = np.linspace(0, 1, n_t, endpoint=False)
    zeta = w.stroke_bias + w.stroke_amp * np.sin(2 * math.pi * ts)
    zdot = w.stroke_amp * 2 * math.pi * freq * np.cos(2 * math.pi * ts)
    r = (np.arange(n_r) + 0.5) / n_r * w.semi_span
    dr = w.semi_span / n_r
    c = chord_at(w, r)
    # strip velocity relative to air (in the plane normal to the span): forward V, vertical from flapping
    vz = -(r[None, :] * zdot[:, None]) * np.cos(zeta)[:, None]          # downstroke (zdot>0) moves the strip down
    U = np.sqrt(airspeed ** 2 + vz ** 2)
    phi = np.arctan2(-vz, airspeed if airspeed > 1e-6 else 1e-6)      # inflow angle (air comes from below on downstroke)
    alpha = np.where(zdot[:, None] > 0, w.alpha_target, w.alpha_target * w.upstroke_load) * np.ones_like(vz)
    if w.lift_model == "flat_plate":
        CL = w.Cl_fp * np.sin(2 * alpha)
        CD = w.CD0 + 2 * w.Cl_fp * np.sin(alpha) ** 2
    else:
        CL = w.lift_factor * np.clip(a_slope * alpha, -w.CL_max, w.CL_max)
        CD = w.CD0 + CL ** 2 / (math.pi * w.oswald * AR)
    q = 0.5 * rho * U ** 2 * c[None, :] * dr
    dL, dD = q * CL, q * CD                                             # normal / along local flow
    # resolve: local flow comes at angle phi below the horizontal; lift tilts forward by phi
    thrust = dL * np.sin(phi) - dD * np.cos(phi)
    lift = (dL * np.cos(phi) + dD * np.sin(phi)) * np.cos(zeta)[:, None]
    # shaft power: force along the strip's vertical motion times its speed
    f_vert = dL * np.cos(phi) + dD * np.sin(phi)
    power = (f_vert * (r[None, :] * np.abs(zdot)[:, None] * np.cos(zeta)[:, None])).sum(axis=1)
    T = 2 * thrust.sum(axis=1); L = 2 * lift.sum(axis=1); P = 2 * power
    return {"mean": {"thrust": float(T.mean()), "lift": float(L.mean()), "shaft_power_w": float(P.mean())},
            "AR": AR, "label": "UNVERIFIED (no public validation data for conventional ornithopters in RLSOK yet)"}


# ----------------------------------------------------------------------------------------------------------------
# DeLaurier modified strip theory for flapping wings (J. D. DeLaurier, "An aerodynamic model for flapping-wing
# flight", The Aeronautical Journal 97(964), 1993). Only the published method is used. Per strip:
#   relative angle at 3/4 chord  a = theta + (-hdot + 0.5 c thetadot) / U        (pitch axis at 1/4 chord)
#   unsteady effective angle     a' = AR/(2+AR) [a_mean + F'(k) a_osc + c/(2U) G'(k)/k da_osc/dt]
#                                F', G' = Jones' finite-wing modification of Theodorsen's function, k = w c / 2U
#   attached (|a'| <= stall):    dNc = 1/2 rho U V 2 pi a' c dr,  dNa = rho pi c^2/4 (U da/dt - c/4 theta'') dr,
#                                leading-edge suction dTs = eta_s 2 pi (a' - c thetadot / 4U)^2 1/2 rho U V c dr,
#                                friction dDf = CD0 1/2 rho V^2 c dr
#   separated:                   dNc = Cd_cf 1/2 rho |Vn| Vn c dr (cross-flow drag), dNa halved, no suction/friction
#   forces in flight-path axes:  lift = (dN cos(theta) + dFx sin(theta)) cos(zeta),  thrust = dFx cos(theta) - dN sin(theta)
# Kinematics: flap zeta = bias + A sin(wt) (tip up positive); strip pitch theta = alpha_target + dynamic twist, the
# twist being twist_fraction x the ideal feathering angle atan(r w A / U), nose-down on the downstroke.
# Status of this model: see release/bird/AERO_EYEBIRD_FLIGHT_CHECK_C.json and AERO_UVLM_CROSSCHECK_v3.json.
# ----------------------------------------------------------------------------------------------------------------
def _jones(k: np.ndarray, AR: float):
    c1 = 0.5 * AR / (2.32 + AR)
    c2 = 0.181 + 0.772 / AR
    F = 1 - c1 * k ** 2 / (k ** 2 + c2 ** 2)
    G = -c1 * c2 * k / (k ** 2 + c2 ** 2)
    return F, G


def _pdiff(x: np.ndarray, dt: float) -> np.ndarray:
    """Periodic central difference along axis 0."""
    return (np.roll(x, -1, axis=0) - np.roll(x, 1, axis=0)) / (2 * dt)


def delaurier_cycle(w: ConventionalWing, freq: float, airspeed: float, n_t: int = 200, n_r: int = 20,
                    rho: float = RHO_AIR, plunge_m: float = 0.0) -> dict:
    """Cycle-mean thrust, lift and shaft power of BOTH wings (DeLaurier 1993). plunge_m > 0 replaces flapping by a
    rigid heave of that amplitude (used only to check the implementation against Garrick)."""
    U = max(airspeed, 0.3)
    span = 2 * w.semi_span
    AR = span ** 2 / (2 * wing_area_one(w))
    om = 2 * math.pi * freq
    T = 1.0 / freq
    n_t = max(n_t, 120)
    ts = np.linspace(0, 1, n_t, endpoint=False)
    dt = T / n_t
    r = (np.arange(n_r) + 0.5) / n_r * w.semi_span
    dr = w.semi_span / n_r
    c = chord_at(w, r)[None, :]
    if plunge_m:
        zeta = np.zeros(n_t)
        hdot = np.broadcast_to((plunge_m * om * np.cos(om * T * ts))[:, None], (n_t, n_r))
        twist = np.zeros((n_t, n_r))
    else:
        zeta = w.stroke_bias + w.stroke_amp * np.sin(om * T * ts)
        zdot = w.stroke_amp * om * np.cos(om * T * ts)
        hdot = (r[None, :] * zdot[:, None]) * np.cos(zeta)[:, None]             # up positive
        ideal = np.arctan(r * om * w.stroke_amp / U)                             # amplitude at each strip
        twist = w.twist_fraction * ideal[None, :] * np.cos(om * T * ts)[:, None]  # nose-down while hdot < 0
    tb = w.alpha_target if w.tilt_trim else 0.0                  # stroke plane (body) pitch to the path
    theta = (w.incidence if w.tilt_trim else w.alpha_target) + twist   # chord angle to the stroke plane
    thd = _pdiff(theta, dt)
    thdd = _pdiff(thd, dt)
    Ux, Uz = U * math.cos(tb), U * math.sin(tb)                  # free stream in body axes (Uz: from below)
    if w.tilt_trim:
        a = theta + np.arctan2(Uz - hdot + 0.5 * c * thd, Ux)
    else:
        a = theta + (-hdot + 0.5 * c * thd) / U
    U = Ux                                                        # Theodorsen terms use the in-plane speed
    a_mean = a.mean(axis=0, keepdims=True)
    a_osc = a - a_mean
    k = om * c / (2 * U)
    Fj, Gj = _jones(k, AR)
    ap = AR / (2 + AR) * (a_mean + Fj * a_osc + c / (2 * U) * (Gj / k) * _pdiff(a_osc, dt))
    V = np.sqrt(U ** 2 + (Uz - hdot) ** 2)
    q = 0.5 * rho * U * V * c * dr
    att = (np.abs(ap) <= math.radians(w.stall_deg)) | (not w.stall)
    dNa = rho * math.pi * c ** 2 / 4 * (U * _pdiff(a, dt) - 0.25 * c * thdd) * dr
    Vn = (V if w.tilt_trim else U) * np.sin(a)                   # normal velocity at mid-chord
    lev = w.post_stall == "polhamus" or (w.post_stall == "polhamus_st" and
                                          freq * (2 * wing_area_one(w) / span) / max(airspeed, 0.3) >= w.lev_strouhal)
    if lev:
        sep = (2 * math.pi * AR / (AR + 2) * np.sin(a) * np.cos(a) + math.pi * np.sin(a) * np.abs(np.sin(a))) * 0.5 * rho * V ** 2 * c * dr
    else:
        sep = w.cd_crossflow * 0.5 * rho * np.abs(Vn) * Vn * c * dr
    dNc = np.where(att, q * 2 * math.pi * ap, sep)
    dN = dNc + np.where(att, dNa, 0.5 * dNa)
    dTs = np.where(att, w.suction_eff * 2 * math.pi * (ap - c * thd / (4 * U)) ** 2 * q, 0.0)
    dDf = np.where(att, w.CD0 * 0.5 * rho * V ** 2 * c * dr, 0.0)
    dFx = dTs - dDf
    up_b = (dN * np.cos(theta) + dFx * np.sin(theta)) * np.cos(zeta)[:, None]   # body axes
    fwd_b = dFx * np.cos(theta) - dN * np.sin(theta)
    lift = up_b * math.cos(tb) + fwd_b * math.sin(tb)                           # flight-path axes
    thrust = fwd_b * math.cos(tb) - up_b * math.sin(tb)
    # shaft power: the drive moves each strip with hdot against the strip's force along that motion
    power = -((dN * np.cos(theta) + dFx * np.sin(theta)) * hdot).sum(axis=1)
    L = 2 * lift.sum(axis=1); Th = 2 * thrust.sum(axis=1); P = 2 * power
    return {"mean": {"thrust": float(Th.mean()), "lift": float(L.mean()), "shaft_power_w": float(np.clip(P, 0, None).mean()),
                     "shaft_power_w_full_recovery": float(P.mean()), "attached_frac": float(att.mean())},
            "AR": AR, "label": "DeLaurier 1993 modified strip theory; validation status in release/bird/"}
