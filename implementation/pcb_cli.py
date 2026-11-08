"""Native KiCad provider with an independent, explicitly authorized ledger.

KiCad performs ERC/DRC and exports. Windows Job Objects enforce the bounds.
Receipts/manifests retain the same evidence-reading format as engineering_cli.
"""
import argparse
import contextlib
import csv
import json
import math
import msvcrt
import os
import re
import sys
import time
from pathlib import Path
from .completion import digest, read, sha
from .windows_process import run as native_run

ROOT=Path(__file__).resolve().parents[1]
BASE=ROOT/'usage/pcb-20261011'
ENGINE=BASE/'engine'
TOOL=BASE/'tools/KiCad-10.0.7/bin/kicad-cli.exe'
PROJECT=BASE/'project'
PYTHON=Path(sys.executable).resolve()
PS=Path(os.environ.get('ATOMS_PCB_POWERSHELL',str(PYTHON.parents[1]/'native/powershell/pwsh.exe')))

def linux(path):
    path=str(Path(path).resolve())
    if len(path)<3 or path[1:3]!=':\\':raise ValueError('local absolute drive path required')
    return '/mnt/'+path[0].lower()+'/'+path[3:].replace('\\','/')

def write(path,value):
    path=Path(path);temporary=path.with_name(path.name+'.tmp')
    temporary.write_text(json.dumps(value,indent=2,allow_nan=False));os.replace(temporary,path)

@contextlib.contextmanager
def locked():
    ENGINE.mkdir(parents=True,exist_ok=True)
    with (ENGINE/'native.lock').open('a+b') as lock:
        if lock.tell()==0:lock.write(b'0');lock.flush()
        lock.seek(0);msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
        try:yield
        finally:lock.seek(0);msvcrt.locking(lock.fileno(),msvcrt.LK_UNLCK,1)

def initialize():
    authorization=read(BASE/'authorization.json');request=read(BASE/'RESOURCE_REQUEST.json')
    if authorization.get('status')!='AUTHORIZED' or authorization.get('limits')!=request['limits']:
        raise ValueError('explicit independent PCB resource authorization with exact limits required')
    if (ENGINE/'plan.json').exists():return
    matrix={'scope':'native KiCad10.0.7 passive resistor PCB digital files only; no hardware test/manufacturing release',
            'gates':{'PCB':{'required':True,'rule':'real zero-violation ERC/DRC, schematic/board parity, Gerber/drill/BOM exports, real short detected'}}}
    write(ENGINE/'matrix.json',matrix)
    plan={'budget':{'cpu':300,'wall':1800},'controller_hold':{'cpu':20,'wall':100},
          'matrix_sha256':sha(ENGINE/'matrix.json'),'input_hashes':{linux(BASE/'authorization.json'):sha(BASE/'authorization.json')},
          'roles':{'baseline':{'candidate_id':'PCB_NATIVE_QUALIFICATION'},'candidate':{'candidate_id':'PCB_NATIVE_QUALIFICATION'}},
          'authorization_sha256':sha(BASE/'authorization.json'),'spend':0,'per_job_memory':4294967296,'single_concurrency':True}
    write(ENGINE/'plan.json',plan);write(ENGINE/'resources.json',{'spent':{'cpu':0,'wall':0},'stages':{}})
    write(ENGINE/'results.json',{'PCB':{'status':'NOT_PROVEN','scope':matrix['scope']}})

