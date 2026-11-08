"""Original differential-drive mobile robot on real MuJoCo wheel/ground contacts."""
import csv
import json
import math
import xml.etree.ElementTree as ET
from .projects import read

MODEL='''<mujoco model="RLSOK mobile base">
 <compiler angle="radian"/><option timestep="0.002" gravity="0 0 -9.81" integrator="implicitfast"/>
 <worldbody><geom name="ground" type="plane" size="3 3 .1" friction="1 .005 .0001"/>
 <body name="chassis" pos="0 0 .085"><freejoint name="base_free"/>
 <geom name="chassis_shell" type="box" size=".12 .07 .025" mass="1"/>
 <body name="left_wheel" pos="0 .095 -.035"><joint name="left_wheel" axis="0 1 0" damping=".002"/>
 <geom type="cylinder" size=".05 .014" quat=".707106781 .707106781 0 0" mass=".12" friction="1 .005 .0001"/></body>
 <body name="right_wheel" pos="0 -.095 -.035"><joint name="right_wheel" axis="0 1 0" damping=".002"/>
 <geom type="cylinder" size=".05 .014" quat=".707106781 .707106781 0 0" mass=".12" friction="1 .005 .0001"/></body>
 <geom name="rear_caster" priority="1" type="sphere" pos="-.1 0 -.06" size=".025" mass=".04" friction=".001 .0001 .00001"/>
 <geom name="front_caster" priority="1" type="sphere" pos=".1 0 -.06" size=".025" mass=".04" friction=".001 .0001 .00001"/>
 </body></worldbody>
 <actuator><velocity name="left_drive" joint="left_wheel" kv=".15" ctrllimited="true" ctrlrange="-15 15" forcelimited="true" forcerange="-.4 .4"/>
 <velocity name="right_drive" joint="right_wheel" kv=".15" ctrllimited="true" ctrlrange="-15 15" forcelimited="true" forcerange="-.4 .4"/></actuator>
</mujoco>'''

def create(store):
    controller={'kind':'differential-drive/1','goal_xy_m':[.6,0.], 'distance_gain':2.,'heading_gain':3.,
                'max_wheel_rad_s':1.,'wheel_radius_m':.05,'track_width_m':.19,'control_period_s':.01}
    req={'metric':'goal_distance_m','maximum':.04,'duration_s':3.,'terminal_speed_max_m_s':.04,
         'max_tilt_rad':.2,'minimum_contact_fraction':.95}
    files={'robot.xml':MODEL,'controller.json':json.dumps(controller),'requirements.json':json.dumps(req),
        'components.json':json.dumps({'structure':'chassis, two driven wheels, two passive caster approximations',
            'drive':'2 ideal bounded velocity actuators, 0.4 Nm force limit',
            'sensors':'ideal world-pose and velocity feedback (odometry/localization not identified)',
            'electronics':'controller/driver NOT_SELECTED','power':'NOT_VALIDATED','manufacturing':'NOT_REVIEWED'}),
        'README.md':'Complete digital mobile base reference. SI units; real MuJoCo gravity/contact/dynamics. '
            'Geometry, friction and mass are assumed; ideal velocity drive and world-pose sensing. '
            'No real motor, battery, localization, terrain, durability, manufacturing or physical qualification.\n'}
    return store.create('Wheeled mobile robot','Reach [.6,0] m in 3s, final error <= .04m and terminal speed <= .04m/s; remain upright with ground contact.',
                        'wheeled',files,{'mechanics':{},'control':{},'electronics':{},'power':{}})

def validate(xml,c,r):
    def finite(v,lo,hi):return type(v) in (int,float) and math.isfinite(v) and lo<=v<=hi
    if c.get('kind')!='differential-drive/1':raise ValueError('Wrong mobile controller')
    if not isinstance(c.get('goal_xy_m'),list) or len(c['goal_xy_m'])!=2 or not all(finite(v,-2,2) for v in c['goal_xy_m']):
        raise ValueError('Bounded XY goal required')
    for k,lo,hi in [('distance_gain',.1,10),('heading_gain',.1,10),('max_wheel_rad_s',.1,15),
                     ('wheel_radius_m',.01,.2),('track_width_m',.05,.5),('control_period_s',.002,.05)]:
        if not finite(c.get(k),lo,hi):raise ValueError('Invalid mobile controller field '+k)
    if r.get('metric')!='goal_distance_m':raise ValueError('Unsupported mobile requirement')
    for k,lo,hi in [('maximum',.001,.2),('duration_s',.1,5),('terminal_speed_max_m_s',.001,.5),
                     ('max_tilt_rad',.01,.5),('minimum_contact_fraction',.5,1.)]:
        if not finite(r.get(k),lo,hi):raise ValueError('Invalid mobile requirement '+k)
    option=ET.fromstring(xml).find('option')
    dt=float(option.get('timestep')) if option is not None else 0
    if not finite(dt,.001,.005) or abs(c['control_period_s']/dt-round(c['control_period_s']/dt))>1e-8 or abs(r['duration_s']/dt-round(r['duration_s']/dt))>1e-8:
        raise ValueError('Simulation/control horizon must align with timestep')

