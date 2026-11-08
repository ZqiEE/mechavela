"""Persistent FIFO, fixed trusted tool, immutable inputs, bounded local CPU execution."""
from __future__ import annotations
import importlib.metadata
import os
from pathlib import Path
import platform
import queue
import sys
import threading
import time
import uuid
from .projects import digest, encode, read, write
from .compute import LocalCompute, Resources

ROOT = Path(__file__).resolve().parents[2]

def capabilities():
    try:
        version = importlib.metadata.version('mujoco')
    except importlib.metadata.PackageNotFoundError:
        version = None
    return {'schema': 'rlsok.capabilities/1', 'hardware': {'os': platform.system(), 'cpu_count': os.cpu_count()},
        'project_code':{'adapter':'project.python/1','status':'OWNER_OPT_IN_TRUSTED_LOCAL','scope':'User AI authors arbitrary source/model/controller algorithms and code/tools.json declarations; not restricted by legacy robot families. Installed libraries from owner Python environment; resource bounds are not a filesystem/network security sandbox. New tools need no core edits.'},
        'compute': {'local': {'supported': os.name == 'nt', 'platform_charge': 0},
                    'self_hosted': {'supported': False, 'reason': 'Existing SSH training is separate; generic worker protocol not implemented'},
                    'rlsok_cloud': {'supported': False, 'pricing': None, 'metering': False, 'billing': False}},
        'adapters': [{'id':'mujoco.control-graph/1','installed_version':version,'status':'AVAILABLE' if version and os.name=='nt' else 'BLOCKED',
                      'inputs':['robot.xml','controller.json','requirements.json'],'outputs':['trace.csv','result.json'],'scope':'One native free tree with named position channels and a two-wheel planar velocity block; world point and base tasks, ideal torque drives, not universal/hardware control'},
                     {'id':'mujoco.joint-trajectory/1','installed_version':version,
                      'status':'AVAILABLE' if version and os.name=='nt' else 'BLOCKED',
                      'inputs':['robot.xml','controller.json','requirements.json'],'outputs':['trace.csv','result.json'],
                      'scope':'Data-only named-hinge keyframes, optional free base, native PD; user-defined gait must be separately qualified'},
                     {'id':'mujoco.joint-space/1','installed_version':version,
                      'status':'AVAILABLE' if version and os.name=='nt' else 'BLOCKED',
                      'inputs':['robot.xml','controller.json','requirements.json'],'outputs':['trace.csv','result.json'],
                      'scope':'1..24 named hinges, optional unactuated free base, ideal <=20Nm motors; joint tracking/posture only, not gait or physical qualification'},
                     {'id': 'mujoco.joint-pd/1', 'installed_version': version,
                      'status': 'AVAILABLE' if version and os.name == 'nt' else 'BLOCKED',
                      'inputs': ['robot.xml', 'controller.json', 'requirements.json'],
                      'outputs': ['trace.csv', 'result.json'], 'license': 'Apache-2.0',
                      'scope': 'Two-hinge primitive MJCF, fixed PD controller, local CPU; runtime load may still fail'},
                     {'id':'mujoco.component-arm/1','installed_version':version,
                      'status':'AVAILABLE' if version and os.name=='nt' else 'BLOCKED',
                      'inputs':['robot.xml','controller.json','requirements.json','actuators.json','motor-source.json'],
                      'outputs':['trace.csv','scenarios.csv','scenario-models.json','result.json'],
                      'scope':'Horizontal arm with constrained drive; assumed thermal/power, not hardware qualified'},
                     {'id':'mujoco.differential-drive/1','installed_version':version,
                      'status':'AVAILABLE' if version and os.name=='nt' else 'BLOCKED',
                      'inputs':['robot.xml','controller.json','requirements.json'],
                      'outputs':['trace.csv','result.json'],'scope':'Primitive mobile base, flat-ground waypoint; no terrain/power/hardware qualification'},
                     {'id':'bird.mission/1','status':'AVAILABLE' if os.name=='nt' else 'BLOCKED',
                      'inputs':['design.json','controller.json','requirements.json','physical.json','parts-library.json','criteria-r1.json','criteria-r2.json','criteria-r3.json'],
                      'outputs':['flight-log.json','result.json','replay/index.json','replay/run-00.json'],
                      'scope':'Original aerodynamics and assumed attitude; one R3 case per task, no physical flight validation'},
                     {'id':'bird-cad-ai-loop','status':'LEGACY_ADAPTER_PARTIAL','scope':'Original complete bird CAD/AI loop retained in sources; not a supported shared task'},
                     {'id':'cad.parametric/1','status':'INPUT_AND_DEPENDENCY_CHECK_REQUIRED','scope':'Shared task: restricted parameterized solid, original build123d/OCCT exports; no arbitrary CAD scripts'},
                     {'id':'ngspice.circuit/1','status':'INPUT_AND_DEPENDENCY_CHECK_REQUIRED','scope':'Shared task: existing ngspice op/tran/ac, native vectors, metrics and BOM'},
                     *[{'id':name,'status':'LEGACY_ADAPTER_PARTIAL',
                        'scope':'Historical integration retained in source; unsupported as a shared task beyond the explicit versioned adapters'}
                       for name in ('cad','kicad','ngspice','robot-bench','robot-train')],
                     *[{'id': name, 'status': 'NOT_INTEGRATED_IN_WORKSPACE'} for name in
                       ('isaac-sim', 'isaac-lab', 'gazebo', 'ros2', 'cfd', 'hydrodynamics')]],
        'morphologies': {m: ('PARTIAL_REFERENCE' if m in ('manipulator','wheeled','flying','legged') else 'PROJECT_STORAGE_ONLY') for m in
                        ('humanoid', 'legged', 'manipulator', 'wheeled', 'tracked', 'flying', 'underwater', 'biomimetic', 'custom')}}