class Stage:
    def __init__(self,name,label,cap,inputs,tools,command=None,profile=None,*,
                 engine=None,base=None,recipe=None,memory_cap=4294967296,
                 disk_cap=8589934592,result_updater=None,path_formatter=None):
        """Run one fully reserved native stage in an independently frozen ledger.

        Optional paths never alter the PCB module globals. ``result_updater`` is
        called as ``callback(stage, receipt)``; its default retains PCB reporting.
        ``path_formatter(path)`` controls descriptor paths and defaults to WSL
        drive paths. A Windows-only caller may pass ``str``.
        """
        if any(not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_-]+',value)
               for value in (name,label)):
            raise ValueError('safe stage name and job label required')
        self.engine=Path(ENGINE if engine is None else engine).resolve()
        self.base=Path(BASE if base is None else base).resolve()
        self.final_disk_check=engine is not None or base is not None or path_formatter is not None
        if not self.engine.is_relative_to(self.base):
            raise ValueError('stage engine must be inside its independently bounded base')
        self.recipe_source=Path(__file__ if recipe is None else recipe).resolve()
        self.memory_cap,self.disk_cap=memory_cap,disk_cap
        if any(type(value) is not int or value<=0 for value in (memory_cap,disk_cap)):
            raise ValueError('positive integer memory and disk caps required')
        self.path_formatter=linux if path_formatter is None else path_formatter
        self.result_updater=result_updater
        if not callable(self.path_formatter) or (result_updater is not None and not callable(result_updater)):
            raise ValueError('path formatter and result updater must be callable')
        self.name,self.label,self.cap=name,label,dict(cap)
        self.inputs={Path(p).resolve():h for p,h in inputs.items()}
        self.tools={Path(p).resolve():h for p,h in tools.items()}
        self.command,self.profile=command,profile
        self.payload_sha256=self.tools[self.recipe_source]
        self.frozen_bindings={}
        self.accounting_unknown=None
        self.calls=[];self.start=time.monotonic();self.cpu_start=time.process_time()
    @contextlib.contextmanager
    def ledger_lock(self):
        # Separate from the original CLI's outer native.lock, so old callers
        # retain their lock while direct Stage users also serialize admission.
        with (self.engine/'stage-ledger.lock').open('a+b') as lock:
            if lock.tell()==0:lock.write(b'0');lock.flush()
            lock.seek(0);msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
            try:yield
            finally:lock.seek(0);msvcrt.locking(lock.fileno(),msvcrt.LK_UNLCK,1)
    def freeze_plan(self):
        paths={key:path for key,path in (
            ('plan',self.engine/'plan.json'),('matrix',self.engine/'matrix.json'),
            ('authorization',self.base/'authorization.json'),
            ('request',self.base/'RESOURCE_REQUEST.json'))}
        plan=read(paths['plan']);authorization=read(paths['authorization']);request=read(paths['request'])
        if (plan['matrix_sha256']!=sha(paths['matrix'])
                or plan['authorization_sha256']!=sha(paths['authorization'])):
            raise ValueError('frozen plan matrix/authorization binding changed')
        limits=authorization.get('limits')
        if authorization.get('status')!='AUTHORIZED' or not isinstance(limits,dict) or limits!=request.get('limits'):
            raise ValueError('explicit independent resource authorization with exact limits required')
        def number(value,minimum=0):
            return type(value) in (int,float) and math.isfinite(value) and value>=minimum
        for key,limit in (('cpu','cpu_seconds'),('wall','active_tool_wall_seconds')):
            if (not number(self.cap.get(key)) or self.cap[key]<=0
                    or not number(plan['budget'].get(key)) or plan['budget'][key]<=0
                    or not number(plan['controller_hold'].get(key))
                    or not number(limits.get(limit)) or plan['budget'][key]>limits[limit]):
                raise ValueError('invalid or unauthorized stage resource budget')
        if (type(plan.get('per_job_memory')) is not int or type(limits.get('memory_bytes')) is not int
                or self.memory_cap>plan['per_job_memory'] or plan['per_job_memory']>limits['memory_bytes']
                or type(limits.get('disk_ceiling_bytes')) is not int or self.disk_cap>limits['disk_ceiling_bytes']
                or plan.get('single_concurrency') is not True or limits.get('concurrency')!=1
                or plan.get('spend')!=0 or limits.get('external_spend_usd')!=0):
            raise ValueError('stage exceeds frozen native isolation/resource authorization')
        bindings={path:sha(path) for path in paths.values()}
        for raw,expected in plan['input_hashes'].items():
            match=re.fullmatch(r'/mnt/([A-Za-z])/(.+)',raw)
            path=Path(match[1]+':/'+match[2] if match else raw)
            if not path.is_absolute() or not path.is_file() or sha(path)!=expected:
                raise ValueError('frozen workflow input changed: '+raw)
            bindings[path.resolve()]=expected
        if plan['input_hashes'].get(self.path_formatter(paths['authorization']))!=plan['authorization_sha256']:
            raise ValueError('workflow inputs must bind this session authorization')
        self.frozen_bindings=bindings
        return plan
    def begin(self):
        with self.ledger_lock():self._begin()
    def _begin(self):
        plan=self.freeze_plan();ledger=read(self.engine/'resources.json')
        if self.name in ledger['stages']:raise ValueError('immutable stage exists; no replay')
        if any(s['state']!='SETTLED' for s in ledger['stages'].values()):
            raise ValueError('unfinished native stage requires resource reconciliation before another launch')
        for key in ('cpu','wall'):
            if type(ledger['spent'][key]) not in (int,float) or not math.isfinite(ledger['spent'][key]) or ledger['spent'][key]<0:
                raise ValueError('invalid cumulative native resource accounting')
            held=sum(s['cap'][key] for s in ledger['stages'].values() if s['state']!='SETTLED')
            if ledger['spent'][key]+held+self.cap[key]+plan['controller_hold'][key]>plan['budget'][key]:raise ValueError('INSUFFICIENT_COMPLETE_STAGE_BUDGET')
        (self.engine/'jobs'/self.label).mkdir(parents=True,exist_ok=False)
        self.job=self.engine/'jobs'/self.label
        self.recipe=self.job/'verification-recipe.py'
        self.recipe.write_bytes(self.recipe_source.read_bytes())
        if sha(self.recipe)!=self.payload_sha256:raise ValueError('provider changed before recipe freeze')
        self.tools.pop(self.recipe_source)
        self.tools[self.recipe]=self.payload_sha256
        self.verify_identities()
        if self.profile:
            write(self.job/'verification-profile.json',self.profile)
            self.inputs[self.job/'verification-profile.json']=sha(self.job/'verification-profile.json')
        binds=[{'source':self.path_formatter(path),'target':'/inputs/'+str(i)+'/'+Path(path).name,'sha256':hash_value}
               for i,(path,hash_value) in enumerate(self.inputs.items())]
        spec={'stage':self.name,'stage_cap':self.cap,'identity_hashes':{self.path_formatter(p):h for p,h in self.tools.items()},
              'jobs':[{'label':self.label,'payload':self.path_formatter(self.recipe),'payload_sha256':self.payload_sha256,'binds':binds,'cap':self.cap}]}
        descriptor={'spec':spec,'workflow_plan_sha256':sha(self.engine/'plan.json')};descriptor['descriptor_sha256']=digest(descriptor)
        (self.engine/'cli-plans').mkdir(exist_ok=True);descriptor_path=self.engine/'cli-plans'/(self.name+'.json')
        write(descriptor_path,descriptor)
        self.frozen_bindings[descriptor_path]=sha(descriptor_path)
        self.descriptor_sha256=descriptor['descriptor_sha256']
        ledger['stages'][self.name]={'cap':self.cap,'state':'RUNNING','jobs':[],'cli_plan_sha256':descriptor['descriptor_sha256']}
        write(self.engine/'resources.json',ledger)
    def verify_identities(self):
        for path,hash_value in [*self.frozen_bindings.items(),*self.inputs.items(),*self.tools.items()]:
            if not Path(path).is_file() or sha(path)!=hash_value:
                raise ValueError('frozen input/tool version changed: '+str(path))
    def active_reservation(self,ledger):
        stage=ledger['stages'].get(self.name)
        if (not stage or stage['state']!='RUNNING' or stage['cap']!=self.cap
                or stage.get('cli_plan_sha256')!=self.descriptor_sha256):
            raise ValueError('active stage reservation changed or already settled')
        return stage
    def cost(self):
        return {'cpu':time.process_time()-self.cpu_start+sum(x['cpu_seconds'] for x in self.calls),'wall':time.monotonic()-self.start}
    def call(self,argv,name,max_wall=None,*,environment=None):
        """Run a trusted tool; an explicit environment replaces inherited values.

        Job Objects bound resources and descendant lifetimes. They do not
        provide filesystem or network isolation.
        """
        with self.ledger_lock():return self._call(argv,name,max_wall,environment)
    def _call(self,argv,name,max_wall,environment):
        self.verify_identities()
        self.active_reservation(read(self.engine/'resources.json'))
        if not isinstance(name,str) or not re.fullmatch(r'[A-Za-z0-9_-]+',name):
            raise ValueError('safe native call name required')
        cost=self.cost();cpu=self.cap['cpu']-cost['cpu'];wall=self.cap['wall']-cost['wall']
        if cpu<=0 or wall<=0:raise TimeoutError('stage admission budget exhausted')
        if max_wall is not None and (type(max_wall) not in (int,float) or not math.isfinite(max_wall) or max_wall<=0):
            raise ValueError('positive finite native wall cap required')
        if environment is None:
            env=dict(os.environ)
            for key,folder in [('KICAD_CONFIG_HOME','config'),('APPDATA','appdata'),('LOCALAPPDATA','localappdata'),('TEMP','temp'),('TMP','temp')]:
                path=self.base/folder;path.mkdir(exist_ok=True);env[key]=str(path)
        else:
            if not isinstance(environment,dict) or not environment or any(
                    not isinstance(k,str) or not k or '=' in k or '\0' in k
                    or not isinstance(v,str) or '\0' in v for k,v in environment.items()):
                raise ValueError('nonempty explicit string environment required')
            env=dict(environment)
        try:
            result=native_run(argv,self.job/name,cpu,min(wall,max_wall or wall),memory_cap=self.memory_cap,
                              env=env,disk_root=self.base,disk_cap=self.disk_cap)
        except BaseException as error:
            # Native runner cannot currently return accounting on exceptional paths.
            # Keep the whole stage reservation; do not invent zero child CPU cost.
            self.accounting_unknown={'call':name,'argv':[str(x) for x in argv],
                                     'reason':type(error).__name__+': '+str(error)}
            write(self.job/'resource-accounting-pending.json',self.accounting_unknown)
            raise
        self.calls.append(result)
        if result.get('all_job_processes_quiescent') is not True:
            self.accounting_unknown={'call':name,'argv':[str(x) for x in argv],
                                     'reason':'final native process quiescence not confirmed'}
            write(self.job/'resource-accounting-pending.json',self.accounting_unknown)
            raise RuntimeError(self.accounting_unknown['reason'])
        for key in ('stdout','stderr'):
            destination=self.job/(name+'-'+key+'.log')
            destination.write_bytes(Path(result[key]).read_bytes())
        if result['limit_event']:raise TimeoutError(result['limit_event'])
        return result
    def finish(self,code,reason=None,failure_kind=None):
        with self.ledger_lock():return self._finish(code,reason,failure_kind)
    def _finish(self,code,reason=None,failure_kind=None):
        ledger=read(self.engine/'resources.json');stage=self.active_reservation(ledger)
        final_disk_bytes=None
        if not self.accounting_unknown:
            try:self.verify_identities()
            except (OSError,ValueError) as error:code,reason,failure_kind=1,str(error),'TOOL_ERROR'
        if self.final_disk_check:
            try:
                final_disk_bytes=0
                for root,dirs,files in os.walk(self.base,followlinks=False):
                    for name in files:
                        try:final_disk_bytes+=os.stat(os.path.join(root,name),follow_symlinks=False).st_size
                        except FileNotFoundError:pass
                if final_disk_bytes>self.disk_cap:
                    code,reason,failure_kind=124,'final stage directory disk ceiling exceeded','BUDGET'
            except OSError as error:code,reason,failure_kind=1,str(error),'TOOL_ERROR'
        cost=self.cost();artifacts={p.name:sha(p) for p in self.job.iterdir() if p.is_file() and p.name!='execution.json'}
        unknown=self.accounting_unknown is not None
        value={'state':'RESOURCE_ACCOUNTING_PENDING' if unknown else 'SUCCEEDED' if code==0 else 'BUDGET_EXCEEDED' if failure_kind=='BUDGET' else 'FAILED','exit_code':code,
               'failure_kind':failure_kind,'reason':reason,'controller_cost':None if unknown else cost,
               'known_cost_lower_bound':cost if unknown else None,
               'controller_cost_scope':'UNKNOWN_CHILD_CPU; complete reservation retained' if unknown else 'measured controller interval plus quiescent Windows job CPU; final outer CLI tail is covered by held allowance',
               'memory_peak_bytes':max((x['memory_peak_bytes'] for x in self.calls),default=0),'artifact_sha256':artifacts,
               'actual_native_calls':self.calls,'resource_provider':'Windows Job Object; no cgroup claim',
               'all_job_processes_quiescent':None if unknown else all(x['all_job_processes_quiescent'] for x in self.calls),
               'execution_binding':{'stage':self.name,'label':self.label,'cap':self.cap,'payload_sha256':self.payload_sha256}}
        if self.final_disk_check:
            value['final_disk_observed_bytes']=final_disk_bytes
            value['final_disk_monitor_scope']=str(self.base)
        write(self.job/'execution.json',value)
        if unknown:
            stage['state']='UNKNOWN_INTERRUPTED'
            stage['recovery']={'receipt':str(self.job/'execution.json'),'known_cost_lower_bound':cost,
                               'reason':'RESOURCE_ACCOUNTING_PENDING',
                               'condition':'recover final native accounting and process quiescence; reservation remains held'}
        else:
            stage['jobs']=[{'label':self.label,'state':value['state'],'cost':cost,'receipt_sha256':sha(self.job/'execution.json')}];stage['state']='SETTLED'
            for key in ('cpu','wall'):ledger['spent'][key]+=cost[key]
        write(self.engine/'resources.json',ledger)
        if self.result_updater is None:self.update_results(value)
        else:self.result_updater(self,value)
        return value
    def update_results(self,receipt):
        result=read(self.engine/'results.json');pcb=result.setdefault('PCB',{})
        checks=read(self.job/'pcb-checks.json') if (self.job/'pcb-checks.json').exists() else {}
        summary={'stage':self.name,'command':self.command,'state':receipt['state'],
                 'failure_kind':receipt['failure_kind'],'reason':receipt['reason'],
                 'receipt':self.path_formatter(self.job/'execution.json'),'receipt_sha256':sha(self.job/'execution.json'),
                 'check_status':checks.get('status'),'profile':self.profile}
        pcb.setdefault('history',[]).append(summary)
        pcb['latest_attempt']=summary
        if self.command=='install':pcb['installation']=summary
        if self.command=='run' and receipt['state']=='SUCCEEDED' and checks.get('status')=='PASS_PCB_DIGITAL_FILES':
            pcb.setdefault('verified_projects',{})[digest(self.profile)]=summary
        if self.command=='negative' and receipt['failure_kind']=='DESIGN_VIOLATION' and checks.get('status')=='REAL_NEGATIVE_DETECTED':
            pcb.setdefault('verified_negative_cases',{})[digest(self.profile)]=summary
        # Qualification status cannot be earned by unrelated caller projects.
        def identities(entries):
            values=set()
            for entry in entries.values():
                if not entry['profile'].get('qualification_case'):continue
                descriptor=read(self.engine/'cli-plans'/(entry['stage']+'.json'))
                recorded_stage=read(self.engine/'resources.json')['stages'][entry['stage']]
                if descriptor['workflow_plan_sha256']!=sha(self.engine/'plan.json') or descriptor['descriptor_sha256']!=recorded_stage['cli_plan_sha256'] or digest({k:v for k,v in descriptor.items() if k!='descriptor_sha256'})!=descriptor['descriptor_sha256']:
                    raise ValueError('qualification descriptor seal changed')
                receipt_path=self.engine/'jobs'/descriptor['spec']['jobs'][0]['label']/'execution.json'
                if sha(receipt_path)!=entry['receipt_sha256']:raise ValueError('qualification receipt changed')
                prefix=self.path_formatter(entry['profile']['project'])+'/'
                # Pair the identical positive design/rules/libraries. The negative
                # board deliberately differs, and its separate receipt binds it.
                shared={b['source'][len(prefix):]:b['sha256'] for b in descriptor['spec']['jobs'][0]['binds']
                        if b['source'].startswith(prefix) and Path(b['source']).name not in ('shorted.kicad_pcb','shorted.kicad_pro')}
                values.add((digest(shared),entry['profile']['tool_sha256']))
            return values
        positive=identities(pcb.get('verified_projects',{}));negative=identities(pcb.get('verified_negative_cases',{}))
        pcb['status']='PASS_LIMITED_PCB_TOOL_QUALIFICATION' if positive & negative else 'NOT_PROVEN'
        pcb['positive_qualification_passed']=bool(positive);pcb['real_short_qualification_passed']=bool(negative)
        pcb['matching_source_and_tool_identity']=bool(positive & negative)
        write(self.engine/'results.json',result)

