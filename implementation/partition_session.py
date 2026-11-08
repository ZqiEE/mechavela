"""Local deterministic synthesis sessions above the unchanged execution core."""
import copy,json,os,re,time,resource,math,argparse,sys,fcntl
from pathlib import Path
from .partition import *
from core.ledger import Budget
from core.sandbox import Sandbox
from .interface_boundary import analyze as interface_analyze,apply as interface_apply

POLICY='hierarchical-v1:keep-architectural-cells;opt-fast-noff-purge;memory-map;techmap;dfflibmap;abc-lib;init-metadata-retained'
def write(p,v):
 p=Path(p);temp=p.with_name(p.name+'.tmp');temp.write_bytes(encoded(v));os.replace(temp,p)
def resource_total():
 r=resource.getrusage(resource.RUSAGE_SELF);return r.ru_utime+r.ru_stime
class Session:
 def __init__(self,directory):
  self.directory=Path(directory).resolve();self.plan=load(self.directory/'plan.json')
  self.rows=load(self.directory/'inventory.json');self.library=set(self.plan['library_cells'])
  self.cache=self.directory/'cache';self.jobs=self.directory/'jobs';self.events=self.directory/'events.jsonl'
 @classmethod
 def create(cls,directory,roles,tool,library,budget):
  if any(type(budget.get(k)) not in (int,float) or not 0<budget[k]<=cap for k,cap in [('cpu',7200),('wall',3600)]):raise ValueError('explicit authorized resource budget required')
  if budget.get('ram_bytes')!=4*1024**3 or budget.get('external_spend_usd',0)!=0 or budget.get('concurrency',1)!=1:raise ValueError('fixed resource constraints violated')
  if budget['cpu']<120 or budget['wall']<120:raise ValueError('inventory preprocessing needs explicit120CPU/120wall allowance within session cap')
  directory=Path(directory).resolve()
  if directory.exists():raise ValueError('session already exists; use status/resume')
  directory.mkdir(parents=True);(directory/'inputs').mkdir();(directory/'cache').mkdir();(directory/'jobs').mkdir()
  begin=time.monotonic();cpu=resource_total();inventory={};bindings={};orders={};physical={};root={};interfaces={};tool=Path(tool).resolve();library=Path(library).resolve()
  for role,b in roles.items():
   if not re.fullmatch('[a-zA-Z0-9_-]{1,32}',role):raise ValueError('unsafe role')
   path=Path(b['json']).resolve()
   if path.stat().st_size>256*1024**2:raise ValueError('elaboration JSON size limit exceeded')
   if not re.fullmatch('[a-f0-9]{64}',b.get('candidate_id','')):raise ValueError('full candidate identity required')
   if file_hash(path)!=b['json_sha256'] or file_hash(b['rtlil'])!=b['rtlil_sha256']:raise ValueError('frozen input identity mismatch')
   original_mods=load(path)['modules'];key=signatures(original_mods);interfaces[role]=interface_analyze(original_mods,b.get('top','Vortex'));mods=interface_apply(original_mods,interfaces[role]);order=reachable(mods,b.get('top','Vortex'));orders[role]=[key(n) for n in order];root[role]=name(key(b.get('top','Vortex')));bindings[role]=b
   for n in order:
    if time.monotonic()-begin>min(120,budget['wall']) or resource_total()-cpu>min(120,budget['cpu']):raise ValueError('bounded inventory budget exhausted; incomplete session is not runnable')
    k=key(n);canonical=name(k);m=normalize(mods,n,key)
    if blackbox(m):
     if k in physical and abi(physical[k])!=abi(m):raise ValueError('physical module key collision')
     physical[k]=m;continue
    if k in inventory:
     inventory[k]['sources'].append(dict(role=role,module=n));continue
    kids={c['type'] for c in m.get('cells',{}).values() if c['type'].startswith('atoms_p_')}
    definitions={name(key(x)):normalize(mods,x,key) for x in mods if name(key(x)) in kids}
    raw=dict(modules={canonical:m,**{cn:boundary(cm) for cn,cm in definitions.items()}})
    fn=directory/'inputs'/(k+'.json');write(fn,raw)
    width=sum(len(bs) for c in m.get('cells',{}).values() if not c['type'].startswith('atoms_p_') for bs in c.get('connections',{}).values())
    arithmetic=sum(len(c['connections'].get('A',[]))*len(c['connections'].get('B',[])) for c in m.get('cells',{}).values() if c['type'] in ('$mul','$div','$mod','$pow'))
    inventory[k]=dict(key=k,top=canonical,input_sha256=file_hash(fn),sources=[dict(role=role,module=n)],risk=width+arithmetic,cells=len(m.get('cells',{})),children=sorted(kids))
  text=library.read_text();cells=sorted(set(re.findall(r'cell\s*\(\s*(\w+)\s*\)',text)));assert cells
  write(directory/'interface-evidence.json',interfaces)
  plan=dict(version=2,roles=bindings,roots=root,orders=orders,budget=budget,tool=str(tool),tool_identity=dict(wrapper=file_hash(tool/'bin/yosys'),binary=file_hash(tool/'libexec/yosys')),library=str(library),library_sha256=file_hash(library),library_cells=cells,policy=POLICY,payload_sha256=file_hash(Path(__file__).with_name('partition_payload.py')),abc_script_sha256=file_hash(Path(__file__).with_name('abc_bounded.script')),interface_evidence_sha256=file_hash(directory/'interface-evidence.json'),physical_boundaries={k:name(k) for k in physical},scope='standard-cell logic mapping with explicit physical SRAM boundaries; full PPA NOT_SUPPORTED')
  write(directory/'physical.json',dict(modules={name(k):m for k,m in physical.items()}));write(directory/'inventory.json',inventory);write(directory/'plan.json',plan)
  write(directory/'resources.json',dict(cpu=resource_total()-cpu,wall=time.monotonic()-begin,components=[dict(stage='inventory',cpu=resource_total()-cpu,wall=time.monotonic()-begin)]))
  return cls(directory).status()
 def identity(self,k):return dict(source_key=k,derived_input_sha256=self.rows[k]['input_sha256'],tool_identity=self.plan['tool_identity'],library_sha256=self.plan['library_sha256'],policy=self.plan['policy'])
 def _cached(self,k):
  p=self.cache/k
  if not (p/'manifest.json').exists():return None
  return verify_cache(p,self.identity(k))
 def status(self):
  stats=dict(required_logic_classes=len(self.rows),mapped=0,local_functional_pass=0,not_mapped=0,not_proven=0)
  for k in self.rows:
   m=self._cached(k)
   if m:
    stats['mapped']+=1;stats['local_functional_pass']+=m.get('functional')=='PASS'
   else:stats['not_mapped']+=1
  stats['not_proven']=len(self.rows)-stats['local_functional_pass'];cost=load(self.directory/'resources.json');b=self.plan['budget']
  links={r:load(self.directory/('link-'+r+'.json')) for r in self.plan['roles'] if (self.directory/('link-'+r+'.json')).exists()}
  state='READY' if stats['not_mapped'] else 'PARTITIONS_MAPPED'
  if len(links)==len(self.plan['roles']):state='LOGIC_LINKED' if all(x.get('logic_mapping')=='PASS' for x in links.values()) else 'LINK_BLOCKED'
  return dict(session=str(self.directory),status=state,coverage=stats,resources=dict(cpu_seconds=cost['cpu'],wall_seconds=cost['wall'],breakdown=str(self.directory/'resources.json')),remaining=dict(cpu=max(0,b['cpu']-cost['cpu']),wall=max(0,b['wall']-cost['wall'])),links=links,physical_boundary_types=len(self.plan['physical_boundaries']),full_gpu_behavioral='NOT_SUPPORTED: physical RAM boundary semantics missing' if self.plan['physical_boundaries'] else 'NOT_PROVEN',ppa='NOT_SUPPORTED',user_goal_achieved=False)
 def _event(self,v):
  with self.events.open('ab') as f:f.write(encoded({**v,'created':time.time()})+b'\n')
 def _admit(self,cpu,wall):
  used=load(self.directory/'resources.json');b=self.plan['budget']
  if used['cpu']+cpu>b['cpu'] or used['wall']+wall>b['wall']:raise ValueError('INSUFFICIENT_WHOLE_BATCH_BUDGET')
  if file_hash(Path(self.plan['tool'])/'bin/yosys')!=self.plan['tool_identity']['wrapper'] or file_hash(Path(self.plan['tool'])/'libexec/yosys')!=self.plan['tool_identity']['binary']:raise ValueError('tool identity changed')
  if file_hash(self.plan['library'])!=self.plan['library_sha256']:raise ValueError('technology library changed')
  if file_hash(Path(__file__).with_name('partition_payload.py'))!=self.plan['payload_sha256']:raise ValueError('session recipe changed')
  if self.plan.get('abc_script_sha256') and file_hash(Path(__file__).with_name('abc_bounded.script'))!=self.plan['abc_script_sha256']:raise ValueError('ABC recipe changed')
 def _charge(self,cpu,wall,component):
  used=load(self.directory/'resources.json');used['cpu']+=cpu;used['wall']+=wall;used['components'].append(component);write(self.directory/'resources.json',used)
 def _execute(self,mode,k,cpu,wall,extra=()):
  row=self.rows.get(k,{});top=row.get('top',k);job_id=f'{mode}-{time.time_ns()}';p=self.jobs/job_id;p.mkdir()
  request=dict(mode=mode,top=top,library_cells=self.plan['library_cells']);write(p/'request.json',request)
  if row and file_hash(self.directory/'inputs'/(k+'.json'))!=row['input_sha256']:raise ValueError('source partition changed')
  b=Budget.parse(dict(cpu_time_seconds=cpu,wall_time_seconds=wall,ram_bytes=4*1024**3,disk_bytes=1024**3,concurrency=1,max_candidates=1,log_bytes=8*1024**2,external_spend_limit_usd=0))
  binds=[(str(Path(__file__).parent),'/scripts'),(str(p/'request.json'),'/request.json'),(self.plan['tool'],'/tool'),(self.plan['library'],'/library.lib')]
  if row:binds.append((str(self.directory/'inputs'/(k+'.json')),'/source.json'))
  if mode in ('proof','export'):binds.append((str(self.cache/k/'mapped.json'),'/mapped.json'))
  binds.extend(extra);self._event(dict(job=job_id,mode=mode,key=k,state='STARTED',budget=b))
  r=Sandbox('/opt/atoms-g03/partition-engine-sandboxes').run(['python3','/scripts/partition_payload.py'],binds,b)
  (p/'driver.log').write_bytes(r.pop('log'))
  for fn,data in r.pop('artifacts').items():(p/fn).write_bytes(data)
  r.update(mode=mode,key=k,source_input_sha256=row.get('input_sha256'),job_id=job_id,budget=b);write(p/'execution.json',r);self._event(dict(job=job_id,state=r['state'],reason=r['reason'],cpu=r['cpu_seconds'],wall=r['wall_seconds']));return p,r
 def run(self,mode='map',keys=None,limit=12,cpu_per=120,wall_per=90,stop_on_failure=True,checkpoint=None,checkpoint_sha256=None):
  if mode not in ('map','resume','proof','export'):raise ValueError('unsupported stage')
  if mode=='resume' and (not checkpoint or file_hash(checkpoint)!=checkpoint_sha256 or not keys or len(keys)!=1):raise ValueError('exact single checkpoint binding required')
  if mode=='resume':
   cp=Path(checkpoint).resolve()
   if cp.parent.parent!=self.jobs.resolve():raise ValueError('checkpoint not owned by session')
   producer=load(cp.parent/'execution.json')
   if producer['key']!=keys[0] or producer.get('source_input_sha256')!=self.rows[keys[0]]['input_sha256']:raise ValueError('checkpoint source/config mismatch')
  if type(limit) is not int or not 1<=limit<=500:raise ValueError('invalid batch safety cap')
  failure_path=self.directory/'failures.json';failures=load(failure_path) if failure_path.exists() else {}
  def attempted_same_failure(k):
   f=failures.get(mode+':'+k);return f and f.get('recipe')==self.plan['payload_sha256'] and f.get('source')==self.rows[k]['input_sha256']
  if keys is None:keys=[k for k in sorted(self.rows,key=lambda k:self.rows[k]['risk'],reverse=True) if not attempted_same_failure(k) and (not self._cached(k) if mode=='map' else self._cached(k) and (self._cached(k).get('functional')!='PASS' if mode=='proof' else not (self.cache/k/'mapped.v').exists()))][:limit]
  if len(keys)>limit or len(set(keys))!=len(keys) or any(k not in self.rows for k in keys):raise ValueError('invalid class selection')
  self._admit(len(keys)*cpu_per+5,len(keys)*wall_per+5);begin=time.monotonic();helper=resource_total();sandbox_cpu=0;results=[]
  with (self.directory/'run.lock').open('a') as lock:
   fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
   try:
    for k in keys:
     cached=self._cached(k)
     if mode in ('map','resume') and cached or mode=='proof' and cached and cached.get('functional')=='PASS':results.append(dict(key=k,status='REUSED'));continue
     p,r=self._execute(mode,k,cpu_per,wall_per,[(str(Path(checkpoint).resolve()),'/checkpoint.il')] if mode=='resume' else ());sandbox_cpu+=r['cpu_seconds'];ok=r['state']=='SUCCEEDED'
     if ok:
      cache=self.cache/k;cache.mkdir(exist_ok=True)
      if mode in ('map','resume'):
       for fn in ('mapped.json','mapped.v','gate.json'):(cache/fn).write_bytes((p/fn).read_bytes())
       meta={**self.identity(k),'mapping':'PASS','functional':'NOT_RUN','evidence':str(p),'mapping_script_sha256':file_hash(p/'map.ys'),'mapping_profile':'strash/get/nf/put bounded combinational library map; FFs outside ABC','checkpoint_sha256':checkpoint_sha256 if mode=='resume' else None,'files':{fn:file_hash(cache/fn) for fn in ('mapped.json','mapped.v','gate.json')}};write(cache/'manifest.json',meta)
      elif mode=='proof':
       meta=self._cached(k);meta['functional']='PASS';meta['functional_evidence']=str(p);write(cache/'manifest.json',meta)
      elif mode=='export':
       (cache/'mapped.v').write_bytes((p/'mapped.v').read_bytes());meta=self._cached(k);meta['files']['mapped.v']=file_hash(cache/'mapped.v');write(cache/'manifest.json',meta)
     results.append(dict(key=k,state=r['state'],reason=r['reason'],exit_code=r['exit_code'],cpu=r['cpu_seconds'],wall=r['wall_seconds'],memory=r['memory_peak_bytes'],evidence=str(p)))
     if not ok:
      failures[mode+':'+k]=dict(recipe=self.plan['payload_sha256'],source=self.rows[k]['input_sha256'],reason=r['reason'] or 'TOOL_ERROR',evidence=str(p));write(failure_path,failures)
     if not ok and stop_on_failure:break
   finally:
    totalcpu=sandbox_cpu+resource_total()-helper;totalwall=time.monotonic()-begin;self._charge(totalcpu,totalwall,dict(stage=mode,sandbox_cpu=sandbox_cpu,driver_cpu=resource_total()-helper,total_wall=totalwall,jobs=len(results)))
  records=self.directory/('batch-'+str(time.time_ns())+'.json');write(records,dict(results=results))
  return dict(processed=len(results),succeeded=sum(r.get('state')=='SUCCEEDED' for r in results),reused=sum(r.get('status')=='REUSED' for r in results),failed=[r for r in results if r.get('state') not in ('SUCCEEDED',None)],records=str(records),status=self.status())
 def link(self,roles=None,cpu_per=600,wall_per=300):
  roles=roles or list(self.plan['roles']);missing=[k for k in self.rows if not self._cached(k) or not (self.cache/k/'mapped.v').exists()]
  if missing:raise ValueError('COMPLETE_LINK_NOT_READY: '+str(len(missing))+' required mapped netlists missing')
  self._admit(len(roles)*cpu_per+5,len(roles)*wall_per+5);begin=time.monotonic();helper=resource_total();sandbox=0;results={}
  with (self.directory/'run.lock').open('a') as lock:
   fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
   try:
    for role in roles:
     if role not in self.plan['roots']:raise ValueError('unknown role')
     keys=list(dict.fromkeys(k for k in self.plan['orders'][role] if k in self.rows));planfile=self.directory/('link-plan-'+role+'.json');write(planfile,dict(netlists=['/cache/'+k+'/mapped.v' for k in keys]))
     p,r=self._execute('link',self.plan['roots'][role],cpu_per,wall_per,[(str(self.cache),'/cache'),(str(planfile),'/link-plan.json'),(str(self.directory/'physical.json'),'/blackboxes.json')]);sandbox+=r['cpu_seconds'];v=dict(state=r['state'],exit_code=r['exit_code'],reason=r['reason'],cpu=r['cpu_seconds'],wall=r['wall_seconds'],memory=r['memory_peak_bytes'],evidence=str(p),physical_boundary_types=len(self.plan['physical_boundaries']),functional='NOT_PROVEN',ppa='NOT_SUPPORTED')
     if r['state']=='SUCCEEDED':v['netlist_sha256']=file_hash(p/'full.v');v['logic_mapping']='PASS';v['netlist_bytes']=(p/'full.v').stat().st_size
     else:v['logic_mapping']='NOT_COMPLETED'
     write(self.directory/('link-'+role+'.json'),v);results[role]=v;print(json.dumps(dict(event='link_finished',role=role,**v)),flush=True)
   finally:self._charge(sandbox+resource_total()-helper,time.monotonic()-begin,dict(stage='link',sandbox_cpu=sandbox,driver_cpu=resource_total()-helper,total_wall=time.monotonic()-begin))
  return results
def main():
 p=argparse.ArgumentParser();p.add_argument('operation',choices=['create','run','link','status']);p.add_argument('--directory',required=True);p.add_argument('--args-file');a=p.parse_args();data=load(a.args_file) if a.args_file else {}
 try:
  result=Session.create(a.directory,**data) if a.operation=='create' else getattr(Session(a.directory),a.operation)(**data);print(json.dumps(result));return 0
 except (ValueError,OSError) as e:print(json.dumps(dict(status='REJECTED',detail=str(e))));return 1
if __name__=='__main__':raise SystemExit(main())
