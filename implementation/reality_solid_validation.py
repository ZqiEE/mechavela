"""Read native CalculiX .dat tables and compare with declared bar references.

No mesh, displacement, temperature or flux is solved here. This report alone
cannot establish native execution; the caller must bind the actual Workflow
receipt, tool identities, mesh, input deck and raw output separately.
"""
import math
from pathlib import Path
import re

from .reality_solid_inputs import finite, inspect_mesh, parse_gmsh_inp


TABLES = {
    'displacements': ('U', 1, 3), 'forces': ('RF', 1, 3),
    'stresses': ('S', 2, 6), 'temperatures': ('NT', 1, 1),
    'heat generation': ('RFL', 1, 1), 'heat flux': ('HFL', 2, 3),
}
HEADER = re.compile(r'^\s*(displacements|forces|stresses|temperatures|heat generation|heat flux)'
                    r'(?:\s*\([^\n]*\))?\s+for set\s+(\S+)\s+and time\s+(\S+)\s*$', re.I)


def number(text):
    return finite(float(text.replace('D', 'E').replace('d', 'e')))


def parse_dat(path, kind):
    path = Path(path)
    if path.stat().st_size > 33554432:
        raise ValueError('BOUNDED_CALCULIX_DAT_REQUIRED')
    expected = {'U', 'RF', 'S'} if kind == 'structural' else {'NT', 'RFL', 'HFL'} if kind == 'thermal' else set()
    if not expected:
        raise ValueError('EXPLICIT_SOLID_KIND_REQUIRED')
    tables = {}; current = None
    for raw in path.read_text(encoding='ascii').splitlines():
        if not raw.strip():
            continue
        match = HEADER.fullmatch(raw)
        if match:
            name, setname, timestamp = match.groups()
            tag, ids, columns = TABLES[name.lower()]
            if tag not in expected or tag in tables or setname.upper() != ('DOMAIN' if ids == 2 else 'NALL') or not math.isclose(number(timestamp), 1., rel_tol=0, abs_tol=1e-7):
                raise ValueError('EXACT_SINGLE_FINAL_NATIVE_OUTPUT_TABLE_REQUIRED')
            current = (tag, ids, columns); tables[tag] = {}
            continue
        if current is None:
            raise ValueError('UNRECOGNIZED_NATIVE_DAT_HEADER')
        tag, ids, columns = current; values = raw.split()
        if len(values) != ids + columns:
            raise ValueError('EXACT_NATIVE_DAT_COLUMN_COUNT_REQUIRED')
        key = tuple(int(x) for x in values[:ids])
        if any(x <= 0 for x in key) or key in tables[tag]:
            raise ValueError('UNIQUE_NATIVE_NODE_OR_INTEGRATION_POINT_REQUIRED')
        tables[tag][key] = tuple(number(x) for x in values[ids:])
    if set(tables) != expected or any(not rows for rows in tables.values()):
        raise ValueError('ALL_REQUESTED_NATIVE_OUTPUTS_REQUIRED')
    return tables