def install(stage):
    package=BASE/'kicad-10.0.7-x86_64.exe'
    if not package.exists():
        r=stage.call([PYTHON,ROOT/'scripts/download_kicad.py'],'download',1000)
        if r['exit_code']:raise RuntimeError('official download failed; preserve partial and review evidence before resume')
    metadata=read(BASE/'download.json')
    if sha(package)!=metadata['sha256'] or package.stat().st_size!=968953784:raise ValueError('installer identity mismatch')
    # Query the built-in cmdlet directly. No execution policy override or change.
    signature_command="Get-AuthenticodeSignature -LiteralPath '"+str(package).replace("'","''")+"' | Select-Object @{Name='status';Expression={[string]$_.Status}},@{Name='subject';Expression={$_.SignerCertificate.Subject}},@{Name='thumbprint';Expression={$_.SignerCertificate.Thumbprint}} | ConvertTo-Json -Compress"
    r=stage.call([PS,'-NoProfile','-Command',signature_command],'installer-signature',30)
    if r['exit_code']:raise ValueError('native signature query failed; signature not established')
    signature=json.loads((stage.job/'installer-signature-stdout.log').read_text(encoding='utf-8-sig'))
    if signature.get('status')!='Valid' or 'KICAD SERVICES CORPORATION' not in str(signature.get('subject')).upper():
        raise ValueError('official installer signature invalid or unexpected signer')
    if TOOL.exists():raise ValueError('tool destination exists; do not overwrite installation')
    destination=TOOL.parents[1];destination.parent.mkdir(parents=True,exist_ok=True)
    r=stage.call([package,'/S','/CURRENTUSER','/D='+str(destination)],'installer',400)
    if r['exit_code'] or not TOOL.is_file():raise RuntimeError('installer failed or expected CLI missing')
    r=stage.call([TOOL,'version'],'version',20)
    version=Path(r['stdout']).read_text(errors='replace').strip()
    if r['exit_code'] or version!='10.0.7':raise ValueError('actual CLI version mismatch: '+version)
    lock={'version':version,'path':str(TOOL),'linux_path':linux(TOOL),'sha256':sha(TOOL),'installer_sha256':metadata['sha256'],
          'installer_signature':signature}
    write(BASE/'tool-lock.json',lock);write(stage.job/'installation.json',lock)
    return 0,None,None

