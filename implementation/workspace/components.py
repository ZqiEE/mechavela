"""Adapters for existing motor/electrothermal helpers and bounded loaded-arm scenarios.

The drive interface is an assumed ideal torque/current controller, not MX-64 firmware.
No hardware approval or temperature identification is inferred from numerical success.
"""
import csv
import hashlib
import json
import math
from pathlib import Path
import xml.etree.ElementTree as ET
from implementation.robot.motors import MotorRecord, validate_params
from implementation.robot.thermal import ThermalParams, ThermalState
from .projects import encode, digest

def load(folder):
    p = json.loads((folder / 'actuators.json').read_text(encoding='utf-8'))
    if p.get('schema') != 'rlsok.arm-components/1':
        raise ValueError('Unsupported component profile schema')
    def number(value, lo, hi):
        return type(value) in (int, float) and math.isfinite(value) and lo <= value <= hi
    motor = p.get('motor', {})
    params = motor.get('params', {})
    if validate_params(params) or params.get('model') != 'm1' or 'R' not in params:
        raise ValueError('A complete voltage-driven BAM m1 parameter record is required')
    for k, lo, hi in [('kt', 1e-5, 100), ('R', 1e-5, 1000), ('armature', 1e-8, 10),
                       ('friction_base', 0, 10), ('friction_viscous', 0, 10)]:
        if not number(params[k], lo, hi): raise ValueError('Out-of-range motor parameter: '+k)
    if digest(encode(params)) != motor.get('params_sha256'):
        raise ValueError('Motor parameter identity mismatch: update provenance when importing changes')
    raw_source = (folder / 'motor-source.json').read_bytes()
    if digest(raw_source) != motor.get('source_file_sha256') or json.loads(raw_source) != params:
        raise ValueError('Motor source snapshot does not match parameter record')
    for key, lo, hi in [('supply_voltage_V', 1, 48), ('max_current_A', .001, 10),
                        ('max_output_torque_Nm', .001, 2), ('driver_efficiency', .1, 1),
                        ('electronics_power_W', 0, 20), ('encoder_counts', 16, 1000000),
                        ('control_period_s', .002, .05)]:
        if not number(p.get(key), lo, hi): raise ValueError('Invalid component parameter: '+key)
    if type(p['encoder_counts']) is not int:
        raise ValueError('Encoder counts must be an integer')
    thermal = p.get('thermal', {})
    for key, lo, hi in [('r_th_K_W', .01, 1000), ('tau_s', .1, 100000), ('ambient_C', -20, 60)]:
        if not number(thermal.get(key), lo, hi): raise ValueError('Invalid thermal parameter: '+key)
    scenarios = p.get('scenarios')
    if not isinstance(scenarios, list) or not 1 <= len(scenarios) <= 4:
        raise ValueError('Need 1..4 payload scenarios')
    names = set()
    for s in scenarios:
        name = s.get('id')
        if not isinstance(name, str) or not name.isidentifier() or name in names:
            raise ValueError('Unique scenario identifier required')
        names.add(name)
        if not number(s.get('payload_kg'), 0, 2): raise ValueError('Payload must be 0..2 kg')
    dts = p.get('timesteps_s')
    if not isinstance(dts, list) or len(dts) != 2 or not all(number(dt, .0005, .005) for dt in dts) or dts[0] != 2*dts[1]:
        raise ValueError('Two timesteps with 2:1 refinement required')
    for dt in dts:
        if abs(p['control_period_s']/dt-round(p['control_period_s']/dt)) > 1e-8:
            raise ValueError('Controller period must be an integer multiple of each solver timestep')
    req = json.loads((folder / 'requirements.json').read_text())
    limits = req.get('component_limits', {})
    fields = {'peak_current_A': (.001, 10), 'peak_speed_rad_s': (.001, 100),
              'peak_supply_current_A': (.001, 20), 'input_energy_J': (.001, 1000),
              'max_temperature_C': (-10, 150), 'final_error_step_delta_rad': (1e-8, .1)}
    if set(limits) != set(fields): raise ValueError('All component acceptance criteria must be explicit')
    for key, (lo, hi) in fields.items():
        if not number(limits[key], lo, hi): raise ValueError('Invalid acceptance criterion: '+key)
    # Period and test horizon do not change when refining the solver.
    if any(abs(req['duration_s']/dt-round(req['duration_s']/dt)) > 1e-8 for dt in dts):
        raise ValueError('Duration must align with both timesteps')
    return p

