"""Trusted versioned adapter registry. Project core and orchestration do not select by robot category."""
from dataclasses import dataclass

@dataclass(frozen=True)
class Adapter:
    id: str
    controller: str
    extra_inputs: tuple=()
    extra_sources: tuple=()
    inputs: tuple=('robot.xml','controller.json','requirements.json')

ADAPTERS={
 'mujoco.control-graph/1':Adapter('mujoco.control-graph/1','control_graph/1',(),('implementation/workspace/control_graph.py','implementation/workspace/jointspace.py','implementation/workspace/mobile.py')),
 'mujoco.joint-trajectory/1':Adapter('mujoco.joint-trajectory/1','joint_trajectory_pd/1',(),('implementation/workspace/jointspace.py','implementation/workspace/trajectory.py')),
 'mujoco.joint-space/1':Adapter('mujoco.joint-space/1','joint_space_pd/1',(),('implementation/workspace/jointspace.py',)),
 'ngspice.circuit/1':Adapter('ngspice.circuit/1','',(),
     ('implementation/workspace/electrical.py','implementation/solver_interface.py','implementation/reality_worker.py',
      'implementation/solvers/ngspice.py','implementation/solvers/ngspice_worker.py')+tuple('implementation/circuit/'+n+'.py' for n in ('netlist','spec','metrics','bom')),
     ('circuit.json','electrical-requirements.json')),
 'cad.parametric/1':Adapter('cad.parametric/1','',(),('implementation/workspace/cad.py','implementation/workspace/cad_kernel.py','implementation/workspace/cad_setup.py'),
     ('cad.json','cad-requirements.json')),
 'bird.mission/1':Adapter('bird.mission/1','bird-mission/1',
     ('design.json','physical.json','parts-library.json','criteria-r1.json','criteria-r2.json','criteria-r3.json'),
     ('implementation/workspace/bird.py',)+tuple('implementation/bird/'+n+'.py' for n in ('aero','flight','design','mission','replay','cad_sandbox')),
     ('controller.json','requirements.json')),
 'mujoco.joint-pd/1':Adapter('mujoco.joint-pd/1','joint_pd/1'),
 'mujoco.component-arm/1':Adapter('mujoco.component-arm/1','joint_pd/1',
     ('actuators.json','motor-source.json'),
     ('implementation/workspace/components.py','implementation/robot/motors.py',
      'implementation/robot/thermal.py','implementation/robot/labels.py')),
 'mujoco.differential-drive/1':Adapter('mujoco.differential-drive/1','differential-drive/1',(),
     ('implementation/workspace/mobile.py','implementation/hardware/core.py'))}

def select(controller,assets):
    if not isinstance(controller,dict):raise ValueError('Controller must be a JSON object; edit its native draft')
    kind=controller.get('kind')
    if kind=='control_graph/1':
        if 'actuators.json' in assets:raise ValueError('Component drive constraints cannot be downgraded to ideal composed control')
        key='mujoco.control-graph/1'
    elif kind in ('joint_space_pd/1','joint_trajectory_pd/1'):
        if 'actuators.json' in assets:raise ValueError('Component drive constraints cannot be downgraded to ideal joint-space dynamics')
        key='mujoco.joint-space/1' if kind=='joint_space_pd/1' else 'mujoco.joint-trajectory/1'
    elif kind=='joint_pd/1':key='mujoco.component-arm/1' if 'actuators.json'in assets else 'mujoco.joint-pd/1'
    elif kind=='differential-drive/1':key='mujoco.differential-drive/1'
    elif kind=='bird-mission/1':key='bird.mission/1'
    else:raise ValueError('Unsupported controller capability; no category substitution')
    spec=ADAPTERS[key]
    if not set(spec.extra_inputs)<=set(assets):raise ValueError('Required adapter inputs missing')
    return spec
