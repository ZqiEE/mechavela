"""Frozen verification spec: testbench + metrics + acceptance checks.

The external AI drafts the spec from the natural-language requirement (or a
human supplies one). After validation it is frozen (sha256) and the AI can
only change the DUT, never the testbench, loads, sources or thresholds.
"""
from __future__ import annotations

import copy
import re

from implementation.circuit import metrics as M
from implementation.circuit import netlist as N

SCHEMA = "rlsok.circuit-spec/1"
SPEC_FIELDS = {"interpretation", "ports", "testbench", "analysis", "vectors", "metrics", "checks",
               "operating_conditions", "assumptions"}
NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}$")
VECTOR = re.compile(r"^[A-Za-z0-9_().#:-]{1,96}$")


def _number(value, label):
    if type(value) not in (int, float) or value != value or value in (float("inf"), float("-inf")):
        raise ValueError("NUMBER_REQUIRED: " + label)
    return float(value)


def validate_spec(draft: dict, adapter) -> dict:
    """Validate an AI/human spec draft; return the normalized spec body."""
    if not isinstance(draft, dict):
        raise ValueError("SPEC_MUST_BE_OBJECT")
    missing = SPEC_FIELDS - set(draft)
    if missing:
        raise ValueError("SPEC_MISSING_FIELDS: " + ",".join(sorted(missing)))
    extra = set(draft) - SPEC_FIELDS
    if extra:
        raise ValueError("SPEC_UNKNOWN_FIELDS: " + ",".join(sorted(extra)))
    spec = copy.deepcopy(draft)
    ports = spec["ports"]
    if not isinstance(ports, list) or not 2 <= len(ports) <= 8 or len({p.lower() for p in ports}) != len(ports) \
            or any(not isinstance(p, str) or not NAME.match(p) for p in ports):
        raise ValueError("PORTS_MUST_BE_2_TO_8_UNIQUE_NAMES")
    if isinstance(spec["testbench"], str):
        spec["testbench"] = spec["testbench"].splitlines()
    tb_elements = N.validate_testbench(spec["testbench"], ports)
    xdut = next(e for e in tb_elements if e["kind"] == "X")
    if "0" not in xdut["nodes"]:
        raise ValueError("XDUT_MUST_CONNECT_ONE_PORT_TO_GROUND_NODE_0")
    if not any(e["kind"] in "VI" for e in tb_elements):
        raise ValueError("TESTBENCH_NEEDS_A_SOURCE")
    analysis = spec["analysis"]
    if not isinstance(spec["vectors"], list) or not spec["vectors"] or any(
            not isinstance(v, str) or not VECTOR.match(v) for v in spec["vectors"]):
        raise ValueError("VECTORS_MUST_BE_NONEMPTY_NAME_LIST")
    spec["vectors"] = [v.lower() for v in spec["vectors"]]
    # analysis validated with the solver's own rules
    adapter.validate({"netlist": "probe\nR1 a 0 1\nV1 a 0 1\n.end\n", "analysis": analysis, "vectors": ["v(a)"]})
    kind = analysis["kind"]
    metrics_def = spec["metrics"]
    if not isinstance(metrics_def, dict) or not 1 <= len(metrics_def) <= 40:
        raise ValueError("METRICS_MUST_BE_1_TO_40")
    for name, definition in metrics_def.items():
        if not NAME.match(name):
            raise ValueError("BAD_METRIC_NAME: " + name)
        if not isinstance(definition, dict):
            raise ValueError("METRIC_DEFINITION_OBJECT: " + name)
        if "derived" in definition:
            if set(definition) - {"derived", "of", "unit", "description"} or definition["derived"] not in M.DERIVED \
                    or not isinstance(definition.get("of"), list) or len(definition["of"]) != 2 \
                    or any(x not in metrics_def for x in definition["of"]):
                raise ValueError("BAD_DERIVED_METRIC: " + name)
            continue
        allowed = {"vector", "stat", "window", "multiply", "scale", "unit", "description",
                   "threshold", "at_hz", "level_db", "drop_db", "ref_hz"}
        if set(definition) - allowed:
            raise ValueError("UNKNOWN_METRIC_KEYS: %s %s" % (name, sorted(set(definition) - allowed)))
        definition["vector"] = str(definition.get("vector", "")).lower()
        if definition["vector"] not in spec["vectors"]:
            raise ValueError("METRIC_VECTOR_NOT_RECORDED: %s -> %s" % (name, definition["vector"]))
        if definition.get("multiply"):
            definition["multiply"] = definition["multiply"].lower()
            if definition["multiply"] not in spec["vectors"]:
                raise ValueError("METRIC_MULTIPLY_VECTOR_NOT_RECORDED: " + name)
            if kind == "ac":
                raise ValueError("MULTIPLY_NOT_SUPPORTED_FOR_AC: " + name)
        stat = definition.get("stat")
        if stat not in M.STATS_BY_KIND[kind]:
            raise ValueError("BAD_STAT_FOR_%s: %s=%r (allowed %s)" % (kind.upper(), name, stat, ",".join(M.STATS_BY_KIND[kind])))
        for key in ("scale", "threshold", "at_hz", "level_db", "drop_db", "ref_hz"):
            if key in definition:
                _number(definition[key], name + "." + key)
        if stat in ("db_at", "phase_at") and "at_hz" not in definition:
            raise ValueError("AT_HZ_REQUIRED: " + name)
        if stat == "f_cross_db" and not ("level_db" in definition) ^ ({"drop_db", "ref_hz"} <= set(definition)):
            raise ValueError("F_CROSS_DB_NEEDS_level_db_OR_drop_db_AND_ref_hz: " + name)
        if kind == "ac":
            for key in ("at_hz", "ref_hz"):
                if key in definition and not analysis["start_hz"] <= definition[key] <= analysis["stop_hz"]:
                    raise ValueError("FREQUENCY_OUTSIDE_SWEEP: %s.%s" % (name, key))
        if "window" in definition:
            window = definition["window"]
            if kind == "op" or not isinstance(window, list) or len(window) != 2:
                raise ValueError("WINDOW_NEEDS_TRAN_OR_AC_AND_TWO_VALUES: " + name)
            a, b = (_number(x, name + ".window") for x in window)
            lo, hi = (0, analysis["stop_seconds"]) if kind == "tran" else (analysis["start_hz"], analysis["stop_hz"])
            if not lo <= a < b <= hi:
                raise ValueError("WINDOW_OUTSIDE_ANALYSIS: " + name)
    checks = spec["checks"]
    if not isinstance(checks, list) or not 1 <= len(checks) <= 40:
        raise ValueError("CHECKS_MUST_BE_1_TO_40")
    ids = set()
    for check in checks:
        if not isinstance(check, dict) or check.get("metric") not in metrics_def or check.get("op") not in M.OPS:
            raise ValueError("BAD_CHECK: %r" % (check,))
        if not NAME.match(str(check.get("id", ""))) or check["id"] in ids:
            raise ValueError("CHECK_ID_UNIQUE_NAME_REQUIRED")
        ids.add(check["id"])
        if check["op"] == "between":
            if _number(check.get("min"), check["id"]) > _number(check.get("max"), check["id"]):
                raise ValueError("BETWEEN_MIN_GT_MAX: " + check["id"])
        else:
            _number(check.get("value"), check["id"])
        if set(check) - {"id", "metric", "op", "value", "min", "max", "unit", "description"}:
            raise ValueError("UNKNOWN_CHECK_KEYS: " + check["id"])
    conditions = spec["operating_conditions"]
    if not isinstance(conditions, dict) or not conditions:
        raise ValueError("OPERATING_CONDITIONS_REQUIRED")
    if not isinstance(spec["assumptions"], list):
        raise ValueError("ASSUMPTIONS_LIST_REQUIRED")
    if not isinstance(spec["interpretation"], str) or not spec["interpretation"].strip():
        raise ValueError("INTERPRETATION_REQUIRED")
    return spec


