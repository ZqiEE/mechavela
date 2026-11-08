"""Explicit bounded CAD dependency readiness; never a geometry-validation result.

Native loader initialization is separately recorded, not charged to the unchanged
24-second CAD child deadline. Only this fixed initialization has a 60s wall cap.
The engineering worker retains its original 15CPU/30wall/1GiB/16MiB limits.
"""
import hashlib,json,os,sys
from pathlib import Path
from .projects import write

LIMITS={'cpu_seconds':15,'wall_seconds':60,'memory_bytes':1073741824,'disk_bytes':16777216}

def identity(addon):
    native=addon/'OCP/OCP.cp312-win_amd64.pyd'
    with native.open('rb') as f:sha=hashlib.file_digest(f,'sha256').hexdigest()
    files=[addon/'OCP/__init__.py',*sorted((addon/'cadquery_ocp_novtk.libs').glob('*.dll'))]
    dependencies={str(p.relative_to(addon)):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    return {'path':str(addon.resolve()),'native_sha256':sha,'dependencies':dependencies}

def prepare(folder,addon,env,cancel):
    from implementation.windows_process import run
    from . import cad_kernel
    output=folder/'cad-preparation';output.mkdir()
    key=identity(addon)
    argv=[sys.executable,'-I','-B',str(Path(cad_kernel.__file__).resolve()),'--prepare',str(output),str(addon)]
    receipt=run(
        argv,output/'execution',15,60,1073741824,env=env,disk_root=output,disk_cap=16777216,cancel=cancel)
    record={'schema':'rlsok.cad-preparation/1','limits':LIMITS,'identity':key,'execution':receipt,
            'engineering_validation':'NOT_RUN','state':'FAILED'}
    if not receipt['limit_event'] and receipt['exit_code']==0 and receipt['all_job_processes_quiescent']:
        ready=json.loads((output/'ready.json').read_text(encoding='utf-8'))
        if ready['state']=='DEPENDENCIES_LOADED' and ready['geometry_generated'] is False:
            record.update(state='READY',native=ready)
    write(folder/'cad-preparation.json',record)
    return record
