"""Deterministic partition inventory and strict content-bound synthesis cache.

No RTL edits, model calls, technology estimates or whole-chip PASS inference.
"""
import copy,hashlib,json,re
from pathlib import Path

def encoded(v):return json.dumps(v,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
def digest(v):return hashlib.sha256(encoded(v)).hexdigest()
def file_hash(p):
 p=Path(p)
 if p.is_symlink():raise ValueError('symlink input rejected')
 h=hashlib.sha256()
 with p.open('rb') as f:
  for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
 return h.hexdigest()
def load(p):return json.loads(Path(p).read_text())
def blackbox(m):
 v=m.get('attributes',{}).get('blackbox',0)
 return bool(int(v,2) if isinstance(v,str) and v and set(v)<=set('01') else v)
def abi(m):return {n:(p['direction'],len(p['bits']),p.get('offset',0),p.get('upto',0),p.get('signed',0)) for n,p in m.get('ports',{}).items()}
def scrub(v):
 if isinstance(v,dict):return {k:scrub(x) for k,x in v.items() if k not in ('src','hdlname','scopename','creator')}
 if isinstance(v,list):return [scrub(x) for x in v]
 return v
def signatures(mods):
 result={};visiting=set()
 def key(n):
  if n in result:return result[n]
  if n in visiting:raise ValueError('recursive module hierarchy unsupported')
  visiting.add(n);m=scrub(mods[n]);cells=[]
  for c in m.get('cells',{}).values():
   if c['type'] in mods:c['type']=key(c['type'])
   cells.append(c)
  m['cells']=sorted(cells,key=encoded);m['netnames']=sorted(m.get('netnames',{}).values(),key=encoded)
  result[n]=digest(m);visiting.remove(n);return result[n]
 return key
def reachable(mods,root):
 seen=set();order=[]
 def visit(n):
  if n in seen:return
  seen.add(n)
  for c in mods[n].get('cells',{}).values():
   if c['type'] in mods:visit(c['type'])
  order.append(n)
 visit(root);return order
def name(key):
 if not re.fullmatch('[a-f0-9]{64}',key):raise ValueError('unsafe partition identity')
 return 'atoms_p_'+key
def normalize(mods,n,key):
 m=copy.deepcopy(mods[n])
 # Drop debug paths, retain named wires + init/keep/signed semantics for proofs.
 def debug_clean(v):
  if isinstance(v,dict):return {k:debug_clean(x) for k,x in v.items() if k not in ('src','hdlname','scopename','creator')}
  if isinstance(v,list):return [debug_clean(x) for x in v]
  return v
 m=debug_clean(m)
 for c in m.get('cells',{}).values():
  if c['type'] in mods:c['type']=name(key(c['type']))
 return m
def boundary(m):return dict(attributes={'blackbox':'1'},ports=m['ports'],parameter_default_values=m.get('parameter_default_values',{}),cells={},netnames={})
def check_mapped(source,mapped,definitions,library_cells):
 if abi(source)!=abi(mapped):raise ValueError('root ABI mismatch')
 if blackbox(mapped):raise ValueError('mapped logic is still blackbox')
 children={cn:c for cn,c in source.get('cells',{}).items() if c['type'] in definitions}
 for cn,c in children.items():
  mc=mapped.get('cells',{}).get(cn)
  if not mc or mc['type']!=c['type'] or mc.get('parameters',{})!=c.get('parameters',{}):raise ValueError('missing/wrong child instance')
  if {p:len(b) for p,b in c['connections'].items()}!={p:len(b) for p,b in mc['connections'].items()}:raise ValueError('child connection ABI mismatch')
 for c in mapped.get('cells',{}).values():
  if c['type'].startswith('$'):raise ValueError('unmapped generic cell')
  if c['type'] not in definitions and c['type'] not in library_cells:raise ValueError('unresolved standard/child cell')
 return dict(status='PASS',child_instances=len(children),cells=len(mapped.get('cells',{})))
def merge_unique(pairs):
 result={}
 for n,m in pairs:
  if n in result:raise ValueError('duplicate module definition')
  result[n]=m
 return result
def cache_key(source_key,tool,library,policy):return digest(dict(source=source_key,tool=tool,library=library,policy=policy))
def verify_cache(path,expected):
 path=Path(path);meta=load(path/'manifest.json')
 for k,v in expected.items():
  if meta.get(k)!=v:raise ValueError('cache binding mismatch: '+k)
 for fn,h in meta['files'].items():
  if not re.fullmatch('[a-zA-Z0-9_.-]+',fn) or file_hash(path/fn)!=h:raise ValueError('cache artifact mismatch')
 if meta['mapping']!='PASS':raise ValueError('cache mapping incomplete')
 return meta