def violations(report,kind,source,version):
    """Return native violations, rejecting missing report sections and wrong identities."""
    if not isinstance(report,dict) or report.get('$schema')!='https://schemas.kicad.org/'+kind.lower()+'.v1.json':
        raise ValueError('unknown native '+kind+' report schema')
    if report.get('kicad_version')!=version:raise ValueError('native report KiCad version mismatch')
    reported=report.get('source')
    if not isinstance(reported,str) or Path(reported).name.casefold()!=Path(source).name.casefold():
        raise ValueError('native report source mismatch')
    if kind=='ERC':
        sheets=report.get('sheets')
        if not isinstance(sheets,list) or not sheets:raise ValueError('native ERC sheets required')
        arrays=[s.get('violations') if isinstance(s,dict) else None for s in sheets]
    elif kind=='DRC':
        arrays=[report.get(k) for k in ('violations','unconnected_items','schematic_parity')]
    else:raise ValueError('unsupported native report kind')
    if any(not isinstance(v,list) for v in arrays):raise ValueError('incomplete native '+kind+' report')
    items=[item for array in arrays for item in array]
    for item in items:
        if (not isinstance(item,dict) or not isinstance(item.get('type'),str)
                or item.get('severity') not in ('error','warning') or not isinstance(item.get('items'),list)):
            raise ValueError('invalid native violation record')
    return items

