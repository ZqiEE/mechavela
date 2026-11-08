"""Owner-defined world tasks; original data-only geometry and named control blocks."""
import csv,json,tempfile,xml.etree.ElementTree as ET
from pathlib import Path
from .build import compile_design,SYSTEM_VERSION,authoring_guide
from .projects import encode,digest
from . import control_graph as graph
SCHEMA='rlsok.system-authoring/1'

def prepare(store,pid,criteria,position_count,velocity_count,expected):
    if type(position_count) is not int or not 1<=position_count<=22 or type(velocity_count) is not int or velocity_count!=2:raise ValueError('1..22 position channels and two planar velocity channels required')
    if not isinstance(criteria,dict) or 'metric' in criteria or 'authoring_mode' in criteria:raise ValueError('Explicit owner task fields required')
    req={**criteria,'metric':'composed_task/1','authoring_mode':SCHEMA}
    # Same numerical requirement validator as the worker, without inventing a model.
    required={'duration_s','base_goal_xy_m','maximum_base_error_m','tool_goal_xyz_m','maximum_tool_error_m','maximum_terminal_speed_m_s','maximum_base_tilt_rad','minimum_tool_motion_m','minimum_tool_body_mass_kg','minimum_drive_contact_fraction'}
    if set(criteria)!=required:raise ValueError('All composed world/task criteria must be supplied')
    from .build import number,vector
    limits={'duration_s':(.1,5),'maximum_base_error_m':(.001,.2),'maximum_tool_error_m':(.001,.2),'maximum_terminal_speed_m_s':(.001,.5),'maximum_base_tilt_rad':(.01,.5),'minimum_tool_motion_m':(.01,1),'minimum_tool_body_mass_kg':(.01,2),'minimum_drive_contact_fraction':(.1,1)}
    for k,b in limits.items():number(req[k],*b)
    if not isinstance(req['base_goal_xy_m'],list) or len(req['base_goal_xy_m'])!=2:raise ValueError('World XY goal required')
    for v in req['base_goal_xy_m']:number(v,-2,2)
    vector(req['tool_goal_xyz_m'],-2,2)
    with store.lock:
        p=store.get(pid)
        if p['revision']!=expected:raise ValueError('Revision conflict')
        if set(p['assets'])-{'requirements.md','notes.md'}:raise ValueError('Prepare an original system in a blank project; existing goals remain intact')
        brief={'schema':SCHEMA,'position_count':position_count,'velocity_count':velocity_count,'intent':p['intent']}
        return {'revision':store.revise(pid,{'requirements.json':encode(req).decode(),'system-brief.json':encode(brief).decode(),
            'build-brief.md':p['intent']+'\n\nOwner world-frame and coupled-motion criteria are frozen in requirements.json. No geometry or control answer has been created. Use build_system with your own geometry and named channel bindings, run one coupled native model, diagnose failures, revise and verify.\n'},'Owner froze original composed-system task; no geometry or inference',expected)}

def guide():
    return {'schema':SCHEMA,'design_fields':['geometry','controller'],
        'geometry':{'fields':['schema','bodies'],'schema':SYSTEM_VERSION,'body_fields':authoring_guide()['body_fields'],'body_count':'1..32; one world-parent free root at index0, every other body connected to an earlier parent',
            'optional_body_fields':{'armature_kg_m2':'0..1, hinge reflected inertia assumption','body_rpy_rad':'roll,pitch,yaw in radians; local rotation Rz(yaw)Ry(pitch)Rx(roll), applied to body, geometry and joint frame','continuous':'boolean, hinges only; true for velocity axes, absent/false for position axes','contact':'{friction:[sliding,torsional,rolling],priority:0..5}; coefficients0..2; higher-priority MuJoCo contact settings take precedence'},
            'generated_names':'joint=body_id+_joint; geom=body_id+_solid. Cylinder axis is local Z. Choose all dimensions, masses, mounts and orientations yourself.'},
        'controller':{'kind':graph.KIND,'channels':'one per hinge, named; may be submitted in any order. position:{joint,mode:"position",kp:0..200,kd:0..30,target_rad:-1.5..1.5,ramp_s:.05..2,max_torque_Nm:.01..20}; velocity:{joint,mode:"velocity",kv:0..30,max_torque_Nm:.01..20}',
            'drive':'{left:joint_name,right:joint_name,distance_gain:.1..10,heading_gain:.1..10,max_wheel_rad_s:.1..15}. Exactly the two velocity joints. No owner goal is copied into controller; live feedback always reads frozen requirements.',
            'drive_geometry':'Two equal-radius centered local-Z cylinder geometries with coaxial local-Z continuous hinges. Their rotated axes must be parallel to base lateral Y, on one axle. Left center has larger base-local Y than right. Radius, track and spin signs are measured from YOUR compiled model, not supplied answers.',
            'observation':'{body:payload_body_id,point_m:[x,y,z]}. Point must equal that body geometry mass center/offset_m, and every position joint must be in its ancestor chain. Payload body mass must meet the frozen minimum.',
            'position_reference':'Native hinge initial positions are zero; each position reference ramps linearly from zero to its AI-chosen target over ramp_s. Native velocities/positions/forces, base and payload world/local positions are recorded.'},
        'scope':'One native coupled model, named position/velocity binding plus an existing planar-drive feedback block. No robot model, arm solution or IK/template is provided. Goals/counts and established binding identities cannot be changed by AI; user native edits need review.'}

