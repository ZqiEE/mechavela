"""Read sealed native forces against a restricted Stokes sphere reference.

No fluid solver or integration is implemented. Each new model must independently
pass provenance, model-scope, force-law, motion and momentum checks.
"""
import importlib.util
import json
import math
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT/'examples/reality/qualification-r2/air/reference.py'
spec = importlib.util.spec_from_file_location('rlsok_air_reference', REFERENCE)
reference = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reference)
LIMITS = {'max_force_error_N': 1e-15, 'max_velocity_error_m_per_s': 2e-8,
          'max_position_error_m': 5e-8, 'max_momentum_relative_residual': .002,
          'max_signed_symmetry_error_m_per_s': 1e-12}
SCOPE = 'Ideal sphere on one frictionless translation axis; constant uniform wind; isolated native low-Re viscous force. No resolved flow, quadratic drag, lift, turbulence, walls, wakes, rotation, compressibility or measured-air calibration.'


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def number(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError('FINITE_AIR_SI_NUMBER_REQUIRED')
    return float(value)


def close(actual, expected, name, atol=1e-14):
    if not math.isclose(number(actual), number(expected), rel_tol=1e-11, abs_tol=atol):
        raise ValueError('AIR_MODEL_OR_STATE_MISMATCH: '+name)


def numbers(text):
    return [number(float(value)) for value in text.split()]


def model_parameters(path):
    root = ET.parse(path).getroot()
    if root.tag != 'mujoco' or set(root.attrib) != {'model'} or [x.tag for x in root] != ['compiler','option','worldbody']:
        raise ValueError('OUTSIDE_VALIDATED_AIR_MODEL_SCOPE')
    if root.find('compiler').attrib != {'angle':'radian','inertiafromgeom':'false'}:
        raise ValueError('EXPLICIT_AIR_INERTIA_REQUIRED')
    option = root.find('option')
    if (set(option.attrib) != {'timestep','gravity','integrator','density','viscosity','wind'}
            or option.get('integrator') != 'implicitfast' or numbers(option.get('gravity')) != [0,0,0]):
        raise ValueError('AIR_SCOPE_REQUIRES_IMPLICITFAST_AND_ZERO_GRAVITY')
    world = root.find('worldbody')
    if len(world) != 1 or world[0].tag != 'body' or world[0].attrib != {'name':'sphere','pos':'0 0 0'}:
        raise ValueError('ONE_CENTERED_NONROTATING_SPHERE_REQUIRED')
    body = world[0]
    if [x.tag for x in body] != ['inertial','joint','geom']:
        raise ValueError('NO_ADDITIONAL_AIR_MODEL_FORCES_OR_GEOMETRY_ALLOWED')
    inertial, joint, geom = list(body)
    if (set(inertial.attrib) != {'pos','mass','diaginertia'} or numbers(inertial.get('pos')) != [0,0,0]
            or joint.attrib != {'name':'translation_x','type':'slide','axis':'1 0 0','limited':'false','damping':'0','frictionloss':'0'}
            or set(geom.attrib) != {'name','type','size','contype','conaffinity','fluidshape','fluidcoef'}
            or any(geom.get(k) != v for k,v in {'name':'sphere_fluid','type':'sphere','contype':'0','conaffinity':'0','fluidshape':'ellipsoid','fluidcoef':'0 0 0 0 0'}.items())):
        raise ValueError('ISOLATED_NATIVE_SPHERICAL_STOKES_TERM_REQUIRED')
    radius = numbers(geom.get('size'))
    wind = numbers(option.get('wind'))
    if len(radius) != 1 or len(wind) != 3 or wind[1:] != [0,0]:
        raise ValueError('ONE_RADIUS_AND_AXIAL_UNIFORM_WIND_REQUIRED')
    r, m, mu, rho, dt = radius[0], float(inertial.get('mass')), float(option.get('viscosity')), float(option.get('density')), float(option.get('timestep'))
    if not (.0005 <= r <= .002 and .5e-6 <= m <= 2e-6 and 0 <= mu <= 4e-5 and rho == 1.2
            and 0 < abs(wind[0]) <= .0002 and 0 < dt <= .001):
        raise ValueError('OUTSIDE_DECLARED_AIR_PARAMETER_ENVELOPE')
    inertia = numbers(inertial.get('diaginertia'))
    if len(inertia) != 3:
        raise ValueError('THREE_PRINCIPAL_INERTIAS_REQUIRED')
    for actual in inertia:
        close(actual, .4*m*r*r, 'uniform sphere inertia', atol=1e-25)
    reynolds = None if mu == 0 else rho*abs(wind[0])*2*r/mu
    if reynolds is not None and reynolds > .05:
        raise ValueError('REYNOLDS_OUTSIDE_CREEPING_FLOW_REFERENCE_SCOPE')
    return {'radius_m':r, 'mass_kg':m, 'dynamic_viscosity_Pa_s':mu, 'density_kg_per_m3':rho,
            'wind_world_m_per_s':wind, 'timestep_seconds':dt, 'initial_Re':reynolds,
            'zero_viscosity_is_code_path_negative_only':mu == 0}


def inspect_rows(header, rows, footer, params, steps):
    if (header.get('record') != 'header' or header.get('schema') != 'rlsok.mechanical-dynamics/1'
            or header.get('engine_version') != '3.16.0' or header.get('engine') != 'mujoco'
            or header.get('nv') != 1 or header.get('nq') != 1 or header.get('steps') != steps
            or header.get('integrator') != 'implicitfast'
            or header.get('dofs') != [{'index':0,'joint':'translation_x','joint_type':'slide'}]
            or footer != {'record':'footer','complete':True,'state_count':steps+1,'steps':steps}
            or len(rows) != steps+1):
        raise ValueError('COMPLETE_NATIVE_SLIDE_DYNAMICS_REQUIRED')
    dt, mass = params['timestep_seconds'], params['mass_kg']
    close(header['timestep_seconds'], dt, 'native timestep')
    max_x = max_v = max_force = max_decomp = max_equation = 0.
    force_integral = 0.
    prior = None
    for i,row in enumerate(rows):
        if row.get('record') != 'state' or row.get('step') != i or len(row['qpos']) != 1 or len(row['qvel']) != 1:
            raise ValueError('CONTIGUOUS_AIR_NATIVE_STATES_REQUIRED')
        close(row['time_seconds'], i*dt, 'native time', atol=1e-9)
        if row['contact_data'] or row['actuators'] or row['joints']:
            raise ValueError('NO_CONTACT_ACTUATOR_OR_HINGE_OBSERVATION_ALLOWED')
        close(row['mass_matrix'][0][0],mass,'native slide mass',atol=1e-18)
        if len(row['mass_matrix']) != 1 or len(row['mass_matrix'][0]) != 1:
            raise ValueError('SCALAR_CONSTANT_MASS_REQUIRED')
        q, velocity = number(row['qpos'][0]), number(row['qvel'][0])
        close(row['generalized_momentum'][0],mass*velocity,'native momentum',atol=1e-20)
        force = row['generalized_forces']
        if any(len(force[k]) != 1 for k in ('fluid','passive','smooth','bias','applied','actuator','constraint')):
            raise ValueError('SCALAR_NATIVE_FORCE_TERMS_REQUIRED')
        for k in ('bias','applied','actuator','constraint'):
            close(force[k][0],0.,'unexpected '+k,atol=1e-20)
        if any(any(number(v) != 0 for v in wrench) for wrench in row['body_applied_wrenches_force_then_torque']):
            raise ValueError('NO_EXTERNAL_BODY_WRENCH_ALLOWED')
        expected = reference.stokes_reference(row['time_seconds'],params['radius_m'],mass,params['dynamic_viscosity_Pa_s'],params['wind_world_m_per_s'][0])
        residual = reference.compare_native_state(row,params)
        max_x = max(max_x,abs(q-expected['position_m']))
        max_v = max(max_v,abs(velocity-expected['velocity_m_per_s']))
        max_force = max(max_force,abs(residual['force_law_error_N']))
        max_decomp = max(max_decomp,abs(residual['passive_decomposition_error_N']))
        max_equation = max(max_equation,abs(mass*number(row['qacc'][0])-number(force['fluid'][0])))
        if prior is not None:
            force_integral += .5*(prior+number(force['fluid'][0]))*dt
        prior = number(force['fluid'][0])
    close(rows[0]['qpos'][0],0.,'rest position')
    close(rows[0]['qvel'][0],0.,'rest velocity')
    momentum = mass*(rows[-1]['qvel'][0]-rows[0]['qvel'][0])
    impulse_error = 0. if force_integral == momentum == 0. else abs(force_integral-momentum)/abs(momentum) if momentum else math.inf
    checks = {'position_reference':max_x <= LIMITS['max_position_error_m'],
              'velocity_reference':max_v <= LIMITS['max_velocity_error_m_per_s'],
              'native_force_law':max_force <= LIMITS['max_force_error_N'],
              'passive_decomposition':max_decomp <= LIMITS['max_force_error_N'],
              'instantaneous_equation':max_equation <= LIMITS['max_force_error_N'],
              'full_step_impulse':impulse_error <= LIMITS['max_momentum_relative_residual']}
    return {'reference_validation':'MATCHED' if all(checks.values()) else 'MISMATCH','checks':checks,
            'metrics':{'final_position_m':rows[-1]['qpos'][0],'final_velocity_m_per_s':rows[-1]['qvel'][0],
                       'initial_native_fluid_force_N':rows[0]['generalized_forces']['fluid'][0],
                       'native_fluid_impulse_Ns':force_integral,'momentum_change_kg_m_per_s':momentum,
                       'momentum_relative_residual':impulse_error,'maximum_force_law_error_N':max_force,
                       'maximum_equation_error_N':max_equation,'maximum_position_error_m':max_x,
                       'maximum_velocity_error_m_per_s':max_v},'native_states_verified':len(rows)}


def validate_run(directory):
    from implementation.reality import verify, sha
    directory = Path(directory).resolve()
    verified = verify(directory)
    if verified.get('status') != 'FILES_AND_NATIVE_RECEIPT_VERIFIED':
        raise ValueError('SEALED_NATIVE_AIR_RUN_REQUIRED')
    request, result = read(directory/'request.json'), read(directory/'result.json')
    p = request['parameters']
    if (request['engine'] != 'mujoco' or request['project']['entry'] != 'sphere.xml'
            or set(request['project']['files']) != {'sphere.xml'}
            or p != {'steps':2000,'sample_every':10,'bodies':['sphere'],'joints':[],'controls':[],'render':False}):
        raise ValueError('FINITE_AIR_RUN_AND_EXPLICIT_OBSERVATIONS_REQUIRED')
    params = model_parameters(directory/'project/sphere.xml')
    files = read(directory/'files.json')['files']
    def artifact(name):
        key = name if name in files else 'native/'+name
        if key not in files or sha(directory/key) != files[key]:
            raise ValueError('SEALED_AIR_ARTIFACT_REQUIRED: '+name)
        return directory/key
    trajectory = read(artifact('trajectory.json'))
    model = trajectory['model']
    if (model['integrator'] != 'implicitfast' or model['gravity_m_per_s2'] != [0,0,0]
            or set(model['bodies']) != {'sphere'} or model['actuators'] or model['joints']):
        raise ValueError('NATIVE_AIR_MODEL_MISMATCH')
    close(model['bodies']['sphere']['mass_kg'],params['mass_kg'],'compiled mass',atol=1e-18)
    fluid = model['fluid']
    for k in ('density_kg_per_m3','dynamic_viscosity_Pa_s'):
        close(fluid[k],params[k],'native '+k)
    if fluid['wind_world_m_per_s'] != params['wind_world_m_per_s']:
        raise ValueError('NATIVE_WIND_MISMATCH')
    compiled = fluid['geom_native_fluid_parameters']
    if set(compiled) != {'sphere_fluid'} or len(compiled['sphere_fluid']) != 12 or compiled['sphere_fluid'][:6] != [1,0,0,0,0,0]:
        raise ValueError('NATIVE_ELLIPSOID_INTERACTION_AND_ZERO_QUADRATIC_LIFT_REQUIRED')
    lines = artifact('dynamics.jsonl').read_text(encoding='utf-8').splitlines()
    if len(lines) != 2003:
        raise ValueError('EXACT_2001_NATIVE_STATES_AND_TWO_BOOKENDS_REQUIRED')
    records = [json.loads(line) for line in lines]
    report = inspect_rows(records[0],records[1:-1],records[-1],params,2000)
    close(result['metrics']['body.sphere.final_velocity_x_m_per_s'],report['metrics']['final_velocity_m_per_s'],'result velocity')
    report.update(parameters=params,source_native_acceptance=verified['engineering_acceptance'],
                  evidence={key:{'path':str(path),'sha256':sha(path)} for key,path in {
                      'file_index':directory/'files.json','receipt':directory/'execution.json','result':directory/'result.json',
                      'model':directory/'project/sphere.xml','dynamics':artifact('dynamics.jsonl'),'trajectory':artifact('trajectory.json')}.items()},
                  scope=SCOPE,professional_solver_executions_launched=0,platform_goal_achieved=False)
    return report


def compare_runs(runs, expected):
    if set(runs) != set(expected) or len(runs) not in (4,5):
        raise ValueError('EXACT_APPROVED_AIR_RUN_SET_REQUIRED')
    reports = {name:validate_run(path) for name,path in runs.items()}
    matched = all(item['reference_validation'] == 'MATCHED' and item['source_native_acceptance'] == expected[name] for name,item in reports.items())
    baseline, doubled, reversed_wind, zero = (reports[key] for key in ('stokes_air','double_viscosity','reverse_wind','zero_viscosity_negative'))
    force_ratio = doubled['metrics']['initial_native_fluid_force_N']/baseline['metrics']['initial_native_fluid_force_N']
    symmetry = abs(reversed_wind['metrics']['final_velocity_m_per_s']+baseline['metrics']['final_velocity_m_per_s'])
    relational = abs(force_ratio-2) <= 1e-10 and symmetry <= LIMITS['max_signed_symmetry_error_m_per_s'] and zero['metrics']['final_velocity_m_per_s'] == 0
    return {'schema':'rlsok.air-stokes-validation/1','status':'PASS_SCOPED_NATIVE_AIR_STOKES' if matched and relational else 'NOT_PROVEN',
            'cases':reports,'doubled_viscosity_initial_force_ratio':force_ratio,'reverse_wind_velocity_symmetry_error_m_per_s':symmetry,
            'scope':SCOPE,'limits':LIMITS,'each_new_design_requires_new_native_evidence':True,
            'professional_solver_executions_launched':0,'platform_goal_achieved':False}


if __name__ == '__main__':
    if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
    if len(sys.argv) != 2: raise SystemExit('Usage: reality_air_validation.py SEALED_RUN_DIRECTORY')
    print(json.dumps(validate_run(sys.argv[1]),allow_nan=False))
