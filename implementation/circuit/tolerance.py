"""Component tolerance cases (R, L, C values of the DUT varied within ±tol).

Generates perturbed DUT texts; every case is then simulated by the native
solver. Methods: one-factor-at-a-time extremes, two global corners and seeded
uniform Monte Carlo. This is sampling, not a mathematical worst-case proof.
"""
from __future__ import annotations

import random

from implementation.circuit import netlist as N


def toleranced_elements(dut):
    return [e for e in dut["elements"] if e["kind"] in N.PASSIVE_TOLERANCED]


def build_cases(dut_lines, dut, relative, samples, seed, max_cases=200):
    elements = toleranced_elements(dut)
    lines = N.logical_lines(dut_lines)
    index = {e["line"]: i for i, e in enumerate(elements)}
    cases = []

    def make(name, factors):
        new_lines = []
        for line in lines:
            if line in index:
                element = elements[index[line]]
                factor = factors.get(element["name"], 1.0)
                new_lines.append(N.replace_value(line, element, element["value"] * factor) if factor != 1.0 else line)
            else:
                new_lines.append(line)
        cases.append({"id": name, "factors": factors, "dut": new_lines})

    for element in elements:
        make("ofat_%s_plus" % element["name"], {element["name"]: 1 + relative})
        make("ofat_%s_minus" % element["name"], {element["name"]: 1 - relative})
    if elements:
        make("corner_all_plus", {e["name"]: 1 + relative for e in elements})
        make("corner_all_minus", {e["name"]: 1 - relative for e in elements})
    generator = random.Random(seed)
    for k in range(samples if elements else 0):
        make("mc_%03d" % (k + 1), {e["name"]: 1 + generator.uniform(-relative, relative) for e in elements})
    if len(cases) > max_cases:
        raise ValueError("TOO_MANY_TOLERANCE_CASES: %d > %d" % (len(cases), max_cases))
    return {"method": "OFAT ±tol for each R/L/C + 2 global corners + %d seeded uniform Monte Carlo" % (samples if elements else 0),
            "relative": relative, "seed": seed, "monte_carlo_samples": samples if elements else 0,
            "varied_elements": [{"name": e["name"], "kind": e["kind"], "nominal": e["value"]} for e in elements],
            "not_varied": "Semiconductor .model parameters, switch models, coupling K, internal signal sources and all testbench elements",
            "cases": cases}
