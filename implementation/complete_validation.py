"""Single-coordinator engineering workflow: immutable gates and durable budgets."""
import argparse,fcntl,json,os,time,resource,hashlib,math
from contextlib import contextmanager
from pathlib import Path
from core.ledger import Budget
from core.sandbox import Sandbox
import core.sandbox as sandbox_module

def read(p):return json.loads(Path(p).read_text())
def write(p,v):
 p=Path(p);tmp=p.with_name(p.name+'.tmp');tmp.write_text(json.dumps(v,indent=2,sort_keys=True));os.replace(tmp,p)
def sha(p):
 h=hashlib.sha256()
 with Path(p).open('rb') as f:
  for b in iter(lambda:f.read(1024**2),b''):h.update(b)
 return h.hexdigest()
def cpu():
 a=resource.getrusage(resource.RUSAGE_SELF);b=resource.getrusage(resource.RUSAGE_CHILDREN)
 return a.ru_utime+a.ru_stime+b.ru_utime+b.ru_stime
class Workflow:
 def __init__(self,root,lock_path='/opt/atoms-g03/complete-validation.lock'):
  self.root=Path(root);self.plan=read(self.root/'plan.json')
  self.lock_path=Path(lock_path);self.plan_sha256=sha(self.root/'plan.json')
  if sha(self.root/'matrix.json')!=self.plan['matrix_sha256']:raise ValueError('frozen acceptance changed')
 @contextmanager
 def locked(self):
  self.lock_path.parent.mkdir(parents=True,exist_ok=True)
  with self.lock_path.open('a+') as lock:
   fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
   try:yield
   finally:fcntl.flock(lock,fcntl.LOCK_UN)
 @staticmethod
 def check_cap(cap):
  if not isinstance(cap,dict) or set(cap)!={'cpu','wall'} or any(type(x) not in (int,float) or not math.isfinite(x) or x<=0 for x in cap.values()):raise ValueError('positive finite CPU and wall limits required')
 def reserve(self,name,cap):
  self.check_cap(cap)
  with self.locked():
   self._guard();self._reserve(name,cap)
 def _reserve(self,name,cap):
  self.check_cap(cap)
  if not isinstance(name,str) or not name or len(name)>128:raise ValueError('invalid stage name')
  q=read(self.root/'resources.json');self._validate_ledger(q);self._verify_jobs(q)
  if name in q['stages']:raise ValueError('stage exists; resume only sealed receipts')
  for key in ['cpu','wall']:
   held=sum(max(s['cap'][key],sum(j['cost'][key] for j in s['jobs'])) for s in q['stages'].values() if s['state'] in ['RESERVED','RUNNING','UNKNOWN_INTERRUPTED'])
   if q['spent'][key]+held+cap[key]+(self._legacy_hold(q) if key=='cpu' else 0)>self.plan['budget'][key]:raise ValueError('INSUFFICIENT_COMPLETE_STAGE_BUDGET')
  q['stages'][name]={'cap':cap,'state':'RESERVED','jobs':[]};write(self.root/'resources.json',q)
 def execute(self,stage,label,payload,binds,cap):
  self.check_cap(cap)
  if not label or Path(label).name!=label or label in ('.','..'):raise ValueError('invalid job label')
  with self.locked():
   start=time.monotonic();startcpu=cpu();self0=resource.getrusage(resource.RUSAGE_SELF)
   self._guard()
   q=read(self.root/'resources.json');self._validate_ledger(q);self._verify_jobs(q);s=q['stages'][stage]
   if s['state']=='SETTLED':raise ValueError('settled stage cannot be charged or executed again')
   if s['state'] in ('RUNNING','UNKNOWN_INTERRUPTED') or s.get('pending'):raise ValueError('UNKNOWN_INTERRUPTED: retain pending reservation and reconcile before resume')
   used={k:sum(j['cost'][k] for j in s['jobs']) for k in ['cpu','wall']}
   if any(used[k]+cap[k]>s['cap'][k] for k in ['cpu','wall']):raise ValueError('stage reservation insufficient')
   if any(j['label']==label for j in s['jobs']):raise ValueError('existing job cannot be replayed')
   for path,digest in self.plan['input_hashes'].items():
    if sha(path)!=digest:raise ValueError('frozen input identity changed: '+path)
   job=self.root/'jobs'/label;job.mkdir(parents=True,exist_ok=False)
   s['state']='RUNNING';s['pending']={'label':label,'cap':cap,'payload_sha256':sha(payload)};write(self.root/'resources.json',q)
   final_group={};cleanup_original=sandbox_module.remove_group
   def final_cleanup(group,timeout=2):
    try:
     deadline=time.monotonic()+timeout
     while 'populated 0' not in (group/'cgroup.events').read_text() and time.monotonic()<deadline:time.sleep(.01)
     if 'populated 0' not in (group/'cgroup.events').read_text():raise RuntimeError('UNKNOWN_FINAL_CGROUP: descendants not quiescent')
     final_group.update(dict(line.split() for line in (group/'cpu.stat').read_text().splitlines()))
    finally:cleanup_original(group,timeout)
   sandbox_module.remove_group=final_cleanup
   try:
    left={'cpu':cap['cpu']-(cpu()-startcpu),'wall':cap['wall']-(time.monotonic()-start)}
    if any(v<=0 for v in left.values()):
     final_group['usage_usec']='0'
     result={'log':b'','artifacts':{},'state':'BUDGET_EXCEEDED','reason':'PREFLIGHT_RESOURCE_LIMIT','exit_code':None,'cpu_seconds':0,'wall_seconds':0,'memory_peak_bytes':0,'disk_bytes':0}
    else:
     b=Budget.parse(dict(cpu_time_seconds=left['cpu'],wall_time_seconds=left['wall'],ram_bytes=4*1024**3,disk_bytes=1024**3,concurrency=1,max_candidates=1,log_bytes=8*1024**2,external_spend_limit_usd=0))
     result=Sandbox('/opt/atoms-g03/complete-validation-sandboxes').run(['python3','/payload.py'],[(str(payload),'/payload.py'),*binds],b)
    (job/'driver.log').write_bytes(result.pop('log'))
    artifact_hashes={'driver.log':sha(job/'driver.log')}
    for name,data in result.pop('artifacts').items():
     if not isinstance(name,str) or not name or Path(name).name!=name or name in ('.','..','execution.json','driver.log'):raise ValueError('unsafe or reserved artifact name')
     (job/name).write_bytes(data)
     artifact_hashes[name]=sha(job/name)
    result['artifact_sha256']=artifact_hashes
    self1=resource.getrusage(resource.RUSAGE_SELF);own=self1.ru_utime+self1.ru_stime-self0.ru_utime-self0.ru_stime
    if 'usage_usec' not in final_group:raise RuntimeError('UNKNOWN_FINAL_CGROUP: final CPU receipt unavailable; retain reservation')
    final_cpu=int(final_group['usage_usec'])/1e6
    self._numbers({'cpu':final_cpu,'wall':result['wall_seconds']},positive=False)
    if result['state']=='SUCCEEDED' and result['exit_code']!=0:raise ValueError('inconsistent success receipt')
    cost={'cpu':max(cpu()-startcpu,final_cpu+own),'wall':time.monotonic()-start}
    result['final_cgroup_cpu_seconds']=final_cpu;result['final_cgroup_quiescent']=True
    result['execution_binding']={'stage':stage,'label':label,'cap':cap,'payload_sha256':s['pending']['payload_sha256']}
    result['controller_cost']=cost;result['controller_cost_scope']='preflight/identity checks through final cleanup: final cgroup CPU plus coordinator self CPU, maximum with reaped-child accounting; unrelated authoring/shell launches outside this call remain separately UNKNOWN';write(job/'execution.json',result)
    s['jobs'].append({'label':label,'cost':cost,'state':result['state'],'receipt_sha256':sha(job/'execution.json')});s['state']='RESERVED';s.pop('pending',None)
    write(self.root/'resources.json',q);return result,job
   except BaseException as error:
    write(job/'interruption.json',{'state':'UNKNOWN_INTERRUPTED','error_type':type(error).__name__,'error':str(error),'observed_controller_cpu_seconds':max(0,cpu()-startcpu),'observed_elapsed_wall_seconds':time.monotonic()-start,'final_cgroup_sample':final_group or None,'pending_cap':cap,'cost_completeness':'UNKNOWN; retain reservation, do not settle'})
    s['state']='UNKNOWN_INTERRUPTED';write(self.root/'resources.json',q);raise
   finally:sandbox_module.remove_group=cleanup_original
 def settle(self,name):
  with self.locked():
   self._guard();self._settle(name)
 def _settle(self,name):
  q=read(self.root/'resources.json');self._validate_ledger(q);self._verify_jobs(q);s=q['stages'][name]
  if s['state']!='RESERVED':raise ValueError('stage has unresolved pending cost')
  for k in ['cpu','wall']:q['spent'][k]+=sum(j['cost'][k] for j in s['jobs'])
  s['state']='SETTLED';write(self.root/'resources.json',q)
 def _guard(self):
  if sha(self.root/'plan.json')!=self.plan_sha256:raise ValueError('frozen plan changed')
  if sha(self.root/'matrix.json')!=self.plan['matrix_sha256']:raise ValueError('frozen acceptance changed')
 @staticmethod
 def _numbers(values,positive=False):
  if not isinstance(values,dict) or set(values)!={'cpu','wall'} or any(type(x) not in (int,float) or not math.isfinite(x) or x<0 or (positive and x==0) for x in values.values()):raise ValueError('invalid finite resource accounting')
 def _validate_ledger(self,q):
  self._numbers(self.plan['budget'],positive=True);self._numbers(q['spent'])
  settled={'cpu':0,'wall':0};labels=set()
  for s in q['stages'].values():
   self.check_cap(s['cap'])
   if s['state'] not in ('RESERVED','RUNNING','UNKNOWN_INTERRUPTED','SETTLED'):raise ValueError('invalid reservation state')
   if s.get('pending') and s['state'] not in ('RUNNING','UNKNOWN_INTERRUPTED'):raise ValueError('unresolved pending cost')
   for j in s['jobs']:
    self._numbers(j['cost']);label=j['label']
    if not isinstance(label,str) or not label or Path(label).name!=label or label in ('.','..') or label in labels:raise ValueError('invalid or duplicate receipt label')
    labels.add(label)
    if s['state']=='SETTLED':
     for k in settled:settled[k]+=j['cost'][k]
  if any(q['spent'][k]+1e-8<settled[k] for k in settled):raise ValueError('spent below settled receipts')
 def _verify_jobs(self,q):
  self._legacy_stages=set()
  for stage_name,s in q['stages'].items():
   for j in s['jobs']:
    path=self.root/'jobs'/j['label']/'execution.json'
    if not path.is_file() or sha(path)!=j['receipt_sha256']:raise ValueError('receipt hash mismatch')
    receipt=read(path);self._numbers(receipt['controller_cost'])
    if any(abs(receipt['controller_cost'][k]-j['cost'][k])>1e-8 for k in ('cpu','wall')):
     self._verify_reconciliation(j,receipt)
     self._legacy_stages.add(stage_name)
    if receipt['state']!=j['state'] or (receipt['state']=='SUCCEEDED' and receipt['exit_code']!=0):raise ValueError('inconsistent success receipt')
    final_cpu=receipt.get('final_cgroup_cpu_seconds')
    if final_cpu is not None and (type(final_cpu) not in (int,float) or not math.isfinite(final_cpu) or final_cpu<0 or receipt['controller_cost']['cpu']+1e-8<final_cpu):raise ValueError('invalid final cgroup cost')
 def _verify_reconciliation(self,job,receipt):
  binding=read(self.root/'accounting-bindings.json')
  if sha(self.root/'cost-reconciliation.json')!=binding['reconciliation_sha256']:raise ValueError('reconciliation changed')
  expected=binding['receipts'][job['label']]
  self._numbers(expected['accepted_cost'])
  if expected['receipt_sha256']!=job['receipt_sha256']:raise ValueError('reconciliation receipt identity changed')
  rows=[v for v in read(self.root/'cost-reconciliation.json')['corrections'] if v['job']==job['label']]
  if len(rows)!=1:raise ValueError('missing unique reconciliation')
  row=rows[0];old=receipt['controller_cost'];raw=receipt['cpu_seconds']
  self._numbers({'cpu':row['old_process_counter'],'wall':row['raw_cgroup_observed_minimum']})
  self._numbers({'cpu':raw,'wall':old['wall']})
  target={'cpu':max(old['cpu'],raw),'wall':old['wall']}
  if any(abs(target[k]-job['cost'][k])>1e-8 or abs(target[k]-expected['accepted_cost'][k])>1e-8 for k in target):raise ValueError('reconciliation is not measured minimum')
  if abs(row['old_process_counter']-old['cpu'])>1e-8 or abs(row['raw_cgroup_observed_minimum']-raw)>1e-8 or row['final_tail']!='UNKNOWN for prior receipts; not invented':raise ValueError('reconciliation basis mismatch')
 def _legacy_hold(self,q):
  # Retain unused original stage CPU allowances for historical final-sample
  # gaps. This is a budget hold, never reported as measured CPU consumption.
  return sum(max(0,s['cap']['cpu']-sum(j['cost']['cpu'] for j in s['jobs'])) for name,s in q['stages'].items() if s['state']=='SETTLED' and name in self._legacy_stages)
 def status(self):
  with self.locked():
   try:
    self._guard();q=read(self.root/'resources.json');self._validate_ledger(q);self._verify_jobs(q)
    value=self._status_unlocked()
    successful={sha(self.root/'jobs'/j['label']/'execution.json') for s in q['stages'].values() for j in s['jobs'] if j['state']=='SUCCEEDED'}
    for gate,result in value['gates'].items():
     if result.get('status') in ('PASS','PASS_SCOPED') and not any(item.get('sha256') in successful for item in result.get('evidence',[]) if isinstance(item,dict)):
      value['invalid_evidence'].append(gate+':'+result['status']+'_WITHOUT_BOUND_SUCCESS_RECEIPT')
    overruns=[name for name,s in q['stages'].items() if any(sum(j['cost'][k] for j in s['jobs'])>s['cap'][k] for k in ('cpu','wall'))]
    value['stage_cap_exceeded']=overruns
    if value['invalid_evidence']:value['status']='EVIDENCE_INVALID'
    elif any(x<0 for x in value['remaining_budget'].values()):value['status']='BUDGET_EXCEEDED'
    value['historical_stage_limit_events']=overruns
    # Keep a historical session/stage limit violation visible without making
    # it the permanent status of a still-funded, incomplete research goal.
    return value
   except (ValueError,KeyError,TypeError,OSError) as error:
    return {'status':'EVIDENCE_INVALID','invalid_evidence':[str(error)],'user_goal_achieved':False,'scope':'software evidence/ledger validation; no engineering PASS'}
 def _status_unlocked(self):
  q=read(self.root/'resources.json');matrix=read(self.root/'matrix.json');results=read(self.root/'results.json')
  invalid=[]
  for path,digest in self.plan['input_hashes'].items():
   if not Path(path).is_file() or sha(path)!=digest:invalid.append(path)
  for name,result in results.items():
   evidence=result.get('evidence')
   if result.get('status') in ('PASS','PASS_SCOPED') and (not isinstance(evidence,list) or not evidence):invalid.append(name+':'+result['status']+'_WITHOUT_EVIDENCE')
   if isinstance(evidence,list):
    for item in evidence:
     path=Path(item['path']);path=path if path.is_absolute() else self.root/path
     if not path.is_file() or sha(path)!=item['sha256']:invalid.append(str(path))
  for stage in q['stages'].values():
   for job in stage['jobs']:
    path=self.root/'jobs'/job['label']/'execution.json'
    if not path.is_file() or sha(path)!=job['receipt_sha256']:invalid.append(str(path))
  missing=[k for k,v in matrix['gates'].items() if v['required'] and results.get(k,{}).get('status')!='PASS']
  pending=[name for name,s in q['stages'].items() if s['state']!='SETTLED']
  hold=self._legacy_hold(q)
  remaining={k:self.plan['budget'][k]-q['spent'][k]-sum(max(s['cap'][k],sum(j['cost'][k] for j in s['jobs'])) for s in q['stages'].values() if s['state']!='SETTLED')-(hold if k=='cpu' else 0) for k in ('cpu','wall')}
  return {'status':'EVIDENCE_INVALID' if invalid else ('PASS' if not missing and not pending else 'NOT_PROVEN'),'invalid_evidence':invalid,'required_gates_incomplete':missing,'pending_stages':pending,'gates':results,'resources':q,'remaining_budget':remaining,'legacy_accounting_cpu_hold':hold,'accounting_scope':'measured minima with historical final-tail UNKNOWN; unused legacy CPU allowances held, not actual consumption','scope':matrix['scope'],'user_goal_achieved':False}
def main():
 p=argparse.ArgumentParser();p.add_argument('command',choices=['status','report']);p.add_argument('--root',required=True);a=p.parse_args();print(json.dumps(Workflow(a.root).status(),indent=2))
if __name__=='__main__':main()