def metrics(rows,c,r):
    last=rows[-1]
    return {'goal_distance_m':math.hypot(last['x_m']-c['goal_xy_m'][0],last['y_m']-c['goal_xy_m'][1]),
            'terminal_speed_m_s':math.hypot(last['vx_m_s'],last['vy_m_s']),
            'max_tilt_rad':max(row['tilt_rad'] for row in rows),
            'contact_fraction':sum(row['contacts']>0 for row in rows)/len(rows),
            'peak_drive_torque_Nm':max(max(abs(row['torque_left_Nm']),abs(row['torque_right_Nm'])) for row in rows)}

def checks(m,r):
    return {'goal_distance':m['goal_distance_m']<=r['maximum'],
            'terminal_speed':m['terminal_speed_m_s']<=r['terminal_speed_max_m_s'],
            'upright':m['max_tilt_rad']<=r['max_tilt_rad'],
            'ground_contact':m['contact_fraction']>=r['minimum_contact_fraction'],
            'drive_force_bound':m['peak_drive_torque_Nm']<=.4+1e-10}

def wheel_reference(x,y,yaw,c):
    """Existing differential-drive feedback, reusable without a robot template."""
    dx,dy=c['goal_xy_m'][0]-x,c['goal_xy_m'][1]-y;distance=math.hypot(dx,dy);bearing=math.atan2(dy,dx)
    angle=math.atan2(math.sin(bearing-yaw),math.cos(bearing-yaw));v=c['distance_gain']*distance*max(0.,math.cos(angle));turn=c['heading_gain']*angle if distance>.005 else 0.
    return [max(-c['max_wheel_rad_s'],min(c['max_wheel_rad_s'],value/c['wheel_radius_m'])) for value in (v-turn*c['track_width_m']/2,v+turn*c['track_width_m']/2)]

