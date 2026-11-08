"""Versioned bridge to the original bird aerodynamics/mission engines, never MuJoCo.

CAD code is an editable design asset, not executable by this adapter. Historical CAD and
24-case R2 evidence are immutable attachments; this bounded task executes ONE selected case.
"""
import json
import math
from dataclasses import asdict, replace
from pathlib import Path
from .projects import encode, read, write, digest

ROOT = Path(__file__).resolve().parents[2]
SOURCES = tuple('implementation/bird/'+n+'.py' for n in ('aero','flight','design','mission','replay','cad_sandbox'))

def create(store):
    from implementation.bird import mission as M
    base = ROOT/'examples_builtin/bird/r2'
    ev = read(base/'package/evaluation.json')
    gains = asdict(M.MissionGains())
    gains.update(flare_sink_mps=.4, flare_speed_mps=5.)
    values = {'design.json': (base/'design.json').read_text(encoding='utf-8'),
        'controller.json': encode({'kind':'bird-mission/1','gains':gains,'case':'nominal'}).decode(),
        'requirements.json': encode({'criteria':'MISSION_CRITERIA_R3','coverage':'one selected case; full acceptance requires all 24',
                                     'mission':asdict(M.Mission(cruise_speed=7.))}).decode(),
        'physical.json': encode({'mass_kg':ev['mass_g']/1000,'frequency_limits_hz':[1.,9.71],
            'provenance':'Original R2 evaluated mass and drive limits; not measured hardware. Geometry edits require mass review.'}).decode(),
        'parts-library.json': (ROOT/'release/bird/parts_library.json').read_text(encoding='utf-8'),
        'README.md': 'Original R2 bird. Specialized aerodynamics + assumed attitude dynamics. UNVERIFIED.\n'
            'Run one registered case per bounded task; a nominal pass is not a 24-case verdict.\n'
            'Original full bird CAD/AI code is preserved; arbitrary CAD Python and complete BirdLoop regeneration are unsupported shared tasks.\n'}
    for n in (1,2,3):
        values[f'criteria-r{n}.json']=(ROOT/f'release/bird/MISSION_CRITERIA_R{n}.json').read_text(encoding='utf-8')
    p=store.create('Robotic bird — R2', 'Preserve and reproduce the original R2 bird engineering project', 'flying', values,
        {'aerodynamics':{'adapter':'bird.mission/1','validation':'UNVERIFIED'},
         'cad':{'status':'LEGACY_ADAPTER_PARTIAL'},'physical':{'status':'NOT_TESTED'}})
    files={}
    for prefix,folder in [('r2',base),('release',ROOT/'release/bird')]:
        for f in folder.rglob('*'):
            if f.is_file() and not f.is_symlink():files[prefix+'/'+f.relative_to(folder).as_posix()]=f.read_bytes()
    # Preserve original sources of historical design/AI iteration and their evidence, without executing them.
    history=ROOT/'reference-history' if (ROOT/'reference-history').exists() else ROOT/'usage/bird-r2-20261022'
    for n in ('README.md','build_r2.py','design_r2.py','RLSOK-bird-R2-build-package.zip'):
        f=history/n
        if f.exists():files['history/'+n]=f.read_bytes()
    files.update(history_files())
    store.attach(p['id'],files,'Original repository bird R2 + release reports; historical simulation, no new hardware evidence')
    return store.get(p['id'])

