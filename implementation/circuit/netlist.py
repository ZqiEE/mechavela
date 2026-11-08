"""SPICE netlist parsing helpers (values, elements) and DUT rules.

Pure text processing; no simulation. Used to validate what an external AI
submits, to perturb component values for tolerance runs, and to build the BOM.
"""
from __future__ import annotations

import math
import re

SUFFIX = [("meg", 1e6), ("mil", 25.4e-6), ("t", 1e12), ("g", 1e9), ("k", 1e3), ("m", 1e-3),
          ("u", 1e-6), ("µ", 1e-6), ("n", 1e-9), ("p", 1e-12), ("f", 1e-15)]
NUMBER = re.compile(r"^([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)([a-zA-Zµ]*)$")

# Element letters allowed inside the AI-designed DUT by default. Ideal
# controlled sources (B/E/F/G/H) and current sources are excluded so an
# "ideal regulator" cannot be faked; internal V sources are allowed only as
# signal generators and are power-audited after every simulation.
# JFET (J) is not accepted by the existing reality_worker.validate_spice filter, whose
# sha256 is bound into historical completion contracts, so it is not offered here.
DEFAULT_DUT_ELEMENTS = ("R", "C", "L", "K", "D", "Q", "M", "S", "V")
TESTBENCH_ELEMENTS = ("V", "I", "R", "C", "L", "X")
# (min, max) node count before the model name: BJT has an optional substrate node,
# MOSFET may be the 3-terminal VDMOS form or carry thermal nodes.
SEMICONDUCTOR_NODES = {"D": (2, 2), "Q": (3, 4), "J": (3, 3), "M": (3, 5)}
PASSIVE_TOLERANCED = ("R", "C", "L")


def parse_value(token: str) -> float:
    """SPICE number with engineering suffix and optional unit letters (e.g. 10k, 4.7uF, 2meg)."""
    match = NUMBER.match(token.strip())
    if not match:
        raise ValueError("NOT_A_SPICE_NUMBER: " + token)
    base, tail = float(match.group(1)), match.group(2).lower()
    for suffix, scale in SUFFIX:
        if tail.startswith(suffix):
            return base * scale
    if tail and not re.fullmatch(r"[a-z]*", tail):
        raise ValueError("NOT_A_SPICE_NUMBER: " + token)
    # trailing pure unit letters such as "ohm", "v", "a", "h" are ignored by SPICE
    if tail and tail[0] in "tgkmunpf":
        raise ValueError("AMBIGUOUS_SUFFIX: " + token)
    return base


def format_value(value: float) -> str:
    return "{:.12g}".format(value)


def logical_lines(text_or_lines):
    """Join '+' continuation lines; drop comments and blank lines. Keeps order."""
    lines = text_or_lines.splitlines() if isinstance(text_or_lines, str) else list(text_or_lines)
    out = []
    for raw in lines:
        line = raw.split(";", 1)[0].rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("*"):
            continue
        if stripped.startswith("+"):
            if not out:
                raise ValueError("CONTINUATION_WITHOUT_LINE")
            out[-1] = out[-1] + " " + stripped[1:].strip()
        else:
            out.append(stripped)
    return out


NODE_COUNT = {"R": 2, "C": 2, "L": 2, "D": 2, "V": 2, "I": 2, "Q": 3, "J": 3, "M": 4, "S": 4, "W": 2,
              "E": 4, "G": 4, "F": 2, "H": 2, "B": 2}


def parse_element(line: str) -> dict:
    tokens = line.replace("(", " ( ").replace(")", " ) ").split() if line[0].upper() in "VI" else line.split()
    name = tokens[0]
    kind = name[0].upper()
    if kind == "K":
        if len(tokens) != 4:
            raise ValueError("COUPLING_K_NEEDS_L1_L2_K: " + line)
        return {"name": name, "kind": "K", "nodes": [], "inductors": tokens[1:3], "value": parse_value(tokens[3]),
                "value_token_index": 3, "line": line}
    if kind == "X":
        return {"name": name, "kind": "X", "nodes": tokens[1:-1], "subckt": tokens[-1], "line": line}
    count = NODE_COUNT.get(kind)
    if count is None:
        raise ValueError("UNSUPPORTED_ELEMENT: " + name)
    if len(tokens) < count + 1:
        raise ValueError("TOO_FEW_TOKENS: " + line)
    element = {"name": name, "kind": kind, "nodes": tokens[1:1 + count], "line": line}
    rest = tokens[1 + count:]
    if kind in "RCL":
        if not rest:
            raise ValueError("MISSING_VALUE: " + line)
        element["value"] = parse_value(rest[0])
        element["value_token_index"] = 1 + count
        element["extra"] = rest[1:]
    elif kind in "DQJM":
        # Node count is resolved against the defined .model names in validate_dut_body
        # (BJT substrate node and 3-terminal VDMOS are optional forms).
        element["model"] = rest[0] if rest else None
        element["extra"] = rest[1:]
    elif kind in "SW":
        element["model"] = rest[0] if rest else None
        element["extra"] = rest[1:]
    elif kind in "VI":
        element["source"] = " ".join(rest)
    else:
        element["extra"] = rest
    return element


