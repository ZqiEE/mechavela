"""Bounded data-only primitive assembly -> existing native MJCF/PD contract.

This generates editable sources, not a CAD kernel, solver, or validation verdict.
No imported code, file references, arbitrary expressions or commands are executed.
"""
import csv,io,json,math,re,xml.etree.ElementTree as ET
from pathlib import Path
from .projects import encode,digest

VERSION='rlsok.primitive-builder/1'
GENERAL_VERSION='rlsok.primitive-builder/2'
SYSTEM_VERSION='rlsok.primitive-builder/3'
AXES={'x':'1 0 0','y':'0 1 0','z':'0 0 1'}

def number(v,lo,hi):
    if type(v) not in (int,float) or not math.isfinite(v) or not lo<=v<=hi:raise ValueError(f'Finite value {lo}..{hi} required')
    return float(v)

def vector(v,lo,hi):
    if not isinstance(v,list) or len(v)!=3:raise ValueError('Three coordinates required')
    return [number(x,lo,hi) for x in v]

def compile_design(data,confirm_criteria=False):
    if not isinstance(data,dict) or set(data)!={'schema','bodies','control','criteria'} or data['schema'] not in (VERSION,GENERAL_VERSION,SYSTEM_VERSION):raise ValueError('Unsupported builder schema')
    general=data['schema']!=VERSION;system=data['schema']==SYSTEM_VERSION
    bodies=data['bodies'];control=data['control'];criteria=data['criteria']
    if not isinstance(bodies,list) or not 1<=len(bodies)<=(32 if general else 8):raise ValueError('Add bounded primitive bodies:1..32 for schema2,1..8 for schema1')
    if not isinstance(control,dict) or set(control)!={'enabled','kp','kd','target_rad'} or type(control['enabled']) is not bool:raise ValueError('Invalid control schema')
    root=ET.Element('mujoco',model='User primitive assembly');ET.SubElement(root,'compiler',angle='radian');ET.SubElement(root,'option',timestep='0.002',gravity='0 0 -9.81',integrator='implicitfast')
    world=ET.SubElement(root,'worldbody');ET.SubElement(world,'geom',name='ground',type='plane',size='3 3 .1')
    nodes=[];ids=set();free_count=0;bom=io.StringIO();writer=csv.writer(bom,lineterminator='\n');writer.writerow(['Body','Parent','Primitive','Mass_kg','Joint','Actuator','Hardware_qualification'])
    fmt=lambda xs:' '.join(format(x,'.12g') for x in xs)
    for i,b in enumerate(bodies):
        fields={'id','parent','joint','axis','shape','position_m','offset_m','dimensions_m','mass_kg'}
        optional=({'armature_kg_m2'} if general else set())|({'body_rpy_rad','continuous','contact'} if system else set())
        if not isinstance(b,dict) or not fields<=set(b)<=fields|optional:raise ValueError('Invalid body fields')
        name=b['id']
        if not isinstance(name,str) or not re.fullmatch('[A-Za-z][A-Za-z0-9_]{0,23}',name) or name in ids:raise ValueError('Unique body names:letters,numbers,underscore,24characters')
        ids.add(name);parent=b['parent']
        if type(parent) is not int or not -1<=parent<i:raise ValueError('Parent must be world(-1) or an earlier body; cycles refused')
        if any(not isinstance(b[k],str) for k in ('joint','axis','shape')) or b['joint'] not in (('fixed','hinge','free') if general else ('fixed','hinge')) or b['axis'] not in AXES or b['shape'] not in ('box','cylinder','sphere','capsule'):raise ValueError('Unsupported primitive/joint/axis')
        if b['joint']=='free':
            free_count+=1
            if parent!=-1 or free_count>1:raise ValueError('At most one free root body supported')
        pos=vector(b['position_m'],-2,2);offset=vector(b['offset_m'],-1,1);dims=vector(b['dimensions_m'],.005,1);mass=number(b['mass_kg'],.01,10)
        if b['shape']!='box' and not math.isclose(dims[0],dims[1],rel_tol=1e-9):raise ValueError('Round primitives require equal X/Y diameters')
        if b['shape']=='sphere' and not math.isclose(dims[0],dims[2],rel_tol=1e-9):raise ValueError('Sphere dimensions must be equal diameters')
        node=ET.SubElement(world if parent==-1 else nodes[parent],'body',name=name,pos=fmt(pos));nodes.append(node)
        if 'body_rpy_rad' in b:
            roll,pitch,yaw=vector(b['body_rpy_rad'],-math.pi,math.pi);cr,sr=math.cos(roll/2),math.sin(roll/2);cp,sp=math.cos(pitch/2),math.sin(pitch/2);cy,sy=math.cos(yaw/2),math.sin(yaw/2)
            node.set('quat',fmt([cr*cp*cy+sr*sp*sy,sr*cp*cy-cr*sp*sy,cr*sp*cy+sr*cp*sy,cr*cp*sy-sr*sp*cy]))
        if b['joint']=='hinge':ET.SubElement(node,'joint',name=name+'_joint',axis=AXES[b['axis']],damping='.04',range='-2 2')
        if 'continuous' in b:
            if type(b['continuous']) is not bool or b['joint']!='hinge':raise ValueError('Continuous flag applies to hinges only')
            if b['continuous']:node.find('joint').attrib.pop('range');node.find('joint').set('limited','false')
        if 'armature_kg_m2' in b:
            inertia=number(b['armature_kg_m2'],0,1)
            if b['joint']!='hinge':raise ValueError('Rotor inertia applies to a hinge only')
            node.find('joint').set('armature',str(inertia))
        if b['joint']=='free':ET.SubElement(node,'freejoint',name=name+'_free')
        attrs={'name':name+'_solid','type':b['shape'],'mass':str(mass),'pos':fmt(offset)}
        if 'contact' in b:
            c=b['contact']
            if not isinstance(c,dict) or set(c)!={'friction','priority'} or type(c['priority']) is not int or not 0<=c['priority']<=5:raise ValueError('Bounded contact friction/priority required')
            attrs.update(friction=fmt(vector(c['friction'],0,2)),priority=str(c['priority']))
        if b['shape']=='box':attrs['size']=fmt([x/2 for x in dims])
        elif b['shape']=='sphere':attrs['size']=fmt([dims[0]/2])
        elif b['shape']=='cylinder':attrs['size']=fmt([dims[0]/2,dims[2]/2])
        else:
            attrs.pop('pos');attrs['size']=fmt([dims[0]/2]);attrs['fromto']=fmt([*offset[:2],offset[2]-dims[2]/2,*offset[:2],offset[2]+dims[2]/2])
        ET.SubElement(node,'geom',**attrs)
        writer.writerow([name,'world' if parent==-1 else bodies[parent]['id'],b['shape'],mass,b['joint'],'ideal torque motor' if b['joint']=='hinge' else 'none','NOT_TESTED'])
    joints=[j.attrib['name'] for j in root.iter('joint')]
    compatible=1<=len(joints)<=24 if general else len(joints)==2
    if control['enabled'] and not compatible:raise ValueError('Schema1 PD requires exactly2hinge joints; schema2 supports1..24 named hinges; otherwise keep geometry-only')
    if joints:
        actuators=ET.SubElement(root,'actuator');sensors=ET.SubElement(root,'sensor')
        for i,name in enumerate(joints):
            ET.SubElement(actuators,'motor',name=f'motor{i+1}',joint=name,ctrllimited='true',ctrlrange='-20 20' if general else '-2 2')
            ET.SubElement(sensors,'jointpos',name=f'q{i+1}',joint=name);ET.SubElement(sensors,'jointvel',name=f'dq{i+1}',joint=name)
    if control['enabled']:
        kp=number(control['kp'],0,200 if general else 100);kd=number(control['kd'],0,30 if general else 10);target=control['target_rad']
        if not isinstance(target,list) or len(target)!=len(joints):raise ValueError('One target per controlled hinge required')
        target=[number(t,-1.5,1.5) for t in target]
        c={'kind':'joint_space_pd/1' if general else 'joint_pd/1','kp':kp,'kd':kd,'target_rad':target}
        if general:c['joint_names']=joints
    else:c={'kind':'unconfigured/1','reason':'Geometry only; no compatible controller configured by the user'}
    required={'maximum','duration_s'};allowed=required|({'minimum_base_height_m','maximum_base_tilt_rad'} if general else set())
    if not isinstance(criteria,dict) or not required<=set(criteria)<=allowed:raise ValueError('Invalid acceptance criteria')
    maximum=number(criteria['maximum'],.00001,.5);duration=number(criteria['duration_s'],.1,5)
    extra={}
    for key,lo,hi in [('minimum_base_height_m',.01,2),('maximum_base_tilt_rad',0,1.57)]:
        if key in criteria:
            if not free_count:raise ValueError('Standing criteria require a floating base')
            extra[key]=number(criteria[key],lo,hi)
    values={'assembly.json':encode(data).decode(),'robot.xml':ET.tostring(root,encoding='unicode'),'controller.json':encode(c).decode(),
        'components.json':encode({'generator':data['schema'],'bodies':bodies,'joint_order':joints,'actuation':'Ideal simulated torque motors,'+('20Nm' if general else '2Nm')+' bounds','sensors':'Ideal simulated joint position/velocity','power':'NOT_VALIDATED','electronics':'UNSELECTED','manufacturing':'NOT_REVIEWED','physical':'NOT_TESTED'}).decode(),
        'BOM.csv':bom.getvalue(),'build-generator.txt':Path(__file__).read_text(encoding='utf-8')}
    if control['enabled'] and confirm_criteria:
        values['requirements.json']=encode({'metric':'final_joint_error_rad','maximum':maximum,'duration_s':duration,**extra,
            **({'target_rad':target,'builder_schema':data['schema']} if general else {}),
            'scope':'User-defined primitive rigid-body joint positioning/posture; ideal motors/encoders; not hardware qualification'}).decode()
    manifest={'generator':data['schema'],'outputs':{n:digest(t.encode()) for n,t in values.items()},'joint_order':joints,
        'capabilities':{'primitive_geometry':'GENERATED','two_hinge_PD_inputs':not general and compatible and control['enabled'],
            'named_hinge_PD_inputs':general and compatible and control['enabled'],'floating_base':bool(free_count),'physics':'NOT_RUN','requirements':'NOT_EVALUATED','manufacturing':'NOT_REVIEWED','physical':'NOT_TESTED'},
        'limitations':['Primitive fixed/hinge tree and schema2 optional free base; no CAD assembly/tolerancing','Joint positioning/posture is not gait, inverse kinematics, collision safety, flight or hydrodynamics','BOM is simulated component inventory, not a procurement/manufacturing BOM']}
    values['build-manifest.json']=encode(manifest).decode();return values,manifest

