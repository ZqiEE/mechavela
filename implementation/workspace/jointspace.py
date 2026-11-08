"""Bounded named-hinge PD on native MuJoCo, including an unactuated floating base.

No morphology-specific dynamics, gait, imported code or inverse kinematics. Full
step traces permit independent goal/standing checks rather than trusting a UI.
"""
import csv,json,math,re,xml.etree.ElementTree as ET
from .projects import read

ADAPTER='mujoco.joint-space/1'

def finite(v,lo,hi):return type(v) in (int,float) and math.isfinite(v) and lo<=v<=hi

def validate(folder,controller=None,requirements=None):
    xml=(folder/'robot.xml').read_text(encoding='utf-8');c=read(folder/'controller.json') if controller is None else controller;r=read(folder/'requirements.json') if requirements is None else requirements
    if len(xml)>100000 or '<!' in xml:raise ValueError('Self-contained bounded primitive MJCF required')
    root=ET.fromstring(xml)
    allowed={'mujoco','compiler','option','worldbody','body','geom','joint','freejoint','site','actuator','motor','sensor','jointpos','jointvel'}
    if root.tag!='mujoco' or len(list(root.iter()))>256 or any(e.tag not in allowed or any(k in e.attrib for k in ('file','plugin','mesh','hfield','texture')) for e in root.iter()):raise ValueError('Only bounded self-contained primitive MJCF; no files/plugins/includes')
    if not isinstance(c,dict) or set(c)!={'kind','joint_names','target_rad','kp','kd'} or c['kind']!='joint_space_pd/1':raise ValueError('Invalid joint-space controller schema')
    names=c['joint_names'];target=c['target_rad']
    if not isinstance(names,list) or not 1<=len(names)<=24 or any(not isinstance(n,str) or not re.fullmatch('[A-Za-z][A-Za-z0-9_]{0,47}',n) for n in names) or len(set(names))!=len(names):raise ValueError('1..24 unique named hinge joints required')
    if not isinstance(target,list) or len(target)!=len(names) or not all(finite(x,-1.5,1.5) for x in target):raise ValueError('One bounded target per named hinge required')
    if not finite(c['kp'],0,200) or not finite(c['kd'],0,30):raise ValueError('Bounded PD gains required')
    if any(j.get('type','hinge')!='hinge' for j in root.iter('joint')) or [j.get('name') for j in root.iter('joint')]!=names:raise ValueError('Named hinge order must match the actual kinematic tree')
    if len(list(root.iter('freejoint')))>1:raise ValueError('At most one unactuated free base supported')
    if not isinstance(r,dict) or r.get('metric')!='final_joint_error_rad' or not finite(r.get('maximum'),.00001,.5) or not finite(r.get('duration_s'),.1,5):raise ValueError('Bounded joint error/duration criterion required')
    if 'target_rad' in r and r['target_rad']!=target:raise ValueError('Controller target differs from owner-approved criterion')
    for key,lo,hi in [('minimum_base_height_m',.01,2),('maximum_base_tilt_rad',0,1.57)]:
        if key in r and (not finite(r[key],lo,hi) or not list(root.iter('freejoint'))):raise ValueError('Floating-base criterion requires a real free base and bounded limit')
    return xml,c,r

