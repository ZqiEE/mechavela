"""An original two-link desktop robot, ideal actuators; no hardware qualification."""
import json

MODEL = '''<mujoco model="RLSOK desktop arm">
 <compiler angle="radian"/><option timestep="0.002" gravity="0 0 -9.81" integrator="implicitfast"/>
 <worldbody><geom name="table" type="plane" size="1 1 .1"/>
  <body name="base" pos="0 0 .08"><geom type="cylinder" size=".07 .08" mass="1"/>
   <body name="shoulder" pos="0 0 .1"><joint name="shoulder" axis="0 0 1" damping=".08" range="-2 2"/>
    <geom type="capsule" fromto="0 0 0 .25 0 0" size=".018" mass=".3"/>
    <body name="elbow" pos=".25 0 0"><joint name="elbow" axis="0 0 1" damping=".06" range="-2 2"/>
     <geom type="capsule" fromto="0 0 0 .20 0 0" size=".015" mass=".2"/>
     <site name="tool" pos=".2 0 0" size=".015"/>
    </body></body></body></worldbody>
 <actuator><motor name="m1" joint="shoulder" ctrllimited="true" ctrlrange="-2 2"/>
 <motor name="m2" joint="elbow" ctrllimited="true" ctrlrange="-2 2"/></actuator>
 <sensor><jointpos name="q1" joint="shoulder"/><jointpos name="q2" joint="elbow"/>
 <jointvel name="dq1" joint="shoulder"/><jointvel name="dq2" joint="elbow"/></sensor>
</mujoco>'''

def assets():
    return {'robot.xml': MODEL,
            'controller.json': json.dumps({'kind': 'joint_pd/1', 'kp': .02, 'kd': .04, 'target_rad': [.7, -.5]}),
            'requirements.json': json.dumps({'metric': 'final_joint_error_rad', 'maximum': .03,
                                              'duration_s': 2.0, 'scope': 'unloaded planar joint positioning'}),
            'components.json': json.dumps({'links': ['base', 'shoulder', 'elbow'],
                'actuators': {'quantity': 2, 'type': 'ideal torque motor', 'limit_Nm': 2,
                              'hardware_part': 'UNSELECTED'},
                'sensors': ['2 ideal joint encoders', '2 ideal velocity channels'],
                'electronics': {'controller': 'UNSELECTED', 'motor_drivers': 'UNSELECTED'},
                'power': {'supply': 'UNSELECTED', 'energy_budget': 'NOT_VALIDATED'},
                'manufacturing': {'materials': 'UNSELECTED', 'assembly': 'base, shoulder, elbow; dimensions in MJCF; SI',
                                  'status': 'NOT_REVIEWED'}}),
            'README.md': 'Original RLSOK two-link tabletop positioning robot. SI units.\n'
              'Both axes vertical; gravity does not load joint rotation. No payload, wiring, motor identification, '
              'friction calibration, collision qualification, manufacturing or physical verification.\n'
              'Controller: torque = clip(kp*(target-q)-kd*dq, -2, 2), sampled every 0.002 seconds.\n'}

def create(store):
    return store.create('Desktop arm reference', 'Reach [.7, -.5] rad within 2 seconds, maximum joint error <= .03 rad.',
                        'manipulator', assets(), {'mechanics': {}, 'electronics': {}, 'control': {}, 'manufacturing': {}})
