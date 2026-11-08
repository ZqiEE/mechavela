"""Owner-selected external CAD host. Project assets cannot select an executable."""
import hashlib,json,os,subprocess
from pathlib import Path
from .projects import read,write,digest

def configured(store):
    path=store.root.parent/'cad-runtime.json'
    return path if path.is_file() else None

def validate(path):
    path=Path(path);data=read(path)
    if data.get('schema')!='rlsok.freecad-host/1' or data.get('provider')!='freecad':raise ValueError('Invalid owner CAD runtime configuration')
    root=Path(data['root'])
    if not root.is_absolute() or not root.is_dir():raise ValueError('Absolute existing CAD host directory required')
    files=data.get('native_files')
    if not isinstance(files,dict) or not files or len(files)>4096:raise ValueError('Pinned qualified native file identities required')
    actual_native=set()
    for file in root.rglob('*'):
        if file.is_symlink() or getattr(file,'is_junction',lambda:False)():raise ValueError('Linked CAD payload refused')
        if not file.is_file():continue
        with file.open('rb') as stream:magic=stream.read(2)
        if magic==b'MZ' or file.suffix.lower() in ('.exe','.dll','.pyd','.node','.ocx','.sys'):actual_native.add(file.relative_to(root).as_posix())
    if actual_native!=set(files):raise ValueError('Qualified CAD native inventory changed; no automatic fallback')
    for name,expected in files.items():
        if not isinstance(name,str) or '\\' in name or ':' in name or name.startswith('/') or any(p in ('','..','.') for p in name.split('/')):raise ValueError('Invalid CAD runtime identity path')
        file=root/name
        if any(p.is_symlink() or getattr(p,'is_junction',lambda:False)() for p in (file,*file.parents)):raise ValueError('Linked CAD runtime refused')
        if not isinstance(expected,str) or len(expected)!=64:raise ValueError('Invalid CAD native SHA256')
        with file.open('rb') as stream:actual=hashlib.file_digest(stream,'sha256').hexdigest()
        if actual!=expected:raise ValueError('Qualified CAD runtime changed: '+name)
    if 'bin/freecadcmd.exe' not in files:raise ValueError('Qualified FreeCAD command host missing')
    qualification=data.get('qualification')
    if not isinstance(qualification,dict) or not qualification.get('receipt_sha256'):raise ValueError('Native qualification identity required')
    return data

def inspect_cad(profile,source,out,spec,scene,mesh=False):
    profile=Path(profile);data=read(profile);out=Path(out)
    job=out/'freecad-job.json';write(job,{'source':str(Path(source).resolve()),'out':str(out.resolve()),'checks':spec,'scene':scene,'meshes':mesh})
    env={k:v for k,v in os.environ.items() if k.upper() in ('SYSTEMROOT','WINDIR','PATH','TEMP','TMP','COMSPEC','HOME','USERPROFILE','APPDATA','LOCALAPPDATA')}
    env.update(RLSOK_CAD_JOB=str(job.resolve()),OMP_NUM_THREADS='1',OPENBLAS_NUM_THREADS='1',PYTHONNOUSERSITE='1')
    worker=Path(__file__).with_name('freecad_worker.py')
    with (out/'freecad-stdout.log').open('wb') as stdout,(out/'freecad-stderr.log').open('wb') as stderr:
        result=subprocess.run([str(Path(data['root'])/'bin/freecadcmd.exe'),'--safe-mode',str(worker)],env=env,stdout=stdout,stderr=stderr)
    if result.returncode or not (out/'cad-report.json').is_file():
        error=out/'freecad-error.json'
        detail=read(error).get('error','') if error.is_file() else 'Native host exited '+str(result.returncode)
        raise RuntimeError('FreeCAD CAD inspection failed; no legacy fallback: '+detail[:1500])
    report=read(out/'cad-report.json');parts=[MeshPart(row,out) for row in report['parts']]
    return report,parts

class MeshPart:
    """Serialized native tessellation for the unchanged MuJoCo binding interface."""
    def __init__(self,row,out):self.label=row['name'];self.color=row.get('color');self.path=Path(out)/'meshes'/(row['id']+'.json')
    def tessellate(self,*args):
        mesh=read(self.path);return mesh['vertices'],mesh['faces']