def check_result(stage,result,path,kind,source,version):
    if result['exit_code'] not in (0,5):
        raise RuntimeError('native '+kind+' tool failure: exit '+str(result['exit_code']))
    records=violations(read(path),kind,source,version)
    if (result['exit_code']==0)!=(len(records)==0):
        raise ValueError('native '+kind+' exit/report disagreement')
    return records

def reuse_erc(stage,name,schematic,version):
    if not name.replace('_','').isalnum():raise ValueError('safe ERC source stage required')
    entry=read(ENGINE/'resources.json')['stages'][name]
    old_job=entry['jobs'][0];directory=ENGINE/'jobs'/old_job['label']
    receipt_path=directory/'execution.json';receipt=read(receipt_path)
    if entry['state']!='SETTLED' or sha(receipt_path)!=old_job['receipt_sha256']:
        raise ValueError('ERC source receipt seal mismatch')
    descriptor=read(ENGINE/'cli-plans'/(name+'.json'))
    if descriptor['workflow_plan_sha256']!=sha(ENGINE/'plan.json') or descriptor['descriptor_sha256']!=entry['cli_plan_sha256'] or digest({k:v for k,v in descriptor.items() if k!='descriptor_sha256'})!=descriptor['descriptor_sha256']:
        raise ValueError('ERC source descriptor seal mismatch')
    spec=descriptor['spec'];original=spec['jobs'][0]
    for bind in original['binds']:
        if bind['source'].endswith('.kicad_pcb') or '/verification-profile.json' in bind['source']:continue
        matches=[p for p in stage.inputs if Path(p).name==Path(bind['source']).name]
        if len(matches)!=1 or stage.inputs[matches[0]]!=bind['sha256']:
            raise ValueError('ERC source dependency differs from current input: '+bind['source'])
    report_path=directory/'erc.json'
    if receipt['artifact_sha256'].get('erc.json')!=sha(report_path) or spec['identity_hashes'].get(linux(TOOL))!=sha(TOOL):
        raise ValueError('ERC source artifact/tool identity changed')
    calls=[c for c in receipt['actual_native_calls'] if c['argv'][1:3]==['sch','erc']]
    if len(calls)!=1 or calls[0]['exit_code']!=0 or violations(read(report_path),'ERC',schematic,version):
        raise ValueError('reused native ERC did not pass')
    (stage.job/'erc.json').write_bytes(report_path.read_bytes())
    provenance={'kind':'REUSED_ACTUAL_NATIVE_ERC_NOT_REEXECUTED','stage':name,'receipt_sha256':sha(receipt_path),'report_sha256':sha(report_path),'source_receipt':linux(receipt_path),'native_call':calls[0]}
    write(stage.job/'erc-provenance.json',provenance)
    return provenance

