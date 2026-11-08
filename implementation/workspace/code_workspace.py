"""User/AI code, project-declared tools and honest local execution receipts.

Job Objects limit resources/process lifetimes, NOT filesystem or network access.
This opt-in trusted-local environment is deliberately distinct from fixed adapters.
"""
import json,os,re,runpy,sys,time
import math
from dataclasses import dataclass
from pathlib import Path
from .projects import asset_name,digest,encode,read,write
from .compute import Resources
ADAPTER='project.python/1'
SCOPE='TRUSTED_LOCAL: project code runs as your Windows user; Job Objects are resource limits, NOT filesystem/network isolation. Never execute untrusted code.'

@dataclass(frozen=True)
class CodeResources(Resources):
    cpu_seconds:float=60
    wall_seconds:float=120
    memory_bytes:int=2147483648
    disk_bytes:int=67108864
    def validate(self):
        if any(type(v) not in (int,float) or not math.isfinite(v) or not 0<v<=bound for v,bound in ((self.cpu_seconds,300),(self.wall_seconds,600))):raise ValueError('Code CPU/wall budget outside owner envelope')
        if type(self.memory_bytes) is not int or not 0<self.memory_bytes<=4294967296 or type(self.disk_bytes) is not int or not 0<self.disk_bytes<=268435456 or self.gpu_count!=0:raise ValueError('Code memory/disk/GPU budget outside owner envelope')

def resources(config):
    # Existing approvals retain their original15CPU/30wall; no silent budget increase.
    return CodeResources(**config['limits']) if 'limits' in config else Resources()

def approve(store,pid,expected,confirm,python=None,python_paths=None,limits=None):
    if confirm is not True:raise ValueError('Explicit trusted-local code execution approval required')
    exe=Path(python or sys.executable)
    root=Path(__file__).resolve().parents[2];addon=root/'thirdparty/cad/site-packages'
    if not addon.is_dir():addon=root/'packaging/build_cad/site-packages'
    paths=([str(addon)] if python is None and addon.is_dir() else []) if python_paths is None else python_paths
    if not exe.is_absolute() or not exe.is_file() or exe.name.lower() not in ('python.exe','python3.exe'):raise ValueError('Choose an existing absolute Python interpreter')
    if not isinstance(paths,list) or len(paths)>8 or any(not isinstance(s,str) or not Path(s).is_absolute() or not Path(s).is_dir() for s in paths):raise ValueError('Choose existing absolute library directories')
    if limits is not None and (not isinstance(limits,dict) or set(limits)-set(vars(CodeResources()))):raise ValueError('Unknown code resource fields')
    budget=CodeResources(**(limits or {}));budget.validate()
    config={'schema':'rlsok.code-environment/1','python':str(exe.resolve()),'python_paths':paths,'limits':vars(budget),'scope':SCOPE}
    with store.lock:
        p=store.get(pid)
        if p['revision']!=expected:raise ValueError('Revision conflict')
        # The approval is outside exported assets: imports/forks cannot inherit consent.
        write(store.path(pid)/'local-code-approval.json',config)
    return config

def environment(store,pid):
    path=store.path(pid)/'local-code-approval.json'
    if not path.is_file():raise ValueError('Owner must enable a trusted-local code environment for THIS project; imported approval is never trusted')
    return read(path)

def tools(store,pid,revision=None):
    p=store.get(pid);names=p['assets'] if revision is None else read(store.path(pid)/'revisions'/revision/'revision.json')['assets']
    if 'code/tools.json' not in names:return []
    data=json.loads(store.asset(pid,'code/tools.json',revision))
    if not isinstance(data,dict) or set(data)!={'schema','tools'} or data['schema']!='rlsok.project-tools/1' or not isinstance(data['tools'],list) or len(data['tools'])>32:raise ValueError('Expected project-tools/1 with up to32 tools')
    seen=set()
    for t in data['tools']:
        if not isinstance(t,dict) or not {'id','entrypoint','description'}<=set(t) or set(t)-{'id','entrypoint','description','runtime'} or not isinstance(t['id'],str) or not re.fullmatch('[a-zA-Z0-9_.-]{1,80}',t['id']) or t['id'] in seen:raise ValueError('Unique tool id, entrypoint and description required')
        if t.get('runtime','python') not in ('python','freecad'):raise ValueError('Project tool runtime must be python or owner-configured freecad')
        asset_name(t['entrypoint'])
        if not t['entrypoint'].startswith('code/') or not t['entrypoint'].endswith('.py') or t['entrypoint'] not in names:raise ValueError('Tool entrypoint must be a versioned code/*.py file')
        if not isinstance(t['description'],str) or len(t['description'])>2000:raise ValueError('Bounded tool description required')
        seen.add(t['id'])
    return data['tools']