def create(store):
    from .reference import assets
    from implementation.robot.motors import MotorLibrary, BAM_PARAMS
    from implementation.robot.thermal import params_for
    # Explicit built-in record only: never silently shadowed by a user-library record.
    record = next(r for r in MotorLibrary(user_dir=Path('__no_user_library__')).builtin()
                  if r.motor_id == 'mx64' and r.model == 'm1')
    tp = params_for('mx64')
    p = {'schema': 'rlsok.arm-components/1', 'motor': {
            'candidate_part': 'ROBOTIS MX-64; candidate only, not procured/bench-tested',
            'params': record.params, 'params_sha256': digest(encode(record.params)),
            'source': record.source, 'source_file_sha256': record.sha256,
            'qualification': 'UPSTREAM_IDENTIFICATION_ONLY'},
         'supply_voltage_V': 12., 'max_current_A': 1.2, 'max_output_torque_Nm': 2.,
         'driver_efficiency': .9, 'electronics_power_W': 3., 'encoder_counts': 4096,
         'control_period_s': .01,
         'thermal': {'r_th_K_W': tp.r_th, 'tau_s': tp.tau, 'ambient_C': tp.t_amb,
                     'status': 'ASSUMED_NOT_IDENTIFIED'},
         'scenarios': [{'id': 'unloaded', 'payload_kg': 0.}, {'id': 'nominal', 'payload_kg': .25},
                       {'id': 'stress', 'payload_kg': .5}], 'timesteps_s': [.002, .001],
         'assumptions': ['12 V regulated supply, no voltage sag/wiring losses',
            'Ideal torque/current controller; MX-64 firmware interface NOT_VERIFIED',
            'No added external gearbox: kt/armature are joint-output equivalents from BAM',
            'Quantized position encoder, ideal noiseless velocity; no backlash/delay',
            'Driver efficiency and controller idle power are assumptions',
            'Thermal R/tau unmeasured; constant electrical resistance, no regen energy recovery']}
    files = assets()
    c = json.loads(files['controller.json']); c.update(kp=.4, kd=.15)
    req = json.loads(files['requirements.json'])
    req.update(scope='Horizontal planar positioning with payload; no lifting qualification',
               component_limits={'peak_current_A': 1.2, 'peak_speed_rad_s': 6.,
                  'peak_supply_current_A': 3., 'input_energy_J': 40.,
                  'max_temperature_C': 60., 'final_error_step_delta_rad': .003})
    files.update({'controller.json': json.dumps(c), 'requirements.json': json.dumps(req),
        'actuators.json': json.dumps(p, indent=2),
        'motor-source.json': (BAM_PARAMS / 'mx64/m1.json').read_bytes().decode('utf-8'),
        'README.md': 'Component-constrained horizontal arm candidate. Payload scenarios and motor '
          'limits are declared in actuators.json. Candidate hardware is not purchased or qualified. '
          'Inspect result.assumptions and evidence completeness. Temperature/energy estimates are conditional. '
          'No lifting, firmware, power supply, manufacturing or physical verification.\n'})
    components = json.loads(files['components.json'])
    components.update(actuators={'quantity': 2, 'candidate': 'MX-64', 'status': 'CANDIDATE_NOT_HARDWARE_VALIDATED'},
        sensors={'position': '4096 counts/revolution; no individual calibration', 'velocity': 'IDEAL'},
        electronics={'motor_driver': 'IDEAL_INTERFACE_ASSUMPTION', 'host_controller': 'UNSELECTED'},
        power={'supply': '12V / 3A design requirement; part UNSELECTED', 'wiring': 'NOT_DESIGNED'})
    files['components.json'] = json.dumps(components, indent=2)
    return store.create('Component-constrained desktop arm', 'Position horizontal payloads 0/0.25/0.5 kg '
        'within 2s and 0.03 rad; meet declared current, speed, input-energy and conditional thermal limits.',
        'manipulator', files, {'mechanics': {}, 'electronics': {}, 'control': {}, 'manufacturing': {}})

HEADERS = ['scenario', 'dt_s', 'time_s', 'q1_rad', 'q2_rad', 'dq1_rad_s', 'dq2_rad_s',
           'torque1_Nm', 'torque2_Nm', 'current1_A', 'current2_A', 'voltage1_V', 'voltage2_V',
           'pre_speed1_rad_s', 'pre_speed2_rad_s', 'input_power_W', 'input_energy_J', 'temp1_C', 'temp2_C']