def save_design(store,pid,data,expected,confirm_criteria=False,overwrite_native=False,actor='User'):
    with store.lock:
        p=store.get(pid)
        if p['revision']!=expected:raise ValueError('Revision conflict:reload before building')
        if 'motion.json' in p['assets']:
            raise ValueError('Cyclic-motion project: edit its native motion/controller or use build_motion. Static assembly rebuilding cannot silently replace its trajectory.')
        if 'system.json' in p['assets']:raise ValueError('Composed-control project: use its native editor or build_system; static building cannot replace mixed control')
        if 'actuators.json' in p['assets']:
            raise ValueError('This is a component-constrained project. Edit its native files to preserve motor, payload and acceptance constraints, or build an ideal-motor assembly in a new blank project.')
        previous=json.loads(store.asset(pid,'build-manifest.json')) if 'build-manifest.json' in p['assets'] else None
        changed=[n for n in ('robot.xml','controller.json','components.json','BOM.csv') if n in p['assets'] and (not previous or previous.get('outputs',{}).get(n)!=p['assets'][n])]
        if changed and not overwrite_native:raise ValueError('Native files changed outside the builder; review and explicitly approve replacing:'+','.join(changed))
        values,manifest=compile_design(data,confirm_criteria)
        if data['control']['enabled'] and not confirm_criteria:
            if 'requirements.json' not in p['assets']:raise ValueError('Define and confirm your acceptance criterion before enabling PD simulation')
            current=json.loads(store.asset(pid,'requirements.json'))
            keys={'maximum','duration_s'}|({'minimum_base_height_m','maximum_base_tilt_rad'}&set(current))
            if current.get('metric')!='final_joint_error_rad' or any(current.get(k)!=data['criteria'].get(k) for k in keys) or ('target_rad' in current and current['target_rad']!=data['control']['target_rad']):raise ValueError('Acceptance criteria differ; explicit user confirmation required')
        rev=store.revise(pid,values,actor+' built native primitive assembly; physics NOT_RUN',expected)
        return {'revision':rev,'manifest':manifest,'files':list(values)}