def inspect(store,pid):
    try:env=environment(store,pid)
    except ValueError:env=None
    try:declared=tools(store,pid);error=None
    except (ValueError,KeyError) as e:declared=[];error=str(e)
    import importlib.metadata
    root=Path(__file__).resolve().parents[2];bundled=root/'thirdparty/cad/site-packages'
    if not bundled.is_dir():bundled=root/'packaging/build_cad/site-packages'
    paths=[str(bundled),*sys.path] if env is None or Path(env['python']).resolve()==Path(sys.executable).resolve() else list(env.get('python_paths',[]))
    versions={d.metadata['Name'].lower():d.version for d in importlib.metadata.distributions(path=paths) if d.metadata.get('Name')}
    libraries={name:versions.get(name,'NOT_IN_METADATA') for name in ['build123d','cadquery','cadquery-ocp-novtk','mujoco','trimesh']}
    from .cad_compatibility import describe as cad_compatibility
    from .freecad_host import configured
    host_profile=configured(store);cad_host=None
    if host_profile:
        try:
            host=read(host_profile);cad_host={'provider':host.get('provider'),'executable':str(Path(host['root'])/'bin/freecadcmd.exe'),
                'invocation':'With explicit project execute_code permission, use subprocess with [executable, "--safe-mode", absolute_project_script.py]. Write STEP and editable FCStd under artifacts/. Declare STEP in code/design-assets.json, then call inspect_design(source_run) for independent CAD/assembly feedback and automatic recorded 3D display. Never import FreeCAD into this Python interpreter or activate a different environment.',
                'qualification':host.get('qualification'),'scope':'Owner-selected complete external host; does not grant project execution permission. Runtime identities checked before platform inspection.'}
        except (OSError,ValueError,KeyError,TypeError):cad_host={'status':'INVALID_OWNER_CONFIGURATION'}
    return {'adapter':ADAPTER,'environment':env,'scope':SCOPE,'tools':declared,'manifest_error':error,
            'library_metadata':libraries,'cad_host':cad_host,'cad_runtime_compatibility':cad_compatibility(),'library_scope':'Package metadata only, not proof of a successful import. Default bundled environment when no owner environment is enabled; custom interpreter packages require an actual project-code probe.',
            'execution_state':'ENABLED' if env else 'OWNER_APPROVAL_REQUIRED',
            'authoring':'write_code(files: serialized JSON filename->UTF8 text), paths under code/. Add code/tools.json {schema:"rlsok.project-tools/1",tools:[{id,entrypoint:"code/path.py",description,runtime:"freecad"}]}. runtime is optional: default python; freecad uses the owner-selected external CAD host with its own interpreter, direct FreeCAD/Part/Import API and platform exception propagation. Requires the SAME explicit project execution permission; no auto approval. No robot/controller schema restrictions. run_code(tool,arguments:serialized JSON string array) executes in a fresh work snapshot. Write STEP and editable FCStd under artifacts/ and declare the STEP in code/design-assets.json. After each build call inspect_design(source_run), read real assembly feedback, fix source, rebuild and reinspect. The workspace displays recorded native CAD. Code exit0 and self-authored JSON never establish requirements PASS. Owner requirements remain read-only via editing tools.',
            'permissions':['edit_code','execute_code'],'limits':vars(resources(env)) if env else vars(CodeResources())}

def edit(store,pid,files,expected):
    values=json.loads(files)
    if not isinstance(values,dict) or not values:raise ValueError('Serialized filename->text mapping required')
    if any(not isinstance(n,str) or not n.startswith('code/') for n in values):raise ValueError('AI code edits must stay under code/; owner assets are protected')
    for n in values:asset_name(n)
    return {'revision':store.revise(pid,values,'AI project source revision',expected=expected)}

def submit(runtime,pid,tool,arguments='[]'):
    store=runtime.store
    with runtime.lock,store.lock:
        config=environment(store,pid);p=store.get(pid)
        selected=next((t for t in tools(store,pid) if t['id']==tool),None)
        if selected is None:raise ValueError('Unknown project-declared tool')
        args=json.loads(arguments)
        if not isinstance(args,list) or len(args)>32 or any(not isinstance(a,str) or len(a)>4000 for a in args):raise ValueError('Arguments must be a JSON string array (up to32)')
        if sum(not e.is_set() for e in runtime.done.values())>=16:raise ValueError('Queue capacity reached')
        import uuid,threading
        rid=uuid.uuid4().hex;folder=store.path(pid)/'runs'/rid;folder.mkdir(parents=True)
        names=list(p['assets'])
        inputs={}
        for n in names:
            raw=store.asset(pid,n,p['revision']).encode('utf-8');path=folder/n;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw);inputs[n]=digest(raw)
        budget=resources(config);budget.validate()
        r={'schema':'rlsok.run/1','id':rid,'sequence':len(p['runs'])+1,'project':pid,'revision':p['revision'],'state':'QUEUED','adapter':ADAPTER,'inputs':inputs,'budget':vars(budget),'compute':'local','platform_charge':0,'code_tool':selected,'arguments':args,'code_environment':config,'acceptance':'NOT_EVALUATED: execution receipt and user-code output, not independent engineering qualification'}
        write(folder/'run.json',r);runtime.events[rid]=threading.Event();runtime.done[rid]=threading.Event();runtime.queue.put((folder,r));return r

