"""Restricted parameter data -> the existing OpenCascade kernel/export APIs.

Never execute imported/proposed Python. Only this audited template generates CAD code.
Historical arbitrary bird cad_code and full assemblies remain explicitly unsupported here.
"""
import math,json,os
from pathlib import Path
from .projects import read,write,encode
ROOT=Path(__file__).resolve().parents[2]

def defaults():
    return {'cad.json':encode({'shape':'box','length_mm':60.,'width_mm':40.,'height_mm':1.,'hole_diameter_mm':6.}).decode(),
        'cad-requirements.json':encode({'minimum_volume_mm3':18000.,'maximum_volume_mm3':20000.,
            'scope':'Reference mounting plate geometry, no strength or manufacturing qualification'}).decode()}

def validate(folder):
    p=read(folder/'cad.json');req=read(folder/'cad-requirements.json')
    if set(p)!={'shape','length_mm','width_mm','height_mm','hole_diameter_mm'} or p['shape']!='box':raise ValueError('Supported CAD schema: parameterized box with optional through-hole')
    for k in ('length_mm','width_mm','height_mm','hole_diameter_mm'):
        if type(p[k]) not in (int,float) or not math.isfinite(p[k]) or not 0<=p[k]<=1000:raise ValueError('Finite CAD dimension 0..1000 mm required')
    if min(p[k] for k in ('length_mm','width_mm','height_mm'))<=0 or p['hole_diameter_mm']>=min(p['length_mm'],p['width_mm']):raise ValueError('Invalid box/hole dimensions')
    lo,hi=req['minimum_volume_mm3'],req['maximum_volume_mm3']
    if any(type(v) not in (int,float) or not math.isfinite(v) for v in (lo,hi)) or not 0<=lo<=hi<=1e9:raise ValueError('Invalid CAD volume criterion')
    return p,req

def run(folder):
    from . import cad_kernel
    import time
    (folder/'cad').mkdir(exist_ok=True)
    with (folder/'cad/timing.jsonl').open('a',encoding='utf-8') as f:f.write(json.dumps({'stage':'adapter_begin','time_ns':time.perf_counter_ns()})+'\n')
    p,req=validate(folder)
    # Match the dependency directory actually passed to the CAD subprocess.
    # Source verification can use the installed add-on without adding it to sys.path.
    addon=Path(os.environ.get('RLSOK_CAD_PATH') or ROOT/'thirdparty/cad/site-packages')
    if addon.exists():os.environ['RLSOK_CAD_PATH']=str(addon)
    import importlib.metadata
    version=next((d.version for d in importlib.metadata.distributions(path=[str(addon)]) if d.metadata['Name']=='cadquery-ocp-novtk'),None) if addon.exists() else None
    version=version or importlib.metadata.version('cadquery-ocp-novtk')
    props=cad_kernel.execute(folder,addon,timeout_s=24)
    expected=(p['length_mm']*p['width_mm']-math.pi*(p['hole_diameter_mm']/2)**2)*p['height_mm']
    kernel_ok=math.isclose(props['volume_mm3'],expected,rel_tol=1e-8,abs_tol=1e-6)
    passed=kernel_ok and req['minimum_volume_mm3']<=props['volume_mm3']<=req['maximum_volume_mm3']
    # Kernel/export result is an actual measurement; no solver-dynamics claim.
    result={'adapter':'cad.parametric/1','solver':{'name':'OpenCascade / OCP','version':props['ocp_version'],'package_version':version},
        'criteria':req,'measured':{'volume_mm3':props['volume_mm3'],'analytic_volume_mm3':expected},
        'checks':{'kernel_volume_matches_analytic':kernel_ok,'volume_requirement':passed,
                  'step_roundtrip_matches_kernel':math.isclose(props['step_roundtrip_volume_mm3'],props['volume_mm3'],rel_tol=1e-8,abs_tol=1e-6)},
        'passed':passed,'warnings':[],'levels':{'proposal':'RECORDED','artifact':'GENERATED','static':'PASSED_GEOMETRY' if kernel_ok else 'FAILED',
            'simulation':'NOT_APPLICABLE','requirements':'PASSED_GEOMETRY' if passed else 'FAILED','manufacturing':'NOT_REVIEWED','physical':'NOT_TESTED'},
        'limitations':['Restricted parametric solid only; arbitrary CAD Python not executable','No assembly, strength, tolerancing, manufacturing or physical validation']}
    with (folder/'cad/timing.jsonl').open('a',encoding='utf-8') as f:f.write(json.dumps({'stage':'requirements_evidence_begin','time_ns':time.perf_counter_ns()})+'\n')
    write(folder/'result.json',result)
    with (folder/'cad/timing.jsonl').open('a',encoding='utf-8') as f:f.write(json.dumps({'stage':'requirements_evidence_end','time_ns':time.perf_counter_ns()})+'\n')

def verify(folder,result):
    p,req=validate(folder);props=read(folder/'cad/cad_result.json')['structure']
    expected=(p['length_mm']*p['width_mm']-math.pi*(p['hole_diameter_mm']/2)**2)*p['height_mm']
    kernel=math.isclose(expected,props['volume_mm3'],rel_tol=1e-8,abs_tol=1e-6)
    if 'ocp_version' in props:
        if not math.isclose(props['step_roundtrip_volume_mm3'],props['volume_mm3'],rel_tol=1e-8,abs_tol=1e-6) or result['checks'].get('step_roundtrip_matches_kernel') is not True:
            raise ValueError('CAD STEP roundtrip evidence mismatch')
    if result['measured']!={'volume_mm3':props['volume_mm3'],'analytic_volume_mm3':expected} or result['criteria']!=req or result['passed']!=(kernel and req['minimum_volume_mm3']<=props['volume_mm3']<=req['maximum_volume_mm3']):raise ValueError('CAD evidence mismatch')
