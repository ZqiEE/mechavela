"""Owner-frozen motion goals, model-authored geometry and cyclic keyframe data.

No robot template, inverse-kinematics answer, gait or solver is supplied here.
Periodic data expansion is morphology-neutral; all dynamics remain MuJoCo.
"""
import csv,json,math,tempfile
from pathlib import Path
from .build import compile_design,GENERAL_VERSION,number,authoring_guide
from .projects import encode,digest
from . import trajectory

SCHEMA='rlsok.motion-authoring/1'
BOUNDS={'maximum':(.00001,.5),'duration_s':(.1,5),'minimum_forward_distance_m':(.001,2),
        'maximum_lateral_drift_m':(0,1),'minimum_base_height_m':(.01,2),'maximum_base_tilt_rad':(0,1.57),
        'minimum_swing_clearance_m':(.001,.1)}

def prepare(store,pid,criteria,joints,supports,expected):
    """Owner action only; no geometry or motion data is inserted."""
    if type(joints) is not int or not 1<=joints<=24 or type(supports) is not int or not 2<=supports<=8:raise ValueError('Bounded joint and support counts required')
    if not isinstance(criteria,dict) or set(criteria)!=set(BOUNDS)|{'minimum_support_cycles'}:raise ValueError('Explicit complete locomotion criteria required')
    r={k:number(criteria[k],*v) for k,v in BOUNDS.items()}
    if type(criteria['minimum_support_cycles']) is not int or not 1<=criteria['minimum_support_cycles']<=10:raise ValueError('1..10 complete support cycles required')
    r.update(metric='forward_distance_m',minimum_support_cycles=criteria['minimum_support_cycles'],authoring_mode=SCHEMA)
    with store.lock:
        p=store.get(pid)
        if p['revision']!=expected:raise ValueError('Revision conflict: reload before preparing motion brief')
        if set(p['assets'])-{'requirements.md','notes.md'}:raise ValueError('Prepare motion authoring only in a blank project; native goals are preserved')
        brief={'schema':SCHEMA,'joint_count':joints,'support_count':supports,'require_free_base':True,'intent':p['intent']}
        return {'revision':store.revise(pid,{'requirements.json':encode(r).decode(),'motion-brief.json':encode(brief).decode(),
            'build-brief.md':p['intent']+'\n\nOwner-frozen locomotion criteria are in requirements.json and structure counts in motion-brief.json. No geometry, gait or model request was inserted. Design your own assembly and cyclic joint reference with build_motion. Run real physics, inspect failures, revise, repeat and export; do not change acceptance.\n'},'Owner approved blank motion-design brief; no template or inference',expected)}

def expand(motion,n,duration):
    fields={'period_s','start_s','end_s','initial_rad','cycle'}
    if not isinstance(motion,dict) or set(motion)!=fields:raise ValueError('Invalid repeated-keyframe data fields')
    period=number(motion['period_s'],.2,5);start=number(motion['start_s'],.1,duration);end=number(motion['end_s'],start,duration)
    cycle=motion['cycle'];initial=motion['initial_rad']
    def angles(q):
        if not isinstance(q,list) or len(q)!=n:raise ValueError('One angle per declared hinge required')
        return [number(v,-1.5,1.5) for v in q]
    initial=angles(initial)
    if not isinstance(cycle,list) or not 3<=len(cycle)<=32:raise ValueError('3..32 cycle points including equal phase0/1 endpoints required')
    points=[];last=-1
    for item in cycle:
        if not isinstance(item,dict) or set(item)!={'phase','target_rad'}:raise ValueError('Cycle point must contain phase and target_rad')
        phase=number(item['phase'],0,1)
        if phase<=last:raise ValueError('Cycle phases must strictly increase')
        points.append((phase,angles(item['target_rad'])));last=phase
    if points[0][0]!=0 or points[-1][0]!=1 or points[0][1]!=points[-1][1]:raise ValueError('Cycle must close continuously at phases0/1')
    from bisect import bisect_right
    def at(time):
        phase=((time-start)/period)%1;i=max(0,bisect_right([v[0] for v in points],phase)-1);a,b=points[i:i+2];u=(phase-a[0])/(b[0]-a[0])
        return [x+(y-x)*u for x,y in zip(a[1],b[1])]
    frames=[{'time_s':0.,'target_rad':initial},{'time_s':start,'target_rad':points[0][1]}]
    for k in range(math.ceil((end-start)/period)+1):
        for phase,q in points[1:]:
            t=start+(k+phase)*period
            if t<end-1e-9:frames.append({'time_s':t,'target_rad':q})
    if end>start:frames.append({'time_s':end,'target_rad':at(end)})
    if duration>end:frames.append({'time_s':duration,'target_rad':frames[-1]['target_rad']})
    if len(frames)>256:raise ValueError('Expanded trajectory exceeds256 frames')
    return frames

def guide():
    return {'schema':SCHEMA,'design_fields':['assembly','motion','kp','kd','support_geoms'],
        'assembly':authoring_guide(),'assembly_note':'Use primitive-builder/2, enabled=true. Its control.target_rad must equal motion.initial_rad; control.kp/kd must equal top-level kp/kd. Its criteria must match maximum/duration/height/tilt from owner requirements. No template is provided.',
        'motion_fields':{'period_s':'.2..5','start_s':'.1..approved duration; initial posture transition ends here','end_s':'start_s..approved duration; hold last value afterwards','initial_rad':'one angle per joint, -1.5..1.5','cycle':'3..32 {phase:0..1,target_rad:[one angle per joint]}; phases strictly increasing with identical vectors at phase0/1. Interpolation is linear; repeats until end_s.'},
        'kp':'0..200','kd':'0..30','support_geoms':'Owner-count named sphere feet from YOUR assembly, generated as body_id+_solid',
        'revision_rules':'Goals and counts remain owner-owned. First build establishes ordered joint names and support names; later builds cannot rename these. Actual generated native edits need owner review; build_motion cannot overwrite them.',
        'scope':'Data expansion only, not an auto gait/IK/controller answer. Choose your own geometry, foot motion, phasing, gains and declared rotor inertia; diagnose actual native failures.'}