REQUIRED_LAYERS=('F.Cu','B.Cu','F.Mask','B.Mask','F.SilkS','Edge.Cuts')

def inspect_exports(stage):
    """Check the frozen exported file set and syntax framing, not fabrication fitness."""
    gerbers=[p for p in (stage.job/'gerbers').iterdir() if p.is_file()]
    extensions={'F.Cu':'.gtl','B.Cu':'.gbl','F.Mask':'.gts','B.Mask':'.gbs','F.SilkS':'.gto','Edge.Cuts':'.gm1'}
    layer_files={}
    for layer in REQUIRED_LAYERS:
        matches=[p for p in gerbers if p.stem.endswith('-'+layer.replace('.','_')) or p.suffix.lower()==extensions[layer]]
        if len(matches)!=1:raise ValueError('missing/ambiguous required Gerber layer: '+layer)
        data=matches[0].read_text(encoding='utf-8-sig')
        if '%FS' not in data or not data.rstrip().endswith('M02*'):
            raise ValueError('incomplete native Gerber file: '+matches[0].name)
        layer_files[layer]=matches[0].name
    drills=[p for p in (stage.job/'drill').iterdir() if p.is_file() and p.suffix.lower()=='.drl']
    if not drills:raise ValueError('Excellon drill file missing')
    hits=0
    for path in drills:
        data=path.read_text(encoding='utf-8-sig');lines=[line.strip() for line in data.splitlines() if line.strip()]
        if not lines or lines[0]!='M48' or lines[-1]!='M30' or not any(line in ('%','M95') for line in lines):
            raise ValueError('incomplete native Excellon file: '+path.name)
        tool_definitions=[line for line in lines if re.fullmatch(r'T\d+C\d+(?:\.\d*)?',line)]
        coordinates=[line for line in lines if re.fullmatch(r'(?:X-?\d+(?:\.\d*)?(?:Y-?\d+(?:\.\d*)?)?|Y-?\d+(?:\.\d*)?)',line)]
        if coordinates and not tool_definitions:raise ValueError('Excellon hits lack drill tool definitions')
        hits+=len(coordinates)
    if hits<stage.profile['minimum_drill_hits']:raise ValueError('required native drill hits missing')
    with (stage.job/'bom.csv').open(encoding='utf-8-sig',newline='') as stream:
        reader=csv.DictReader(stream)
        if reader.fieldnames!=['Reference','Value','Footprint']:raise ValueError('unexpected BOM columns')
        rows=list(reader)
    references=set()
    for row in rows:
        if set(row)!={'Reference','Value','Footprint'} or any(not value for value in row.values()):
            raise ValueError('incomplete native BOM row')
        refs={ref.strip() for ref in row['Reference'].split(',')}
        if '' in refs or references & refs:raise ValueError('empty/duplicate native BOM reference')
        references.update(refs)
    if references!=set(stage.profile['expected_references']):raise ValueError('native BOM reference set mismatch')
    for directory in ('gerbers','drill'):
        for path in (stage.job/directory).iterdir():
            if path.is_file(): (stage.job/(directory+'-'+path.name)).write_bytes(path.read_bytes())
    return {'gerber_layers':layer_files,'excellon_files':[p.name for p in drills],
            'excellon_coordinate_hits':hits,'bom_references':sorted(references),
            'scope':'native export file framing/completeness; no independent Gerber geometry or fabrication validation'}

