"""Motor library.

Built-in entries are the servo models identified by BAM (Rhoban, Apache-2.0,
https://github.com/Rhoban/bam) on their pendulum test bench.  Users can add
their own identified motors (same JSON format as BAM's params files); those are
stored with their sha256 and stay "未验证" until a reviewer/benchmark confirms
them.  A motor that is in neither set is returned as an explicit UNVERIFIED
placeholder - nothing is invented for it.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .labels import Status, label

ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "robot_assets"
BAM_PARAMS = ASSETS / "motors" / "bam_params"
BAM_VENDOR = ASSETS / "vendor"
USER_MOTORS = ROOT / "usage" / "robot-motors"

FRICTION_MODELS = ("m1", "m2", "m3", "m4", "m5", "m6")

# Firmware/driver constants used by BAM's actuator classes when the parameters
# were identified (bam/dynamixel/actuator.py, bam/feetech/actuator.py,
# bam/waveshare/actuator.py at the vendored commit).  The test suite checks
# this table against the vendored BAM classes so it cannot silently drift.
BAM_DRIVERS = {
    "sts3215": {"vendor": "Feetech", "name": "STS3215 (7.4 V)", "control": "volts",
                "vin": 7.4, "kp": 32, "error_gain": 0.166, "max_pwm": 0.97},
    "mx64": {"vendor": "Dynamixel", "name": "MX-64", "control": "volts",
             "vin": 15.0, "kp": 32.0, "error_gain": 0.158, "max_pwm": 0.9625},
    "mx106": {"vendor": "Dynamixel", "name": "MX-106", "control": "volts",
              "vin": 15.0, "kp": 32.0, "error_gain": 0.158, "max_pwm": 0.9625},
    "xl320": {"vendor": "Dynamixel", "name": "XL-320", "control": "volts",
              "vin": 7.5, "kp": 32.0, "error_gain": 0.05048199, "max_pwm": 1.0},
    "xl330": {"vendor": "Dynamixel", "name": "XL330", "control": "volts",
              "vin": 7.5, "kp": 400, "error_gain": 0.0028773775022263564, "max_pwm": 1.0},
    "waveshare_st3025": {"vendor": "Waveshare", "name": "ST3025", "control": "volts",
                         "vin": 12.0, "kp": 32, "error_gain": 0.166, "max_pwm": 1.0},
    "erob80_50": {"vendor": "ZeroErr", "name": "eRob80 (1:50)", "control": "amps"},
    "erob80_100": {"vendor": "ZeroErr", "name": "eRob80 (1:100)", "control": "amps"},
}

REQUIRED_KEYS = ("kt", "armature", "friction_base", "friction_viscous", "model", "actuator")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{1,63}$")


class MotorError(ValueError):
    pass


@dataclass
class MotorRecord:
    motor_id: str          # e.g. "sts3215"
    model: str             # BAM friction model m1..m6
    params: dict
    driver: dict
    source: dict
    label: dict
    sha256: str | None = None
    extra: dict = field(default_factory=dict)

    @property
    def known(self) -> bool:
        return bool(self.params)

    @property
    def kt(self) -> float:
        return float(self.params["kt"])

    @property
    def resistance(self) -> float | None:
        r = self.params.get("R")
        return None if r is None else float(r)

    # --- electrical helpers (DC motor equation used by BAM) -----------------
    def current_from_torque(self, torque):
        """Winding current [A] that produces a motor torque [N m]: I = tau / kt."""
        return torque / self.kt

    def copper_loss(self, torque, resistance: float | None = None):
        """Joule loss [W] = I^2 R.  Needs R (not identified for current-driven eRob80/100)."""
        r = self.resistance if resistance is None else resistance
        if r is None:
            raise MotorError(f"{self.motor_id}: winding resistance R not identified")
        i = self.current_from_torque(torque)
        return i * i * r

    def torque_limit(self, speed: float) -> float:
        """Max torque [N m] at joint speed [rad/s] for a voltage-driven servo:
        tau = kt (duty_max * vin - kt * w) / R, clipped at 0 (BAM DC-motor equation)."""
        d = self.driver
        if d.get("control") != "volts" or self.resistance is None:
            raise MotorError(f"{self.motor_id}: no voltage-drive envelope (control={d.get('control')})")
        v = d["vin"] * d["max_pwm"]
        return max(0.0, self.kt * (v - self.kt * abs(speed)) / self.resistance)

    def stall_torque(self) -> float:
        return self.torque_limit(0.0)

    def no_load_speed(self) -> float:
        d = self.driver
        return d["vin"] * d["max_pwm"] / self.kt

    def to_json(self) -> dict:
        return {
            "motor_id": self.motor_id, "model": self.model, "params": self.params,
            "driver": self.driver, "source": self.source, "label": self.label,
            "sha256": self.sha256, **({"extra": self.extra} if self.extra else {}),
        }


def _bam_commit() -> str | None:
    manifest = ASSETS / "MANIFEST.json"
    if manifest.exists():
        data = json.loads(manifest.read_text(encoding="utf-8"))
        return data.get("sources", {}).get("bam", {}).get("commit")
    return None


def validate_params(data: dict) -> list[str]:
    """Return a list of problems (empty == acceptable)."""
    problems = []
    for k in REQUIRED_KEYS:
        if k not in data:
            problems.append(f"缺少字段 {k}")
    if data.get("model") not in FRICTION_MODELS:
        problems.append(f"model 必须是 {FRICTION_MODELS} 之一")
    for k, v in data.items():
        if k in ("model", "actuator"):
            continue
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v):
            problems.append(f"{k} 必须是有限数值")
    for k in ("kt", "armature", "R"):
        if k in data and isinstance(data[k], (int, float)) and data[k] <= 0:
            problems.append(f"{k} 必须 > 0")
    for k in ("friction_base", "friction_viscous"):
        if k in data and isinstance(data[k], (int, float)) and data[k] < 0:
            problems.append(f"{k} 不能为负")
    return problems


class MotorLibrary:
    def __init__(self, bam_params: Path = BAM_PARAMS, user_dir: Path = USER_MOTORS):
        self.bam_params = Path(bam_params)
        self.user_dir = Path(user_dir)

    # -------------------------------------------------------------- listing
    def builtin(self) -> list[MotorRecord]:
        out = []
        if not self.bam_params.exists():
            return out
        for d in sorted(p for p in self.bam_params.iterdir() if p.is_dir()):
            for f in sorted(d.glob("m*.json")):
                out.append(self._from_bam_file(f))
        return out

    def user(self) -> list[MotorRecord]:
        out = []
        if not self.user_dir.exists():
            return out
        for f in sorted(self.user_dir.glob("*.json")):
            rec = json.loads(f.read_text(encoding="utf-8"))
            out.append(MotorRecord(**{k: rec[k] for k in (
                "motor_id", "model", "params", "driver", "source", "label", "sha256")}))
        return out

    def catalog(self) -> list[dict]:
        rows = {}
        for r in self.builtin() + self.user():
            key = (r.motor_id, r.source.get("kind"))
            row = rows.setdefault(key, {
                "motor_id": r.motor_id, "name": r.driver.get("name", r.motor_id),
                "vendor": r.driver.get("vendor"), "kind": r.source.get("kind"),
                "models": [], "label": r.label,
            })
            row["models"].append(r.model)
        return list(rows.values())

    # -------------------------------------------------------------- lookup
    def get(self, motor_id: str, model: str = "m6") -> MotorRecord:
        motor_id = motor_id.strip().lower()
        for r in self.user():
            if r.motor_id == motor_id and r.model == model:
                return r
        for r in self.builtin():
            if r.motor_id == motor_id and r.model == model:
                return r
        return MotorRecord(
            motor_id=motor_id, model=model, params={}, driver={}, source={"kind": "none"},
            label=label(Status.UNVERIFIED,
                        reason=f"电机 {motor_id} 不在库中：没有任何辨识数据，仿真只能用假设参数",
                        ),
        )

    # -------------------------------------------------------------- upload
    def add_user_motor(self, motor_id: str, data: dict, *, driver: dict | None = None,
                       note: str = "") -> MotorRecord:
        motor_id = motor_id.strip().lower()
        if not ID_RE.match(motor_id):
            raise MotorError("motor_id 只能含小写字母、数字、._-，长度 2–64")
        if motor_id in BAM_DRIVERS and not motor_id.startswith("user."):
            motor_id = "user." + motor_id  # never shadow a built-in identification
        problems = validate_params(data)
        if problems:
            raise MotorError("；".join(problems))
        raw = json.dumps(data, sort_keys=True).encode()
        sha = hashlib.sha256(raw).hexdigest()
        drv = dict(driver or BAM_DRIVERS.get(data["actuator"], {}))
        rec = MotorRecord(
            motor_id=motor_id, model=data["model"], params=dict(data), driver=drv,
            source={"kind": "user", "note": note},
            label=label(Status.UNVERIFIED, "用户上传的辨识参数，平台未复核",
                        reason="用户数据只做记录与仿真，不会被标为已验证，除非用真机基准对上"),
            sha256=sha,
        )
        self.user_dir.mkdir(parents=True, exist_ok=True)
        (self.user_dir / f"{motor_id}.{data['model']}.json").write_text(
            json.dumps(rec.to_json(), indent=2, ensure_ascii=False), encoding="utf-8")
        return rec

    # -------------------------------------------------------------- internal
    def _from_bam_file(self, f: Path) -> MotorRecord:
        data = json.loads(f.read_text(encoding="utf-8"))
        motor_id = data["actuator"]
        drv = dict(BAM_DRIVERS.get(motor_id, {}))
        commit = _bam_commit()
        conds = [
            f"参数来自 BAM（Rhoban）在其摆锤测试台上对该型号的实测辨识，提交 {commit or '未知'}",
            "RLSOK 没有对你手上的个体电机复测：个体差异、温度、磨损会造成偏离",
        ]
        if drv.get("control") == "volts":
            conds.append(f"辨识时供电 {drv['vin']} V、固件 P 增益 {drv['kp']}；不同电压/增益需重新换算")
        return MotorRecord(
            motor_id=motor_id, model=data["model"], params=data, driver=drv,
            source={"kind": "bam", "repo": "https://github.com/Rhoban/bam", "commit": commit,
                    "file": f.relative_to(ASSETS).as_posix(), "license": "Apache-2.0"},
            label=label(Status.VERIFIED, *conds),
            sha256=hashlib.sha256(f.read_bytes()).hexdigest(),
        )


def import_bam():
    """Import the vendored BAM package (robot_assets/vendor/bam)."""
    if str(BAM_VENDOR) not in sys.path:
        sys.path.insert(0, str(BAM_VENDOR))
    import bam.model  # noqa: F401
    import bam.mujoco  # noqa: F401
    import bam

    return bam


def bam_model(record: MotorRecord):
    """Build a BAM Model object for a library record (raises if unknown)."""
    if not record.known:
        raise MotorError(f"{record.motor_id}: 不在库中，无法构建 BAM 模型")
    import_bam()
    from bam.model import load_model_from_dict

    return load_model_from_dict(dict(record.params))
