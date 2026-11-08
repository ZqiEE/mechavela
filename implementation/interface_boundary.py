"""Legalize passive SV interface aliases for independent synthesis only.

An un-driven, un-consumed output member can be a shared interface wire driven
by the parent. Remove only that redundant partition pin/parent connection,
after tracing a real driver in EVERY parent instance. Never invent a value.
"""
import copy
from .partition import blackbox,reachable

def analyze(mods,root):
 order=reachable(mods,root);driven={};consumed={};parents={n:[] for n in order};candidates={}
 for parent in order:
  for cn,c in mods[parent].get('cells',{}).items():
   if c['type'] in parents:parents[c['type']].append((parent,cn,c))
 for n in order:
  m=mods[n];driver={b for p in m.get('ports',{}).values() if p['direction'] in ('input','inout') for b in p['bits'] if isinstance(b,int)};consumer=set()
  if blackbox(m):driver.update(b for p in m['ports'].values() if p['direction'] in ('output','inout') for b in p['bits'] if isinstance(b,int))
  for c in m.get('cells',{}).values():
   child=mods.get(c['type']);directions=c.get('port_directions',{})
   for pn,bits in c.get('connections',{}).items():
    direction=child.get('ports',{}).get(pn,{}).get('direction') if child else directions.get(pn)
    if direction in ('input','inout'):consumer.update(b for b in bits if isinstance(b,int))
    if direction in ('output','inout'):
     if child:
      cb=child['ports'][pn]['bits'];driver.update(b for idx,b in enumerate(bits) if isinstance(b,int) and idx<len(cb) and (not isinstance(cb[idx],int) or cb[idx] in driven[c['type']]))
     else:driver.update(b for b in bits if isinstance(b,int))
  driven[n]=driver;consumed[n]=consumer
  if blackbox(m):continue
  for pn,p in m.get('ports',{}).items():
   if '.' not in pn or p['direction']!='output' or not p['bits'] or not all(isinstance(b,int) and b not in driver and b not in consumer for b in p['bits']):continue
   aliases=[other for other,op in m['ports'].items() if other!=pn and set(p['bits'])&set(op['bits'])]
   if not aliases:candidates.setdefault(n,[]).append(pn)
 verified={};rejected=[];traces=[]
 for n,pins in candidates.items():
  for pn in pins:
   invocations=parents[n];good=bool(invocations);instances=[]
   for parent,cn,c in invocations:
    bits=c.get('connections',{}).get(pn)
    if bits is None:instances.append(dict(parent=parent,instance=cn,status='UNCONNECTED'));continue
    ok=all(not isinstance(b,int) or b in driven[parent] for b in bits);good&=ok
    instances.append(dict(parent=parent,instance=cn,parent_bits=bits,status='EXTERNALLY_DRIVEN' if ok else 'NO_REAL_DRIVER'))
   if good:verified.setdefault(n,[]).append(pn);traces.append(dict(module=n,port=pn,width=len(mods[n]['ports'][pn]['bits']),local_driver=False,local_consumers=0,parent_instances=instances,transformation='remove passive port and matching redundant cell-pin only; keep original parent net/drivers/fanouts'))
   else:rejected.append(dict(module=n,port=pn,parent_instances=instances,reason='no verified external driver in every instance; no transformation'))
 return dict(verified=verified,traces=traces,rejected=rejected)

def apply(mods,analysis):
 result=copy.deepcopy(mods);pins=analysis['verified']
 for n,names in pins.items():
  for pn in names:del result[n]['ports'][pn]
 for m in result.values():
  for c in m.get('cells',{}).values():
   for pn in pins.get(c['type'],[]):c.get('connections',{}).pop(pn,None);c.get('port_directions',{}).pop(pn,None)
 return result