def _model(xml,c):
    import mujoco,numpy as np
    model=mujoco.MjModel.from_xml_string(xml);n=len(c['joint_names'])
    hinges=[i for i,t in enumerate(model.jnt_type) if t==mujoco.mjtJoint.mjJNT_HINGE]
    free=[i for i,t in enumerate(model.jnt_type) if t==mujoco.mjtJoint.mjJNT_FREE]
    if len(hinges)!=n or len(free)>1 or model.nq!=n+7*len(free) or model.nv!=n+6*len(free) or model.nu!=n:raise ValueError('Only the declared hinges plus optional unactuated free base are supported')
    if model.nbody>40 or model.ngeom>80 or not .001<=model.opt.timestep<=.01:raise ValueError('Body/geometry/timestep bounds exceeded')
    if not np.all(np.isfinite(model.opt.gravity)) or np.max(np.abs(model.opt.gravity))>20:raise ValueError('Gravity outside supported envelope')
    if not np.all(model.actuator_trntype==mujoco.mjtTrn.mjTRN_JOINT) or model.actuator_trnid[:,0].tolist()!=hinges:raise ValueError('One ordered scalar motor transmission per named hinge required')
    if (not np.all(model.actuator_gaintype==mujoco.mjtGain.mjGAIN_FIXED) or not np.all(model.actuator_biastype==mujoco.mjtBias.mjBIAS_NONE)
        or not np.all(model.actuator_dyntype==mujoco.mjtDyn.mjDYN_NONE) or not np.allclose(model.actuator_gainprm[:,0],1)
        or not np.allclose(model.actuator_gear[:,0],1) or not np.allclose(model.actuator_gear[:,1:],0)):
        raise ValueError('Only unit-gain ideal torque motors supported; no implicit force/gear substitution')
    bounds=model.actuator_ctrlrange
    if not np.all(model.actuator_ctrllimited) or not np.all(np.isfinite(bounds)) or np.any(np.abs(bounds)>20) or np.any(bounds[:,0]>=0) or np.any(bounds[:,1]<=0):raise ValueError('Finite enabled ideal motor limits within +/-20Nm required')
    return model,np.array(model.jnt_qposadr[hinges]),np.array(model.jnt_dofadr[hinges]),int(model.jnt_bodyid[free[0]]) if free else None

def measurements(rows,n,target):
    final=rows[-1]
    return {'final_joint_error_rad':max(abs(final[f'q{i+1}_rad']-v) for i,v in enumerate(target)),
        'peak_command_Nm':max(abs(row[f'command{i+1}_Nm']) for row in rows for i in range(n)),
        'minimum_base_height_m':min(row['z_m'] for row in rows),'maximum_base_tilt_rad':max(row['tilt_rad'] for row in rows),
        'contact_samples':sum(row['contacts']>0 for row in rows)}

def passed(m,req,warnings):
    return (not warnings and m['final_joint_error_rad']<=req['maximum']
        and ('minimum_base_height_m' not in req or (m['minimum_base_height_m']>=req['minimum_base_height_m'] and m['contact_samples']>0))
        and ('maximum_base_tilt_rad' not in req or m['maximum_base_tilt_rad']<=req['maximum_base_tilt_rad']))

