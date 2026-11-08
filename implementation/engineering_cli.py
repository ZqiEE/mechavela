"""Coordinator-only engineering CLI. No budget creation or gate-result writes.

Run as python -m implementation.engineering_cli after coordinator integration.
Private Workflow reservation/settlement helpers are called only under its lock.
"""
import argparse,hashlib,json,math,re
from pathlib import Path,PurePosixPath
from .complete_validation import Workflow,read,write,sha

def identity(value):
 return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
def safe(value):
 if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}',value):raise ValueError('unsafe stage/job/artifact name')
 return value
def file_binding(path,digest):
 p=Path(path)
 if any(part.lower() in ('.ssh','.aws','.azure','.kube','.codex','credentials') or part.lower().startswith('.env') for part in p.parts):raise ValueError('credential/private resource rejected')
 if not p.is_absolute() or p.is_symlink() or not p.is_file() or not isinstance(digest,str) or sha(p)!=digest:raise ValueError('file binding mismatch; directory binds unsupported')
 return str(p.resolve())
class EngineeringCLI:
 def __init__(self,root,workflow=None):self.w=workflow or Workflow(root);self.root=Path(root)
 def descriptor(self,stage):return self.root/'cli-plans'/(safe(stage)+'.json')
 def validate(self,spec):
  if set(spec)!={'stage','stage_cap','jobs','identity_hashes'}:raise ValueError('stage/jobs/cap/identity_hashes required; no new budget fields')
  safe(spec['stage']);self.w.check_cap(spec['stage_cap'])
  if not isinstance(spec['identity_hashes'],dict):raise ValueError('identity hash map required')
  for path,digest in spec['identity_hashes'].items():
   file_binding(path,digest)
   if path in self.w.plan.get('input_hashes',{}) and self.w.plan['input_hashes'][path]!=digest:raise ValueError('identity contradicts frozen workflow')
  if not isinstance(spec['jobs'],list) or not spec['jobs']:raise ValueError('nonempty jobs required')
  labels=set();totals={'cpu':0,'wall':0}
  for j in spec['jobs']:
   if set(j)!={'label','payload','payload_sha256','binds','cap'}:raise ValueError('exact job schema required')
   safe(j['label'])
   if j['label'] in labels:raise ValueError('duplicate job label')
   labels.add(j['label']);self.w.check_cap(j['cap'])
   file_binding(j['payload'],j['payload_sha256'])
   if not isinstance(j['binds'],list):raise ValueError('bind list required')
   targets=set()
   for b in j['binds']:
    if set(b) not in ({'source','target','sha256'},{'source','target'}):raise ValueError('exact bind schema required')
    source=Path(b['source']);p=PurePosixPath(b['target'])
    if not p.is_absolute() or len(p.parts)<2 or '..' in p.parts or str(p)!=b['target'] or b['target'] in targets or p.parts[1] in ('payload.py','work','tmp','proc','dev','usr','bin','lib','lib64'):raise ValueError('unsafe or duplicate bind target')
    if source.is_dir():
     approved=self.w.plan.get('approved_tool_directories',[])
     if set(b)!={'source','target'} or not source.is_absolute() or source.is_symlink() or b['target'] not in ('/tool','/formal','/archives') or not (str(source.resolve()).startswith('/opt/atoms-g03/') or str(source.resolve()) in approved):raise ValueError('only dedicated readonly tool/formal/archive directories in the frozen authorization plan supported')
     if b['target']=='/archives' and not str(source.resolve()).startswith('/opt/atoms-g03/reality-archives/'):raise ValueError('archive cache must use dedicated Linux reality directory')
     if not any(Path(path).is_relative_to(source.resolve()) for path in spec['identity_hashes']):raise ValueError('directory requires explicit executable identities')
    else:file_binding(b['source'],b['sha256'])
    targets.add(b['target'])
   for k in totals:totals[k]+=j['cap'][k]
  if any(totals[k]>spec['stage_cap'][k] for k in totals):raise ValueError('whole-stage cap does not cover all jobs')
 def plan(self,spec):
  self.validate(spec)
  with self.w.locked():
   self.w._guard();p=self.descriptor(spec['stage']);q=read(self.root/'resources.json')
   if p.exists() or spec['stage'] in q['stages']:raise ValueError('plan/stage already exists; immutable plan')
   v={'spec':spec,'workflow_plan_sha256':sha(self.root/'plan.json')};v['descriptor_sha256']=identity(v)
   p.parent.mkdir(exist_ok=True);write(p,v)
  return {'status':'PLANNED_NOT_RESERVED','stage':spec['stage'],'descriptor_sha256':v['descriptor_sha256']}
 def load(self,stage,verify_inputs=True):
  v=read(self.descriptor(stage));expected=v['descriptor_sha256'];body={k:x for k,x in v.items() if k!='descriptor_sha256'}
  if identity(body)!=expected or v['workflow_plan_sha256']!=sha(self.root/'plan.json') or v['spec']['stage']!=stage:raise ValueError('immutable plan binding changed')
  if verify_inputs:self.validate(v['spec'])
  return v
 def ledger(self,stage,v):
  q=read(self.root/'resources.json');self.w._validate_ledger(q);self.w._verify_jobs(q);s=q['stages'][stage]
  if s.get('cli_plan_sha256')!=v['descriptor_sha256'] or s['cap']!=v['spec']['stage_cap']:raise ValueError('stage/CLI plan binding changed')
  if s['state'] in ('RUNNING','UNKNOWN_INTERRUPTED') or s.get('pending'):raise ValueError('UNKNOWN_INTERRUPTED: no replay or settlement')
  allowed={j['label'] for j in v['spec']['jobs']}
  if any(j['label'] not in allowed for j in s['jobs']):raise ValueError('unplanned receipt')
  return q,s
 def reserve(self,stage):
  with self.w.locked():
   self.w._guard();v=self.load(stage);self.w._reserve(stage,v['spec']['stage_cap'])
   q=read(self.root/'resources.json');q['stages'][stage]['cli_plan_sha256']=v['descriptor_sha256'];write(self.root/'resources.json',q)
  return {'status':'WHOLE_STAGE_RESERVED','stage':stage}
 def run(self,stage,max_jobs=1,resume=False):
  if type(max_jobs) is not int or not 1<=max_jobs<=500:raise ValueError('positive bounded max-jobs required')
  with self.w.locked():
   self.w._guard();v=self.load(stage,False);_,s=self.ledger(stage,v)
   if s['state']=='SETTLED':return {'status':'ALREADY_SETTLED','stage':stage,'launched':0}
   self.validate(v['spec'])
   if s['jobs'] and not resume:raise ValueError('existing checkpoint: use resume')
   if any(j['state']!='SUCCEEDED' for j in s['jobs']):
    return {'status':'FAILED_CHECKPOINT_REQUIRES_REVIEW','stage':stage,'launched':0,'engineering_acceptance':'NOT_EVALUATED','user_goal_achieved':False}
  launched=0
  for job in v['spec']['jobs']:
   with self.w.locked():
    self.w._guard();v=self.load(stage);_,s=self.ledger(stage,v)
    if s['state']=='SETTLED':break
    if any(j['label']==job['label'] for j in s['jobs']):continue  # success AND failure are never replayed
   if launched>=max_jobs:break
   # Workflow owns project lock, accounting, interrupted reservation, and receipt sealing.
   result,_=self.w.execute(stage,job['label'],Path(job['payload']),[(b['source'],b['target']) for b in job['binds']],job['cap']);launched+=1
   if result['state']!='SUCCEEDED':break
  with self.w.locked():
   self.w._guard();v=self.load(stage);_,s=self.ledger(stage,v);seen={j['label'] for j in s['jobs']}
   pending=[j['label'] for j in v['spec']['jobs'] if j['label'] not in seen]
   failures=[j['label'] for j in s['jobs'] if j['state']!='SUCCEEDED']
   if not pending and s['state']!='SETTLED':self.w._settle(stage)
  return {'status':('CHECKPOINT' if pending else 'EXECUTION_COMPLETE')+('_FAILED' if failures else ''),'stage':stage,'launched':launched,'not_run':pending,'failed':failures,'engineering_acceptance':'NOT_EVALUATED','user_goal_achieved':False}
 def status(self):
  value=self.w.status();resources=value.get('resources',{});stages=resources.get('stages',{})
  return {k:value.get(k) for k in ['status','invalid_evidence','required_gates_incomplete','pending_stages','remaining_budget','legacy_accounting_cpu_hold','historical_stage_limit_events','scope','user_goal_achieved']}|{
   'goal_status':'IN_PROGRESS','best_candidate':self.w.plan['roles']['candidate']['candidate_id'],
   'active_execution':any(s['state']=='RUNNING' for s in stages.values()),
   'interrupted_cost_unresolved':any(s['state']=='UNKNOWN_INTERRUPTED' for s in stages.values()),
   'gates':{k:{n:r.get(n) for n in ['status','scope','blocked_reason'] if n in r} for k,r in value.get('gates',{}).items()},
   'next_required_action':'Close remaining exact RAM and logic proof dependencies; complete constrained STA and matching-activity power within authorized complete-stage budgets',
   'budget_scope':'historical stage limit events retained separately; no new authorization'}
 def recover(self,stage):
  with self.w.locked():
   self.w._guard();v=self.load(stage,False);q=read(self.root/'resources.json');self.w._validate_ledger(q);self.w._verify_jobs(q);s=q['stages'][stage]
   if s.get('cli_plan_sha256')!=v['descriptor_sha256']:raise ValueError('stage plan binding changed')
   if s['state'] not in ('RUNNING','UNKNOWN_INTERRUPTED'):raise ValueError('no interrupted stage')
   pending=s.get('pending')
   if not pending:raise ValueError('NO_FINAL_RECEIPT: reservation retained; resource reconciliation required')
   label=safe(pending['label']);j=next(j for j in v['spec']['jobs'] if j['label']==label)
   p=self.root/'jobs'/label/'execution.json'
   if not p.is_file() or p.is_symlink():raise ValueError('NO_FINAL_RECEIPT: cannot guess cost or replay interrupted computation')
   r=read(p);binding={'stage':stage,'label':label,'cap':pending['cap'],'payload_sha256':pending['payload_sha256']}
   if r.get('execution_binding')!=binding or j['payload_sha256']!=pending['payload_sha256'] or j['cap']!=pending['cap']:raise ValueError('interrupted receipt identity mismatch')
   self.w._numbers(r['controller_cost'])
   final=r.get('final_cgroup_cpu_seconds')
   if r.get('final_cgroup_quiescent') is not True or type(final) not in (float,int) or not math.isfinite(final) or final<0 or r['controller_cost']['cpu']<final:raise ValueError('final resource evidence unavailable')
   if r['state'] not in ('SUCCEEDED','FAILED','BUDGET_EXCEEDED','CANCELLED') or (r['state']=='SUCCEEDED' and r['exit_code']!=0):raise ValueError('invalid final execution state')
   if not isinstance(r.get('artifact_sha256'),dict):raise ValueError('no sealed artifact identities')
   for name,digest in r['artifact_sha256'].items():
    safe(name);f=p.parent/name
    if f.is_symlink() or not f.is_file() or sha(f)!=digest:raise ValueError('interrupted artifact identity mismatch')
   if any(x['label']==label for x in s['jobs']):raise ValueError('receipt already charged')
   s['jobs'].append({'label':label,'state':r['state'],'cost':r['controller_cost'],'receipt_sha256':sha(p)})
   s.pop('pending');s['state']='RESERVED';s['recovered_receipt']=label;write(self.root/'resources.json',q)
  return {'status':'RECOVERED_SEALED_RECEIPT','launched':0,'label':label,'engineering_acceptance':'NOT_EVALUATED'}
 def abort(self,stage):
  with self.w.locked():
   self.w._guard();v=self.load(stage);q,s=self.ledger(stage,v)
   if s['state']=='SETTLED':return {'status':'ALREADY_SETTLED','launched':0}
   missing=[j['label'] for j in v['spec']['jobs'] if not any(r['label']==j['label'] for r in s['jobs'])]
   s['aborted']=True;s['not_run']=missing;write(self.root/'resources.json',q)
   self.w._settle(stage)
  return {'status':'ABORTED','not_run':missing,'engineering_acceptance':'NOT_EVALUATED','launched':0}
 def artifact(self,stage,label=None,name=None):
  if label is not None:safe(label)
  if name is not None:safe(name)
  with self.w.locked():
   self.w._guard();v=self.load(stage,False);_,s=self.ledger(stage,v)
   if label is None:
    if name is not None:raise ValueError('--name requires --label')
    return {'receipts':[{'label':j['label'],'state':j['state'],'receipt_sha256':j['receipt_sha256']} for j in s['jobs']],'scope':'sealed receipt index only'}
   if not any(j['label']==label for j in s['jobs']):raise ValueError('no sealed job receipt')
   if name is None:
    receipt=read(self.root/'jobs'/label/'execution.json')
    return {'label':label,'artifact_sha256':receipt.get('artifact_sha256',{}),'scope':'hash index from sealed receipt; artifact contents not inspected'}
   p=self.root/'jobs'/label/name
   if p.is_symlink() or not p.is_file():raise ValueError('artifact unavailable')
   receipt=read(self.root/'jobs'/label/'execution.json');digest=receipt.get('artifact_sha256',{}).get(name)
   if not digest or sha(p)!=digest:raise ValueError('artifact missing sealed hash or changed')
   return {'path':str(p.resolve()),'sha256':digest,'size':p.stat().st_size,'scope':'receipt-bound artifact; engineering acceptance separately evaluated'}