def build(store,pid,serialized,expected):
    if not isinstance(serialized,str) or len(serialized.encode())>100000:raise ValueError('Bounded serialized motion design required')
    d=json.loads(serialized)
    if not isinstance(d,dict) or set(d)!={'assembly','motion','kp','kd','support_geoms'}:raise ValueError('Invalid motion design fields')
    with store.lock:
        p=store.get(pid)
        if p['revision']!=expected:raise ValueError('Revision conflict: inspect before building motion')
        if 'actuators.json' in p['assets']:raise ValueError('Component drive constraints cannot be replaced by ideal motors')
        if 'motion-brief.json' not in p['assets']:raise ValueError('Owner must approve a blank motion brief first')
        brief=json.loads(store.asset(pid,'motion-brief.json'));req=json.loads(store.asset(pid,'requirements.json'))
        if brief.get('schema')!=SCHEMA or req.get('authoring_mode')!=SCHEMA:raise ValueError('Owner motion capability missing')
        assembly=d['assembly']
        if not isinstance(assembly,dict) or assembly.get('schema')!=GENERAL_VERSION:raise ValueError('Motion assembly requires primitive-builder/2')
        if not isinstance(d['motion'],dict):raise ValueError('Motion must be a data object')
        required={k:req[k] for k in ('maximum','duration_s','minimum_base_height_m','maximum_base_tilt_rad')}
        if assembly.get('criteria')!=required:raise ValueError('AI cannot change owner acceptance criteria')
        files,manifest=compile_design(assembly)
        if assembly['bodies'][0]['joint']!='free' or any(b['parent']<0 for b in assembly['bodies'][1:]):raise ValueError('One connected robot rooted at the free base is required')
        static=json.loads(files['controller.json']);names=static.get('joint_names',[])
        if len(names)!=brief['joint_count'] or not manifest['capabilities']['floating_base']:raise ValueError('Assembly differs from owner structure counts/free-base capability')
        if assembly['control']['target_rad']!=d['motion'].get('initial_rad'):raise ValueError('Assembly initial targets must match motion initial angles')
        if any(assembly['control'][k]!=d[k] for k in ('kp','kd')):raise ValueError('Assembly and trajectory gains must agree')
        c={'kind':trajectory.KIND,'joint_names':names,'kp':number(d['kp'],0,200),'kd':number(d['kd'],0,30),
           'keyframes':expand(d['motion'],len(names),req['duration_s']),'support_geoms':d['support_geoms']}
        if not isinstance(c['support_geoms'],list) or len(c['support_geoms'])!=brief['support_count']:raise ValueError('Owner support count differs')
        if 'controller.json' in p['assets']:
            current=json.loads(store.asset(pid,'controller.json'))
            if current.get('kind')!=c['kind'] or current.get('joint_names')!=names or current.get('support_geoms')!=c['support_geoms']:raise ValueError('AI cannot rename established joints/supports or change controller capability')
        if 'build-manifest.json' in p['assets']:
            previous=json.loads(store.asset(pid,'build-manifest.json'))
            if any(p['assets'].get(n)!=previous['outputs'].get(n) for n in ('robot.xml','controller.json','components.json','BOM.csv','motion.json')):raise ValueError('Native files changed outside motion builder; owner review required')
        elif 'robot.xml' in p['assets']:raise ValueError('Existing native model requires owner review')
        files.update({'controller.json':encode(c).decode(),'motion.json':encode(d).decode(),'motion-generator.txt':Path(__file__).read_text(encoding='utf-8')})
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)
            for n in ('robot.xml','controller.json'):(folder/n).write_text(files[n],encoding='utf-8')
            (folder/'requirements.json').write_text(encode(req).decode(),encoding='utf-8');trajectory.validate(folder)
        manifest.update(generator=SCHEMA);manifest['capabilities'].update(named_hinge_PD_inputs=False,cyclic_joint_trajectory_inputs=True)
        manifest['outputs']={n:digest(t.encode()) for n,t in files.items() if n!='build-manifest.json'}
        files['build-manifest.json']=encode(manifest).decode()
        revision=store.revise(pid,files,'AI authored original geometry and cyclic motion; physics NOT_RUN',expected)
        return {'revision':revision,'files':list(files),'expanded_keyframes':len(c['keyframes']),'manifest':manifest}

def diagnostics(store,pid,rid):
    p=store.get(pid);run=next((r for r in p['runs'] if r['id']==rid),None)
    if not run or run['state']!='COMPLETED' or run['adapter']!=trajectory.ADAPTER:raise ValueError('A completed trajectory run is required')
    folder=store.path(pid)/'runs'/rid;path=folder/'trace.csv';raw=path.read_bytes()
    if digest(raw)!=run['outputs']['trace.csv']:raise ValueError('Native trace hash mismatch')
    rows=list(csv.DictReader(raw.decode().splitlines()));step=max(1,(len(rows)-1)//20)
    selected=rows[::step]
    if selected[-1]!=rows[-1]:selected.append(rows[-1])
    return {'run':rid,'trace_sha256':digest(raw),'source':'Actual recorded CSV; evenly spaced observations, not model-generated diagnosis',
        'measured':run['result']['measured'],'criteria':run['result']['criteria'],
        'samples':[{k:float(v) for k,v in row.items() if k in ('time_s','x_m','y_m','z_m','tilt_rad') or k.startswith(('support','q','target'))} for row in selected]}
