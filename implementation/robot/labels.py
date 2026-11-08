"""Verification labels attached to every robot-engine result.

Every number RLSOK reports carries one of these, plus the conditions under
which it holds.  The rule from the platform owner: a result is "已验证" only
when it was actually produced by a run that passed pre-registered criteria;
anything assumed, smoke-sized, not run, or outside its validated scope says so.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum


class Status(str, Enum):
    VERIFIED = "VERIFIED"            # real run, met pre-registered criteria
    FAILED = "FAILED"                # real run, did not meet criteria
    UNVERIFIED = "UNVERIFIED"        # value exists but nobody checked it against reality
    ASSUMED = "ASSUMED"              # placeholder parameter, no identification behind it
    SMOKE = "SMOKE"                  # real run, too small to mean anything (pipeline check)
    NOT_RUN = "NOT_RUN"              # implemented, never executed
    NOT_AVAILABLE = "NOT_AVAILABLE"  # cannot run on this machine (e.g. no GPU)


ZH = {
    Status.VERIFIED: "已验证",
    Status.FAILED: "未通过",
    Status.UNVERIFIED: "未验证",
    Status.ASSUMED: "假设值（未验证）",
    Status.SMOKE: "冒烟运行（非基准，未验证）",
    Status.NOT_RUN: "未运行（未验证）",
    Status.NOT_AVAILABLE: "本机无法运行（未验证）",
}

EN = {
    Status.VERIFIED: "Verified",
    Status.FAILED: "Failed",
    Status.UNVERIFIED: "Unverified",
    Status.ASSUMED: "Assumed value (unverified)",
    Status.SMOKE: "Smoke run (not a benchmark, unverified)",
    Status.NOT_RUN: "Not run (unverified)",
    Status.NOT_AVAILABLE: "Cannot run on this machine (unverified)",
}


@dataclass
class Label:
    status: Status
    conditions: list[str] = field(default_factory=list)
    reason: str = ""
    evidence: list[str] = field(default_factory=list)

    @property
    def verified(self) -> bool:
        return self.status is Status.VERIFIED

    def to_json(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        d["zh"] = ZH[self.status]
        d["en"] = EN[self.status]
        return d


def label(status: Status, *conditions: str, reason: str = "", evidence=()) -> dict:
    return Label(status, list(conditions), reason, list(evidence)).to_json()