def main(argv=None):
 p=argparse.ArgumentParser();p.add_argument('command',choices=['plan','reserve','run','resume','recover','abort','status','report','artifact']);p.add_argument('--root',required=True);p.add_argument('--manifest');p.add_argument('--label');p.add_argument('--name');p.add_argument('--max-jobs',type=int,default=1);a=p.parse_args(argv)
 try:
  c=EngineeringCLI(a.root)
  if a.command not in ('status','report'):
   if not a.manifest:raise ValueError('--manifest required')
   spec=read(a.manifest);stage=safe(spec['stage'])
   if a.command=='plan':c.validate(spec)
   elif c.load(stage,False)['spec']!=spec:raise ValueError('manifest differs from frozen CLI plan')
  if a.command=='plan':r=c.plan(spec)
  elif a.command=='reserve':r=c.reserve(stage)
  elif a.command in ('run','resume'):r=c.run(stage,a.max_jobs,a.command=='resume')
  elif a.command=='artifact':r=c.artifact(stage,a.label,a.name)
  elif a.command=='abort':r=c.abort(stage)
  elif a.command=='recover':r=c.recover(stage)
  elif a.command=='report':r=c.w.status()
  else:r=c.status()
  print(json.dumps(r,allow_nan=False));return 0
 except (ValueError,OSError,KeyError,TypeError,RuntimeError) as error:
  print(json.dumps({'status':'REJECTED','reason':str(error),'engineering_acceptance':'NOT_EVALUATED'}));return 1
if __name__=='__main__':raise SystemExit(main())