def validate_bar(mesh, dat, plan, kind, force_factor=1., conductivity_factor=1.):
    nodes, cells = parse_gmsh_inp(mesh)
    length, width, height = map(finite, plan['geometry']['dimensions_m'])
    geometry = inspect_mesh(nodes, cells, (length, width, height))
    values = parse_dat(dat, kind)
    for tag, rows in values.items():
        expected = {(e, 1) for e in cells} if tag in ('S', 'HFL') else {(n,) for n in nodes}
        if set(rows) != expected:
            raise ValueError('COMPLETE_NATIVE_NODE_AND_C3D4_INTEGRATION_POINT_COVERAGE_REQUIRED')
    area = width * height; material = plan['material']; checks = {}; metrics = {}
    if kind == 'structural':
        force = finite(plan['structural']['force_x_N']) * finite(force_factor)
        young = finite(material['young_modulus_Pa']); nu = finite(material['poisson_ratio'])
        if force <= 0 or young <= 0 or not -1 < nu < .5:
            raise ValueError('POSITIVE_STABLE_ISOTROPIC_REFERENCE_PARAMETERS_REQUIRED')
        strain = force / area / young; displacement = strain * length; stress = force / area
        errors = []
        for (n,), actual in values['U'].items():
            x, y, z = nodes[n]; reference = (strain*x, -nu*strain*y, -nu*strain*z)
            errors.extend(abs(a-b) for a,b in zip(actual, reference))
        stress_error = max(abs(a-b) for row in values['S'].values() for a,b in zip(row,(stress,0.,0.,0.,0.,0.))) / stress
        reaction = math.fsum(values['RF'][(n,)][0] for n in geometry['left'])
        net = [math.fsum(row[i] for row in values['RF'].values()) for i in range(3)]
        actual_u = max(values['U'][(n,)][0] for n in geometry['right'])
        checks = {'all_node_affine_displacements': max(errors) <= max(1e-10, .01*displacement),
                  'all_integration_point_stresses': stress_error <= .01,
                  'left_reaction_balance': abs(reaction+force)/force <= .01,
                  'global_force_balance': max(map(abs,net))/force <= .01}
        metrics = {'right_displacement_x_m':actual_u,'reference_displacement_x_m':displacement,
                   'max_displacement_error_m':max(errors),'max_stress_relative_error':stress_error,
                   'left_reaction_x_N':reaction,'applied_force_x_N':force}
        design_pass = actual_u <= finite(plan['structural']['design_maximum_displacement_m'])
    else:
        cold = finite(plan['thermal']['left_temperature_K']); hot = finite(plan['thermal']['right_temperature_K'])
        k = finite(material['conductivity_W_per_m_K']) * finite(conductivity_factor)
        if k <= 0 or hot <= cold:
            raise ValueError('POSITIVE_CONDUCTIVITY_AND_TEMPERATURE_GRADIENT_REQUIRED')
        flux = -k*(hot-cold)/length; rate = abs(flux)*area
        terror = max(abs(row[0]-(cold+(hot-cold)*nodes[key[0]][0]/length)) for key,row in values['NT'].items())
        ferror = max(abs(a-b) for row in values['HFL'].values() for a,b in zip(row,(flux,0.,0.))) / abs(flux)
        left = math.fsum(values['RFL'][(n,)][0] for n in geometry['left'])
        right = math.fsum(values['RFL'][(n,)][0] for n in geometry['right'])
        total = math.fsum(row[0] for row in values['RFL'].values())
        checks = {'all_node_temperatures':terror <= .01,'all_integration_point_fluxes':ferror <= .01,
                  'both_end_heat_rate_magnitudes':max(abs(abs(left)-rate),abs(abs(right)-rate))/rate <= .01,
                  'opposed_end_heat_rates':left*right < 0 and abs(left+right)/rate <= .01,
                  'global_heat_balance':abs(total)/rate <= .01}
        metrics = {'max_temperature_error_K':terror,'max_flux_relative_error':ferror,
                   'mean_heat_flux_x_W_per_m2':math.fsum(row[0] for row in values['HFL'].values())/len(cells),
                   'reference_heat_flux_x_W_per_m2':flux,'left_heat_rate_W':left,'right_heat_rate_W':right,
                   'heat_rate_magnitude_W':abs(right),'reference_heat_rate_W':rate}
        design_pass = abs(right) >= finite(plan['thermal']['design_minimum_heat_rate_W'])
    verified = all(checks.values())
    return {'status':'NUMERICAL_REFERENCE_PASS' if verified else 'NUMERICAL_REFERENCE_FAIL',
            'engineering_acceptance':'PASS_SCOPED_SIMULATION' if verified and design_pass else 'FAIL',
            'checks':checks,'metrics':metrics,'nodes_checked':len(nodes),'integration_points_checked':len(cells),
            'native_execution_established_by_this_report':False,'platform_goal_achieved':False}