def validate(stage,negative=False):
    lock=read(BASE/'tool-lock.json')
    if lock['version']!='10.0.7' or sha(TOOL)!=lock['sha256']:raise ValueError('frozen KiCad tool changed')
    board=Path(stage.profile['board']);schematic=Path(stage.profile['schematic'])
    if not negative:
        erc_path=stage.job/'erc.json'
        if stage.profile.get('reuse_erc_stage'):
            reuse_erc(stage,stage.profile['reuse_erc_stage'],schematic,lock['version']);records=[]
        else:
            erc=stage.call([TOOL,'sch','erc','--format','json','--severity-all','--exit-code-violations','-o',erc_path,schematic],'erc',40)
            records=check_result(stage,erc,erc_path,'ERC',schematic,lock['version'])
        if records:
            write(stage.job/'pcb-checks.json',{'status':'FAIL','kind':'ERC','violations':len(records),'actual_tool_exit_code':erc['exit_code']})
            return 5,'actual ERC violations','DESIGN_VIOLATION'
    drc_path=stage.job/'drc.json'
    args=[TOOL,'pcb','drc','--format','json','--severity-all','--exit-code-violations','-o',drc_path]
    if not negative:args.append('--schematic-parity')
    drc=stage.call([*args,board],'drc',40);records=check_result(stage,drc,drc_path,'DRC',board,lock['version'])
    if negative:
        short_uuid=stage.profile['short_segment_uuid']
        matches=[record for record in records if record['type']=='shorting_items' and
                 any(isinstance(item,dict) and item.get('uuid')==short_uuid for item in record['items'])]
        detected=drc['exit_code']==5 and bool(matches)
        stage.verify_identities()
        write(stage.job/'pcb-checks.json',{'status':'REAL_NEGATIVE_DETECTED' if detected else 'NEGATIVE_NOT_DETECTED',
              'actual_tool_exit_code':drc['exit_code'],'violations':len(records),'matched_short_violations':matches,
              'short_segment_uuid':short_uuid,'negative_case':'real SOURCE/RETURN copper short'})
        # The design really fails DRC. The separate negative-check status records
        # whether that failure proves the tool integration's frozen expectation.
        if detected:return 5,'actual native shorting_items violation matched frozen copper UUID','DESIGN_VIOLATION'
        return (5,'native design violations exist; required short check not satisfied','DESIGN_VIOLATION') if records else (1,'required real short not identified','NEGATIVE_NOT_DETECTED')
    if records:
        write(stage.job/'pcb-checks.json',{'status':'FAIL','kind':'DRC','violations':len(records),'actual_tool_exit_code':drc['exit_code']})
        return 5,'actual DRC violations','DESIGN_VIOLATION'
    exports=[('gerbers',[TOOL,'pcb','export','gerbers','--layers',','.join(REQUIRED_LAYERS),'-o',str(stage.job/'gerbers')+os.sep,board]),
             ('drill',[TOOL,'pcb','export','drill','-o',str(stage.job/'drill')+os.sep,board]),
             ('bom',[TOOL,'sch','export','bom','--fields','Reference,Value,Footprint','--labels','Reference,Value,Footprint',
                     '--group-by','Reference','--field-delimiter',',','--string-delimiter','"',
                     '--ref-delimiter',',','--ref-range-delimiter','','-o',stage.job/'bom.csv',schematic])]
    for name,argv in exports:
        r=stage.call(argv,name,40)
        if r['exit_code']:raise RuntimeError('native export failed: '+name)
    export_checks=inspect_exports(stage)
    stage.verify_identities()
    write(stage.job/'pcb-checks.json',{'status':'PASS_PCB_DIGITAL_FILES','erc_violations':0,'drc_violations':0,
          'tool_version':lock['version'],'tool_sha256':lock['sha256'],'input_hashes':{linux(p):h for p,h in stage.inputs.items()},
          'exports':{p.name:sha(p) for p in stage.job.iterdir() if p.is_file() and (p.name.startswith(('gerbers-','drill-')) or p.name=='bom.csv')},
          'export_checks':export_checks,'profile':stage.profile,
          'scope':'native ERC/DRC/parity and specified digital exports only; no physical electrical testing/manufacturing release'})
    return 0,None,None