def parse_model(line: str) -> dict:
    match = re.match(r"^\.model\s+(\S+)\s+([A-Za-z]+)\s*\(?(.*?)\)?\s*$", line, re.IGNORECASE)
    if not match:
        raise ValueError("BAD_MODEL_LINE: " + line)
    params = dict(re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([^\s,()]+)", match.group(3)))
    return {"name": match.group(1), "type": match.group(2).upper(), "params": params, "line": line}


def source_kind(source: str) -> str:
    text = source.strip().upper()
    for word in ("PULSE", "SIN", "PWL", "EXP", "SFFM", "AM"):
        if re.search(r"\b" + word + r"\b", text):
            return word
    return "DC"


def validate_dut_body(lines, ports, allowed=None):
    """Validate AI DUT body lines. Returns parsed {elements, models}."""
    allowed = DEFAULT_DUT_ELEMENTS if allowed is None else allowed
    if isinstance(lines, str):
        lines = lines.splitlines()
    joined = logical_lines(lines)
    if not joined:
        raise ValueError("EMPTY_DUT")
    if len(joined) > 400:
        raise ValueError("DUT_TOO_LARGE")
    elements, models, names = [], [], set()
    for line in joined:
        if "{" in line or "}" in line or "'" in line or "`" in line:
            raise ValueError("EXPRESSIONS_NOT_ALLOWED_IN_DUT (use plain numbers): " + line)
        if re.search(r"\bic\s*=", line, re.IGNORECASE):
            # A precharged C or L would be an energy source the power audit cannot see.
            raise ValueError("INITIAL_CONDITIONS_NOT_ALLOWED_IN_DUT: " + line)
        lower = line.lower()
        if lower.startswith("."):
            if lower.startswith(".model"):
                models.append(parse_model(line))
                continue
            raise ValueError("ONLY_.model_DIRECTIVE_ALLOWED_IN_DUT: " + line.split()[0])
        kind = line[0].upper()
        if kind not in allowed:
            raise ValueError("ELEMENT_NOT_ALLOWED_IN_DUT: %s (allowed: %s)" % (line.split()[0], ",".join(allowed)))
        element = parse_element(line)
        if element["name"].lower() in names:
            raise ValueError("DUPLICATE_ELEMENT: " + element["name"])
        names.add(element["name"].lower())
        if kind in "RCL" and not (element["value"] > 0 and math.isfinite(element["value"])):
            raise ValueError("NONPOSITIVE_VALUE: " + line)
        if kind == "K" and not 0 < element["value"] <= 1:
            raise ValueError("COUPLING_OUT_OF_RANGE: " + line)
        elements.append(element)
    model_names = {m["name"].lower() for m in models}
    for element in elements:
        if element["kind"] in SEMICONDUCTOR_NODES:
            tokens = element["line"].split()
            low, high = SEMICONDUCTOR_NODES[element["kind"]]
            index = next((i for i in range(1 + low, min(1 + high, len(tokens) - 1) + 1)
                          if tokens[i].lower() in model_names), None)
            if index is None:
                raise ValueError("ELEMENT_MODEL_MUST_BE_DEFINED_IN_DUT: %s (expected %d-%d nodes then a .model name defined in the DUT)"
                                 % (element["line"], low, high))
            element.update(nodes=tokens[1:index], model=tokens[index], extra=tokens[index + 1:])
        model = element.get("model")
        if element["kind"] in "DQJMSW" and (not model or model.lower() not in model_names):
            raise ValueError("ELEMENT_MODEL_MUST_BE_DEFINED_IN_DUT: %s -> %s" % (element["name"], model))
    used = {node.lower() for element in elements for node in element["nodes"]}
    for port in ports:
        if port.lower() not in used and port not in ("0",):
            raise ValueError("PORT_NOT_CONNECTED_IN_DUT: " + port)
    if "0" in used:
        raise ValueError("DUT_MUST_USE_ITS_GROUND_PORT_NOT_NODE_0")
    return {"elements": elements, "models": models}


def validate_testbench(lines, ports):
    joined = logical_lines(lines)
    xdut = [l for l in joined if l.split()[0].upper() == "XDUT"]
    if len(xdut) != 1:
        raise ValueError("TESTBENCH_NEEDS_EXACTLY_ONE_XDUT")
    elements, names = [], set()
    for line in joined:
        if line.startswith("."):
            if line.lower().startswith((".temp", ".option", ".options", ".ic", ".nodeset")):
                continue
            raise ValueError("TESTBENCH_DIRECTIVE_NOT_ALLOWED: " + line.split()[0])
        if "{" in line or "`" in line:
            raise ValueError("EXPRESSIONS_NOT_ALLOWED_IN_TESTBENCH: " + line)
        kind = line[0].upper()
        if kind not in TESTBENCH_ELEMENTS:
            raise ValueError("TESTBENCH_ELEMENT_NOT_ALLOWED: " + line.split()[0])
        element = parse_element(line)
        if element["name"].lower() in names:
            raise ValueError("DUPLICATE_ELEMENT: " + element["name"])
        names.add(element["name"].lower())
        if kind == "X":
            if element["name"].upper() != "XDUT" or element["subckt"].lower() != "dut":
                raise ValueError("ONLY_XDUT_SUBCIRCUIT_ALLOWED")
            if len(element["nodes"]) != len(ports):
                raise ValueError("XDUT_NODE_COUNT_MUST_MATCH_PORTS")
        elements.append(element)
    return elements


def assemble(title, testbench_lines, ports, dut_lines):
    title = re.sub(r"[\r\n]", " ", title)[:120]
    body = [title, "* --- testbench (frozen by spec) ---"]
    body += logical_lines(testbench_lines)
    body += ["* --- DUT (external AI design) ---", ".subckt dut " + " ".join(ports)]
    body += logical_lines(dut_lines)
    body += [".ends dut", ".end", ""]
    return "\n".join(body)


def replace_value(line: str, element: dict, new_value: float) -> str:
    tokens = line.split()
    tokens[element["value_token_index"]] = format_value(new_value)
    return " ".join(tokens)
