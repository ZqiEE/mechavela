"""Local Robot Hub: real templates, private experiments and reviewed portable snapshots.

No internet service/identity/hosting is implied. Projects and solvers remain authoritative.
Shared packages contain selected current native assets, never execution journals or credentials.
"""
import io,json,math,re,time,uuid,zipfile
from pathlib import Path
from .projects import Projects,asset_name,digest,encode,read,write

TEMPLATES={
 'arm':('Desktop arm','robot','manipulator','Position two joints; change the target and run real MuJoCo dynamics.','Ideal actuators, unloaded planar arm. No hardware/manufacturing verification.'),
 'wheeled':('Wheeled explorer','robot','wheeled','Choose a waypoint and measure actual wheel/ground dynamics.','Assumed friction and mass, ideal drive and sensing; flat ground only.'),
 'bird':('Robotic bird · R2','robot','flying','Run a selected case using the original aerodynamic and mission engines.','Aerodynamics/attitude UNVERIFIED; one case is not full acceptance or real flight.'),
 'cad':('Parametric mounting plate','asset','mechanical','Edit dimensions, export actual STEP/STL and check geometric criteria.','Fixed primitive adapter; CAD first-use startup remains an open release gate.'),
 'electrical':('Voltage divider','asset','electrical','Reuse a real ngspice circuit and quantitative testbench.','Circuit simulation only; no PCB, manufacturing or physical qualification.')}
LICENSES=('All rights reserved','MIT','CC-BY-4.0','CERN-OHL-P-2.0')
SECRET=re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----|\b(?:sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16})\b|(?:api[_-]?key|access[_-]?token|password|authorization|client[_-]?secret|secret|token|credentials)[\s"\x27]*[:=][\s"\x27]*(?!null\b|false\b|["\x27\s]*(?:[,}\]]|$))[^\s,"\x27}\]]+',re.I)

