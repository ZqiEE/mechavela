"""First-order motor thermal model.

    C dT/dt = P - (T - T_amb) / R_th          tau = R_th * C
    P = I^2 R(T),  I = tau_motor / kt,  R(T) = R_20 (1 + alpha_cu (T - 20 C))

Discretised exactly for piecewise-constant power:
    T_inf = T_amb + P R_th
    T[k+1] = T_inf + (T[k] - T_inf) exp(-dt / tau)

No servo vendor publishes R_th / tau for the hobby servos in the library, and
BAM did not identify them, so the defaults are ASSUMED and labelled as such.
They become user-calibrated only through `fit()` on a measured temperature log.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import numpy as np

from .labels import Status, label

ALPHA_CU = 0.00393  # 1/K, copper resistance temperature coefficient (physical constant)


@dataclass
class ThermalParams:
    r_th: float          # K/W winding-to-ambient
    tau: float           # s thermal time constant
    t_amb: float = 25.0  # C
    t_max: float = 70.0  # C protection/limit used for penalties and warnings
    copper_tc: bool = True
    status: Status = Status.ASSUMED
    note: str = ""

    def label(self) -> dict:
        if self.status is Status.ASSUMED:
            return label(Status.ASSUMED,
                         f"R_th={self.r_th} K/W、tau={self.tau} s 为假设值，没有厂商数据或实测辨识",
                         f"T_max={self.t_max} °C 为保护阈值假设；以你的固件设置为准",
                         "只能用来比较不同策略的相对发热，不能当作真实温度",
                         reason=self.note)
        return label(self.status, self.note)

    def to_json(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        d["label"] = self.label()
        return d


# Placeholder values: order of magnitude of small brushed DC motors in closed
# plastic housings.  Not measured for these servos.
DEFAULTS = {
    "sts3215": ThermalParams(r_th=15.0, tau=600.0, t_max=70.0,
                             note="STS3215 无公开热参数；70 °C 为该系列固件常见默认过温保护值，未核实"),
    "xl330": ThermalParams(r_th=25.0, tau=400.0, t_max=70.0, note="XL330 无公开热参数"),
    "xl320": ThermalParams(r_th=25.0, tau=400.0, t_max=65.0, note="XL-320 无公开热参数"),
    "mx64": ThermalParams(r_th=8.0, tau=900.0, t_max=80.0, note="MX-64 无公开热参数"),
    "mx106": ThermalParams(r_th=7.0, tau=1000.0, t_max=80.0, note="MX-106 无公开热参数"),
    "waveshare_st3025": ThermalParams(r_th=12.0, tau=700.0, t_max=70.0, note="ST3025 无公开热参数"),
}


def params_for(motor_id: str) -> ThermalParams | None:
    p = DEFAULTS.get(motor_id)
    return None if p is None else ThermalParams(**asdict(p))


def resistance_at(r20: float, temp_c, copper_tc: bool = True):
    if not copper_tc:
        return r20 + 0 * temp_c
    return r20 * (1.0 + ALPHA_CU * (temp_c - 20.0))


class ThermalState:
    """Vectorised over joints.  Call step() with motor torque each sim step."""

    def __init__(self, params: ThermalParams, kt: float, r20: float, n: int):
        self.p = params
        self.kt = kt
        self.r20 = r20
        self.temp = np.full(n, params.t_amb, dtype=float)
        self.energy = np.zeros(n)

    def step(self, torque: np.ndarray, dt: float) -> np.ndarray:
        current = np.asarray(torque, dtype=float) / self.kt
        r = resistance_at(self.r20, self.temp, self.p.copper_tc)
        power = current * current * r
        t_inf = self.p.t_amb + power * self.p.r_th
        self.temp = t_inf + (self.temp - t_inf) * math.exp(-dt / self.p.tau)
        self.energy += power * dt
        return power


def simulate(power: np.ndarray, dt: float, params: ThermalParams, t0: float | None = None) -> np.ndarray:
    """Temperature trace for a given power trace (no copper coefficient)."""
    power = np.asarray(power, dtype=float)
    t = np.empty(len(power) + 1)
    t[0] = params.t_amb if t0 is None else t0
    a = math.exp(-dt / params.tau)
    for k, pk in enumerate(power):
        t_inf = params.t_amb + pk * params.r_th
        t[k + 1] = t_inf + (t[k] - t_inf) * a
    return t


def steady_state(power: float, params: ThermalParams) -> float:
    return params.t_amb + power * params.r_th


def time_to_limit(power: float, params: ThermalParams, t0: float | None = None) -> float:
    """Seconds of constant power until T reaches t_max (inf if never)."""
    t0 = params.t_amb if t0 is None else t0
    t_inf = steady_state(power, params)
    if t_inf <= params.t_max:
        return math.inf
    if t0 >= params.t_max:
        return 0.0
    return -params.tau * math.log((t_inf - params.t_max) / (t_inf - t0))


def fit(dt: float, power: np.ndarray, temp: np.ndarray, t_amb: float | None = None) -> dict:
    """Identify (R_th, tau, T_amb) from a measured log by linear least squares on
    T[k+1] = a T[k] + b P[k] + c, with a = exp(-dt/tau), b = R_th (1-a), c = T_amb (1-a).

    power has len N, temp has len N+1 (temperature before and after each interval).
    """
    power = np.asarray(power, float)
    temp = np.asarray(temp, float)
    if len(temp) != len(power) + 1 or len(power) < 10:
        raise ValueError("need N>=10 power samples and N+1 temperature samples")
    y = temp[1:]
    if t_amb is None:
        X = np.column_stack([temp[:-1], power, np.ones_like(power)])
        (a, b, c), *_ = np.linalg.lstsq(X, y, rcond=None)
    else:
        X = np.column_stack([temp[:-1] - t_amb, power])
        (a, b), *_ = np.linalg.lstsq(X, y - t_amb, rcond=None)
        c = t_amb * (1 - a)
    if not (0 < a < 1):
        raise ValueError(f"identified decay a={a:.4f} not in (0,1): log not informative")
    tau = -dt / math.log(a)
    r_th = b / (1 - a)
    amb = c / (1 - a)
    pred = simulate(power, dt, ThermalParams(r_th=r_th, tau=tau, t_amb=amb), t0=temp[0])
    rmse = float(np.sqrt(np.mean((pred - temp) ** 2)))
    return {"r_th": float(r_th), "tau": float(tau), "t_amb": float(amb), "rmse_c": rmse}


def overheat_penalty(temp, t_max: float, margin: float = 10.0, xp=np):
    """Reward term (>=0, subtract it): quadratic once T exceeds t_max - margin.
    `xp` may be numpy or jax.numpy so the same function goes into training."""
    soft = t_max - margin
    excess = xp.maximum(temp - soft, 0.0) / margin
    return xp.sum(excess * excess, axis=-1)
