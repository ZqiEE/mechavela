"""Exact child cuts for conditional local equivalence; no reset assumptions."""
import copy
from .partition import abi

def cut_pair(source, mapped, top):
    gold=copy.deepcopy(source[top]); gate=copy.deepcopy(mapped[top])
    children={n:c for n,c in gold.get('cells',{}).items() if c['type'] in source}
    obligations=[]
    def cut(m):
        bits=[b for c in m['cells'].values() for bs in c['connections'].values() for b in bs if isinstance(b,int)]
        bits += [b for p in m['ports'].values() for b in p['bits'] if isinstance(b,int)]
        nextbit=max(bits,default=0)+1; replace={}
        for idx,(cn,c) in enumerate(sorted(children.items())):
            actual=m['cells'].pop(cn)
            if actual['type']!=c['type']:raise ValueError('child changed')
            for pn,p in sorted(source[c['type']]['ports'].items()):
                if pn not in actual['connections']:
                    if p['direction']!='output':raise ValueError('missing child input')
                    continue
                bs=actual['connections'][pn]; n=f'__atoms_boundary_{idx}_{pn}'
                if n in m['ports']:raise ValueError('port collision')
                if p['direction']=='output':
                    free=[]
                    for b in bs:
                        if isinstance(b,int):
                            if b in replace:raise ValueError('aliased child output')
                            replace[b]=nextbit
                        free.append(nextbit);nextbit+=1
                    m['ports'][n]={'direction':'input','bits':free}
                elif p['direction']=='input':m['ports'][n]={'direction':'output','bits':bs.copy()}
                else:raise ValueError('inout unsupported')
        for p in m['ports'].values():p['bits']=[replace.get(b,b) for b in p['bits']]
        for c in m['cells'].values():
            for pn,bs in c['connections'].items():c['connections'][pn]=[replace.get(b,b) for b in bs]
        for n in m.get('netnames',{}).values():n['bits']=[replace.get(b,b) for b in n['bits']]
        return m
    for cn,c in children.items():obligations.append({'instance':cn,'type':c['type'],'condition':'child implementation requires independent equivalence; free output shared, every connected input compared'})
    g=cut(gold);m=cut(gate)
    if abi(g)!=abi(m):raise ValueError('cut ABI mismatch')
    return {'modules':{top:g}},{'modules':{top:m}},obligations