def execute(runtime,folder,r,event):
    work=folder/'work';work.mkdir();artifacts=work/'artifacts';artifacts.mkdir()
    for n in r['inputs']:
        dest=work/n;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes((folder/n).read_bytes())
    config=r['code_environment'];entry=r['code_tool']['entrypoint']
    # No credentials/provider settings/data-store paths passed in the environment.
    env={k:v for k,v in os.environ.items() if k.upper() in ('SYSTEMROOT','WINDIR','TEMP','TMP','PATH','COMSPEC')}
    env.update(OMP_NUM_THREADS='1',OPENBLAS_NUM_THREADS='1',PYTHONIOENCODING='utf-8')
    # Mature libraries need a writable home/config location. Use a fresh run-local
    # profile instead of inheriting user credentials/config or breaking expanduser().
    home=work/'.home'
    for key,relative in {'HOME':'','USERPROFILE':'','LOCALAPPDATA':'AppData/Local','APPDATA':'AppData/Roaming','XDG_CONFIG_HOME':'.config','XDG_CACHE_HOME':'.cache'}.items():
        directory=home/relative;directory.mkdir(parents=True,exist_ok=True);env[key]=str(directory)
    bootstrap='import sys,os,runpy,json,site;root=sys.argv.pop(1);paths=json.loads(sys.argv.pop(1));os.chdir(root);sys.path[:0]=[root,os.path.join(root,"code")]+paths;[site.addsitedir(p) for p in paths];script=sys.argv[1];sys.argv=sys.argv[1:];runpy.run_path(script,run_name="__main__")'
    argv=[config['python'],'-I','-B','-c',bootstrap,str(work),json.dumps(config['python_paths']),entry,*r['arguments']]
    tool_files=[Path(__file__)]
    if r['code_tool'].get('runtime')=='freecad':
        from .freecad_host import configured,validate
        profile=configured(runtime.store)
        if not profile:raise ValueError('Owner has not configured a qualified FreeCAD host; no Python/OCP fallback')
        host=validate(profile);job=folder/'freecad-script-job.json';write(job,{'work':str(work.resolve()),'script':entry,'arguments':r['arguments']})
        temp=folder/'t';temp.mkdir(exist_ok=True);env.update(TEMP=str(temp.resolve()),TMP=str(temp.resolve()))
        env.update(RLSOK_FREECAD_SCRIPT_JOB=str(job.resolve()),RLSOK_ARTIFACT_DIR=str(artifacts.resolve()),RLSOK_NATIVE_PROBE_OUTPUT=str(artifacts.resolve()))
        argv=[str(Path(host['root'])/'bin/freecadcmd.exe'),'--safe-mode',str(Path(__file__).with_name('freecad_execute.py'))]
        r['native_runtime']={'provider':'freecad','root':host['root'],'profile_sha256':digest(profile.read_bytes()),'qualification':host['qualification']}
        tool_files.extend(Path(__file__).with_name(name+'.py') for name in ('freecad_execute','freecad_host'))
    r['tool_sources']={f.relative_to(Path(__file__).resolve().parents[2]).as_posix():digest(f.read_bytes()) for f in tool_files}
    for source in tool_files:
        snapshot=folder/'tool_sources'/(source.name+'.txt');snapshot.parent.mkdir(exist_ok=True);snapshot.write_bytes(source.read_bytes())
    receipt=runtime.backend.execute(argv,folder/'execution',resources(config),env,folder,event.is_set);r['execution']=receipt
    if any(digest((folder/n).read_bytes())!=sha for n,sha in r['inputs'].items()):raise ValueError('Immutable code input changed during execution')
    if any(digest(f.read_bytes())!=r['tool_sources'][f.relative_to(Path(__file__).resolve().parents[2]).as_posix()] for f in tool_files):raise ValueError('Code runner source changed during execution')
    r['state']='CANCELLED' if receipt['limit_event']=='CANCELLED' else 'FAILED' if receipt['limit_event'] or receipt['exit_code']!=0 else 'COMPLETED'
    if r['state']!='COMPLETED':r['error']=receipt['limit_event'] or 'Project code failed; read execution/stderr.log'
    # Only trusted parent writes the execution verdict; do not parse project result.json as PASS.
    result={'adapter':ADAPTER,'execution_state':r['state'],'levels':{'requirements':'NOT_EVALUATED'},'measured':{},'limitations':[SCOPE,'Project code outputs are self-authored; not independent engineering acceptance'],'scope':SCOPE,'acceptance':'User algorithm outputs are self-authored; no independent physical/requirements verdict'}
    write(folder/'result.json',result);r['result']=result
    r['outputs']={}
    for p in [folder/'result.json',folder/'execution/stdout.log',folder/'execution/stderr.log',*artifacts.rglob('*')]:
        if any(x.is_symlink() or (hasattr(x,'is_junction') and x.is_junction()) for x in (p,*p.parents)):raise ValueError('Linked output refused')
        if p.is_file():r['outputs'][p.relative_to(folder).as_posix()]=digest(p.read_bytes())