def build(store,pid,serialized,expected):
    if not isinstance(serialized,str) or len(serialized.encode())>100000:raise ValueError('Bounded serialized system data required')
    d=json.loads(serialized)
    if not isinstance(d,dict) or set(d)!={'geometry','controller'}:raise ValueError('geometry/controller data required')
    with store.lock:
        p=store.get(pid)
        if p['revision']!=expected:raise ValueError('Revision conflict: inspect before editing')
        if 'actuators.json' in p['assets']:raise ValueError('Component constraints cannot be replaced by ideal control')
        if 'system-brief.json' not in p['assets']:raise ValueError('Owner must freeze a blank system brief first')
        brief=json.loads(store.asset(pid,'system-brief.json'));req=json.loads(store.asset(pid,'requirements.json'));g=d['geometry'];c=d['controller']
        if brief.get('schema')!=SCHEMA or req.get('authoring_mode')!=SCHEMA:raise ValueError('Owner system capability missing')
        if not isinstance(g,dict) or set(g)!={'schema','bodies'} or g['schema']!=SYSTEM_VERSION:raise ValueError('Geometry-only schema3 required')
        # Disabled legacy controls are compiler plumbing only; no model/answer is inserted.
        files,manifest=compile_design({**g,'control':{'enabled':False,'kp':0,'kd':0,'target_rad':[]},'criteria':{'maximum':.1,'duration_s':req['duration_s']}})
        if g['bodies'][0]['joint']!='free' or any(b['parent']<0 for b in g['bodies'][1:]):raise ValueError('One connected free-root tree required')
        if not isinstance(c,dict) or not isinstance(c.get('channels'),list):raise ValueError('Named control channel objects required')
        root=ET.fromstring(files['robot.xml']);names=[j.get('name') for j in root.iter('joint')]
        channels=c['channels']
        if any(not isinstance(ch,dict) or not isinstance(ch.get('joint'),str) for ch in channels):raise ValueError('Channel joint names required')
        by_name={ch['joint']:ch for ch in channels}
        if len(by_name)!=len(channels) or set(by_name)!=set(names):raise ValueError('Exactly one named channel per generated hinge required')
        c={**c,'channels':[by_name[n] for n in names]}
        if any(sum(ch.get('mode')==mode for ch in channels)!=brief[mode+'_count'] for mode in ('position','velocity')):raise ValueError('Owner control counts differ')
        for motor,ch in zip(root.find('actuator'),c['channels']):
            from .build import number
            limit=number(ch.get('max_torque_Nm'),.01,20);motor.set('ctrlrange',f'{-limit} {limit}')
        def signature(ctrl):return {'kind':ctrl.get('kind'),'channels':[(ch['joint'],ch['mode']) for ch in ctrl.get('channels',[])],'bindings':{k:ctrl.get('drive',{}).get(k) for k in ('left','right')},'observation':ctrl.get('observation')}
        if 'build-manifest.json' in p['assets']:
            old=json.loads(store.asset(pid,'build-manifest.json'))
            if any(p['assets'].get(n)!=old['outputs'].get(n) for n in ('robot.xml','controller.json','components.json','BOM.csv','system.json')):raise ValueError('Native files changed outside system builder; owner review required')
        elif 'robot.xml' in p['assets']:raise ValueError('Existing native geometry cannot be replaced')
        if 'controller.json' in p['assets']:
            current=json.loads(store.asset(pid,'controller.json'))
            if not isinstance(current,dict) or signature(current)!=signature(c):raise ValueError('Established control/observation identities are owner-protected')
        files.update({'assembly.json':encode(g).decode(),'robot.xml':ET.tostring(root,encoding='unicode'),'controller.json':encode(c).decode(),'system.json':encode(d).decode(),'system-generator.txt':Path(__file__).read_text(encoding='utf-8')})
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)
            for n in ('robot.xml','controller.json'):(folder/n).write_text(files[n],encoding='utf-8')
            (folder/'requirements.json').write_text(encode(req).decode(),encoding='utf-8');xml,checked,criteria=graph.validate(folder);graph.model_geometry(xml,checked,criteria)
        manifest.update(generator=SCHEMA);manifest['capabilities'].update(mixed_position_velocity_inputs=True)
        manifest['outputs']={n:digest(text.encode()) for n,text in files.items() if n!='build-manifest.json'};files['build-manifest.json']=encode(manifest).decode()
        return {'revision':store.revise(pid,files,'AI authored original geometry/control composition; physics NOT_RUN',expected),'files':list(files),'manifest':manifest}

def diagnostics(store,pid,rid):
    p=store.get(pid);run=next((r for r in p['runs'] if r['id']==rid),None)
    if not run or run['state']!='COMPLETED' or run['adapter']!=graph.ADAPTER:raise ValueError('Completed composed native run required')
    path=store.path(pid)/'runs'/rid/'trace.csv';raw=path.read_bytes()
    if digest(raw)!=run['outputs']['trace.csv']:raise ValueError('Native trace hash mismatch')
    rows=list(csv.DictReader(raw.decode().splitlines()));selected=rows[::max(1,(len(rows)-1)//20)]
    if selected[-1]!=rows[-1]:selected.append(rows[-1])
    return {'run':rid,'trace_sha256':digest(raw),'measured':run['result']['measured'],'criteria':run['result']['criteria'],'geometry':run['result']['geometry'],'samples':[{k:float(v) for k,v in row.items()} for row in selected],
            'source':'Actual single coupled MuJoCo model samples; no supplied diagnosis or repair answer'}