def authoring_guide():
    return {'schema':VERSION,'general_schema':GENERAL_VERSION,'design_fields':['schema','bodies','control','criteria'],
        'bodies':'1..8 objects; parent is world(-1) or an earlier body index; positions/offsets are local metres',
        'body_fields':{'id':'Unique letter-led name, 24 chars, letters/digits/underscore','parent':'integer -1 or earlier body index',
            'joint':'fixed or hinge; schema2 also permits one world-parent free base','axis':'x, y or z','shape':'box, sphere, cylinder or capsule',
            'position_m':'three numbers -2..2','offset_m':'three numbers -1..1','dimensions_m':'three full dimensions .005..1; equal X/Y for round shapes',
            'mass_kg':'.01..10'},
        'optional_schema2_body_field':{'armature_kg_m2':'0..1 for a hinge only; explicit simulated reflected rotor inertia, not identified hardware. Default0. Very low inertia plus high discrete PD damping can be unstable.'},
        'control_fields':{'enabled':'true for approved two-hinge dynamics','kp':'0..100','kd':'0..10','target_rad':'two owner-approved numbers; cannot change'},
        'criteria_fields':{'maximum':'must match requirements.json','duration_s':'must match requirements.json'},
        'general_mode':'Schema2:1..32 bodies,1..24 named hinges, optional free base, kp0..200/kd0..30, one target per hinge; ideal motors <=20Nm. Owner brief builder_schema selects mode. Standing criteria may include minimum_base_height_m and maximum_base_tilt_rad. No gait/planning/hardware qualification.',
        'boundary':'Owner must prepare a build brief first. AI can design geometry and gains, never approve/change goals, criteria or native overwrite. Schema1 ideal motors <=2Nm; no hardware qualification.'}


