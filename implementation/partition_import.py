"""Import already mapped, content-bound artifacts without executing tools."""
import time,copy
from pathlib import Path
from .partition import *
from .partition_session import Session,write,resource_total

def import_artifacts(directory,artifacts):
 s=Session(directory);start=time.monotonic();cpu=resource_total();sources={};count=0;skipped=[]
 for role,b in s.plan['roles'].items():
  if file_hash(b['json'])!=b['json_sha256']:raise ValueError('original source changed')
  mods=load(b['json'])['modules'];sources[role]=(mods,signatures(mods))
 for a in artifacts:
  if a['role'] not in sources:raise ValueError('unknown source role')
  mods,key=sources[a['role']];source_module=a['source_module'];k=key(source_module)
  if k not in s.rows:skipped.append(dict(artifact=a,reason='source not reachable'));continue
  if s._cached(k):continue
  if file_hash(a['mapped'])!=a['mapped_sha256']:raise ValueError('historic artifact hash mismatch')
  for path,h in a.get('certificates',{}).items():
   if file_hash(path)!=h:raise ValueError('historic certificate hash mismatch')
  old=load(a['mapped'])['modules'][a.get('mapped_module',source_module)];mapped=normalize({**mods,'__import':old},'__import',key)
  # normalize uses only source child declarations; the import root does not
  # occur as a child. Its source-key relationship is explicit in this manifest.
  source=load(s.directory/'inputs'/(k+'.json'))['modules']
  try:gate=check_mapped(source[name(k)],mapped,source,s.library)
  except ValueError as e:skipped.append(dict(source_module=source_module,reason=str(e)));continue
  cache=s.cache/k;cache.mkdir(exist_ok=True);write(cache/'mapped.json',dict(modules={name(k):mapped}));write(cache/'gate.json',gate)
  meta={**s.identity(k),'mapping':'PASS','functional':'PASS' if a.get('functional')=='PASS' else 'NOT_RUN','files':{fn:file_hash(cache/fn) for fn in ('mapped.json','gate.json')},'imported':a,'mapping_recipe':'historic recipe retained, only source-exact module/type renaming; compatible port/state/technology binding checked'};write(cache/'manifest.json',meta);count+=1
 elapsed=time.monotonic()-start;helper=resource_total()-cpu;s._charge(helper,elapsed,dict(stage='import',driver_cpu=helper,wall=elapsed,imported=count))
 write(s.directory/'import-result.json',dict(imported=count,skipped=skipped,cpu=helper,wall=elapsed));return s.status()
