"""Versioned native assets with immutable revisions and explicit evidence levels."""
from __future__ import annotations
import hashlib
import io
import json
import os
import re
import threading
import time
import uuid
import zipfile
from pathlib import Path

LEVELS = ('proposal', 'artifact', 'static', 'simulation', 'requirements', 'manufacturing', 'physical')

def digest(raw):
    return hashlib.sha256(raw).hexdigest()

def encode(value):
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8')

def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    tmp.write_bytes(encode(value))
    for attempt in range(5):
        try:
            tmp.replace(path)
            break
        except PermissionError:
            if attempt == 4: raise
            time.sleep(.01)

def read(path):
    for attempt in range(5):
        try:
            return json.loads(Path(path).read_text(encoding='utf-8'))
        except PermissionError:
            if attempt == 4: raise
            time.sleep(.01)

def asset_name(name):
    legacy=isinstance(name,str) and re.fullmatch(r'[a-zA-Z0-9_-]+\.(json|xml|urdf|md|csv|txt)',name)
    code=isinstance(name,str) and len(name)<=180 and name.startswith('code/') and all(re.fullmatch(r'[a-zA-Z0-9_.-]+',s) and s not in ('.','..') for s in name.split('/')) and re.fullmatch(r'.+\.(py|json|xml|urdf|md|csv|txt|yaml|yml|toml|js|ts|cpp|h|c|rs|sh|bat|cmd|ini|cfg|obj|mtl)',name)
    if not (legacy or code):
        raise ValueError('Use a flat engineering asset or a relative text/source file under code/')
    if code and any(s.endswith('.') or s.split('.')[0].upper() in {'CON','PRN','AUX','NUL',*('COM'+str(i) for i in range(1,10)),*('LPT'+str(i) for i in range(1,10))} for s in name.split('/')):
        raise ValueError('Reserved Windows source path')
    return name

def attachment_name(name):
    if (not isinstance(name,str) or len(name)>250 or '\\' in name or ':' in name
        or any(not re.fullmatch(r'[a-zA-Z0-9_. -]+',s) or s in ('.','..') for s in name.split('/'))):
        raise ValueError('Unsafe attachment name')
    return name

