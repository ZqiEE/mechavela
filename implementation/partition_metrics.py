"""Area of actual mapped standard-cell instances; SRAM never assigned area."""
import re,time
from collections import Counter
from pathlib import Path
from .partition import load,name,file_hash
from .partition_session import Session,write,resource_total

def metrics(directory):
 s=Session(directory);begin=time.monotonic();cpu=resource_total();text=Path(s.plan['library']).read_text();areas={}
 for m in re.finditer(r'cell\s*\(\s*(\w+)\s*\)\s*\{',text):
  pos=m.end();depth=1;end=pos
  while end<len(text) and depth:
   if text[end]=='{':depth+=1
   elif text[end]=='}':depth-=1
   end+=1
  a=re.search(r'\barea\s*:\s*([0-9.eE+-]+)\s*;',text[pos:end])
  if a:areas[m[1]]=float(a[1])
 cached={};definitions={name(k):k for k in s.rows}
 for k in s.rows:
  if not s._cached(k):raise ValueError('complete mapped inventory required')
  m=load(s.cache/k/'mapped.json')['modules'][name(k)];cached[name(k)]=Counter(c['type'] for c in m.get('cells',{}).values())
 physical=set(s.plan['physical_boundaries'].values());computed={};visiting=set()
 def count(n):
  if n in computed:return computed[n]
  if n in visiting:raise ValueError('recursive mapped hierarchy')
  if n in physical:return Counter({n:1})
  if n in areas:return Counter({n:1})
  if n not in cached:raise ValueError('unknown area leaf '+n)
  visiting.add(n);v=Counter()
  for child,instances in cached[n].items():
   for leaf,number in count(child).items():v[leaf]+=instances*number
  visiting.remove(n);computed[n]=v;return v
 roles={}
 for role,root in s.plan['roots'].items():
  link=load(s.directory/('link-'+role+'.json'))
  if link['logic_mapping']!='PASS':raise ValueError('complete role link not passed')
  if file_hash(Path(link['evidence'])/'full.v')!=link['netlist_sha256']:raise ValueError('linked netlist identity mismatch')
  v=count(root);cells={c:n for c,n in v.items() if c in areas};ram={c:n for c,n in v.items() if c in physical}
  roles[role]=dict(mapped_standard_cell_area=sum(areas[c]*n for c,n in cells.items()),area_unit='Nangate45 library area, conventional um^2; no chip/die area claim',standard_cell_instances=sum(cells.values()),physical_sram_instances=sum(ram.values()),physical_sram_area='UNKNOWN',whole_chip_area='UNKNOWN',cell_histogram=cells,physical_sram_histogram=ram,critical_path='UNKNOWN',slack='UNKNOWN',dynamic_power='UNKNOWN',static_power='UNKNOWN',functional=s.status()['full_gpu_behavioral'],netlist_sha256=link['netlist_sha256'])
 result=dict(status='AREA_ONLY',scope='mapped standard-cell instance sum, explicit SRAM exclusion; not full GPU PPA',library_sha256=s.plan['library_sha256'],roles=roles)
 write(s.directory/'metrics.json',result);s._charge(resource_total()-cpu,time.monotonic()-begin,dict(stage='mapped_area',driver_cpu=resource_total()-cpu,wall=time.monotonic()-begin));return result
