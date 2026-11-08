"""Named position/velocity channels composed in ONE native MuJoCo model.

Reuses the existing differential-drive feedback block, with geometry measured
from the supplied model. No robot model, arm angles or IK answer is supplied.
"""
import csv,json,math,xml.etree.ElementTree as ET
from . import jointspace,mobile
from .projects import read
ADAPTER='mujoco.control-graph/1'
KIND='control_graph/1'

def validate(folder):
    c=read(folder/'controller.json');r=read(folder/'requirements.json')
    if not isinstance(c,dict) or set(c)!={'kind','channels','drive','observation'} or c['kind']!=KIND:raise ValueError('Invalid composed controller schema')
    channels=c['channels']
    if not isinstance(channels,list) or not 3<=len(channels)<=24:raise ValueError('3..24 named channels required')
    names=[]
    for ch in channels:
        if not isinstance(ch,dict) or ch.get('mode') not in ('position','velocity'):raise ValueError('Position or velocity channel required')
        fields={'joint','mode','max_torque_Nm'}|({'kp','kd','target_rad','ramp_s'} if ch['mode']=='position' else {'kv'})
        if set(ch)!=fields:raise ValueError('Unexpected control channel fields')
        names.append(ch['joint'])
        bounds={'max_torque_Nm':(.01,20),'kp':(0,200),'kd':(0,30),'target_rad':(-1.5,1.5),'ramp_s':(.05,2),'kv':(0,30)}
        if any(not jointspace.finite(ch[k],*bounds[k]) for k in ch if k in bounds):raise ValueError('Nonfinite/out-of-envelope channel parameter')
    required={'metric','duration_s','base_goal_xy_m','maximum_base_error_m','tool_goal_xyz_m','maximum_tool_error_m','maximum_terminal_speed_m_s','maximum_base_tilt_rad','minimum_tool_motion_m','minimum_tool_body_mass_kg','minimum_drive_contact_fraction'}
    if not isinstance(r,dict) or not required<=set(r)<=required|{'authoring_mode'} or r['metric']!='composed_task/1':raise ValueError('Explicit composed task criteria required')
    bounds={'duration_s':(.1,5),'maximum_base_error_m':(.001,.2),'maximum_tool_error_m':(.001,.2),'maximum_terminal_speed_m_s':(.001,.5),'maximum_base_tilt_rad':(.01,.5),'minimum_tool_motion_m':(.01,1),'minimum_tool_body_mass_kg':(.01,2),'minimum_drive_contact_fraction':(.1,1)}
    if any(not jointspace.finite(r[k],*b) for k,b in bounds.items()):raise ValueError('Invalid owner task bound')
    for key,n in [('base_goal_xy_m',2),('tool_goal_xyz_m',3)]:
        if not isinstance(r[key],list) or len(r[key])!=n or not all(jointspace.finite(v,-2,2) for v in r[key]):raise ValueError('Bounded world-frame goal required')
    xml,_,_=jointspace.validate(folder,{'kind':'joint_space_pd/1','joint_names':names,'target_rad':[0]*len(names),'kp':0,'kd':0},{'metric':'final_joint_error_rad','maximum':.1,'duration_s':r['duration_s']})
    joints=list(ET.fromstring(xml).iter('joint'))
    if any((j.get('limited')=='false')!=(ch['mode']=='velocity') for j,ch in zip(joints,channels)):raise ValueError('Velocity axes must be continuous; position axes bounded')
    d=c['drive'];o=c['observation']
    if not isinstance(d,dict) or set(d)!={'left','right','distance_gain','heading_gain','max_wheel_rad_s'}:raise ValueError('Explicit planar-drive bindings required')
    velocity=[ch['joint'] for ch in channels if ch['mode']=='velocity']
    if any(not isinstance(d[k],str) for k in ('left','right')) or len(velocity)!=2 or len({d['left'],d['right']})!=2 or set(velocity)!={d['left'],d['right']}:raise ValueError('Planar drive requires exactly two declared velocity axes')
    if any(not jointspace.finite(d[k],lo,hi) for k,lo,hi in [('distance_gain',.1,10),('heading_gain',.1,10),('max_wheel_rad_s',.1,15)]):raise ValueError('Invalid planar feedback gains')
    if not isinstance(o,dict) or set(o)!={'body','point_m'} or not isinstance(o['body'],str) or not isinstance(o['point_m'],list) or len(o['point_m'])!=3 or not all(jointspace.finite(v,-1,1) for v in o['point_m']):raise ValueError('Explicit observed body/local point required')
    return xml,c,r