def validate(folder):
    from implementation.bird import mission as M
    d,c,r,phys = [read(folder/n) for n in ('design.json','controller.json','requirements.json','physical.json')]
    # Reject NaN and excessive structures before any native computation. Never execute embedded CAD.
    json.dumps([d,c,r,phys],allow_nan=False)
    def number(v,lo,hi):
        if type(v) not in (int,float) or not math.isfinite(v) or not lo<=v<=hi:raise ValueError('Bird parameter outside bounded adapter envelope')
    w=d['wing']
    for k in ('semi_span_m','root_chord_m','tip_chord_m'):number(w[k],.01,2.)
    number(d.get('body_cda_m2',.003),.00001,.2)
    if d.get('wings_per_side',1) not in (1,2):raise ValueError('Unsupported wing configuration')
    k=d.get('kinematics',{})
    for key,lo,hi in [('amplitude_deg',1,85),('bias_deg',-45,45),('dihedral_deg',-30,45),('upstroke_load',0,1),('incidence_deg',-30,30),('twist_fraction',0,1)]:
        if key in k:number(k[key],lo,hi)
    if k.get('aero_model','delaurier_tilt') not in ('strip','delaurier_tilt'):raise ValueError('Unsupported bird aero model')
    if w.get('planform'):
        st=w['planform']['stations']
        if not 2<=len(st)<=20:raise ValueError('Planform station limit')
        previous=-1
        for row in st:
            if len(row)!=4:raise ValueError('Four planform columns required')
            for v in row:number(v,-2,2)
            if not previous<row[0]<=1 or row[2]<=0 or not 0<=row[3]<=1:raise ValueError('Invalid station ordering/chord')
            previous=row[0]
        number(w['planform'].get('primaries',0),0,20)
    if c.get('kind')!='bird-mission/1' or c.get('case') not in {n for n,_,_ in M.cases(2)}:raise ValueError('Unknown registered bird case')
    for key,val in c['gains'].items():
        if key not in asdict(M.MissionGains()):raise ValueError('Unknown bird gain')
        number(val,0,50)
    if r.get('criteria')!='MISSION_CRITERIA_R3':raise ValueError('Use original registered mission criteria')
    ms=M.Mission(**r['mission'])
    for key,val in asdict(ms).items():number(val,.01,360 if key=='turn_deg' else 240)
    if ms.t_max!=240 or ms.turn_deg!=360:raise ValueError('Registered duration/turn may not be weakened')
    number(phys['mass_kg'],.005,2)
    low,high=phys['frequency_limits_hz'];number(low,1,10);number(high,low,12)
    for n in (1,2,3):
        if (folder/f'criteria-r{n}.json').read_bytes() != (ROOT/f'release/bird/MISSION_CRITERIA_R{n}.json').read_text(encoding='utf-8').encode():
            raise ValueError('Registered bird criteria changed')
    lib=read(folder/'parts-library.json')
    if len(lib['parts'])>200:raise ValueError('Parts library limit')
    return d,c,r,phys,lib

def run(folder):
    from implementation.bird import design as D, mission as M, replay as RP
    import numpy as np
    d,c,r,phys,rawlib=validate(folder)
    lib={'parts':{p['id']:p for p in rawlib['parts']},'eff':rawlib['assumed_efficiencies'],
         'policy':rawlib['price_policy'],'endurance_calibration':rawlib.get('endurance_calibration')}
    table,model,_,_=D.aero_table(d,lib)
    name,over,wind=next(case for case in M.cases(2) if case[0]==c['case'])
    native=M.fly(table,phys['mass_kg'],phys['frequency_limits_hz'],
        replace(M.MissionGains(**c['gains']),**over),mission=M.Mission(**r['mission']),wind=wind)
    j=M.judge(native,phys['frequency_limits_hz'])
    # Original mission uses NaN for unavailable power/battery channels. Preserve them as
    # explicit missing values in strict JSON, never invent a battery measurement.
    for key in ('battery_pct','p_elec_w'):
        native['log'][key]=[v if math.isfinite(v) else None for v in native['log'][key]]
    write(folder/'flight-log.json',native)
    RP.start(folder/'replay',d,{},M.LABEL,[name],{'coverage':'one selected case','criteria':'MISSION_CRITERIA_R3'})
    RP.add_run(folder/'replay',name,native,j);RP.finish(folder/'replay','SIM_PASS' if j['passed'] else 'SIM_FAIL')
    result={'adapter':'bird.mission/1','solver':{'name':'RLSOK original bird aero/mission','version':'source-sha256 receipt','numpy':np.__version__},
        'aero_model':model,'case':name,'criteria':r,'checks':j['rules'],
        'measured':{k:v for k,v in j['numbers'].items() if type(v) in (int,float)},'judgement':j,
        'passed':j['passed'],'label':M.LABEL,'warnings':['UNVERIFIED aerodynamics/attitude; simulation only'],
        'levels':{'proposal':'RECORDED','artifact':'GENERATED','static':'PASSED_INPUT_CHECKS',
                  'simulation':'COMPLETED','requirements':'PASSED_SELECTED_CASE' if j['passed'] else 'FAILED',
                  'manufacturing':'NOT_REVIEWED','physical':'NOT_TESTED'},
        'limitations':D.LABEL['reasons']+['One selected case only; full R3 requires all 24 cases.',
            'Mass/drive copied from R2 evaluation, not hardware measurements; geometry edits need mass reevaluation.',
            'Battery/energy not evaluated in this task; missing channels are null, not measured zero.',
            'Complete original BirdLoop/CAD regeneration is not a supported shared task in this RC; original artifacts are preserved.']}
    write(folder/'result.json',result)
    print(json.dumps({'case':name,'requirements':result['levels']['requirements']}),flush=True)