def project_profile(args):
    project=Path(args.project).resolve()
    if not project.is_dir() or project.is_relative_to(ENGINE.resolve()):
        raise ValueError('existing input project directory outside the execution ledger required')
    def input_file(value,suffix):
        path=Path(value);path=(path if path.is_absolute() else project/path).resolve()
        if not path.is_file() or not path.is_relative_to(project) or path.suffix!=suffix:
            raise ValueError('existing '+suffix+' inside the input project required')
        return path
    negative=args.command=='negative'
    board=input_file(args.board or ('shorted.kicad_pcb' if negative else 'coupon.kicad_pcb'),'.kicad_pcb')
    schematic=input_file(args.schematic or 'coupon.kicad_sch','.kicad_sch')
    if re.search(r'"In\d+\.Cu"',board.read_text(encoding='utf-8-sig')):
        raise ValueError('this frozen export profile supports two outer copper layers only')
    if not negative and (board.parent!=schematic.parent or board.stem!=schematic.stem):
        raise ValueError('native schematic parity requires same-directory, same-stem PCB and schematic')
    rules=board.with_suffix('.kicad_pro')
    if not rules.is_file():raise ValueError('explicit same-stem project rules file required: '+str(rules))
    qualification=(project.is_relative_to(BASE.resolve()) and not project.is_relative_to(ENGINE.resolve())
                   and board.name==('shorted.kicad_pcb' if negative else 'coupon.kicad_pcb') and schematic.name=='coupon.kicad_sch'
                   and (project/'case-definition.json').is_file())
    if qualification:
        frozen=read(BASE/'PCB_ACCEPTANCE_FROZEN.json');definition=read(project/'case-definition.json')
        qualification=definition.get('short_segment_uuid')==frozen['required_negative']['item_uuid']
    raw_references=args.expected_references
    if raw_references is None:
        if not qualification:raise ValueError('--expected-references is required for caller projects')
        raw_references='J1,J2,R1'
    references=[value.strip() for value in raw_references.split(',')]
    if not references or any(not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*',value) for value in references) or len(set(references))!=len(references):
        raise ValueError('distinct nonempty comma-separated references required')
    qualification=qualification and set(references)=={'J1','J2','R1'}
    profile={'project':str(project),'board':str(board),'schematic':str(schematic),'rules':str(rules),
             'expected_references':sorted(references),'required_gerber_layers':list(REQUIRED_LAYERS),
             'minimum_drill_hits':2 if qualification else 0,'qualification_case':qualification,
             'scope':'two outer copper layers, masks, front silkscreen and outline; native ERC/DRC/parity and digital exports',
             'short_segment_uuid':None}
    if negative:
        definition=read(project/'case-definition.json');short_uuid=definition.get('short_segment_uuid')
        if definition.get('negative')!=board.name or not isinstance(short_uuid,str) or not re.fullmatch(r'[0-9a-fA-F-]{36}',short_uuid):
            raise ValueError('negative case-definition must bind board name and actual added copper UUID')
        if short_uuid not in board.read_text(encoding='utf-8-sig'):
            raise ValueError('frozen negative copper UUID absent from board input')
        if sha(rules)!=sha(schematic.with_suffix('.kicad_pro')):
            raise ValueError('negative project rules must match frozen positive project rules')
        profile['short_segment_uuid']=short_uuid
    inputs={}
    for path in sorted(project.rglob('*')):
        if path.is_file() and path.suffix!='.kicad_prl' and not path.name.endswith('.lck'):
            if not path.resolve().is_relative_to(project):raise ValueError('project dependency escapes the frozen input directory')
            inputs[path.resolve()]=sha(path)
    profile['source_manifest_sha256']=digest({linux(p):h for p,h in inputs.items()})
    profile['tool_sha256']=read(BASE/'tool-lock.json')['sha256']
    profile['reuse_erc_stage']=args.reuse_erc_stage if not negative else None
    return inputs,profile

def main():
    parser=argparse.ArgumentParser();parser.add_argument('command',choices=['install','run','negative','status']);parser.add_argument('--stage',required=False)
    parser.add_argument('--project',default=str(PROJECT),help='directory containing the real input project and local dependencies')
    parser.add_argument('--board',help='PCB path, absolute or relative to --project')
    parser.add_argument('--schematic',help='schematic path, absolute or relative to --project')
    parser.add_argument('--expected-references',help='frozen comma-separated BOM reference set; required for caller projects')
    parser.add_argument('--reuse-erc-stage',help='reuse exact unchanged schematic dependencies from a sealed native ERC pass')
    args=parser.parse_args()
    with locked():
        initialize()
        if args.command=='status':
            print(json.dumps({'plan':read(ENGINE/'plan.json'),'resources':read(ENGINE/'resources.json'),'results':read(ENGINE/'results.json')}));return 0
        name=args.stage or {'install':'PCB_INSTALL_01','run':'PCB_POSITIVE_01','negative':'PCB_NEGATIVE_01'}[args.command]
        if not name.replace('_','').isalnum():raise ValueError('safe stage name required')
        tools={Path(__file__):sha(__file__),ROOT/'implementation/windows_process.py':sha(ROOT/'implementation/windows_process.py')}
        inputs={};profile=None
        if args.command=='install':
            cap={'cpu':200,'wall':1400};inputs={BASE/'official-source.json':sha(BASE/'official-source.json')}
            tools.update({ROOT/'scripts/download_kicad.py':sha(ROOT/'scripts/download_kicad.py'),ROOT/'scripts/kicad_signature.ps1':sha(ROOT/'scripts/kicad_signature.ps1')})
        else:
            cap={'cpu':35,'wall':120} if args.command=='run' else {'cpu':15,'wall':40}
            inputs,profile=project_profile(args)
            tools[TOOL]=read(BASE/'tool-lock.json')['sha256']
        stage=Stage(name,name.lower(),cap,inputs,tools,args.command,profile);stage.begin()
        try:
            code,reason,kind=install(stage) if args.command=='install' else validate(stage,args.command=='negative')
        except TimeoutError as error:code,reason,kind=124,str(error),'BUDGET'
        except (OSError,ValueError,RuntimeError) as error:code,reason,kind=1,str(error),'TOOL_ERROR'
        result=stage.finish(code,reason,kind);print(json.dumps(result));return 0 if result['state']=='SUCCEEDED' and result['exit_code']==0 else 1

if __name__=='__main__':raise SystemExit(main())