class Projects:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()

    def path(self, pid):
        if not isinstance(pid, str) or not re.fullmatch('[a-f0-9]{32}', pid):
            raise ValueError('Invalid project id')
        path = self.root / pid
        if any(x.is_symlink() for x in (path,*path.parents)):
            raise ValueError('Project symlinks refused')
        return path

    def create(self, name, intent, morphology='custom', assets=None, domains=None):
        if not isinstance(name, str) or not name.strip() or not isinstance(intent, str):
            raise ValueError('Project name and literal requirements are required')
        if not isinstance(morphology, str) or len(morphology) > 100:
            raise ValueError('Morphology must be an extensible text tag')
        pid = uuid.uuid4().hex
        p = {'schema': 'rlsok.robot-project/1', 'id': pid, 'name': name[:160],
             'intent': intent[:20000], 'morphology': morphology, 'visibility': 'private',
             'license': 'UNSPECIFIED', 'domains': domains or {}, 'revision': None,
             'origin':{'library':'test' if os.environ.get('RLSOK_PROJECT_CONTEXT')=='test' else 'personal',
                       'source':'Explicit project creation in the selected application library'}}
        with self.lock:
            self.path(pid).mkdir()
            write(self.path(pid) / 'project.json', p)
            self.revise(pid, assets or {'requirements.md': intent}, 'Initial robot project')
        return self.get(pid)

    def get(self, pid):
        p = read(self.path(pid) / 'project.json')
        rev = read(self.path(pid) / 'revisions' / p['revision'] / 'revision.json')
        p['assets'] = rev['assets']
        p['history'] = sorted([read(x) for x in (self.path(pid) / 'revisions').glob('*/revision.json')], key=lambda h: h.get('sequence', 0))
        p['runs'] = sorted([read(x) for x in (self.path(pid) / 'runs').glob('*/run.json')], key=lambda r: r.get('sequence', 0))
        p['play_sessions']=sorted([read(x) for x in (self.path(pid)/'play').glob('*/session.json')],key=lambda s:s.get('created_ns',0))
        missing=[]
        for run in p['runs']:
            for source in run.get('tool_sources',{}):
                name=Path(source.replace('\\','/')).name+'.txt'
                if not (self.path(pid)/'runs'/run['id']/'tool_sources'/name).is_file():missing.append({'run':run['id'],'source':name})
        p['evidence_status']='INCOMPLETE_MISSING_SOURCE' if missing else 'NOT_RECENTLY_VERIFIED'
        p['evidence_warnings']=missing
        p['agent_history'] = sorted([read(x) for x in (self.path(pid) / 'agent').glob('*.json')],key=lambda h:h.get('sequence',0))
        p['agent_sessions'] = []
        p['mcp_activity']=[]
        for path in (self.path(pid)/'agent/mcp').glob('*.json'):
            call=read(path)
            p['mcp_activity'].append({k:call.get(k) for k in ('id','connection','tool','state','revision_before','revision_after','started_ns','ended_ns','error')})
        p['mcp_activity']=sorted(p['mcp_activity'],key=lambda c:c['started_ns'])[-64:]
        for path in sorted((self.path(pid)/'agent'/'sessions').glob('*.json'),key=lambda x:x.stat().st_mtime_ns):
            session=read(path)
            p['agent_sessions'].append({k:session.get(k) for k in ('id','state','model_requested','turns','usage','wall_seconds')})
        return p

    def list(self):
        return [self.get(x.name) for x in self.root.iterdir() if x.is_dir() and not x.is_symlink()
                and (x / 'project.json').exists() and read(x / 'project.json').get('revision')]

    def attach(self,pid,files,source):
        """Append immutable historical bytes; never promote claims to fresh validation."""
        with self.lock:
            p=read(self.path(pid)/'project.json'); old=p.get('attachments',{})
            if len(old)+len(files)>2000 or sum(m['bytes'] for m in old.values())+sum(len(b) for b in files.values())>64000000:
                raise ValueError('Historical attachment limit')
            for name,raw in files.items():
                attachment_name(name)
                if name in old:raise ValueError('Attachment already exists; originals are immutable')
                if not isinstance(raw,bytes):raise ValueError('Attachment requires bytes')
            for name,raw in files.items():
                path=self.path(pid)/'attachments'/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
                old[name]={'sha256':digest(raw),'bytes':len(raw),'source':source,'validation':'HISTORICAL_NOT_REEXECUTED'}
            p['attachments']=old;write(self.path(pid)/'project.json',p)

    def attachment(self,pid,name):
        attachment_name(name)
        p=read(self.path(pid)/'project.json')
        if name not in p.get('attachments',{}):raise ValueError('Unknown project attachment')
        base=self.path(pid)/'attachments';path=base/name
        if any(x.is_symlink() for x in (path,*path.parents)):raise ValueError('Attachment symlinks refused')
        raw=path.read_bytes()
        if digest(raw)!=p['attachments'][name]['sha256']:raise ValueError('Historical attachment changed')
        return raw

    def asset(self, pid, name, revision=None):
        p = read(self.path(pid) / 'project.json')
        revision = revision or p['revision']
        if not re.fullmatch('[a-f0-9]{64}', revision):
            raise ValueError('Invalid revision')
        path = self.path(pid) / 'revisions' / revision / asset_name(name)
        if any(x.is_symlink() or (hasattr(x,'is_junction') and x.is_junction()) for x in (path,*path.parents)):
            raise ValueError('Asset symlinks refused')
        # Text assets are content-addressed. Preserve CRLF bytes on retrieval too.
        return path.read_bytes().decode('utf-8')

    def revise(self, pid, changes, reason, expected=None, replace=False):
        if not isinstance(changes, dict) or not changes or len(changes) > 32:
            raise ValueError('Provide 1..32 changed assets')
        with self.lock:
            p = read(self.path(pid) / 'project.json')
            if expected is not None and p['revision'] != expected:
                raise ValueError('Revision conflict: reload before saving')
            contents = {}
            if p['revision']:
                prev = read(self.path(pid) / 'revisions' / p['revision'] / 'revision.json')
                contents = {n: self.asset(pid, n) for n in prev['assets']}
            if replace:contents={}
            for n, value in changes.items():
                asset_name(n)
                if not isinstance(value, str) or len(value.encode()) > 256000:
                    raise ValueError('Assets must be UTF-8 text, at most 256KB each')
                contents[n] = value
            if sum(len(v.encode()) for v in contents.values()) > 2000000:
                raise ValueError('Project text limit is 2MB')
            folded={n.casefold() for n in contents}
            if len(folded)!=len(contents) or any('/'.join(n.split('/')[:i]) in folded for n in folded for i in range(1,len(n.split('/')))):
                raise ValueError('Conflicting file paths on Windows')
            hashes = {n: digest(v.encode()) for n, v in sorted(contents.items())}
            sequence = prev.get('sequence', 0) + 1 if p['revision'] else 1
            meta = {'parent': p['revision'], 'sequence': sequence, 'reason': str(reason)[:1000], 'assets': hashes}
            rev = digest(encode(meta))
            dest = self.path(pid) / 'revisions' / rev
            dest.mkdir(parents=True, exist_ok=False)
            for n, v in contents.items():
                (dest / n).parent.mkdir(parents=True,exist_ok=True)
                (dest / n).write_text(v, encoding='utf-8', newline='')
            write(dest / 'revision.json', dict(meta, id=rev))
            p['revision'] = rev
            write(self.path(pid) / 'project.json', p)
        return rev

    def restore(self,pid,revision,expected):
        if not re.fullmatch('[a-f0-9]{64}',revision):raise ValueError('Invalid revision')
        old=read(self.path(pid)/'revisions'/revision/'revision.json')
        return self.revise(pid,{n:self.asset(pid,n,revision) for n in old['assets']},
            'User restored revision '+revision,expected=expected,replace=True)

    def export(self, pid):
        """Export only project records; never settings, credentials or arbitrary paths."""
        base = self.path(pid)
        p = self.get(pid)
        if any(r['state'] in ('RUNNING', 'QUEUED') for r in p['runs']):
            raise ValueError('Wait for active runs before exporting a coherent evidence snapshot')
        files = {}
        for f in base.rglob('*'):
            if f.name=='local-code-approval.json':continue
            if f.is_symlink():
                raise ValueError('Symlinks cannot be exported')
            if f.is_file() and (f.suffix in ('.json', '.jsonl', '.xml', '.urdf', '.md', '.csv', '.txt', '.log','.gz','.stl','.step','.cir','.py','.glb','.gltf','.bin','.brep','.mjz','.stp') or f.relative_to(base).parts[0]=='attachments' or 'code' in f.relative_to(base).parts or 'artifacts' in f.relative_to(base).parts):
                files[f.relative_to(base).as_posix()] = f.read_bytes()
        out = io.BytesIO()
        if 'motor-source.json' in p['assets']:
            # Preserve the upstream license when redistributing the bundled BAM parameter snapshot.
            license_file = Path(__file__).resolve().parents[2] / 'robot_assets/motors/LICENSE.bam'
            files['LICENSE-bam.txt'] = license_file.read_bytes()
        with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
            for name, raw in sorted(files.items()):
                z.writestr(name, raw)
            z.writestr('MANIFEST.json', encode({n: digest(raw) for n, raw in sorted(files.items())}))
        return out.getvalue()

    def import_archive(self, raw):
        """Import current native assets; imported run claims never become new validation."""
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            entries = z.infolist()
            if len(entries) > 4000 or sum(i.file_size for i in entries) > 64000000:
                raise ValueError('Archive size/member limit exceeded')
            names = [i.filename for i in entries]
            if len(set(names)) != len(names) or any(n.startswith(('/', '\\')) or '..' in n.split('/') or '\\' in n for n in names):
                raise ValueError('Unsafe or duplicate archive names')
            manifest = json.loads(z.read('MANIFEST.json'))
            if set(manifest) != set(names) - {'MANIFEST.json'}:
                raise ValueError('Manifest member mismatch')
            for n, sha in manifest.items():
                if digest(z.read(n)) != sha:
                    raise ValueError('Archive hash mismatch')
            p = json.loads(z.read('project.json'))
            if p.get('schema') != 'rlsok.robot-project/1':
                raise ValueError('Unsupported project schema')
            revision = p['revision']
            if not re.fullmatch('[a-f0-9]{64}', revision):
                raise ValueError('Invalid imported revision')
            rev = json.loads(z.read('revisions/' + revision + '/revision.json'))
            values = {asset_name(n): z.read('revisions/' + revision + '/' + asset_name(n)).decode('utf-8') for n in rev['assets']}
            child=self.create(p['name'] + ' (imported)', p['intent'], p['morphology'], values, p.get('domains'))
            # Preserve original archive byte-for-byte, including prior revisions/receipts, as
            # historical evidence. It cannot create executable tasks or inherit a fresh pass.
            self.attach(child['id'],{'imports/'+digest(raw)+'.zip':raw},'User imported archive; claims untrusted until inspected/reexecuted')
            original={n[len('attachments/'):]:z.read(n) for n in names if n.startswith('attachments/')}
            if original:self.attach(child['id'],original,'Preserved imported historical artifacts')
            return self.get(child['id'])

    def fork(self, pid, name):
        p = self.get(pid)
        child = self.create(name, p['intent'], p['morphology'],
                            {n: self.asset(pid, n) for n in p['assets']}, p['domains'])
        meta = read(self.path(child['id']) / 'project.json')
        meta['attribution'] = {'project': pid, 'revision': p['revision'], 'license': p['license']}
        write(self.path(child['id']) / 'project.json', meta)
        if p.get('attachments'):
            self.attach(child['id'],{n:self.attachment(pid,n) for n in p['attachments']},'Fork attribution: '+pid)
        return self.get(child['id'])