def model_geometry(xml,c,r):
    import mujoco,numpy as np
    names=[ch['joint'] for ch in c['channels']];model,qadr,vadr,base=jointspace._model(xml,{'joint_names':names})
    if base is None or not np.allclose(model.opt.gravity,[0,0,-9.81]):raise ValueError('One native free base under gravity required')
    free=int(np.where(model.jnt_type==mujoco.mjtJoint.mjJNT_FREE)[0][0]);fq=int(model.jnt_qposadr[free])
    data=mujoco.MjData(model);mujoco.mj_forward(model,data)
    plane=[i for i,t in enumerate(model.geom_type) if t==mujoco.mjtGeom.mjGEOM_PLANE]
    if len(plane)!=1 or model.geom_bodyid[plane[0]]!=0 or not np.allclose(data.geom_xmat[plane[0]].reshape(3,3)[:,2],[0,0,1]):raise ValueError('One horizontal world ground plane required')
    tool=mujoco.mj_name2id(model,mujoco.mjtObj.mjOBJ_BODY,c['observation']['body'])
    if tool<=0 or model.body_mass[tool]<r['minimum_tool_body_mass_kg']:raise ValueError('Observed native body/payload mass outside owner criterion')
    if not np.allclose(c['observation']['point_m'],model.body_ipos[tool],atol=1e-9,rtol=0):raise ValueError('Observed point must be the modeled payload body mass center')
    ancestors=set();node=tool
    while node:ancestors.add(node);node=int(model.body_parentid[node])
    position=[i for i,ch in enumerate(c['channels']) if ch['mode']=='position']
    if base not in ancestors or any(int(model.jnt_bodyid[model.joint(names[i]).id]) not in ancestors for i in position):raise ValueError('Position-controlled channels must belong to the observed point kinematic chain')
    wheels=[];signs=[];centers=[];radii=[];indices=[]
    for name in (c['drive']['left'],c['drive']['right']):
        i=names.index(name);jid=model.joint(name).id;body=int(model.jnt_bodyid[jid]);node=int(model.body_parentid[body])
        while node!=base and node:
            if model.body_jntnum[node]:raise ValueError('Drive axes cannot be mounted beyond another actuated joint')
            node=int(model.body_parentid[node])
        if node!=base:raise ValueError('Drive must be attached to the same free base')
        geoms=[g for g in range(model.ngeom) if model.geom_bodyid[g]==body]
        if len(geoms)!=1 or model.geom_type[geoms[0]]!=mujoco.mjtGeom.mjGEOM_CYLINDER or not np.allclose(model.geom_pos[geoms[0]],0) or not np.allclose(model.jnt_axis[jid],[0,0,1]):raise ValueError('Each drive axis needs one centered local-Z cylinder and coaxial hinge')
        g=geoms[0];axis=data.xmat[body].reshape(3,3)[:,2];base_y=data.xmat[base].reshape(3,3)[:,1];dot=float(axis@base_y)
        if abs(abs(dot)-1)>1e-6:raise ValueError('Drive cylinders must be parallel to the base lateral axis')
        wheels.append(g);signs.append(1 if dot>0 else -1);radii.append(float(model.geom_size[g,0]));centers.append(data.xmat[base].reshape(3,3).T@(data.geom_xpos[g]-data.xpos[base]));indices.append(i)
    track=float(centers[0][1]-centers[1][1])
    if not .05<=track<=1.5 or abs(centers[0][0]-centers[1][0])>1e-6 or not .01<=radii[0]<=.5 or abs(radii[0]-radii[1])>1e-8:raise ValueError('Planar-drive geometry requires an aligned axle, equal radii and ordered left/right bindings')
    if not np.allclose(model.actuator_ctrlrange,[[-ch['max_torque_Nm'],ch['max_torque_Nm']] for ch in c['channels']]):raise ValueError('Declared per-channel torque limits must match native actuators')
    return model,qadr,vadr,base,tool,fq,{'wheel_radius_m':radii[0],'track_width_m':track,'signs':signs,'indices':indices,'geoms':wheels,'plane':plane[0]}

def references(row,c,r,geometry):
    nav={**c['drive'],**{k:geometry[k] for k in ('wheel_radius_m','track_width_m')},'goal_xy_m':r['base_goal_xy_m']}
    speeds=mobile.wheel_reference(row['x_m'],row['y_m'],row['yaw_rad'],nav);velocity={i:s*v for i,s,v in zip(geometry['indices'],geometry['signs'],speeds)}
    return [velocity[i] if ch['mode']=='velocity' else ch['target_rad']*min(1,row['time_s']/ch['ramp_s']) for i,ch in enumerate(c['channels'])]

