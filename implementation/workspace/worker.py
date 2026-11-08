"""Trusted fixed PD controller. Untrusted Python, plugins and external XML files never execute."""
import csv
import json
import math
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

def validate(folder,adapter=None):
    if adapter=='mujoco.control-graph/1' or (folder/'controller.json').exists() and json.loads((folder/'controller.json').read_text(encoding='utf-8')).get('kind')=='control_graph/1':
        from .control_graph import validate as validate_graph
        return validate_graph(folder)
    if adapter in ('cad.parametric/1','ngspice.circuit/1'):
        from . import cad,electrical
        (cad if adapter=='cad.parametric/1' else electrical).validate(folder)
        return None,{},{}
    if json.loads((folder/'controller.json').read_text(encoding='utf-8')).get('kind')=='joint_trajectory_pd/1':
        from .trajectory import validate as validate_trajectory
        return validate_trajectory(folder)
    if json.loads((folder/'controller.json').read_text(encoding='utf-8')).get('kind')=='joint_space_pd/1':
        from .jointspace import validate as validate_jointspace
        return validate_jointspace(folder)
    if json.loads((folder/'controller.json').read_text()).get('kind')=='bird-mission/1':
        from .bird import validate as bird_validate
        bird_validate(folder)
        return None,json.loads((folder/'controller.json').read_text()),json.loads((folder/'requirements.json').read_text())
    xml = (folder / 'robot.xml').read_text(encoding='utf-8')
    if len(xml) > 100000 or '<!' in xml:
        raise ValueError('XML declarations/entities are not accepted')
    root = ET.fromstring(xml)
    allowed = {'mujoco', 'compiler', 'option', 'worldbody', 'body', 'geom', 'joint', 'site',
               'actuator', 'motor', 'velocity', 'freejoint', 'sensor', 'jointpos', 'jointvel'}
    if root.tag != 'mujoco' or any(e.tag not in allowed or any(k in e.attrib for k in
                    ('file', 'plugin', 'mesh', 'hfield', 'texture')) for e in root.iter()):
        raise ValueError('Adapter only accepts self-contained primitive MJCF; no includes/plugins/files')
    if len(list(root.iter())) > 128:
        raise ValueError('Model element limit exceeded')
    c = json.loads((folder / 'controller.json').read_text())
    r = json.loads((folder / 'requirements.json').read_text())
    from .engineering import select
    spec=select(c,[f.name for f in folder.iterdir()])
    if spec.id=='mujoco.differential-drive/1':
        from .mobile import validate as mobile_validate
        mobile_validate(xml,c,r)
        return xml,c,r
    def finite(x, lo, hi):
        return type(x) in (int, float) and math.isfinite(x) and lo <= x <= hi
    if c.get('kind') != 'joint_pd/1' or not finite(c.get('kp'), 0, 100) or not finite(c.get('kd'), 0, 10):
        raise ValueError('Invalid bounded PD gains')
    target = c.get('target_rad')
    if not isinstance(target, list) or len(target) != 2 or not all(finite(x, -2, 2) for x in target):
        raise ValueError('This adapter requires two bounded joint targets')
    if r.get('metric') != 'final_joint_error_rad' or not finite(r.get('maximum'), .00001, .5) or not finite(r.get('duration_s'), .1, 5):
        raise ValueError('Unsupported requirement or duration (0.1..5 seconds)')
    if (folder / 'actuators.json').exists():
        from implementation.workspace.components import load
        load(folder)
    return xml, c, r