def run(folder,xml,c,r):
    import mujoco
    import numpy as np
    from implementation.hardware.core import Channel,Capabilities,SimulatedDevice
    validate(xml,c,r)
    model=mujoco.MjModel.from_xml_string(xml)
    if (model.nq,model.nv,model.nu)!=(9,8,2):raise ValueError('Mobile adapter needs free base and two wheel axes')
    if list(model.jnt_type)!=[int(mujoco.mjtJoint.mjJNT_FREE),int(mujoco.mjtJoint.mjJNT_HINGE),int(mujoco.mjtJoint.mjJNT_HINGE)]:
        raise ValueError('Mobile model requires a free base and two hinge wheels')
    if not np.all(model.actuator_forcelimited) or not np.allclose(model.actuator_forcerange,[[-.4,.4],[-.4,.4]]) or not np.allclose(model.actuator_ctrlrange,[[-15,15],[-15,15]]) or not np.allclose(model.actuator_gear[:,0],1):
        raise ValueError('Expected bounded unit-gear wheel actuators')
    chassis=model.body('chassis').id
    ground=model.geom('ground').id
    if list(model.actuator_trnid[:,0])!=[model.joint('left_wheel').id,model.joint('right_wheel').id]:raise ValueError('Wheel mapping mismatch')
    data=mujoco.MjData(model);mujoco.mj_forward(model,data)
    device=SimulatedDevice(Capabilities('mujoco:wheels',{'velocity':{'left':Channel('rad/s',-15,15),'right':Channel('rad/s',-15,15)}},
        {'left_speed':'rad/s','right_speed':'rad/s'}));device.connect()
    dt=float(model.opt.timestep);period=round(c['control_period_s']/dt);rows=[]
    for i in range(round(r['duration_s']/dt)):
        if i%period==0:
            qw,qx,qy,qz=data.qpos[3:7]
            yaw=math.atan2(2*(qw*qz+qx*qy),1-2*(qy*qy+qz*qz))
            wheel=wheel_reference(float(data.qpos[0]),float(data.qpos[1]),yaw,c)
            outputs=device.command('velocity',{'left':float(wheel[0]),'right':float(wheel[1])},{'left':'rad/s','right':'rad/s'})
            data.ctrl[:]=[outputs['left'],outputs['right']]
        mujoco.mj_step(model,data)
        if not np.all(np.isfinite(data.qpos)) or abs(data.time-(i+1)*dt)>1e-8:raise RuntimeError('Solver reset/nonfinite state')
        device.observe({'left_speed':float(data.qvel[6]),'right_speed':float(data.qvel[7])})
        mujoco.mj_forward(model,data)
        tilt=math.acos(float(np.clip(data.xmat[chassis].reshape(3,3)[2,2],-1,1)))
        rows.append({'time_s':float(data.time),'x_m':float(data.qpos[0]),'y_m':float(data.qpos[1]),'z_m':float(data.qpos[2]),
            'qw':float(data.qpos[3]),'qx':float(data.qpos[4]),'qy':float(data.qpos[5]),'qz':float(data.qpos[6]),
            'vx_m_s':float(data.qvel[0]),'vy_m_s':float(data.qvel[1]),'tilt_rad':tilt,
            'contacts':sum(ground in (int(contact.geom1),int(contact.geom2)) for contact in data.contact),
            'left_rad':float(data.qpos[7]),'right_rad':float(data.qpos[8]),
            'command_left_rad_s':float(data.ctrl[0]),'command_right_rad_s':float(data.ctrl[1]),
            'torque_left_Nm':float(data.actuator_force[0]),'torque_right_Nm':float(data.actuator_force[1])})
    warning_count=sum(int(w.number) for w in data.warning)
    m=metrics(rows,c,r);verdicts=checks(m,r);passed=all(verdicts.values()) and not warning_count
    with (folder/'trace.csv').open('w',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    device.emergency_stop('Simulation completed')
    return {'adapter':'mujoco.differential-drive/1','solver':{'name':'MuJoCo','version':mujoco.__version__},
        'duration_s':float(data.time),'steps':len(rows),'measured':m,'criteria':r,'checks':verdicts,'passed':passed,
        'warnings':warning_count,'hardware_interface':device.capabilities.describe(),
        'levels':{'proposal':'RECORDED','artifact':'GENERATED','static':'PASSED','simulation':'COMPLETED',
            'requirements':'PASSED_SCOPED_SIMULATION' if passed else 'FAILED','manufacturing':'NOT_REVIEWED','physical':'NOT_TESTED'},
        'limitations':['Assumed primitive geometry/mass/friction','Ideal velocity actuation and world-pose sensing',
                       'Flat-ground waypoint test only; no power/terrain/localization/hardware qualification']}

def verify_evidence(folder,result,c,r):
    with (folder/'trace.csv').open() as f:
        rows=[{k:float(v) for k,v in row.items()} for row in csv.DictReader(f)]
    dt=float(ET.fromstring((folder/'robot.xml').read_text()).find('option').get('timestep'))
    if len(rows)!=round(r['duration_s']/dt):raise ValueError('Incomplete mobile trace')
    for i,row in enumerate(rows):
        if any(not math.isfinite(v) for v in row.values()) or abs(row['time_s']-(i+1)*dt)>1e-8:
            raise ValueError('Invalid mobile time/state trace')
        if abs(sum(row[k]**2 for k in ('qw','qx','qy','qz'))-1)>1e-7:raise ValueError('Invalid base quaternion')
        if max(abs(row[k]) for k in ('command_left_rad_s','command_right_rad_s'))>c['max_wheel_rad_s']+1e-10:
            raise ValueError('Wheel command bound violated')
    m=metrics(rows,c,r)
    for key,value in m.items():
        if abs(result['measured'][key]-value)>1e-10:raise ValueError('Mobile measurement mismatch')
    verdicts=checks(m,r)
    if result['checks']!=verdicts or result['passed']!=(all(verdicts.values()) and not result['warnings']):
        raise ValueError('Mobile criterion mismatch')
    if result['levels']['physical']!='NOT_TESTED':raise ValueError('Invalid physical qualification')
