"""Trusted fixed recipe run only inside the existing resource sandbox."""
import json,os,subprocess,time,copy
from pathlib import Path
from partition import load,abi,blackbox,check_mapped
WORK=Path('/work');LIB='/library.lib';ENV={**os.environ,'PATH':'/tool/bin:/usr/bin'};ENV.pop('LD_LIBRARY_PATH',None)
q=load('/request.json');mode=q['mode'];top=q['top']
def execute(label,script):
 (WORK/(label+'.ys')).write_text(script);start=time.monotonic()
 with (WORK/(label+'.log')).open('wb') as f:r=subprocess.run(['/tool/bin/yosys','-s',str(WORK/(label+'.ys'))],env=ENV,stdout=f,stderr=subprocess.STDOUT)
 result=dict(stage=label,exit_code=r.returncode,wall_seconds=time.monotonic()-start)
 (WORK/(label+'-result.json')).write_text(json.dumps(result));return r.returncode
if mode in ('map','resume'):
 coarse=f'''read_json /source.json
hierarchy -check -top {top}
setattr -set keep 1 t:atoms_p_*
opt -fast -noff -purge
memory -nomap
opt_clean -purge
memory_map
opt -fast -noff -purge
techmap
opt -fast -noff -purge
dffunmap
dfflibmap -liberty {LIB}
write_rtlil /work/pre-abc.il
'''
 if mode=='resume':coarse='read_rtlil /checkpoint.il\n'
 code=execute('map',coarse+f'''abc -liberty {LIB} -script /scripts/abc_bounded.script
read_liberty -lib {LIB}
hilomap -hicell LOGIC1_X1 Z -locell LOGIC0_X1 Z -singleton
clean -purge
check -assert
write_json /work/mapped-all.json
select {top}
write_verilog -noattr -selected /work/mapped.v
''')
 if code:raise SystemExit(code)
 original=load('/source.json')['modules'];d=load('/work/mapped-all.json')['modules'];m=d[top]
 gate=check_mapped(original[top],m,original,set(q['library_cells']))
 (WORK/'mapped.json').write_text(json.dumps(dict(modules={top:m}),sort_keys=True,separators=(',',':')))
 (WORK/'gate.json').write_text(json.dumps(gate));(WORK/'mapped-all.json').unlink()
elif mode=='export':
 code=execute('export',f'''read_json /mapped.json
hierarchy -top {top}
select {top}
write_verilog -noattr -selected /work/mapped.v
''')
 if code:raise SystemExit(code)
elif mode=='proof':
 original=load('/source.json')['modules'];gold=copy.deepcopy(original[top]);gate=copy.deepcopy(load('/mapped.json')['modules'][top]);children={cn:c for cn,c in gold.get('cells',{}).items() if c['type'] in original};cuts=[]
 def cut(m):
  ports=m['ports'];cells=m['cells'];nets=m.get('netnames',{});bits=[b for c in cells.values() for bs in c['connections'].values() for b in bs if isinstance(b,int)]+[b for p in ports.values() for b in p['bits'] if isinstance(b,int)];nextbit=max(bits,default=0)+1;replace={}
  for idx,(cn,c) in enumerate(sorted(children.items())):
   mc=cells.pop(cn)
   for pn,p in sorted(original[c['type']]['ports'].items()):
    if pn not in mc['connections']:
     if p['direction']!='output':raise ValueError('missing child input')
     continue
    bs=mc['connections'][pn];key=f'__atoms_boundary_{idx}_{pn}'
    if key in ports:raise ValueError('boundary naming collision')
    if p['direction']=='output':
     free=[]
     for b in bs:
      if isinstance(b,int):
       if b in replace:raise ValueError('aliased child output not supported')
       replace[b]=nextbit
      free.append(nextbit);nextbit+=1
     ports[key]=dict(direction='input',bits=free)
    elif p['direction']=='input':ports[key]=dict(direction='output',bits=bs.copy())
    else:raise ValueError('inout cut unsupported')
  for p in ports.values():p['bits']=[replace.get(b,b) for b in p['bits']]
  for c in cells.values():
   for pn,bs in c['connections'].items():c['connections'][pn]=[replace.get(b,b) for b in bs]
  for n in nets.values():n['bits']=[replace.get(b,b) for b in n['bits']]
  # Retain actual internal wire/state names and init attrs; do not lose the
  # state relation as the earlier one-off diagnostic adapter did.
  return m
 g=cut(gold);m=cut(gate)
 if abi(g)!=abi(m):raise ValueError('proof cut ABI mismatch')
 (WORK/'gold.json').write_text(json.dumps(dict(modules={'gold':g})));(WORK/'gate.json').write_text(json.dumps(dict(modules={'gate':m})))
 code=execute('proof',f'''read_json /work/gold.json
hierarchy -check -top gold
memory -nomap
memory_map
design -stash gold
read_liberty -ignore_miss_func {LIB}
read_json /work/gate.json
hierarchy -check -top gate
flatten
design -stash gate
design -copy-from gold -as gold gold
design -copy-from gate -as gate gate
equiv_make gold gate equiv
hierarchy -top equiv
equiv_simple -seq 4
equiv_induct -seq 4
equiv_status -assert
''')
 (WORK/'functional.json').write_text(json.dumps(dict(status='PASS' if code==0 else 'NOT_PROVEN',scope='local conditional equivalence with exact child free outputs; child implementations need own certificates',constraints='no added reset/protocol/initial assumptions')))
 if code:raise SystemExit(code)
elif mode=='link':
 plan=load('/link-plan.json');lines=[f'read_liberty -lib {LIB}']
 for file in plan['netlists']:lines.append('read_verilog '+file)
 # SRAM placeholders are explicit source-exact ports, not silently excluded.
 lines+=['read_json /blackboxes.json',f'hierarchy -check -top {top}','check -assert','stat -liberty '+LIB,'write_verilog -noattr /work/full.v','write_rtlil /work/full.il']
 code=execute('link','\n'.join(lines)+'\n')
 if code:raise SystemExit(code)
 (WORK/'link-gate.json').write_text(json.dumps(dict(status='PASS',full_mapping='mapped standard-cell logic with declared physical SRAM boundaries',missing_logic=0,global_optimization=False,sram_model='NOT_SUPPORTED',functional='NOT_RUN: separate obligation coverage required')))
else:raise ValueError('unsupported fixed recipe')
