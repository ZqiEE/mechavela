"""Checked RAM state correspondence for a Yosys combinational next-state proof.

This adapter does not prove equivalence. A complete SAT certificate over every
next-state bit and external output is required, in addition to these checks.
"""
import copy

COMBINATIONAL={'$and','$or','$xor','$xnor','$not','$eq','$ne','$eqx','$nex','$lt','$le','$gt','$ge','$logic_and','$logic_or','$logic_not','$mux','$pmux','$pos','$add','$sub','$shl','$shr','$sshl','$sshr','$reduce_and','$reduce_or','$reduce_xor','$reduce_xnor','$reduce_bool'}
def number(v):return int(v,2) if isinstance(v,str) else v
def extract(top,lib,dataw,size,physical,ff_semantics,lib_block):
 if top.get('processes') or top.get('memories'):raise ValueError('unlowered storage')
 if any('init' in x.get('attributes',{}) for x in [top,*top.get('netnames',{}).values(),*top['cells'].values()]):raise ValueError('initialization needs a separate admissibility proof')
 states=[]
 for i in range(size):
  bits=top['netnames'][f'mem[{i}]']['bits']
  if len(bits)!=dataw:raise ValueError('wrong state word width')
  states.extend(bits)
 states.extend(top['ports']['rdata']['bits'])
 if len(states)!=dataw*(size+1) or any(type(b) is not int for b in states) or len(set(states))!=len(states):raise ValueError('missing or aliased state')
 q_to_d={};storage={};qn=[];drivers={};checked=set()
 if physical:ff_semantics(lib)
 for name,c in top['cells'].items():
  p=c.get('parameters',{});pins=c['connections'];kind=c['type']
  if (physical and kind=='DFF_X1') or (not physical and kind=='$dff'):
   if physical:
    if p or set(pins)!={'CK','D','Q','QN'} or any(len(v)!=1 for v in pins.values()):raise ValueError('physical FF ABI differs')
    clock=pins['CK'];qn.extend(zip(pins['Q'],pins['QN']))
   else:
    if set(pins)!={'CLK','D','Q'} or number(p.get('CLK_POLARITY',0))!=1 or number(p.get('WIDTH',0))!=len(pins['Q']):raise ValueError('reference FF semantics differ')
    clock=pins['CLK']
   if clock!=top['ports']['clk']['bits'] or len(pins['D'])!=len(pins['Q']):raise ValueError('clock or state width differs')
   for q,d in zip(pins['Q'],pins['D']):
    if type(q) is not int or q in q_to_d:raise ValueError('state is constant or not bijective')
    q_to_d[q]=d
   storage[name]=c
  elif physical:
   if kind not in checked:
    import re
    body=lib_block(lib,r'\bcell\s*\(\s*"?'+re.escape(kind)+r'"?\s*\)')
    if re.search(r'\b(ff|latch)\s*\(',body):raise ValueError('additional physical state')
    checked.add(kind)
  elif kind not in COMBINATIONAL:raise ValueError('unsupported reference cell '+kind)
  for pin,bits in pins.items():
   if c['port_directions'][pin]=='output':
    for bit in bits:
     if type(bit) is int:drivers[bit]=drivers.get(bit,0)+1
 if set(q_to_d)!=set(states):raise ValueError('all storage must be covered exactly once')
 if any(drivers.get(b)!=1 for b in [*states,*(n for _,n in qn)]):raise ValueError('multiple or missing state drivers')
 if any(type(n) is not int or n in states for _,n in qn):raise ValueError('invalid complementary output')
 result=copy.deepcopy(top)
 for name in storage:del result['cells'][name]
 for i,(q,n) in enumerate(qn):
  key=f'__atoms_qn_{i}'
  if key in result['cells']:raise ValueError('generated cell collision')
  result['cells'][key]={'type':'$not','parameters':{'A_SIGNED':'0','A_WIDTH':'1','Y_WIDTH':'1'},'attributes':{},'port_directions':{'A':'input','Y':'output'},'connections':{'A':[q],'Y':[n]}}
 for n in ['__state','__nextstate']:
  if n in result['ports'] or n in result.get('netnames',{}):raise ValueError('generated port collision')
 result['ports']['__state']={'direction':'input','bits':states}
 result['ports']['__nextstate']={'direction':'output','bits':[q_to_d[b] for b in states]}
 # Compare only ports; no unproved internal combinational equality is a cut.
 result['netnames']={}
 return result,{'state_bits':len(states),'ff_cells':len(storage),'initial_state':'arbitrary corresponding states, bijective; no reset/protocol assumptions','state_order':'all mem words low-to-high bits then rdata','all_external_outputs_retained':True}
def pair(gold,gate,lib,dataw,size,ff_semantics,lib_block):
 if set(gold['ports'])!=set(gate['ports']):raise ValueError('interface names differ')
 for n,p in gold['ports'].items():
  other=gate['ports'][n]
  if p['direction'] not in ('input','output') or p['direction']!=other['direction'] or len(p['bits'])!=len(other['bits']):raise ValueError('interface ABI differs')
 g,gs=extract(gold,lib,dataw,size,False,ff_semantics,lib_block);t,ts=extract(gate,lib,dataw,size,True,ff_semantics,lib_block)
 return {'modules':{'gold':g,'gate':t}},{'gold':gs,'gate':ts,'proof':'NOT_PROVEN until all next-state and external outputs universally proved'}
