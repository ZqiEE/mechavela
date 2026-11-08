import json,hashlib,time,subprocess,uuid,os
from pathlib import Path
from core.ledger import ROOT,encoded
from core.sandbox import Sandbox

def main():
 evidence=ROOT/'evidence/G06';evidence.mkdir(parents=True,exist_ok=True);directory=evidence/uuid.uuid4().hex[:12];directory.mkdir()
 if not (evidence/'implementation.lock.json').exists():
  (evidence/'implementation.lock.json').write_bytes((ROOT/'implementation/toolchain.lock.json').read_bytes())
 lockpath=evidence/'implementation.lock.json';lock=json.loads(lockpath.read_text())
 home=Path(os.environ.get('ATOMS_PHYSICAL_HOME','/opt/atoms-g06'));library=home/'nangate45';design=home/'design';tools=home/'openroad'
 for name,record in lock['library_files'].items():
  if hashlib.sha256((library/name).read_bytes()).hexdigest()!=record['sha256']:raise ValueError('library lock changed')
 lock['installed_yosys']=subprocess.check_output(['yosys','-V'],text=True).strip()
 lock['yosys_binary_sha256']=hashlib.sha256(Path('/usr/bin/yosys').read_bytes()).hexdigest()
 lock['openroad_binary_sha256']=hashlib.sha256((tools/'usr/bin/openroad').read_bytes()).hexdigest()
 lock['openroad_version']='v2.0-16524-g4cc9d2133'
 lock['dependencies']=subprocess.check_output(['dpkg-query','-W','-f=${Package}=${Version}\n','yosys','berkeley-abc','libqt5charts5','tcl-tclreadline'],text=True).strip().splitlines()
 lockpath.write_text(json.dumps(lock,indent=2))
 (directory/'lock.json').write_bytes(lockpath.read_bytes())
 (evidence/'latest.json').write_text(json.dumps({'directory':str(directory)},indent=2))
 sandbox=Sandbox('/opt/atoms-g03/g06-sandboxes');summary={'status':'RUNNING','scope':'module','results':[],'external_spend_usd':0};start=time.monotonic();cpu=0.
 for name,policy in [('baseline',0),('candidate_A',1)]:
  case=directory/name;case.mkdir();request=Path('/opt/atoms-g06')/(directory.name+'-'+name+'.json');request.write_bytes(encoded({'policy':policy}))
  budget=dict(wall_time_seconds=300,cpu_time_seconds=min(900,1800-cpu),ram_bytes=4*1024**3,disk_bytes=1024**3,concurrency=1,max_candidates=1,log_bytes=8*1024**2,external_spend_limit_usd=0)
  result=sandbox.run(['python3','/scripts/executor.py'],[(str(ROOT/'implementation'),'/scripts'),(str(request),'/request.json'),(str(design),'/design'),(str(library),'/library'),(str(tools),'/openroad')],budget)
  cpu+=result['cpu_seconds'];(case/'driver.log').write_bytes(result.pop('log'))
  for filename,data in result.pop('artifacts').items():(case/filename).write_bytes(data)
  result.update(name=name,scope='module',budget=budget);(case/'execution.json').write_text(json.dumps(result,indent=2));summary['results'].append(result)
  summary.update(wall_seconds=time.monotonic()-start,cpu_seconds=cpu)
  if result['state']!='SUCCEEDED':summary['status']='FAIL'
  (directory/'summary.json').write_text(json.dumps(summary,indent=2))
  if summary['status']=='FAIL':raise RuntimeError('actual flow failed: '+str(case))
 summary['status']='TOOLS_COMPLETED_METRIC_VALIDATION_REQUIRED';(directory/'summary.json').write_text(json.dumps(summary,indent=2));print(json.dumps({'status':summary['status'],'evidence':str(directory)}))
if __name__=='__main__':main()
