"""Offline integrity and quantitative checks; not a signature or physical certification."""
import csv
import json
import math
from .projects import digest, read, encode

def verify(store, pid):
    project = store.get(pid)
    for n,meta in project.get('attachments',{}).items():
        if digest(store.attachment(pid,n))!=meta['sha256']:raise ValueError('Historical attachment hash mismatch')
    checked = []
    for rev in project['history']:
        meta = {k: v for k, v in rev.items() if k != 'id'}
        if digest(encode(meta)) != rev['id']:
            raise ValueError('Revision identity mismatch')
        for n, sha in rev['assets'].items():
            if digest(store.asset(pid, n, rev['id']).encode()) != sha:
                raise ValueError('Revision asset hash mismatch')
    for run in project['runs']:
        folder = store.path(pid) / 'runs' / run['id']
        for n, sha in run['inputs'].items():
            if run['adapter']=='project.design/1':
                from .design_assets import relative
                relative(folder,n)
            raw = (folder / n).read_bytes()
            if run['adapter']=='project.design/1':
                from .design_assets import relative
                relative(folder,n)
                if not n.startswith('inputs/'):raise ValueError('Invalid native inspection snapshot path')
                original=n[len('inputs/'):];source_id=run.get('source_run')
                if source_id:
                    source=next((r for r in project['runs'] if r['id']==source_id),None)
                    if not source or source['revision']!=run['revision']:raise ValueError('Native artifact source revision mismatch')
                    source_name=original if original in source['inputs'] else 'work/'+original
                    if source_name not in source['inputs'] and source_name not in source.get('outputs',{}):raise ValueError('Unrecorded native artifact source')
                    expected=relative(store.path(pid)/'runs'/source_id,source_name).read_bytes()
                else:expected=store.asset(pid,original,run['revision']).encode()
            else:expected=store.asset(pid,n,run['revision']).encode()
            if digest(raw) != sha or raw != expected:
                raise ValueError('Run input lineage mismatch')
        for n, sha in run.get('outputs', {}).items():
            if digest((folder / n).read_bytes()) != sha:
                raise ValueError('Run output hash mismatch')
        for n, sha in run.get('tool_sources', {}).items():
            snapshot = folder / 'tool_sources' / (n.replace('\\', '/').split('/')[-1] + '.txt')
            if digest(snapshot.read_bytes()) != sha:
                raise ValueError('Tool source snapshot mismatch')
        if run['state'] == 'COMPLETED':
            if 'preparation' in run:
                preparation=read(folder/'cad-preparation.json')
                if preparation!=run['preparation'] or preparation['state']!='READY':
                    raise ValueError('CAD preparation evidence mismatch')
                setup=preparation['execution']
                if setup['exit_code']!=0 or setup['limit_event'] or not setup['all_job_processes_quiescent']:
                    raise ValueError('CAD preparation lacks clean resource receipt')
            receipt = run['execution']
            if type(receipt['exit_code']) is not int or receipt['exit_code'] != 0 or receipt['limit_event'] or not receipt['all_job_processes_quiescent']:
                raise ValueError('Completed run lacks clean execution receipt')
            result = read(folder / 'result.json')
            if result != run['result']:
                raise ValueError('Result record mismatch')
            if run['adapter']=='project.design/1':
                if result.get('adapter')!=run['adapter'] or result.get('levels',{}).get('requirements')!='NOT_EVALUATED':raise ValueError('Native asset inspection is not requirements acceptance')
                if read(folder/'design/report.json')!=result['design']:raise ValueError('Native design report mismatch')
                checked.append({'run':run['id'],'execution_state':run['state'],'requirements':'NOT_EVALUATED','checks':'NATIVE_ASSET_SOURCE_LINEAGE_OUTPUT_HASHES_AND_PROCESS_RECEIPT_ONLY'})
                continue
            if run['adapter']=='project.python/1':
                if result.get('adapter')!=run['adapter'] or result.get('levels',{}).get('requirements')!='NOT_EVALUATED' or result.get('execution_state')!='COMPLETED':raise ValueError('Code execution must not promote user-authored outputs to independent requirements PASS')
                checked.append({'run':run['id'],'execution_state':run['state'],'requirements':'NOT_EVALUATED','checks':'SOURCE_LINEAGE_OUTPUT_HASHES_AND_PROCESS_RECEIPT_ONLY'})
                continue
            if result.get('adapter') in ('cad.parametric/1','ngspice.circuit/1'):
                from . import cad,electrical
                (cad if result['adapter']=='cad.parametric/1' else electrical).verify(folder,result)
                checked.append({'run':run['id'],'execution_state':run['state'],'requirements':result['levels']['requirements'],'checks':'NATIVE_ENGINEERING_ARTIFACTS'})
                continue
            controller, req = read(folder / 'controller.json'), read(folder / 'requirements.json')
            if result.get('adapter')=='mujoco.control-graph/1':
                from .control_graph import verify as verify_graph
                verify_graph(folder,result)
                checked.append({'run':run['id'],'execution_state':run['state'],'requirements':result['levels']['requirements'],'checks':'FULL_STEP_MIXED_CONTROL_AND_NATIVE_OBSERVED_POINT_KINEMATICS'})
                continue
            if result.get('adapter')=='mujoco.joint-trajectory/1':
                from .trajectory import verify as verify_trajectory
                verify_trajectory(folder,result)
                checked.append({'run':run['id'],'execution_state':run['state'],'requirements':result['levels']['requirements'],'checks':'FULL_STEP_KEYFRAME_TARGET_JOINT_AND_BASE_TRACE'})
                continue
            if result.get('adapter')=='mujoco.joint-space/1':
                from .jointspace import verify as verify_jointspace
                verify_jointspace(folder,result)
                checked.append({'run':run['id'],'execution_state':run['state'],'requirements':result['levels']['requirements'],'checks':'FULL_STEP_NAMED_JOINT_AND_FLOATING_BASE_TRACE'})
                continue
            if result.get('adapter')=='bird.mission/1':
                from .bird import verify_evidence
                verify_evidence(folder,result,controller,req)
                checked.append({'run':run['id'],'execution_state':run['state'],'requirements':result['levels']['requirements'],
                                'checks':'ORIGINAL_BIRD_JUDGE_FROM_NATIVE_LOG; NOT_HARDWARE'})
                continue
            if result.get('adapter')=='mujoco.differential-drive/1':
                from .mobile import verify_evidence
                verify_evidence(folder,result,controller,req)
                checked.append({'run':run['id'],'execution_state':run['state'],
                                'requirements':result['levels']['requirements'],'checks':'MOBILE_FULL_TRACE'})
                continue
            if result.get('adapter') == 'mujoco.component-arm/1':
                from .component_evidence import verify_components
                verify_components(folder, result, controller, req)
                checked.append({'run':run['id'],'execution_state':run['state'],
                                'requirements':result['levels']['requirements'],'checks':'ALL_SCENARIO_SAMPLES'})
                continue
            with (folder / 'trace.csv').open() as f:
                rows = list(csv.DictReader(f))
            final = rows[-1]
            if any(not math.isfinite(float(v)) for row in rows for v in row.values()):
                raise ValueError('Trace contains nonfinite measurements')
            error = max(abs(float(final['q%d_rad' % (i+1)])-q) for i, q in enumerate(controller['target_rad']))
            if abs(error-result['measured']['final_joint_error_rad']) > 1e-12 or result['criteria'] != req:
                raise ValueError('Quantitative measurement mismatch')
            expected = error <= req['maximum'] and not result['warnings']
            if result['passed'] != expected:
                raise ValueError('Requirement verdict mismatch')
        checked.append({'run': run['id'], 'execution_state': run['state'], 'requirements': run.get('result', {}).get('levels', {}).get('requirements', 'NOT_EVALUATED')})
    return {'scope': 'File integrity, lineage, terminal execution receipt, final trace criterion; no authenticity/physical claim',
            'project': pid, 'revisions': len(project['history']), 'runs': checked, 'integrity': 'PASSED'}