def verdict(measured, req, warnings):
    criteria = {'final_joint_error_rad': req['maximum'], **req['component_limits']}
    return {key: {'measured': measured[key], 'maximum': maximum,
                  'passed': measured[key] <= maximum} for key, maximum in criteria.items()}

def run(folder, xml, controller, req):
    import mujoco
    import numpy as np
    from implementation.hardware.core import Channel,Capabilities,SimulatedDevice
    p = load(folder)
    rec = MotorRecord('component_candidate', 'm1', p['motor']['params'],
        {'control': 'volts', 'vin': p['supply_voltage_V'], 'max_pwm': 1.}, p['motor']['source'], {})
    tp = ThermalParams(p['thermal']['r_th_K_W'], p['thermal']['tau_s'], p['thermal']['ambient_C'], copper_tc=False)
    all_rows, summary, models = [], [], {}
    for scenario in p['scenarios']:
        for dt in p['timesteps_s']:
            root = ET.fromstring(xml)
            if root.find('.//body[@name="elbow"]') is None:
                raise ValueError('Component adapter requires the named reference arm geometry')
            if scenario['payload_kg']:
                ET.SubElement(root.find('.//body[@name="elbow"]'), 'geom',
                    {'type': 'sphere', 'size': '.025', 'pos': '.2 0 0', 'mass': str(scenario['payload_kg']),
                     'contype': '0', 'conaffinity': '0'})
            root.find('option').set('timestep', str(dt))
            for name in ('shoulder', 'elbow'):
                joint = root.find('.//joint[@name="'+name+'"]')
                if joint is None or joint.get('axis') != '0 0 1':
                    raise ValueError('Only the reference vertical hinge axes are supported')
                joint.set('armature', str(rec.params['armature']))
                joint.set('frictionloss', str(rec.params['friction_base']))
                joint.set('damping', str(rec.params['friction_viscous']))
            compiled_xml = ET.tostring(root, encoding='unicode')
            models[scenario['id']+'_'+str(dt)] = compiled_xml
            model = mujoco.MjModel.from_xml_string(compiled_xml)
            if model.nq != 2 or model.nv != 2 or model.nu != 2 or not np.allclose(model.actuator_gear[:, 0], 1):
                raise ValueError('Component adapter requires two hinge/motor pairs with unit gear')
            if not np.all(model.actuator_ctrllimited) or not np.allclose(model.actuator_ctrlrange[:,0], -model.actuator_ctrlrange[:,1]) or np.any(model.actuator_ctrlrange[:,1] <= 0):
                raise ValueError('Symmetric positive torque limits required')
            if list(model.jnt_qposadr) != [0,1] or list(model.actuator_trnid[:,0]) != [model.joint('shoulder').id, model.joint('elbow').id]:
                raise ValueError('Reference shoulder/elbow actuator order required')
            data = mujoco.MjData(model)
            device=SimulatedDevice(Capabilities('mujoco:component-arm',{'torque':{
                'first':Channel('Nm',-2,2),'second':Channel('Nm',-2,2)}},{'q1':'rad','q2':'rad'}));device.connect()
            thermal = ThermalState(tp, rec.kt, rec.resistance, 2)
            period = round(p['control_period_s']/dt)
            peak_i = peak_w = peak_supply = peak_torque = energy = 0.
            max_temp = tp.t_amb
            target = np.array(controller['target_rad'])
            command = np.zeros(2)
            rows = []
            for i in range(round(req['duration_s']/dt)):
                pre_speed = data.qvel.copy()
                if i % period == 0:
                    sensed = np.round(data.qpos*p['encoder_counts']/(2*math.pi))*(2*math.pi)/p['encoder_counts']
                    command = controller['kp']*(target-sensed)-controller['kd']*data.qvel
                envelope = np.array([min(rec.torque_limit(float(w)), p['max_current_A']*rec.kt,
                    p['max_output_torque_Nm'], float(np.max(np.abs(model.actuator_ctrlrange[j])))) for j,w in enumerate(pre_speed)])
                if np.max(np.abs(pre_speed)) > rec.no_load_speed():
                    raise RuntimeError('Speed outside supported conservative electrical envelope')
                torque = np.clip(command, -envelope, envelope)
                current = rec.current_from_torque(torque)
                voltage = current*rec.resistance+rec.kt*pre_speed
                if np.max(np.abs(voltage)) > p['supply_voltage_V']+1e-10:
                    raise RuntimeError('Electrical voltage bound violated')
                power = float(np.maximum(voltage*current, 0).sum()/p['driver_efficiency']+p['electronics_power_W'])
                energy += power*dt
                thermal.step(torque, dt)
                output=device.command('torque',{'first':float(torque[0]),'second':float(torque[1])},
                                      {'first':'Nm','second':'Nm'})
                data.ctrl[:] = [output['first'],output['second']]
                mujoco.mj_step(model, data)
                device.observe({'q1':float(data.qpos[0]),'q2':float(data.qpos[1])})
                if not np.allclose(data.actuator_force, torque, atol=1e-10):
                    raise RuntimeError('Actual actuator force differs from commanded torque')
                if not np.all(np.isfinite(data.qpos)) or abs(data.time-(i+1)*dt) > 1e-8:
                    raise RuntimeError('Solver reset/nonfinite state')
                peak_i = max(peak_i, float(np.max(np.abs(current))))
                peak_w = max(peak_w, float(np.max(np.abs(data.qvel))))
                peak_torque = max(peak_torque, float(np.max(np.abs(torque))))
                peak_supply = max(peak_supply, power/p['supply_voltage_V'])
                max_temp = max(max_temp, float(np.max(thermal.temp)))
                rows.append([scenario['id'], dt, float(data.time), *data.qpos.tolist(), *data.qvel.tolist(),
                    *torque.tolist(), *current.tolist(), *voltage.tolist(), *pre_speed.tolist(), power, energy, *thermal.temp.tolist()])
            warning_count = sum(int(w.number) for w in data.warning)
            summary.append({'scenario': scenario['id'], 'payload_kg': scenario['payload_kg'], 'dt_s': dt,
                'measured': {'final_joint_error_rad': float(np.max(np.abs(data.qpos-target))),
                    'peak_current_A': peak_i, 'peak_speed_rad_s': peak_w, 'peak_supply_current_A': peak_supply,
                    'input_energy_J': energy, 'max_temperature_C': max_temp, 'peak_command_Nm': peak_torque},
                'warnings': warning_count})
            all_rows.extend(rows)
    measured = {key: max(s['measured'][key] for s in summary) for key in summary[0]['measured']}
    measured['final_error_step_delta_rad'] = max(abs(summary[i]['measured']['final_joint_error_rad']-
        summary[i+1]['measured']['final_joint_error_rad']) for i in range(0,len(summary),2))
    warnings = [s for s in summary if s['warnings']]
    checks = verdict(measured, req, warnings)
    passed = all(c['passed'] for c in checks.values()) and not warnings
    with (folder / 'scenarios.csv').open('w', newline='') as f:
        w=csv.writer(f); w.writerow(HEADERS); w.writerows(all_rows)
    (folder / 'scenario-models.json').write_text(json.dumps(models, indent=2))
    # Existing replay API consumes the same basic columns; all-scenario evidence stays separate.
    first = p['scenarios'][0]['id']; finest = min(p['timesteps_s'])
    with (folder / 'trace.csv').open('w', newline='') as f:
        w=csv.writer(f); w.writerow(['time_s','q1_rad','q2_rad','dq1_rad_s','dq2_rad_s','torque1_Nm','torque2_Nm'])
        w.writerows([r[2:9] for r in all_rows if r[0]==first and r[1]==finest])
    return {'adapter': 'mujoco.component-arm/1', 'solver': {'name':'MuJoCo','version':mujoco.__version__},
        'hardware_interface':device.capabilities.describe(),
        'duration_s': req['duration_s'], 'steps': len(all_rows), 'measured': measured, 'criteria': req,
        'checks': checks, 'scenarios': summary, 'passed': passed, 'warnings': warnings,
        'levels': {'proposal':'RECORDED','artifact':'GENERATED','static':'PASSED','simulation':'COMPLETED',
            'requirements':'PASSED_CONDITIONAL' if passed else 'FAILED','manufacturing':'NOT_REVIEWED','physical':'NOT_TESTED'},
        'component_qualification': 'UPSTREAM_PARAMETERS_PLUS_UNVERIFIED_ASSUMPTIONS',
        'replay_scenario': {'id':first, 'dt_s':finest},
        'assumptions': p['assumptions'], 'thermal_status': 'ASSUMED_NOT_IDENTIFIED',
        'limitations': ['Horizontal payload positioning only, no lifting claim',
            'Ideal current/torque controller; hardware firmware compatibility not established',
            'Encoder quantization only; velocity sensing remains ideal',
            'Temperature/energy conditional on unmeasured thermal/driver assumptions',
            'Two-step final-error agreement is not complete dynamical convergence or hardware validation']}