def run(folder,adapter=None):
    xml, c, req = validate(folder,adapter)
    if c.get('kind')=='control_graph/1':
        from .control_graph import run as run_graph
        return run_graph(folder)
    if c.get('kind')=='joint_trajectory_pd/1':
        from .trajectory import run as run_trajectory
        return run_trajectory(folder)
    if c.get('kind')=='joint_space_pd/1':
        from .jointspace import run as run_jointspace
        return run_jointspace(folder)
    if adapter in ('cad.parametric/1','ngspice.circuit/1'):
        from . import cad,electrical
        return (cad if adapter=='cad.parametric/1' else electrical).run(folder)
    if c.get('kind')=='bird-mission/1':
        from .bird import run as bird_run
        return bird_run(folder)
    if c.get('kind')=='differential-drive/1':
        from .mobile import run as mobile_run
        result=mobile_run(folder,xml,c,req)
        (folder/'result.json').write_text(json.dumps(result,indent=2,allow_nan=False))
        print(json.dumps({'steps':result['steps'],'requirements':result['levels']['requirements']}),flush=True)
        return
    if (folder / 'actuators.json').exists():
        from implementation.workspace.components import run as component_run
        result = component_run(folder, xml, c, req)
        (folder / 'result.json').write_text(json.dumps(result, indent=2, allow_nan=False))
        print(json.dumps({'steps': result['steps'], 'requirements': result['levels']['requirements']}), flush=True)
        return
    import mujoco
    import numpy as np
    model = mujoco.MjModel.from_xml_string(xml)
    if model.nq != 2 or model.nv != 2 or model.nu != 2 or not .001 <= model.opt.timestep <= .01:
        raise ValueError('Adapter requires two hinge axes, two motors, timestep 0.001..0.01')
    if any(int(x) != int(mujoco.mjtJoint.mjJNT_HINGE) for x in model.jnt_type):
        raise ValueError('PD adapter requires hinge joints')
    if not np.all(model.actuator_ctrllimited) or np.any(np.abs(model.actuator_ctrlrange) > 2):
        raise ValueError('Actuator torque bounds must be enabled and <=2 Nm')
    data = mujoco.MjData(model)
    target = np.array(c['target_rad'])
    from implementation.hardware.core import Channel,Capabilities,SimulatedDevice
    device=SimulatedDevice(Capabilities('mujoco:arm',{'torque':{'first':Channel('Nm',-2,2),'second':Channel('Nm',-2,2)}},
        {'q1':'rad','q2':'rad'}));device.connect()
    steps = math.ceil(req['duration_s'] / model.opt.timestep)
    rows = []
    peak = 0.
    for i in range(steps):
        data.ctrl[:] = np.clip(c['kp'] * (target - data.qpos) - c['kd'] * data.qvel,
                               model.actuator_ctrlrange[:, 0], model.actuator_ctrlrange[:, 1])
        output=device.command('torque',{'first':float(data.ctrl[0]),'second':float(data.ctrl[1])},
                              {'first':'Nm','second':'Nm'})
        data.ctrl[:]=[output['first'],output['second']]
        peak = max(peak, float(np.max(np.abs(data.ctrl))))
        mujoco.mj_step(model, data)
        device.observe({'q1':float(data.qpos[0]),'q2':float(data.qpos[1])})
        if not np.all(np.isfinite(data.qpos)) or float(data.time) < (i+1)*model.opt.timestep - 1e-8:
            raise RuntimeError('Nonfinite state or solver reset')
        if i % 5 == 0 or i == steps - 1:
            rows.append([float(data.time), *data.qpos.tolist(), *data.qvel.tolist(), *data.ctrl.tolist()])
    warnings = {str(i): int(w.number) for i, w in enumerate(data.warning) if w.number}
    error = float(np.max(np.abs(target-data.qpos)))
    with (folder / 'trace.csv').open('w', newline='') as f:
        w = csv.writer(f); w.writerow(['time_s', 'q1_rad', 'q2_rad', 'dq1_rad_s', 'dq2_rad_s', 'torque1_Nm', 'torque2_Nm']); w.writerows(rows)
    result = {'adapter': 'mujoco.joint-pd/1', 'solver': {'name': 'MuJoCo', 'version': mujoco.__version__},
              'hardware_interface':device.capabilities.describe(),
              'steps': steps, 'duration_s': float(data.time), 'measured': {'final_joint_error_rad': error, 'peak_command_Nm': peak},
              'criteria': req, 'passed': error <= req['maximum'] and not warnings, 'warnings': warnings,
              'levels': {'proposal': 'RECORDED', 'artifact': 'GENERATED', 'static': 'PASSED',
                         'simulation': 'COMPLETED', 'requirements': 'PASSED' if error <= req['maximum'] and not warnings else 'FAILED',
                         'manufacturing': 'NOT_REVIEWED', 'physical': 'NOT_TESTED'},
              'limitations': ['Ideal motors/sensors; primitive two-hinge rigid-body positioning', 'No motor/electronics/energy qualification',
                              'No manufacturing or physical verification', 'Trace is recorded simulation, not live hardware']}
    (folder / 'result.json').write_text(json.dumps(result, indent=2, allow_nan=False))
    print(json.dumps({'steps': steps, 'requirements': result['levels']['requirements']}), flush=True)

if __name__ == '__main__':
    run(Path(sys.argv[1]))
