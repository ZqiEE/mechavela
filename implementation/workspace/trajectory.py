"""Bounded data-only joint keyframes, tracked by existing native MuJoCo PD.

Generic motion input, not an automatic walking-policy generator. No project
Python, expressions or callbacks execute. Existing static PD stays schema1.
"""
import bisect,math,xml.etree.ElementTree as ET
from .projects import read
from . import jointspace

ADAPTER='mujoco.joint-trajectory/1'
KIND='joint_trajectory_pd/1'

def validate(folder):
    c=read(folder/'controller.json');r=read(folder/'requirements.json')
    fields={'kind','joint_names','kp','kd','keyframes'}
    if not isinstance(c,dict) or not fields<=set(c)<=fields|{'support_geoms'} or c['kind']!=KIND:raise ValueError('Invalid joint trajectory controller schema')
    frames=c['keyframes'];names=c['joint_names']
    if not isinstance(names,list) or not isinstance(frames,list) or not 2<=len(frames)<=256:raise ValueError('2..256 bounded keyframes required')
    previous=-1
    for frame in frames:
        if not isinstance(frame,dict) or set(frame)!={'time_s','target_rad'}:raise ValueError('Invalid keyframe fields')
        t=frame['time_s'];q=frame['target_rad']
        if not jointspace.finite(t,0,5) or t<=previous:raise ValueError('Keyframe times must increase strictly within0..5s')
        if not isinstance(q,list) or len(q)!=len(names) or not all(jointspace.finite(x,-1.5,1.5) for x in q):raise ValueError('One bounded angle per named hinge required')
        previous=t
    if frames[0]['time_s']!=0:raise ValueError('Trajectory must start at time zero')
    metrics=('final_joint_error_rad','forward_distance_m')
    if not isinstance(r,dict) or r.get('metric') not in metrics:raise ValueError('Unsupported trajectory acceptance metric')
    if 'target_rad' in r:raise ValueError('Trajectory targets belong in keyframes; static target criteria cannot be silently reused')
    shim={**r,'metric':'final_joint_error_rad'}
    pd={k:c[k] for k in ('joint_names','kp','kd')};pd.update(kind='joint_space_pd/1',target_rad=frames[-1]['target_rad'])
    xml,_,_=jointspace.validate(folder,pd,shim)
    supports=c.get('support_geoms',[])
    if not isinstance(supports,list) or len(supports)>8 or any(not isinstance(n,str) for n in supports) or len(set(supports))!=len(supports):raise ValueError('At most8 distinct support geometry names required')
    geoms={g.get('name'):g for g in ET.fromstring(xml).iter('geom')}
    if any(name not in geoms or geoms[name].get('type')!='sphere' for name in supports):raise ValueError('Support tracing currently requires named spherical feet')
    if frames[-1]['time_s']>r['duration_s']:raise ValueError('Trajectory exceeds approved duration')
    if r['metric']=='forward_distance_m':
        if not 2<=len(supports)<=8:raise ValueError('Locomotion requires2..8 explicitly named support feet')
        if not list(ET.fromstring(xml).iter('freejoint')):raise ValueError('Locomotion requires an unactuated free base')
        for key,lo,hi in [('minimum_forward_distance_m',.001,2),('maximum_lateral_drift_m',0,1),('minimum_base_height_m',.01,2),('maximum_base_tilt_rad',0,1.57)]:
            if not jointspace.finite(r.get(key),lo,hi):raise ValueError('Locomotion requires bounded progress, lateral, height and tilt criteria')
        if type(r.get('minimum_support_cycles')) is not int or not 1<=r['minimum_support_cycles']<=10 or not jointspace.finite(r.get('minimum_swing_clearance_m'),.001,.1):raise ValueError('Locomotion requires real loaded/unloaded support cycles and swing clearance')
    return xml,c,r