def commands(row,c,r,geometry):
    refs=references(row,c,r,geometry);values=[]
    for i,(ch,ref) in enumerate(zip(c['channels'],refs),1):
        raw=ch['kv']*(ref-row[f'dq{i}_rad_s']) if ch['mode']=='velocity' else ch['kp']*(ref-row[f'q{i}_rad'])-ch['kd']*row[f'dq{i}_rad_s']
        values.append(max(-ch['max_torque_Nm'],min(ch['max_torque_Nm'],raw)))
    return refs,values

def measurements(rows,r):
    a,b=rows[0],rows[-1];dt=b['time_s']-rows[-2]['time_s']
    return {'base_error_m':math.hypot(b['x_m']-r['base_goal_xy_m'][0],b['y_m']-r['base_goal_xy_m'][1]),
        'tool_error_m':math.dist([b[k] for k in ('tool_x_m','tool_y_m','tool_z_m')],r['tool_goal_xyz_m']),
        'terminal_speed_m_s':math.hypot(b['x_m']-rows[-2]['x_m'],b['y_m']-rows[-2]['y_m'])/dt,
        'maximum_base_tilt_rad':max(row['tilt_rad'] for row in rows),
        'tool_relative_motion_m':math.dist([b[k] for k in ('local_x_m','local_y_m','local_z_m')],[a[k] for k in ('local_x_m','local_y_m','local_z_m')]),
        'minimum_drive_contact_fraction':min(sum(row[f'drive{i}_load_N']>1e-6 for row in rows)/len(rows) for i in (1,2))}

def passed(m,r,warnings):
    return not warnings and m['base_error_m']<=r['maximum_base_error_m'] and m['tool_error_m']<=r['maximum_tool_error_m'] and m['terminal_speed_m_s']<=r['maximum_terminal_speed_m_s'] and m['maximum_base_tilt_rad']<=r['maximum_base_tilt_rad'] and m['tool_relative_motion_m']>=r['minimum_tool_motion_m'] and m['minimum_drive_contact_fraction']>=r['minimum_drive_contact_fraction']

def run(folder):
    import mujoco,numpy as np
    xml,c,r=validate(folder);model,qadr,vadr,base,tool,fq,geometry=model_geometry(xml,c,r);data=mujoco.MjData(model);mujoco.mj_forward(model,data);n=len(qadr);rows=[]
    dt=float(model.opt.timestep);steps=math.ceil(r['duration_s']/dt)
    def sample():
        xyz=data.xpos[base];quat=data.xquat[base];rotation=data.xmat[base].reshape(3,3);point=data.xpos[tool]+data.xmat[tool].reshape(3,3)@np.array(c['observation']['point_m']);local=rotation.T@(point-xyz)
        qw,qx,qy,qz=quat;row=dict(time_s=float(data.time),x_m=float(xyz[0]),y_m=float(xyz[1]),z_m=float(xyz[2]),qw=float(qw),qx=float(qx),qy=float(qy),qz=float(qz),yaw_rad=math.atan2(2*(qw*qz+qx*qy),1-2*(qy*qy+qz*qz)),tilt_rad=math.acos(float(np.clip(rotation[2,2],-1,1))))
        for prefix,values in [('q',data.qpos[qadr]),('dq',data.qvel[vadr]),('command',data.ctrl),('torque',data.actuator_force)]:
            unit={'q':'rad','dq':'rad_s','command':'Nm','torque':'Nm'}[prefix]
            row.update({f'{prefix}{i}_{unit}':float(v) for i,v in enumerate(values,1)})
        row.update(dict(zip(('tool_x_m','tool_y_m','tool_z_m','local_x_m','local_y_m','local_z_m'),map(float,[*point,*local]))))
        loads=[0.,0.]
        for j,contact in enumerate(data.contact):
            pair=list(map(int,contact.geom))
            if geometry['plane'] in pair:
                for i,g in enumerate(geometry['geoms']):
                    if g in pair:
                        force=np.zeros(6);mujoco.mj_contactForce(model,data,j,force);loads[i]+=max(0.,float(force[0]))
        row.update({f'drive{i}_load_N':v for i,v in enumerate(loads,1)});row.update({f'ref{i}':float(v) for i,v in enumerate(references(row,c,r,geometry),1)})
        if not all(math.isfinite(v) for v in row.values()):raise RuntimeError('Nonfinite native state')
        rows.append(row)
    sample()
    for step in range(steps):
        _,data.ctrl[:]=commands(rows[-1],c,r,geometry);mujoco.mj_step(model,data);mujoco.mj_forward(model,data)
        if abs(data.time-(step+1)*dt)>1e-8:raise RuntimeError('Native solver reset/time mismatch')
        sample()
    warnings={str(i):int(w.number) for i,w in enumerate(data.warning) if w.number};m=measurements(rows,r);ok=passed(m,r,warnings)
    with (folder/'trace.csv').open('w',newline='',encoding='utf-8') as f:w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
    result={'adapter':ADAPTER,'solver':{'name':'MuJoCo','version':mujoco.__version__},'steps':steps,'timestep_s':dt,'duration_s':float(data.time),'measured':m,'criteria':r,'warnings':warnings,'passed':ok,'geometry':{k:geometry[k] for k in ('wheel_radius_m','track_width_m','signs','indices')},
        'levels':{'artifact':'GENERATED','static':'PASSED','simulation':'COMPLETED','requirements':'PASSED' if ok else 'FAILED','manufacturing':'NOT_REVIEWED','physical':'NOT_TESTED'},
        'limitations':['One coupled native rigid-body model; ideal torque drives/encoders/world pose, no hardware, battery or localization qualification','One two-axis equal-radius planar drive plus serial point-observed position channels; not universal control, terrain, planning, IK or collision safety']}
    (folder/'result.json').write_text(json.dumps(result,indent=2,allow_nan=False));print(json.dumps({'requirements':result['levels']['requirements']}),flush=True)