class Hub:
    def __init__(self,data,store):
        self.root=Path(data)/'hub';self.store=store
    def _path(self,item):
        if not isinstance(item,str) or not re.fullmatch('[a-f0-9]{32}',item):raise ValueError('Invalid shared item')
        path=self.root/item
        if path.is_symlink():raise ValueError('Hub symlinks refused')
        return path
    def list(self):
        builtins=[dict(id='builtin-'+key,title=v[0],kind=v[1],morphology=v[2],description=v[3],limitations=v[4],origin='RLSOK reference',
            validation='REEXECUTION_REQUIRED',license='Reference; no redistribution license inferred',remix=True,versions=[],transport='bundled') for key,v in TEMPLATES.items()]
        shared=[self.get(p.name) for p in self.root.glob('*') if p.is_dir() and not p.is_symlink() and (p/'item.json').is_file()]
        for card in builtins:
            key=card['id'][8:]
            card['experiment']=self.experiment_spec({'kind':'joint_pd/1','target_rad':[.7,-.5]} if key=='arm' else {'kind':'differential-drive/1','goal_xy_m':[.6,0.]} if key=='wheeled' else {'kind':'bird-mission/1','case':'nominal'} if key=='bird' else {})
        return {'items':builtins+shared,'hosting':'LOCAL_ONLY','internet_publish':'NOT_CONFIGURED','message':'This computer’s Hub. Download a share package for another user to import. Local links are not public URLs.'}
    def get(self,item):
        p=read(self._path(item)/'item.json');return p
    def geometry(self,item):
        if item in ('builtin-arm','builtin-wheeled'):
            from . import reference,mobile
            xml=(reference if item=='builtin-arm' else mobile).MODEL
            return {'kind':'mjcf','xml':xml,'sha256':digest(xml.encode()),'scope':'Actual native MJCF geometry; not a simulation result'}
        if item=='builtin-bird':
            root=Path(__file__).resolve().parents[2]/'examples_builtin/bird/r2'
            return {'kind':'bird-rig','rig':read(root/'replay/index.json')['rig'],'scope':'Original R2 CAD meshes and design-derived assembly; geometry only, not flight validation'}
        if item.startswith('builtin-'):return {'kind':'unsupported','scope':'No supported robot geometry; open the native engineering asset'}
        with zipfile.ZipFile(io.BytesIO(self.package(item))) as z:
            p=json.loads(z.read('project.json'));name='revisions/'+p['revision']+'/robot.xml'
            if name in z.namelist():
                xml=z.read(name).decode();return {'kind':'mjcf','xml':xml,'sha256':digest(xml.encode()),'scope':'Shared native geometry, not independently validated'}
        return {'kind':'unsupported','scope':'No compatible geometry in this snapshot; no substitute robot shown'}
    @staticmethod
    def bird_mesh(name):
        root=Path(__file__).resolve().parents[2]/'examples_builtin/bird/r2';rig=read(root/'replay/index.json')['rig']
        if name not in {m['name'] for m in rig['meshes']}:raise ValueError('Unknown R2 mesh')
        return (root/'cad'/(name+'.stl')).read_bytes()
    @staticmethod
    def experiment_spec(controller):
        kind=controller.get('kind')
        if kind in ('joint_pd/1','differential-drive/1'):
            key='target_rad' if kind=='joint_pd/1' else 'goal_xy_m'
            return {'key':key,'value':controller[key],'units':'radians' if key=='target_rad' else 'metres'}
        if kind=='bird-mission/1':
            from implementation.bird.mission import cases
            return {'key':'case','value':controller['case'],'choices':[n for n,_,_ in cases(2)],'units':'registered flight case'}
        return None
    def package(self,item,version=None):
        p=self.get(item);version=version or p['version']
        if version not in p['versions'] or not re.fullmatch('[a-f0-9]{64}',version):raise ValueError('Unknown snapshot version')
        f=self._path(item)/'versions'/(version+'.zip')
        if f.is_symlink():raise ValueError('Snapshot symlinks refused')
        raw=f.read_bytes();self._unpack(raw);return raw
    def _template(self,key):
        from . import reference,mobile,bird,cad,electrical
        if key not in TEMPLATES:raise ValueError('Unknown reference')
        if key=='bird':p=bird.create(self.store)
        elif key=='wheeled':p=mobile.create(self.store)
        else:p=reference.create(self.store) if key=='arm' else self.store.create(TEMPLATES[key][0],TEMPLATES[key][3],TEMPLATES[key][2],(cad if key=='cad' else electrical).defaults())
        if key in ('arm','wheeled'):
            c=json.loads(self.store.asset(p['id'],'controller.json'))
            c.update({'kp':2.,'kd':.4} if key=='arm' else {'max_wheel_rad_s':12.})
            self.store.revise(p['id'],{'controller.json':encode(c).decode()},'Hub playable preset; not an AI decision or inherited simulation pass')
        return self.store.get(p['id'])
    def create(self,item,parameters=None):
        if not isinstance(item,str):raise ValueError('Choose a Hub item')
        if item.startswith('builtin-'):p=self._template(item[8:]);origin={'item':item,'license':'Reference; redistribution rights not inferred'}
        else:
            card=self.get(item)
            if not card['remix']:raise PermissionError('Publisher does not permit remixing this snapshot')
            raw=self.package(item);p=self.store.import_archive(raw);origin={'item':item,'version':card['version'],'author':card['author'],'license':card['license'],'upstream':card.get('attribution')}
        meta=read(self.store.path(p['id'])/'project.json');meta['attribution']=origin;meta['hub_origin']=item
        write(self.store.path(p['id'])/'project.json',meta)
        if parameters:self.experiment(p['id'],p['revision'],parameters)
        return self.store.get(p['id'])
    def experiment(self,pid,expected,parameters):
        if not isinstance(parameters,dict):raise ValueError('Scenario parameters must be an object')
        p=self.store.get(pid)
        if p['revision']!=expected:raise ValueError('Revision conflict: reload before changing an experiment')
        if 'controller.json' not in p['assets']:raise ValueError('This asset uses its native file editor')
        c=json.loads(self.store.asset(pid,'controller.json'));kind=c.get('kind')
        key='target_rad' if kind=='joint_pd/1' else 'goal_xy_m' if kind=='differential-drive/1' else 'case' if kind=='bird-mission/1' else None
        if key is None or set(parameters)!={key}:raise ValueError('Only the supported scenario target/case can be changed here')
        value=parameters[key]
        if key=='case':
            from implementation.bird.mission import cases
            if value not in {n for n,_,_ in cases(2)}:raise ValueError('Unknown registered flight case')
        elif not isinstance(value,list) or len(value)!=2 or not all(type(v) in (int,float) and math.isfinite(v) and -1.5<=v<=1.5 for v in value):raise ValueError('Choose two finite coordinates between -1.5 and 1.5')
        c[key]=value
        return self.store.revise(pid,{'controller.json':encode(c).decode()},'User-selected Hub experiment; acceptance thresholds unchanged',expected=expected)
    def review(self,pid,expected,title,description,author,license,assets):
        p=self.store.get(pid)
        if p['revision']!=expected:raise ValueError('Revision conflict: review the latest design')
        if any(r['state'] in ('QUEUED','RUNNING') for r in p['runs']):raise ValueError('Wait for active runs before sharing')
        if license not in LICENSES:raise ValueError('Choose an explicit publication license')
        for value,maximum in ((title,160),(description,4000),(author,160)):
            if not isinstance(value,str) or not value.strip() or len(value)>maximum:raise ValueError('Title, description and creator are required and bounded')
        if not isinstance(assets,list) or not 1<=len(assets)<=32 or len(set(assets))!=len(assets):raise ValueError('Select 1..32 native files')
        contents={}
        for n in assets:
            asset_name(n)
            if n not in p['assets']:raise ValueError('Select current project files only')
            raw=self.store.asset(pid,n).encode()
            if SECRET.search(raw.decode()):raise ValueError('Possible credential/private key in '+n+'; remove it or exclude the file')
            contents[n]=raw
        summaries=[{'adapter':r['adapter'],'revision':r['revision'],'solver':r['result'].get('solver'),'measured':r['result'].get('measured'),
            'criteria':r['result'].get('criteria'),'passed':r['result'].get('passed'),'levels':r['result'].get('levels')} for r in p['runs'] if r['revision']==p['revision'] and r.get('result')]
        card={'schema':'rlsok.hub-card/1','title':title,'description':description,'author':author,'license':license,'remix':license!='All rights reserved',
            'kind':'robot' if 'controller.json' in contents else 'asset','morphology':p['morphology'],'source_revision':p['revision'],
            'attribution':p.get('attribution'),'validation':'PUBLISHER_SUMMARY_REEXECUTION_REQUIRED','evidence_status':p['evidence_status'],
            'summaries':summaries,'evidence_scope':'Summary only. Full journals, original attachments, source history and credentials are excluded. Rerun to establish independent evidence.',
            'files':{n:digest(b) for n,b in sorted(contents.items())}}
        card['experiment']=self.experiment_spec(json.loads(contents['controller.json'])) if 'controller.json' in contents else None
        from .engineering import ADAPTERS,select
        try:spec=select(json.loads(contents['controller.json']),contents) if 'controller.json' in contents else ADAPTERS['cad.parametric/1'] if 'cad.json' in contents else ADAPTERS['ngspice.circuit/1'] if 'circuit.json' in contents else None
        except ValueError:spec=None
        missing=sorted(set(spec.inputs+spec.extra_inputs)-contents.keys()) if spec else []
        card['simulation']={'adapter':spec.id if spec else None,'ready':bool(spec and not missing),'missing_inputs':missing,'scope':'Input compatibility only; dependencies and actual execution are checked on the receiving computer.'}
        card['asset_sources']=p.get('asset_sources',[])
        if SECRET.search(encode(card).decode()):raise ValueError('Possible credential in publication metadata; review it before sharing')
        fingerprint=digest(encode(card))
        return {'card':card,'review':fingerprint,'bytes':sum(map(len,contents.values())),'files':list(contents),'excluded':['Previous revisions','Original attachments','Model prompts and logs','Tool grants and credentials','Solver execution paths/logs'],'transport':'LOCAL_ONLY'},contents
    def publish(self,**data):
        rights=data.pop('rights_confirmed',False);review=data.pop('review',None)
        if rights is not True:raise PermissionError('Confirm that you reviewed the selected files and have rights to distribute them')
        with self.store.lock:
            preview,contents=self.review(**data)
            if preview['review']!=review:raise ValueError('Share review changed; review again')
            owners=read(self.root/'owners.json') if (self.root/'owners.json').exists() else {}
            card=preview['card'];item=owners.get(data['pid']) or uuid.uuid4().hex;version=digest(encode(card));card.update(id=item,version=version)
            # A minimal standard project archive; existing safe importer and solver flows are reused.
            tmp={'parent':None,'sequence':1,'reason':'Reviewed Hub snapshot','assets':card['files']};revision=digest(encode(tmp));pid=item
            files={'project.json':encode({'schema':'rlsok.robot-project/1','id':pid,'name':card['title'],'intent':card['description'],'morphology':card['morphology'],'visibility':'private','license':card['license'],'revision':revision}),
                'hub-card.json':encode(card),'revisions/'+revision+'/revision.json':encode(dict(tmp,id=revision))}
            files.update({'revisions/'+revision+'/'+n:b for n,b in contents.items()})
            raw=self._pack(files);self._save(card,raw,'local publication')
            owners[data['pid']]=item;write(self.root/'owners.json',owners)
            return self.get(item)
    def _save(self,card,raw,origin):
        p=self._path(card['id']);p.mkdir(parents=True,exist_ok=True);folder=p/'versions';folder.mkdir(exist_ok=True)
        dest=folder/(card['version']+'.zip')
        if dest.exists() and dest.read_bytes()!=raw:raise ValueError('Immutable publication collision')
        if not dest.exists():dest.write_bytes(raw)
        meta=dict(card,versions=[card['version']],origin=origin,transport='LOCAL_ONLY',published_at=time.time())
        if (p/'item.json').exists():
            old=read(p/'item.json');meta['versions']=list(dict.fromkeys(old['versions']+meta['versions']))
        write(p/'item.json',meta)
    @staticmethod
    def _pack(files):
        buf=io.BytesIO()
        with zipfile.ZipFile(buf,'w',zipfile.ZIP_DEFLATED) as z:
            for n,b in sorted(dict(files,**{'MANIFEST.json':encode({n:digest(b) for n,b in files.items()})}).items()):
                info=zipfile.ZipInfo(n,date_time=(1980,1,1,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED;z.writestr(info,b)
        return buf.getvalue()
    @staticmethod
    def _unpack(raw):
        if len(raw)>4000000:raise ValueError('Share package exceeds 4MB')
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            info=z.infolist();names=[i.filename for i in info]
            if len(names)>100 or sum(i.file_size for i in info)>8000000 or len(set(names))!=len(names):raise ValueError('Share archive limits/duplicates')
            if any('\\' in n or ':' in n or n.startswith('/') or '..' in n.split('/') for n in names):raise ValueError('Unsafe share path')
            manifest=json.loads(z.read('MANIFEST.json'));card=json.loads(z.read('hub-card.json'));project=json.loads(z.read('project.json'))
            if card.get('schema')!='rlsok.hub-card/1' or project.get('schema')!='rlsok.robot-project/1':raise ValueError('Unsupported share schema')
            if not re.fullmatch('[a-f0-9]{32}',card.get('id','')) or not re.fullmatch('[a-f0-9]{64}',card.get('version','')):raise ValueError('Invalid share identity')
            canonical={k:v for k,v in card.items() if k not in ('id','version')}
            if digest(encode(canonical))!=card['version']:raise ValueError('Card fingerprint mismatch')
            rev=project['revision']
            if not isinstance(rev,str) or not re.fullmatch('[a-f0-9]{64}',rev):raise ValueError('Invalid snapshot revision')
            for n in card['files']:asset_name(n)
            if not 1<=len(card['files'])<=32:raise ValueError('Native file count limit')
            expected={'hub-card.json','project.json','revisions/'+rev+'/revision.json'}|{'revisions/'+rev+'/'+n for n in card['files']}
            if set(manifest)!=expected or set(names)!=expected|{'MANIFEST.json'}:raise ValueError('Unexpected files; only reviewed native assets may be shared')
            for n,h in manifest.items():
                b=z.read(n)
                if digest(b)!=h:raise ValueError('Share file hash mismatch')
                if SECRET.search(b.decode('utf-8')):raise ValueError('Possible credentials in share package')
            r=json.loads(z.read('revisions/'+rev+'/revision.json'))
            if r['assets']!=card['files'] or digest(encode({k:v for k,v in r.items() if k!='id'}))!=rev:raise ValueError('Revision mismatch')
            for n,h in card['files'].items():
                b=z.read('revisions/'+rev+'/'+n)
                if len(b)>256000 or digest(b)!=h:raise ValueError('Native asset size/hash mismatch')
            if sum(z.getinfo('revisions/'+rev+'/'+n).file_size for n in card['files'])>2000000:raise ValueError('Project native text limit')
            if project['license']!=card['license'] or project['name']!=card['title'] or project['morphology']!=card['morphology']:raise ValueError('Project/card mismatch')
            if card['license'] not in LICENSES or card['remix']!=(card['license']!='All rights reserved'):raise ValueError('Inconsistent license/remix permission')
            return card
    def import_package(self,raw):
        try:card=self._unpack(raw)
        except (zipfile.BadZipFile,UnicodeError,TypeError) as error:raise ValueError('Invalid share archive/schema') from error
        with self.store.lock:
            if (self._path(card['id'])/'item.json').exists():
                if self.package(card['id'])!=raw:raise ValueError('Snapshot identity collision; existing publication preserved')
                return self.get(card['id'])
            self._save(card,raw,'Imported · publisher identity and claims unverified')
        return self.get(card['id'])

    def import_project(self,raw):
        """One existing Open/Import command handles both private exports and Hub packages."""
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as z:shared='hub-card.json' in z.namelist()
        except zipfile.BadZipFile as error:raise ValueError('Invalid project archive') from error
        if not shared:return self.store.import_archive(raw)
        card=self.import_package(raw)
        return self.create(card['id'])

    def use_asset(self,item,pid,expected):
        from . import cad,electrical
        if item in ('builtin-cad','builtin-electrical'):
            values=(cad if item=='builtin-cad' else electrical).defaults();origin={'item':item,'license':'Reference; redistribution rights not inferred'}
        else:
            card=self.get(item)
            if card['kind']!='asset' or not card['remix']:raise PermissionError('Choose a reusable asset with remix permission')
            raw=self.package(item)
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                rev=json.loads(z.read('project.json'))['revision'];values={n:z.read('revisions/'+rev+'/'+n).decode() for n in card['files']}
            origin={'item':item,'version':card['version'],'author':card['author'],'license':card['license']}
        if {'controller.json','requirements.json','robot.xml'}&values.keys():raise ValueError('Robot definitions require Remix, not asset insertion')
        with self.store.lock:
            p=self.store.get(pid)
            if set(values)&p['assets'].keys():raise ValueError('Asset names already exist; originals were preserved')
            self.store.revise(pid,values,'User reused Hub engineering asset '+item,expected=expected)
            meta=read(self.store.path(pid)/'project.json');meta.setdefault('asset_sources',[]).append(dict(origin,files={n:digest(v.encode()) for n,v in values.items()}));write(self.store.path(pid)/'project.json',meta)
        return self.store.get(pid)