class Runtime:
    def submit_design(self,pid,source_run=None):
        from .design_assets import submit
        if os.name!='nt':raise RuntimeError('Native inspection containment currently verified on Windows only')
        return submit(self,pid,source_run)

    def submit_code(self,pid,tool,arguments='[]'):
        from .code_workspace import submit
        if os.name!='nt':raise RuntimeError('Code environment currently verified on Windows only')
        return submit(self,pid,tool,arguments)

    def __init__(self, store, backend=None):
        self.store = store
        self.backend = backend or LocalCompute()
        self.queue = queue.Queue()
        self.events = {}
        self.done = {}
        self.lock = threading.RLock()
        self.cad_readiness = None
        # Never restart unconfirmed old workloads or treat interrupted jobs as completed.
        for p in store.list():
            for r in p['runs']:
                if r['state'] in ('QUEUED', 'RUNNING'):
                    r.update(state='INTERRUPTED', error='Server restarted; explicit resubmission required')
                    write(store.path(p['id']) / 'runs' / r['id'] / 'run.json', r)
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def submit(self, pid, adapter=None):
        if os.name != 'nt':
            raise RuntimeError('Generic workspace containment currently verified on Windows only; Linux backend NOT_IMPLEMENTED')
        p = self.store.get(pid)
        from .engineering import select,ADAPTERS
        spec=ADAPTERS[adapter] if adapter else select(__import__('json').loads(self.store.asset(pid,'controller.json')),p['assets'])
        if not set(spec.inputs+spec.extra_inputs)<=set(p['assets']):raise ValueError('Required engineering assets missing')
        if spec.controller and spec.id!=select(__import__('json').loads(self.store.asset(pid,'controller.json')),p['assets']).id:
            raise ValueError('Robot adapter must match declared controller and component capabilities; no downgrade')
        selected=spec.id
        if adapter is not None and adapter != selected:
            raise ValueError('Adapter must match current project assets')
        adapter = selected
        with self.lock:
            if sum(not e.is_set() for e in self.done.values()) >= 16:
                raise ValueError('Queue capacity reached')
            rid = uuid.uuid4().hex
            folder = self.store.path(pid) / 'runs' / rid
            folder.mkdir(parents=True)
            inputs = {}
            with self.store.lock:
                p = self.store.get(pid)
                spec=ADAPTERS[selected]
                if spec.controller and spec.id!=select(__import__('json').loads(self.store.asset(pid,'controller.json',p['revision'])),p['assets']).id:
                    raise ValueError('Robot capabilities changed during submission; reload')
                if spec.id!=selected:raise ValueError('Project adapter changed during submission; reload')
                names = list(spec.inputs)
                names += list(spec.extra_inputs)
                if "assembly.json" in p["assets"]:
                    names += [n for n in ("assembly.json","components.json","BOM.csv","build-manifest.json","build-generator.txt") if n in p["assets"] and n not in names]
                if "motion.json" in p["assets"]:
                    names += [n for n in ("motion.json","motion-brief.json","motion-generator.txt") if n in p["assets"] and n not in names]
                if "system.json" in p["assets"]:
                    names += [n for n in ("system.json","system-brief.json","system-generator.txt") if n in p["assets"] and n not in names]
                for name in names:
                    raw = self.store.asset(pid, name, p['revision']).encode()
                    (folder / name).write_bytes(raw)
                    inputs[name] = digest(raw)
            from .worker import validate
            validate(folder,adapter)
            r = {'schema': 'rlsok.run/1', 'id': rid, 'sequence': len(p['runs']) + 1, 'project': pid, 'revision': p['revision'],
                 'state': 'QUEUED', 'adapter': adapter, 'inputs': inputs,
                 'budget': {'cpu_seconds': 15, 'wall_seconds': 30, 'memory_bytes': 1073741824,
                            'disk_bytes': 16777216}, 'compute': 'local', 'platform_charge': 0,
                 'checkpoint': 'Immutable input snapshot; solver restart only, no mid-step resume'}
            if adapter=='cad.parametric/1':
                from .cad_setup import LIMITS
                r['preparation_budget']={**LIMITS,'scope':'First-use dependency initialization only; separate from unchanged24s CAD child /30s engineering worker'}
            write(folder / 'run.json', r)
            self.events[rid] = threading.Event()
            self.done[rid] = threading.Event()
            self.queue.put((folder, r))
            return r

    def cancel(self, pid, rid):
        if not any(r['id'] == rid for r in self.store.get(pid)['runs']):
            raise ValueError('Unknown project run')
        with self.lock:
            if rid in self.events:
                self.events[rid].set()
        return {'cancel_requested': rid}

    def _loop(self):
        while True:
            item = self.queue.get()
            if item is None:
                self.queue.task_done(); return
            folder, r = item
            event = self.events[r['id']]
            try:
                if event.is_set():
                    r['state'] = 'CANCELLED'
                else:
                    r['state'] = 'RUNNING'; write(folder / 'run.json', r)
                    if r['adapter']=='project.design/1':
                        from .design_assets import execute
                        execute(self,folder,r,event)
                        continue
                    if r['adapter']=='project.python/1':
                        from .code_workspace import execute
                        execute(self,folder,r,event)
                        continue
                    code = ('import sys,time,json;from pathlib import Path;boot=time.perf_counter_ns();'
                            'sys.path.insert(0,sys.argv.pop(1));folder=Path(sys.argv[1]);'
                            '(folder/"worker-timing.json").write_text(json.dumps({"python_entry_ns":boot}));'
                            'from implementation.workspace.worker import run;'
                            '(folder/"worker-timing.json").write_text(json.dumps({"python_entry_ns":boot,"modules_ready_ns":time.perf_counter_ns()}));'
                            'run(folder,sys.argv[2])')
                    argv = [sys.executable, '-I', '-B', '-c', code, str(ROOT), str(folder),r['adapter']]
                    env = {k: v for k, v in os.environ.items() if k.upper() in
                           ('SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'PATH', 'COMSPEC','USERPROFILE','LOCALAPPDATA','APPDATA')}
                    env.update(OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1')
                    env.update(PYTHONIOENCODING='utf-8',RLSOK_WORKER_PYTHON=sys.executable)
                    for key in ('RLSOK_NGSPICE_LIBRARY','RLSOK_NGSPICE_PROVENANCE','RLSOK_CAD_PATH'):
                        if key in os.environ:env[key]=os.environ[key]
                    sources = (Path(__file__), Path(__file__).with_name('worker.py'), Path(__file__).with_name('compute.py'), ROOT / 'implementation/windows_process.py')
                    from .engineering import ADAPTERS
                    sources += (Path(__file__).with_name('engineering.py'),Path(__file__).with_name('projects.py'),ROOT/'implementation/hardware/core.py')
                    sources += tuple(ROOT/path for path in ADAPTERS[r['adapter']].extra_sources)
                    if "assembly.json" in r["inputs"]:sources += (Path(__file__).with_name("build.py"),)
                    if "motion.json" in r["inputs"]:sources += (Path(__file__).with_name("motion_authoring.py"),)
                    if "system.json" in r["inputs"]:sources += (Path(__file__).with_name("system_authoring.py"),)
                    sources=tuple(dict.fromkeys(sources))
                    source_hashes = {str(path.relative_to(ROOT)): digest(path.read_bytes()) for path in sources}
                    for path in sources:
                        snapshot = folder / 'tool_sources' / (path.name + '.txt')
                        snapshot.parent.mkdir(exist_ok=True)
                        snapshot.write_bytes(path.read_bytes())
                    r['tool_sources']=source_hashes
                    r['environment']={'python':sys.version,'os':platform.platform()}
                    if r['adapter']=='cad.parametric/1':
                        from .cad_setup import prepare,identity
                        addon=Path(env.get('RLSOK_CAD_PATH') or ROOT/'thirdparty/cad/site-packages')
                        key=identity(addon)
                        r['phase']='PREPARING_CAD_DEPENDENCIES';write(folder/'run.json',r)
                        if self.cad_readiness and self.cad_readiness['identity']==key:
                            preparation={**self.cad_readiness,'reused':True}
                            write(folder/'cad-preparation.json',preparation)
                        else:
                            preparation=prepare(folder,addon,env,event.is_set)
                            if preparation['state']=='READY':self.cad_readiness=preparation
                        r['preparation']=preparation
                        if preparation['state']!='READY':
                            r['state']='CANCELLED' if event.is_set() else 'FAILED'
                            r['error']='CAD dependency preparation failed; engineering NOT_RUN; inspect cad-preparation.json'
                            r['outputs']={f.relative_to(folder).as_posix():digest(f.read_bytes()) for f in folder.rglob('*') if f.is_file() and f.name!='run.json' and 'tool_sources' not in f.parts and f.relative_to(folder).as_posix() not in r['inputs']}
                            continue
                        r['phase']='ENGINEERING_24S_CAD_CHILD';write(folder/'run.json',r)
                    receipt = self.backend.execute(argv, folder / 'execution', Resources(), env, folder, event.is_set)
                    r['execution'] = receipt
                    r['environment'] = {'python': sys.version, 'os': platform.platform()}
                    r['tool_sources'] = source_hashes
                    if any(digest(path.read_bytes()) != source_hashes[str(path.relative_to(ROOT))] for path in sources):
                        raise RuntimeError('Tool source changed during execution')
                    if any(digest((folder / n).read_bytes()) != sha for n, sha in r['inputs'].items()):
                        raise RuntimeError('Input changed during execution')
                    if receipt['limit_event'] or receipt['exit_code'] != 0:
                        r['state'] = 'CANCELLED' if receipt['limit_event'] == 'CANCELLED' else 'FAILED'
                        r['error'] = receipt['limit_event'] or 'Tool failed: inspect execution/stderr.log'
                    else:
                        result = read(folder / 'result.json')
                        r.update(state='COMPLETED', result=result)
                    r['outputs'] = {f.relative_to(folder).as_posix(): digest(f.read_bytes()) for f in folder.rglob('*')
                        if f.is_file() and f.relative_to(folder).parts[0] not in ('execution','tool_sources')
                        and f.name!='run.json' and f.relative_to(folder).as_posix() not in r['inputs']}
            except Exception as e:
                r.update(state='FAILED', error=f'{type(e).__name__}: {e}')
            finally:
                write(folder / 'run.json', r)
                self.done[r['id']].set()
                self.queue.task_done()

    def wait(self, pid, rid, timeout=None):
        if timeout is None:
            timeout=100 if read(self.store.path(pid)/'runs'/rid/'run.json')['adapter']=='cad.parametric/1' else 40
        if rid in self.done and not self.done[rid].wait(timeout):
            raise TimeoutError('Run remains active')
        return read(self.store.path(pid) / 'runs' / rid / 'run.json')

    def close(self):
        with self.lock:
            for event in self.events.values():
                event.set()
            self.queue.put(None)
        self.thread.join(12)