def temperature_celsius(spec) -> float:
    for line in N.logical_lines(spec["testbench"]):
        if line.lower().startswith(".temp"):
            return N.parse_value(line.split()[1])
        found = re.search(r"\btemp\s*=\s*([^\s]+)", line, re.IGNORECASE)
        if line.lower().startswith(".option") and found:
            return N.parse_value(found.group(1))
    return 27.0  # ngspice default nominal temperature


def audit_vectors(spec, dut):
    """Extra vectors the platform records to audit power of every ideal source."""
    ports = [p.lower() for p in spec["ports"]]
    xdut = next(e for e in N.validate_testbench(spec["testbench"], spec["ports"]) if e["kind"] == "X")
    port_map = {p: xdut["nodes"][i].lower() for i, p in enumerate(ports)}

    def node_vector(node, inside):
        node = node.lower()
        if inside:
            node = port_map.get(node, "xdut." + node)
        return None if node in ("0", "gnd") and not inside else ("v(%s)" % node if node != "0" else None)

    sources = []
    for element in N.validate_testbench(spec["testbench"], spec["ports"]):
        if element["kind"] == "V":
            sources.append({"name": element["name"], "where": "testbench", "branch": element["name"].lower() + "#branch",
                            "plus": node_vector(element["nodes"][0], False), "minus": node_vector(element["nodes"][1], False)})
    for element in dut["elements"]:
        if element["kind"] == "V":
            sources.append({"name": element["name"], "where": "dut",
                            "branch": "v.xdut.%s#branch" % element["name"].lower(),
                            "plus": node_vector(element["nodes"][0], True), "minus": node_vector(element["nodes"][1], True)})
    vectors = []
    for source in sources:
        for key in ("branch", "plus", "minus"):
            if source[key] and source[key] not in vectors:
                vectors.append(source[key])
    return sources, vectors


def source_power(source, vectors, kind, absolute=False):
    """Average power DELIVERED by an ideal V source (SPICE branch current flows into + terminal).

    absolute=True returns the mean of |p(t)| instead, so a source cannot hide
    energy it delivers in one interval by absorbing it in another.
    """
    current = vectors[source["branch"]]
    plus = vectors[source["plus"]] if source["plus"] else [0.0] * len(current)
    minus = vectors[source["minus"]] if source["minus"] else [0.0] * len(current)
    power = [-(p - m) * i for p, m, i in zip(plus, minus, current)]
    if absolute:
        power = [abs(value) for value in power]
    if kind == "op":
        return power[0]
    t = vectors["time"]
    return M.trapezoid(t, power) / (t[-1] - t[0])