def verify(folder,result):
    import mujoco,numpy as np
    xml,c,r=validate(folder);model,qadr,vadr,base,tool,fq,geometry=model_geometry(xml,c,r)
    with (folder/'trace.csv').open() as f:rows=[{k:float(v) for k,v in row.items()} for row in csv.DictReader(f)]
    steps=math.ceil(r['duration_s']/model.opt.timestep)
    if result['adapter']!=ADAPTER or result['steps']!=steps or len(rows)!=steps+1 or abs(result['duration_s']-steps*model.opt.timestep)>1e-8 or result['criteria']!=r:raise ValueError('Full task horizon/criteria mismatch')
    data=mujoco.MjData(model)
    expected_geometry={k:geometry[k] for k in ('wheel_radius_m','track_width_m','signs','indices')}
    if result.get('geometry')!=expected_geometry or abs(result['timestep_s']-float(model.opt.timestep))>1e-12:raise ValueError('Native control geometry/timestep mismatch')
    for i,row in enumerate(rows):
        if not all(math.isfinite(v) for v in row.values()) or abs(row['time_s']-i*model.opt.timestep)>1e-8:raise ValueError('Nonfinite/incomplete native time lineage')
        data.qpos[qadr]=[row[f'q{k}_rad'] for k in range(1,len(qadr)+1)];data.qpos[fq:fq+7]=[row[k] for k in ('x_m','y_m','z_m','qw','qx','qy','qz')];mujoco.mj_forward(model,data)
        qw,qx,qy,qz=[row[k] for k in ('qw','qx','qy','qz')];yaw=math.atan2(2*(qw*qz+qx*qy),1-2*(qy*qy+qz*qz));tilt=math.acos(float(np.clip(data.xmat[base].reshape(3,3)[2,2],-1,1)))
        if abs(qw*qw+qx*qx+qy*qy+qz*qz-1)>1e-8 or abs(yaw-row['yaw_rad'])>1e-8 or abs(tilt-row['tilt_rad'])>1e-8:raise ValueError('Native quaternion/yaw/tilt mismatch')
        point=data.xpos[tool]+data.xmat[tool].reshape(3,3)@np.array(c['observation']['point_m']);local=data.xmat[base].reshape(3,3).T@(point-data.xpos[base])
        if not np.allclose([*point,*local],[row[k] for k in ('tool_x_m','tool_y_m','tool_z_m','local_x_m','local_y_m','local_z_m')],atol=1e-9,rtol=0):raise ValueError('Observed body point differs from independent native kinematics')
        refs=references(row,c,r,geometry)
        if any(abs(v-row[f'ref{k}'])>1e-9 for k,v in enumerate(refs,1)):raise ValueError('Reference differs from approved feedback/position binding')
        for k,ch in enumerate(c['channels'],1):
            if abs(row[f'torque{k}_Nm'])>ch['max_torque_Nm']+1e-9:raise ValueError('Native torque bound violated')
        if i:
            _,cmd=commands(rows[i-1],c,r,geometry)
            if any(abs(v-row[f'command{k}_Nm'])>1e-8 for k,v in enumerate(cmd,1)):raise ValueError('Independent mixed-channel command mismatch')
    m=measurements(rows,r)
    ok=passed(m,r,result['warnings'])
    if any(abs(v-result['measured'].get(k,float('inf')))>1e-10 for k,v in m.items()) or result['passed']!=ok or result['levels']['requirements']!=('PASSED' if ok else 'FAILED'):raise ValueError('Independent composed task verdict mismatch')
