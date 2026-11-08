"""Source-exact RAM proof payload; caller reserves whole job before execution.

Uses frozen Yosys/ABC and previously reviewed state/support adapters. Does not
map implementation cells, add initial assumptions, or modify the GPU source.
"""
import json, os, sys, subprocess, hashlib
from pathlib import Path
sys.path[:0]=['/adapter','/proposal','/support','/type_adapter']
from combinational_cut import ff_semantics, block
from ram_observable_support import check
from next_ram_plan import relation_adapter
from ram_state_relation import pair

def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
env={**os.environ,'PATH':'/tool/bin:/usr/bin'};env.pop('LD_LIBRARY_PATH',None)
contract=json.loads(Path('/contract.json').read_text());p=contract['parameters']
assert sha('/gold.v')==contract['gold_sha256']
assert sha('/mapped.json')==contract['mapped_sha256']
assert sha('/library.lib')==contract['library_sha256']
def yosys(name,script):
 Path('/work/'+name+'.ys').write_text(script)
 with open('/work/'+name+'.log','w') as log:
  r=subprocess.run(['/tool/bin/yosys','-s','/work/'+name+'.ys'],env=env,stdout=log,stderr=subprocess.STDOUT)
 if r.returncode:raise SystemExit(r.returncode)

args=' '.join('-chparam '+k+' '+str(p[k]) for k in ['DATAW','SIZE','WRENW','ADDRW'])
yosys('reference','read_verilog -sv /gold.v\nhierarchy -check -top gold '+args+'\nproc\nmemory_map\nwrite_json /work/reference.json\n')
old=json.loads(Path('/work/reference.json').read_text())['modules']['gold']
assert not old.get('attributes',{}).get('blackbox'), 'formal reference must contain actual RAM behavior, not a blackbox declaration'
support=check(old,p['SIZE']);Path('/work/support.json').write_text(json.dumps(support,indent=2))
yosys('clean','read_json /work/reference.json\nopt_clean -purge\nwrite_json /work/gold-clean.json\n')
gold=json.loads(Path('/work/gold-clean.json').read_text())['modules']['gold']
assert old['ports']==gold['ports']
gate=json.loads(Path('/mapped.json').read_text())['modules']['gate']
lib=Path('/library.lib').read_text()
cuts,relation=relation_adapter(gold,gate,lib,contract['name'],p,pair,ff_semantics,block)
Path('/work/cuts.json').write_text(json.dumps(cuts))
Path('/work/relation.json').write_text(json.dumps(relation,indent=2))
yosys('miter','read_liberty -ignore_miss_func /library.lib\nread_json /work/cuts.json\nsetattr -mod -unset keep_hierarchy\nmiter -equiv -flatten gold gate miter\nhierarchy -check -top miter\ncheck -assert\ntechmap\nsetundef -anyseq\naigmap\nwrite_aiger -zinit -map /work/miter.aim -ywmap /work/miter.ywa /work/miter.aig\n')
header=list(map(int,Path('/work/miter.aig').read_bytes().split(b'\n',1)[0].split()[1:]))
assert len(header)==5 and header[2]==0 and header[3]==1
mapping=json.loads(Path('/work/miter.ywa').read_text())
assert not mapping.get('inits') and not mapping.get('clocks') and not mapping.get('asserts') and not mapping.get('assumes')
assert mapping['input_count']==header[1]
with open('/work/proof.log','w') as log:
 run=subprocess.run(['/tool/bin/yosys-abc','-c','read_aiger /work/miter.aig; strash; dc2; fraig; sat'],env=env,stdout=log,stderr=subprocess.STDOUT)
passed=run.returncode==0 and 'UNSATISFIABLE' in Path('/work/proof.log').read_text()
files=['reference.json','gold-clean.json','support.json','cuts.json','relation.json','miter.aig','miter.aim','miter.ywa','reference.ys','clean.ys','miter.ys','proof.log']
result={'status':'PASS_COMPOSITIONAL_INDUCTION' if passed else 'NOT_PROVEN','name':contract['name'],'parameters':p,'exit_code':run.returncode,'state_bits':p['DATAW']*(p['SIZE']+1),'input_count':header[1],'zero_latches':True,'single_complete_trigger':True,'no_hardware_assumptions':True,'initial_state':'arbitrary bijective corresponding states','scope':'exact source-to-mapped type, binary Yosys formal semantics; not timing or four-state events','contract_sha256':sha('/contract.json'),'input_hashes':{n:sha('/'+n) for n in ['gold.v','mapped.json','library.lib']},'artifact_hashes':{n:sha('/work/'+n) for n in files},'mapped_semantic_remapping':False,'zinit_scope':'AIG export of latch-free combinational miter; no hardware initialization'}
Path('/work/ram-type-certificate.json').write_text(json.dumps(result,indent=2))
raise SystemExit(0 if passed else 1)
