"""Fail closed when physical reports or artifacts are missing."""
import json
import re
from pathlib import Path
from implementation.gate import validate

def extract(directory, known_cells):
    directory = Path(directory)
    execution = json.loads((directory / 'execution.json').read_text())
    log = (directory / 'route.log').read_text()
    synth = (directory / 'synthesis.log').read_text()
    cells = json.loads((directory / 'mapped.json').read_text())['modules']['atoms_issue_select']['cells']
    unknown = sorted({cell['type'] for cell in cells.values()} - known_cells)
    def number(pattern, text=log):
        matches = re.findall(pattern, text)
        if not matches:
            raise ValueError('Missing metric: ' + pattern)
        return float(matches[-1])
    def slack(filename):
        values = re.findall(r'([-+\d.]+)\s+slack \((?:MET|VIOLATED)\)', (directory / filename).read_text())
        if not values:
            raise ValueError('Missing timing paths: ' + filename)
        return min(map(float, values))
    for artifact in ('routed.def', 'routed.odb', 'routed.v', 'routed.spef', 'route.guide', 'wire-length.csv'):
        if (directory / artifact).stat().st_size == 0:
            raise ValueError('Empty artifact: ' + artifact)
    # check_placement is fatal on violations; the completion marker occurs afterwards.
    complete = execution['state'] == 'SUCCEEDED' and execution['exit_code'] == 0 and 'FLOW_COMPLETED_MODULE_ONLY' in log
    drc = int(number(r'\[INFO DRT-0199\]\s+Number of violations = (\d+)\.'))
    if (directory / 'route.drc').read_text().strip():
        raise ValueError('Nonempty final DRC report')
    for report in ('electrical.rpt', 'constraint-check.rpt'):
        if (directory / report).read_text().strip():
            raise ValueError('Constraint violations: ' + report)
    metric = dict(synthesis_ok='found and reported 0 problems.' in synth.lower() and not unknown,
                  mapped_unknown_cells=len(unknown), unmapped_cell_types=unknown,
                  mapped_area_um2=number(r'Chip area for module[^:]+:\s*([\d.]+)', synth),
                  mapped_cell_count=len(cells), route_complete=complete,
                  post_route_cell_area_um2=number(r'POST_ROUTE_CELL_AREA_UM2 ([\d.]+)'),
                  post_route_cell_count=int(number(r'POST_ROUTE_CELL_COUNT (\d+)')),
                  setup_slack_ns=slack('setup.rpt'), hold_slack_ns=slack('hold.rpt'),
                  drc_violations=drc, placement_violations=0 if complete else None,
                  power_w=None, estimated_power_w=number(r'Total\s+[\deE.+-]+\s+[\deE.+-]+\s+[\deE.+-]+\s+([\deE.+-]+)', (directory/'power.rpt').read_text()),
                  power_assumption='OpenSTA default activity; no workload VCD; no hardware measurement',
                  detailed_route_wire_length_um=number(r'Total wire length = ([\d.]+) um\.'),
                  scope='module', evidence_kind='physical_design_tool_result')
    metric['gate'] = validate(metric)
    return metric
