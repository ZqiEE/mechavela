import json,subprocess,os
from pathlib import Path
request=json.loads(Path('/request.json').read_text())
policy=request['policy'];assert type(policy) is int and policy in (0,1)
script='''read_verilog -sv /design/arbiter.sv /design/top.sv
chparam -set POLICY %d atoms_issue_select
hierarchy -check -top atoms_issue_select
synth -top atoms_issue_select -flatten
dfflibmap -liberty /library/lib/NangateOpenCellLibrary_typical.lib
abc -liberty /library/lib/NangateOpenCellLibrary_typical.lib
read_liberty -lib /library/lib/NangateOpenCellLibrary_typical.lib
hilomap -hicell LOGIC1_X1 Z -locell LOGIC0_X1 Z -singleton
clean
check -assert
stat -liberty /library/lib/NangateOpenCellLibrary_typical.lib
write_verilog -noattr /work/netlist.v
write_json /work/mapped.json
'''%policy
Path('/work/synth.ys').write_text(script)
env={**os.environ,'LD_LIBRARY_PATH':'/openroad/opt/or-tools/lib:/runtime','QT_QPA_PLATFORM':'offscreen'}
for name,command in [('synthesis',['yosys','-s','/work/synth.ys']),('route',['/openroad/usr/bin/openroad','-exit','/scripts/route.tcl'])]:
 if name=='route' and request.get('synthesis_only',False):break
 with Path('/work',name+'.log').open('wb') as out:
  run=subprocess.run(command,stdout=out,stderr=subprocess.STDOUT,env=env,timeout=120)
 print(name,run.returncode,flush=True)
 if run.returncode:raise RuntimeError(name+' failed')