def run(folder,trajectory=None):
    import mujoco,numpy as np
    xml,c,req=(trajectory.validate(folder) if trajectory else validate(folder));model,qadr,vadr,base=_model(xml,c);data=mujoco.MjData(model);mujoco.mj_forward(model,data)
    n=len(qadr);reference=lambda t:trajectory.reference(c,t) if trajectory else c['target_rad'];steps=math.ceil(req['duration_s']/model.opt.timestep)
    support_sample,support_headers=trajectory.support_sampler(model,c) if trajectory else (None,[])
    rows=[]
    def sample():
        xyz=data.xpos[base].tolist() if base is not None else [0.,0.,0.]
        quat=data.xquat[base].tolist() if base is not None else [1.,0.,0.,0.]
        tilt=math.acos(float(np.clip(data.xmat[base].reshape(3,3)[2,2],-1,1))) if base is not None else 0.
        values=[float(data.time),*data.qpos[qadr],*data.qvel[vadr],*data.ctrl,*data.actuator_force,*xyz,*quat,tilt,int(data.ncon)]
        if trajectory:values.extend(reference(float(data.time)))
        if support_sample:values.extend(support_sample(data))
        if not all(math.isfinite(v) for v in values):raise RuntimeError('Nonfinite native state')
        rows.append(dict(zip(headers,map(float,values))))
    headers=['time_s']+[f'q{i+1}_rad' for i in range(n)]+[f'dq{i+1}_rad_s' for i in range(n)]+[f'command{i+1}_Nm' for i in range(n)]+[f'torque{i+1}_Nm' for i in range(n)]+['x_m','y_m','z_m','qw','qx','qy','qz','tilt_rad','contacts']
    if trajectory:headers += [f'target{i+1}_rad' for i in range(n)]
    headers += support_headers
    def contacts():
        return [{'geoms':[mujoco.mj_id2name(model,mujoco.mjtObj.mjOBJ_GEOM,int(g)) for g in contact.geom],
                 'distance_m':float(contact.dist)} for contact in list(data.contact)[:64]]
    initial_contacts=contacts()
    sample()
    for i in range(steps):
        target=np.array(reference(float(data.time)))
        data.ctrl[:]=np.clip(c['kp']*(target-data.qpos[qadr])-c['kd']*data.qvel[vadr],model.actuator_ctrlrange[:,0],model.actuator_ctrlrange[:,1])
        mujoco.mj_step(model,data);mujoco.mj_forward(model,data)
        if data.time<(i+1)*model.opt.timestep-1e-8:raise RuntimeError('Native solver reset')
        sample()
    warnings={str(i):int(w.number) for i,w in enumerate(data.warning) if w.number}
    m=measurements(rows,n,reference(rows[-1]['time_s']))
    if trajectory:m.update(trajectory.measurements(rows,c,req))
    ok=trajectory.passed(m,req,warnings) if trajectory else passed(m,req,warnings)
    with (folder/'trace.csv').open('w',newline='',encoding='utf-8') as f:
        writer=csv.DictWriter(f,fieldnames=headers);writer.writeheader();writer.writerows(rows)
    (folder/'contacts.json').write_text(json.dumps({'initial':initial_contacts,'final':contacts(),
        'scope':'Native contact pair/distance snapshots only; no force or convergence qualification'},indent=2),encoding='utf-8')
    result={'adapter':trajectory.ADAPTER if trajectory else ADAPTER,'solver':{'name':'MuJoCo','version':mujoco.__version__},'joint_names':c['joint_names'],
        'steps':steps,'duration_s':float(data.time),'timestep_s':float(model.opt.timestep),'floating_base':base is not None,
        'gravity_m_s2':model.opt.gravity.tolist(),'measured':m,'criteria':req,'warnings':warnings,'passed':ok,
        'levels':{'proposal':'RECORDED','artifact':'GENERATED','static':'PASSED','simulation':'COMPLETED','requirements':'PASSED' if ok else 'FAILED','manufacturing':'NOT_REVIEWED','physical':'NOT_TESTED'},
        'limitations':['Bounded named-hinge PD with ideal torque motors/encoders; not motor/electrical qualification',
            ('User-defined flat-plane keyframes and support observations require separate gait/horizon qualification; no terrain robustness or contact convergence' if trajectory else 'Floating-base posture/contact smoke is not validated locomotion, terrain robustness or contact convergence'),
            'Joint-space positioning is not inverse kinematics, planning, collision safety or hardware certification']}
    (folder/'result.json').write_text(json.dumps(result,indent=2,allow_nan=False),encoding='utf-8')
    print(json.dumps({'steps':steps,'requirements':result['levels']['requirements']}),flush=True)

def verify(folder,result,trajectory=None):
    xml,c,req=trajectory.validate(folder) if trajectory else validate(folder);n=len(c['joint_names'])
    with (folder/'trace.csv').open(encoding='utf-8') as f:rows=[{k:float(v) for k,v in row.items()} for row in csv.DictReader(f)]
    if len(rows)!=result['steps']+1 or rows[0]['time_s']!=0 or not all(math.isfinite(v) for row in rows for v in row.values()):raise ValueError('Incomplete or nonfinite full-step trace')
    if any(abs(row[f'command{i+1}_Nm'])>20+1e-10 or abs(row[f'torque{i+1}_Nm'])>20+1e-10 for row in rows for i in range(n)):raise ValueError('Trace motor torque bounds violated')
    if any(abs(row['time_s']-i*result['timestep_s'])>1e-8 for i,row in enumerate(rows)):raise ValueError('Native time lineage differs from full-step trace')
    if trajectory:trajectory.verify_reference(rows,c,xml)
    m=measurements(rows,n,trajectory.reference(c,rows[-1]['time_s']) if trajectory else c['target_rad'])
    if trajectory:m.update(trajectory.measurements(rows,c,req))
    if any(abs(m[k]-result['measured'].get(k,float('inf')))>1e-10 for k in m) or result['criteria']!=req or result['joint_names']!=c['joint_names']:raise ValueError('Independent joint/base measurement or criterion mismatch')
    expected=trajectory.passed(m,req,result['warnings']) if trajectory else passed(m,req,result['warnings'])
    if result['passed']!=expected or result['levels']['requirements']!=('PASSED' if expected else 'FAILED'):raise ValueError('Independent requirement verdict mismatch')