def support_sampler(model,c):
    """Native contact force and sphere-to-plane clearance, not guessed stance."""
    import mujoco,numpy as np
    names=c.get('support_geoms',[])
    if not names:return None,[]
    ids=[mujoco.mj_name2id(model,mujoco.mjtObj.mjOBJ_GEOM,n) for n in names]
    planes=[i for i,t in enumerate(model.geom_type) if t==mujoco.mjtGeom.mjGEOM_PLANE]
    if len(planes)!=1:raise ValueError('Support tracing requires one flat ground plane')
    plane=planes[0]
    if model.geom_bodyid[plane]!=0 or not np.allclose(model.geom_quat[plane],[1,0,0,0]):raise ValueError('Support tracing requires a horizontal world plane')
    if any(i<0 or model.geom_bodyid[i]==0 for i in ids):raise ValueError('Support geometry must belong to the robot')
    floor=float(model.geom_pos[plane,2]);radii=model.geom_size[ids,0]
    headers=[name for i in range(len(ids)) for name in (f'support{i+1}_load_N',f'support{i+1}_clearance_m')]
    def sample(data):
        loads={g:0. for g in ids}
        for j,contact in enumerate(data.contact):
            pair=list(map(int,contact.geom))
            if plane in pair:
                foot=pair[1] if pair[0]==plane else pair[0]
                if foot in loads:
                    wrench=np.zeros(6);mujoco.mj_contactForce(model,data,j,wrench);loads[foot]+=max(0.,float(wrench[0]))
        return [v for i,g in enumerate(ids) for v in (loads[g],float(data.geom_xpos[g,2])-float(radii[i])-floor)]
    return sample,headers

def reference(c,time):
    frames=c['keyframes'];times=[f['time_s'] for f in frames]
    i=bisect.bisect_right(times,time)-1
    if i<0:return frames[0]['target_rad']
    if i>=len(frames)-1:return frames[-1]['target_rad']
    a,b=frames[i:i+2];u=(time-a['time_s'])/(b['time_s']-a['time_s'])
    return [x+(y-x)*u for x,y in zip(a['target_rad'],b['target_rad'])]

def measurements(rows,c,req):
    first,last=rows[0],rows[-1]
    measured={'forward_distance_m':last['x_m']-first['x_m'],
            'maximum_lateral_drift_m':max(abs(row['y_m']-first['y_m']) for row in rows)}
    cycles=[];clearances=[]
    for i in range(1,len(c.get('support_geoms',[]))+1):
        loaded=False;air=False;peak=0.;count=0;best=0.
        for row in rows:
            load=row[f'support{i}_load_N'];height=row[f'support{i}_clearance_m']
            if load<0:raise ValueError('Negative support load')
            if load>1e-6:
                if air:
                    if peak>=req.get('minimum_swing_clearance_m',.001):count+=1;best=max(best,peak)
                    air=False;peak=0.
                loaded=True
            elif loaded:
                air=True;peak=max(peak,height)
        cycles.append(count);clearances.append(best)
    if cycles:measured.update(minimum_support_cycles=min(cycles),minimum_swing_clearance_m=min(clearances))
    return measured

def passed(m,r,warnings):
    return (jointspace.passed(m,r,warnings) and (r['metric']!='forward_distance_m' or
            (m['forward_distance_m']>=r['minimum_forward_distance_m'] and m['maximum_lateral_drift_m']<=r['maximum_lateral_drift_m']
             and m['minimum_support_cycles']>=r['minimum_support_cycles'] and m['minimum_swing_clearance_m']>=r['minimum_swing_clearance_m'])))

def verify_reference(rows,c,xml):
    for row in rows:
        if any(abs(row.get(f'target{i+1}_rad',math.inf)-q)>1e-10 for i,q in enumerate(reference(c,row['time_s']))):raise ValueError('Trace target differs from frozen keyframe interpolation')
    # Commands applied during step i are recorded in row i+1, independently
    # recomputed from the state/reference in row i and declared torque limits.
    bounds=[list(map(float,m.get('ctrlrange','').split())) for m in ET.fromstring(xml).iter('motor')]
    if len(bounds)!=len(c['joint_names']) or any(len(b)!=2 for b in bounds):raise ValueError('Invalid motor bounds in trajectory evidence')
    for before,after in zip(rows,rows[1:]):
        target=reference(c,before['time_s'])
        for i,q in enumerate(target,1):
            raw=c['kp']*(q-before[f'q{i}_rad'])-c['kd']*before[f'dq{i}_rad_s']
            command=after[f'command{i}_Nm']
            expected=max(bounds[i-1][0],min(bounds[i-1][1],raw))
            if abs(command-expected)>1e-8:raise ValueError('PD command inconsistent with frozen reference/state')

def run(folder):
    import sys
    return jointspace.run(folder,sys.modules[__name__])

def verify(folder,result):
    import sys
    xml,_,req=validate(folder)
    dt=float(ET.fromstring(xml).find('option').get('timestep'))
    steps=math.ceil(req['duration_s']/dt)
    if result['adapter']!=ADAPTER or result['steps']!=steps or abs(result['timestep_s']-dt)>1e-12 or abs(result['duration_s']-steps*dt)>1e-8:raise ValueError('Trajectory duration differs from frozen criterion/model')
    return jointspace.verify(folder,result,sys.modules[__name__])
