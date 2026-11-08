"""Coordinator-only witness conversion; prepare mode invokes no external tool.

Never uses a solver, edits RTL, or introduces assumptions. Caller must reserve
whole job and enforce cgroup CPU/memory caps; subprocess timeout is wall-only.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import time

def digest(data): return hashlib.sha256(data).hexdigest()

def validate_replay(text):
    rows=text.splitlines()
    if len(rows)!=5 or rows[-1]!='# DONE' or len(rows[0])!=1772 or any(len(r)!=1110 for r in rows[1:4]):
        raise ValueError('requires original master replay, not folded witness')
    if any(set(r)-set('01') for r in rows[:-1]):
        raise ValueError('unknown witness bits forbidden')
    if set(rows[0])-{'0'}:
        raise ValueError('must preserve certified master initialization')
    return rows

def conversion_commands(converter, out):
    return [[str(converter),'aiw2yw','--help'],
            [str(converter),'aiw2yw',str(out/'master-replay.aiw'),str(out/'master.ywa'),str(out/'master-replay.yw')],
            [str(converter),'yw2vcd','--help'],
            [str(converter),'yw2vcd',str(out/'master-replay.yw'),str(out/'master-replay.vcd')]]

def prepare(evidence, previous, out):
    import tarfile
    start,cpu=time.perf_counter(),time.process_time()
    review=json.loads((previous/'trace-review.json').read_text())
    if review['master_miter_trace']!='MASTER_AIG_COUNTEREXAMPLE_VALID':
        raise ValueError('master trace was not validated')
    archive=evidence/'jobs/cta-pdr/cta-evidence.tar.gz'
    raw=archive.read_bytes()
    if digest(raw)!=review['sha256']['archive']:
        raise ValueError('archive identity changed')
    master=(evidence/'jobs/cta-pdr/cta-master.aig').read_bytes()
    if digest(master)!=review['sha256']['master']:
        raise ValueError('master identity changed')
    replay=(previous/'master-replay.aiw').read_text()
    validate_replay(replay)
    if replay!=review['master_replay_aiw']:
        raise ValueError('replay differs from evaluated master witness')
    if out.exists():raise ValueError('fresh directory required; do not overwrite evidence')
    out.mkdir(parents=True)
    (out/'master.aig').write_bytes(master)
    (out/'master-replay.aiw').write_text(replay)
    required={'master.aim':('design_aiger.aim','aim'),
              'master.ywa':('design_aiger.ywa','ywa'),
              'partition.sv':('eqy/partitions/cta.__atoms_boundary_0_clk.sv','partition_sv')}
    with tarfile.open(archive) as tar:
        for name,(suffix,key) in required.items():
            members=[m for m in tar.getmembers() if m.name.endswith(suffix) and m.isfile()]
            if len(members)!=1 or members[0].size>1_000_000:
                raise ValueError('missing/ambiguous/oversized archive member')
            data=tar.extractfile(members[0]).read()
            if digest(data)!=review['sha256'][key]:
                raise ValueError('source map identity mismatch')
            (out/name).write_bytes(data)
    request={'status':'PREPARED_NOT_CONVERTED','engineering_proof':'NOT_RUN',
             'master_sha256':digest(master),'master_replay_sha256':digest(replay.encode()),
             'mapping':'master mapping only; never use folded initial state',
             'property12':review['property12_witness_source'],
             'RTL_validation':'NOT_VALIDATED','cut_spuriousness':'UNKNOWN',
             'new_assumptions':0,'cpu_seconds':time.process_time()-cpu,
             'wall_seconds':time.perf_counter()-start}
    (out/'request.json').write_text(json.dumps(request,indent=2))
    return request

def execute(converter,out):
    # Explicitly requested by coordinator only. No retry or dynamic shell.
    tool=converter.resolve(strict=True)
    commands=conversion_commands(tool,out)
    receipts=[];start=time.monotonic()
    for i,cmd in enumerate(commands):
        remaining=10-(time.monotonic()-start)
        if remaining<=0:raise TimeoutError('whole conversion wall cap')
        try:
            with (out/f'conversion-{i}.log').open('wb') as log:
                run=subprocess.run(cmd,stdout=log,stderr=subprocess.STDOUT,timeout=remaining,check=False)
        except subprocess.TimeoutExpired:
            receipts.append({'command':cmd,'exit_code':None,'status':'WALL_TIMEOUT'})
            break
        receipts.append({'command':cmd,'exit_code':run.returncode})
        if run.returncode!=0:break
    result={'status':'CONVERSION_FINISHED_REQUIRES_SOURCE_REVIEW' if len(receipts)==4 and all(r.get('exit_code')==0 for r in receipts) else 'CONVERSION_INCOMPLETE',
            'tool_sha256':digest(tool.read_bytes()),'receipts':receipts,
            'wall_seconds':time.monotonic()-start,'CPU_cgroup_receipt':'REQUIRED_FROM_COORDINATOR',
            'RTL_counterexample':'NOT_VALIDATED','cut_spuriousness':'UNKNOWN'}
    (out/'conversion.json').write_text(json.dumps(result,indent=2))
    return result

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--evidence',type=Path,required=True)
    ap.add_argument('--previous',type=Path,required=True);ap.add_argument('--out',type=Path,required=True)
    ap.add_argument('--execute',action='store_true');ap.add_argument('--converter',type=Path)
    args=ap.parse_args()
    if args.execute and args.converter is None:ap.error('--execute requires exact existing --converter path')
    result=prepare(args.evidence,args.previous,args.out)
    if args.execute:result=execute(args.converter,args.out)
    print(json.dumps(result,indent=2))
