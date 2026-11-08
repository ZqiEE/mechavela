"""Bill of materials derived from the final DUT netlist (no invented part numbers)."""
from __future__ import annotations

import csv
import io
import re

from implementation.circuit import netlist as N

KIND = {"R": "电阻 resistor", "C": "电容 capacitor", "L": "电感 inductor", "K": "磁耦合 coupling",
        "D": "二极管 diode", "Q": "双极晶体管 BJT", "M": "MOSFET", "J": "JFET",
        "S": "压控开关(理想) voltage-controlled switch", "V": "内部信号源(理想) internal signal source"}
UNIT = {"R": "Ω", "C": "F", "L": "H", "K": ""}


def engineering(value, unit):
    if value == 0:
        return "0" + unit
    for scale, prefix in ((1e9, "G"), (1e6, "M"), (1e3, "k"), (1, ""), (1e-3, "m"), (1e-6, "µ"), (1e-9, "n"), (1e-12, "p")):
        if abs(value) >= scale:
            return "{:.4g}{}{}".format(value / scale, prefix, unit)
    return "{:.4g}{}".format(value, unit)


PARASITIC = re.compile(r"^([RLC])PAR_([A-Za-z0-9_]+)$", re.IGNORECASE)


def build_bom(dut, relative_tolerance, part_suggestions=None):
    """One row per element. Elements named <R|L|C>PAR_<part> model a parasitic of <part>
    (ESR, winding resistance ...) and are marked purchasable=False; ideal internal sources too."""
    models = {m["name"].lower(): m for m in dut["models"]}
    names = {e["name"].lower() for e in dut["elements"]}
    suggestions = {k.lower(): v for k, v in (part_suggestions or {}).items()} if isinstance(part_suggestions, dict) else {}
    rows = []
    for element in dut["elements"]:
        kind = element["kind"]
        row = {"refdes": element["name"], "type": KIND.get(kind, kind), "nodes": " ".join(element["nodes"])}
        if kind in "RCLK":
            row["value"] = engineering(element["value"], UNIT[kind])
            row["value_si"] = element["value"]
            row["tolerance"] = "±%g%%" % (relative_tolerance * 100) if kind in "RCL" else "not varied"
        elif kind == "V":
            row["value"] = element["source"]
            row["tolerance"] = "ideal (not a purchasable part; replace by controller/driver)"
        else:
            model = models.get((element.get("model") or "").lower())
            row["value"] = element.get("model")
            row["model"] = model["line"] if model else None
            row["tolerance"] = "model parameters not varied"
        parasitic = PARASITIC.match(element["name"])
        if parasitic and parasitic.group(2).lower() in names:
            row["type"] = "寄生参数 parasitic of " + parasitic.group(2)
            row["purchasable"] = False
            row["note"] = "simulated model of %s; not a separate part" % parasitic.group(2)
        elif kind == "V":
            row["purchasable"] = False
        else:
            row["purchasable"] = True
        suggestion = suggestions.get(element["name"].lower())
        if suggestion:
            row["ai_part_suggestion_unverified"] = str(suggestion)[:200]
        rows.append(row)
    return rows


def bom_csv(rows):
    stream = io.StringIO()
    fields = ["refdes", "type", "value", "tolerance", "purchasable", "nodes", "model", "note", "ai_part_suggestion_unverified"]
    writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return stream.getvalue()