def prepare_brief(store,pid,criteria,target,expected,schema=VERSION):
    """Local owner operation, never exposed as an AI tool; no geometry is inserted."""
    if schema not in (VERSION,GENERAL_VERSION):raise ValueError('Unsupported builder schema')
    base={'maximum','duration_s'};allowed=base|({'minimum_base_height_m','maximum_base_tilt_rad'} if schema==GENERAL_VERSION else set())
    if not isinstance(criteria,dict) or not base<=set(criteria)<=allowed:raise ValueError('Invalid acceptance criteria')
    maximum=number(criteria['maximum'],.00001,.5);duration=number(criteria['duration_s'],.1,5)
    if not isinstance(target,list) or not (1<=len(target)<=24 if schema==GENERAL_VERSION else len(target)==2):raise ValueError('Owner joint targets required for selected schema')
    target=[number(x,-1.5,1.5) for x in target]
    extra={k:number(criteria[k],lo,hi) for k,lo,hi in [('minimum_base_height_m',.01,2),('maximum_base_tilt_rad',0,1.57)] if k in criteria}
    with store.lock:
        p=store.get(pid)
        if p['revision']!=expected:raise ValueError('Revision conflict:reload before preparing brief')
        if set(p['assets'])&{'robot.xml','assembly.json','controller.json','requirements.json','actuators.json'}:raise ValueError('Prepare an AI build brief in a blank project; existing native goals are preserved')
        requirements={'metric':'final_joint_error_rad','maximum':maximum,'duration_s':duration,**extra,'target_rad':target,'builder_schema':schema,
                      'scope':p['intent']+'\nIdeal primitive rigid-body positioning only; no physical/manufacturing qualification.'}
        return {'revision':store.revise(pid,{'requirements.json':encode(requirements).decode(),
            'build-brief.md':p['intent']+'\n\nOwner-defined targets (rad): '+str(target)+'; maximum error (rad): '+str(maximum)+'; duration (s): '+str(duration)+'.\nNo model geometry has been created. AI must use build_assembly to design native files, run real simulations and verify evidence.\n'},
            'Owner prepared AI design brief; no model or solver executed',expected)}


def build_from_agent(store,pid,serialized,expected):
    if not isinstance(serialized,str) or len(serialized.encode())>100000:raise ValueError('Bounded serialized assembly design required')
    data=json.loads(serialized)
    files,_=compile_design(data)
    with store.lock:
        p=store.get(pid)
        if p['revision']!=expected:raise ValueError('Revision conflict:reload before building')
        if 'requirements.json' not in p['assets']:raise ValueError('Owner must prepare an AI build brief with fixed targets and criteria first')
        req=json.loads(store.asset(pid,'requirements.json'))
        current=json.loads(store.asset(pid,'controller.json')) if 'controller.json' in p['assets'] else {}
        if not isinstance(req,dict) or not isinstance(current,dict):raise ValueError('Invalid native goals/controller draft; owner review required')
        target=req.get('target_rad',current.get('target_rad'))
        proposed=json.loads(files['controller.json'])
        if not isinstance(target,list) or not 1<=len(target)<=24:raise ValueError('Owner-approved joint targets missing from build brief')
        if req.get('builder_schema',VERSION)!=data['schema']:raise ValueError('Builder schema differs from owner-approved capability')
        if current and (current.get('kind')!=proposed['kind'] or current.get('joint_names')!=proposed.get('joint_names')):raise ValueError('AI builder cannot change existing controller capability or named joint goals')
        if data['control']['enabled'] is not True or data['control']['target_rad']!=target:raise ValueError('AI cannot change owner joint targets or disable approved dynamics')
        if current and current.get('target_rad')!=target:raise ValueError('Controller goals differ from approved brief; owner review required')
        return save_design(store,pid,data,expected,actor='AI')