def verify_evidence(folder,result,controller,req):
    from implementation.bird import mission as M
    native=read(folder/'flight-log.json')
    if any(not math.isfinite(v) for a in native['log'].values() for v in a if type(v) in (int,float)):
        raise ValueError('Nonfinite flight log')
    expected=M.judge(native,read(folder/'physical.json')['frequency_limits_hz'])
    if expected!=result['judgement'] or result['passed']!=expected['passed'] or result['criteria']!=req:
        raise ValueError('Bird log criterion mismatch')
    measured={k:v for k,v in expected['numbers'].items() if type(v) in (int,float)}
    if result['measured']!=measured or result['checks']!=expected['rules'] or result['case']!=controller['case']:
        raise ValueError('Bird measurement/case mismatch')
    if native['mission']!=req['mission']:raise ValueError('Bird mission input mismatch')
    if result['levels']['physical']!='NOT_TESTED':raise ValueError('Invalid physical claim')

def history_files():
    """Small native design/report copies + complete on-disk inventory; originals stay intact."""
    history=ROOT/'reference-history' if (ROOT/'reference-history').exists() else ROOT/'usage/bird-r2-20261022';files={};inventory={}
    for f in history.rglob('*'):
        if f.is_file() and not f.is_symlink():
            name=f.relative_to(history).as_posix();raw=f.read_bytes()
            inventory[name]={'sha256':digest(raw),'bytes':len(raw)}
            if f.name in ('design.json','eval.json','spec.json','mission_result.json','cad_result.json','flight_params.json'):
                files['history/'+name]=raw
    for f in (ROOT/'implementation/bird').glob('*.py'):
        files['sources/'+f.name]=f.read_bytes()
    files['history/inventory.json']=encode({'root':'usage/bird-r2-20261022','files':inventory,
        'preservation':'All original files retained in source library; selected design/report copies included here. Not revalidation.'})
    return files

def coverage(store,pid):
    """Report compatible recorded case coverage, never flight certification or copied claims."""
    from implementation.bird import mission as M
    p=store.get(pid);current=read(store.path(pid)/'revisions'/p['revision']/'controller.json')
    gains={k:v for k,v in current.items() if k!='case'}
    required={n for n,_,_ in M.cases(2)};matched={};source_sets=set()
    keys=('design.json','physical.json','parts-library.json','requirements.json','criteria-r1.json','criteria-r2.json','criteria-r3.json')
    for r in p['runs']:
        if r.get('adapter')!='bird.mission/1' or r['state']!='COMPLETED':continue
        if any(r['inputs'].get(k)!=p['assets'].get(k) for k in keys):continue
        c=read(store.path(pid)/'runs'/r['id']/'controller.json')
        if {k:v for k,v in c.items() if k!='case'}!=gains:continue
        if c['case'] in required:
            matched[c['case']]={'run':r['id'],'passed':r['result']['passed']}
    for item in matched.values():
        r=next(r for r in p['runs'] if r['id']==item['run']);source_sets.add(digest(encode(r['tool_sources'])))
    complete=set(matched)==required and len(source_sets)==1
    return {'completed_cases':len(matched),'required_cases':24,'same_solver_snapshot':len(source_sets)==1,
        'verdict':'SIM_PASS_UNVERIFIED' if complete and all(v['passed'] for v in matched.values()) else 'SIM_FAIL' if complete else 'INCOMPLETE',
        'cases':matched,'physical':'NOT_TESTED','scope':'Recorded runs matching current inputs/base gains; integrity checked separately. No physical flight claim.'}
