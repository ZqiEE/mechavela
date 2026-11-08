"""One source product identity and non-destructive project-library migration."""
import shutil,os
from pathlib import Path
from .projects import Projects,read,write,digest

VERSION=(Path(__file__).resolve().parents[2]/'VERSION').read_text(encoding='ascii').strip()

def storage_root():
    return Path(os.environ.get('RLSOK_CONSOLE_DATA') or
        Path(os.environ.get('LOCALAPPDATA',Path.home()/'.local/share'))/'RLSOK/data').resolve()

def migrate_projects(source,target):
    """Explicit local operator command. Copy exact bytes; never merge/overwrite differing IDs."""
    source,target=Path(source).resolve(),Path(target).resolve()
    if source==target:raise ValueError('Source and target must differ')
    src,dst=Projects(source),Projects(target)
    results=[]
    def inventory(base):
        files={}
        for f in base.rglob('*'):
            if f.is_symlink():raise ValueError('Migration refuses symlinks')
            if f.is_file():files[f.relative_to(base).as_posix()]=digest(f.read_bytes())
        return files
    for p in src.list():
        if any(r['state'] in ('RUNNING','QUEUED') for r in p['runs']):raise ValueError('Stop active source jobs before migration')
        original=src.path(p['id']);dest=dst.path(p['id']);before=inventory(original)
        if dest.exists():
            state='ALREADY_IDENTICAL' if inventory(dest)==before else 'CONFLICT_PRESERVED_BOTH'
        else:
            temp=target/(p['id']+'.migration')
            if temp.exists():raise ValueError('Previous incomplete migration requires inspection: '+str(temp))
            shutil.copytree(original,temp)
            if inventory(temp)!=before:raise ValueError('Migration copy verification failed')
            temp.rename(dest);state='COPIED_EXACT'
        if inventory(original)!=before:raise ValueError('Source changed during migration')
        results.append({'project':p['id'],'state':state,'files':len(before)})
    report={'source':str(source),'target':str(target),'projects':results,'source_preserved':True}
    return report

if __name__=='__main__':
    import argparse,json
    ap=argparse.ArgumentParser();ap.add_argument('--source',required=True);ap.add_argument('--target',required=True)
    a=ap.parse_args();print(json.dumps(migrate_projects(a.source,a.target),indent=2))
