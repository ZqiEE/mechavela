"""Bounded live MuJoCo play, separated from engineering requirement validation."""
import json,math,os,sys,threading,time,uuid,platform
from pathlib import Path
from .projects import read,write,digest
from .compute import LocalCompute,Resources
ROOT=Path(__file__).resolve().parents[2]

class Play:
    def __init__(self,store):
        self.store=store;self.lock=threading.RLock();self.jobs={}
        for p in store.list():
            for f in (store.path(p['id'])/'play').glob('*/session.json'):
                r=read(f)
                if r['state'] in ('STARTING','RUNNING'):r.update(state='INTERRUPTED',reason='Application restarted; explicit Play required');write(f,r)
    def folder(self,pid,sid):
        import re
        if not isinstance(sid,str) or not re.fullmatch('[a-f0-9]{32}',sid):raise ValueError('Invalid Play session')
        folder=self.store.path(pid)/'play'/sid
        if folder.is_symlink() or folder.parent.is_symlink():raise ValueError('Play symlinks refused')
        return folder
    def start(self,pid,seconds=24):
        if type(seconds) not in (int,float) or not math.isfinite(seconds) or not 1<=seconds<=24:raise ValueError('Play duration must be 1..24 seconds')
        with self.lock,self.store.lock:
            if any(not j['done'].is_set() for j in self.jobs.values()):raise ValueError('Stop the current Play session first')
            p=self.store.get(pid);c=json.loads(self.store.asset(pid,'controller.json'))
            if c.get('kind') not in ('joint_pd/1','differential-drive/1') or 'actuators.json' in p['assets']:raise ValueError('Live Play supports ideal two-joint arms and differential-drive references only; use engineering validation for specialized models')
            sid=uuid.uuid4().hex;folder=self.folder(pid,sid);folder.mkdir(parents=True)
            inputs={n:p['assets'][n] for n in ('robot.xml','controller.json','requirements.json')}
            for n,h in inputs.items():
                raw=self.store.asset(pid,n).encode();assert digest(raw)==h;(folder/n).write_bytes(raw)
            from .worker import validate
            validate(folder)
            config={'kind':c['kind'],'target':c.get('target_rad',[0,0]),'wheels':[0,0],'paused':False,'stop':False,'seq':0,'received':time.monotonic()}
            write(folder/'control.json',config)
            record={'schema':'rlsok.live-play/1','id':sid,'created_ns':time.perf_counter_ns(),'project':pid,'revision':p['revision'],'inputs':inputs,'state':'STARTING','seconds':seconds,
                'scope':'Interactive MuJoCo sandbox; NOT a requirements/manufacturing/physical validation run','requirements':'NOT_EVALUATED','physical':'NOT_TESTED','budget':Resources().__dict__}
            write(folder/'session.json',record);job={'done':threading.Event(),'cancel':threading.Event(),'folder':folder,'record':record};self.jobs[sid]=job
            job['thread']=threading.Thread(target=self._run,args=(job,),daemon=True);job['thread'].start();return record
    def control(self,pid,sid,seq,command):
        if not isinstance(command,dict) or set(command)-{'target','wheels','paused','stop'}:raise ValueError('Unknown Play control')
        if type(seq) is not int or not 1<=seq<=1000:raise ValueError('Bounded integer sequence required')
        for k in ('target','wheels'):
            if k in command and (not isinstance(command[k],list) or len(command[k])!=2 or not all(type(v) in (int,float) and math.isfinite(v) and abs(v)<=(1.5 if k=='target' else 12) for v in command[k])):raise ValueError('Control outside supported envelope')
        if any(k in command and type(command[k]) is not bool for k in ('paused','stop')):raise ValueError('Pause/stop must be boolean')
        with self.lock:
            f=self.folder(pid,sid);r=read(f/'session.json')
            if r['state'] not in ('STARTING','RUNNING'):raise ValueError('Play session has ended')
            c=read(f/'control.json')
            if seq<=c['seq']:raise ValueError('Stale control sequence')
            if ('wheels'in command and c['kind']!='differential-drive/1') or ('target'in command and c['kind']!='joint_pd/1'):raise ValueError('Control does not match robot capability')
            c.update(command,seq=seq,received=time.monotonic());write(f/'control.json',c)
            with (f/'controls.jsonl').open('a',encoding='utf-8') as log:log.write(json.dumps(c,allow_nan=False)+'\n')
            return {'accepted':seq}
    def state(self,pid,sid):
        f=self.folder(pid,sid);r=read(f/'session.json');r['frame']=read(f/'state.json') if (f/'state.json').exists() else None;return r
    def stop(self,pid,sid):
        with self.lock:
            f=self.folder(pid,sid);c=read(f/'control.json');c.update(stop=True,received=time.monotonic());write(f/'control.json',c)
        return {'stop_requested':sid}
    def _run(self,job):
        f=job['folder'];r=job['record'];sources=[Path(__file__)]+[Path(__file__).with_name(n+'.py') for n in ('play_worker','worker','engineering','mobile','compute','projects')]+[ROOT/'implementation/windows_process.py']
        try:
            r['sources']={str(p.relative_to(ROOT)):digest(p.read_bytes()) for p in sources}
            manifest=Path(sys.executable).parents[1]/'MANIFEST.json';r['environment']={'python':sys.version,'os':platform.platform(),'native_runtime_manifest_sha256':digest(manifest.read_bytes()) if manifest.is_file() else None}
            for p in sources:
                d=f/'tool_sources'/p.name;d.parent.mkdir(exist_ok=True);d.write_bytes(p.read_bytes())
            code='import sys;sys.path.insert(0,sys.argv.pop(1));from implementation.workspace.play_worker import run;run(sys.argv[1],float(sys.argv[2]))'
            argv=[sys.executable,'-I','-B','-c',code,str(ROOT),str(f),str(r['seconds'])]
            env={k:v for k,v in os.environ.items() if k.upper() in ('SYSTEMROOT','WINDIR','TEMP','TMP','PATH','COMSPEC','USERPROFILE','LOCALAPPDATA','APPDATA')};env.update(OMP_NUM_THREADS='1',OPENBLAS_NUM_THREADS='1')
            r['state']='RUNNING';write(f/'session.json',r)
            r['execution']=LocalCompute().execute(argv,f/'execution',Resources(),env,f,job['cancel'].is_set)
            for p in sources:
                if digest(p.read_bytes())!=r['sources'][str(p.relative_to(ROOT))]:raise RuntimeError('Play source changed during execution')
            if any(digest((f/n).read_bytes())!=h for n,h in r['inputs'].items()):raise RuntimeError('Frozen Play inputs changed')
            r['state']='STOPPED' if r['execution']['exit_code']==0 and not r['execution']['limit_event'] else 'FAILED'
            if (f/'summary.json').exists():r['summary']=read(f/'summary.json')
            r['outputs']={p.name:digest(p.read_bytes()) for p in (f/'state.json',f/'summary.json',f/'trace.jsonl',f/'controls.jsonl') if p.is_file()}
        except Exception as e:r.update(state='FAILED',error=str(e))
        finally:write(f/'session.json',r);job['done'].set()
    def close(self):
        for job in self.jobs.values():job['cancel'].set()
        for job in self.jobs.values():job['done'].wait(12)
